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
#: 说明的**静态**部分（流程与两种提交方式）。
_DESCRIPTION_BASE = (
    "提交对当前草稿的编辑计划，这是本轮的最终结论。两种提交方式："
    "（1）**能力格式**（推荐）：operations 里每一项形如 "
    '{"op": "<能力名>", "target": {"regionId" 或 "ref"}, "params": {...}},'
    "服务端会先在草稿副本上**整批试做**；"
    "新增区块时在 params.ref 里起一个引用名，后续步骤用 target.ref 指它；"
    "试做失败会返回**具体错误**（哪一步、哪个目标、哪条约束、为什么，"
    "外加字段路径、收到的类型、要求的类型、该填在哪、最小结构示例），"
    "请按反馈改好 operations 后**再次调用本工具**，不要换别的出口；"
    "试做通过才形成待确认候选。"
    "（2）单步格式：target 指明改哪个区块（regionId），operation 指明怎么改。"
    "**能力名、参数、支持范围、分类维度与源码位置见本轮下发的能力总目录**"
    "（编辑说明末尾），不要凭记忆猜能力名或参数。"
    "needsClarification 为 true 时说明还缺参数并给出 clarificationQuestion。"
    "用户表达含糊（比如'改好看点'）时，把候选解释放进 options 让用户选，"
    "不要自己替用户决定。"
)


def _contract_description() -> str:
    """包装能力的那一段说明 —— **从共享契约生成**。

    包装能力（它的载荷里装的是别的操作）不是这里手写的名单：
    契约认出来的每一个包装能力都会在这里说明「子操作填在哪」，
    能力总目录里也是同一份。
    """
    try:
        from app.services.edit_capabilities.contract import build_contract

        contract = build_contract()
    except Exception:  # noqa: BLE001 — 说明文本不该让工具不可用
        return ""
    lines: list[str] = []
    for package in contract.packages:
        members = "、".join(item.op for item in package.members)
        lines.append(
            f"**包装能力 {package.op}**：它的 params.{package.payload_field} 是**非空列表**，"
            f'{{"op": "<操作名>", "params": {{...}}}}；'
            f"允许的 op：{members}。"
            "这些 op **不能**直接作为 operations 的顶层项 —— "
            f"顶层项是 {package.op} 自己，"
            f"它的子操作写在 params.{package.payload_field} 里。"
        )
    if not lines:
        return ""
    return "包装能力（子操作要放在它的 params 里，不要放在顶层）：" + "".join(lines)


def __getattr__(name: str) -> str:
    """``DESCRIPTION`` 按需生成（能力名 / 操作名不手写）。"""
    if name == "DESCRIPTION":
        return f"{_DESCRIPTION_BASE}{_contract_description()}"
    raise AttributeError(name)


PARAMETERS = EditPlanPayload.model_json_schema()
#: ``operations`` 这一项的说明：**顶层能力**与**包装能力内部的操作**不是一回事。
_PARAM_OPERATIONS_DESCRIPTION = (
    "能力格式的操作清单：每一项形如 "
    '{"op": "<能力名>", "target": {"regionId" 或 "ref"}, "params": {...}}。'
    "**包装能力**（子操作要放在它 params 里的列表字段中）不能把子操作直接写成本列表的顶层项 —— "
    "见本轮能力总目录里每项能力的「提交位置」。"
)
if isinstance(PARAMETERS.get("properties"), dict):
    operations_spec = PARAMETERS["properties"].get("operations")
    if isinstance(operations_spec, dict):
        operations_spec["description"] = _PARAM_OPERATIONS_DESCRIPTION
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


def _trace_submit(
    *,
    stage: str,
    name: str,
    params: dict,
    result: dict,
    status: str = "ok",
    error_category: str = "",
    phase: str = "",
    batch_id: str = "",
    checks: dict[str, str] | None = None,
    candidate_registered: bool | None = None,
    applied: bool | None = None,
    changes: list[dict[str, Any]] | None = None,
    structure: dict[str, Any] | None = None,
    truncation: dict[str, Any] | None = None,
    step_index: int | None = None,
    business_reason: str = "",
    extra: dict[str, Any] | None = None,
) -> None:
    """写一条追踪事件（**失败不影响主流程**）。

    既有参数形态**不变**；新增的都是**关键字**参数（透传冻结契约里的
    一等字段：批次 / 三类检查 / 候选登记 / 前后变化 / 结构投影）。
    """
    try:
        from app.services import edit_trace_service

        edit_trace_service.record(
            stage,
            name=name,
            params=params,
            result=result,
            status=status,
            errorCategory=error_category,
            phase=phase,
            batchId=batch_id,
            checks=dict(checks or {}),
            candidateRegistered=candidate_registered,
            applied=applied,
            changes=list(changes or []),
            structure=dict(structure or {}),
            truncation=dict(truncation or {}),
            stepIndex=step_index,
            businessReason=business_reason,
            # 裁剪过就**标出来**（不把「截断了」写成「完整内容」）。
            truncated=bool((truncation or {}).get("changes")),
            extra=dict(extra or {}),
        )
    except Exception:  # noqa: BLE001
        pass


def _bind_real_ids(item: dict[str, Any]) -> tuple[str, str]:
    """把**真实存在**的修改事项 / 候选计划标识绑到本轮追踪上。

    * ``itemId``：来自 ``new_item()`` / ``load_edit_item()`` —— 真实存在；
    * ``planId``：事项里**真有**候选计划标识时才绑（没有就空着 ——
      **不提前造候选 id**，也不从别的请求借）。

    返回 ``(item_id, plan_id)``。
    """
    item_id = str((item or {}).get("itemId") or "")
    plan_id = str((item or {}).get("candidatePlanId") or "")
    try:
        from app.services import edit_trace_service

        edit_trace_service.set_ids(item_id=item_id, plan_id=plan_id)
    except Exception:  # noqa: BLE001 — 绑定失败不该掀翻提交
        pass
    return item_id, plan_id


def _checks(business: str) -> dict[str, str]:
    """三类检查状态（分开记，不混成一句「已生效」）。

    * 业务：试做通过 = ``pass``，失败 = ``fail``，没跑 = ``not_run``；
    * 持久化：事务在**请求层**，工具层**没跑** → ``not_run``；
    * 效果核对：本轮**没有**效果核对行为 → ``not_run``（不推断）。
    """
    try:
        from app.services import edit_trace_service

        return {
            "business": business,
            "persistence": edit_trace_service.PERSIST_NOT_RUN,
            "effect": edit_trace_service.EFFECT_NOT_RUN,
        }
    except Exception:  # noqa: BLE001
        return {"business": business, "persistence": "not_run", "effect": "not_run"}


#: 这些能力是「在**已有**区块上改」，目标不能空。
#: ``ADD_SECTION`` 是**新建**区块 —— 目标本来就空，不参与补目标。
_TARGET_REQUIRED_OPS = frozenset({
    "SET_METRIC_ITEM",
    "SET_METRIC_GROUP",
    "SET_TEXT_CONTENT",
    "SET_TABLE_COLUMNS",
    "SET_TABLE_DISPLAY",
    "SET_TABLE_SUMMARIES",
    "SET_CHART_KIND",
    "SET_CHART_ENCODINGS",
    "SET_CHART_AXES",
    "SET_CHART_LEGEND",
    "SET_CHART_TOOLTIP",
    "SET_CHART_REFERENCE_LINES",
    "SET_DISPLAY",
    "SET_CATEGORY_DATA",
    "SET_PRESENTATION",
    "SET_DATA_BINDING",
    "DELETE_SECTION",
    "DELETE_METRIC",
    "RENAME_SECTION",
    "SET_FORMAT",
    "SET_PAGE",
    "MOVE_REGION",
    "DELETE_REGION",
    "HIDE_REGION",
    "SHOW_REGION",
    "SET_PLACEMENT",
    "SET_STYLE",
    "SET_METADATA",
    "UNGROUP",
    "COPY_REGION",
    "ADD_METRIC",
})


def _fill_missing_targets(
    steps: list[dict[str, Any]],
    *,
    definition: Any,
    user_message: str,
) -> list[dict[str, Any]]:
    """目标没写对时，按**用户原话里点名的区块**补上（唯一命中才补）。

    模型有时会把目标写成 ``{"target": ""}`` —— 目标键名不对、值也空。
    这时候试做会报「找不到要改的卡片」，用户只能重说一遍。

    这里用**用户自己的话**补：原话里出现的**区块名**（草稿里真实存在的
    那些）**唯一命中**时才补 —— 命中多个说明用户指的是好几块，不猜。

    这不是「按标题猜目标」：区块名是用户原话里**点名**的，且唯一命中。
    """
    text = str(user_message or "").strip()
    if not text or definition is None:
        return steps
    regions = list(getattr(definition, "regions", None) or [])
    if not regions:
        return steps
    names = {
        str(region.get("name") or "").strip(): str(region.get("id") or "").strip()
        for region in regions
        if isinstance(region, dict)
    }
    names = {name: rid for name, rid in names.items() if name and rid}
    if not names:
        return steps
    matched = [
        (name, rid) for name, rid in names.items() if name and name in text
    ]
    if len(matched) != 1:
        return steps
    region_id = matched[0][1]
    out: list[dict[str, Any]] = []
    for step in steps:
        op = str(step.get("op") or "").strip()
        target = dict(step.get("target") or {})
        if (
            op in _TARGET_REQUIRED_OPS
            and not str(target.get("regionId") or "").strip()
            and not str(target.get("ref") or "").strip()
        ):
            target["regionId"] = region_id
            step = {**step, "target": target}
        out.append(step)
    return out


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
    # 下面两个字段**在工具的 JSON Schema 里声明了**（``EditPlanPayload``
    # 继承自 ``EditPlan``）—— 模型照着填是合规的。
    #
    # 但它们都是**服务端自己算**的东西：生效组合快照由服务端按当前草稿
    # 取（规则路径就是 ``_current_combination(definition)``），证据由服务端
    # 落。按模型说的用会让「生效组合变了」误报（旧组合 / 臆造组合）。
    #
    # 现场实证：模型带上 ``baseCombination`` 调用本工具 → ``run()`` 直接抛
    # ``TypeError: run() got an unexpected keyword argument 'baseCombination'``，
    # **整轮修改全废**（用户只看到一句「没能把修改提交成待确认候选」，
    # 草稿一个字没改）。所以这里**收下但不用**。
    baseCombination: dict[str, Any] | None = None,
    editFacts: list[dict[str, Any]] | None = None,
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
        # 参数不合法：**结构就不合法**，没到试做那一步。
        _trace_submit(
            stage="submit",
            name=NAME,
            params={"format": "capability", "intent": str(intent or "")},
            result={"accepted": False, "reason": "params_invalid"},
            status="error",
            error_category="params_invalid",
            phase="plan",
            checks=_checks("not_run"),
        )
        return {"accepted": False, "error": f"载荷不合法：{details}"}
    except Exception as exc:  # noqa: BLE001
        _trace_submit(
            stage="submit",
            name=NAME,
            params={"format": "capability", "intent": str(intent or "")},
            result={"accepted": False, "reason": "params_invalid"},
            status="error",
            error_category="params_invalid",
            phase="plan",
            checks=_checks("not_run"),
        )
        return {"accepted": False, "error": f"载荷不合法：{exc}"}

    capability_steps = _capability_steps(validated)
    if not capability_steps:
        # 旧格式：单步 / 既有组合计划，行为不变。
        record_submission(NAME, validated.model_dump(mode="json"))
        _trace_submit(
            stage="submit",
            name=NAME,
            params={"format": "legacy", "intent": str(validated.intent or "")},
            result={"accepted": True, "trial": False},
            phase="plan",
        )
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
        _trace_submit(
            stage="submit",
            name=NAME,
            params={"format": "capability", "steps": len(capability_steps)},
            result={"accepted": False},
            status="error",
            error_category="tool_error",
            phase="plan",
            checks=_checks("not_run"),
        )
        return {
            "accepted": False,
            "error": "服务端没有读到当前草稿，无法试做；草稿没有变化。",
        }

    item = load_edit_item()
    # 原要求优先用模型传的 ``originalRequest``；模型没传时用**用户这一轮
    # 的原话**兜底 —— 服务端在工具上下文里带着它，不能让归档记录丢掉
    # 最关键的那句话。
    user_message = ""
    ctx_now = current_tool_context()
    if ctx_now is not None:
        user_message = str((ctx_now.extra or {}).get("userMessage") or "")
    original_text = originalRequest or user_message
    if item is None:
        item = new_item(
            original_request=original_text,
            base_revision=int((definition.metadata or {}).get("revision") or 0),
        )
    elif original_text and not item.get("originalRequest"):
        # 事项已存在但**原话还空着** → 补上。
        # 只在空值时补：补充同一事项时不得覆盖原要求。
        item["originalRequest"] = original_text
    # 这一件事的操作清单：**每次提交都以本次为准**（追加 / 更正 / 撤销
    # 都是「给一份新的完整清单」）。
    item["operations"] = capability_steps
    # **真实存在的 id 立刻绑定**（``new_item`` / ``load_edit_item`` 给的）：
    # 不提前造候选 id，也不从别的请求借。
    item_id, plan_id = _bind_real_ids(item)

    capability_steps = _fill_missing_targets(
        capability_steps, definition=definition, user_message=original_text
    )

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
        _trace_submit(
            stage="trial",
            name=NAME,
            params={"format": "capability", "steps": len(capability_steps)},
            result={
                "ok": False,
                "errorCount": len(trial.errors or []),
                "firstError": (trial.errors or [{}])[0] if trial.errors else {},
            },
            status="error",
            error_category="trial_failed",
            phase="trial",
            batch_id=trial.batch_id,
            checks=_checks("fail"),
            # 试做没过 → 候选**没有**登记（不是「提交槽写成功了」）。
            candidate_registered=False,
            applied=False,
            changes=list(trial.facts.get("presentationChanges") or []),
            structure=dict(trial.structure or {}),
            truncation=dict(trial.facts.get("truncation") or {}),
            extra={
                "batchOutcome": "failed",
                "copyOnly": True,
                "appliedToDraft": False,
                "itemId": item_id,
                "planId": plan_id,
                "note": "副本上的局部变化，整批未通过、未正式生效",
            },
        )
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
    # 候选**真的登记成功**才算登记成功：``save_edit_item`` 抛了就记 False
    # （工具提交槽写成功**不能**代替持久化成功）。异常照旧往上抛 ——
    # 返回语义不变。
    try:
        save_edit_item(item)
    except Exception as exc:  # noqa: BLE001 — 记事实，异常照旧往上抛
        _trace_submit(
            stage="candidate",
            name=NAME,
            params={"format": "capability"},
            result={
                "registered": False,
                "reason": "candidate_save_failed",
                "errorType": type(exc).__name__,
            },
            status="error",
            error_category="tool_error",
            phase="candidate",
            batch_id=trial.batch_id,
            checks=_checks("pass"),
            candidate_registered=False,
            applied=False,
            business_reason="candidate_save_failed",
            extra={"itemId": item_id, "planId": plan_id},
        )
        raise
    candidate_registered = True

    payload = validated.model_dump(mode="json")
    payload["editFacts"] = facts
    payload["summary"] = facts["summary"]
    payload["confidence"] = max(float(validated.confidence or 0.0), 0.9)
    payload["needsClarification"] = False
    if primary:
        payload["target"] = {**(payload.get("target") or {}), "regionId": primary}
    _fill_step_summaries(payload.get("operations") or [], trial.resolved_operations)
    record_submission(NAME, payload)
    # 提交**结构合法**（过了载荷校验 + 试做校验），这一轮真的写进了提交槽。
    _trace_submit(
        stage="submit",
        name=NAME,
        params={"format": "capability", "steps": len(capability_steps)},
        result={"accepted": True, "checked": True, "trial": True},
        phase="plan",
        batch_id=trial.batch_id,
        checks=_checks("pass"),
    )
    _trace_submit(
        stage="trial",
        name=NAME,
        params={"format": "capability", "steps": len(capability_steps)},
        result={"ok": True, "resolved": len(trial.resolved_operations or [])},
        phase="trial",
        batch_id=trial.batch_id,
        checks=_checks("pass"),
        applied=False,
        changes=list(facts.get("presentationChanges") or []),
        structure=dict(trial.structure or {}),
        truncation=dict(facts.get("truncation") or {}),
    )
    _trace_submit(
        stage="candidate",
        name=NAME,
        params={"format": "capability"},
        result={
            "registered": True,
            "primaryTargetRegionId": primary,
            "summary": facts["summary"],
        },
        phase="candidate",
        batch_id=trial.batch_id,
        checks=_checks("pass"),
        # 候选**真的登记了**（``save_edit_item`` 成功）。
        candidate_registered=candidate_registered,
        applied=False,
        changes=list(facts.get("presentationChanges") or []),
        structure=dict(trial.structure or {}),
        truncation=dict(facts.get("truncation") or {}),
        extra={"itemId": item_id, "planId": plan_id},
    )
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
