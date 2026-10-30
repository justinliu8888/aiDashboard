"""用户 JSON 模式的区域载荷装配（工单 #64）。

**用完整已校验数据渲染**（规格第 22 / 43 条）：模型只看到字段结构、
缺失与类型统计和有上限的样本；屏幕上的数字来自**全部**已校验行，
不是那个样本。

三条口径贯穿这一层：

- **null、缺失与 0 分开**：缺失渲染成「（缺失）」，null 渲染成 ``null``，
  0 就是 0 —— 报表不骗人（规格第 34 条）；
- **不编值**：没有日期就不画趋势，没有单位就不写单位，
  无效日期或混合类型降级明细展示并说明数据问题（规格第 24 / 52 条）；
- **默认直接展示输入值**：求和 / 分组 / 占比只在方案声明了对应展示意图
  时才生效，且温度、PUE、累计电表读数、编号**不默认求和**（规格第 25 条）。
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

from app.services.effective_combination import normalize_combination
from app.services.json_input_service import (
    MAX_DISPLAY_ROWS,
    OP_GROUP_BY,
    OP_PASSTHROUGH,
    OP_RATIO,
    OP_SUM,
    PROCESSING_OPERATIONS,
    apply_processing_rules,
    build_rule_id,
    parse_datetime_value,
    render_cell,
)


#: 用户 JSON 模式下的区域级数据源标记（契约表 §1.1：随来源模式走）。
USER_JSON_DATA_SOURCE = "user_json"


def _mapping_by_region(
    combination: Mapping[str, Any],
) -> dict[str, dict[str, str]]:
    """生效组合里的字段映射：``regionId → {columnId: inputFieldKey}``。"""
    result: dict[str, dict[str, str]] = {}
    for item in normalize_combination(combination).get("fieldMapping") or []:
        region_id = str(item.get("regionId") or "")
        column_id = str(item.get("columnId") or "")
        field_key = str(item.get("inputFieldKey") or "")
        if not region_id or not column_id or not field_key:
            continue
        result.setdefault(region_id, {})[column_id] = field_key
    return result


def _rule_region_index(
    mapping: Sequence[Mapping[str, Any]],
) -> dict[str, str]:
    """规则 ID → 区域 ID 的**反查表**。

    规则 ID 的文法是 ``rule_<regionId>_<columnId>_<operation>``
    （见 :func:`build_rule_id`）。**regionId 与 columnId 里都可能带下划线**
    —— 列 id 取自用户输入的字段名（``total_energy`` / ``device_id``），
    所以**不能靠拆字符串猜**两者的边界：拆出来的区域可能根本不存在
    （「幻影区域」），规则会被静默丢掉，汇总口径不生效也没有提示。

    这里改成**显式枚举**：字段映射里每条 ``(regionId, columnId)``
    都能按文法列出全部可能的规则 ID（操作是受控枚举），建成反查表。
    """
    index: dict[str, str] = {}
    for item in mapping:
        region_id = str(item.get("regionId") or "")
        column_id = str(item.get("columnId") or "")
        if not region_id or not column_id:
            continue
        for operation in PROCESSING_OPERATIONS:
            index.setdefault(build_rule_id(region_id, column_id, operation), region_id)
    return index


def _resolve_rule_region(
    rule_id: str,
    index: Mapping[str, str],
    region_ids: Sequence[str],
) -> str | None:
    """这条规则属于哪个区域；认不出来返回 ``None``（**不猜**）。

    1. **显式反查表**：字段映射里 ``(regionId, columnId)`` 按文法列出的
       规则 ID —— 精确命中，不拆字符串。
    2. **兜底**：映射里没有这条规则时，在**已知区域**里找能作为前缀的
       那个（**最长**优先，免得 ``region_1`` 抢走 ``region_10``），
       再确认尾部那个是**受控操作**。
    3. 都认不出来 → ``None``：这条规则**不落到任何区域** ——
       更不会落到「幻影区域」。
    """
    normalized = str(rule_id or "").strip().lower()
    if not normalized:
        return None
    region_id = index.get(normalized)
    if region_id:
        return region_id
    prefix = "rule_"
    if not normalized.startswith(prefix):
        return None
    rest = normalized[len(prefix):]
    for candidate in sorted(region_ids, key=len, reverse=True):
        marker = f"{candidate.strip().lower()}_"
        if not rest.startswith(marker):
            continue
        head = rest[len(marker):]
        for operation in PROCESSING_OPERATIONS:
            if head.endswith(f"_{operation.lower()}"):
                return candidate
    return None


def _rules_by_region(
    combination: Mapping[str, Any],
    *,
    region_ids: Sequence[str] = (),
) -> dict[str, list[dict[str, Any]]]:
    """生效组合里的处理规则：``regionId → 规则列表``。

    规则归属**显式解析**（契约表 §2.1 的处理规则条目里只有 ``ruleId``，
    没有单独的 ``regionId`` 键，所以归属要靠 ``ruleId`` 的文法还原），
    **不靠拆字符串猜** —— 见 :func:`_resolve_rule_region`。
    """
    normalized = normalize_combination(combination)
    mapping = list(normalized.get("fieldMapping") or [])
    index = _rule_region_index(mapping)
    result: dict[str, list[dict[str, Any]]] = {}
    for rule in normalized.get("processingRules") or []:
        region_id = _resolve_rule_region(
            str(rule.get("ruleId") or ""), index, region_ids
        )
        if not region_id:
            # 认不出归属 → 不执行，也**不落到幻影区域**。
            continue
        result.setdefault(region_id, []).append(dict(rule))
    return result


def _declared_unit(region: Mapping[str, Any], column_id: str) -> str:
    """这一列**声明过的**单位。

    只认方案声明的单位（``format.unitLabel``）。**没有单位就不写单位** ——
    不猜 kWh（规格第 35 条）。
    """
    component = region.get("component") or {}
    for column in component.get("columns") or []:
        if not isinstance(column, Mapping):
            continue
        if str(column.get("id") or "") != column_id:
            continue
        fmt = column.get("format") or {}
        return str(fmt.get("unitLabel") or "")
    metric = component.get("metric") or {}
    if isinstance(metric, Mapping):
        if str(metric.get("fieldRef") or metric.get("id") or "") == column_id:
            fmt = metric.get("format") or {}
            return str(fmt.get("unitLabel") or "")
    return ""


def _declared_precision(region: Mapping[str, Any], column_id: str) -> int:
    component = region.get("component") or {}
    for column in component.get("columns") or []:
        if isinstance(column, Mapping) and str(column.get("id") or "") == column_id:
            fmt = column.get("format") or {}
            try:
                return int(fmt.get("precision"))
            except (TypeError, ValueError):
                return 1
    metric = component.get("metric") or {}
    if isinstance(metric, Mapping):
        fmt = metric.get("format") or {}
        try:
            return int(fmt.get("precision"))
        except (TypeError, ValueError):
            return 1
    return 1


def _notes(items: Sequence[str]) -> list[str]:
    return [str(item) for item in items if item]


def _table_payload(
    *,
    region: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
    mapping: Mapping[str, str],
    issues: Sequence[str],
    record_count: int,
) -> dict[str, Any]:
    """明细表载荷：每列都指向一个输入字段。"""
    component = region.get("component") or {}
    columns = [
        dict(column)
        for column in (component.get("columns") or [])
        if isinstance(column, Mapping)
    ]
    if not columns:
        return {
            "columns": [],
            "rowNumber": False,
            "rows": [],
            "dataSource": USER_JSON_DATA_SOURCE,
            "recordCount": record_count,
            "displayedRows": 0,
            "notes": _notes(
                list(issues)
                + ["这个板块没有声明栏目，已按明细展示原始数据。"]
            ),
        }
    displayed = list(rows)[:MAX_DISPLAY_ROWS]
    payload_rows: list[dict[str, Any]] = []
    for row in displayed:
        payload_row: dict[str, Any] = {}
        for column in columns:
            field_ref = str(column.get("fieldRef") or column.get("id") or "")
            column_id = str(column.get("id") or "")
            field_key = mapping.get(column_id)
            if not field_key:
                payload_row[field_ref] = "（未绑定）"
                continue
            present = field_key in row
            payload_row[field_ref] = render_cell(row.get(field_key), present=present)
        payload_rows.append(payload_row)
    notes = list(issues)
    if len(rows) > len(displayed):
        notes.append(
            f"共 {len(rows)} 行，已显示前 {len(displayed)} 行"
            f"（上限 {MAX_DISPLAY_ROWS} 行）；汇总计算仍按全部 {len(rows)} 行。"
        )
    unmapped = [
        column
        for column in columns
        if not mapping.get(str(column.get("id") or ""))
    ]
    if unmapped:
        notes.append(
            "有 "
            + str(len(unmapped))
            + " 个栏目没能绑定到输入字段（"
            + "、".join(str(column.get("label") or column.get("id")) for column in unmapped[:5])
            + "），已标注「（未绑定）」，没有编值。"
        )
    return {
        "columns": columns,
        "rowNumber": bool(component.get("rowNumber", True)),
        "rows": payload_rows,
        "dataSource": USER_JSON_DATA_SOURCE,
        "recordCount": record_count,
        "displayedRows": len(payload_rows),
        "notes": _notes(notes),
    }


def _metric_card_payload(
    *,
    region: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
    mapping: Mapping[str, str],
    rules: Sequence[Mapping[str, Any]],
    issues: Sequence[str],
    record_count: int,
) -> dict[str, Any]:
    """指标卡载荷：值来自受控处理规则的**结果**。"""
    component = region.get("component") or {}
    metric = component.get("metric") or {}
    if not isinstance(metric, Mapping):
        metric = {}
    label = str(metric.get("label") or region.get("name") or "指标")
    column_id = str(metric.get("fieldRef") or metric.get("id") or "")
    field_key = mapping.get(column_id)
    notes = list(issues)
    value: Any = None
    unit = _declared_unit(region, column_id)
    precision = _declared_precision(region, column_id)
    applied = _describe_rules(rules, field_key)
    if not field_key:
        notes.append("这个指标没能绑定到输入字段，没有编值。")
    elif not rows:
        notes.append("输入数据里没有可用记录。")
    else:
        raw = rows[0].get(field_key)
        value = render_cell(raw, present=field_key in rows[0])
        if value is None and field_key not in rows[0]:
            notes.append(f"字段「{field_key}」在数据里缺失。")
    metrics = [
        {
            "metricKey": column_id or field_key or label,
            "label": label,
            "kind": "generic",
            "unit": unit,
            "value": value,
            "precision": precision,
            "capabilityPending": False,
            "pendingReason": None,
        }
    ]
    return {
        "label": label,
        "value": value,
        "unit": unit,
        "precision": precision,
        # 用户数据没有同比口径：不编。
        "yoyPercent": None,
        "comparisonAvailable": False,
        "windowLabel": f"用户提供的 JSON 数据（{record_count} 行）",
        "dataSource": USER_JSON_DATA_SOURCE,
        "metrics": metrics,
        "notes": _notes(notes + ([applied] if applied else [])),
    }


def _describe_rules(
    rules: Sequence[Mapping[str, Any]], field_key: str | None
) -> str:
    """处理规则的人话说明 —— 让用户看得见口径是怎么来的。"""
    for rule in rules:
        operation = str(rule.get("operation") or "")
        if operation == OP_PASSTHROUGH:
            continue
        target = str(rule.get("targetFieldKey") or "")
        if field_key and target != field_key:
            continue
        if operation == OP_SUM:
            return "口径：按求和展示（依据：方案声明的总量口径，用户已确认）。"
        if operation == OP_GROUP_BY:
            params = rule.get("params") or {}
            return (
                "口径：按「"
                + str(params.get("groupBy") or "")
                + "」分组展示（依据：方案声明的比较口径，用户已确认）。"
            )
        if operation == OP_RATIO:
            params = rule.get("params") or {}
            return (
                "口径：按「"
                + str(params.get("valueField") or target)
                + "」占比展示（依据：方案声明的构成口径，用户已确认）。"
            )
    return ""


def _chart_payload(
    *,
    region: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
    mapping: Mapping[str, str],
    issues: Sequence[str],
    record_count: int,
) -> dict[str, Any]:
    """图表载荷。

    **没有日期就不画趋势**：横轴字段取不到、或取到的值解析不成时间，
    就给空点并说明原因（降级明细展示），不编趋势。
    """
    component = region.get("component") or {}
    notes = list(issues)
    # 横纵轴按**区域声明的编码**来（通道 → 字段），再经映射找到输入字段。
    x_column = ""
    y_column = ""
    for encoding in component.get("encodings") or []:
        if not isinstance(encoding, Mapping):
            continue
        channel = str(encoding.get("channel") or "").upper()
        field_ref = str(encoding.get("fieldRef") or "")
        if channel == "X":
            x_column = field_ref
        elif channel == "Y":
            y_column = field_ref
    x_field = mapping.get(x_column, "")
    y_field = mapping.get(y_column, "")
    if not x_field:
        x_field = next(
            (
                key for column_id, key in mapping.items()
                if column_id != y_column
                and ("date" in str(column_id).lower() or "time" in str(column_id).lower())
            ),
            "",
        )
    if not y_field:
        y_field = next(
            (key for column_id, key in mapping.items() if key != x_field),
            "",
        )
    x_labels: list[str] = []
    y_values: list[float] = []
    points: list[dict[str, Any]] = []
    unit = ""
    if not x_field or not y_field:
        notes.append("数据里没有可用的横轴或纵轴字段，已降级明细展示，没有编趋势。")
    else:
        invalid_dates = 0
        for row in rows:
            raw_x = row.get(x_field)
            moment = parse_datetime_value(raw_x)
            if moment is None:
                invalid_dates += 1
                continue
            raw_y = row.get(y_field)
            if not isinstance(raw_y, (int, float)) or isinstance(raw_y, bool):
                continue
            x_labels.append(moment.isoformat())
            y_values.append(float(raw_y))
            points.append({x_field: moment.isoformat(), y_field: float(raw_y)})
        if invalid_dates:
            notes.append(
                f"横轴字段「{x_field}」有 {invalid_dates} 行的日期无法解析，"
                "这些行没有进图，也没有丢掉（明细表里还在）。"
            )
        if not points:
            notes.append("没有可解析的日期与数值组合，已降级明细展示，没有编趋势。")
    return {
        "chartKind": str(component.get("chartKind") or "LINE"),
        "grain": "RAW",
        "xField": x_field,
        "yField": y_field,
        "unit": unit,
        "xLabels": x_labels,
        "yValues": y_values,
        "points": points,
        "dataSource": USER_JSON_DATA_SOURCE,
        "recordCount": record_count,
        "notes": _notes(notes),
    }


def _data_quality_payload(
    *,
    region: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
    issues: Sequence[str],
    record_count: int,
) -> dict[str, Any]:
    """数据质量载荷：用**真实统计**，不编覆盖率。"""
    total_cells = 0
    missing_cells = 0
    for row in rows:
        for value in row.values():
            total_cells += 1
            if value is None:
                missing_cells += 1
    seen: set[str] = set()
    duplicates = 0
    for row in rows:
        key = repr(sorted((str(k), str(v)) for k, v in row.items()))
        if key in seen:
            duplicates += 1
        else:
            seen.add(key)
    coverage = None
    if total_cells:
        coverage = round((total_cells - missing_cells) / total_cells, 4)
    return {
        "coverage": coverage,
        "missing": missing_cells,
        "duplicates": duplicates,
        "freshnessSeconds": None,
        "dataSource": USER_JSON_DATA_SOURCE,
        "notes": _notes(list(issues)),
    }


def _text_payload(
    *, region: Mapping[str, Any], issues: Sequence[str]
) -> dict[str, Any]:
    component = region.get("component") or {}
    content = component.get("content") or {}
    text = str(content.get("template") or region.get("name") or "")
    return {
        "rendered": text,
        "template": text,
        "dataSource": USER_JSON_DATA_SOURCE,
        "notes": _notes(list(issues)),
    }


def build_user_json_payloads(
    *,
    definition: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
    combination: Mapping[str, Any],
    issues: Sequence[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """按生效组合，用**完整已校验数据**生成每个区域的载荷。

    返回 ``{regionId: payload}``，与模拟模式的载荷**同构** ——
    前端不需要为 JSON 模式另写一套渲染。
    """
    normalized = normalize_combination(combination)
    mapping_by_region = _mapping_by_region(normalized)
    region_ids = [
        str(region.get("id") or "")
        for region in definition.get("regions") or []
        if isinstance(region, Mapping) and region.get("id")
    ]
    rules_by_region = _rules_by_region(normalized, region_ids=region_ids)
    shared_issues = [str(item) for item in (issues or []) if item]
    record_count = len(rows)
    result: dict[str, dict[str, Any]] = {}
    for region in definition.get("regions") or []:
        if not isinstance(region, Mapping):
            continue
        region_id = str(region.get("id") or "")
        if not region_id:
            continue
        role = str(region.get("role") or "").upper()
        if role in ("REPORT_TITLE",):
            continue
        component = region.get("component") or {}
        ctype = str(component.get("type") or "").upper()
        mapping = mapping_by_region.get(region_id, {})
        rules = rules_by_region.get(region_id, [])
        # 受控规则只作用在这一块的数据上；未知操作不执行。
        processed = apply_processing_rules(rows, rules)
        if ctype == "TABLE":
            payload = _table_payload(
                region=region,
                rows=processed,
                mapping=mapping,
                issues=shared_issues,
                record_count=record_count,
            )
        elif ctype == "METRIC_CARD":
            payload = _metric_card_payload(
                region=region,
                rows=processed,
                mapping=mapping,
                rules=rules,
                issues=shared_issues,
                record_count=record_count,
            )
        elif ctype == "CHART":
            payload = _chart_payload(
                region=region,
                rows=processed,
                mapping=mapping,
                issues=shared_issues,
                record_count=record_count,
            )
        elif ctype == "DATA_QUALITY":
            payload = _data_quality_payload(
                region=region,
                rows=processed,
                issues=shared_issues,
                record_count=record_count,
            )
        elif ctype == "TEXT":
            payload = _text_payload(region=region, issues=shared_issues)
        else:
            payload = {
                "dataSource": USER_JSON_DATA_SOURCE,
                "notes": _notes(
                    list(shared_issues)
                    + [f"组件类型 {ctype} 在用户 JSON 模式下没有对应展示，已留空。"]
                ),
            }
        result[region_id] = payload
    return result
