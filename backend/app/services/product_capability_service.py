"""实际产品能力事实模块（工单 04 / GitHub #16）。

用户问「你支持分钟趋势吗」「能耗成本能出吗」这类问题时，
助手要依据**可核实的登记事实和执行规则**回答，而不是凭印象承诺。

这个模块只回答事实：��生成、不修改、不保存、不预览、不推进任何业务对象。

## 四层语义，必须分开说

    1. 目录登记      —— 指标 / 数据源在不在**当前授权目录**里；
    2. 链路执行支持  —— 当前生成 / 编辑 / 预览链路真的跑不跑得出这个结果；
    3. 数据来源      —— 预览数值是模拟业务数据，还是已接通生产取数；
    4. 边界          —— 确实没有 / 无权限 / 上下文不足，三者不能混着说。

「目录里登记了」不等于「产品链路支持」：成本、PUE 就在指标目录里，
但当前机房能耗模板出不了这个数。这两件事必须分别报。

## 不维护第二份支持清单

粒度支持来自目录登记（``catalog.supportedGrains``），
指标能不能出数直接问**生成链路自己的解析规则**（``report_draft_service``），
区块白名单同样取自生成链路（``COMPONENT_KEYS``）。
规则改了这里跟着改，不会出现「目录说支持、链路不支持」的第二份清单。

对生成链路的私有解析器（``_resolve_grain`` / ``_resolve_metrics``），
这里刻意**直接调用**而不是复刻一遍 —— 复刻就等于维护第二份支持清单。
它们是纯函数：不保存、不预览、不写业务状态，只抛阻断异常。
"""
from __future__ import annotations

from typing import Any

from app.services import catalog_mock_service
from app.services import metric_mock_service
from app.services import report_draft_service
from app.services import scope_service


# ---------- 结果语义 ----------

#: 事实已查到，且当前链路支持所问的能力。
OUTCOME_OK = "OK"
#: 对象在授权目录里登记了，但当前生成 / 编辑 / 预览链路不支持它。
OUTCOME_REGISTERED_NOT_SUPPORTED = "REGISTERED_NOT_SUPPORTED"
#: 当前授权目录里确实没有这个对象。
OUTCOME_NOT_REGISTERED = "NOT_REGISTERED"
#: 对象存在但当前账号没有访问权限 —— 不等于「不存在」。
OUTCOME_NOT_AUTHORIZED = "NOT_AUTHORIZED"
#: 上下文不足，无法定位到具体对象；已给出确定的产品范围并提出澄清。
OUTCOME_MISSING_CONTEXT = "MISSING_CONTEXT"


_GRAIN_LABELS = {
    "MINUTE": "分钟",
    "HOUR": "小时",
    "DAY": "天",
    "WEEK": "周",
    "MONTH": "月",
}

#: 需要设备维度的区块：没有设备字段就排不出名次。
_DEVICE_DIMENSION_COMPONENTS = ("ranking",)
#: 需要时间维度的区块：没有时间字段就画不出趋势。
_TIME_DIMENSION_COMPONENTS = ("trend",)
#: 需要可聚合数值字段的区块。
_VALUE_FIELD_COMPONENTS = ("total",)


def describe_capability(
    *,
    project_id: str | None = None,
    subject_ref: str | None = None,
    subject_id: str | None = None,
    source_ref: str | None = None,
    metric_ref: str | None = None,
    grain_unit: str | None = None,
    component: str | None = None,
) -> dict[str, Any]:
    """核对一项报表能力在当前产品链路的真实边界。

    三个入参都是可选的：

        source_ref   具体数据源（``catalog.machine_room.energy`` 等）
        metric_ref   具体指标（``metric.machine_room.total_energy`` 等）
        grain_unit   时间粒度（``MINUTE`` / ``HOUR`` / ``DAY`` ...）
        component    报表区块（``total`` / ``ranking`` / ``trend`` ...）

    只给粒度或区块、没给数据源时，返回当前授权范围内的**产品范围**，
    并附一条针对性澄清，而不是替用户挑一个数据源。
    """
    source_ref = _clean(source_ref)
    metric_ref = _clean(metric_ref)
    grain_unit = _clean(grain_unit)
    grain_unit = grain_unit.upper() if grain_unit else None
    component = _clean(component)

    if source_ref:
        return _describe_source(
            project_id=project_id,
            subject_ref=subject_ref,
            subject_id=subject_id,
            source_ref=source_ref,
            metric_ref=metric_ref,
            grain_unit=grain_unit,
            component=component,
        )
    if metric_ref:
        return _describe_metric(
            project_id=project_id,
            subject_ref=subject_ref,
            subject_id=subject_id,
            metric_ref=metric_ref,
            grain_unit=grain_unit,
            component=component,
        )
    return _describe_scope(
        project_id=project_id,
        subject_ref=subject_ref,
        subject_id=subject_id,
        grain_unit=grain_unit,
        component=component,
    )


# ---------- 按数据源核对 ----------

def _describe_source(
    *,
    project_id: str | None,
    subject_ref: str | None,
    subject_id: str | None,
    source_ref: str,
    metric_ref: str | None,
    grain_unit: str | None,
    component: str | None,
) -> dict[str, Any]:
    question = _question(source_ref, metric_ref, grain_unit, component)
    catalog = catalog_mock_service.get_catalog(source_ref)
    if catalog is None:
        return _not_registered_source(
            question=question,
            source_ref=source_ref,
            project_id=project_id,
            subject_ref=subject_ref,
            subject_id=subject_id,
        )
    if not _visible(catalog, project_id, subject_ref, subject_id):
        return _not_authorized(question, kind="source", ref=source_ref)

    catalog_facts = _catalog_facts(catalog)
    components, component_reasons = _component_support(catalog)
    grain = _grain_facts(catalog, grain_unit)

    metric_facts: dict[str, Any] = _empty_metric(metric_ref)
    metric_generation: tuple[bool, str | None, list[str]] = (True, None, [])
    if metric_ref:
        metric = metric_mock_service.get_metric(metric_ref)
        if metric is None:
            return _not_registered_metric(question, metric_ref)
        if not _visible(metric, project_id, subject_ref, subject_id):
            return _not_authorized(question, kind="metric", ref=metric_ref)
        metric_facts = _metric_facts(metric)
        metric_generation = _generation_support_for_metric(metric_ref)

    component_key = _normalize_component(component)
    component_fact = _component_fact(
        component_key, components, component_reasons, component
    )

    generation_supported, generation_reason = _generation_verdict(
        catalog=catalog,
        components=components,
        component_reasons=component_reasons,
        component_key=component_key,
        grain=grain,
        metric_generation=metric_generation,
    )

    alternatives = _collect_alternatives(
        grain=grain,
        metric_generation=metric_generation,
        components=components,
        component_reasons=component_reasons,
        component_key=component_key,
    )

    outcome = (
        OUTCOME_OK
        if generation_supported
        else OUTCOME_REGISTERED_NOT_SUPPORTED
    )

    return {
        "outcome": outcome,
        "question": question,
        "catalog": catalog_facts,
        "metric": metric_facts,
        "execution": {
            "generation": {
                "supported": generation_supported,
                "reason": generation_reason,
            },
            "edit": {
                "supported": generation_supported,
                "reason": _edit_reason(generation_supported, generation_reason),
            },
            "preview": {
                "supported": generation_supported,
                "reason": _preview_reason(generation_supported, generation_reason),
            },
            "components": components,
            "componentReasons": component_reasons,
            "component": component_fact,
            "grain": grain,
        },
        "dataSource": _data_source_facts(source_ref),
        "alternatives": alternatives,
        "clarification": None,
        "mustState": _must_state(outcome, generation_reason, catalog_facts),
    }


# ---------- 按指标核对 ----------

def _describe_metric(
    *,
    project_id: str | None,
    subject_ref: str | None,
    subject_id: str | None,
    metric_ref: str,
    grain_unit: str | None,
    component: str | None,
) -> dict[str, Any]:
    question = _question(None, metric_ref, grain_unit, component)
    metric = metric_mock_service.get_metric(metric_ref)
    if metric is None:
        return _not_registered_metric(question, metric_ref)
    if not _visible(metric, project_id, subject_ref, subject_id):
        return _not_authorized(question, kind="metric", ref=metric_ref)

    metric_facts = _metric_facts(metric)
    generation = _generation_support_for_metric(metric_ref)

    sources = _visible_catalogs(project_id, subject_ref, subject_id)
    metric_facts["sourcesProvidingField"] = [
        item["sourceRef"]
        for item in sources
        if metric.get("fieldRef") in (item.get("fields") or {})
    ]

    components, component_reasons = _component_support(_default_catalog())
    grain = _grain_facts(_default_catalog(), grain_unit)
    component_key = _normalize_component(component)
    component_fact = _component_fact(
        component_key, components, component_reasons, component
    )

    supported, reason = generation[0], generation[1]
    if supported and not metric_facts["sourcesProvidingField"]:
        supported = False
        reason = (
            f"指标 {metric_ref} 依赖字段 {metric.get('fieldRef')}，"
            "当前授权范围内的数据源都没有这个字段。"
        )

    outcome = OUTCOME_OK if supported else OUTCOME_REGISTERED_NOT_SUPPORTED
    return {
        "outcome": outcome,
        "question": question,
        "catalog": {
            "registered": False,
            "sourceRef": None,
            "description": None,
            "supportedGrains": [],
            "fields": [],
            "hasDeviceDimension": False,
            "timeField": None,
            "missingFields": [],
        },
        "metric": metric_facts,
        "execution": {
            "generation": {"supported": supported, "reason": reason},
            "edit": {
                "supported": supported,
                "reason": _edit_reason(supported, reason),
            },
            "preview": {
                "supported": supported,
                "reason": _preview_reason(supported, reason),
            },
            "components": components,
            "componentReasons": component_reasons,
            "component": component_fact,
            "grain": grain,
        },
        "dataSource": _data_source_facts(None),
        "alternatives": list(generation[2]),
        "clarification": None,
        "mustState": _must_state(outcome, reason, None),
    }


# ---------- 缺上下文：给确定范围 + 澄清 ----------

def _describe_scope(
    *,
    project_id: str | None,
    subject_ref: str | None,
    subject_id: str | None,
    grain_unit: str | None,
    component: str | None,
) -> dict[str, Any]:
    question = _question(None, None, grain_unit, component)
    sources = _visible_catalogs(project_id, subject_ref, subject_id)
    metrics = scope_service.filter_visible(
        metric_mock_service.list_metrics(subject_ref),
        project_id=project_id,
        subject_ref=subject_ref,
        subject_id=subject_id,
    )

    source_facts = [_catalog_facts(item) for item in sources]
    grain_support: dict[str, list[str]] = {}
    for item in sources:
        for grain in item.get("supportedGrains") or []:
            grain_support.setdefault(grain, []).append(item["sourceRef"])

    metric_facts = []
    for metric in metrics:
        supported, reason, _alternatives = _generation_support_for_metric(
            metric["metricRef"]
        )
        metric_facts.append(
            {
                "metricRef": metric["metricRef"],
                "label": metric.get("label"),
                "unit": metric.get("unit"),
                "registered": True,
                "generationSupported": supported,
                "reason": reason,
            }
        )

    missing = ["sourceRef", "metricRef"]
    if grain_unit:
        missing = ["sourceRef"]
    if component:
        missing = ["sourceRef"]

    return {
        "outcome": OUTCOME_MISSING_CONTEXT,
        "question": question,
        "catalog": {
            "registered": False,
            "sourceRef": None,
            "description": None,
            "supportedGrains": [],
            "fields": [],
            "hasDeviceDimension": False,
            "timeField": None,
            "missingFields": [],
        },
        "metric": _empty_metric(),
        "execution": {
            "generation": {"supported": True, "reason": None},
            "edit": {"supported": True, "reason": None},
            "preview": {"supported": True, "reason": None},
            "components": {},
            "componentReasons": {},
            "component": {"requested": component, "supported": None, "reason": None},
            "grain": {
                "requested": grain_unit,
                "supported": (
                    grain_unit in grain_support if grain_unit else None
                ),
                "supportedGrains": sorted(grain_support.keys()),
                "reason": None,
            },
        },
        "dataSource": _data_source_facts(None),
        "scopeSummary": {
            "sources": source_facts,
            "grainSupport": grain_support,
            "metrics": metric_facts,
        },
        "alternatives": [],
        "clarification": {
            "question": (
                "你想问哪个数据源或指标的能力？"
                "当前授权范围内可以核对："
                + "、".join(item["sourceRef"] for item in source_facts)
                + "。"
            ),
            "missing": missing,
        },
        "mustState": [
            "当前授权范围内的数据源与粒度范围如上",
            "没有指定对象时不要替用户挑选数据源",
        ],
    }


# ---------- 事实片段 ----------

def _catalog_facts(catalog: dict[str, Any]) -> dict[str, Any]:
    fields = catalog.get("fields") or {}
    return {
        "registered": True,
        "sourceRef": catalog.get("sourceRef"),
        "sourceVersion": catalog.get("sourceVersion"),
        "description": catalog.get("description"),
        "supportedGrains": list(catalog.get("supportedGrains") or []),
        "fields": sorted(fields.keys()),
        "hasDeviceDimension": "deviceId" in fields,
        "timeField": _time_field(fields),
        "missingFields": _missing_fields(fields),
    }


def _metric_facts(metric: dict[str, Any]) -> dict[str, Any]:
    return {
        "registered": True,
        "metricRef": metric.get("metricRef"),
        "label": metric.get("label"),
        "unit": metric.get("unit"),
        "fieldRef": metric.get("fieldRef"),
        "sourcesProvidingField": [],
    }


def _time_field(fields: dict[str, Any]) -> str | None:
    """这个数据源的时间字段叫什么。

    优先 ``timestamp``，其次 ``logicalDate``，再按语义类型找。
    """
    if "timestamp" in fields:
        return "timestamp"
    if "logicalDate" in fields:
        return "logicalDate"
    for name, spec in fields.items():
        if isinstance(spec, dict) and spec.get("semanticType") == "TIMESTAMP":
            return name
    for name, spec in fields.items():
        if isinstance(spec, dict) and str(spec.get("dataType") or "").upper() == "DATE":
            return name
    return None


def _missing_fields(fields: dict[str, Any]) -> list[str]:
    """相对默认机房能耗源缺哪些字段 —— 用来披露维度 / 时间字段限制。"""
    reference = _default_catalog().get("fields") or {}
    return sorted(set(reference.keys()) - set(fields.keys()))


def _default_catalog() -> dict[str, Any]:
    """默认机房能耗数据源：字段基准的参照物。"""
    from app.data.machine_room_energy import CATALOG_MACHINE_ROOM_ENERGY

    return CATALOG_MACHINE_ROOM_ENERGY


# ---------- 区块支持 ----------

def _component_support(
    catalog: dict[str, Any],
) -> tuple[dict[str, bool], dict[str, str]]:
    """每个报表区块在当前数据源上能不能出数。

    白名单取自生成链路的 ``COMPONENT_KEYS``；能否出数看数据源有没有
    支撑它的字段（设备维度 / 时间字段 / 可聚合数值字段）。
    """
    fields = catalog.get("fields") or {}
    supported: dict[str, bool] = {}
    reasons: dict[str, str] = {}
    for key in report_draft_service.COMPONENT_KEYS:
        ok, reason = _component_supported(key, fields)
        supported[key] = ok
        if reason:
            reasons[key] = reason
    return supported, reasons


def _component_supported(key: str, fields: dict[str, Any]) -> tuple[bool, str | None]:
    if key in _DEVICE_DIMENSION_COMPONENTS and "deviceId" not in fields:
        return False, "该数据源没有设备维度，无法按设备排名，也出不了设备明细表。"
    if key in _TIME_DIMENSION_COMPONENTS and _time_field(fields) is None:
        return False, "该数据源没有时间字段，无法生成趋势。"
    if key in _VALUE_FIELD_COMPONENTS and not _has_value_field(fields):
        return False, "该数据源没有可聚合的数值字段，无法出指标卡。"
    return True, None


def _has_value_field(fields: dict[str, Any]) -> bool:
    for name, spec in fields.items():
        if name == "energyKwh":
            return True
        if isinstance(spec, dict) and spec.get("semanticType") == "INTERVAL_ENERGY":
            return True
    return False


def _component_fact(
    component_key: str | None,
    components: dict[str, bool],
    component_reasons: dict[str, str],
    raw_component: str | None,
) -> dict[str, Any]:
    """用户点名的那个区块，当前能不能出数。"""
    if not raw_component:
        return {"requested": None, "supported": None, "reason": None}
    if component_key is None:
        return {
            "requested": raw_component,
            "supported": False,
            "reason": (
                f"当前链路不支持这个区块，可用的区块是："
                f"{'、'.join(report_draft_service.COMPONENT_KEYS)}。"
            ),
        }
    supported = components.get(component_key, False)
    return {
        "requested": component_key,
        "supported": supported,
        "reason": component_reasons.get(component_key),
    }


def _normalize_component(component: str | None) -> str | None:
    """区块键归一：用生成链路自己的别名表，避免维护第二份。"""
    if not component:
        return None
    return report_draft_service._normalize_component(component)


# ---------- 粒度与指标的执行支持 ----------

def _grain_facts(catalog: dict[str, Any], grain_unit: str | None) -> dict[str, Any]:
    """粒度支持情况：直接问生成链路的粒度规则。"""
    supported_grains = list(catalog.get("supportedGrains") or [])
    if not grain_unit:
        return {
            "requested": None,
            "supported": None,
            "supportedGrains": supported_grains,
            "reason": None,
            "alternatives": [],
        }
    try:
        report_draft_service._resolve_grain(catalog, grain_unit)
    except report_draft_service.DefinitionBuildError as exc:
        return {
            "requested": grain_unit,
            "supported": False,
            "supportedGrains": supported_grains,
            "reason": exc.message,
            "alternatives": list(exc.alternatives),
        }
    return {
        "requested": grain_unit,
        "supported": True,
        "supportedGrains": supported_grains,
        "reason": None,
        "alternatives": [],
    }


def _generation_support_for_metric(
    metric_ref: str,
) -> tuple[bool, str | None, list[str]]:
    """指标能不能出数：直接问生成链路的指标规则。

    返回 ``(supported, reason, alternatives)``；
    ``reason`` / ``alternatives`` 来自链路自己的阻断异常，
    所以和真正生成时报的错是同一份依据。
    """
    try:
        report_draft_service._resolve_metrics([metric_ref])
    except report_draft_service.DefinitionBuildError as exc:
        return False, exc.message, list(exc.alternatives)
    return True, None, []


def _generation_verdict(
    *,
    catalog: dict[str, Any],
    components: dict[str, bool],
    component_reasons: dict[str, str],
    component_key: str | None,
    grain: dict[str, Any],
    metric_generation: tuple[bool, str | None, list[str]],
) -> tuple[bool, str | None]:
    """综合判断：这条能力当前跑不跑得起来。"""
    if metric_generation[0] is False:
        return False, metric_generation[1]
    if grain.get("supported") is False:
        return False, grain.get("reason")
    if component_key is not None and components.get(component_key) is False:
        return False, component_reasons.get(component_key)
    unsupported = [key for key, ok in components.items() if not ok]
    if unsupported:
        return False, (
            f"当前生成模板包含的区块「{'、'.join(unsupported)}」"
            f"在数据源 {catalog.get('sourceRef')} 上出不了数。"
        )
    return True, None


def _collect_alternatives(
    *,
    grain: dict[str, Any],
    metric_generation: tuple[bool, str | None, list[str]],
    components: dict[str, bool],
    component_reasons: dict[str, str],
    component_key: str | None,
) -> list[str]:
    """替代方案必须来自执行规则本身，不能随口编。"""
    alternatives: list[str] = []
    for item in metric_generation[2]:
        if item not in alternatives:
            alternatives.append(item)
    for item in grain.get("alternatives") or []:
        if item not in alternatives:
            alternatives.append(item)
    unsupported = [
        key
        for key, ok in components.items()
        if not ok and key != component_key
    ]
    for key in unsupported:
        item = f"去掉「{key}」区块（{component_reasons.get(key) or '当前数据源支撑不了'}）"
        if item not in alternatives:
            alternatives.append(item)
    return alternatives


# ---------- 数据来源：模拟数据 vs 生产接通 ----------

def _data_source_facts(source_ref: str | None) -> dict[str, Any]:
    """预览数值的来源。

    模式从实现上判断，不写死：预览层现在直接调用模拟数据构建器，
    所以是模拟业务数据；哪天换成生产取数，这里自然跟着变。
    """
    mode = preview_data_mode()
    note = (
        "预览链路的数值来自确定性模拟业务数据，尚未接通真实 EAS / Java 生产取数。"
    )
    if source_ref:
        note = (
            f"数据源 {source_ref} 的预览数值同样来自模拟业务数据，"
            "不代表该源已接通生产取数。"
        )
    return {
        "mode": mode,
        "productionConnected": mode == "PRODUCTION",
        "note": note,
    }


def preview_data_mode() -> str:
    """预览链路的取数模式：``SIMULATED`` 或 ``PRODUCTION``。"""
    from app.services import mock_data_service
    from app.services import preview_service

    builder = getattr(preview_service, "build_snapshot", None)
    if builder is None:
        return "SIMULATED"
    return "SIMULATED" if builder is mock_data_service.build_snapshot else "PRODUCTION"


# ---------- 边界结果 ----------

def _not_registered_source(
    *,
    question: dict[str, Any],
    source_ref: str,
    project_id: str | None,
    subject_ref: str | None,
    subject_id: str | None,
) -> dict[str, Any]:
    """授权目录里确实没有这个数据源。

    替代项用链路自己的阻断原因，但只保留**当前账号可见**的那些 ——
    不能把看不到的数据源推荐出去。
    """
    try:
        report_draft_service._resolve_source(source_ref)
    except report_draft_service.DefinitionBuildError as exc:
        raw_alternatives = list(exc.alternatives)
        reason = exc.message
    else:  # pragma: no cover - 目录里有却解析失败属于异常分支
        raw_alternatives = []
        reason = f"数据源 {source_ref} 不在已登记的目录里。"

    visible_refs = {
        item["sourceRef"]
        for item in _visible_catalogs(project_id, subject_ref, subject_id)
    }
    alternatives = [
        item for item in raw_alternatives if _alt_source_ref(item) in visible_refs
    ]

    return {
        "outcome": OUTCOME_NOT_REGISTERED,
        "question": question,
        "catalog": _empty_catalog(),
        "metric": _empty_metric(),
        "execution": _empty_execution(),
        "dataSource": _data_source_facts(None),
        "alternatives": alternatives,
        "clarification": None,
        "mustState": [
            "当前授权范围的数据目录里没有这个数据源",
            "不要把「查不到」说成「数据不存在」",
            reason,
        ],
    }


def _not_registered_metric(
    question: dict[str, Any], metric_ref: str
) -> dict[str, Any]:
    return {
        "outcome": OUTCOME_NOT_REGISTERED,
        "question": question,
        "catalog": _empty_catalog(),
        "metric": _empty_metric(metric_ref),
        "execution": _empty_execution(),
        "dataSource": _data_source_facts(None),
        "alternatives": [],
        "clarification": None,
        "mustState": [
            f"指标 {metric_ref} 没有在指标目录里登记",
            "不要把「查不到」说成「数据不存在」",
        ],
    }


def _not_authorized(
    question: dict[str, Any], *, kind: str, ref: str
) -> dict[str, Any]:
    """无权限。

    只说明边界，不返回该对象的粒度、字段、口径 —— 越权查询不泄露细节。
    """
    label = "数据源" if kind == "source" else "指标"
    return {
        "outcome": OUTCOME_NOT_AUTHORIZED,
        "question": question,
        "catalog": _empty_catalog(),
        "metric": _empty_metric(),
        "execution": _empty_execution(),
        "dataSource": _data_source_facts(None),
        "alternatives": [],
        "clarification": None,
        "mustState": [
            f"当前账号没有这个{label}的访问权限",
            "不要泄露未授权对象的能力细节，也不要说成「不存在」",
        ],
    }


def _empty_metric(metric_ref: str | None = None) -> dict[str, Any]:
    """指标块的结构保持一致；查不到 / 无权限时字段一律为空，不泄露细节。"""
    return {
        "registered": False,
        "metricRef": metric_ref,
        "label": None,
        "unit": None,
        "fieldRef": None,
        "sourcesProvidingField": [],
    }


def _empty_catalog() -> dict[str, Any]:
    return {
        "registered": False,
        "sourceRef": None,
        "description": None,
        "supportedGrains": [],
        "fields": [],
        "hasDeviceDimension": False,
        "timeField": None,
        "missingFields": [],
    }


def _empty_execution() -> dict[str, Any]:
    return {
        "generation": {"supported": False, "reason": None},
        "edit": {"supported": False, "reason": None},
        "preview": {"supported": False, "reason": None},
        "components": {},
        "componentReasons": {},
        "component": {"requested": None, "supported": None, "reason": None},
        "grain": {
            "requested": None,
            "supported": None,
            "supportedGrains": [],
            "reason": None,
            "alternatives": [],
        },
    }


# ---------- 小工具 ----------

def _question(
    source_ref: str | None,
    metric_ref: str | None,
    grain_unit: str | None,
    component: str | None,
) -> dict[str, Any]:
    return {
        "sourceRef": source_ref,
        "metricRef": metric_ref,
        "grainUnit": grain_unit,
        "component": component,
    }


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None


def _visible(
    item: dict[str, Any],
    project_id: str | None,
    subject_ref: str | None,
    subject_id: str | None,
) -> bool:
    return scope_service.is_item_visible(
        item,
        project_id=project_id,
        subject_ref=subject_ref,
        subject_id=subject_id,
    )


def _visible_catalogs(
    project_id: str | None,
    subject_ref: str | None,
    subject_id: str | None,
) -> list[dict[str, Any]]:
    return scope_service.filter_visible(
        catalog_mock_service.list_catalogs(),
        project_id=project_id,
        subject_ref=subject_ref,
        subject_id=subject_id,
    )


def _alt_source_ref(alternative: str) -> str:
    """从阻断原因给的替代文本里取出数据源 ref。"""
    return str(alternative).split("（", 1)[0].strip()


def _edit_reason(supported: bool, reason: str | None) -> str | None:
    if supported:
        return None
    return (
        f"修改链路只能在当前链路跑得通的范围里改：{reason}"
        if reason
        else "修改链路不支持这个变更。"
    )


def _preview_reason(supported: bool, reason: str | None) -> str | None:
    if supported:
        return "预览链路可以渲染该结果，数值来自模拟业务数据。"
    return reason


def _must_state(
    outcome: str,
    reason: str | None,
    catalog_facts: dict[str, Any] | None,
) -> list[str]:
    """答案必须说清的事实点（不是固定话术模板）。"""
    if outcome == OUTCOME_REGISTERED_NOT_SUPPORTED:
        return [
            "该对象在目录里已登记",
            "但当前产品链路不支持它，不能承诺生成成功",
            reason or "",
        ]
    if outcome == OUTCOME_OK and catalog_facts:
        return [
            "当前产品链路支持该能力",
            "预览数值来自模拟业务数据，不代表已接通生产取数",
        ]
    return []
