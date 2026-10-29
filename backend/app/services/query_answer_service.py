"""只读查询的确定性回答器。

两个用途：

    1. Fake Agent 的查询轮：没有真实模型时，用同一套事实工具 + 同一套事实
       口径拼出一条可核对的回答（测试替身，不替代产品验收）。
    2. 查询失败时的兜底口径：把「读不到」如实说成读不到。

**不负责**模型式的自然表达。真实模型走 ReAct + 事实工具自己写答案；
这里的输出是「按事实拼出来的确定答案」，只用于确定性测试和失败兜底。

C / D 的事实工具如果返回一个 ``summary`` 字段（面向用户的一句话），
会被并进回答里，这样进度与能力两类事实也能出现在同一条回答中。
"""
from __future__ import annotations

import json
import re
from typing import Any

from app.schemas.fact import (
    LIVE_PROGRESS_CLAIM_MARKERS,
    RECORDED_NOT_LIVE_MARKERS,
    RECORDED_STAGE_DISCLOSURE,
    RECORDED_STATE_MARKERS,
    FactStatus,
    SessionFacts,
)


#: 用户在问别的对话 / 历史会话的草稿。首版不检索历史，只做引导。
_HISTORY_HINTS: tuple[str, ...] = (
    "之前", "以前", "历史", "别的对话", "其他对话", "其它对话",
    "别的会话", "其他会话", "其它会话", "别的报表", "其他报表",
)

#: 事实工具返回体里用于并进回答的字段。
_SUMMARY_KEY = "summary"


def is_history_question(message: str) -> bool:
    """这句是不是在问别的对话 / 历史里的草稿。"""
    text = (message or "").strip()
    return any(hint in text for hint in _HISTORY_HINTS)


#: 判定「冒充实时任务监控」时，标志词前面出现这些否定词就不算。
#:
#: 「不是实时进度」「无法提供实时进度」「没有正在跑」都是在**守**红线，
#: 不能当成违反红线。
_NEGATORS: tuple[str, ...] = (
    "无法", "不能", "不会", "没有", "尚未", "无从", "不是", "不给",
)

#: 百分比只有**跟着进度语义**出现才是编进度。
#:
#: 「毛利率是 12.5%」是指标口径，不是进度；「进度到 60%」才是。
_LIVE_PROGRESS_PERCENT_PATTERN = re.compile(
    r"(进度|完成|跑到|还剩|剩余|做到)[^。；;]{0,12}?\d+(?:\.\d+)?\s*%"
)


def recorded_stage_disclosed(reply: str) -> bool:
    """用户可见答案里是否说清了「已记录、不是实时」这条边界。

    两半都要说才算数：

        - 「这是已记录的状态」（工单 #03：回答明确是已记录状态）；
        - 「不是实时任务监控」（同一条：不冒充实时任务监控）。

    只说一半不行：日常说法（「你的需求已记录」）不含「不是实时」，
    而「没有实时任务状态来源」也不等于说清了这是已记录的状态。
    """
    text = reply or ""
    stated = any(marker in text for marker in RECORDED_STATE_MARKERS)
    not_live = any(marker in text for marker in RECORDED_NOT_LIVE_MARKERS)
    return stated and not_live


def claims_live_progress(reply: str) -> bool:
    """答案里是不是在冒充实时任务监控（编百分比 / 说正在跑）。

    工单 #03 的红线：首版没有实时任务状态来源，也不能编百分比或剩余时间。
    """
    text = reply or ""
    if _LIVE_PROGRESS_PERCENT_PATTERN.search(text):
        return True
    for marker in LIVE_PROGRESS_CLAIM_MARKERS:
        start = 0
        while True:
            index = text.find(marker, start)
            if index < 0:
                break
            prefix = text[max(0, index - 6):index]
            if not any(negator in prefix for negator in _NEGATORS):
                return True
            start = index + len(marker)
    return False


def recorded_stage_fallback(facts: SessionFacts) -> str:
    """编造了实时进度时，用服务端事实替换整句回答。

    服务端不能靠「再补一句否认」来抵消一个编出来的进度 ——
    两句话会互相打架，用户读到的是假数字。
    这里直接用服务端记录换掉它：说明下一步，并说清这条边界。

    **读不到记录时不能替用户断言「已记录」**：会话不存在、记录读不到、
    范围不符，都要如实说「无法确认」。
    """
    if facts.status is not FactStatus.FOUND:
        reason = facts.reason or "暂时无法确认当前对话的情况"
        return (
            f"这次查询没有完成，{reason}。"
            "草稿和待确认操作都没有变化，可以稍后再问一次。"
        )
    steps = list(facts.draft.pendingSteps) if facts.draft else []
    head = (
        f"当前对话已记录的下一步：{'、'.join(steps)}。"
        if steps
        else "当前对话已记录了报表的业务阶段。"
    )
    return f"{head}{RECORDED_STAGE_DISCLOSURE}"


def compose_query_reply(
    *,
    message: str,
    results: list[tuple[str, dict[str, Any]]],
) -> tuple[str, list[str], list[str]]:
    """按事实拼一条确定性的只读回答。

    返回 ``(reply, verified_facts, limitations)``。
    """
    by_name = {name: payload for name, payload in results}
    facts: list[str] = []
    limitations: list[str] = []
    lines: list[str] = []

    draft = by_name.get("get_current_draft") or {}
    status = str(draft.get("status") or "")
    if draft.get("error") and not status:
        # 事实工具本身报错：这是「读不到」，不能说成「没有」。
        status = "UNAVAILABLE"
        draft = {**draft, "reason": str(draft.get("error"))}

    if status == "UNAVAILABLE":
        reason = str(draft.get("reason") or "读取当前对话的记录失败")
        limitations.append(reason)
        return (
            "暂时无法确认当前对话的报表草稿："
            f"{reason}。草稿和待确认操作都没有变化，可以稍后再问一次。",
            facts,
            limitations,
        )
    if status == "FORBIDDEN":
        reason = str(draft.get("reason") or "当前请求范围与该会话记录不一致")
        limitations.append(reason)
        return (
            f"无法确认当前对话的报表草稿：{reason}。",
            facts,
            limitations,
        )

    if is_history_question(message):
        lines.append(
            "这次只核对了当前对话，没有去翻其他会话的记录。"
            "之前其它对话里的报表草稿在左侧会话列表里，切换过去就能看到。"
        )
        limitations.append("查询范围限定为当前对话，未检索历史会话")

    if draft.get("exists"):
        name = str(draft.get("reportName") or "").strip()
        revision = int(draft.get("revision") or 0)
        region_count = int(draft.get("regionCount") or 0)
        label = f"「{name}」" if name else ""
        facts.append(f"当前对话存在报表草稿{label}，版本 {revision}")
        lines.append(
            f"当前对话有一份报表草稿{label}（版本 {revision}，{region_count} 个区块）。"
        )
        steps = [str(step) for step in (draft.get("pendingSteps") or []) if step]
        if steps:
            facts.extend(f"待完成步骤：{step}" for step in steps)
            lines.append("接下来要做的是：" + "、".join(steps) + "。")
        saved = draft.get("savedDefinition")
        if isinstance(saved, dict) and saved.get("available"):
            facts.append(f"已有保存的定义（保存版本 {saved.get('revision')}）")
        preview = draft.get("preview")
        if isinstance(preview, dict) and preview.get("reportRunId"):
            facts.append(f"已有报表预览（运行标识 {preview.get('reportRunId')}）")
    else:
        facts.append("当前对话没有报表草稿")
        lines.append("当前对话没有报表草稿。")

    pending_plan = by_name.get("get_current_session_status", {}).get("pendingEditPlan")
    if isinstance(pending_plan, dict):
        facts.append("当前对话有一份待确认的修改计划")
        lines.append("另外还有一份待确认的修改计划没有处理。")

    for name, payload in results:
        if name in {"get_current_draft", "get_current_session_status"}:
            continue
        if not isinstance(payload, dict):
            continue
        summary = payload.get(_SUMMARY_KEY)
        if isinstance(summary, str) and summary.strip():
            facts.append(summary.strip())
            lines.append(summary.strip())
        else:
            facts.append(f"{name}: {json.dumps(payload, ensure_ascii=False)[:300]}")

    reply = "".join(line if line.endswith(("。", "！", "？")) else line + "。" for line in lines)
    return reply.strip(), facts, limitations
