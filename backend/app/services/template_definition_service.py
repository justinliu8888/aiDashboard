"""Deterministic adapter from the stored template to the legacy definition.

真相来源分成两半（GitHub #39）：

    - 板块数量 / 顺序 / 标题 / **组件类型** / **定稿栏目** → **已确认方案**
      ``plan["sections"]``；
    - 指标绑定 / 数据状态 / 占位原因 → **模板区域** ``current.regions``（按
      ``sectionId`` 对齐）。方案里没有「绑到哪个口径」「为什么取不到数」。

所以草稿**长成用户确认的那份方案的样子**：板块一个不多一个不少、顺序一致、
组件类型按方案声明呈现、**表格列按方案声明落位**。指标不支持的板块
**保留方案声明的组件类型与栏目**（+ 占位原因），**不降级成纯文字**。

区域上另外挂一份**纯结构**的示意语义描述（``templateBinding.previewSpec``）：
量身份、单位、列定义与「能力待接通」标记都在里面，**不含任何样例数值** ——
造数器（#50）与示意载荷（#51）按它造数，前端（#53）按它标注。
"""
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from app.data.machine_room_energy import METRIC_DEVICE_ENERGY, METRIC_TOTAL_ENERGY
from app.schemas.report_definition import ReportDefinitionDraft
from app.schemas.report_plan import COMPONENT_TYPE_VALUES, PlanContractError
from app.schemas.template_design import TemplateDesign, TemplateRegion
from app.services.data_source_mode import (
    DEFAULT_SOURCE_MODE,
    SOURCE_MODE_KEY,
    SOURCE_MODE_SIMULATED,
    SOURCE_MODE_USER_JSON,
    record_source_mode,
    resolve_source_mode,
    source_mode_of_region,
)
from app.services.effective_combination import build_combination
from app.services.json_input_service import (
    OP_GROUP_BY,
    OP_PASSTHROUGH,
    OP_RATIO,
    OP_SUM,
    build_field_mapping,
    find_field_key_in_text,
    is_date_field,
    is_summable_field,
    non_summable_reason,
    normalize_field_key,
)
from app.services.report_draft_service import build_definition_draft
from app.services.report_template_service import classify_metric_kind
from app.services.metric_mock_service import get_metric, match_metric_by_alias
from app.services.simulated_field_semantics import (
    BINDING_KIND_CATALOG,
    BINDING_KIND_SIMULATED,
    SCOPE_METRIC_FAMILY,
    describe_simulated_columns,
    deterministic_scope,
    intent_names_registered_metric,
    metric_family_seed_key,
    out_of_catalog_reason,
    resolve_metric_semantics,
    simulated_metric_id,
)


#: 方案组件类型 → 草稿区域形状。
#: 方案声明的组件类型必须**原样**落到草稿里：暂不可取数的板块保留组件类型
#: 与定稿栏目，不降级成纯文字区域（#39 硬要求）。
_PLAN_COMPONENT_SHAPES: dict[str, dict[str, Any]] = {
    "metric_card": {"role": "SUMMARY", "componentType": "METRIC_CARD"},
    "line_chart": {"role": "TREND", "componentType": "CHART", "chartKind": "LINE"},
    "bar_chart": {"role": "DETAIL", "componentType": "CHART", "chartKind": "BAR"},
    "pie_chart": {"role": "DETAIL", "componentType": "CHART", "chartKind": "PIE"},
    "table": {"role": "DETAIL", "componentType": "TABLE"},
    "text": {"role": "DESCRIPTION", "componentType": "TEXT"},
}

#: 方案没声明栏目时，表格区域沿用的既有补齐列（保持既有行为，列不凭空消失）。
#:
#: 这两列就是**能耗趋势表**：列自带单位，载荷与前端不必硬编码 kWh。
_DEFAULT_TABLE_COLUMNS: list[dict[str, Any]] = [
    {"id": "timestamp", "label": "日期", "fieldRef": "timestamp"},
    {
        "id": "energyKwh", "label": "耗电", "fieldRef": "energyKwh",
        "format": {"kind": "NUMBER", "precision": 1, "unitLabel": "kWh"},
    },
]

#: 语义量身份 → 该量的默认单位（指标目录没登记单位时用这个）。
#:
#: 「没有单位」是**明确事实**（告警列表、机柜状态是离散记录；PUE 是无量纲
#: 比值），不是「不知道」—— 载荷层据此决定要不要在图上写单位。
_KIND_DEFAULT_UNITS: dict[str, str] = {
    "energy": "kWh",
    "cost": "元",
    "power": "kW",
    "pue": "",
    "alarms": "",
    "cabinet_status": "",
}

#: 上游没给占位原因时的**兜底文案**（规格 US15：页面上不出现旧口径）。
#:
#: 与 ``report_template_service`` 的说法**完全一致** —— 全仓库只有这一套
#: 口径：能力没接通就说「能力待接通」，不用旧的说法。
_DEFAULT_PENDING_REASON = "能力待接通：当前已接通的能力里没有对应指标。"

#: 指标登记了、但这一块暂时执行不了（数据源没有对应字段）时的占位原因。
#:
#: 这一块**照样出数**（模拟业务数据）—— 构建定义这一层不做真实字段准入，
#: 不能因为「取不到真实值」就让整份定义失败、让用户看不到版式。
_NOT_EXECUTABLE_REASON = (
    "能力待接通：这一块暂时取不到真实数值，当前按模拟业务数据出数，"
    "接入真实数据后自动替换。"
)

#: 定义组件类型 → 方案组件类型（历史定义的只读适配用）。
_DEFINITION_COMPONENT_TYPES: dict[str, str] = {
    "METRIC_CARD": "metric_card",
    "TABLE": "table",
    "TEXT": "text",
}
_DEFINITION_CHART_TYPES: dict[str, str] = {
    "LINE": "line_chart",
    "BAR": "bar_chart",
    "PIE": "pie_chart",
}


def _empty_shell(
    kind: str, *, title: str, reason: str,
    columns: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """占位板块的空壳：保留方案声明的组件类型与**定稿栏目** + 占位原因。

    **不编任何数值** —— 数值由草稿阶段的模拟业务数据渲染产生（ADR 0004），
    而且那张卡片要标「能力待接通」。
    """
    shape = _PLAN_COMPONENT_SHAPES[kind]
    component: dict[str, Any] = {"type": shape["componentType"]}
    if shape["componentType"] == "METRIC_CARD":
        component["metric"] = {
            "id": "", "label": title, "fieldRef": "",
            "format": {"kind": "NUMBER", "precision": 1, "unitLabel": ""},
        }
    elif shape["componentType"] == "CHART":
        component["chartKind"] = shape["chartKind"]
        component["encodings"] = []
    elif shape["componentType"] == "TABLE":
        # 占位表格也保留方案声明的栏目 —— 定稿列不因占位而丢。
        component["columns"] = [dict(column) for column in (columns or [])]
        component["rowNumber"] = True
    elif shape["componentType"] == "TEXT":
        component["content"] = {
            "format": "PLAIN",
            "template": reason or _DEFAULT_PENDING_REASON,
        }
    return {"role": shape["role"], "component": component}


def _declared_columns(
    region: TemplateRegion, section: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """这一块的**定稿列**。

    优先用模板区域带来的（它就是方案透传过来的，已经定稿）；模板是旧的
    没有列时，退回方案板块自己声明的。两处都没有 → 空列表，由调用方按
    既有规则补齐。
    """
    if region.columns:
        return [
            column.model_dump(mode="json", exclude_none=True)
            for column in region.columns
        ]
    raw = section.get("columns")
    if not isinstance(raw, (list, tuple)):
        return []
    return [
        dict(item) for item in raw
        if isinstance(item, Mapping) and str(item.get("label") or "").strip()
    ]


def _slug(text: str) -> str:
    """把任意文本压成可放进标识符的一段。"""
    return "".join(
        char if char.isalnum() else "_" for char in str(text or "")
    ).strip("_").lower() or "x"


def _resolve_unit(catalog: Mapping[str, Any], kind: str) -> str | None:
    """这一量的单位。

    指标目录登记了单位就以目录为准（PUE 登记的是**无量纲**）；目录没登记
    时按语义量身份取默认单位（实时功率 → kW）。实在没有（陌生指标）才给
    ``None`` —— 前端据此**不写单位**，不硬编码 kWh。
    """
    unit = catalog.get("unit")
    if isinstance(unit, str):
        return unit
    return _KIND_DEFAULT_UNITS.get(kind)


def _build_preview_spec(
    *,
    section_id: str,
    role: str,
    component_type: str,
    intents: list[str],
    metric_refs: list[str],
    capability_pending: bool,
    pending_reason: str | None,
    columns: list[dict[str, Any]],
    source_mode: str = DEFAULT_SOURCE_MODE,
    region_id: str = "",
) -> dict[str, Any]:
    """生成区域的**纯结构**示意语义描述。

    形状（契约表 §1 / §3）::

        {
            "role": "SUMMARY" | "TREND" | "DETAIL" | "DESCRIPTION" | ...,
            "kind": "energy" | "cost" | "pue" | "power" | "alarms"
                    | "cabinet_status" | "generic",
            "unit": str | None,
            "sourceMode": "simulated" | "user_json",
            "capabilityPending": bool,
            "clarification": {"question", "candidates"} | None,
            "columns": [ {"id", "label", "fieldRef", "format"?,
                          "semanticKind", "valueType",
                          "deterministicScope"} ],
            "metrics": [
                {"metricKey", "label", "kind", "unit",
                 "capabilityPending", "pendingReason",
                 "bindingKind", "simulatedId", "deterministicScope"}, ...
            ],
        }

    区域 ``kind`` / ``unit`` 表示**主展示量**；多指标身份以 ``metrics``
    为准；区域的能力布尔是各项的**逻辑或**。``columns`` 只在表格板块
    复用定稿列。

    ``role`` 是区域结构上那个角色的**原样透传**（与 ``preview_service``
    产出的载荷同构：``REPORT_TITLE`` 是标题区，``SUMMARY`` / ``TREND`` /
    ``DETAIL`` 是正文区）—— 载荷层组装 ``regions[]`` 时照它带，别自己猜。

    ``unit`` 是这一量的单位：载荷与前端**从这里读**，不硬编码 kWh。
    指标目录登记的单位优先（PUE 登记的是**无量纲**）；目录没登记时按语义量
    身份取默认单位（实时功率 → kW）；实在没有（陌生指标）才是 ``None``。

    表格列的 ``format``（含 ``unitLabel``）**原样透传方案声明**，转换层
    不替它补 —— 混单位的一张表按区域单位乱填会写错单位。方案没声明格式
    时那一列就没有单位标注（不写错的单位）。

    **列与指标的语义描述**（``semanticKind`` / ``valueType`` /
    ``deterministicScope``；指标另有 ``bindingKind`` / ``simulatedId``）是
    **描述驱动**的那一半：造数器按描述造数，不按列名或标题猜值类型
    （工单 #59）。

    这里**不保存任何样例业务结果** —— 只有结构、单位与原因。
    """
    metrics: list[dict[str, Any]] = []
    clarification: dict[str, Any] | None = None
    # A catalog binding may cover only one intent; it cannot replace the confirmed plan.
    plan_driven = source_mode == SOURCE_MODE_SIMULATED and bool(intents)
    if metric_refs and not plan_driven:
        for metric_ref in metric_refs:
            catalog = get_metric(metric_ref) or {}
            label = str(catalog.get("label") or metric_ref)
            kind = classify_metric_kind(intent=label, metric_ref=metric_ref)
            unit = _resolve_unit(catalog, kind)
            metrics.append({
                "metricKey": metric_ref,
                "label": label,
                "kind": kind,
                "unit": unit,
                "capabilityPending": bool(capability_pending),
                "pendingReason": pending_reason if capability_pending else None,
                # 目录内指标：绑的是真实目录里的口径，``simulatedId`` 为空。
                "bindingKind": BINDING_KIND_CATALOG,
                "simulatedId": None,
                "deterministicScope": deterministic_scope(
                    SCOPE_METRIC_FAMILY,
                    metric_family_seed_key(kind, metric_ref, unit or ""),
                ),
            })
    else:
        for index, intent in enumerate(intents):
            metric_ref = match_metric_by_alias(intent) if plan_driven else None
            catalog = get_metric(metric_ref) if metric_ref else None
            if catalog and not intent_names_registered_metric(intent, metric_ref, {metric_ref: catalog}):
                catalog = None
            semantics = resolve_metric_semantics(
                intent=intent,
                registered_kind=classify_metric_kind(intent=intent, metric_ref=metric_ref) if catalog else None,
                registered_unit=str(catalog.get("unit") or "") if catalog else "",
            )
            kind = semantics.kind
            unit = semantics.unit or _KIND_DEFAULT_UNITS.get(kind)
            catalog_binding = bool(catalog and metric_ref in metric_refs)
            metric_key = metric_ref if catalog_binding else f"metric.{kind}.{_slug(section_id)}_{index + 1}"
            metrics.append({
                # 陌生指标也给一个**稳定**的身份键，不靠标题猜。
                "metricKey": metric_key,
                # 用户原话保留：系统不悄悄把「电脑耗电率」改名成别的指标。
                "label": intent,
                "kind": kind,
                "unit": unit,
                "capabilityPending": bool(capability_pending),
                "pendingReason": pending_reason if capability_pending else None,
                # 目录外指标：**不伪造 metricRef**，只给一个稳定的模拟身份。
                "bindingKind": BINDING_KIND_CATALOG if catalog_binding else BINDING_KIND_SIMULATED,
                "simulatedId": None if catalog_binding else simulated_metric_id(section_id, index),
                "deterministicScope": deterministic_scope(
                    SCOPE_METRIC_FAMILY,
                    metric_family_seed_key(kind, metric_key, unit or ""),
                ),
            })
            if index == 0 and semantics.clarification:
                # 含义不清：给出**最关键的语义区别**，并按显式假设出数。
                clarification = semantics.clarification
    primary = metrics[0] if metrics else {}
    if not metrics and capability_pending:
        # 一个指标都认不出来，但这一块确实接不到数 → 给一个**明确标注**的
        # generic 条目，能力标记不能因为没有指标而丢掉。
        metrics.append({
            "metricKey": f"metric.generic.{_slug(section_id)}_1",
            "label": "未识别指标",
            "kind": "generic",
            "unit": None,
            "capabilityPending": True,
            "pendingReason": pending_reason,
            "bindingKind": BINDING_KIND_SIMULATED,
            "simulatedId": simulated_metric_id(section_id, 0),
            "deterministicScope": deterministic_scope(
                SCOPE_METRIC_FAMILY,
                metric_family_seed_key("generic", f"metric.generic.{_slug(section_id)}_1", ""),
            ),
        })
    return {
        "role": role,
        "kind": str(primary.get("kind") or "generic"),
        "unit": primary.get("unit"),
        # 来源模式：服务端记录的事实，贯穿模型、校验、会话恢复与渲染。
        SOURCE_MODE_KEY: source_mode,
        "capabilityPending": any(item["capabilityPending"] for item in metrics),
        # 含义不清的指标：给出澄清选项（目录外指标都**照样出数**，
        # 只是把「口径还没确认」这件事写明）。
        "clarification": clarification,
        "columns": (
            describe_simulated_columns(
                columns, region_id=region_id, section_id=section_id
            )
            if component_type == "table" else []
        ),
        "metrics": metrics,
    }


def _plan_context(time_window: dict[str, Any], grain: str) -> dict[str, Any]:
    """区域级上下文：时间窗与粒度**一律按方案**（取数区与占位区同源）。"""
    return {
        "timeWindow": deepcopy(time_window),
        "grain": {"mode": "REPLACE", "value": {"unit": grain}},
    }


def _plan_grain_unit(plan: Mapping[str, Any] | None) -> str | None:
    """方案里定稿的时间粒度（``requirements.grainUnit``）。

    **方案是粒度的唯一权威**：用户在方案里确认的粒度，转换层不许自己
    再推定一个（那正是「用户说按小时、图上出日点」这种静默收窄的来源）。
    方案没写（历史方案 / 字段缺失）时返回 None，由调用方按既有规则兜底。
    """
    requirements = (plan or {}).get("requirements") or {}
    if not isinstance(requirements, Mapping):
        return None
    raw = requirements.get("grainUnit")
    if raw is None:
        return None
    # 枚举（GrainUnit）取它的值；普通字符串原样用。
    value = getattr(raw, "value", raw)
    unit = str(value).strip().upper()
    return unit or None


def _plan_placement(kind: str, *, index: int) -> dict[str, Any]:
    """按方案顺序与声明的组件类型算出确定性占位（12 列网格）。

    **不读历史模板坐标**（#39 硬要求）：拖拽下线后布局不再从模板继承，
    同一份方案每次转换都得到同一份布局。
    """
    return {
        "type": "GRID", "column": 1, "row": index + 1,
        "columnSpan": 4 if kind == "metric_card" else 12, "rowSpan": 1,
    }


# --------------------------------------------------------------------------
# 用户 JSON 输入的绑定（工单 #64）
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _JsonBinding:
    """当前会话里用户提交的那份 JSON 输入。

    绑定的是**最新一版**（候选或生效都取它）：定义建好、方案确认、
    组合原子提交之后，这一版就是生效输入。提交失败或冲突时
    **旧生效组合原样保留** —— 组合在定义里，不在输入记录里。
    """

    input_id: str
    input_version: int
    field_keys: list[str]
    schema: dict[str, Any]
    rows: list[dict[str, Any]]


def _resolve_json_binding() -> _JsonBinding | None:
    """当前会话有没有用户提交的 JSON 输入。

    会话身份走请求级上下文（与既有工具同一套口径）。
    没有提交过 → ``None``（按模拟示例处理，不改既有行为）。
    """
    try:
        from app.services.session_context import current_session_key
        from app.services.user_json_service import user_json_service
    except Exception:  # noqa: BLE001
        return None
    session_id = current_session_key()
    if not session_id:
        return None
    try:
        record = user_json_service.latest_for_session(session_id)
    except Exception:  # noqa: BLE001
        return None
    if record is None:
        return None
    rows = user_json_service.rows_of(record)
    field_keys = [
        str(item.get("key"))
        for item in (record.schema.get("fields") or [])
        if item.get("key")
    ]
    return _JsonBinding(
        input_id=record.inputId,
        input_version=record.inputVersion,
        field_keys=field_keys,
        schema=record.schema,
        rows=rows,
    )


def _field_types(schema: Mapping[str, Any]) -> dict[str, list[str]]:
    return {
        str(item.get("key")): list(item.get("types") or [])
        for item in (schema.get("fields") or [])
        if item.get("key")
    }


def _json_data_issues(binding: _JsonBinding) -> list[str]:
    """这份数据的**问题清单**（类型混乱、缺失、null）。

    只说结构事实，不带业务数值（ADR 0004 §5.1）。
    """
    issues: list[str] = []
    for item in binding.schema.get("fields") or []:
        key = str(item.get("key") or "")
        if item.get("mixed"):
            issues.append(
                f"字段「{key}」混了多种类型（{'、'.join(item.get('types') or [])}），"
                "已按明细展示并保留原值。"
            )
        if item.get("missingCount"):
            issues.append(
                f"字段「{key}」有 {item.get('missingCount')} 行缺失，"
                "缺失按「（缺失）」标注，不当成 0。"
            )
        if item.get("nullCount"):
            issues.append(
                f"字段「{key}」有 {item.get('nullCount')} 行是 null，"
                "null 与 0 分开标注。"
            )
    return issues


def _numeric_field_keys(binding: _JsonBinding) -> list[str]:
    """输入里**数值型**的字段（按字段结构判定，不按名字猜）。"""
    types = _field_types(binding.schema)
    return [
        key for key in binding.field_keys
        if "number" in (types.get(key) or [])
    ]


def _date_field_keys(binding: _JsonBinding) -> list[str]:
    """输入里**日期型**的字段（按取值能不能解析成时间判定）。"""
    result: list[str] = []
    for key in binding.field_keys:
        values = [row.get(key) for row in binding.rows[:50]]
        if is_date_field(key, values):
            result.append(key)
    return result


def _region_intent_text(
    region: Mapping[str, Any], section: Mapping[str, Any] | None = None
) -> list[str]:
    """这个区域声明的**指标意图**文字（模型的原话）。

    意图里可能原样带着输入字段名（模型被要求这样写），服务端据此
    把指标 / 图表绑到用户的字段上。

    意图有两个来源，都要看：

    - **方案板块的 ``metricIntents``**（模型写的原话）；
    - 定义里区域 ``previewSpec.metrics[].label``（目录内指标这里是目录
      里的显示名，不是模型原话 —— 所以方案侧那份不能漏）。
    """
    texts: list[str] = []
    if isinstance(section, Mapping):
        for intent in section.get("metricIntents") or []:
            text = str(intent or "").strip()
            if text:
                texts.append(text)
    binding = region.get("templateBinding") or {}
    spec = binding.get("previewSpec") or {}
    for metric in spec.get("metrics") or []:
        if isinstance(metric, Mapping):
            label = str(metric.get("label") or "")
            if label:
                texts.append(label)
    name = str(region.get("name") or "")
    if name:
        texts.append(name)
    return texts


def _match_field(
    *,
    labels: list[str],
    binding: _JsonBinding,
    candidates: list[str],
    extra_texts: list[str] | None = None,
) -> str | None:
    """按字段身份在候选字段里找一个匹配的。

    顺序（**从确定到宽松**）：

    1. **归一后相等**：列 id / 显示名 / 编码里的字段名与输入字段名相等；
    2. **原样出现**：输入字段名在列 id / 显示名 / 指标意图里字面出现
       （带词边界）—— 模型把字段名写进来时能认出来；
    3. **角色兜底**：横轴取日期字段、纵轴取数值字段，
       **只在候选唯一时**用（有歧义就不猜）。

    兜底只在这一层做，且只在名字确实对不上时用。
    """
    normalized = {
        normalize_field_key(key): key for key in binding.field_keys
    }
    for label in labels:
        key = normalized.get(normalize_field_key(label))
        if key:
            return key
    for label in list(labels) + list(extra_texts or []):
        key = find_field_key_in_text(label, binding.field_keys)
        if key:
            return key
    if not candidates:
        return None
    return candidates[0] if len(candidates) == 1 else None


def _region_column_targets(
    region: Mapping[str, Any],
) -> list[tuple[str, str, str]]:
    """这个区域要绑定的列：``(columnId, 标签候选, 角色)``。

    角色取值：

    - ``value``  值列（表格的普通列、指标卡的那一个量）；
    - ``x``      趋势图的横轴（日期）；
    - ``y``      趋势图的纵轴（数值）。
    """
    component = region.get("component") or {}
    if not isinstance(component, Mapping):
        return []
    ctype = str(component.get("type") or "").upper()
    targets: list[tuple[str, str, str]] = []
    if ctype == "TABLE":
        for column in component.get("columns") or []:
            if not isinstance(column, Mapping):
                continue
            targets.append(
                (
                    str(column.get("id") or column.get("fieldRef") or ""),
                    str(column.get("label") or ""),
                    "value",
                )
            )
    elif ctype == "METRIC_CARD":
        metric = component.get("metric") or {}
        if isinstance(metric, Mapping):
            targets.append(
                (
                    str(metric.get("fieldRef") or metric.get("id") or ""),
                    str(metric.get("label") or ""),
                    "value",
                )
            )
    elif ctype == "CHART":
        for encoding in component.get("encodings") or []:
            if not isinstance(encoding, Mapping):
                continue
            channel = str(encoding.get("channel") or "").upper()
            role = "x" if channel == "X" else "y" if channel == "Y" else ""
            if not role:
                continue
            targets.append(
                (
                    str(encoding.get("fieldRef") or ""),
                    str(encoding.get("fieldRef") or ""),
                    role,
                )
            )
    return targets


def _field_from_intent(
    intent_texts: Sequence[str],
    binding: _JsonBinding,
    candidates: Sequence[str],
) -> str | None:
    """从**指标意图**里认出输入字段（模型被要求原样写出字段名）。

    认出的字段必须**符合角色**：横轴只认日期字段、纵轴只认数值字段 ——
    把数值当时间用（或者反过来）就是编口径。

    意图里认不出就退回候选集合 —— **只在候选唯一时**用（有歧义不猜）。
    """
    for text in intent_texts:
        key = find_field_key_in_text(text, binding.field_keys)
        if key and key in candidates:
            return key
    if len(candidates) == 1:
        return candidates[0]
    return None


def _apply_json_chart_encodings(
    *,
    regions: list[dict[str, Any]],
    binding: _JsonBinding,
    plan_sections: list[Any],
) -> None:
    """JSON 模式：把图表的坐标轴绑到**用户的字段**上。

    为什么要在这一步处理：目录外指标（占位区）的图表**没有坐标编码**
    （空壳里只有 chartKind），没有编码就没有「横轴 / 纵轴」可绑。

    绑定的判据与字段映射一致（契约表 §3.5）：横轴取**日期字段**、
    纵轴取**指标意图里点名的数值字段**；认不出就不绑 —— 宁可留空，
    也不猜一个轴出来。
    """
    date_keys = _date_field_keys(binding)
    numeric_keys = _numeric_field_keys(binding)
    sections = list(plan_sections or [])
    for index, region in enumerate(regions):
        component = region.get("component") or {}
        if str(component.get("type") or "").upper() != "CHART":
            continue
        section = sections[index] if index < len(sections) else None
        intent_texts = _region_intent_text(region, section)
        x_field = _field_from_intent(intent_texts, binding, date_keys)
        y_field = _field_from_intent(intent_texts, binding, numeric_keys)
        if not x_field or not y_field or x_field == y_field:
            continue
        component["encodings"] = [
            {"channel": "X", "fieldRef": x_field},
            {"channel": "Y", "fieldRef": y_field},
        ]


def _build_json_field_mapping(
    *,
    regions: list[dict[str, Any]],
    binding: _JsonBinding,
    plan_sections: list[Any] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """构造 JSON 模式下的「板块与列 → 输入字段」映射。

    逐项核对：绑定的字段**确实存在于当前输入**（规格第 23 条）。
    绑不上的列不进映射 —— 渲染时降级明细展示并说明，不编值。
    """
    numeric_keys = _numeric_field_keys(binding)
    date_keys = _date_field_keys(binding)
    mapping: list[dict[str, Any]] = []
    unmapped: list[dict[str, Any]] = []
    sections = list(plan_sections or [])
    for index, region in enumerate(regions):
        region_id = str(region.get("id") or "")
        section = sections[index] if index < len(sections) else None
        intent_texts = _region_intent_text(region, section)
        # 同一个字段可以在**不同板块**各绑一次（趋势图的横轴与明细表的
        # 日期列常常是同一个字段）；同一板块内只绑一次。
        used: set[str] = set()
        for column_id, label, role in _region_column_targets(region):
            candidates: list[str] = []
            if role == "x":
                candidates = date_keys
            elif role == "y":
                candidates = numeric_keys
            # 指标意图里的字段名只用来认**值列**：横轴认错了会把数值
            # 当成时间用。
            field_key = _match_field(
                labels=[column_id, label],
                binding=binding,
                candidates=candidates,
                extra_texts=intent_texts if role in ("value", "y") else None,
            )
            if field_key and field_key not in used:
                used.add(field_key)
                mapping.append(
                    {
                        "regionId": region_id,
                        "columnId": column_id,
                        "inputFieldKey": field_key,
                        "inputFieldPath": None,
                    }
                )
                continue
            unmapped.append(
                {
                    "regionId": region_id,
                    "columnId": column_id,
                    "label": label,
                    "reason": (
                        "输入里没有同名字段"
                        if not field_key
                        else "该字段已被其它列绑定"
                    ),
                }
            )
    return mapping, unmapped


def _build_json_processing_rules(
    *,
    regions: list[dict[str, Any]],
    plan_sections: list[Any],
    mapping: list[dict[str, Any]],
    binding: _JsonBinding,
) -> tuple[list[dict[str, Any]], list[str]]:
    """JSON 模式下的**受控处理规则**。

    默认**直接展示输入值**（``PASSTHROUGH``）。求和 / 分组 / 占比只在
    **方案声明了对应展示意图**（用户确认过的那份方案）时才生成，
    并且：温度、PUE、累计电表读数、编号这类字段**不默认求和**
    （规格第 25 条）—— 命中就退回明细展示。

    第二个返回值是**数据问题说明**：命中「不默认求和」时把原因记下来
    （写进 ``metadata.jsonInputIssues.dataIssues``）—— 用户得知道
    「这个字段为什么没被求和」，否则会把第一行的值当成总量。
    """
    types = _field_types(binding.schema)
    rules: list[dict[str, Any]] = []
    reasons: list[str] = []
    for index, region in enumerate(regions):
        region_id = str(region.get("id") or "")
        section = plan_sections[index] if index < len(plan_sections) else {}
        intent = str((section or {}).get("displayIntent") or "")
        region_mappings = [
            item for item in mapping if item.get("regionId") == region_id
        ]
        if not region_mappings:
            continue
        group_key = _first_text_field(
            region_mappings, types, date_keys=_date_field_keys(binding)
        )
        value_key = _first_numeric_field(region_mappings, types)
        for item in region_mappings:
            field_key = str(item.get("inputFieldKey") or "")
            column_id = str(item.get("columnId") or "")
            operation = OP_PASSTHROUGH
            params: dict[str, Any] = {}
            if intent == "total" and field_key == value_key:
                if is_summable_field(field_key):
                    operation = OP_SUM
                else:
                    # 不默认求和：这一条按明细展示，不做汇总 ——
                    # **并把原因写进数据问题说明**（用户要知道为什么）。
                    reason = non_summable_reason(field_key)
                    if reason and reason not in reasons:
                        reasons.append(reason)
            elif (
                intent in ("comparison", "composition")
                and field_key == value_key
                and group_key
                and group_key != field_key
            ):
                operation = OP_GROUP_BY if intent == "comparison" else OP_RATIO
                params = {"groupBy": group_key, "valueField": field_key}
            rules.append(
                {
                    "ruleId": f"rule_{region_id}_{column_id}_{operation}".lower(),
                    "operation": operation,
                    "targetFieldKey": field_key,
                    "params": params,
                }
            )
    return rules, reasons


def _first_text_field(
    mapping: list[dict[str, Any]],
    types: Mapping[str, list[str]],
    *,
    date_keys: Sequence[str] = (),
) -> str | None:
    """分组键：第一个**非数值、非日期**的字段。

    日期字段不当分组键：按时间分组得到的是趋势，不是「各对象对比」。
    """
    for item in mapping:
        key = str(item.get("inputFieldKey") or "")
        if key in set(date_keys):
            continue
        field_types = types.get(key) or []
        if field_types and "number" not in field_types and "boolean" not in field_types:
            return key
    return None


def _first_numeric_field(
    mapping: list[dict[str, Any]], types: Mapping[str, list[str]]
) -> str | None:
    for item in mapping:
        key = str(item.get("inputFieldKey") or "")
        if "number" in (types.get(key) or []):
            return key
    return None


def _align_resample_grain(region: dict[str, Any], grain: str) -> None:
    """把区域管道里重采样节点的粒度对齐到方案粒度。

    区域上下文（``context.grain``）说的是「这个板块按什么粒度画」，
    管道里的 ``RESAMPLE`` 节点说的是「执行层按什么粒度出数」——
    两者必须一致，否则同一份报表两套口径（界面按小时、执行按天）。
    """
    data = region.get("data")
    if not isinstance(data, dict):
        return
    pipeline = data.get("pipeline")
    if not isinstance(pipeline, dict):
        return
    nodes = pipeline.get("nodes")
    if not isinstance(nodes, list):
        return
    for node in nodes:
        if isinstance(node, dict) and node.get("type") == "RESAMPLE":
            node["grain"] = {"unit": grain}


def build_template_definition(
    template: dict[str, Any], plan: dict[str, Any], subject_id: str,
    *, source_mode: Any = None,
) -> ReportDefinitionDraft:
    """把模板设计转换成报表定义草稿。

    ``source_mode`` 是**服务端记录的来源模式**（模拟示例 / 用户 JSON）。
    读取顺序：显式参数 → 方案里记录的值 → 默认口径（模拟示例）。
    判定**不靠标题、板块名或提示词猜**。

    记录下来的模式落在三处（模型、恢复、渲染都读它）：
    定义级 ``metadata.sourceMode``、区域级 ``templateBinding.sourceMode``、
    区域级 ``templateBinding.previewSpec.sourceMode``。
    """
    current = TemplateDesign.model_validate(template)
    # 用户 JSON 输入（工单 #64）：会话里提交过 JSON 就走 JSON 模式。
    # 绑定的是**最新一版**输入记录 —— 组合随定义一起提交，
    # 提交失败 / 冲突时旧生效组合原样保留。
    #
    # 优先级：调用方显式声明的来源模式 → 会话里的 JSON 输入 → 方案里记录的值
    # → 默认口径（模拟示例）。
    declared_mode = resolve_source_mode(
        source_mode or (plan or {}).get(SOURCE_MODE_KEY)
    )
    json_binding = _resolve_json_binding()
    if declared_mode.recorded:
        mode = declared_mode
    elif json_binding is not None:
        mode = resolve_source_mode(SOURCE_MODE_USER_JSON)
    else:
        mode = declared_mode
    window = (plan.get("requirements") or {}).get("timeRange")
    seed = build_definition_draft(current.title, subject_id, timeRange=window,
                                  componentRequests=["total"])
    # 区域级时间窗：一律取**方案时间窗**解析出来的结果（取数区与占位区同源）。
    # 复用草稿组装层的解析结果，而不是在转换层再解析一遍 → 两层口径不可能漂移。
    plan_time_window = deepcopy(seed.regions[1]["context"]["timeWindow"])
    # 粒度：**方案定稿的那一份**（唯一权威）。
    # 转换层不再从草稿组装层另取一个 —— 那样「用户说按周、定义里是按天」
    # 就成了静默收窄（规格：方案、模板、区域定义和窗口解析完整传递，
    # 不在转换或渲染时临时收窄）。方案没写粒度时才回退到组装层的默认。
    plan_grain = _plan_grain_unit(plan) or str(
        (seed.defaults.get("grain") or {}).get("unit") or "DAY"
    )
    # 定义级默认粒度同样跟方案一致：造数器与预览上下文都从 defaults.grain 读它。
    seed.defaults["grain"] = {"unit": plan_grain}
    regions: list[dict[str, Any]] = []
    supported_refs = {METRIC_TOTAL_ENERGY["metricRef"], METRIC_DEVICE_ENERGY["metricRef"]}
    # 主循环按**方案板块**遍历（不是按模板区域）：数量 / 顺序 / 标题 / 组件类型
    # 全部以用户确认的那份方案为准。
    plan_sections = list(plan.get("sections") or [])
    regions_by_section = {r.sectionId: r for r in current.regions if r.sectionId}

    for index, section in enumerate(plan_sections):
        section_id = str(section.get("id") or f"section_{index + 1}")
        title = str(section.get("title") or f"板块 {index + 1}")
        kind = str(section.get("componentType") or "").strip()      # ← 组件类型取方案
        if kind not in COMPONENT_TYPE_VALUES:                        # ← 第三道闸：转换层也硬失败
            raise PlanContractError(
                f"板块 {index + 1}「{title}」的组件类型非法：{kind!r}；"
                f"合法取值为 {' / '.join(COMPONENT_TYPE_VALUES)}。",
                section_id=section_id, section_title=title, section_index=index,
            )
        region = regions_by_section.get(section_id)
        if region is None:
            raise PlanContractError(
                f"板块 {index + 1}「{title}」在模板里没有对应区域，"
                f"草稿无法与方案对齐，请重新生成模板。",
                section_id=section_id, section_title=title, section_index=index,
            )
        # 定稿列：模板区域带来的（方案透传）优先，其次方案板块自己声明的。
        declared_columns = _declared_columns(region, section)
        intents = [
            str(item).strip()
            for item in (section.get("metricIntents") if "metricIntents" in section else region.metricIntents) or []
            if str(item).strip()
        ]
        # 构建定义这一层**不做真实字段准入**：绑不上的指标不是错误 ——
        # 目录外的指标名（工厂里任何新东西）照样产出**合法定义**（模拟绑定），
        # 不丢弃、不报错。这一块照样有示意数据可看。
        executable_binding = (
            region.dataStatus == "bound"
            and len(region.metricRefs) == 1
            and region.metricRefs[0] in supported_refs
        )
        # 绑得上就用真实引用；绑不上（目录外 / 暂时执行不了）用模拟绑定。
        binding_metric_refs = list(region.metricRefs) if (
            region.dataStatus != "bound" or executable_binding
        ) else []
        if region.dataStatus == "bound" and executable_binding:
            if kind == "text":
                # 纯说明板块不绑定指标 —— 模板层不该把它标成 bound。
                raise PlanContractError(
                    f"板块 {index + 1}「{title}」是纯说明板块，不应该有指标绑定。",
                    section_id=section_id, section_title=title, section_index=index,
                )
            device = region.metricRefs[0] == METRIC_DEVICE_ENERGY["metricRef"]
            key = "total" if kind == "metric_card" else "ranking" if device else "trend"
            built = build_definition_draft(current.title, subject_id, timeRange=window,
                                           componentRequests=[key], metricRefs=region.metricRefs)
            converted = deepcopy(built.regions[1])
            # 每个区域显式声明**方案粒度**（bound 区也要）：区域窗口与粒度同源，
            # 不能一个说 7 天、另一个已经是别的粒度。
            converted.setdefault("context", {})["grain"] = {
                "mode": "REPLACE", "value": {"unit": plan_grain}
            }
            # 管道里的重采样粒度跟区域上下文同源：否则执行层按天出数、
            # 界面按小时画点，同一份报表两套口径。
            _align_resample_grain(converted, plan_grain)
            if device:
                # 设备级指标卡：执行绑定指向设备耗电口径（这是**实现层**绑定，
                # 不是展示口径；展示口径一律按方案的时间窗 —— 不收窄到最后一天）。
                if kind == "metric_card":
                    for node in converted["data"]["pipeline"]["nodes"]:
                        if node.get("type") == "METRIC":
                            node["metricRef"] = METRIC_DEVICE_ENERGY["metricRef"]
                            node["metricVersion"] = METRIC_DEVICE_ENERGY["metricVersion"]
            if kind == "table":
                # 表格列**以方案声明为准**：定稿列的 id/label/fieldRef 与顺序
                # 原样落位，不重建硬编码列、不按标题猜列。
                if declared_columns:
                    if device:
                        converted["component"]["columns"] = declared_columns
                    else:
                        converted["component"] = {
                            "type": "TABLE", "columns": declared_columns,
                        }
                elif not device:
                    # 方案没声明栏目 → 沿用既有补齐规则（列不凭空消失）。
                    converted["component"] = {
                        "type": "TABLE", "columns": deepcopy(_DEFAULT_TABLE_COLUMNS),
                    }
            elif kind.endswith("_chart"):
                converted["component"] = {
                    "type": "CHART", "chartKind": kind.split("_")[0].upper(),
                    "encodings": [
                        {"channel": "X", "fieldRef": "deviceName" if device else "timestamp"},
                        {"channel": "Y", "fieldRef": "energyKwh"},
                    ],
                }
            elif kind == "metric_card":
                converted["component"]["metric"]["label"] = title
        elif region.dataStatus == "placeholder" or (
            region.dataStatus == "bound" and not executable_binding
        ):
            # 方案声明的组件类型与**定稿栏目**原样保留：空壳 + 占位原因，
            # 不降级成纯文字区域（#39 硬要求）。**不编任何数值** ——
            # 数值由模拟业务数据渲染，能力待接通的还要标出来。
            #
            # 目录外指标（没有绑上任何口径）的占位原因**按来源模式**重写：
            # 真实目录的限制与「模拟能不能出数」是两件事，两种模式下都不许
            # 把前者说成后者。
            placeholder_reason = region.placeholderReason or _DEFAULT_PENDING_REASON
            if region.dataStatus == "bound":
                # 指标登记了、但这一块暂时执行不了（数据源没有对应字段）：
                # **照样出数**（模拟业务数据），不因此让整份定义失败。
                placeholder_reason = _NOT_EXECUTABLE_REASON
            elif not region.metricRefs and region.capabilityPending:
                placeholder_reason = out_of_catalog_reason(
                    mode.mode, intent=intents[0] if intents else ""
                )
            converted = _empty_shell(
                kind, title=title,
                reason=placeholder_reason,
                columns=declared_columns,
            )
            converted["context"] = _plan_context(plan_time_window, plan_grain)
        elif region.dataStatus == "text":
            text = (region.data or {}).get("template") or (region.data or {}).get("rendered") or title
            converted = {
                "role": "DESCRIPTION", "component": {
                    "type": "TEXT", "content": {
                        "format": "PLAIN",
                        "template": text or _DEFAULT_PENDING_REASON,
                    },
                },
                "context": _plan_context(plan_time_window, plan_grain),
            }
        else:
            raise PlanContractError(
                f"板块 {index + 1}「{title}」的数据状态无法识别：{region.dataStatus!r}。",
                section_id=section_id, section_title=title, section_index=index,
            )
        # 角色取**区域结构上那个**（与 preview_service 产出的载荷同构），
        # 载荷层组装 regions[] 时照它带，不自己猜。
        preview_spec = _build_preview_spec(
            section_id=region.sectionId or section_id,
            role=str(converted.get("role") or ""),
            component_type=kind,
            intents=intents,
            metric_refs=binding_metric_refs,
            capability_pending=bool(region.capabilityPending),
            pending_reason=region.placeholderReason,
            columns=declared_columns,
            source_mode=mode.mode,
            region_id=str(region.id or ""),
        )
        converted.update({
            "id": region.id, "name": title,
            "layout": {"placement": _plan_placement(kind, index=index)},
            "templateBinding": {
                "sectionId": region.sectionId,
                # 绑不上的那一块按**模拟**渲染：状态如实记成占位，
                # 指标引用清空（不假装绑着某个真实口径）。
                "dataStatus": (
                    region.dataStatus
                    if (region.dataStatus != "bound" or executable_binding)
                    else "placeholder"
                ),
                "placeholderReason": (
                    region.placeholderReason
                    if (region.dataStatus != "bound" or executable_binding)
                    else _NOT_EXECUTABLE_REASON
                ),
                "dataSourceLabel": region.dataSourceLabel,
                "metricRefs": list(binding_metric_refs),
                # 来源模式：服务端记录的事实，模型 / 校验 / 会话恢复 / 渲染共用。
                SOURCE_MODE_KEY: mode.mode,
                # bound 区域：指标已绑定、当前显示的是模拟业务数据 → 原因从
                # **模板层** ``data.pendingReason`` 原样透传（#38 产出，#39 取用）。
                # 本票**不另写文案**，全链路只有一处来源。
                "pendingReason": (region.data or {}).get("pendingReason")
                if (region.dataStatus == "bound" and executable_binding) else None,
                # 纯结构的示意语义：量身份、单位、列定义、能力标记。
                # 造数器（#50）与示意载荷（#51）按它造数，不猜口径。
                "previewSpec": preview_spec,
            },
        })
        regions.append(converted)

    if not regions:
        # 兜底（防御分支）：正常方案不可能没有板块（``ReportPlan.sections`` 至少一个）。
        # 与模板层兜底一致 —— 一个 TEXT 说明区，**不拿固定日报冒充方案**。
        fallback = _empty_shell(
            "text", title=current.title, reason="方案里没有可生成的板块。"
        )
        fallback["id"] = "region_1"
        fallback["name"] = current.title
        fallback["context"] = _plan_context(plan_time_window, plan_grain)
        fallback["templateBinding"] = {
            "sectionId": None, "dataStatus": "text",
            "placeholderReason": None, "dataSourceLabel": None,
            "metricRefs": [], "pendingReason": None,
            SOURCE_MODE_KEY: mode.mode,
            "previewSpec": _build_preview_spec(
                section_id="", role=str(fallback.get("role") or ""),
                component_type="text", intents=[], metric_refs=[],
                capability_pending=False, pending_reason=None, columns=[],
                source_mode=mode.mode,
            ),
        }
        regions = [fallback]

    seed.regions = regions
    seed.intentBindings = []
    # 定义级来源模式：会话恢复时按它判断「这份报表的数字从哪来」。
    record_source_mode(seed.metadata, mode.mode)
    seed.metadata["templateBinding"] = {
        "templateId": current.id, "templateRevision": current.revision,
        "planId": current.planId, "planRevision": current.planRevision,
        # 定义身份与版本：给 previewContext 锚定用（#51 建上下文时读这两个）。
        # 身份取**方案 id**（同一份方案的多次确认共用一个定义身份），
        # 版本取**方案 revision**（方案一改就产生新版本，旧上下文保留）。
        "definitionId": current.planId or current.id,
        "definitionVersion": current.planRevision,
    }
    if json_binding is not None:
        # 生效组合（契约表 §2）：来源模式、输入 ID 与版本、字段映射、
        # 处理规则与定义版本**绑成一组**，随定义一起落库。
        _apply_json_chart_encodings(
            regions=regions,
            binding=json_binding,
            plan_sections=list(plan.get("sections") or []),
        )
        mapping, unmapped = _build_json_field_mapping(
            regions=regions,
            binding=json_binding,
            plan_sections=list(plan.get("sections") or []),
        )
        rules, non_summable_notes = _build_json_processing_rules(
            regions=regions,
            plan_sections=list(plan.get("sections") or []),
            mapping=mapping,
            binding=json_binding,
        )
        seed.metadata["effectiveCombination"] = build_combination(
            plan_id=str(current.planId or ""),
            plan_revision=int(current.planRevision or 0),
            definition_id=str(current.planId or current.id),
            definition_version=int(current.planRevision or 0),
            source_mode=mode.mode,
            input_id=json_binding.input_id,
            input_version=json_binding.input_version,
            field_mapping=mapping,
            processing_rules=rules,
        )
        # 绑不上的列：不编值、不丢行 —— 记下来，渲染时降级明细展示并说明。
        # 「不默认求和」的字段也记在这里：用户得知道那个字段为什么没汇总。
        seed.metadata["jsonInputIssues"] = {
            "unmappedColumns": unmapped,
            "dataIssues": _json_data_issues(json_binding) + list(non_summable_notes),
        }
    seed.metadata["pendingNotes"] = list(current.pendingNotes)
    seed.dependencies["metricRefs"] = [
        item for item in seed.dependencies.get("metricRefs", [])
        if item["ref"] in {ref for r in current.regions if r.dataStatus == "bound" for ref in r.metricRefs}
    ]
    return seed


# --------------------------------------------------------------------------
# 历史定义的只读语义适配（#51 读取入口调用）
# --------------------------------------------------------------------------


def _plan_section(
    plan: Mapping[str, Any] | None, section_id: str,
) -> Mapping[str, Any] | None:
    """按板块 id 在方案里找板块。"""
    if not plan or not section_id:
        return None
    for section in (plan.get("sections") or []):
        if isinstance(section, Mapping) and str(section.get("id") or "") == section_id:
            return section
    return None


def _definition_component_type(component: Mapping[str, Any]) -> str:
    """定义里的组件类型 → 方案组件类型（只用于判断「是不是表格」）。"""
    component_type = str(component.get("type") or "").upper()
    if component_type in _DEFINITION_COMPONENT_TYPES:
        return _DEFINITION_COMPONENT_TYPES[component_type]
    if component_type == "CHART":
        chart_kind = str(component.get("chartKind") or "LINE").upper()
        return _DEFINITION_CHART_TYPES.get(chart_kind, "line_chart")
    return ""


def _capability_pending_from_reason(data_status: str, reason: Any) -> bool:
    """历史定义没有 ``capabilityPending`` 标记 —— 按占位原因还原。

    **授权范围**不是能力缺失；口径待核实 / 指标缺失才是「能力待接通」。
    """
    if str(data_status) != "placeholder":
        return False
    text = str(reason or "")
    if "授权范围" in text:
        return False
    return True


def adapt_preview_spec(
    region: Mapping[str, Any], *, plan: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """给**历史定义**补一份纯内存的示意语义描述。

    ADR 0004 之前落库的定义没有 ``templateBinding.previewSpec``。本函数按
    跨工单契约「历史定义缺 previewSpec」的顺序做**只读**适配：

        1. 已有方案的指标意图（传了 ``plan`` 时）；
        2. 既有绑定指标引用 ``templateBinding.metricRefs``；
        3. 既有结构化列信息（``component.columns``）。

    都认不出来时给一个**明确标注**的 ``generic`` 描述并标成能力待接通 ——
    **不按区域标题猜真实口径**，也**不回写**旧方案 / 旧定义。

    已有 ``previewSpec`` 的定义原样返回（本函数不做二次加工）。

    历史定义没有来源模式记录 → 用**默认口径**（模拟示例，ADR 0004），
    并把这一事实写进描述里（不替历史定义编一个来源）。
    """
    binding = region.get("templateBinding") or {}
    if not isinstance(binding, Mapping):
        binding = {}
    existing = binding.get("previewSpec")
    if isinstance(existing, Mapping) and existing:
        return dict(existing)

    mode = source_mode_of_region(region)
    component = region.get("component") or {}
    if not isinstance(component, Mapping):
        component = {}
    section_id = str(binding.get("sectionId") or "")
    plan_section = _plan_section(plan, section_id)
    intents = [
        str(item).strip()
        for item in ((plan_section or {}).get("metricIntents") or [])
        if str(item).strip()
    ]
    metric_refs = [
        str(item).strip()
        for item in (binding.get("metricRefs") or [])
        if str(item).strip()
    ]
    columns = [
        dict(item) for item in (component.get("columns") or [])
        if isinstance(item, Mapping)
    ]
    if not columns and plan_section is not None:
        columns = [
            dict(item) for item in (plan_section.get("columns") or [])
            if isinstance(item, Mapping)
        ]
    reason = binding.get("placeholderReason")
    return _build_preview_spec(
        section_id=section_id,
        role=str(region.get("role") or ""),
        component_type=_definition_component_type(component),
        intents=intents,
        metric_refs=metric_refs,
        capability_pending=_capability_pending_from_reason(
            str(binding.get("dataStatus") or ""), reason,
        ),
        pending_reason=str(reason) if reason else None,
        columns=columns,
        source_mode=mode.mode,
        region_id=str(region.get("id") or ""),
    )
