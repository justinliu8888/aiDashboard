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
from typing import Any


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
