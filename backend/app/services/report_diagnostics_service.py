"""模型诊断：每轮请求标识、上游元信息与失败诊断记录。

这一层是**旁路**：它只观察，不参与业务判断，也不改变模型行为。

分工（契约表 §6）：

- **模型适配层**在这里开一次调用：生成 ``requestId``，取会话/消息/操作标识；
- **解析层**把「解析与校验分类」回写到同一次调用上，并写失败诊断记录；
- **诊断记录**独立于常规日志与会话数据库，落受限本机目录。

三条硬边界（契约表 §6.5）：

- 精确原文模式**默认关闭**；
- 脱敏拿不准就**不写内容**，只留元信息并写明原因（``contentOmittedReason``）；
- 写日志 / 写诊断**失败不得让业务请求失败**。

标识只做关联，不做业务判定；「诊断截取」不得被误说成「模型截断」
（``outputTotalLength`` 记输出总长度，``truncated`` 只表示诊断侧截取）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import get_settings


logger = logging.getLogger("app.report_diagnostics")

#: 解析分类（契约表 §6.3）：取值域固定四值，三类失败**不许合并**。
PARSE_NOT_JSON = "PARSE_NOT_JSON"
PARSE_STRUCTURE_INVALID = "PARSE_STRUCTURE_INVALID"
TERMINAL_RESULT_MISSING = "TERMINAL_RESULT_MISSING"
PARSE_OK = "OK"

#: 上游没给元信息时的占位。**不靠耗时或文本猜截断**。
UNKNOWN = "未知"

#: 诊断正文最多留多少字符（超出是**诊断侧**截取，写 ``truncated``）。
MAX_CONTENT_CHARS = 4000

#: 诊断文件名里带日期，便于按天清理。
_FILE_PREFIX = "diagnostics-"
_FILE_SUFFIX = ".jsonl"


def new_request_id() -> str:
    """每次模型调用一个请求标识（契约表 §6.1）。"""
    return f"req_{uuid.uuid4().hex[:12]}"


def new_diagnostic_id() -> str:
    """一条诊断记录一个标识。"""
    return f"diag_{uuid.uuid4().hex[:12]}"


def prompt_digest(text: str) -> str:
    """提示词内容摘要哈希（前 16 位十六进制，便于在日志里对齐）。"""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------
# 请求级上下文：异步调用里不会被并发请求覆盖
# --------------------------------------------------------------------------


@dataclass
class RequestContext:
    """一次对话轮次的关联信息。

    用 ``ContextVar`` 承载：不需要「上一次响应」那种可被并发覆盖的全局变量
    （契约表 §6.2）。每个请求自己建一个对象，天然隔离。
    """

    conversation_id: str | None = None
    message_id: str | None = None
    operation_id: str | None = None
    stage: str | None = None
    prompt_version: str | None = None
    prompt_digest: str | None = None
    #: 这一轮请求**还在等模型调用**吗。
    #:
    #: ``begin_request_context`` 开的轮次是 True：这一轮里的模型调用继承
    #: 它的会话标识，同一轮里的重试序号递增。
    #: 解析层接手状态之后（这一轮的模型调用已经结束）就置 False ——
    #: 此后再来的模型调用**不属于这一轮**，会话标识留空、序号从 1 起，
    #: 不认错上一轮的会话（契约表 §6.2：序号不跨轮累加）。
    model_call_open: bool = False


_request_context: ContextVar[RequestContext | None] = ContextVar(
    "report_request_context", default=None
)
_current_model_call: ContextVar["ModelCallState | None"] = ContextVar(
    "report_current_model_call", default=None
)


def _ensure_context() -> RequestContext:
    """拿到本轮的请求上下文；没有就建一个空的。

    **不猜**会话标识：没有请求上下文时这里是空的，会话标识由
    ``begin_model_call`` 按「这一轮还在等模型调用吗」决定留空还是继承。
    """
    context = _request_context.get()
    if context is None:
        context = RequestContext()
        _request_context.set(context)
    return context


def begin_request_context(
    *,
    conversation_id: str | None = None,
    message_id: str | None = None,
    stage: str | None = None,
) -> RequestContext:
    """开一轮请求上下文。

    调用方（路由层）在收到请求时调用；同一轮里的模型调用都从这里取关联信息。
    """
    context = RequestContext(
        conversation_id=conversation_id,
        message_id=message_id,
        stage=stage,
        model_call_open=True,
    )
    _request_context.set(context)
    _current_model_call.set(None)
    return context


def current_request_context() -> RequestContext | None:
    return _request_context.get()


def note_operation(operation_id: str | None) -> None:
    """记下本轮认领到的设计操作标识。

    由存储层在**认领操作**时调用（操作 id 在那里生成）；
    模型适配层随后就能把请求标识和操作标识关联起来。
    """
    context = _ensure_context()
    context.operation_id = operation_id


def set_call_context(
    *,
    stage: str | None = None,
    prompt_version: str | None = None,
    prompt_digest: str | None = None,
) -> None:
    """记下本轮的阶段与提示词信息（解析层在调用模型前设置）。"""
    context = _ensure_context()
    if stage is not None:
        context.stage = stage
    if prompt_version is not None:
        context.prompt_version = prompt_version
    if prompt_digest is not None:
        context.prompt_digest = prompt_digest


# --------------------------------------------------------------------------
# 一次模型调用的元信息
# --------------------------------------------------------------------------


@dataclass
class ModelCallState:
    """一次模型调用的元信息。字段名以契约表 §6.1 为准，不另造名字。"""

    request_id: str
    attempt: int = 1
    conversation_id: str | None = None
    message_id: str | None = None
    operation_id: str | None = None
    stage: str | None = None
    model_id: str | None = None
    prompt_version: str | None = None
    prompt_digest: str | None = None
    duration_ms: int | None = None
    output_length: int | None = None
    finish_reason: str = UNKNOWN
    token_usage: Any = UNKNOWN
    response_id: str | None = None
    parse_status: str | None = None
    error_type: str | None = None


def begin_model_call(*, model_id: str | None = None) -> ModelCallState:
    """开始一次模型调用：生成请求标识，带上关联信息。

    **每次调用一个全新的 ``requestId``**；重试是「新标识 + 递增的 attempt」，
    上一轮的标识不被覆盖（契约表 §6.2）。

    attempt 的算法：同一次请求（同一会话 + 同一消息）里接着调，序号加一；
    换了请求就从 1 重新起。序号跟着**这一轮请求**走，不跨轮累加。

    **没有请求上下文时不沿用上一轮的**：这一轮不在等模型调用（请求上下文
    已经过了这一轮），会话标识留空、序号从 1 起，并记一条告警 ——
    免得把这次调用认到上一轮的会话上去。
    """
    context = _ensure_context()
    previous = _current_model_call.get()
    attempt = 1
    conversation_id = context.conversation_id
    message_id = context.message_id
    if not context.model_call_open:
        # 没有请求上下文：不沿用上一轮的会话标识，序号从 1 起。
        attempt = 1
        conversation_id = None
        message_id = None
        _warn_no_request_context()
    elif (
        previous is not None
        and previous.conversation_id == conversation_id
        and previous.message_id == message_id
    ):
        attempt = previous.attempt + 1
    state = ModelCallState(
        request_id=new_request_id(),
        attempt=attempt,
        conversation_id=conversation_id,
        message_id=message_id,
        operation_id=context.operation_id,
        stage=context.stage,
        model_id=model_id,
        prompt_version=context.prompt_version,
        prompt_digest=context.prompt_digest,
    )
    _current_model_call.set(state)
    return state


def _warn_no_request_context() -> None:
    """告警：这一次模型调用没有请求上下文。

    会话标识留空、序号从 1 起 —— 不认错上一轮的会话。
    """
    _safe_warning("本次模型调用没有请求上下文：会话标识留空，序号从 1 起")


def current_model_call() -> ModelCallState | None:
    """当前这一次模型调用的元信息（没有就返回 None）。"""
    return _current_model_call.get()


def ensure_model_call_state(*, model_id: str | None = None) -> ModelCallState:
    """拿到当前调用的元信息；没有就补一个。

    正常路径上由模型适配层创建（契约要求标识在适配层生成）；
    这里只兜住「注入的替身没有适配层」的情况，保证解析失败也有标识可关联。

    解析层接手状态 = **这一轮的模型调用已经结束**：调用链就此关闭。
    之后再来的模型调用就不属于这一轮了（见 ``begin_model_call``）。
    """
    state = _current_model_call.get()
    if state is None:
        state = begin_model_call(model_id=model_id)
    _close_model_call_chain()
    return state


def _close_model_call_chain() -> None:
    """关掉这一轮的模型调用链（解析层接手之后）。"""
    context = _request_context.get()
    if context is not None:
        context.model_call_open = False


def text_or_unknown(value: Any) -> str:
    """上游没给就写「未知」，不留空。"""
    if isinstance(value, str) and value.strip():
        return value
    return UNKNOWN


def token_usage_or_unknown(usage: Any) -> Any:
    """把上游 token 用量归一成 dict；没有就写「未知」。"""
    if usage is None:
        return UNKNOWN
    if isinstance(usage, dict):
        numbers = {
            str(key): int(value)
            for key, value in usage.items()
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        }
        return numbers or UNKNOWN
    numbers: dict[str, int] = {}
    for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = getattr(usage, name, None)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            numbers[name] = int(value)
    return numbers or UNKNOWN


def format_token_usage(usage: Any) -> str:
    """把 token 用量写成日志友好的 ``k=v,k=v``。"""
    if not isinstance(usage, dict) or not usage:
        return UNKNOWN
    return ",".join(f"{key}={value}" for key, value in usage.items())


# --------------------------------------------------------------------------
# 常规日志：只写标识与元信息
# --------------------------------------------------------------------------


def _safe_warning(message: str, *args: Any) -> None:
    """写一条告警日志。

    **日志器自己抛错也不许让业务请求失败**（契约表 §6.5）。
    故障处理路径上已经在 ``except`` 里了，日志再抛错会把原始异常盖掉，
    一路穿出到业务请求 —— 那一轮就直接失败了。
    """
    try:
        logger.warning(message, *args)
    except Exception:  # noqa: BLE001
        pass


def _failure_stack(
    state: ModelCallState, exc: BaseException | None
) -> tuple[Any, Any, Any] | None:
    """失败时的调用栈：**只有代码位置，没有异常消息**。

    返回 ``(异常类型, None, traceback)`` —— 与路由层留栈的口径一致
    （``routers/agent.py``）：异常对象被换掉，所以**异常消息不会进日志**。

    异常对象从哪儿来（拿不到就**不写栈** —— 宁可少写，也不写错的那一条）：

    1. 调用方显式传进来的 ``exc``；
    2. 当前**正在处理**的异常（``sys.exc_info()``）—— 模型适配层在
       ``except`` 里调用 ``log_model_call``，那里拿到的就是同一个异常。

    第 2 条只在「这次调用确实失败了」（``error_type`` 有值）且
    「正在处理的异常类型与 ``error_type`` 一致」时才用 —— 免得把
    无关异常的栈挂到这次调用上。
    """
    if exc is not None:
        return (type(exc), None, exc.__traceback__)
    if state.error_type is None:
        return None
    exc_type, _value, exc_tb = sys.exc_info()
    if exc_type is None or exc_tb is None:
        return None
    if exc_type.__name__ != state.error_type:
        return None
    return (exc_type, None, exc_tb)


def log_model_call(
    state: ModelCallState, *, exc: BaseException | None = None
) -> None:
    """记一次模型调用的元信息。

    **不写**用户原话、模型返回原文、上游异常原文、凭据与业务数值。
    写日志失败不影响业务请求。

    **失败时留调用栈**：口径与路由层完全一致 —— ``exc_info=(类型, None, 栈)``。
    异常对象被换掉，所以异常消息（可能带用户数据、凭据）不进日志；
    栈里只有代码位置，排查的人能直接看出「炸在哪一行」。

    ``exc`` 允许调用方显式把异常递进来（只用来取栈，消息不会被写出去）；
    不传时按 :func:`_failure_stack` 的规则从当前正在处理的异常里取。
    """
    error_type = state.error_type
    if error_type is None and exc is not None:
        error_type = type(exc).__name__
    try:
        logger.info(
            "step=model_call requestId=%s attempt=%s conversationId=%s messageId=%s "
            "operationId=%s stage=%s modelId=%s promptVersion=%s promptDigest=%s "
            "durationMs=%s outputLength=%s finishReason=%s tokenUsage=%s "
            "responseId=%s errorType=%s",
            state.request_id,
            state.attempt,
            state.conversation_id,
            state.message_id,
            state.operation_id,
            state.stage,
            state.model_id,
            state.prompt_version,
            state.prompt_digest,
            state.duration_ms,
            state.output_length,
            state.finish_reason,
            format_token_usage(state.token_usage),
            state.response_id,
            error_type,
            exc_info=_failure_stack(state, exc),
        )
    except Exception:  # noqa: BLE001
        # 日志本身出问题也不能让业务请求失败。
        pass


def log_model_parse(
    state: ModelCallState,
    *,
    parse_status: str | None = None,
    reason: str | None = None,
    json_offset: int | None = None,
    json_line: int | None = None,
    json_column: int | None = None,
) -> None:
    """记一次解析/校验的结论。

    ``parseStatus`` 是契约里的四值之一；不适用的场合**不写这个字段**，
    而不是编一个第五个取值。
    """
    try:
        logger.info(
            "step=model_parse requestId=%s attempt=%s conversationId=%s operationId=%s "
            "stage=%s modelId=%s parseStatus=%s reason=%s outputLength=%s "
            "jsonOffset=%s jsonLine=%s jsonColumn=%s",
            state.request_id,
            state.attempt,
            state.conversation_id,
            state.operation_id,
            state.stage,
            state.model_id,
            parse_status,
            reason,
            state.output_length,
            json_offset,
            json_line,
            json_column,
        )
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------
# 脱敏：保留「形状」，不留内容
# --------------------------------------------------------------------------

#: 凭据样式：即使在精确原文模式下也要抹掉（凭据永远不落盘）。
_CREDENTIAL_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{4,}"), "[凭据]"),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}"), "[凭据]"),
    (re.compile(r"\bAKIA[0-9A-Z]{12,}"), "[凭据]"),
    (
        re.compile(r"\beyJ[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]{4,}"),
        "[凭据]",
    ),
    (
        re.compile(
            r"(?i)\b(api[_-]?key|access[_-]?key|secret[_-]?key|token|password|passwd|pwd)\b"
            r"\s*[:=]\s*\"?[^\s\"',;]{4,}"
        ),
        r"\1=[凭据]",
    ),
    (re.compile(r"\b[0-9a-f]{32,}\b", re.IGNORECASE), "[凭据]"),
)

#: 只保留「像字段名」的键；其余（可能是用户原话）一律掩掉。
_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_\-]{0,31}$")

#: **键名本身**长得像凭据的形状：AWS access key id、``sk-`` 前缀、JWT、
#: 长十六进制串等。这类键原样保留就等于把凭据写进了诊断正文。
#:
#: 复用正文那一套凭据形状 —— 正文里认得出来的凭据，键名里也认得出来。
_CREDENTIAL_KEY_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    pattern for pattern, _ in _CREDENTIAL_PATTERNS
)


def _looks_like_credential(text: str) -> bool:
    """这个键名本身是不是一个凭据（而不是一个字段名）。"""
    return any(pattern.search(text) for pattern in _CREDENTIAL_KEY_PATTERNS)


def _scrub_credentials(text: str) -> str:
    """抹掉凭据样式的片段。"""
    result = text
    for pattern, replacement in _CREDENTIAL_PATTERNS:
        result = pattern.sub(replacement, result)
    return result


def _has_control_chars(text: str) -> bool:
    """正文里有控制字符（非换行/制表）时，无法保证脱敏完整。"""
    return any(
        ord(char) < 0x20 and char not in "\n\r\t" for char in text
    )


def mask_text_shape(text: str) -> str:
    """把自由文本变成「字符类别图」。

    字母→``a``、数字→``#``、汉字→``字``，标点与换行保留：
    能看出「像不像 JSON」「断在哪」，但一个字的内容都不留。
    """
    out: list[str] = []
    for char in text:
        if char == "\n":
            out.append("\n")
        elif char.isspace():
            out.append(" ")
        elif char.isdigit():
            out.append("#")
        elif "一" <= char <= "鿿" or "㐀" <= char <= "䶿":
            out.append("字")
        elif char.isalpha():
            out.append("a")
        else:
            out.append(char)
    return "".join(out)


def _safe_key(key: Any) -> str:
    """把键名变成可以写进诊断正文的形式。

    - 像字段名、又不像凭据 → **原样保留**（``energyKwh``、``deviceName``
      仍然可读，便于排查时对上模型给的字段）；
    - 像凭据（AWS access key id、``sk-`` 前缀、JWT、长十六进制串…）
      → 降级成 ``«key:N»``：键名本身就是凭据，留着就等于把凭据落盘；
    - 不像字段名（可能是用户原话，含中文等）→ 降级成 ``«key:N»``。
    """
    text = str(key)
    if not _KEY_PATTERN.match(text):
        return f"«key:{len(text)}»"
    if _looks_like_credential(text):
        return f"«key:{len(text)}»"
    return text


def _render_value(value: Any, depth: int, max_depth: int) -> str:
    if depth >= max_depth:
        return "«deep»"
    if isinstance(value, dict):
        items: list[str] = []
        for index, (key, item) in enumerate(value.items()):
            if index >= 40:
                items.append("«more»")
                break
            key_text = json.dumps(_safe_key(key), ensure_ascii=False)
            items.append(f"{key_text}: {_render_value(item, depth + 1, max_depth)}")
        return "{" + ", ".join(items) + "}"
    if isinstance(value, list):
        items = [_render_value(item, depth + 1, max_depth) for item in value[:20]]
        if len(value) > 20:
            items.append("«more»")
        return "[" + ", ".join(items) + "]"
    if isinstance(value, bool):
        return "«bool»"
    if value is None:
        return "«null»"
    if isinstance(value, (int, float)):
        return "«num»"
    if isinstance(value, str):
        return f"«str:{len(value)}»"
    return "«other»"


def render_structure(value: Any, *, max_depth: int = 8) -> str:
    """把已解析的 JSON 渲染成「结构 + 类型 + 长度」，一个取值都不留。"""
    return _render_value(value, 0, max_depth)


def _exact_content_allowed(settings: Any, state: ModelCallState) -> bool:
    """精确原文模式：默认关闭；开启还要「未过期 + 在范围内」。"""
    if not getattr(settings, "diagnostics_exact_content", False):
        return False
    until = str(getattr(settings, "diagnostics_exact_content_until", "") or "").strip()
    if not until:
        return False
    try:
        deadline = datetime.fromisoformat(until)
    except ValueError:
        return False
    if deadline.tzinfo is None:
        deadline = deadline.astimezone()
    if datetime.now(timezone.utc) > deadline.astimezone(timezone.utc):
        return False
    scope = str(getattr(settings, "diagnostics_exact_content_scope", "") or "").strip()
    if not scope or scope == "*":
        return True
    allowed = {item.strip() for item in scope.split(",") if item.strip()}
    return bool(state.conversation_id) and state.conversation_id in allowed


def _prepare_content(
    settings: Any,
    *,
    raw_output: str | None,
    parsed: Any,
    state: ModelCallState,
) -> tuple[str | None, bool, str | None]:
    """准备诊断正文：脱敏后再写；脱敏不保证完整就**不写**。

    返回 ``(正文, 是否已脱敏, 没写正文的原因)``。
    """
    if not isinstance(raw_output, str) or not raw_output.strip():
        return None, True, "模型没有返回可记录的内容"
    if _has_control_chars(raw_output):
        return None, True, "返回内容含控制字符，无法保证脱敏完整，只留元信息"
    if _exact_content_allowed(settings, state):
        # 精确原文模式：仍然抹掉凭据 —— 凭据永远不落盘。
        return _scrub_credentials(raw_output), False, None
    if parsed is not None:
        return render_structure(parsed), True, None
    return mask_text_shape(raw_output), True, None


# --------------------------------------------------------------------------
# 诊断记录：独立文件，不落会话库
# --------------------------------------------------------------------------


def write_diagnostic_record(
    *,
    state: ModelCallState,
    reason: str,
    parse_status: str,
    raw_output: str | None = None,
    parsed: Any = None,
    json_offset: int | None = None,
    json_line: int | None = None,
    json_column: int | None = None,
) -> str | None:
    """写一条失败诊断记录，返回 ``diagnosticId``。

    诊断记录**独立于常规日志与会话数据库**：落受限本机目录，
    不在前端公开目录、不在 Git 仓库、不进 API 响应与用户可见错误消息。
    写失败只记一笔日志，不影响业务请求。
    """
    try:
        settings = get_settings()
        if not getattr(settings, "diagnostics_enabled", True):
            return None
        diagnostic_id = new_diagnostic_id()
        record: dict[str, Any] = {
            "diagnosticId": diagnostic_id,
            "requestId": state.request_id,
            "createdAt": datetime.now(timezone.utc).astimezone().isoformat(
                timespec="seconds"
            ),
            "reason": reason,
            "parseStatus": parse_status,
            "conversationId": state.conversation_id,
            "messageId": state.message_id,
            "operationId": state.operation_id,
            "stage": state.stage,
            "modelId": state.model_id,
            "promptVersion": state.prompt_version,
            "promptDigest": state.prompt_digest,
            "attempt": state.attempt,
            "durationMs": state.duration_ms,
            "outputLength": state.output_length,
            "finishReason": state.finish_reason,
            "tokenUsage": state.token_usage,
            "responseId": state.response_id,
            # 输出总长度：与下面的 truncated 配对看。
            "outputTotalLength": len(raw_output) if isinstance(raw_output, str) else 0,
            # truncated 只表示**诊断侧**截取，不是模型截断。
            "truncated": False,
            "redacted": True,
            "content": None,
            "contentOmittedReason": None,
            "jsonOffset": json_offset,
            "jsonLine": json_line,
            "jsonColumn": json_column,
        }
        content, redacted, omitted = _prepare_content(
            settings, raw_output=raw_output, parsed=parsed, state=state
        )
        record["content"] = content
        record["redacted"] = redacted
        record["contentOmittedReason"] = omitted
        if isinstance(content, str) and len(content) > MAX_CONTENT_CHARS:
            record["content"] = content[:MAX_CONTENT_CHARS]
            record["truncated"] = True
        line = json.dumps(record, ensure_ascii=False)
        max_bytes = int(getattr(settings, "diagnostics_max_record_bytes", 64 * 1024))
        if len(line.encode("utf-8")) > max_bytes:
            # 单条超限：正文去掉，只留元信息（这属于诊断侧截取）。
            record["content"] = None
            record["contentOmittedReason"] = "诊断记录超过单条上限，已省略正文"
            record["truncated"] = True
            line = json.dumps(record, ensure_ascii=False)
        _append_line(
            Path(settings.diagnostics_dir),
            line,
            retention_days=int(getattr(settings, "diagnostics_retention_days", 7)),
            max_total_bytes=int(
                getattr(settings, "diagnostics_max_total_bytes", 10 * 1024 * 1024)
            ),
        )
        return diagnostic_id
    except Exception as exc:  # noqa: BLE001
        # 这里已经在处理异常了：日志再抛错会把原始异常盖掉并穿出到
        # 业务请求（契约表 §6.5：写日志 / 写诊断失败不得让业务请求失败）。
        _safe_warning("写诊断记录失败：%s", type(exc).__name__)
        return None


def _append_line(
    directory: Path,
    line: str,
    *,
    retention_days: int,
    max_total_bytes: int,
) -> None:
    """追加一行诊断记录，并做保留期限与总容量清理。"""
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    name = f"{_FILE_PREFIX}{datetime.now().strftime('%Y%m%d')}{_FILE_SUFFIX}"
    path = directory / name
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    os.chmod(path, 0o600)
    _cleanup(
        directory,
        retention_days=retention_days,
        max_total_bytes=max_total_bytes,
        keep=path,
    )


def _cleanup(
    directory: Path,
    *,
    retention_days: int,
    max_total_bytes: int,
    keep: Path | None = None,
) -> None:
    """到期自动清理：先按保留期限删旧文件，再按总容量删最旧的。

    ``keep`` 是刚写下的那一份：容量不够时也不删它 ——
    宁可暂时超容量，也不能把这一轮的诊断弄丢。
    """
    try:
        files = sorted(
            directory.glob(f"{_FILE_PREFIX}*{_FILE_SUFFIX}"),
            key=lambda item: item.stat().st_mtime,
        )
    except OSError:
        return
    now = time.time()
    if retention_days > 0:
        deadline = now - retention_days * 86400
        for path in files:
            if keep is not None and path == keep:
                continue
            try:
                if path.stat().st_mtime < deadline:
                    path.unlink()
            except OSError:
                continue
    files = [path for path in files if path.exists()]
    total = 0
    for path in files:
        try:
            total += path.stat().st_size
        except OSError:
            continue
    if max_total_bytes <= 0:
        return
    for path in files:
        if total <= max_total_bytes:
            break
        if keep is not None and path == keep:
            continue
        try:
            size = path.stat().st_size
            path.unlink()
            total -= size
        except OSError:
            continue
