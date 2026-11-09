"""隔离试做：把一份**动态操作清单**在草稿副本上整批执行并核验。

为什么要有这一层：

* 旧路径上「结构合法就提交」，完整执行检查发生在**接受之后** ——
  用户先看到一份看似可接受、实际不能执行的计划（需求稿 §1）。
* 需求稿要求：提交前先**完整预执行**，失败把**具体错误**退回**同一个**
  ReAct 循环，修正后再试；预执行失败**不写最终提交槽**。

这一层保证的事：

1. **副本隔离**：从权威草稿深拷贝出副本，只在副本上改。正式草稿、定义版本、
   撤销历史、共享主题都不碰（能力执行器只改副本里的 dict）。
2. **每步独立目标**：每一步有自己的 ``target``（既有 regionId 或本次新增的
   稳定 ref），不共用一个旧目标。
3. **整批原子**：任一步失败 → 整批不生效，原草稿原样。
4. **核验清单**：图表类型与数据编码、分类与数值对应、数据/输出引用、
   步骤目标、最终定义与展示载荷 —— 逐项查，不靠「执行器没报错」当通过。
"""
from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field
from typing import Any

from app.schemas.change_set import ChangeSet, ChangeSetEntry, ChangeSetUnchanged
from app.schemas.report_definition import ReportDefinitionDraft

from app.services.edit_capabilities import (
    CapabilityError,
    OperationContext,
    get_capability,
    validate_params,
)
from app.services.edit_capabilities.base import (
    BUSINESS_DETAIL_FIELD,
    BUSINESS_ISSUES_FIELD,
    BUSINESS_MESSAGE_FIELD,
    BUSINESS_OK_FIELD,
    BUSINESS_REASON_FIELD,
)
from app.services.edit_capabilities.chart_data import (
    PREVIEW_SPEC_KEY,
    SIMULATED_KEY,
)
from app.services.edit_capabilities.blocks import project_structure
from app.services.validation_service import SUPPORTED_CHART_KINDS

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# 结果
# --------------------------------------------------------------------------

def _business_facts(raw: Any) -> dict[str, Any]:
    """把执行实现报上来的**业务事实**整理成固定形状。

    ``businessOk`` 是**三态**：

    * ``True``  —— 业务上确实做成了；
    * ``False`` —— 业务上确实没做成（带着真实的 reason / issues）；
    * ``None``  —— **未取得**（框架没给业务结论，比如参数校验就没过）。

    **绝不拿「执行器没抛异常」当业务成功**。
    """
    data = raw if isinstance(raw, dict) else {}
    ok = data.get(BUSINESS_OK_FIELD)
    if ok is not None and not isinstance(ok, bool):
        ok = None
    issues = data.get(BUSINESS_ISSUES_FIELD)
    detail = data.get(BUSINESS_DETAIL_FIELD)
    return {
        BUSINESS_OK_FIELD: ok,
        BUSINESS_MESSAGE_FIELD: str(data.get(BUSINESS_MESSAGE_FIELD) or ""),
        BUSINESS_REASON_FIELD: str(data.get(BUSINESS_REASON_FIELD) or ""),
        BUSINESS_ISSUES_FIELD: [
            item
            for item in (issues if isinstance(issues, list) else [])
            if isinstance(item, dict)
        ],
        BUSINESS_DETAIL_FIELD: detail if isinstance(detail, dict) else {},
    }


def _attach_business(error: dict[str, Any], raw: Any) -> None:
    """把**业务事实**补进错误形状（增量字段，既有字段语义不动）。"""
    facts = _business_facts(raw)
    for field in (
        BUSINESS_OK_FIELD,
        BUSINESS_MESSAGE_FIELD,
        BUSINESS_REASON_FIELD,
        BUSINESS_ISSUES_FIELD,
        BUSINESS_DETAIL_FIELD,
    ):
        value = facts[field]
        if value in (None, "", [], {}):
            continue
        error[field] = value


def _new_batch_id() -> str:
    """一次试做的**批次 id**（同一个事实只有一个落点：由追踪服务生成）。"""
    try:
        from app.services import edit_trace_service

        return str(edit_trace_service.new_batch_id() or "")
    except Exception:  # noqa: BLE001 — 取不到批次号不该掀翻试做
        return ""


def _checks_of_step(ok: bool | None) -> dict[str, str]:
    """一步的三类检查状态。

    * 业务：这一步**确实**做成了 / 确实没做成 / **没执行**（未取得）；
    * 持久化：试做只跑在副本上，**没有**事务 → ``not_run``；
    * 效果核对：本轮**没有**效果核对行为 → ``not_run``（不推断）。
    """
    try:
        from app.services import edit_trace_service

        business = (
            edit_trace_service.CHECK_NOT_RUN
            if ok is None
            else (edit_trace_service.CHECK_PASS if ok else edit_trace_service.CHECK_FAIL)
        )
        return {
            "business": business,
            "persistence": edit_trace_service.PERSIST_NOT_RUN,
            "effect": edit_trace_service.EFFECT_NOT_RUN,
        }
    except Exception:  # noqa: BLE001
        return {"business": "unknown", "persistence": "not_run", "effect": "not_run"}


def _trace_step(
    *,
    index: int | None,
    op: str,
    target: dict,
    ok: bool | None,
    region_id: str = "",
    title: str = "",
    error: dict | None = None,
    business: Any = None,
    draft_changed: bool | None = False,
    phase: str = "trial",
    batch_id: str = "",
    changes: list[dict[str, Any]] | None = None,
    structure: dict[str, Any] | None = None,
    wrapper_path: list[str] | None = None,
    sub_step_index: int | None = None,
    not_executed: bool = False,
    truncation: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """把一步的执行结果写进追踪（**失败不影响主流程**）。

    记的是**实际发生的事**：这一步是什么能力、落在哪个目标、
    **业务上到底成没成**（``businessOk`` 三态）、失败时的真实业务原因与
    结构化问题、**正式草稿有没有变**，以及这一步的**精确前后变化**与
    **有界结构投影**。

    ``ok=None`` 表示这一步**没有执行**（前面的步骤失败导致整批放弃）：
    它既不是成功，也不是「没有变化」—— ``result`` 里带
    ``notExecuted=True`` 说清这一点。

    试做阶段一律 ``phase="trial"``、``draftChanged=False`` ——
    试做只在副本上跑，正式草稿**确实**没变（这是事实，不是推测）。
    """
    try:
        from app.services import edit_trace_service

        facts = _business_facts(business)
        result: dict = {"ok": ok, "index": index}
        if region_id:
            result["regionId"] = region_id
        if title:
            result["title"] = title
        if error:
            result["reason"] = str(error.get("reason") or "")
            result["constraint"] = str(error.get("constraint") or "")
            # 失败**原文**也留着：用户要看到「哪一步、哪条约束、为什么」，
            # 只有 reason / constraint 的机器码对用户没有意义。
            result["message"] = str(error.get("message") or "")
        if not_executed:
            # **未执行**要说得出口：不是成功，也不是「没有变化」。
            result["notExecuted"] = True
        # 业务事实也进 result：这一步**实际**发生了什么。
        result.update(facts)
        record_extra: dict[str, Any] = {
            "phase": phase,
            "draftChanged": draft_changed,
            **facts,
        }
        if extra:
            record_extra.update(extra)
        edit_trace_service.record(
            edit_trace_service.STAGE_TRIAL,
            name=op or "unknown_op",
            params={"target": target},
            result=result,
            status="ok" if ok is True else ("error" if ok is False else "skipped"),
            errorCategory="" if ok is not False else "trial_failed",
            batchId=batch_id,
            stepIndex=index,
            changes=list(changes or []),
            structure=dict(structure or {}),
            wrapperPath=list(wrapper_path or []),
            subStepIndex=sub_step_index,
            truncation=dict(truncation or {}),
            checks=_checks_of_step(ok),
            applied=False,
            # 裁剪过就**标出来**（不把「截断了」写成「完整内容」）。
            truncated=bool((truncation or {}).get("changes")),
            extra=record_extra,
        )
    except Exception:  # noqa: BLE001
        pass


@dataclass
class TrialResult:
    """一次试做的结果。

    ``ok`` 为真时：``definition`` 是**改好的副本**（可作为正式生效的目标），
    ``facts`` 是**依据实际差异**生成的确认事实（文字确认从这里来，
    不抄模型「打算做什么」）。

    新增字段（**增量**：既有字段语义不变）：

    * ``batch_id``：这次试做的**批次 id**（同一次调用里的多次试做靠它分开）；
    * ``structure``：整批的**有界结构投影**（受影响的区域 + 所在容器）。
    """

    ok: bool
    definition: ReportDefinitionDraft | None = None
    change_set: ChangeSet | None = None
    errors: list[dict[str, Any]] = field(default_factory=list)
    facts: dict[str, Any] = field(default_factory=dict)
    refs: dict[str, str] = field(default_factory=dict)
    resolved_operations: list[dict[str, Any]] = field(default_factory=list)
    #: 试做批次 id。
    batch_id: str = ""
    #: 整批的有界结构投影（``{"affected": [...], "containers": [...]}``）。
    structure: dict[str, Any] = field(default_factory=dict)

    @property
    def error_message(self) -> str:
        """给模型看的一句话错误（指出失败步骤、目标、约束、原因）。"""
        if not self.errors:
            return ""
        first = self.errors[0]
        parts = [str(first.get("message") or "修改未通过检查。")]
        for extra in self.errors[1:3]:
            parts.append(str(extra.get("message") or ""))
        return " ".join(part for part in parts if part)


@dataclass
class OperationSpec:
    """操作清单里的一项（模型给的结构化参数）。"""

    op: str
    target: Any = None
    params: dict[str, Any] = field(default_factory=dict)
    summary: str = ""

    @classmethod
    def from_raw(cls, raw: Any) -> "OperationSpec":
        if not isinstance(raw, dict):
            raise CapabilityError(
                "操作清单里每一项都必须是对象。",
                constraint="操作项必须是对象",
                reason="invalid_operation",
            )
        target = raw.get("target") or {}
        if isinstance(target, str):
            target = {"regionId": target}
        return cls(
            op=str(raw.get("op") or "").strip().upper(),
            target=target,
            params=raw.get("params") or {},
            summary=str(raw.get("summary") or ""),
        )


# --------------------------------------------------------------------------
# 试做
# --------------------------------------------------------------------------

def _revision(definition: ReportDefinitionDraft) -> int:
    return int((definition.metadata or {}).get("revision") or 0)


def _bump_revision(definition: ReportDefinitionDraft) -> None:
    definition.metadata = dict(definition.metadata or {})
    definition.metadata["revision"] = _revision(definition) + 1


def _preview_spec(region: dict[str, Any]) -> dict[str, Any]:
    binding = region.get("templateBinding") or {}
    spec = binding.get("previewSpec") if isinstance(binding, dict) else None
    return spec if isinstance(spec, dict) else {}


def _categorical_binding(region: dict[str, Any]) -> dict[str, Any] | None:
    binding = _preview_spec(region).get(PREVIEW_SPEC_KEY)
    return binding if isinstance(binding, dict) else None


def _validate_categorical(region: dict[str, Any], errors: list[dict[str, Any]]) -> None:
    """分类与数值必须一一对应，标签不能空、不能重复、数值必须是数。"""
    binding = _categorical_binding(region)
    if not binding:
        return
    region_id = str(region.get("id") or "")
    categories = binding.get("categories") or []
    labels: list[str] = []
    for item in categories:
        if not isinstance(item, dict):
            errors.append(
                {
                    "step": None,
                    "op": "SET_CATEGORY_DATA",
                    "target": region_id,
                    "constraint": "分类项必须是对象",
                    "reason": "invalid_category",
                    "message": f"区块 {region_id!r} 的分类项格式不对。",
                }
            )
            return
        label = str(item.get("label") or "").strip()
        if not label:
            errors.append(
                {
                    "step": None,
                    "op": "SET_CATEGORY_DATA",
                    "target": region_id,
                    "constraint": "分类标签不能为空",
                    "reason": "empty_category_label",
                    "message": f"区块 {region_id!r} 有一个分类没有标签。",
                }
            )
            return
        labels.append(label)
        try:
            float(item.get("value"))
        except (TypeError, ValueError):
            errors.append(
                {
                    "step": None,
                    "op": "SET_CATEGORY_DATA",
                    "target": region_id,
                    "constraint": "分类数值必须是数字",
                    "reason": "invalid_category_value",
                    "message": f"区块 {region_id!r} 的分类 {label!r} 的数值不是数字。",
                }
            )
            return
    if len(set(labels)) != len(labels):
        errors.append(
            {
                "step": None,
                "op": "SET_CATEGORY_DATA",
                "target": region_id,
                "constraint": "分类标签必须唯一",
                "reason": "duplicate_category",
                "message": f"区块 {region_id!r} 的分类标签重复。",
            }
        )


def _validate_chart_encoding(region: dict[str, Any], errors: list[dict[str, Any]]) -> None:
    """图表类型与数据编码要匹配，输出引用要有效。"""
    region_id = str(region.get("id") or "")
    component = region.get("component") or {}
    if str(component.get("type") or "") != "CHART":
        return
    chart_kind = str(component.get("chartKind") or "")
    if chart_kind not in SUPPORTED_CHART_KINDS:
        errors.append(
            {
                "step": None,
                "op": "SET_DISPLAY",
                "target": region_id,
                "constraint": "chartKind 必须在支持范围内",
                "reason": "unsupported_chart_kind",
                "message": (
                    f"区块 {region_id!r} 的图表类型 {chart_kind!r} 不支持；"
                    f"可用：{'、'.join(sorted(SUPPORTED_CHART_KINDS))}。"
                ),
            }
        )
    binding = _categorical_binding(region)
    if not binding:
        return
    encodings = component.get("encodings") or []
    channels = {
        str(e.get("channel") or "").upper(): str(e.get("fieldRef") or "")
        for e in encodings
        if isinstance(e, dict)
    }
    if channels.get("X") != str(binding.get("categoryField") or ""):
        errors.append(
            {
                "step": None,
                "op": "SET_CATEGORY_DATA",
                "target": region_id,
                "constraint": "横轴编码必须指向分类字段",
                "reason": "encoding_mismatch",
                "message": (
                    f"区块 {region_id!r} 的横轴编码与分类字段不一致"
                    f"（{channels.get('X')!r} ≠ {binding.get('categoryField')!r}）。"
                ),
            }
        )
    if channels.get("Y") != str(binding.get("valueField") or ""):
        errors.append(
            {
                "step": None,
                "op": "SET_CATEGORY_DATA",
                "target": region_id,
                "constraint": "纵轴编码必须指向计数结果字段",
                "reason": "encoding_mismatch",
                "message": (
                    f"区块 {region_id!r} 的纵轴编码与计数字段不一致"
                    f"（{channels.get('Y')!r} ≠ {binding.get('valueField')!r}）。"
                ),
            }
        )
    output_ref = str((region.get("binding") or {}).get("outputRef") or "")
    if not output_ref:
        errors.append(
            {
                "step": None,
                "op": "ADD_SECTION",
                "target": region_id,
                "constraint": "区块必须有输出引用",
                "reason": "missing_output_ref",
                "message": f"区块 {region_id!r} 没有输出引用。",
            }
        )
    else:
        pipeline = (region.get("data") or {}).get("pipeline") or {}
        outputs = pipeline.get("outputs") or {}
        key = output_ref.split(":", 1)[-1]
        if key not in outputs:
            errors.append(
                {
                    "step": None,
                    "op": "ADD_SECTION",
                    "target": region_id,
                    "constraint": "输出引用必须指向已声明的输出",
                    "reason": "unknown_output_ref",
                    "message": (
                        f"区块 {region_id!r} 的输出引用 {output_ref!r} 没有对应的输出定义。"
                    ),
                }
            )


def _validate_preview_payload(
    definition: ReportDefinitionDraft,
    preview_context: dict[str, Any] | None,
    errors: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """最终定义必须有对应的展示载荷（每个区块都能出数）。"""
    if not preview_context:
        return None
    from app.services.preview_service import (
        PreviewContextError,
        build_report_preview_payload,
    )
    from app.services.simulated_data_service import SimulatedDataError

    definition_dict = definition.model_dump(mode="json")
    try:
        payload = build_report_preview_payload(
            definition_dict, preview_context
        )
    except (SimulatedDataError, PreviewContextError, ValueError) as exc:
        errors.append(
            {
                "step": None,
                "op": "CHECK_PAYLOAD",
                "target": "",
                "constraint": "最终定义必须能生成展示载荷",
                "reason": "preview_payload_failed",
                "message": f"修改后的草稿生成不出展示载荷：{exc}",
            }
        )
        return None
    regions = payload.get("regions") or []
    chart_regions = [
        region
        for region in definition.regions or []
        if str((region.get("component") or {}).get("type") or "") == "CHART"
    ]
    payload_ids = {
        str(region.get("regionId") or "") for region in regions if isinstance(region, dict)
    }
    for region in chart_regions:
        region_id = str(region.get("id") or "")
        if region_id not in payload_ids:
            errors.append(
                {
                    "step": None,
                    "op": "CHECK_PAYLOAD",
                    "target": region_id,
                    "constraint": "每个图表区块都要有展示载荷",
                    "reason": "missing_preview_payload",
                    "message": f"图表区块 {region_id!r} 没有对应的展示载荷。",
                }
            )
    # 分类载荷的标签与数值必须一一对应。
    for region in regions:
        if not isinstance(region, dict):
            continue
        payload_body = region.get("payload") or {}
        labels = payload_body.get("xLabels") or []
        values = payload_body.get("yValues") or []
        if labels and values and len(labels) != len(values):
            errors.append(
                {
                    "step": None,
                    "op": "CHECK_PAYLOAD",
                    "target": str(region.get("regionId") or ""),
                    "constraint": "标签与数值必须一一对应",
                    "reason": "payload_length_mismatch",
                    "message": (
                        f"区块 {str(region.get('regionId') or '')!r} 的标签数"
                        f"（{len(labels)}）与数值数（{len(values)}）不一致。"
                    ),
                }
            )
    return payload


def _collect_facts(
    before: ReportDefinitionDraft,
    after: ReportDefinitionDraft,
    operations: list[dict[str, Any]],
    refs: dict[str, str],
) -> dict[str, Any]:
    """依据**实际差异**整理确认事实。

    文字确认从这里生成 —— 不是抄模型「打算做什么」。
    """
    before_regions = {str(r.get("id") or ""): r for r in before.regions or []}
    after_regions = {str(r.get("id") or ""): r for r in after.regions or []}
    added: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    modified: list[dict[str, Any]] = []
    unchanged_ids: list[str] = []

    for region_id, region in after_regions.items():
        if region_id not in before_regions:
            component = region.get("component") or {}
            binding = _categorical_binding(region) or {}
            added.append(
                {
                    "regionId": region_id,
                    "title": str(region.get("name") or region_id),
                    "componentType": str(component.get("type") or ""),
                    "chartKind": str(component.get("chartKind") or ""),
                    "categoryLabel": str(binding.get("categoryLabel") or ""),
                    "statistic": str(binding.get("statistic") or ""),
                    "categories": [
                        {"label": item.get("label"), "value": item.get("value")}
                        for item in binding.get("categories") or []
                    ],
                    "unit": str(binding.get("unit") or ""),
                    "simulated": bool(binding.get(SIMULATED_KEY)),
                }
            )
    for region_id, region in before_regions.items():
        if region_id not in after_regions:
            removed.append(
                {
                    "regionId": region_id,
                    "title": str(region.get("name") or region_id),
                    "componentType": str((region.get("component") or {}).get("type") or ""),
                }
            )
    for region_id, region in after_regions.items():
        old = before_regions.get(region_id)
        if old is None:
            continue
        if copy.deepcopy(old) == region:
            unchanged_ids.append(region_id)
            continue
        old_component = old.get("component") or {}
        new_component = region.get("component") or {}
        if old_component.get("chartKind") != new_component.get("chartKind"):
            modified.append(
                {
                    "regionId": region_id,
                    "title": str(region.get("name") or region_id),
                    "beforeChartKind": str(old_component.get("chartKind") or ""),
                    "afterChartKind": str(new_component.get("chartKind") or ""),
                }
            )
        else:
            modified.append(
                {
                    "regionId": region_id,
                    "title": str(region.get("name") or region_id),
                    "beforeChartKind": str(old_component.get("chartKind") or ""),
                    "afterChartKind": str(new_component.get("chartKind") or ""),
                }
            )

    # 位置：新块是否落在被删块原来的位置。
    #
    # 判定方式：新增的区块在**改后**的顺序里必须连成一段，且这一段**前面**
    # 的区块序列与**改前**被删位置之前的区块序列完全一样 —— 换句话说，
    # 新块确实顶上了老块的位置，而不是被挪到了别处。
    replaced_in_place = False
    if removed and added:
        before_ids = [str(r.get("id") or "") for r in before.regions or []]
        after_ids = [str(r.get("id") or "") for r in after.regions or []]
        removed_id = removed[0]["regionId"]
        added_ids = [item["regionId"] for item in added]
        if removed_id in before_ids:
            removed_index = before_ids.index(removed_id)
            start = None
            for index in range(len(after_ids) - len(added_ids) + 1):
                if after_ids[index: index + len(added_ids)] == added_ids:
                    start = index
                    break
            if start is not None:
                replaced_in_place = (
                    after_ids[:start] == before_ids[:removed_index]
                )
    return {
        "added": added,
        "removed": removed,
        "modified": modified,
        "unchangedRegionIds": unchanged_ids,
        "refs": dict(refs),
        "replacedInPlace": replaced_in_place,
        "operationCount": len(operations),
        "primaryTargetRegionId": (
            (removed[0]["regionId"] if removed else "")
            or (added[0]["regionId"] if added else "")
            or (modified[0]["regionId"] if modified else "")
        ),
        "baseRevision": _revision(before),
    }


def _outcome_facts(detail: Any) -> dict[str, Any]:
    """能力执行器的 ``detail`` → 这一步的**前后事实**。

    ``changes`` / ``structure`` 来自能力执行器透传上来的那份（展示服务的
    精确 changeSet + 有界结构投影）；**没有**就如实说没有 —— 不自己编。
    """
    data = detail if isinstance(detail, dict) else {}
    changes = data.get("changes")
    structure = data.get("structure")
    truncation = data.get("truncation")
    sub_operations = data.get("subOperations")
    return {
        "changes": (
            [item for item in changes if isinstance(item, dict)]
            if isinstance(changes, list)
            else []
        ),
        "structure": dict(structure) if isinstance(structure, dict) else {},
        "truncation": dict(truncation) if isinstance(truncation, dict) else {},
        "subOperations": (
            [item for item in sub_operations if isinstance(item, dict)]
            if isinstance(sub_operations, list)
            else []
        ),
        # 这一步**报没报过**前后变化（没报 ≠ 没有变化）。
        "changeReported": "changes" in data,
    }


def _pending_step_facts(raw: Any) -> tuple[str, dict[str, Any]]:
    """没执行的那一步：能说清是什么能力、什么目标就说清。"""
    op = ""
    target: dict[str, Any] = {}
    if isinstance(raw, dict):
        op = str(raw.get("op") or "").strip().upper()
        raw_target = raw.get("target")
        if isinstance(raw_target, dict):
            target = {
                key: value
                for key, value in raw_target.items()
                if isinstance(value, str) and value
            }
        elif isinstance(raw_target, str) and raw_target:
            target = {"regionId": raw_target}
    return op, target


def _unchanged_operations(resolved: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """**没有变化**的操作与依据。

    只有「报过前后变化」的能力才谈得上「没有变化」：别的能力**没报**
    变化集 —— 那是「没取得」，不是「没有变化」。
    """
    out: list[dict[str, Any]] = []
    for item in resolved:
        if not item.get("changeReported"):
            continue
        if item.get("changes"):
            continue
        out.append(
            {
                "index": item.get("index"),
                "op": str(item.get("op") or ""),
                "regionId": str(item.get("regionId") or ""),
                "title": str(item.get("title") or ""),
                "basis": "执行器报出的前后变化为空",
            }
        )
    return out


def _check_report(
    preview_context: dict[str, Any] | None, *, reached: bool
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """这次试做**实际做了哪些检查**、**哪些没做**（缺失写明缺失）。

    ``reached=False``：整批在执行阶段就放弃了，核验**根本没跑到**。
    """
    if not reached:
        return [], [
            {
                "check": "batch_verification",
                "scope": "整批",
                "performed": False,
                "reason": "not_reached",
                "missing": [],
            }
        ]
    performed: list[dict[str, Any]] = [
        {
            "check": "categorical_binding",
            "scope": "每个图表区块",
            "performed": True,
        },
        {
            "check": "chart_encoding",
            "scope": "每个图表区块",
            "performed": True,
        },
    ]
    skipped: list[dict[str, Any]] = []
    if preview_context:
        performed.append(
            {"check": "preview_payload", "scope": "最终定义", "performed": True}
        )
    else:
        # 没取到上下文 → **如实说没做**，不猜它会通过。
        skipped.append(
            {
                "check": "preview_payload",
                "scope": "最终定义",
                "performed": False,
                "reason": "missing_context",
                "missing": ["previewContext"],
            }
        )
    return performed, skipped


def _truncation_of(notes: list[dict[str, Any]]) -> dict[str, Any]:
    """字段级裁剪痕迹（没裁过就是空字典 —— 不写空的假痕迹）。"""
    return {"changes": list(notes)} if notes else {}


def _batch_facts(
    *,
    batch_id: str,
    structure: dict[str, Any],
    changes: list[dict[str, Any]],
    truncation: dict[str, Any],
    scope: str,
    unchanged_operations: list[dict[str, Any]],
    checks_performed: list[dict[str, Any]],
    checks_skipped: list[dict[str, Any]],
) -> dict[str, Any]:
    """这一批的**事实**（只新增键；既有键的语义不动）。"""
    return {
        "batchId": batch_id,
        "structure": dict(structure or {}),
        # ``copy_only``：副本上的局部变化，整批没通过、没正式生效；
        # ``batch_result``：这一批跑完的结构。
        "structureScope": scope,
        "presentationChanges": list(changes or []),
        "truncation": dict(truncation or {}),
        "unchangedOperations": list(unchanged_operations or []),
        "checksPerformed": list(checks_performed or []),
        "checksSkipped": list(checks_skipped or []),
    }


def _record_not_executed(
    *, batch_id: str, operations: list[Any], start_index: int
) -> list[int]:
    """为失败步之后的步骤补记「**未执行**」。

    后续步骤**根本没跑**：既不是成功，也不是「没有变化」。
    """
    recorded: list[int] = []
    for index in range(max(start_index, 0), len(operations)):
        op, target = _pending_step_facts(operations[index])
        _trace_step(
            index=index,
            op=op,
            target=target,
            ok=None,
            batch_id=batch_id,
            not_executed=True,
            extra={"notExecutedReason": "previous_step_failed"},
        )
        recorded.append(index)
    return recorded


def run_trial(
    *,
    definition: ReportDefinitionDraft,
    operations: list[Any],
    scope: Any = None,
    preview_context: dict[str, Any] | None = None,
) -> TrialResult:
    """在**副本**上整批执行操作清单并核验。

    返回的 ``TrialResult.definition`` 是改好的副本；``ok`` 为假时
    ``definition`` 为 ``None``（原草稿原样，调用方不需要清理）。

    这次试做有**自己的批次 id**（``TrialResult.batch_id``）：同一次调用里
    的多次试做靠它分开，第 1 步不会和上一次试做的第 1 步混成一步。
    """
    before = definition.model_copy(deep=True)
    working = definition.model_copy(deep=True)
    base_revision = _revision(definition)
    errors: list[dict[str, Any]] = []
    resolved: list[dict[str, Any]] = []
    batch_id = _new_batch_id()
    #: 这一批里**逐步**的前后变化（汇总用）。
    step_changes: list[dict[str, Any]] = []
    #: 这一批里受影响的区域 id（结构投影用）。
    affected_ids: list[str] = []
    #: 字段级裁剪痕迹。
    truncation_notes: list[dict[str, Any]] = []

    ctx = OperationContext(
        definition=working,
        scope=scope,
        refs={},
        base_revision=base_revision,
        preview_context=preview_context or {},
    )

    if not operations:
        errors.append(
            {
                "step": None,
                "op": "",
                "target": "",
                "constraint": "操作清单不能为空",
                "reason": "empty_operations",
                "message": "这次修改没有任何操作。",
            }
        )
        _trace_step(
            index=0,
            op="",
            target={},
            ok=False,
            error=errors[-1],
            batch_id=batch_id,
            extra={
                "batchOutcome": "failed",
                "copyOnly": True,
                "appliedToDraft": False,
                "note": "副本上的局部变化，整批未通过、未正式生效",
            },
        )
        return TrialResult(
            ok=False,
            errors=errors,
            batch_id=batch_id,
            facts=_batch_facts(
                batch_id=batch_id,
                structure={},
                changes=[],
                truncation={},
                scope="copy_only",
                unchanged_operations=[],
                checks_performed=[],
                checks_skipped=[],
            ),
        )

    for index, raw in enumerate(operations):
        ctx.step = index
        try:
            spec = OperationSpec.from_raw(raw)
        except CapabilityError as exc:
            exc.step = index
            errors.append(exc.to_dict())
            break
        ctx.op = spec.op
        capability = get_capability(spec.op)
        if capability is None:
            errors.append(
                {
                    "step": index,
                    "op": spec.op,
                    "target": "",
                    "constraint": "操作名必须是已登记的能力",
                    "reason": "unknown_capability",
                    "message": (
                        f"第 {index + 1} 步的操作 {spec.op!r} 不是已登记的能力；"
                        "可用能力见能力目录。"
                    ),
                }
            )
            break
        try:
            params = validate_params(capability, spec.params)
            ctx.current_target = spec.target
            outcome = capability.executor(ctx, params)
        except CapabilityError as exc:
            exc.step = index
            exc.op = exc.op or spec.op
            error = exc.to_dict()
            # 业务事实是**增量字段**：不改动 step / op / target / constraint /
            # reason / message 的既有语义，只是把真实的业务结论补上去。
            _attach_business(error, getattr(exc, "business", None))
            errors.append(error)
            break
        except Exception as exc:  # noqa: BLE001 — 执行异常也要变成可修正的反馈
            errors.append(
                {
                    "step": index,
                    "op": spec.op,
                    "target": "",
                    "constraint": "这一步必须能真正执行",
                    "reason": "operation_failed",
                    "message": f"第 {index + 1} 步执行失败：{exc}",
                }
            )
            break
        if not outcome.ok:
            error = outcome.error or CapabilityError(
                outcome.message or "这一步没有成功。",
                step=index,
                op=spec.op,
                reason="operation_failed",
            )
            error.step = index
            error_dict = error.to_dict()
            _attach_business(error_dict, getattr(error, "business", None))
            errors.append(error_dict)
            break
        facts_of_step = _outcome_facts(outcome.detail)
        resolved.append(
            {
                "index": index,
                "op": spec.op,
                "target": spec.target if isinstance(spec.target, dict) else {},
                "params": spec.params if isinstance(spec.params, dict) else {},
                "regionId": outcome.region_id,
                "title": outcome.title,
                "chartKind": outcome.chart_kind,
                "message": outcome.message,
                "detail": outcome.detail,
                # 这一步的**精确前后变化**与**结构投影**（追踪与事实用）。
                "changes": facts_of_step["changes"],
                "structure": facts_of_step["structure"],
                "changeReported": facts_of_step["changeReported"],
            }
        )
        step_changes.extend(facts_of_step["changes"])
        truncation_notes.extend(facts_of_step["truncation"].get("changes") or [])
        for item in facts_of_step["structure"].get("affected") or []:
            region_id = str((item or {}).get("regionId") or "")
            if region_id and region_id not in affected_ids:
                affected_ids.append(region_id)
        _trace_step(
            index=index,
            op=spec.op,
            target=spec.target if isinstance(spec.target, dict) else {},
            ok=True,
            region_id=outcome.region_id,
            title=outcome.title,
            business=outcome.detail,
            batch_id=batch_id,
            changes=facts_of_step["changes"],
            structure=facts_of_step["structure"],
            truncation=facts_of_step["truncation"],
        )
        # 包装能力（展示操作包）里的**子操作**：只记操作名与下标 ——
        # 展示服务给的是**整批** changeSet，没法诚实地拆到子操作，
        # 所以这里不伪造子操作的前后变化。
        for position, sub in enumerate(facts_of_step["subOperations"]):
            _trace_step(
                index=index,
                op=spec.op,
                target=spec.target if isinstance(spec.target, dict) else {},
                ok=True,
                business=None,
                batch_id=batch_id,
                structure=facts_of_step["structure"],
                wrapper_path=[spec.op],
                sub_step_index=position,
                extra={
                    "subOp": str(sub.get("op") or ""),
                    "changesAttributed": False,
                },
            )

    if errors:
        # 整批放弃：原草稿不动（副本直接丢掉）。
        failed_index = errors[-1].get("step")
        failed_index = (
            int(failed_index) if isinstance(failed_index, int) else len(resolved)
        )
        structure = project_structure(working, affected_ids, before=before)
        _trace_step(
            index=failed_index,
            op=str(errors[-1].get("op") or ""),
            target={"target": str(errors[-1].get("target") or "")},
            ok=False,
            error=errors[-1],
            business=errors[-1],
            draft_changed=False,
            batch_id=batch_id,
            changes=step_changes,
            structure=structure,
            truncation=_truncation_of(truncation_notes),
            extra={
                "batchOutcome": "failed",
                "copyOnly": True,
                "appliedToDraft": False,
                "partialStepCount": len(resolved),
                "partialChanges": step_changes,
                "note": "副本上的局部变化，整批未通过、未正式生效",
            },
        )
        # 失败步之后的步骤**根本没跑** —— 明确标「未执行」。
        _record_not_executed(
            batch_id=batch_id, operations=operations, start_index=failed_index + 1
        )
        checks_performed, checks_skipped = _check_report(
            preview_context, reached=False
        )
        return TrialResult(
            ok=False,
            errors=errors,
            refs=dict(ctx.refs),
            batch_id=batch_id,
            structure=structure,
            facts=_batch_facts(
                batch_id=batch_id,
                structure=structure,
                changes=step_changes,
                truncation=_truncation_of(truncation_notes),
                scope="copy_only",
                unchanged_operations=_unchanged_operations(resolved),
                checks_performed=checks_performed,
                checks_skipped=checks_skipped,
            ),
        )

    # ---- 核验最终定义 ----
    for region in working.regions or []:
        _validate_categorical(region, errors)
        _validate_chart_encoding(region, errors)
    payload = _validate_preview_payload(working, preview_context, errors)
    if errors:
        # 核验阶段失败：也是**业务事实**（确实没通过检查），不是框架状态。
        for item in errors:
            _attach_business(
                item,
                {
                    BUSINESS_OK_FIELD: False,
                    BUSINESS_REASON_FIELD: str(item.get("reason") or ""),
                    BUSINESS_MESSAGE_FIELD: str(item.get("message") or ""),
                },
            )
        # 也要进追踪（否则「预执行没通过」查不到是哪一步）。
        #
        # 这是**批次级**的失败（每一步都跑完了，是核验没过）：
        # 不对应某一步 → 步骤下标留空，**别把某一步说成失败**。
        structure = project_structure(working, affected_ids, before=before)
        _trace_step(
            index=None,
            op=str(errors[-1].get("op") or ""),
            target={"target": str(errors[-1].get("target") or "")},
            ok=False,
            error=errors[-1],
            business=errors[-1],
            draft_changed=False,
            batch_id=batch_id,
            changes=step_changes,
            structure=structure,
            truncation=_truncation_of(truncation_notes),
            extra={
                "batchOutcome": "failed",
                "failureScope": "batch_verification",
                "copyOnly": True,
                "appliedToDraft": False,
                "partialStepCount": len(resolved),
                "partialChanges": step_changes,
                "note": "副本上的局部变化，整批未通过、未正式生效",
            },
        )
        checks_performed, checks_skipped = _check_report(
            preview_context, reached=True
        )
        return TrialResult(
            ok=False,
            errors=errors,
            refs=dict(ctx.refs),
            batch_id=batch_id,
            structure=structure,
            facts=_batch_facts(
                batch_id=batch_id,
                structure=structure,
                changes=step_changes,
                truncation=_truncation_of(truncation_notes),
                scope="copy_only",
                unchanged_operations=_unchanged_operations(resolved),
                checks_performed=checks_performed,
                checks_skipped=checks_skipped,
            ),
        )

    _bump_revision(working)
    facts = _collect_facts(before, working, resolved, ctx.refs)
    structure = project_structure(working, affected_ids, before=before)
    checks_performed, checks_skipped = _check_report(preview_context, reached=True)
    facts.update(
        _batch_facts(
            batch_id=batch_id,
            structure=structure,
            changes=step_changes,
            truncation=_truncation_of(truncation_notes),
            scope="batch_result",
            unchanged_operations=_unchanged_operations(resolved),
            checks_performed=checks_performed,
            checks_skipped=checks_skipped,
        )
    )

    change_set = ChangeSet(
        baseDefinitionRevision=base_revision,
        changed=[
            ChangeSetEntry(
                regionId=item["regionId"],
                path=f"/regions/{item['regionId']}",
                before=None,
                after=next(
                    (
                        r
                        for r in working.regions or []
                        if str(r.get("id") or "") == item["regionId"]
                    ),
                    None,
                ),
                summary=f"新增「{item['title']}」",
            )
            for item in facts["added"]
        ]
        + [
            ChangeSetEntry(
                regionId=item["regionId"],
                path=f"/regions/{item['regionId']}",
                before=before_regions_of(before, item["regionId"]),
                after=None,
                summary=f"删除「{item['title']}」",
            )
            for item in facts["removed"]
        ]
        + [
            ChangeSetEntry(
                regionId=item["regionId"],
                path=f"/regions/{item['regionId']}/component",
                before=None,
                after=None,
                summary=f"修改「{item['title']}」的展示方式",
            )
            for item in facts["modified"]
        ],
        unchanged=[
            ChangeSetUnchanged(regionId=region_id, summary="未修改")
            for region_id in facts["unchangedRegionIds"]
        ],
        impact=[],
    )

    return TrialResult(
        ok=True,
        definition=working,
        change_set=change_set,
        facts=facts,
        refs=dict(ctx.refs),
        resolved_operations=resolved,
        batch_id=batch_id,
        structure=structure,
    )


def before_regions_of(definition: ReportDefinitionDraft, region_id: str) -> Any:
    for region in definition.regions or []:
        if str(region.get("id") or "") == region_id:
            return copy.deepcopy(region)
    return None


def summarize_facts(facts: dict[str, Any]) -> str:
    """依据实际差异生成**简短**确认文字。

    规则：只说实际发生的事；有几处就说几处；位置只有在真的替换原位时才说。
    """
    added = facts.get("added") or []
    removed = facts.get("removed") or []
    modified = facts.get("modified") or []
    lines: list[str] = []

    if added and removed:
        removed_titles = "、".join(f"「{item['title']}」" for item in removed)
        lines.append(f"将{removed_titles}替换为 {len(added)} 张图：")
        for item in added:
            detail = ""
            if item.get("statistic"):
                detail = f"（{item['statistic']}）"
            kind = {"PIE": "饼图", "BAR": "柱状图", "LINE": "折线图", "AREA": "面积图"}.get(
                str(item.get("chartKind") or ""), ""
            )
            suffix = f"，{kind}" if kind else ""
            lines.append(f"- {item['title']}：{detail}{suffix}")
    elif added:
        lines.append(f"新增 {len(added)} 个区块：")
        for item in added:
            lines.append(f"- {item['title']}")
    elif removed:
        titles = "、".join(f"「{item['title']}」" for item in removed)
        lines.append(f"删除 {titles}")

    if modified:
        parts = []
        for item in modified:
            before = {"PIE": "饼图", "BAR": "柱状图", "LINE": "折线图", "AREA": "面积图"}.get(
                str(item.get("beforeChartKind") or ""), ""
            )
            after = {"PIE": "饼图", "BAR": "柱状图", "LINE": "折线图", "AREA": "面积图"}.get(
                str(item.get("afterChartKind") or ""), ""
            )
            if before and after and before != after:
                parts.append(f"{item['title']}：{before} → {after}")
            else:
                parts.append(str(item["title"]))
        lines.append("调整：" + "；".join(parts))

    if not lines:
        lines.append("本次修改没有改变区块内容。")

    if facts.get("replacedInPlace"):
        lines.append("放在原位置，其他内容不变。")
    return "\n".join(lines)
