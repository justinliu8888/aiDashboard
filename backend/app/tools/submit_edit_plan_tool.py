"""submit_edit_plan：提交模板编辑计划（终结工具，节点 5.5）。

终结工具：调用即代表本轮结论已定。
产出由确定性层消费：仍要走一遍 grounded 校验 → TemplateEditEngine.apply，
模型只产候选计划，不直接改草稿。

**能力格式的计划会先整批试做**（2026-11-04 需求）：

* 操作清单里任意一步带 ``op`` → 走**能力目录**的隔离试做：
  在**服务端权威草稿的副本**上整批执行 + 核验；
* 试做失败 → 返回**具体错误**（哪一步、哪个目标、哪条约束、为什么），
  **不写本轮最终提交槽** —— 同一个 Agent 可以在剩余预算里修正再试；
* 试做通过 → 登记**已检查候选**，才写提交槽。

操作清单本身就是「这一件事」的全部事实：再次提交（追加 / 更正 / 撤销）就是
给一份新的完整清单。清单落在服务端会话快照的**修改事项**里，试做失败也保留。
"""
from __future__ import annotations

from typing import Any

from app.schemas.agent import AgentState
from app.tools.base import EDIT_PLAN_STATES, ToolCategory
from app.tools.context import ToolScope, current_tool_context
from app.tools.schemas import EditPlanPayload
from app.tools.submission import record_submission


NAME = "submit_edit_plan"
DESCRIPTION = (
    "提交对当前草稿的编辑计划，这是本轮的最终结论。两种提交方式："
    "（1）**能力格式**（推荐）：operations 里每一项形如 "
    '{"op": "<能力名>", "target": {"regionId" 或 "ref"}, "params": {...}}，'
    "服务端会先在草稿副本上**整批试做**："
    "能力名见 list_simulated_dimensions 之外的能力目录"
    "（ADD_SECTION 新增区块 / DELETE_SECTION 删除区块 / SET_DISPLAY 改展示类型 / "
    "SET_CATEGORY_DATA 设分类统计口径 / SET_PRESENTATION 样式与布局）；"
    "新增区块时在 params.ref 里起一个引用名，后续步骤用 target.ref 指它；"
    "试做失败会返回**具体错误**（哪一步、哪个目标、哪条约束、为什么），"
    "请按反馈改好 operations 后**再次调用本工具**，不要换别的出口；"
    "试做通过才形成待确认候选。"
    "（2）单步格式：target 指明改哪个区块（regionId），operation 指明怎么改。"
    "needsClarification 为 true 时说明还缺参数并给出 clarificationQuestion。"
    "用户表达含糊（比如'改好看点'）时，把候选解释放进 options 让用户选，"
    "不要自己替用户决定。"
)
PARAMETERS = EditPlanPayload.model_json_schema()
CATEGORY = ToolCategory.TERMINAL
IS_READ_ONLY = False
# 交编辑计划的节点：待确认和编辑中。
# 这两个节点的允许转移里都有 USER_EDITING_TEMPLATE，所以结论落得下去。
APPLICABLE_STATES = EDIT_PLAN_STATES
# 修改和局部追问都保持编辑上下文。
CONCLUSION_STATES = (AgentState.USER_EDITING_TEMPLATE.value,)


def _capability_steps(payload: EditPlanPayload) -> list[dict[str, Any]]:
    """从载荷里取出能力格式的操作项（``op`` 非空的那些）。"""
    steps: list[dict[str, Any]] = []
    for step in payload.operations or []:
        op = str(getattr(step, "op", "") or "").strip()
        if not op:
            continue
        target = getattr(step, "target", None)
        steps.append(
            {
                "op": op,
                "target": {
                    "regionId": str(getattr(target, "regionId", "") or ""),
                    "ref": str(getattr(target, "ref", "") or ""),
                }
                if target is not None
                else {},
                "params": dict(getattr(step, "params", {}) or {}),
                "summary": str(getattr(step, "summary", "") or ""),
            }
        )
    return steps


def _fill_step_summaries(
    operations: list[dict[str, Any]], resolved_operations: list[dict[str, Any]]
) -> None:
    """给每一步补一句**人能读懂**的话（就地改）。

    能力格式的步骤没有模型自述的 summary，前端候选卡就退化成显示
    ``intent``（能力格式下是 ``UNKNOWN``）—— 用户看到的是三条 ``UNKNOWN``。
    这里用**试做解析出来的结果**补上：新增了哪一块、删了哪一块、
    展示方式改成什么，都是核验过的事实，不是模型打算做的事。

    对齐按**能力步骤的次序**：清单里可能混着旧格式的步骤（没有 ``op``），
    试做只执行能力步骤，所以不能直接按下标一一对应。
    """
    cursor = 0
    for step in operations:
        if not str(step.get("op") or "").strip():
            continue
        if cursor >= len(resolved_operations):
            break
        message = str((resolved_operations[cursor] or {}).get("message") or "").strip()
        cursor += 1
        if message:
            step["summary"] = message


def run(
    isTemplateEdit: bool = True,
    intent: str = "UNKNOWN",
    target: dict[str, Any] | None = None,
    operation: dict[str, Any] | None = None,
    confidence: float = 0.0,
    needsClarification: bool = False,
    clarificationQuestion: str = "",
    operations: list[dict[str, Any]] | None = None,
    summary: str = "",
    options: list[dict[str, Any]] | None = None,
    reply: str = "",
    originalRequest: str = "",
) -> dict[str, Any]:
    from pydantic import ValidationError

    raw_payload = {
        "isTemplateEdit": isTemplateEdit,
        "intent": intent,
        "target": target or {},
        "operation": operation or {},
        "confidence": confidence,
        "needsClarification": needsClarification,
        "clarificationQuestion": clarificationQuestion,
        "operations": operations or [],
        "summary": summary,
        "options": options or [],
        "reply": reply,
        "originalRequest": originalRequest,
    }
    try:
        validated = EditPlanPayload.model_validate(raw_payload)
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}"
            for err in exc.errors()
        )
        return {"accepted": False, "error": f"载荷不合法：{details}"}
    except Exception as exc:  # noqa: BLE001
        return {"accepted": False, "error": f"载荷不合法：{exc}"}

    capability_steps = _capability_steps(validated)
    if not capability_steps:
        # 旧格式：单步 / 既有组合计划，行为不变。
        record_submission(NAME, validated.model_dump(mode="json"))
        return {"accepted": True, "tool": NAME}

    # ---- 能力格式：先整批试做 ----
    from app.services.edit_item_service import (
        load_authoritative_definition,
        load_edit_item,
        load_preview_context,
        new_item,
        save_edit_item,
    )
    from app.services.edit_trial_service import run_trial, summarize_facts

    scope = None
    ctx = current_tool_context()
    if ctx is not None:
        scope = ToolScope(
            project_id=ctx.project_id,
            subject_ref=ctx.subject_ref,
            subject_id=ctx.subject_id,
        )
        definition, source = load_authoritative_definition(
            fallback=ctx.definition
        )
    else:
        definition, source = load_authoritative_definition()
    if definition is None:
        return {
            "accepted": False,
            "error": "服务端没有读到当前草稿，无法试做；草稿没有变化。",
        }

    item = load_edit_item()
    if item is None:
        item = new_item(
            original_request=originalRequest or "",
            base_revision=int((definition.metadata or {}).get("revision") or 0),
        )
    elif originalRequest and not item.get("originalRequest"):
        item["originalRequest"] = originalRequest
    # 这一件事的操作清单：**每次提交都以本次为准**（追加 / 更正 / 撤销
    # 都是「给一份新的完整清单」）。
    item["operations"] = capability_steps

    trial = run_trial(
        definition=definition,
        operations=capability_steps,
        scope=scope,
        preview_context=load_preview_context(definition),
    )
    if not trial.ok:
        # 试做失败：**不写提交槽**，错误退回同一个 ReAct 循环。
        # 清单与上下文**保留**（下一轮补充或纠正接着用）。
        item["status"] = "drafting"
        item["lastErrors"] = trial.errors
        save_edit_item(item)
        return {
            "accepted": False,
            "error": trial.error_message,
            "errors": trial.errors,
            "hint": (
                "按上面的具体错误改正 operations，然后再次调用 submit_edit_plan。"
                "草稿与正式数据都没有变化。"
            ),
        }

    facts = trial.facts
    facts["summary"] = summarize_facts(facts)
    # 这一件事的**主目标**在整件事里保持稳定：纠正 / 补充之后仍然指向
    # 同一处（用户最初点名的那块），不因为改了操作清单就漂走。
    existing_ids = {
        str(region.get("id") or "") for region in definition.regions or []
    }
    primary = str(facts.get("primaryTargetRegionId") or "")
    item_primary = str((item.get("facts") or {}).get("primaryTargetRegionId") or "")
    if primary and primary not in existing_ids:
        primary = ""
    if not primary and item_primary:
        primary = item_primary
    facts["primaryTargetRegionId"] = primary
    item["status"] = "checked"
    item["resolvedOperations"] = trial.resolved_operations
    item["refs"] = trial.refs
    item["facts"] = facts
    save_edit_item(item)

    payload = validated.model_dump(mode="json")
    payload["editFacts"] = facts
    payload["summary"] = facts["summary"]
    payload["confidence"] = max(float(validated.confidence or 0.0), 0.9)
    payload["needsClarification"] = False
    if primary:
        payload["target"] = {**(payload.get("target") or {}), "regionId": primary}
    _fill_step_summaries(payload.get("operations") or [], trial.resolved_operations)
    record_submission(NAME, payload)
    return {
        "accepted": True,
        "tool": NAME,
        "checked": True,
        "summary": facts["summary"],
        "resolvedOperations": [
            {
                "op": resolved["op"],
                "regionId": resolved["regionId"],
                "title": resolved["title"],
                "chartKind": resolved["chartKind"],
            }
            for resolved in trial.resolved_operations
        ],
    }
