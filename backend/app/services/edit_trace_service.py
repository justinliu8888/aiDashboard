"""编辑执行追踪：逐圈事件、脱敏与失败摘要（契约 0003 §2）。

为什么要有这一层：

* 「查证遇到技术问题」这种笼统提示掩盖了**实际发生了什么**。本模块把
  每一圈的工具调用、提交、试做、候选登记分别记成**结构化事件**，
  失败时才能说清「失败在哪一步、已知原因是什么、修改是否生效」。
* 事件**不打印模型完整内部思维链**，也不带凭证；只留定位所需的
  错误类别、工具/步骤与脱敏摘要。

三条硬纪律：

1. **上下文是请求作用域**（``ContextVar``）：并行/异步工具**不串线**。
2. **未发生 / 未取得 / 裁剪分别标记**：不把「没拿到」写成「没有」，
   也不把「截断了」写成「完整内容」。
3. **失败类别互不混用**（见契约 §2.4 的取值表）：通用兜底只用
   ``unknown``，并在摘要里写明「原因尚未确定」。
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import os
import re
import threading
import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# 失败类别（契约 §2.4）
# --------------------------------------------------------------------------

#: 模型接口不可用 / 认证失败。
ERR_MODEL_UNAVAILABLE = "model_unavailable"
#: 模型请求超时。
ERR_MODEL_TIMEOUT = "model_timeout"
#: 模型返回错误。
ERR_MODEL_ERROR = "model_error"
#: 事实工具查不到目标 / 返回不支持。
ERR_TOOL_LOOKUP_FAILED = "tool_lookup_failed"
#: 工具执行报错。
ERR_TOOL_ERROR = "tool_error"
#: 模型输出或提交参数不合法。
ERR_PARAMS_INVALID = "params_invalid"
#: 预执行某步校验失败。
ERR_TRIAL_FAILED = "trial_failed"
#: 接受时版本/权限/锁/正式提交失败。
#: 取值与契约 0003 §2.4 的表**逐字一致**（``acceptance_failed``）。
ERR_ACCEPT_FAILED = "acceptance_failed"
#: 预算耗尽仍未提交。
ERR_BUDGET_EXHAUSTED = "budget_exhausted"
#: 循环结束但未提交（预算未耗尽）。
ERR_NOT_SUBMITTED = "not_submitted"
#: 无法进一步定位（**唯一**的通用兜底）。
ERR_UNKNOWN = "unknown"


#: 错误类别 → 用户可读说法。用户看到的是「已知原因」，不是内部码。
ERROR_MESSAGES: dict[str, str] = {
    ERR_MODEL_UNAVAILABLE: "模型接口当前不可用",
    ERR_MODEL_TIMEOUT: "模型请求超时",
    ERR_MODEL_ERROR: "模型接口返回错误",
    ERR_TOOL_LOOKUP_FAILED: "查询工具没有查到目标或返回不支持的能力",
    ERR_TOOL_ERROR: "工具执行报错",
    ERR_PARAMS_INVALID: "提交的参数不合法",
    ERR_TRIAL_FAILED: "修改计划预执行未通过校验",
    ERR_ACCEPT_FAILED: "接受修改时正式提交失败",
    ERR_BUDGET_EXHAUSTED: "本轮推理预算用完，仍未提交可执行的修改计划",
    ERR_NOT_SUBMITTED: "本轮推理结束，但没有提交可执行的修改计划",
    ERR_UNKNOWN: "原因尚未确定",
}


# --------------------------------------------------------------------------
# 阶段（契约 §2.3）
# --------------------------------------------------------------------------

STAGE_AGENT_REPLY = "agent_reply"
STAGE_TOOL_CALL = "tool_call"
STAGE_SUBMIT = "submit"
STAGE_TRIAL = "trial"
STAGE_CANDIDATE = "candidate"
STAGE_ACCEPTANCE = "acceptance"
STAGE_APPLY = "apply"
STAGE_EXPERIENCE = "experience"
STAGE_ERROR = "error"


# --------------------------------------------------------------------------
# 逐轮人话摘要（ReAct 第 N 轮）
# --------------------------------------------------------------------------

#: 记录模型**公开**文本的事件名。
#:
#: 思考摘要的**唯一合法来源**：模型自己说给人听的那部分文本
#: （框架的 ``TEXT_BLOCK_DELTA``）。隐藏的 ``THINKING_BLOCK_*`` 是模型的
#: 内部思维链 —— **不订阅、不读取、不转发、不展示**。
EVENT_PUBLIC_TEXT = "react_public_text"

#: 思考摘要缺失时的**如实**说法（不写「没有思考」）。
THINKING_MISSING = "思考摘要未提供"

#: 后台结果缺失时的**如实**说法。
BACKEND_MISSING = "后台结果未取得"

#: 重复提交时的固定说法（契约口径，逐字使用）。
REPEAT_SUBMISSION_TEXT = "仍提交了相同格式，没有修正成功"

#: 执行阶段（``phase``）的取值与先后顺序。
#:
#: 同一轮里取**最靠后**的那个：计划 → 试做 → 候选 → 已接受 → 已生效。
PHASE_PLAN = "plan"
PHASE_TRIAL = "trial"
PHASE_CANDIDATE = "candidate"
PHASE_ACCEPTED = "accepted"
PHASE_APPLIED = "applied"

PHASE_ORDER: tuple[str, ...] = (
    PHASE_PLAN,
    PHASE_TRIAL,
    PHASE_CANDIDATE,
    PHASE_ACCEPTED,
    PHASE_APPLIED,
)

#: 执行阶段 → 人话。**试做**必须说清「跑在草稿复制品上」。
PHASE_LABELS: dict[str, str] = {
    PHASE_PLAN: "修改计划（还没有落到草稿上）",
    PHASE_TRIAL: "试做：先在草稿复制品上执行，避免改坏原稿",
    PHASE_CANDIDATE: "待你确认的候选（正式草稿还没改）",
    PHASE_ACCEPTED: "你已确认",
    PHASE_APPLIED: "已正式改到草稿上",
}

#: 来源标记（缺失就说缺失，不拿框架状态冒充业务结果）。
SOURCE_MODEL_PUBLIC_TEXT = "model_public_text"
SOURCE_DERIVED_FROM_CALLS = "derived_from_calls"
SOURCE_DERIVED_FROM_PLAN = "derived_from_plan"
SOURCE_BUSINESS_PAYLOAD = "business_payload"
SOURCE_FRAMEWORK_STATE = "framework_state"
SOURCE_MISSING = "missing"

#: 业务原因码 → 人话。只覆盖**已知**的几个；覆盖不到就写「具体原因未取得」。
REASON_PHRASES: dict[str, str] = {
    "invalid_operation": "这一步的写法不对",
    "out_of_range": "取值超出了支持范围",
    "unknown_region": "没有找到要改的那张卡片",
    "unknown_region_id": "没有找到要改的那张卡片",
    "missing_output_ref": "缺少输出引用",
    "unsupported_chart_kind": "图表类型不在支持范围内",
    "params_invalid": "提交的参数不合法",
    "trial_failed": "预执行没有通过检查",
}

#: 提交类工具 → 人话动作短语。
SUBMIT_ACTION_PHRASES: dict[str, str] = {
    "submit_edit_plan": "提交了修改计划",
    "submit_change_plan": "提交了修改计划",
    "submit_fix_plan": "提交了修复计划",
    "submit_draft_plan": "提交了模版生成计划",
    "submit_clarification": "提交了需要你补充的信息",
    "submit_blocked": "说明当前做不到",
    "submit_edit_answer": "提交了本轮结论",
    "submit_query_answer": "提交了本轮答案",
}

#: 查询类工具名前缀 → 人话动作短语。
_READ_TOOL_PREFIXES = ("read_", "get_", "list_", "search_", "query_", "find_")



# --------------------------------------------------------------------------
# 脱敏
# --------------------------------------------------------------------------

#: 凭证样式的键：命中即抹掉。
_CREDENTIAL_KEY_RE = re.compile(
    r"(passw(or)?d|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|"
    r"credential|authorization|cookie|private[_-]?key|session[_-]?key)",
    re.IGNORECASE,
)

#: 正文里的凭证样式。
#:
#: 边界的写法很要紧：``\b`` 在中文里不成立 ——
#: 「密码是 password=xxx」里 ``\bpassword`` **匹配不到**（中文字符本身
#: 就是「单词字符」，前后没有边界）。所以这里用「前一个字符不是
#: ASCII 字母数字下划线」来代替 ``\b``。
_CREDENTIAL_TEXT_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9_])"
    r"(passw(or)?d|passwd|pwd|secret|token|api[_-]?key|"
    r"access[_-]?key|authorization)"
    r"\s*[:=]\s*[\"']?[^\s\"',;]+"
)
_BEARER_RE = re.compile(r"(?i)(?<![A-Za-z0-9_])Bearer\s+[A-Za-z0-9._\-]{8,}")
_SK_RE = re.compile(r"(?<![A-Za-z0-9_])sk-[A-Za-z0-9_\-]{8,}")


def redact(value: Any, *, limit: int = 200) -> Any:
    """脱敏 + 截断。**不返回原始秘密**。

    * 凭证样式的键 → 整个值抹成 ``[redacted]``；
    * 正文里的 ``password=xxx`` / ``Bearer xxx`` / ``sk-xxx`` → 抹掉；
    * 超长文本 → 截断并标 ``[truncated]``。
    """
    return _redact_with_flag(value, limit=limit)[0]


def _redact_with_flag(value: Any, *, limit: int = 200) -> tuple[Any, bool]:
    """脱敏 + 截断，并**如实报告**有没有裁剪过。

    第二个返回值是「是否发生过截断」：被裁剪的内容必须让下游看得见，
    不能把「截断了」写成「完整内容」。
    """
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        truncated = False
        for key, item in value.items():
            if _CREDENTIAL_KEY_RE.search(str(key)):
                out[str(key)] = "[redacted]"
                continue
            cleaned, cut = _redact_with_flag(item, limit=limit)
            out[str(key)] = cleaned
            truncated = truncated or cut
        return out, truncated
    if isinstance(value, (list, tuple)):
        items: list[Any] = []
        truncated = False
        for item in value:
            cleaned, cut = _redact_with_flag(item, limit=limit)
            items.append(cleaned)
            truncated = truncated or cut
        return items, truncated
    if isinstance(value, str):
        text = value
        # 顺序要紧：先抹 ``Bearer`` / ``sk-`` 这类**整体令牌**，
        # 再抹 ``key=value`` 形式 —— 否则 ``Authorization: Bearer xxx``
        # 会被后者吃掉前缀却**留下令牌本身**。
        text = _BEARER_RE.sub("[redacted]", text)
        text = _SK_RE.sub("[redacted]", text)
        text = _CREDENTIAL_TEXT_RE.sub("[redacted]", text)
        if len(text) > limit:
            return text[:limit] + "[truncated]", True
        return text, False
    if isinstance(value, (int, float, bool)) or value is None:
        return value, False
    return _redact_with_flag(str(value), limit=limit)


# --------------------------------------------------------------------------
# 请求作用域上下文
# --------------------------------------------------------------------------

_TRACE: ContextVar[dict[str, Any] | None] = ContextVar("edit_trace", default=None)

#: 请求作用域的**订阅者**（把追踪事件转成 SSE 之类）。
#:
#: 单独一个 ContextVar，而不是塞进 ``_TRACE`` 的字典里 —— ``start_trace()``
#: 会**整个替换**追踪字典，放在里面会被一起抹掉。
_SUBSCRIBERS: ContextVar[tuple[Callable[[dict[str, Any]], Any], ...] | None] = (
    ContextVar("edit_trace_subscribers", default=None)
)

#: 文件写入锁：多线程写同一个 JSONL 文件不能交错。
_FILE_LOCK = threading.RLock()


# --------------------------------------------------------------------------
# 冻结的增量契约（09 号 Prompt）
# --------------------------------------------------------------------------
#
# 这一节是 **T1 / T2 / T3 共用的字段契约**。执行层（T2）、聚合层（T1）、
# 展示层（T3）都只认这里定义的字段名 —— **同一个事实只有一个落点**，
# 前端不另维护一份规则。

#: 归属来源：调用上下文（任务局部，框架真值）。
ATTR_CALL_CONTEXT = "call_context"
#: 归属来源：调用方显式写死。
ATTR_EXPLICIT = "explicit"
#: 归属来源：共享的「当前轮次」（**可能不准**，只能当兜底）。
ATTR_SHARED_CURRENT = "shared_current"
#: 归属来源：没取得。
ATTR_MISSING = "missing"

#: 业务检查状态（三态 + 未执行）。
CHECK_PASS = "pass"
CHECK_FAIL = "fail"
CHECK_NOT_RUN = "not_run"
CHECK_UNKNOWN = "unknown"

#: 持久化状态：事务提交成功 / 失败 / 没走到 / 未知。
PERSIST_COMMITTED = "committed"
PERSIST_FAILED = "failed"
PERSIST_NOT_RUN = "not_run"
PERSIST_UNKNOWN = "unknown"

#: 效果核对状态。本轮**不做**效果核对 → 一律 ``not_run``，
#: 不推断「符合」或「不符合」。
EFFECT_NOT_RUN = "not_run"
EFFECT_UNKNOWN = "unknown"

#: 缺失标记（追加到既有 ``missing`` 列表里）。
MISSING_ITEM_ID = "itemId"
MISSING_PLAN_ID = "planId"
MISSING_TOOL_CALL_ID = "toolCallId"
MISSING_ITERATION = "iteration"
MISSING_REPLY_INDEX = "replyIndex"
MISSING_CALL_CONTEXT = "callContext"
MISSING_BATCH_ID = "batchId"
MISSING_CHANGES = "changes"
MISSING_STRUCTURE = "structure"
MISSING_EFFECT_CHECK = "effectCheck"
MISSING_PERSIST_CHECK = "persistenceCheck"

#: 缺失标记 → 人话（前端与后端共用一份口径）。
MISSING_LABELS: dict[str, str] = {
    MISSING_ITEM_ID: "修改事项标识",
    MISSING_PLAN_ID: "候选计划标识",
    MISSING_TOOL_CALL_ID: "工具调用 id",
    MISSING_ITERATION: "轮次",
    MISSING_REPLY_INDEX: "回复序号",
    MISSING_CALL_CONTEXT: "调用归属",
    MISSING_BATCH_ID: "试做批次",
    MISSING_CHANGES: "前后变化",
    MISSING_STRUCTURE: "结构投影",
    MISSING_EFFECT_CHECK: "效果核对",
    MISSING_PERSIST_CHECK: "持久化结果",
}


@dataclass(frozen=True)
class CallContext:
    """**一次真实工具调用**的不可变、任务局部上下文。

    为什么不用共享的「当前轮次」：

    * 框架在**并行**执行同一批工具调用（``asyncio.gather``）——
      每个调用跑在**各自的任务**里，彼此看不到对方写入的状态；
    * 共享的 ``currentIteration`` 只能回答「现在是第几轮」，
      回答不了「**这次调用**是哪一次调用」。

    所以归属信息必须由**框架真值**在**工具执行的那一刻**固定下来，
    再通过这个不可变对象传进提交 / 试做事件。

    字段都是**取到的值**，取不到就是 ``None`` / 空串 —— 不猜。
    """

    #: 第几次 ``agent.reply()``（1 起）。同一次 reply 里的所有调用共享它。
    reply_index: int
    #: 框架真实迭代号（1 起）；取不到为 ``None``。
    iteration: int | None
    #: 框架给的 ``tool_call_id``；取不到为空串。
    tool_call_id: str
    #: 工具名。
    tool_name: str
    #: 归属来源（固定为 ``call_context``）。
    source: str = ATTR_CALL_CONTEXT


#: **任务局部**的调用上下文。
#:
#: 用独立的 ContextVar（而不是塞进 ``_TRACE`` 的字典）：框架并行执行工具
#: 时每个任务拿到的是**自己那份**上下文副本，不会互相串。
_CALL_CONTEXT: ContextVar[CallContext | None] = ContextVar(
    "edit_trace_call_context", default=None
)


def bind_call_context(ctx: CallContext) -> None:
    """绑定**当前任务**的调用上下文（工具执行前调用）。"""
    _CALL_CONTEXT.set(ctx)


def current_call_context() -> CallContext | None:
    """当前任务的调用上下文；没绑定返回 ``None``。"""
    return _CALL_CONTEXT.get()


def clear_call_context() -> None:
    """清掉调用上下文。"""
    _CALL_CONTEXT.set(None)


def new_batch_id() -> str:
    """一个新的**试做批次**标识。

    一次工具调用里可能有多次试做（同轮修正后再交）—— 每次试做一个批次，
    步骤键要带批次，否则两次试做的「第 1 步」会混成一步。
    """
    return f"batch_{uuid.uuid4().hex[:12]}"


def round_key(
    trace_id: str, reply_index: Any, iteration: Any
) -> str:
    """轮次唯一键：**traceId + replyIndex + iteration**。

    缺哪一段就把那一段写成 ``unknown`` —— 不补造。
    """
    reply_part = str(reply_index) if reply_index is not None else "unknown"
    iter_part = str(iteration) if iteration is not None else "unknown"
    return f"{trace_id or 'unknown'}::{reply_part}::{iter_part}"


def call_key(round_key_value: str, tool_call_id: str) -> str:
    """调用唯一键：轮次键 + 工具调用 id。

    同轮的不同调用按 ``toolCallId`` **并列**；没有调用 id 的用
    ``no-call`` 占位（不假装它属于某一次调用）。
    """
    return f"{round_key_value}::{tool_call_id or 'no-call'}"


def step_key(
    call_key_value: str,
    batch_id: str,
    step_index: Any,
    wrapper_path: Any = None,
    sub_step_index: Any = None,
) -> str:
    """步骤唯一键：调用键 + 批次 + **真实步骤下标**（+ 包装路径）。

    同一次调用里的两次试做，各自的「第 1 步」靠批次分开。

    包装能力（展示操作包）里的**子操作**与它的父步骤**共用同一个
    下标** —— 所以键里还要带上**包装路径**与**子步骤下标**，
    否则子步骤会被父步骤合并掉，看不到「这一步里做了哪几件事」。
    """
    index_part = str(step_index) if step_index is not None else "unknown"
    key = f"{call_key_value}::{batch_id or 'no-batch'}::{index_part}"
    wrapper = wrapper_path if isinstance(wrapper_path, (list, tuple)) else []
    wrapper_part = "/".join(str(item) for item in wrapper if str(item))
    if wrapper_part:
        sub_part = (
            str(sub_step_index) if sub_step_index is not None else "unknown"
        )
        key = f"{key}::{wrapper_part}::{sub_part}"
    return key



def subscribe(callback: Callable[[dict[str, Any]], Any]) -> Callable[[], None]:
    """注册一个**请求作用域**的订阅者。返回取消订阅的函数。

    订阅者收到的是**逐轮人话摘要**（``{"kind": "round", ...}``）——
    与最终摘要同一份数据，不存在第二个来源。

    订阅者是**上下文作用域**的：并行请求各自一份，不会串线。
    """
    current = _SUBSCRIBERS.get() or ()
    _SUBSCRIBERS.set(current + (callback,))

    def _unsubscribe() -> None:
        now = _SUBSCRIBERS.get() or ()
        _SUBSCRIBERS.set(tuple(cb for cb in now if cb is not callback))

    return _unsubscribe


def unsubscribe(callback: Callable[[dict[str, Any]], Any]) -> None:
    """取消订阅（``subscribe`` 返回的函数也能干这件事）。"""
    now = _SUBSCRIBERS.get() or ()
    _SUBSCRIBERS.set(tuple(cb for cb in now if cb is not callback))


def _schedule(awaitable: Any) -> None:
    """把协程挂到当前事件循环上跑（订阅者可以是 async）。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # 没有运行中的事件循环：只能丢掉（不让追踪掀翻主流程）。
        close = getattr(awaitable, "close", None)
        if callable(close):
            close()
        return
    try:
        loop.create_task(awaitable)
    except Exception:  # noqa: BLE001
        logger.debug("[edit_trace] 订阅者协程调度失败", exc_info=True)


def _notify(payload: dict[str, Any]) -> None:
    """通知**本请求**的订阅者（失败不影响主流程）。"""
    for callback in _SUBSCRIBERS.get() or ():
        try:
            result = callback(payload)
            if inspect.isawaitable(result):
                _schedule(result)
        except Exception:  # noqa: BLE001 — 追踪不能拖垮业务
            logger.debug("[edit_trace] 订阅者回调失败", exc_info=True)



def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def current_trace() -> dict[str, Any] | None:
    """当前请求的追踪上下文。没有绑定时返回 ``None``。"""
    return _TRACE.get()


def bind_trace(trace: dict[str, Any]) -> None:
    """把追踪上下文绑定到当前请求。"""
    _TRACE.set(trace)


def clear_trace() -> None:
    """解绑追踪上下文。"""
    _TRACE.set(None)


def start_trace(
    *,
    session_id: str = "",
    draft_id: str = "",
    catalog_version: str = "",
    message_id: str = "",
    item_id: str = "",
    plan_id: str = "",
) -> dict[str, Any]:
    """开一条追踪。返回追踪上下文（同时已绑定到当前上下文）。

    ``message_id`` 是**本轮请求**的消息标识：晚到的追踪事件靠它判断
    「是不是本请求的」。
    """
    trace = _new_trace(
        session_id=session_id,
        draft_id=draft_id,
        catalog_version=catalog_version,
        message_id=message_id,
        item_id=item_id,
        plan_id=plan_id,
    )
    bind_trace(trace)
    return trace


def _new_trace(
    *,
    session_id: str = "",
    draft_id: str = "",
    catalog_version: str = "",
    message_id: str = "",
    item_id: str = "",
    plan_id: str = "",
) -> dict[str, Any]:
    """造一条**新的**追踪上下文（不绑定）。"""
    return {
        "traceId": f"trace_{uuid.uuid4().hex[:16]}",
        "sessionId": session_id or "",
        "draftId": draft_id or "",
        "messageId": message_id or "",
        "itemId": item_id or "",
        "planId": plan_id or "",
        "catalogVersion": catalog_version or "",
        "budget": 0,
        "replyIndex": 0,
        "events": [],
        "startedAt": _now(),
        # 已发布过的逐轮摘要指纹：用来**只推送变化了的轮次**（去重）。
        "publishedRounds": {},
        # 当前正在跑的那一轮：工具自己记的事件靠它归到正确的轮次。
        #
        # 这是**共享兜底**，不是权威归属 —— 权威归属是任务局部的
        # ``CallContext``（见 ``bind_call_context``）。取不到任务局部
        # 上下文时才用这里，并如实标成 ``shared_current``。
        "currentIteration": None,
        # 修订号：每条事件单调递增。前端据此拒绝**旧修订**覆盖新修订。
        "revision": 0,
    }


def ensure_trace(
    *,
    session_id: str = "",
    draft_id: str = "",
    catalog_version: str = "",
    message_id: str = "",
    item_id: str = "",
    plan_id: str = "",
) -> dict[str, Any]:
    """确保当前请求**有**一条追踪；有就补标识，没有才新建。

    为什么不直接 ``start_trace``：请求入口已经开好追踪时，Agent 侧
    再开一次会把**已经记下的事实**（请求来源、失败 notice…）冲掉。
    这里只补标识，**不丢已有事件**。
    """
    trace = _TRACE.get()
    if trace is None:
        return start_trace(
            session_id=session_id,
            draft_id=draft_id,
            catalog_version=catalog_version,
            message_id=message_id,
            item_id=item_id,
            plan_id=plan_id,
        )
    if session_id:
        trace["sessionId"] = session_id
    if draft_id:
        trace["draftId"] = draft_id
    if catalog_version:
        trace["catalogVersion"] = catalog_version
    if message_id:
        trace["messageId"] = message_id
    if item_id:
        trace["itemId"] = item_id
    if plan_id:
        trace["planId"] = plan_id
    return trace


def begin_trace_scope(
    *,
    session_id: str = "",
    draft_id: str = "",
    message_id: str = "",
    item_id: str = "",
    plan_id: str = "",
) -> tuple[dict[str, Any], Any]:
    """开一个**请求作用域**的追踪。

    返回 ``(追踪上下文, token)``。退出请求时用
    ``end_trace_scope(token)`` **恢复**原来的绑定 ——
    不能让本轮的追踪串到下一个请求。
    """
    trace = _new_trace(
        session_id=session_id,
        draft_id=draft_id,
        message_id=message_id,
        item_id=item_id,
        plan_id=plan_id,
    )
    token = _TRACE.set(trace)
    return trace, token


def end_trace_scope(token: Any) -> None:
    """结束请求作用域的追踪：恢复之前的绑定。"""
    try:
        _TRACE.reset(token)
    except (ValueError, RuntimeError):  # pragma: no cover — 跨上下文时的兜底
        _TRACE.set(None)


def _settings() -> Any:
    from app.config import get_settings

    return get_settings()


# --------------------------------------------------------------------------
# 事件
# --------------------------------------------------------------------------

def _resolve_attribution(
    trace: dict[str, Any] | None,
    fields: dict[str, Any],
) -> dict[str, Any]:
    """决定这条事件**属于哪一轮、哪一次调用**。

    优先级（从可靠到兜底）：

    1. **调用方显式写死**（``explicit``）—— 最可靠，但调用方自己也得负责对；
    2. **任务局部的调用上下文**（``call_context``）—— 框架真值，在工具
       执行的那一刻固定下来；并行调用各自一份，不会串；
    3. **共享的当前轮次**（``shared_current``）—— 兜底，**可能不准**，
       如实标出来，不假装是权威归属；
    4. **未取得**（``missing``）。

    来源冲突（显式值与调用上下文不一致）**不静默取胜者**：
    标成 ``conflict=True``，两个值都留。
    """
    call_ctx = current_call_context()
    trace = trace or {}

    explicit_reply = fields.pop("replyIndex", None)
    explicit_iteration = fields.pop("iteration", None)
    explicit_round_iteration = fields.pop("roundIteration", None)
    explicit_call_id = fields.pop("toolCallId", None)

    conflict = False

    # ---- replyIndex ----
    if explicit_reply is not None:
        reply_index = int(explicit_reply or 0)
        reply_source = ATTR_EXPLICIT
    elif call_ctx is not None:
        reply_index = int(call_ctx.reply_index or 0)
        reply_source = ATTR_CALL_CONTEXT
    else:
        reply_index = int(trace.get("replyIndex", 0) or 0)
        reply_source = ATTR_SHARED_CURRENT

    # ---- iteration ----
    ctx_iteration = call_ctx.iteration if call_ctx is not None else None
    if explicit_iteration is not None:
        iteration: int | None = int(explicit_iteration)
        iteration_source = ATTR_EXPLICIT
        if ctx_iteration is not None and ctx_iteration != iteration:
            conflict = True
    elif ctx_iteration is not None:
        iteration = ctx_iteration
        iteration_source = ATTR_CALL_CONTEXT
    elif trace.get("currentIteration") is not None:
        iteration = int(trace.get("currentIteration"))
        iteration_source = ATTR_SHARED_CURRENT
    else:
        iteration = None
        iteration_source = ATTR_MISSING

    # ---- toolCallId ----
    ctx_call_id = str(call_ctx.tool_call_id or "") if call_ctx is not None else ""
    if explicit_call_id is not None and str(explicit_call_id or ""):
        tool_call_id = str(explicit_call_id)
        call_source = ATTR_EXPLICIT
        if ctx_call_id and ctx_call_id != tool_call_id:
            conflict = True
    elif ctx_call_id:
        tool_call_id = ctx_call_id
        call_source = ATTR_CALL_CONTEXT
    else:
        tool_call_id = ""
        call_source = ATTR_MISSING

    if reply_source == ATTR_MISSING:
        reply_source = ATTR_SHARED_CURRENT

    return {
        "replyIndex": reply_index,
        "iteration": iteration,
        "roundIteration": explicit_round_iteration,
        "toolCallId": tool_call_id,
        "attribution": {
            "replyIndex": reply_source,
            "iteration": iteration_source,
            "toolCallId": call_source,
            "conflict": conflict,
            "callContextBound": call_ctx is not None,
        },
    }


def record(stage: str, **fields: Any) -> dict[str, Any]:
    """记一条事件。返回这条事件本身。

    没绑定追踪时**不报错**：返回一个占位事件（调试与单测都能安全调用）。
    """
    trace = _TRACE.get()
    now = _now()
    # 归属（轮次 / 调用）：先按冻结契约解析，再落成字段。
    attribution = _resolve_attribution(trace, fields)
    reply_index = attribution["replyIndex"]
    iteration = attribution["iteration"]
    tool_call_id = attribution["toolCallId"]
    params, params_cut = _redact_with_flag(fields.pop("params", {}) or {})
    result, result_cut = _redact_with_flag(fields.pop("result", {}) or {})
    # ``extra`` 先取出来：执行层可能把业务事实**放在 extra 里** ——
    # 那就**提到一等字段**，免得同一个事实有两个落点（聚合层只认一等字段）。
    raw_extra = fields.pop("extra", None)
    extra_dict = raw_extra if isinstance(raw_extra, dict) else {}
    # ---- 业务事实（执行层写进来的那一份）----
    #
    # ``businessOk`` 是**真实业务结果**：``True``/``False`` = 已知，
    # ``None`` = **未取得**（绝不能拿框架的 ``state`` 顶替）。
    #
    # 显式关键字优先于 extra 里的同名项。
    business_ok = fields.pop("businessOk", extra_dict.get("businessOk"))
    if business_ok is not None:
        business_ok = bool(business_ok)
    business_message = str(
        fields.pop("businessMessage", extra_dict.get("businessMessage")) or ""
    )
    business_reason = str(
        fields.pop("businessReason", extra_dict.get("businessReason")) or ""
    )
    business_issues, issues_cut = _redact_with_flag(
        fields.pop("businessIssues", extra_dict.get("businessIssues")) or []
    )
    business_detail, detail_cut = _redact_with_flag(
        fields.pop("businessDetail", extra_dict.get("businessDetail")) or {}
    )
    phase = str(fields.pop("phase", extra_dict.get("phase")) or "")
    draft_changed = fields.pop("draftChanged", extra_dict.get("draftChanged"))
    if draft_changed is not None:
        draft_changed = bool(draft_changed)
    # ---- 来源 / 缺失标记（谁写都可以；没写就由聚合层自己算）----
    summary_source = str(
        fields.pop("summarySource", extra_dict.get("summarySource")) or ""
    )
    action_source = str(
        fields.pop("actionSource", extra_dict.get("actionSource")) or ""
    )
    result_source = str(
        fields.pop("resultSource", extra_dict.get("resultSource")) or ""
    )
    missing = fields.pop("missing", extra_dict.get("missing")) or []
    if not isinstance(missing, list):
        missing = [str(missing)]
    missing = [str(item) for item in missing if str(item)]
    # 提交形状指纹：用来判「又提交了同一份没改好的东西」。
    submit_shape = str(
        fields.pop("submitShape", extra_dict.get("submitShape")) or ""
    )
    # ---- 增量契约字段（09 号 Prompt 冻结）----
    #
    # 精确前后变化：执行层给 ``changeSet`` 的 ``path/before/after`` 原样带过来。
    changes = fields.pop("changes", extra_dict.get("changes")) or []
    if not isinstance(changes, list):
        changes = []
    changes, changes_cut = _redact_with_flag(changes)
    # 有界结构投影：受影响区域 + 必要父容器 / 祖先的结构事实。
    structure = fields.pop("structure", extra_dict.get("structure")) or {}
    if not isinstance(structure, dict):
        structure = {}
    structure, structure_cut = _redact_with_flag(structure)
    # 三类检查状态**分开**：业务 / 持久化 / 效果核对。
    raw_checks = fields.pop("checks", extra_dict.get("checks")) or {}
    if not isinstance(raw_checks, dict):
        raw_checks = {}
    checks = {
        "business": str(raw_checks.get("business") or CHECK_UNKNOWN),
        "persistence": str(raw_checks.get("persistence") or PERSIST_UNKNOWN),
        "effect": str(raw_checks.get("effect") or EFFECT_UNKNOWN),
    }
    # 候选登记 / 正式生效：三态（True / False / None=未取得）。
    candidate_registered = fields.pop(
        "candidateRegistered", extra_dict.get("candidateRegistered")
    )
    if candidate_registered is not None:
        candidate_registered = bool(candidate_registered)
    applied = fields.pop("applied", extra_dict.get("applied"))
    if applied is not None:
        applied = bool(applied)
    # 批次 / 步骤 / 包装路径。
    batch_id = str(fields.pop("batchId", extra_dict.get("batchId")) or "")
    step_index = fields.pop("stepIndex", extra_dict.get("stepIndex"))
    wrapper_path = fields.pop("wrapperPath", extra_dict.get("wrapperPath")) or []
    if not isinstance(wrapper_path, list):
        wrapper_path = [str(wrapper_path)]
    wrapper_path = [str(item) for item in wrapper_path if str(item)]
    sub_step_index = fields.pop("subStepIndex", extra_dict.get("subStepIndex"))
    # 字段级裁剪痕迹（比 ``truncated`` 更细：说清**哪里**被裁了）。
    truncation = fields.pop("truncation", extra_dict.get("truncation")) or {}
    if not isinstance(truncation, dict):
        truncation = {}

    event: dict[str, Any] = {
        # 每条事件的稳定标识：前端去重、跨轮归并都靠它。
        "eventId": f"evt_{uuid.uuid4().hex[:12]}",
        "traceId": (trace or {}).get("traceId", ""),
        "sessionId": (trace or {}).get("sessionId", ""),
        "messageId": (trace or {}).get("messageId", ""),
        "itemId": fields.pop("itemId", "") or (trace or {}).get("itemId", ""),
        "planId": fields.pop("planId", "") or (trace or {}).get("planId", ""),
        "draftId": fields.pop("draftId", "") or (trace or {}).get("draftId", ""),
        "catalogVersion": (trace or {}).get("catalogVersion", ""),
        "stage": stage,
        "replyIndex": reply_index,
        "iteration": iteration,
        # 工具自己记的事件不知道自己在第几轮 → 继承**当前这一轮**。
        "roundIteration": attribution["roundIteration"],
        "toolCallId": tool_call_id,
        "name": str(fields.pop("name", "") or ""),
        "params": params,
        "result": result,
        # 业务事实（执行层写进来的）。
        "businessOk": business_ok,
        "businessMessage": business_message,
        "businessReason": business_reason,
        "businessIssues": business_issues,
        "businessDetail": business_detail,
        "phase": phase,
        "draftChanged": draft_changed,
        # 来源 / 缺失标记。
        "summarySource": summary_source,
        "actionSource": action_source,
        "resultSource": result_source,
        "missing": missing,
        "submitShape": submit_shape,
        # ---- 增量契约字段 ----
        # 归属来源：每段归属各自标来源；冲突如实标。
        "attribution": attribution["attribution"],
        # 轮次 / 调用 / 步骤的**唯一键**（前端去重与并排的依据）。
        "roundKey": round_key(
            str((trace or {}).get("traceId") or ""), reply_index, iteration
        ),
        "callKey": call_key(
            round_key(
                str((trace or {}).get("traceId") or ""), reply_index, iteration
            ),
            tool_call_id,
        ),
        "batchId": batch_id,
        "stepIndex": step_index,
        "stepKey": step_key(
            call_key(
                round_key(
                    str((trace or {}).get("traceId") or ""),
                    reply_index,
                    iteration,
                ),
                tool_call_id,
            ),
            batch_id,
            step_index,
            wrapper_path,
            sub_step_index,
        ),
        "wrapperPath": wrapper_path,
        "subStepIndex": sub_step_index,
        # 事实账本：精确前后变化 + 有界结构投影。
        "changes": changes,
        "structure": structure,
        # 三类检查状态**分开**：不混成一句「已生效」。
        "checks": checks,
        "candidateRegistered": candidate_registered,
        "applied": applied,
        # 修订号：单调递增；前端据此拒绝旧修订覆盖。
        "revision": int((trace or {}).get("revision", 0) or 0) + 1,
        # 字段级裁剪痕迹。
        "truncation": truncation,
        # 被裁剪过就如实标 True：不能把「截断了」写成「完整内容」。
        "truncated": bool(
            fields.pop("truncated", False)
            or params_cut
            or result_cut
            or issues_cut
            or detail_cut
            or changes_cut
            or structure_cut
        ),
        "durationMs": int(fields.pop("durationMs", 0) or 0),
        "status": str(fields.pop("status", "ok") or "ok"),
        "errorCategory": str(fields.pop("errorCategory", "") or ""),
        "budget": int(fields.pop("budget", 0) or 0) or int(
            (trace or {}).get("budget", 0) or 0
        ),
        "stopReason": str(fields.pop("stopReason", "") or ""),
        "at": now,
    }
    if event["roundIteration"] is None:
        event["roundIteration"] = iteration
    if trace is not None:
        trace["revision"] = event["revision"]
    # 额外字段（不破坏契约的必备字段）：放一个平铺的 extra 里。
    #
    # 已经被提到一等字段的那些（业务事实 / phase / 来源标记）**不再**留在
    # extra 里重复一份 —— 同一个事实只有一个落点。
    leftover_extra = {
        key: value
        for key, value in extra_dict.items()
        if key
        not in (
            "businessOk",
            "businessMessage",
            "businessReason",
            "businessIssues",
            "businessDetail",
            "phase",
            "draftChanged",
            "summarySource",
            "actionSource",
            "resultSource",
            "missing",
            "submitShape",
            # 增量契约字段：同一个事实只有一个落点，不在 extra 里重复一份。
            "changes",
            "structure",
            "checks",
            "candidateRegistered",
            "applied",
            "batchId",
            "stepIndex",
            "wrapperPath",
            "subStepIndex",
            "truncation",
        )
    }
    if leftover_extra:
        event["extra"] = redact(leftover_extra)
    if fields:
        event["extra"] = redact({**(event.get("extra") or {}), **fields})

    if trace is None:
        return event

    trace["events"].append(event)
    _append_to_file(event)
    # 逐轮摘要：与最终摘要**同一份数据**。变了才推（去重）。
    _publish_rounds(trace)
    return event


#: 写日志时**优先**裁剪的字段（体积大、信息密度低）。
#:
#: 裁剪顺序：从这里开始，按体积从大到小丢，**保留有效结构**。
_JSONL_DROP_ORDER: tuple[str, ...] = (
    "structure",
    "changes",
    "result",
    "params",
    "businessDetail",
    "businessIssues",
    "technical",
)


def _crop_value(
    value: Any, cap: int, max_items: int = 20
) -> tuple[Any, bool, int]:
    """把一个值裁到有界体积（**结构保留**）。

    返回 ``(裁后的值, 是否裁过, 被丢掉的条目数)``。
    列表只保留前 ``max_items`` 项 —— 被丢的**条目数**如实报出来。
    """
    if isinstance(value, str):
        if len(value) <= cap:
            return value, False, 0
        return value[:cap] + "[truncated]", True, 0
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        cut = False
        dropped = 0
        for key, item in value.items():
            cropped, item_cut, item_dropped = _crop_value(item, cap, max_items)
            out[str(key)] = cropped
            cut = cut or item_cut
            dropped += item_dropped
        return out, cut, dropped
    if isinstance(value, list):
        items: list[Any] = []
        cut = False
        dropped = 0
        for item in value[:max_items]:
            cropped, item_cut, item_dropped = _crop_value(item, cap, max_items)
            items.append(cropped)
            cut = cut or item_cut
            dropped += item_dropped
        extra = len(value) - len(items)
        if extra > 0:
            cut = True
            dropped += extra
        return items, cut, dropped
    return value, False, 0


def _crop_to_budget(value: Any, budget: int) -> tuple[Any, bool, int]:
    """把一个值裁到**总体积**不超过 ``budget``（结构尽量保留）。

    单条字符串的上限不够 —— 一堆短串加起来照样超预算。所以这里
    按「越来越紧」的几档试裁，取**第一档装得下的**。
    """
    if budget <= 0:
        return value, False, 0
    if len(json.dumps(value, ensure_ascii=False, default=str)) <= budget:
        return value, False, 0
    cropped: Any = value
    cut = False
    dropped = 0
    for cap, items in ((256, 12), (128, 8), (64, 5), (32, 3), (16, 2)):
        cropped, cut, dropped = _crop_value(value, cap, max_items=items)
        if len(json.dumps(cropped, ensure_ascii=False, default=str)) <= budget:
            return cropped, True, dropped
    return cropped, True, dropped


def _bounded_event(event: dict[str, Any], limit: int) -> tuple[dict[str, Any], str]:
    """把事件裁到能写进一行 JSONL 的体积，**结果仍是合法 JSON**。

    旧写法是「序列化后按字符切」—— 切出来的**不是合法 JSON**，下游
    解析必然失败。这里改成**字段级裁剪**：

    1. 先按**总体积预算**裁每个大字段（保留结构：路径、键名、数量），
       报出丢了多少条目；
    2. 还太大就**按体积从大到小丢字段**（保留稳定键与身份信息）；
    3. 最后重新序列化 —— 结果一定是合法 JSON。

    保留：事件 id、归属键、裁剪标记、原条目数与缺失范围。
    """
    if limit <= 0:
        return dict(event), ""
    line = json.dumps(event, ensure_ascii=False, default=str)
    if len(line) <= limit:
        return event, ""
    dropped: list[str] = []
    cropped_fields: list[str] = []
    dropped_items = 0
    working = dict(event)
    # 身份信息（键名 + 稳定 id）大约要占几百字符，剩下的分给大字段。
    identity_budget = 500
    field_budget = max(64, (limit - identity_budget) // max(1, len(_JSONL_DROP_ORDER)))
    for key in _JSONL_DROP_ORDER:
        if key not in working:
            continue
        cropped, cut, dropped_count = _crop_to_budget(
            working[key], field_budget
        )
        if cut:
            working[key] = cropped
            cropped_fields.append(key)
        dropped_items += dropped_count
    # 第二轮：还是太大 → 按顺序**丢字段**（保留稳定键与身份信息）。
    for key in _JSONL_DROP_ORDER:
        if key not in working or not working[key]:
            continue
        candidate = {k: v for k, v in working.items() if k != key}
        candidate["truncated"] = True
        candidate["truncation"] = {
            "scope": "jsonl_field_drop",
            "dropped": sorted(set(dropped) | {key}),
            "croppedFields": sorted(set(cropped_fields)),
            "droppedItems": dropped_items,
            "limitChars": limit,
        }
        size = len(json.dumps(candidate, ensure_ascii=False, default=str))
        working = candidate
        dropped.append(key)
        if size <= limit:
            break
    working["truncated"] = True
    working["truncation"] = {
        "scope": "jsonl_bounded",
        "dropped": sorted(set(dropped)),
        "croppedFields": sorted(set(cropped_fields)),
        "droppedItems": dropped_items,
        "limitChars": limit,
    }
    # 最后兜底：极端情况下连标记都放不下 → 只留身份信息。
    line = json.dumps(working, ensure_ascii=False, default=str)
    if len(line) > limit:
        working = {
            "eventId": event.get("eventId", ""),
            "traceId": event.get("traceId", ""),
            "roundKey": event.get("roundKey", ""),
            "callKey": event.get("callKey", ""),
            "stepKey": event.get("stepKey", ""),
            "stage": event.get("stage", ""),
            "at": event.get("at", ""),
            "truncated": True,
            "truncation": {
                "scope": "jsonl_identity_only",
                "dropped": sorted(set(dropped) | {"*"}),
                "limitChars": limit,
            },
        }
    return working, ""


def _append_to_file(event: dict[str, Any]) -> None:
    """把事件写进 JSONL 文件。**失败不影响主流程**。"""
    try:
        settings = _settings()
        if not getattr(settings, "edit_trace_enabled", True):
            return
        directory = Path(getattr(settings, "edit_trace_dir", "")) if getattr(
            settings, "edit_trace_dir", None
        ) else None
        if not directory:
            return
        directory.mkdir(parents=True, exist_ok=True)
        try:
            directory.chmod(0o700)
        except OSError:
            pass
        path = directory / "edit-trace.jsonl"
        limit = int(getattr(settings, "edit_trace_max_event_chars", 2000) or 2000)
        bounded, _ = _bounded_event(event, limit)
        line = json.dumps(bounded, ensure_ascii=False, default=str)
        with _FILE_LOCK:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
            try:
                path.chmod(0o600)
            except OSError:
                pass
        _enforce_limits(directory, path, settings)
    except Exception:  # noqa: BLE001 — 追踪不能拖垮业务
        logger.debug("[edit_trace] 写追踪文件失败", exc_info=True)


def _enforce_limits(directory: Path, path: Path, settings: Any) -> None:
    """按容量与保留期限清理追踪文件。"""
    try:
        max_bytes = int(getattr(settings, "edit_trace_max_bytes", 0) or 0)
        if max_bytes > 0:
            if path.exists() and path.stat().st_size > max_bytes:
                # 超容量：只留最近的部分（按行切，从尾部留）。
                lines = path.read_text(encoding="utf-8").splitlines()
                kept: list[str] = []
                size = 0
                for line in reversed(lines):
                    size += len(line.encode("utf-8")) + 1
                    if size > max_bytes // 2:
                        break
                    kept.append(line)
                tmp = path.with_suffix(".jsonl.tmp")
                tmp.write_text("\n".join(reversed(kept)) + "\n", encoding="utf-8")
                os.replace(tmp, path)
        days = int(getattr(settings, "edit_trace_retention_days", 0) or 0)
        if days > 0:
            cutoff = datetime.now() - timedelta(days=days)
            for item in directory.iterdir():
                if not item.is_file() or item == path:
                    continue
                if datetime.fromtimestamp(item.stat().st_mtime) < cutoff:
                    item.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        logger.debug("[edit_trace] 清理追踪文件失败", exc_info=True)


# --------------------------------------------------------------------------
# 摘要
# --------------------------------------------------------------------------

def build_lifecycle(events: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """**不归属某一轮**的阶段事实（接受、正式生效、错误）。

    接受是**独立请求**：它不走模型、也不是新的 ReAct 轮次。
    把它伪装成「第 N 轮」会让人误以为模型又跑了一遍。

    所以这些事实单独成列，挂在请求级摘要上。
    """
    items: list[dict[str, Any]] = []
    for event in events or []:
        stage = str(event.get("stage") or "")
        if stage not in (STAGE_ACCEPTANCE, STAGE_APPLY, STAGE_CANDIDATE, STAGE_ERROR):
            continue
        if stage == STAGE_ERROR and event.get("roundIteration") is not None:
            # 轮次内的错误已经在那轮里说过了，不重复列。
            continue
        items.append(
            {
                "stage": stage,
                "phase": str(event.get("phase") or ""),
                "name": str(event.get("name") or ""),
                "ok": event.get("businessOk"),
                "reason": str(event.get("businessReason") or ""),
                "message": str(event.get("businessMessage") or ""),
                "applied": event.get("applied"),
                "candidateRegistered": event.get("candidateRegistered"),
                "checks": dict(event.get("checks") or {}),
                "itemId": str(event.get("itemId") or ""),
                "planId": str(event.get("planId") or ""),
                "revision": int(event.get("revision") or 0),
                "at": str(event.get("at") or ""),
            }
        )
    return items


def trace_summary() -> dict[str, Any]:
    """本轮诊断摘要（可返回给用户）。

    结构按契约 §2.5：阶段 / 停止原因 / 错误类别 / 预算 / 迭代数 /
    工具调用数 / 是否提交 / 是否试做 / 候选是否登记 / 是否生效 /
    用户可读的行 / traceId。
    """
    trace = _TRACE.get()
    if trace is None:
        return {
            "traceId": "",
            "messageId": "",
            "stage": "",
            "stopReason": "",
            "errorCategory": "",
            "budget": 0,
            "iterations": 0,
            "businessRounds": 0,
            "graceRounds": 0,
            "toolCalls": 0,
            "submitted": False,
            "submissionAccepted": False,
            "tried": False,
            "candidateRegistered": False,
            "applied": False,
            "checks": {
                "business": CHECK_UNKNOWN,
                "persistence": PERSIST_UNKNOWN,
                "effect": EFFECT_UNKNOWN,
            },
            "revision": 0,
            "lifecycle": [],
            "rounds": [],
            "messages": [],
        }

    events = list(trace.get("events") or [])
    iterations = 0
    tool_calls = 0
    seen_call_ids: set[str] = set()
    submitted = False
    submission_accepted = False
    tried = False
    candidate_registered = False
    applied = False
    stop_reason = ""
    error_category = ""
    stage = ""

    for event in events:
        value = event.get("iteration")
        if isinstance(value, int) and value > iterations:
            iterations = value
        if event.get("stage") == STAGE_TOOL_CALL:
            # 同一次调用会记「开始」与「结束」两条 → 按调用 id 去重。
            call_id = str(event.get("toolCallId") or "")
            if call_id:
                if call_id in seen_call_ids:
                    continue
                seen_call_ids.add(call_id)
            tool_calls += 1
        if event.get("stage") == STAGE_SUBMIT:
            submitted = True
            if (event.get("result") or {}).get("accepted") is True:
                submission_accepted = True
            if event.get("businessOk") is True:
                submission_accepted = True
        if event.get("stage") == STAGE_TRIAL:
            tried = True
        if event.get("stage") == STAGE_CANDIDATE:
            candidate_registered = True
        if event.get("stage") == STAGE_APPLY:
            applied = True
        if event.get("stopReason"):
            stop_reason = str(event.get("stopReason"))
        if event.get("errorCategory"):
            error_category = str(event.get("errorCategory"))
        if event.get("stage"):
            stage = str(event.get("stage"))

    # 业务轮数与**框架宽限轮数**分开算：宽限不是业务轮。
    grace_used = 0
    total_iterations = iterations
    for event in events:
        if event.get("name") != "react_grace_iters":
            continue
        params = event.get("params") or {}
        result = event.get("result") or {}
        grace_used = int(params.get("graceItersUsed") or 0)
        total_iterations = int(result.get("iterations") or iterations)
    business_rounds = max(0, total_iterations - grace_used)
    budget = int(trace.get("budget", 0) or 0)
    rounds = build_round_summaries(events)
    checks = _checks_of(events)

    return {
        "traceId": trace.get("traceId", ""),
        "messageId": trace.get("messageId", ""),
        "stage": stage,
        "stopReason": stop_reason,
        "errorCategory": error_category,
        "budget": budget,
        "iterations": iterations,
        # 业务轮数：真正用来做事的圈数。
        "businessRounds": business_rounds,
        # 框架结构化输出宽限：单独计数，**不是**业务轮。
        "graceRounds": grace_used,
        "toolCalls": tool_calls,
        "submitted": submitted,
        "submissionAccepted": submission_accepted,
        "tried": tried,
        "candidateRegistered": candidate_registered,
        "applied": applied,
        # 三类状态**分开**：业务 / 持久化 / 效果核对。
        "checks": checks,
        # 本轮追踪的修订号（单调递增）。
        "revision": int(trace.get("revision", 0) or 0),
        # 不属于某一轮的阶段事实（接受 / 正式生效 / 错误）。
        "lifecycle": build_lifecycle(events),
        # 逐轮人话摘要：与流式展示**同一份数据**。
        "rounds": rounds,
        "messages": _summary_messages(
            iterations=iterations,
            business_rounds=business_rounds,
            grace_rounds=grace_used,
            budget=budget,
            tool_calls=tool_calls,
            submitted=submitted,
            submission_accepted=submission_accepted,
            tried=tried,
            candidate_registered=candidate_registered,
            applied=applied,
            error_category=error_category,
            rounds=rounds,
        ),
    }


def _summary_messages(
    *,
    iterations: int,
    business_rounds: int,
    grace_rounds: int,
    budget: int,
    tool_calls: int,
    submitted: bool,
    submission_accepted: bool,
    tried: bool,
    candidate_registered: bool,
    applied: bool,
    error_category: str,
    rounds: list[dict[str, Any]] | None = None,
) -> list[str]:
    """给人看的几行：每行只说**确实发生**的事。"""
    lines: list[str] = []
    if budget:
        lines.append(
            f"本轮预算 {budget} 圈，实际用了 {business_rounds} 圈业务推理。"
        )
    else:
        lines.append(f"本轮实际推理迭代 {iterations} 圈。")
    if grace_rounds:
        # 宽限单独说：它**不是**业务轮，也不是「第二次催交」。
        lines.append(f"另有 {grace_rounds} 圈框架结构化输出宽限（不计入业务轮）。")
    if tool_calls:
        lines.append(f"共调用工具 {tool_calls} 次。")
    if submission_accepted:
        lines.append("已提交修改计划。")
    elif submitted:
        lines.append("调用了提交工具，但提交没有通过。")
    else:
        lines.append("没有提交修改计划。")
    lines.append(
        "已进入修改计划预执行（试做跑在草稿复制品上）。"
        if tried
        else "没有进入修改计划预执行。"
    )
    lines.append(
        "已形成待接受候选。" if candidate_registered else "没有形成待接受候选。"
    )
    lines.append(
        "修改已正式生效。" if applied else "修改没有正式生效，草稿未被改动。"
    )
    if error_category:
        lines.append(f"失败原因：{ERROR_MESSAGES.get(error_category, error_category)}。")
    for round_summary in rounds or []:
        lines.append(str(round_summary.get("text") or ""))
    return lines


#: 工具侧失败类别的优先级：越靠前越**具体**。
#:
#: 「试做失败」「参数不合法」都比「工具报错」具体；通用兜底 ``unknown``
#: 不参与排序（它是兜底，不是已知原因）。
_CATEGORY_PRIORITY: tuple[str, ...] = (
    ERR_ACCEPT_FAILED,
    ERR_TRIAL_FAILED,
    ERR_PARAMS_INVALID,
    ERR_TOOL_ERROR,
    ERR_TOOL_LOOKUP_FAILED,
)


def latest_error_category() -> str:
    """本轮**已知**的失败类别（只取一个，最具体的那个）。

    只看**工具侧**报出来的类别（试做失败 / 参数不合法 / 工具报错 /
    查不到目标）。模型侧与预算侧的分类由调用方自己判 —— 两者不混用。

    没有已知类别时返回空串，调用方再按停止原因兜底。
    """
    trace = _TRACE.get()
    if trace is None:
        return ""
    seen: set[str] = set()
    for event in trace.get("events") or []:
        category = str(event.get("errorCategory") or "")
        if category:
            seen.add(category)
    for category in _CATEGORY_PRIORITY:
        if category in seen:
            return category
    return ""


def set_budget(budget: int) -> None:
    """记下本轮实际用的预算（写进后续每条事件）。"""
    trace = _TRACE.get()
    if trace is not None:
        trace["budget"] = int(budget or 0)


def set_reply_index(reply_index: int) -> None:
    """记下**当前这一轮**是第几次 ``agent.reply()``（1 起）。

    工具自己不知道自己在第几次 reply 里 —— 靠这里继承，工具侧事件的
    ``replyIndex`` 才不会退化成 0。
    """
    trace = _TRACE.get()
    if trace is not None:
        trace["replyIndex"] = int(reply_index or 0)


def set_ids(*, item_id: str = "", plan_id: str = "") -> None:
    """补上本轮的修改事项 / 候选计划标识。

    **真实存在的才写** —— 不提前造候选 id，也不从别的请求借。
    """
    trace = _TRACE.get()
    if trace is None:
        return
    if item_id:
        trace["itemId"] = str(item_id)
    if plan_id:
        trace["planId"] = str(plan_id)


def set_message_id(message_id: str) -> None:
    """记下本轮请求的 ``messageId``（用于迟到事件的请求归属）。"""
    trace = _TRACE.get()
    if trace is None:
        return
    trace["messageId"] = str(message_id or "")


def set_current_iteration(iteration: int | None) -> None:
    """记下**当前正在跑的这一轮**是第几轮（1 起）。

    工具自己不知道自己在第几轮（工具拿不到框架的迭代计数）——
    Agent 在框架事件到达时把真实轮次写进来，工具侧事件靠它归轮。
    """
    trace = _TRACE.get()
    if trace is None:
        return
    trace["currentIteration"] = iteration


def has_accepted_submission() -> bool:
    """本轮是否**真的提交成功**过（按追踪事件判定，不猜）。"""
    trace = _TRACE.get()
    if trace is None:
        return False
    for event in trace.get("events") or []:
        if event.get("stage") != STAGE_SUBMIT:
            continue
        if (event.get("result") or {}).get("accepted") is True:
            return True
        if event.get("businessOk") is True:
            return True
    return False


def has_submission_attempt() -> bool:
    """本轮是否**调用过**终结工具（调用了但没通过也算）。"""
    trace = _TRACE.get()
    if trace is None:
        return False
    return any(
        event.get("stage") == STAGE_SUBMIT
        for event in trace.get("events") or []
    )


# --------------------------------------------------------------------------
# 逐轮人话摘要
# --------------------------------------------------------------------------
#
# 「ReAct 第 N 轮」= **一次真实的框架循环**（想 → 调工具 → 收结果）。
# 同一轮里的多个工具调用属于**同一轮**，并排列出，不编造先后。
#
# 这一层只做一件事：把同一批事件翻译成人话。流式展示与最终摘要都调
# ``build_round_summaries`` —— **同一份数据**，不存在第二个来源。

#: 「未取得」的说法（缺什么就说什么，不填）。
_MISSING_TEXT = "未取得"


def _shorten(text: str, limit: int) -> str:
    """压成一行、限长。"""
    flat = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(flat) <= limit:
        return flat
    return flat[:limit] + "…"


def _title_map(events: list[dict[str, Any]]) -> dict[str, str]:
    """区域 id → 卡片显示名（用户看到的名字，不是技术 id）。"""
    titles: dict[str, str] = {}
    for event in events:
        if event.get("stage") != STAGE_TRIAL:
            continue
        result = event.get("result") or {}
        region_id = str(result.get("regionId") or "")
        title = str(result.get("title") or "")
        if region_id and title:
            titles.setdefault(region_id, title)
    return titles


def _ref_names(events: list[dict[str, Any]]) -> set[str]:
    """模型自造的引用名（``ref``）—— 属于技术细节，不进正文。"""
    refs: set[str] = set()
    for event in events:
        if event.get("stage") != STAGE_TOOL_CALL:
            continue
        for operation in (event.get("params") or {}).get("operations") or []:
            if not isinstance(operation, dict):
                continue
            ref = str((operation.get("target") or {}).get("ref") or "")
            if ref:
                refs.add(ref)
    return refs


def _replace_known_ids(
    text: str,
    titles: dict[str, str],
    refs: set[str],
) -> str:
    """把已知的 id / 引用名换成用户看到的名字。"""
    out = str(text or "")
    for region_id, title in titles.items():
        if region_id and region_id in out:
            out = out.replace(region_id, title)
    for ref in refs:
        if ref and ref in out:
            out = out.replace(ref, "新增区块")
    return out


#: 正文里像标识符的引号片段（``'r1'`` / ``"card_a"``…）。
_ID_TOKEN_RE = re.compile(r"['\"][A-Za-z0-9_\-]{2,}['\"]")

#: 正文里**裸露**的技术标识符（``pie_level`` / ``region_x`` …）。
#:
#: 错误原文里常带着这类 token。正文只说「哪一步、哪条约束没过」，
#: 标识符留给技术细节 —— 不直接拼进正文。
_BARE_ID_RE = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")
_REGION_ID_RE = re.compile(r"\bregion_[^\s,，。；、:：)）]+")


#: **已知的错误码 / 原因码**：这些是技术码，但留在正文里无害 ——
#: 它们不指向用户数据，也不是目标标识。其余的裸标识符一律抹掉。
_KEEP_CODE_TOKENS: frozenset[str] = frozenset(
    list(REASON_PHRASES)
    + [
        ERR_MODEL_UNAVAILABLE,
        ERR_MODEL_TIMEOUT,
        ERR_MODEL_ERROR,
        ERR_TOOL_LOOKUP_FAILED,
        ERR_TOOL_ERROR,
        ERR_PARAMS_INVALID,
        ERR_TRIAL_FAILED,
        ERR_ACCEPT_FAILED,
        ERR_BUDGET_EXHAUSTED,
        ERR_NOT_SUBMITTED,
        ERR_UNKNOWN,
    ]
)


def _strip_id_tokens(text: str) -> str:
    """把正文里剩下的标识符片段抹掉（错误原文不能直接进正文）。

    已知的原因码 / 错误码**保留** —— 它们是「哪条约束没过」的线索，
    不指向用户数据；其余的裸标识符（区域 id、模型自造的引用名）
    一律换成中性说法。
    """

    def _keep_known(match: "re.Match[str]") -> str:
        token = match.group(0)
        if token in _KEEP_CODE_TOKENS:
            return token
        return "「目标区块」"

    out = _ID_TOKEN_RE.sub("「目标区块」", str(text or ""))
    out = _REGION_ID_RE.sub("「目标区块」", out)
    out = _BARE_ID_RE.sub(_keep_known, out)
    return out


def _sanitize(
    text: str,
    titles: dict[str, str],
    refs: set[str],
) -> str:
    """把**错误原文**变成能进正文的说法。

    错误消息里带的是区域 id、引用名这类技术标识符 —— 正文只说
    「哪一步、哪条约束没过」，标识符留在技术细节里。
    """
    out = _replace_known_ids(text, titles, refs)
    out = _strip_id_tokens(out)
    return re.sub(r"\s+", " ", out).strip()


def _round_key(event: dict[str, Any]) -> int | None:
    """事件属于第几轮（取不到就是 ``None`` → 「未取得」）。"""
    value = event.get("roundIteration")
    if isinstance(value, int):
        return value
    value = event.get("iteration")
    if isinstance(value, int):
        return value
    return None


def _round_group_key(event: dict[str, Any]) -> str:
    """一轮的**聚合键**：``traceId + replyIndex + iteration``。

    有 ``roundKey`` 就用它（服务端已经算好）；没有的（旧事件）退回
    ``replyIndex + iteration`` —— **不借用别的轮次**。
    """
    key = str(event.get("roundKey") or "")
    if key:
        return key
    reply_index = event.get("replyIndex")
    iteration = _round_key(event)
    return f"legacy::{reply_index if reply_index is not None else 'unknown'}::{iteration if iteration is not None else 'unknown'}"


def _is_round_fact(event: dict[str, Any]) -> bool:
    """这条事件是不是「某一轮里的事实」。"""
    stage = event.get("stage")
    if stage == STAGE_TOOL_CALL:
        return True
    if stage == STAGE_AGENT_REPLY and event.get("name") == EVENT_PUBLIC_TEXT:
        return True
    if stage in (
        STAGE_SUBMIT,
        STAGE_TRIAL,
        STAGE_CANDIDATE,
        STAGE_ACCEPTANCE,
        STAGE_APPLY,
    ):
        # 工具侧事件必须**落在某一轮里**才算数（否则会混进别的轮次）。
        return event.get("roundIteration") is not None
    return False


def _group_by_round(
    events: list[dict[str, Any]],
) -> list[tuple[str, list[dict[str, Any]]]]:
    """按**轮次唯一键**分组。同一轮的不同调用在轮内再分开。"""
    buckets: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for event in events:
        if not _is_round_fact(event):
            continue
        key = _round_group_key(event)
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(event)
    def _sort_key(key: str) -> tuple[int, int, str]:
        head = buckets[key][0] if buckets.get(key) else {}
        iteration = _round_key(head)
        reply_index = head.get("replyIndex")
        return (
            1 if iteration is None else 0,
            iteration if isinstance(iteration, int) else 0,
            f"{reply_index if reply_index is not None else 0:04d}:{key}",
        )

    return [(key, buckets[key]) for key in sorted(order, key=_sort_key)]


def _tool_calls_of(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """这一轮里的**真实工具调用**（按调用 id 去重，保留信息最全的一条）。"""
    calls: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for event in events:
        if event.get("stage") != STAGE_TOOL_CALL:
            continue
        call_id = str(event.get("toolCallId") or "")
        key = call_id or f"__anonymous_{len(order)}"
        entry = calls.get(key)
        if entry is None:
            entry = {
                "toolCallId": call_id,
                "name": str(event.get("name") or ""),
                "params": event.get("params") or {},
                "result": event.get("result") or {},
                "businessOk": event.get("businessOk"),
                "businessMessage": str(event.get("businessMessage") or ""),
                "businessReason": str(event.get("businessReason") or ""),
                "businessIssues": event.get("businessIssues") or [],
                "businessDetail": event.get("businessDetail") or {},
                "phase": str(event.get("phase") or ""),
                "draftChanged": event.get("draftChanged"),
                "submitShape": str(event.get("submitShape") or ""),
                "state": str((event.get("result") or {}).get("state") or ""),
                "truncated": bool(event.get("truncated")),
            }
            calls[key] = entry
            order.append(key)
            continue
        # 合并：结束事件里的信息更全（参数、真实返回都在那里）。
        for field in ("params", "result", "businessIssues", "businessDetail"):
            value = event.get(field)
            if value:
                entry[field] = value
        if event.get("name"):
            entry["name"] = str(event.get("name"))
        for field in ("businessOk", "draftChanged"):
            if event.get(field) is not None:
                entry[field] = event[field]
        for field in ("businessMessage", "businessReason", "phase", "submitShape"):
            if event.get(field):
                entry[field] = str(event.get(field))
        if (event.get("result") or {}).get("state"):
            entry["state"] = str(event["result"]["state"])
        entry["truncated"] = bool(entry["truncated"] or event.get("truncated"))
    return [calls[key] for key in order]


def _calls_of_round(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """这一轮里的**每次真实调用**（含它自己的步骤 / 前后变化）。

    分组键是 ``callKey``（轮次键 + 工具调用 id）——
    **同轮的不同调用并列**，不按时间接近、不按工具名归并。

    同一次调用里的**多次试做**靠 ``batchId`` 分开：
    第一次试做的「第 1 步」和第二次试做的「第 1 步」不是同一步。
    """
    groups: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for event in events:
        key = str(event.get("callKey") or "") or f"__nocall_{len(order)}"
        group = groups.get(key)
        if group is None:
            group = {
                "callKey": key,
                "toolCallId": str(event.get("toolCallId") or ""),
                "name": str(event.get("name") or ""),
                "params": {},
                "result": {},
                "state": "",
                "businessOk": None,
                "businessMessage": "",
                "businessReason": "",
                "businessIssues": [],
                "businessDetail": {},
                "phase": "",
                "draftChanged": None,
                "submitShape": "",
                "truncated": False,
                "checks": {},
                "candidateRegistered": None,
                "applied": None,
                "changes": [],
                "structure": {},
                "steps": {},
                "stepOrder": [],
                "batchIds": [],
                "attribution": {},
            }
            groups[key] = group
            order.append(key)
        stage = event.get("stage")
        if stage == STAGE_TOOL_CALL:
            for field in ("params", "result", "businessIssues", "businessDetail"):
                value = event.get(field)
                if value:
                    group[field] = value
            if event.get("name"):
                group["name"] = str(event.get("name"))
            for field in ("businessOk", "draftChanged", "candidateRegistered", "applied"):
                if event.get(field) is not None:
                    group[field] = event[field]
            for field in ("businessMessage", "businessReason", "phase", "submitShape"):
                if event.get(field):
                    group[field] = str(event[field])
            if (event.get("result") or {}).get("state"):
                group["state"] = str(event["result"]["state"])
        else:
            # 提交 / 试做 / 候选 / 接受 / 生效阶段：也带事实。
            for field in ("businessOk", "candidateRegistered", "applied"):
                if event.get(field) is not None:
                    group[field] = event[field]
            for field in ("businessMessage", "businessReason", "phase"):
                if event.get(field):
                    group[field] = str(event[field])
            if event.get("draftChanged") is not None:
                group["draftChanged"] = event["draftChanged"]
            if event.get("businessIssues"):
                group["businessIssues"] = list(event["businessIssues"])
            if event.get("businessDetail"):
                group["businessDetail"] = dict(event["businessDetail"])
        if event.get("checks"):
            group["checks"] = dict(event.get("checks") or {})
        if event.get("changes"):
            group["changes"] = list(event.get("changes") or [])
        if event.get("structure"):
            group["structure"] = dict(event.get("structure") or {})
        if event.get("attribution"):
            group["attribution"] = dict(event.get("attribution") or {})
        group["truncated"] = bool(group["truncated"] or event.get("truncated"))

        # 步骤：按 stepKey（调用 + 批次 + 真实下标）归并。
        if stage == STAGE_TRIAL:
            step_key_value = str(event.get("stepKey") or "") or key
            result = event.get("result") or {}
            index = result.get("index")
            if not isinstance(index, int):
                index = event.get("stepIndex")
            step = group["steps"].get(step_key_value)
            if step is None:
                step = {
                    "stepKey": step_key_value,
                    "batchId": str(event.get("batchId") or ""),
                    "index": index,
                    "ok": None,
                    "title": "",
                    "regionId": "",
                    "reason": "",
                    "constraint": "",
                    "changes": [],
                    "structure": {},
                    "businessOk": None,
                    "notExecuted": False,
                    "phase": str(event.get("phase") or ""),
                    "wrapperPath": list(event.get("wrapperPath") or []),
                    "subStepIndex": event.get("subStepIndex"),
                    "kind": "substep" if event.get("wrapperPath") else "step",
                }
                group["steps"][step_key_value] = step
                group["stepOrder"].append(step_key_value)
            if result.get("title"):
                step["title"] = str(result["title"])
            if event.get("wrapperPath"):
                step["wrapperPath"] = list(event.get("wrapperPath") or [])
                step["subStepIndex"] = event.get("subStepIndex")
                step["kind"] = "substep"
            # 子步骤的**子操作名**（包装能力内部那一步叫什么）。
            extra = event.get("extra") or {}
            if isinstance(extra, dict) and extra.get("subOp") and not step["title"]:
                step["title"] = str(extra["subOp"])
            if result.get("regionId"):
                step["regionId"] = str(result["regionId"])
            if result.get("reason"):
                step["reason"] = str(result["reason"])
            if result.get("constraint"):
                step["constraint"] = str(result["constraint"])
            if result.get("ok") is not None:
                step["ok"] = bool(result.get("ok"))
            if event.get("businessOk") is not None:
                step["businessOk"] = event.get("businessOk")
            # 「未执行」：失败批次里**后面的**步骤根本没跑 ——
            # 不能当成成功，也不能当成「无变化」。
            not_executed = result.get("notExecuted")
            if not_executed is None:
                not_executed = event.get("notExecuted")
            if not_executed is None:
                extra = event.get("extra") or {}
                if isinstance(extra, dict):
                    not_executed = extra.get("notExecuted")
            if not_executed is not None:
                step["notExecuted"] = bool(not_executed)
            if event.get("changes"):
                step["changes"] = list(event.get("changes") or [])
            if event.get("structure"):
                step["structure"] = dict(event.get("structure") or {})
            if event.get("phase"):
                step["phase"] = str(event.get("phase"))
        batch_id = str(event.get("batchId") or "")
        if batch_id and batch_id not in group["batchIds"]:
            group["batchIds"].append(batch_id)
    return [groups[key] for key in order]


def _trial_steps(events: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """试做里**每一步**的结果（按步骤下标）。

    同一次调用里的**多次试做**各有各的步骤 —— 这里按**最后一次**
    批次给标签（最新一次才是当前状态），但**每一步的事实都留在事件里**
    （按批次分开，前端能看到每一次试做）。
    """
    steps: dict[int, dict[str, Any]] = {}
    for event in events:
        if event.get("stage") != STAGE_TRIAL:
            continue
        result = event.get("result") or {}
        index = result.get("index")
        if not isinstance(index, int):
            continue
        if result.get("notExecuted"):
            # 「未执行」不算一步**做过**的事：不覆盖已有记录。
            continue
        entry = steps.setdefault(index, {})
        if result.get("title"):
            entry["title"] = str(result["title"])
        if result.get("regionId"):
            entry["regionId"] = str(result["regionId"])
        if result.get("reason"):
            entry["reason"] = str(result["reason"])
        if result.get("constraint"):
            entry["constraint"] = str(result["constraint"])
        if event.get("structure"):
            entry["structure"] = dict(event.get("structure") or {})
        entry["ok"] = bool(result.get("ok"))
    return steps


def _step_name(
    operation: dict[str, Any],
    info: dict[str, Any],
    titles: dict[str, str],
) -> str:
    """一步的**人话名字**（卡片显示名）。取不到就空着 → 「未取得」。"""
    title = str(info.get("title") or "").strip()
    if not title:
        title = str(operation.get("summary") or "").strip()
        if title:
            title = _shorten(title, 30)
    if not title:
        region_id = str((operation.get("target") or {}).get("regionId") or "")
        title = titles.get(region_id, "")
    if not title:
        # 步骤自己没名字（包装能力整批执行）→ 用它**实际影响到的**第一个
        # 可见名称顶上，而不是写「名称未取得」。
        for item in (info.get("structure") or {}).get("affected") or []:
            if isinstance(item, dict) and str(item.get("title") or ""):
                title = str(item["title"])
                break
    return title


def _plan_steps(
    call: dict[str, Any],
    events: list[dict[str, Any]],
    titles: dict[str, str],
) -> list[str]:
    """计划里**每一步**的人话说明。

    每一步都要看得见：少了哪一步就**明说**少了，不静悄悄省略。
    """
    operations = [
        operation
        for operation in ((call.get("params") or {}).get("operations") or [])
        if isinstance(operation, dict)
    ]
    trial_steps = _trial_steps(events)
    total = max(len(operations), (max(trial_steps) + 1) if trial_steps else 0)
    steps: list[str] = []
    for index in range(total):
        operation = operations[index] if index < len(operations) else {}
        info = trial_steps.get(index) or {}
        name = _step_name(operation, info, titles)
        label = f"第 {index + 1} 步"
        if name:
            label = f"{label}「{name}」"
        else:
            label = f"{label}（名称{_MISSING_TEXT}）"
        steps.append(label)
    return steps


def _submit_phrase(tool_name: str) -> str:
    """提交类工具 → 人话动作。"""
    return SUBMIT_ACTION_PHRASES.get(
        tool_name, "提交了本轮结论"
    )


def _read_phrase(tool_name: str) -> str:
    """查询类工具 → 人话动作。"""
    if tool_name.startswith(_READ_TOOL_PREFIXES):
        return "查证了相关信息"
    return "调用了后台工具"


def _thinking_of(
    events: list[dict[str, Any]],
    titles: dict[str, str],
    refs: set[str],
) -> tuple[str, str]:
    """思考摘要 —— **只**来自模型的公开文本。

    没有公开文本就写「思考摘要未提供」，不拿别的东西顶替。
    """
    parts: list[str] = []
    for event in events:
        if event.get("stage") != STAGE_AGENT_REPLY:
            continue
        if event.get("name") != EVENT_PUBLIC_TEXT:
            continue
        text = str((event.get("params") or {}).get("text") or "")
        if text.strip():
            parts.append(text)
    if not parts:
        return THINKING_MISSING, SOURCE_MISSING
    joined = _replace_known_ids(" ".join(parts), titles, refs)
    return _shorten(joined, 160), SOURCE_MODEL_PUBLIC_TEXT


def _action_of(
    calls: list[dict[str, Any]],
    events: list[dict[str, Any]],
    titles: dict[str, str],
) -> tuple[str, str, list[str]]:
    """实际行动 —— 根据**真实调用 / 实际提交的计划**整理。

    返回 ``(正文, 来源, 每一步的名字)``。正文里永远带着「根据实际
    调用/提交计划整理」这层标签，不会被当成模型自己的话。
    """
    plan_names: list[str] = []
    if not calls:
        # 拿不到框架的调用记录 → 用**执行层**记下的试做/提交事实整理，
        # 绝不凭空编出做了什么。
        steps = _trial_step_labels(events, titles, only_failed=False)
        if steps:
            return (
                f"按提交的计划执行了 {len(steps)} 步试做：" + "；".join(steps),
                SOURCE_DERIVED_FROM_PLAN,
                steps,
            )
        if any(event.get("stage") == STAGE_SUBMIT for event in events):
            return "提交了修改计划", SOURCE_DERIVED_FROM_PLAN, plan_names
        return "", SOURCE_MISSING, plan_names

    phrases: list[str] = []
    for call in calls:
        name = str(call.get("name") or "")
        if name.startswith("submit_"):
            steps = _plan_steps(call, events, titles)
            if not plan_names:
                plan_names = steps
            phrase = _submit_phrase(name)
            if steps:
                phrase = f"{phrase}（{len(steps)} 步）：" + "；".join(steps)
            else:
                phrase = f"{phrase}（步骤数{_MISSING_TEXT}）"
            phrases.append(phrase)
        else:
            phrases.append(_read_phrase(name))
    return "；".join(item for item in phrases if item), SOURCE_DERIVED_FROM_CALLS, plan_names


def _business_facts(
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    """这一轮的业务事实。

    **只认业务结果**：``businessOk`` 为 ``None`` 就是「未取得」，
    绝不拿框架的 ``state`` 当业务成功。
    """
    facts: dict[str, Any] = {
        "businessOk": None,
        "businessMessage": "",
        "businessReason": "",
        "businessIssues": [],
        "businessDetail": {},
    }
    for event in events:
        value = event.get("businessOk")
        if value is None:
            continue
        facts["businessOk"] = bool(value)
        facts["businessMessage"] = str(event.get("businessMessage") or "")
        facts["businessReason"] = str(event.get("businessReason") or "")
        facts["businessIssues"] = list(event.get("businessIssues") or [])
        facts["businessDetail"] = dict(event.get("businessDetail") or {})
    return facts


def _trial_step_labels(
    events: list[dict[str, Any]],
    titles: dict[str, str],
    *,
    only_failed: bool = True,
) -> list[str]:
    """试做里那些步的人话标签（「哪一步没过」/「做了哪几步」）。"""
    labels: list[str] = []
    for index, info in sorted(_trial_steps(events).items()):
        if only_failed and info.get("ok") is not False:
            continue
        name = str(info.get("title") or "")
        if not name:
            name = titles.get(str(info.get("regionId") or ""), "")
        if not name:
            for item in (info.get("structure") or {}).get("affected") or []:
                if isinstance(item, dict) and str(item.get("title") or ""):
                    name = str(item["title"])
                    break
        label = f"第 {index + 1} 步"
        label = f"{label}「{name}」" if name else f"{label}（名称{_MISSING_TEXT}）"
        labels.append(label)
    return labels


def _issue_texts(
    issues: list[Any],
    plan_names: list[str],
    trial_labels: list[str],
    titles: dict[str, str],
    refs: set[str],
) -> list[str]:
    """把结构化的业务问题翻成人话（不抄错误原文）。"""
    texts: list[str] = []
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        index = issue.get("step")
        constraint = str(issue.get("constraint") or "").strip()
        reason = str(issue.get("reason") or "").strip()
        piece = ""
        if constraint:
            piece = _shorten(constraint, 60)
        elif reason:
            piece = REASON_PHRASES.get(reason, "")
        if not piece:
            continue
        label = ""
        if isinstance(index, int) and 0 <= index < len(plan_names):
            label = plan_names[index]
        elif trial_labels:
            # 问题本身没带步号 → 用试做里**确实没过的那一步**。
            label = trial_labels[0]
        texts.append(f"{label}未通过：{piece}" if label else piece)
    return texts


def _backend_of(
    events: list[dict[str, Any]],
    calls: list[dict[str, Any]],
    facts: dict[str, Any],
    titles: dict[str, str],
    refs: set[str],
    plan_names: list[str],
    trial_labels: list[str],
    phase: str,
) -> tuple[str, str]:
    """后台结果 —— 说的是**实际执行情况**，不是框架状态。

    返回 ``(正文, 来源)``。
    """
    ok = facts.get("businessOk")
    message = str(facts.get("businessMessage") or "")
    issues = list(facts.get("businessIssues") or [])
    has_state = any(str(call.get("state") or "") for call in calls)

    if ok is None:
        if has_state:
            return (
                f"{BACKEND_MISSING}（只拿到框架状态，不能据此判断业务成功）",
                SOURCE_FRAMEWORK_STATE,
            )
        return BACKEND_MISSING, SOURCE_MISSING

    if ok:
        if phase == PHASE_CANDIDATE:
            text = "后台检查通过，已形成待你确认的候选"
        elif phase == PHASE_TRIAL:
            text = "后台试做通过（在草稿复制品上执行成功）"
        elif phase == PHASE_APPLIED:
            text = "后台已正式生效"
        elif phase == PHASE_ACCEPTED:
            text = "后台已接受这次修改"
        else:
            text = "后台已接受这次调用"
        extra = _shorten(_sanitize(message, titles, refs), 120)
        if extra and extra not in text:
            text = f"{text}：{extra}"
        return text, SOURCE_BUSINESS_PAYLOAD

    details = _issue_texts(issues, plan_names, trial_labels, titles, refs)
    if not details:
        fallback = _shorten(_sanitize(message, titles, refs), 120)
        if fallback:
            details = [fallback]
    if not details:
        reason = str(facts.get("businessReason") or "")
        details = [REASON_PHRASES.get(reason, "具体原因未取得")]
    return "后台未通过：" + "；".join(details), SOURCE_BUSINESS_PAYLOAD


def _group_trial_steps(group: dict[str, Any]) -> dict[int, dict[str, Any]]:
    """一次调用里的试做步骤（按真实下标）。"""
    steps: dict[int, dict[str, Any]] = {}
    for key in group.get("stepOrder") or []:
        step = (group.get("steps") or {}).get(key) or {}
        index = step.get("index")
        if not isinstance(index, int):
            continue
        steps[index] = step
    return steps


def _group_plan_steps(
    group: dict[str, Any], titles: dict[str, str]
) -> list[str]:
    """一次调用里的**计划步骤**人话说明（只算它自己的）。"""
    operations = [
        operation
        for operation in ((group.get("params") or {}).get("operations") or [])
        if isinstance(operation, dict)
    ]
    trial_steps = _group_trial_steps(group)
    total = max(len(operations), (max(trial_steps) + 1) if trial_steps else 0)
    steps: list[str] = []
    for index in range(total):
        operation = operations[index] if index < len(operations) else {}
        info = trial_steps.get(index) or {}
        name = _step_name(operation, info, titles)
        label = f"第 {index + 1} 步"
        if name:
            label = f"{label}「{name}」"
        else:
            label = f"{label}（名称{_MISSING_TEXT}）"
        steps.append(label)
    return steps


def _backend_of_group(
    group: dict[str, Any],
    titles: dict[str, str],
    refs: set[str],
    plan_names: list[str],
    phase: str,
) -> tuple[str, str]:
    """一次调用的后台结果 —— **只认这一次调用自己的**业务事实。

    为什么按调用分开：同一轮里两个调用各有各的成败。
    把它们合成一句「后台未通过：最后一个结果」会把**先前的失败抹掉** ——
    那正是「目标查询失败被后来的提交成功盖掉」这类问题的根源。
    """
    ok = group.get("businessOk")
    message = str(group.get("businessMessage") or "")
    issues = list(group.get("businessIssues") or [])
    state = str(group.get("state") or "")
    trial_labels = [
        f"第 {int(index) + 1} 步"
        for index, step in sorted(_group_trial_steps(group).items())
        if step.get("ok") is False
    ]

    if ok is None:
        if state:
            return (
                f"{BACKEND_MISSING}（只拿到框架状态，不能据此判断业务成功）",
                SOURCE_FRAMEWORK_STATE,
            )
        return BACKEND_MISSING, SOURCE_MISSING

    if ok:
        if phase == PHASE_CANDIDATE:
            text = "后台检查通过，已形成待你确认的候选"
        elif phase == PHASE_TRIAL:
            text = "后台试做通过（在草稿复制品上执行成功）"
        elif phase == PHASE_APPLIED:
            text = "后台已正式生效"
        elif phase == PHASE_ACCEPTED:
            text = "后台已接受这次修改"
        else:
            text = "后台已接受这次调用"
        extra = _shorten(_sanitize(message, titles, refs), 120)
        if extra and extra not in text:
            text = f"{text}：{extra}"
        return text, SOURCE_BUSINESS_PAYLOAD

    details = _issue_texts(issues, plan_names, trial_labels, titles, refs)
    if not details:
        fallback = _shorten(_sanitize(message, titles, refs), 120)
        if fallback:
            details = [fallback]
    if not details:
        reason = str(group.get("businessReason") or "")
        details = [REASON_PHRASES.get(reason, "具体原因未取得")]
    return "后台未通过：" + "；".join(details), SOURCE_BUSINESS_PAYLOAD


def _phase_of(events: list[dict[str, Any]]) -> str:
    """这一轮走到了哪个阶段（取**最靠后**的那个）。"""
    best = ""
    best_index = -1
    for event in events:
        phase = str(event.get("phase") or "")
        if phase not in PHASE_ORDER:
            continue
        index = PHASE_ORDER.index(phase)
        if index > best_index:
            best_index = index
            best = phase
    return best


def _draft_changed_of(
    events: list[dict[str, Any]],
    phase: str,
) -> bool | None:
    """**正式**草稿有没有被改（取不到就是 ``None`` → 「未取得」）。"""
    found: bool | None = None
    for event in events:
        value = event.get("draftChanged")
        if value is None:
            continue
        if value is True:
            return True
        found = False
    if found is False:
        return False
    if phase == PHASE_APPLIED:
        return True
    return None


def _shape_of(params: Any) -> str:
    """提交形状的指纹（没带指纹时从参数里兜底算一个）。"""
    if not isinstance(params, dict):
        return ""
    shape = {
        "intent": params.get("intent"),
        "operations": params.get("operations"),
        "operation": params.get("operation"),
        "target": params.get("target"),
    }
    shape = {
        key: value
        for key, value in shape.items()
        if value not in (None, "", [], {})
    }
    if not shape:
        return ""
    try:
        canonical = json.dumps(shape, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:  # noqa: BLE001
        canonical = str(shape)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def _repeat_submissions(events: list[dict[str, Any]]) -> set[str]:
    """找出「又提交了同一份没改好的东西」的调用。

    判据：**后一次**提交的形状与**前一次**完全一样，且**前一次没有通过**
    （业务结果为失败）。形状一样 = 没改；改成别的 = 改好了，不算重复。
    """
    seen: dict[str, bool] = {}
    repeated: set[str] = set()
    for event in events:
        if event.get("stage") != STAGE_TOOL_CALL:
            continue
        name = str(event.get("name") or "")
        if not name.startswith("submit_"):
            continue
        signature = str(event.get("submitShape") or "") or _shape_of(
            event.get("params") or {}
        )
        if not signature:
            continue
        rejected_before = seen.get(signature)
        if rejected_before:
            repeated.add(str(event.get("toolCallId") or ""))
            continue
        seen[signature] = event.get("businessOk") is False
    return repeated


# --------------------------------------------------------------------------
# 「实际变化」：从精确 before/after 出人话
# --------------------------------------------------------------------------
#
# 规则（09 号 Prompt §4）：
#
# * 变化正文必须**能解释** —— 说清哪个分组、几列改几列、卡片落在第几列第几行；
# * 只在**后台真的给出了 before/after** 时才输出这种句子；
# * 给不出就写「变化细节未取得」，**不套用示例句式**；
# * 父级没有变化也要说 —— 那是排障时最常问的一句。

#: 变化事实缺失时的说法。
CHANGES_MISSING = "变化细节未取得"


def _gap_text(gap: Any) -> str:
    """间距 → 人话（``{"value": 16, "unit": "PX"}`` → 「16 像素」）。"""
    if not isinstance(gap, dict):
        return ""
    value = gap.get("value")
    if value is None:
        return ""
    unit = str(gap.get("unit") or "").upper()
    if unit in ("PX", ""):
        return f"{value} 像素"
    if unit == "PERCENT":
        return f"{value}%"
    return f"{value} {unit}"


def _columns_of(value: Any) -> int | None:
    """从一个排列 / 放置结构里取列数。"""
    if not isinstance(value, dict):
        return None
    columns = value.get("columns")
    if isinstance(columns, int):
        return columns
    return None


def _placement_text(placement: Any) -> str:
    """放置 → 人话（「第 3 列、第 1 行、占 1 列」）。"""
    if not isinstance(placement, dict):
        return ""
    parts: list[str] = []
    column = placement.get("column")
    row = placement.get("row")
    if isinstance(column, int):
        parts.append(f"第 {column} 列")
    if isinstance(row, int):
        parts.append(f"第 {row} 行")
    span = placement.get("columnSpan")
    if isinstance(span, int):
        parts.append(f"占 {span} 列")
    row_span = placement.get("rowSpan")
    if isinstance(row_span, int) and row_span != 1:
        parts.append(f"占 {row_span} 行")
    return "、".join(parts)


def _name_of(region_id: str, titles: dict[str, str], structure: dict[str, Any]) -> str:
    """一个区域的人话名字（可见名称优先，其次结构投影里的标题）。"""
    name = titles.get(region_id, "")
    if name:
        return name
    for item in (structure or {}).get("affected") or []:
        if isinstance(item, dict) and str(item.get("regionId") or "") == region_id:
            return str(item.get("title") or "")
    for item in (structure or {}).get("containers") or []:
        if isinstance(item, dict) and str(item.get("regionId") or "") == region_id:
            return str(item.get("title") or "")
    return ""


def _change_text_of(
    changes: list[Any],
    structure: dict[str, Any],
    titles: dict[str, str],
) -> str:
    """把精确 before/after 翻译成一句人话。

    例（**只有后台真的给出这些 before/after 时**才会这样写）：

    「该分组从两列改为三列；某卡片在其原父容器内改为第 3 列、第 1 行、占 1 列；
    父级没有变化」
    """
    if not changes:
        return ""
    texts: list[str] = []
    containers: dict[str, dict[str, Any]] = {}
    for item in (structure or {}).get("containers") or []:
        if isinstance(item, dict):
            containers[str(item.get("regionId") or "")] = item
    for change in changes:
        if not isinstance(change, dict):
            continue
        region_id = str(change.get("regionId") or "")
        path = str(change.get("path") or "")
        before = change.get("before")
        after = change.get("after")
        name = _name_of(region_id, titles, structure) or ""
        label = f"「{name}」" if name else "该区域"
        # 容器排列：列数 / 间距。
        before_columns = _columns_of(before)
        after_columns = _columns_of(after)
        if before_columns is not None or after_columns is not None:
            piece = ""
            if before_columns is not None and after_columns is not None:
                if before_columns != after_columns:
                    piece = f"{label}从 {before_columns} 列改为 {after_columns} 列"
                else:
                    piece = f"{label}仍为 {after_columns} 列"
            elif after_columns is not None:
                piece = f"{label}改为 {after_columns} 列"
            else:
                piece = f"{label}由 {before_columns} 列改为未取得"
            before_gap = _gap_text((before or {}).get("gap") if isinstance(before, dict) else None)
            after_gap = _gap_text((after or {}).get("gap") if isinstance(after, dict) else None)
            if before_gap and after_gap and before_gap != after_gap:
                piece = f"{piece}（间距 {before_gap} 改为 {after_gap}）"
            elif after_gap:
                piece = f"{piece}（间距 {after_gap}）"
            texts.append(piece)
            continue
        # 叶子卡片放置：第几列第几行。
        before_placement = (
            (before or {}).get("placement") if isinstance(before, dict) else None
        )
        after_placement = (
            (after or {}).get("placement") if isinstance(after, dict) else None
        )
        if before_placement is not None or after_placement is not None:
            before_text = _placement_text(before_placement)
            after_text = _placement_text(after_placement)
            parent_title = ""
            container = containers.get(region_id)
            if container is None:
                for item in (structure or {}).get("affected") or []:
                    if (
                        isinstance(item, dict)
                        and str(item.get("regionId") or "") == region_id
                    ):
                        parent_title = str(item.get("parentTitle") or "")
                        break
            else:
                parent_title = str(container.get("title") or "")
            parent_part = f"其原父容器「{parent_title}」内" if parent_title else "其原父容器内"
            if before_text and after_text and before_text != after_text:
                texts.append(f"{label}在{parent_part}由 {before_text} 改为 {after_text}")
            elif after_text:
                texts.append(f"{label}在{parent_part}改为 {after_text}")
            elif before_text:
                texts.append(f"{label}在{parent_part}原为 {before_text}")
            else:
                texts.append(f"{label}的放置位置未取得")
            continue
        # 其它路径：给不出具体解释就退回摘要（不编）。
        summary = str(change.get("summary") or "").strip()
        if summary:
            texts.append(f"{label}：{summary}")
        else:
            texts.append(f"{label}：{path or '变化路径未取得'}")
    # 父级是否变化：结构投影里的父容器事实说了算。
    parent_note = ""
    for item in (structure or {}).get("affected") or []:
        if not isinstance(item, dict):
            continue
        parent_changed = item.get("parentChanged")
        parent_title = str(item.get("parentTitle") or "")
        if parent_changed is True:
            parent_note = f"父级「{parent_title or '未取得名称'}」有变化"
        elif parent_changed is False:
            parent_note = "父级没有变化"
        if parent_note:
            break
    if parent_note:
        texts.append(parent_note)
    return "；".join(texts)


def _effect_text_of(
    *,
    checks: dict[str, Any],
    candidate_registered: bool | None,
    applied: bool | None,
    draft_changed: bool | None,
    phase: str,
) -> str:
    """「是否生效」—— 三类状态**分开**说。

    禁止用 ``done`` 或「操作正常返回」代替这些状态。
    """
    parts: list[str] = []
    business = str(checks.get("business") or CHECK_UNKNOWN)
    if business == CHECK_PASS:
        parts.append("副本试做通过")
    elif business == CHECK_FAIL:
        parts.append("副本试做失败")
    elif business == CHECK_NOT_RUN:
        parts.append("副本试做未执行")
    else:
        parts.append("副本试做结果未取得")

    if candidate_registered is True:
        parts.append("候选登记成功")
    elif candidate_registered is False:
        parts.append("候选登记失败")
    else:
        parts.append("候选登记未取得")

    persistence = str(checks.get("persistence") or PERSIST_UNKNOWN)
    if persistence == PERSIST_COMMITTED:
        parts.append("事务后生效")
    elif persistence == PERSIST_FAILED:
        parts.append("正式草稿未改（事务失败）")
    elif persistence == PERSIST_NOT_RUN:
        parts.append("正式草稿未改")
    else:
        # 持久化状态没记录下来时，**只按拿到的真实事实说**。
        if applied is True:
            parts.append("事务后生效")
        elif applied is False:
            parts.append("正式草稿未改")
        elif draft_changed is False:
            parts.append("正式草稿未改")
        elif draft_changed is True:
            parts.append("正式草稿已改（生效状态未取得）")
        elif phase == PHASE_APPLIED:
            parts.append("事务后生效")
        else:
            parts.append("正式草稿是否生效未取得")

    effect = str(checks.get("effect") or EFFECT_UNKNOWN)
    if effect == EFFECT_NOT_RUN:
        parts.append("效果核对未执行")
    elif effect == CHECK_PASS:
        parts.append("效果核对通过")
    elif effect == CHECK_FAIL:
        parts.append("效果核对未通过")
    else:
        parts.append("效果核对结果未取得")
    return "；".join(parts)



def _group_label(group: dict[str, Any], index: int) -> str:
    """一次调用的人话标签（**不暴露**原始工具名 / 能力名）。

    原始名字是技术信息，只进技术细节；正文只说用户能懂的说法。
    """
    name = str(group.get("name") or "")
    if name.startswith("submit_"):
        phrase = _submit_phrase(name)
    elif name.startswith(_READ_TOOL_PREFIXES):
        phrase = _read_phrase(name)
    else:
        phrase = _read_phrase(name)
    return f"第 {index} 次调用（{phrase}）"


def _round_label_of(
    key: str, events: list[dict[str, Any]]
) -> tuple[str, int | None, int]:
    """一轮的标题 + 轮号 + 回复序号。

    轮号取**框架真值**（事件里的 iteration）；取不到就写「轮次未取得」——
    不拿共享的「当前轮」猜。
    """
    iteration: int | None = None
    reply_index = 0
    for event in events:
        value = _round_key(event)
        if iteration is None and isinstance(value, int):
            iteration = value
        if not reply_index:
            reply_index = int(event.get("replyIndex", 0) or 0)
    if iteration is None:
        return "ReAct 轮次未取得", None, reply_index
    return f"ReAct 第 {iteration} 轮", iteration, reply_index


def _step_status_text(step: dict[str, Any]) -> str:
    """一步的**人话状态**（含「未执行」）。"""
    if step.get("notExecuted"):
        return "未执行"
    ok = step.get("ok")
    if ok is True:
        return "通过"
    if ok is False:
        return "未通过"
    return "结果未取得"


def _call_view(
    group: dict[str, Any],
    titles: dict[str, str],
    refs: set[str],
    index: int = 1,
) -> dict[str, Any]:
    """一次调用的**结构化视图**（给前端渲染用）。"""
    steps: list[dict[str, Any]] = []
    for key in group.get("stepOrder") or []:
        step = (group.get("steps") or {}).get(key) or {}
        change_text = _change_text_of(
            step.get("changes") or [], step.get("structure") or {}, titles
        )
        name = str(step.get("title") or "")
        if not name:
            name = titles.get(str(step.get("regionId") or ""), "")
        if not name:
            # 步骤本身没给名字（比如包装能力整批执行）→ 用它**实际影响到的**
            # 第一个可见名称顶上，而不是写「名称未取得」。
            for item in (step.get("structure") or {}).get("affected") or []:
                if isinstance(item, dict) and str(item.get("title") or ""):
                    name = str(item["title"])
                    break
        steps.append(
            {
                "stepKey": str(step.get("stepKey") or key),
                "batchId": str(step.get("batchId") or ""),
                "index": step.get("index"),
                "name": name,
                "ok": step.get("ok"),
                "statusText": _step_status_text(step),
                "notExecuted": bool(step.get("notExecuted")),
                "reason": str(step.get("reason") or ""),
                "constraint": str(step.get("constraint") or ""),
                # 包装能力的子操作：与父步骤**不是**同一件事。
                "kind": str(step.get("kind") or "step"),
                "wrapperPath": list(step.get("wrapperPath") or []),
                "subStepIndex": step.get("subStepIndex"),
                "changes": list(step.get("changes") or []),
                "structure": dict(step.get("structure") or {}),
                "changeText": change_text,
            }
        )
    change_text = _change_text_of(
        group.get("changes") or [], group.get("structure") or {}, titles
    )
    return {
        "callKey": str(group.get("callKey") or ""),
        "toolCallId": str(group.get("toolCallId") or ""),
        "name": str(group.get("name") or ""),
        # **人话标签**：正文用这个（可见说法），原始工具名只进技术细节。
        "label": _group_label(group, index),
        "state": str(group.get("state") or ""),
        "phase": str(group.get("phase") or ""),
        "businessOk": group.get("businessOk"),
        "businessReason": str(group.get("businessReason") or ""),
        "batchIds": list(group.get("batchIds") or []),
        "steps": steps,
        "changes": list(group.get("changes") or []),
        "structure": dict(group.get("structure") or {}),
        "changeText": change_text,
        "attribution": dict(group.get("attribution") or {}),
    }


def _summarize_round(
    key: str,
    events: list[dict[str, Any]],
    titles: dict[str, str],
    refs: set[str],
    repeated: set[str],
) -> dict[str, Any]:
    """把一轮的事件翻译成一行人话。

    正文分五层（09 号 Prompt §4）：
    公开决策摘要 / 实际调用及提交计划 / 后台逐步结果 / 实际变化 / 是否生效。
    """
    calls = _tool_calls_of(events)
    groups = _calls_of_round(events)
    thinking, summary_source = _thinking_of(events, titles, refs)
    action, action_source, plan_names = _action_of(calls, events, titles)
    facts = _business_facts(events)
    phase = _phase_of(events)
    trial_labels = _trial_step_labels(events, titles)
    if not plan_names:
        # 没有提交工具调用（拿不到计划清单）→ 至少说清**试做里哪一步没过**。
        plan_names = list(trial_labels)
    # 后台结果按**每次调用**分别说：同轮多个调用各自成败，
    # 不能用最后一个结果概括整轮（那会把先前的失败抹掉）。
    backend_parts: list[str] = []
    backend_sources: list[str] = []
    for index, group in enumerate(groups, start=1):
        group_plan_names = _group_plan_steps(group, titles)
        text, source = _backend_of_group(
            group, titles, refs, group_plan_names or plan_names, phase
        )
        backend_sources.append(source)
        label = _group_label(group, index)
        backend_parts.append(f"{label}：{text}")
    if backend_parts:
        backend = "；".join(backend_parts)
        result_source = (
            SOURCE_BUSINESS_PAYLOAD
            if SOURCE_BUSINESS_PAYLOAD in backend_sources
            else (backend_sources[0] if backend_sources else SOURCE_MISSING)
        )
    else:
        backend, result_source = _backend_of(
            events, calls, facts, titles, refs, plan_names, trial_labels, phase
        )
    draft_changed = _draft_changed_of(events, phase)
    round_label, iteration, reply_index = _round_label_of(key, events)

    # ---- 「实际变化」：所有调用的变化合起来 ----
    change_texts: list[str] = []
    all_changes: list[Any] = []
    all_structure: dict[str, Any] = {}
    for group in groups:
        text = _change_text_of(
            group.get("changes") or [], group.get("structure") or {}, titles
        )
        if text:
            change_texts.append(text)
        all_changes.extend(group.get("changes") or [])
        structure = group.get("structure") or {}
        if structure:
            merged = dict(all_structure)
            for field in ("affected", "containers"):
                merged[field] = list(merged.get(field) or []) + list(
                    structure.get(field) or []
                )
            all_structure = merged
    changes_text = "；".join(change_texts)

    # ---- 「是否生效」：三类状态分开 ----
    checks = _checks_of(events)
    candidate_registered = _candidate_of(events)
    applied = _applied_of(events)
    effect_text = _effect_text_of(
        checks=checks,
        candidate_registered=candidate_registered,
        applied=applied,
        draft_changed=draft_changed,
        phase=phase,
    )

    repeat = any(str(call.get("toolCallId") or "") in repeated for call in calls)
    truncated = any(bool(event.get("truncated")) for event in events)

    missing: list[str] = []
    if iteration is None:
        missing.append(MISSING_ITERATION)
    if summary_source == SOURCE_MISSING:
        missing.append("thinkingSummary")
    if result_source == SOURCE_MISSING:
        missing.append("businessResult")
    if any(not str(call.get("toolCallId") or "") for call in calls):
        missing.append(MISSING_TOOL_CALL_ID)
    if truncated:
        missing.append("detailTruncated")
    if not changes_text and any(
        group.get("changes") for group in groups
    ):
        missing.append(MISSING_CHANGES)
    if checks.get("effect") == EFFECT_NOT_RUN:
        missing.append(MISSING_EFFECT_CHECK)
    if checks.get("persistence") in (PERSIST_UNKNOWN, PERSIST_NOT_RUN):
        missing.append(MISSING_PERSIST_CHECK)

    if draft_changed is True:
        draft_changed_text = "是否改动正式草稿：是"
    elif draft_changed is False:
        draft_changed_text = "是否改动正式草稿：否"
    else:
        draft_changed_text = f"是否改动正式草稿：{_MISSING_TEXT}"

    action_text = action or f"实际行动{_MISSING_TEXT}"
    if repeat:
        action_text = f"{action_text}（{REPEAT_SUBMISSION_TEXT}）"
    if truncated:
        action_text = f"{action_text}（部分内容被截断）"

    text = (
        f"{round_label}：思考摘要：{thinking}；"
        f"实际行动（根据实际调用/提交计划整理）：{action_text}；"
        f"后台结果：{backend}；"
        f"实际变化：{changes_text or CHANGES_MISSING}；"
        f"是否生效：{effect_text}；{draft_changed_text}"
    )

    call_views = [
        _call_view(group, titles, refs, index)
        for index, group in enumerate(groups, start=1)
    ]

    return {
        "key": str(key),
        "round": iteration,
        "roundLabel": round_label,
        "replyIndex": reply_index,
        "thinking": thinking,
        "summarySource": summary_source,
        "action": action_text,
        "actionSource": action_source,
        "backend": backend,
        "resultSource": result_source,
        # 新增：实际变化 / 是否生效。
        "changesText": changes_text,
        "changes": all_changes,
        "structure": all_structure,
        "effectText": effect_text,
        "checks": checks,
        "candidateRegistered": candidate_registered,
        "applied": applied,
        "calls": call_views,
        "businessOk": facts.get("businessOk"),
        "businessMessage": str(facts.get("businessMessage") or ""),
        "businessReason": str(facts.get("businessReason") or ""),
        "businessIssues": list(facts.get("businessIssues") or []),
        "phase": phase,
        "phaseLabel": PHASE_LABELS.get(phase, ""),
        "draftChanged": draft_changed,
        "draftChangedText": draft_changed_text,
        "repeatSubmission": repeat,
        "repeatText": REPEAT_SUBMISSION_TEXT if repeat else "",
        "toolCallIds": [
            str(call.get("toolCallId") or "") for call in calls
        ],
        "missing": missing,
        "truncated": truncated,
        "text": text,
        "technical": _technical_of(calls, facts, groups),
        "at": str(events[-1].get("at") or "") if events else "",
    }


def _checks_of(events: list[dict[str, Any]]) -> dict[str, str]:
    """这一轮的三类检查状态（业务 / 持久化 / 效果核对）。

    **分开**记，不混成一句「已生效」。
    """
    checks = {
        "business": CHECK_UNKNOWN,
        "persistence": PERSIST_UNKNOWN,
        "effect": EFFECT_UNKNOWN,
    }
    for event in events:
        raw = event.get("checks") or {}
        if not isinstance(raw, dict):
            continue
        for field in ("business", "persistence", "effect"):
            value = str(raw.get(field) or "")
            if value:
                checks[field] = value
    # 业务状态没单独写 → 从真实业务结果推（拿不到就是「未取得」）。
    #
    # **失败优先**：一轮里只要有一步业务失败，这一轮就不是「通过」。
    # 否则「提交被接受」会把随后的试做失败盖掉 —— 那正是
    # 「同轮多次提交/试做不能用后一份结果覆盖前一份失败」的反面。
    if checks["business"] == CHECK_UNKNOWN:
        seen_true = False
        for event in events:
            value = event.get("businessOk")
            if value is None:
                continue
            if not value:
                checks["business"] = CHECK_FAIL
                break
            seen_true = True
        else:
            if seen_true:
                checks["business"] = CHECK_PASS
    return checks


def _candidate_of(events: list[dict[str, Any]]) -> bool | None:
    """候选**是否真的登记了**（三态；工具提交槽成功**不算**登记成功）。"""
    found: bool | None = None
    for event in events:
        value = event.get("candidateRegistered")
        if value is None:
            continue
        if value is True:
            return True
        found = False
    return found


def _applied_of(events: list[dict[str, Any]]) -> bool | None:
    """是否**正式生效**（三态；只有事务成功后才可能是 True）。"""
    found: bool | None = None
    for event in events:
        value = event.get("applied")
        if value is None:
            continue
        if value is True:
            return True
        found = False
    return found


def _technical_of(
    calls: list[dict[str, Any]],
    facts: dict[str, Any],
    groups: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """技术细节（可展开）：能力名、参数名、id、错误码都在这里。"""
    technical: dict[str, Any] = {}
    if calls:
        technical["工具"] = [str(call.get("name") or "") for call in calls]
        technical["工具调用 id"] = [
            str(call.get("toolCallId") or "") for call in calls
        ]
        states = [str(call.get("state") or "") for call in calls]
        if any(states):
            technical["框架工具状态"] = states
        operations: list[Any] = []
        for call in calls:
            for operation in (call.get("params") or {}).get("operations") or []:
                if isinstance(operation, dict):
                    operations.append(operation)
        if operations:
            technical["提交的能力与参数"] = operations
        payloads = [
            (call.get("result") or {}).get("business")
            for call in calls
            if (call.get("result") or {}).get("business") is not None
        ]
        if payloads:
            technical["工具真实返回"] = payloads
    if facts.get("businessReason"):
        technical["业务原因码"] = str(facts.get("businessReason"))
    if facts.get("businessIssues"):
        technical["业务问题明细"] = list(facts.get("businessIssues") or [])
    if facts.get("businessDetail"):
        technical["业务明细"] = dict(facts.get("businessDetail") or {})
    if facts.get("businessMessage"):
        technical["业务原始消息"] = str(facts.get("businessMessage"))
    for group in groups or []:
        # 逐调用的精确前后变化与结构投影：只在**真的有**时放进来。
        if group.get("changes"):
            technical.setdefault("逐调用前后变化", []).append(
                {
                    "callKey": str(group.get("callKey") or ""),
                    "toolCallId": str(group.get("toolCallId") or ""),
                    "changes": list(group.get("changes") or []),
                }
            )
        if group.get("structure"):
            technical.setdefault("结构投影", []).append(
                {
                    "callKey": str(group.get("callKey") or ""),
                    "structure": dict(group.get("structure") or {}),
                }
            )
    return technical


def build_round_summaries(
    events: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """把事件聚合成「每一轮一行」的人话摘要。

    **唯一数据源**：同一批事件。流式展示与最终摘要都调它 —— 不会出现
    第二份数据，也不会互相覆盖。
    """
    items = list(events or [])
    titles = _title_map(items)
    refs = _ref_names(items)
    repeated = _repeat_submissions(items)
    rounds = []
    for key, round_events in _group_by_round(items):
        summary = _summarize_round(key, round_events, titles, refs, repeated)
        # 带上 traceId：流式推送与最终摘要里的同一轮**键一致** ——
        # 前端据此去重（不会把同一轮显示成两行），也据此隔离请求。
        summary["traceId"] = str(
            (round_events[0].get("traceId") if round_events else "") or ""
        )
        summary["messageId"] = str(
            (round_events[0].get("messageId") if round_events else "") or ""
        )
        # 轮次唯一键：``traceId + replyIndex + iteration``。
        summary["roundKey"] = str(key)
        # 修订号：该轮**最后**一条事件的修订号。前端据此拒绝旧修订覆盖。
        summary["revision"] = max(
            int(event.get("revision") or 0) for event in round_events
        ) if round_events else 0
        rounds.append(summary)
    return rounds


def round_summaries() -> list[dict[str, Any]]:
    """当前请求的逐轮摘要（与最终摘要同一份数据）。"""
    trace = _TRACE.get()
    if trace is None:
        return []
    return build_round_summaries(trace.get("events") or [])


def _round_fingerprint(round_summary: dict[str, Any]) -> str:
    """一轮的指纹：内容变了才推送（去重）。"""
    return hashlib.sha256(
        json.dumps(round_summary, ensure_ascii=False, sort_keys=True, default=str).encode(
            "utf-8"
        )
    ).hexdigest()[:32]


def _publish_rounds(trace: dict[str, Any]) -> None:
    """重算逐轮摘要，把**变化了的轮次**推给订阅者。"""
    try:
        rounds = build_round_summaries(trace.get("events") or [])
    except Exception:  # noqa: BLE001 — 聚合失败不能掀翻主流程
        logger.debug("[edit_trace] 逐轮摘要聚合失败", exc_info=True)
        return
    published = trace.get("publishedRounds")
    if not isinstance(published, dict):
        published = {}
    for round_summary in rounds:
        key = str(round_summary.get("key"))
        fingerprint = _round_fingerprint(round_summary)
        if published.get(key) == fingerprint:
            continue
        published[key] = fingerprint
        _notify(
            {
                "kind": "round",
                "traceId": str(trace.get("traceId") or ""),
                "round": round_summary,
            }
        )
    trace["publishedRounds"] = published



__all__ = [
    "ATTR_CALL_CONTEXT",
    "ATTR_EXPLICIT",
    "ATTR_MISSING",
    "ATTR_SHARED_CURRENT",
    "CHANGES_MISSING",
    "CHECK_FAIL",
    "CHECK_NOT_RUN",
    "CHECK_PASS",
    "CHECK_UNKNOWN",
    "EFFECT_NOT_RUN",
    "EFFECT_UNKNOWN",
    "ERROR_MESSAGES",
    "ERR_ACCEPT_FAILED",
    "ERR_BUDGET_EXHAUSTED",
    "ERR_MODEL_ERROR",
    "ERR_MODEL_TIMEOUT",
    "ERR_MODEL_UNAVAILABLE",
    "ERR_NOT_SUBMITTED",
    "ERR_PARAMS_INVALID",
    "ERR_TOOL_ERROR",
    "ERR_TOOL_LOOKUP_FAILED",
    "ERR_TRIAL_FAILED",
    "ERR_UNKNOWN",
    "EVENT_PUBLIC_TEXT",
    "MISSING_BATCH_ID",
    "MISSING_CALL_CONTEXT",
    "MISSING_CHANGES",
    "MISSING_EFFECT_CHECK",
    "MISSING_ITEM_ID",
    "MISSING_ITERATION",
    "MISSING_LABELS",
    "MISSING_PERSIST_CHECK",
    "MISSING_PLAN_ID",
    "MISSING_REPLY_INDEX",
    "MISSING_STRUCTURE",
    "MISSING_TOOL_CALL_ID",
    "PERSIST_COMMITTED",
    "PERSIST_FAILED",
    "PERSIST_NOT_RUN",
    "PERSIST_UNKNOWN",
    "PHASE_ACCEPTED",
    "PHASE_APPLIED",
    "PHASE_CANDIDATE",
    "PHASE_LABELS",
    "PHASE_ORDER",
    "PHASE_PLAN",
    "PHASE_TRIAL",
    "REPEAT_SUBMISSION_TEXT",
    "SOURCE_BUSINESS_PAYLOAD",
    "SOURCE_DERIVED_FROM_CALLS",
    "SOURCE_DERIVED_FROM_PLAN",
    "SOURCE_FRAMEWORK_STATE",
    "SOURCE_MISSING",
    "SOURCE_MODEL_PUBLIC_TEXT",
    "STAGE_ACCEPTANCE",
    "STAGE_AGENT_REPLY",
    "STAGE_APPLY",
    "STAGE_CANDIDATE",
    "STAGE_ERROR",
    "STAGE_EXPERIENCE",
    "STAGE_SUBMIT",
    "STAGE_TOOL_CALL",
    "STAGE_TRIAL",
    "THINKING_MISSING",
    "BACKEND_MISSING",
    "CallContext",
    "begin_trace_scope",
    "bind_call_context",
    "bind_trace",
    "build_lifecycle",
    "build_round_summaries",
    "call_key",
    "clear_call_context",
    "clear_trace",
    "current_call_context",
    "current_trace",
    "end_trace_scope",
    "ensure_trace",
    "has_accepted_submission",
    "has_submission_attempt",
    "latest_error_category",
    "new_batch_id",
    "record",
    "redact",
    "round_key",
    "round_summaries",
    "set_budget",
    "set_current_iteration",
    "set_ids",
    "set_message_id",
    "set_reply_index",
    "start_trace",
    "step_key",
    "subscribe",
    "trace_summary",
    "unsubscribe",
]
