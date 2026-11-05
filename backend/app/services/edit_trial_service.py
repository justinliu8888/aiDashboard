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
from app.services.edit_capabilities.chart_data import (
    PREVIEW_SPEC_KEY,
    SIMULATED_KEY,
)
from app.services.validation_service import SUPPORTED_CHART_KINDS

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# 结果
# --------------------------------------------------------------------------

@dataclass
class TrialResult:
    """一次试做的结果。

    ``ok`` 为真时：``definition`` 是**改好的副本**（可作为正式生效的目标），
    ``facts`` 是**依据实际差异**生成的确认事实（文字确认从这里来，
    不抄模型「打算做什么」）。
    """

    ok: bool
    definition: ReportDefinitionDraft | None = None
    change_set: ChangeSet | None = None
    errors: list[dict[str, Any]] = field(default_factory=list)
    facts: dict[str, Any] = field(default_factory=dict)
    refs: dict[str, str] = field(default_factory=dict)
    resolved_operations: list[dict[str, Any]] = field(default_factory=list)

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
    """
    before = definition.model_copy(deep=True)
    working = definition.model_copy(deep=True)
    base_revision = _revision(definition)
    errors: list[dict[str, Any]] = []
    resolved: list[dict[str, Any]] = []

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
        return TrialResult(ok=False, errors=errors)

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
            errors.append(exc.to_dict())
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
            errors.append(error.to_dict())
            break
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
            }
        )

    if errors:
        # 整批放弃：原草稿不动（副本直接丢掉）。
        return TrialResult(ok=False, errors=errors, refs=dict(ctx.refs))

    # ---- 核验最终定义 ----
    for region in working.regions or []:
        _validate_categorical(region, errors)
        _validate_chart_encoding(region, errors)
    payload = _validate_preview_payload(working, preview_context, errors)
    if errors:
        return TrialResult(ok=False, errors=errors, refs=dict(ctx.refs))

    _bump_revision(working)
    facts = _collect_facts(before, working, resolved, ctx.refs)

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
