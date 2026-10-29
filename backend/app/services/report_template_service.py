"""从**已确认的文字方案**生成模板设计（规格 #18 / 工单 02）。

用户口径（需求访谈 Q13）：

    「先别加了吧，数据库里有啥数据就展示啥，如果没有的就按 Q11 和 Q12 的方案来。」

所以这一步**不是**「一律出占位」，而是：

    1. 把方案的每个板块变成一个可视区域；
    2. 逐区域核对**当前已接通的能力**（目录 + 指标 + 授权范围）；
    3. 接得到的区域写明**绑定了哪个指标**、**用哪个时间窗**，
       并标明数据来源性质；
    4. 接不到的区域保留占位，写明原因（数据待连接 / 口径待核实 /
       能力待接通）。

同时守住两条边界：

    - **设计自由度不被缺数据拦住**（Q2：「先自由发挥，不要先确定数据库里
      有没有数据被拦截了」）—— 没有数据也要出模板，只是那一块是占位；
    - **绝不编造真实业务数值或计算口径**（Q11）—— 草稿阶段显示的是系统
      按固定种子生成的**模拟业务数据**，不是真实数据；PUE 这类尚未接通的
      指标只标「能力待接通」，**不编造公式**。

**取数不在这一层发生**：模板阶段只做能力核对，**不读业务数据、不算出任何
数值**。页面上那一屏数值由草稿阶段的模拟业务数据渲染产生（ADR 0004）——
这一层给的是**绑定描述符**与**定稿栏目**，不含任何业务数值。所以
``dataStatus="bound"`` 现在的意思是**「指标已绑定、能力可用」**，不代表
「已经取到真实数」。

数据来源说明：数值来自确定性生成的模拟业务数据，不是生产能耗事实表。
所以每个绑定到区域的数据来源仍然标明性质 —— 把演示数值当成生产数据，
是比缺数据更严重的问题。
"""
from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from typing import Any, Mapping

from app.schemas.report_plan import COMPONENT_TYPE_VALUES, PlanContractError
from app.schemas.template_design import (
    RegionDataStatus,
    TemplateColumn,
    TemplateDesign,
    TemplateRegion,
)
from app.services.catalog_mock_service import get_catalog
from app.data.machine_room_energy import (
    METRIC_DEVICE_ENERGY,
    METRIC_ENERGY_COST,
    METRIC_PUE,
    METRIC_TOTAL_ENERGY,
)
from app.services.metric_mock_service import get_metric, match_metric_by_alias
from app.services.mock_data_service import resolve_room_name
from app.services.scope_service import authorized_subject_ids, is_subject_authorized


logger = logging.getLogger(__name__)

#: 模板 id 前缀。与方案 id（rplan_）、旧草稿的待确认计划（plan_）都不同。
TEMPLATE_ID_PREFIX = "rtpl_"

#: 默认数据源：机房能耗目录。这是当前唯一有数据的源。
DEFAULT_SOURCE_REF = "catalog.machine_room.energy"

#: 数据来源标识与展示文案。界面必须把文案显示出来。
DATA_SOURCE = "mock_energy"
DATA_SOURCE_LABEL = "模拟数据（能耗 Mock 源，非生产事实表）"

#: 指标口径可核对的底层字段。指标登记了但字段不在数据源里，
#: 就说明当前能力算不出这个数 —— 只能占位，不能编。
_SOURCE = get_catalog(DEFAULT_SOURCE_REF) or {}
_SOURCE_FIELDS = _SOURCE.get("fields") or {}

#: 语义量身份（造数器按它决定造哪一类量）。
#:
#: 身份来自**指标口径**；口径对不上时再按方案的指标意图归一化
#: （见 ``classify_metric_kind``）。**不按区域标题猜**。
METRIC_KIND_BY_REF: dict[str, str] = {
    METRIC_TOTAL_ENERGY["metricRef"]: "energy",
    METRIC_DEVICE_ENERGY["metricRef"]: "energy",
    METRIC_ENERGY_COST["metricRef"]: "cost",
    METRIC_PUE["metricRef"]: "pue",
}

#: 合法的列取值键形态（行对象的键）。
_COLUMN_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: 指标意图的常见措辞 → 指标口径。
#:
#: 刻意**只用于模板生成**：指标目录里的别名是旧流程的口径，
#: 不该被这次改动带着变。这里补的是「方案里模型会怎么写」这一层。
_INTENT_PATTERNS: tuple[tuple[str, str], ...] = (
    # 设备级
    ("设备耗电量", METRIC_DEVICE_ENERGY["metricRef"]),
    ("设备耗电", METRIC_DEVICE_ENERGY["metricRef"]),
    ("设备用电", METRIC_DEVICE_ENERGY["metricRef"]),
    ("设备能耗", METRIC_DEVICE_ENERGY["metricRef"]),
    ("单机耗电", METRIC_DEVICE_ENERGY["metricRef"]),
    ("分设备耗电", METRIC_DEVICE_ENERGY["metricRef"]),
    # 总量级
    ("总耗电量", METRIC_TOTAL_ENERGY["metricRef"]),
    ("总耗电", METRIC_TOTAL_ENERGY["metricRef"]),
    ("总用电", METRIC_TOTAL_ENERGY["metricRef"]),
    ("总能耗", METRIC_TOTAL_ENERGY["metricRef"]),
    ("总电量", METRIC_TOTAL_ENERGY["metricRef"]),
    ("耗电总量", METRIC_TOTAL_ENERGY["metricRef"]),
    ("电量汇总", METRIC_TOTAL_ENERGY["metricRef"]),
    ("整体能耗", METRIC_TOTAL_ENERGY["metricRef"]),
    ("机房能耗", METRIC_TOTAL_ENERGY["metricRef"]),
    ("每日耗电", METRIC_TOTAL_ENERGY["metricRef"]),
    ("耗电量", METRIC_TOTAL_ENERGY["metricRef"]),
    ("用电量", METRIC_TOTAL_ENERGY["metricRef"]),
    # 裸「能耗」：目录别名匹配不到时兜到总量级。
    #
    # 这一条**不会**把 PUE 顶替成能耗：目录别名先匹配，
    # 「PUE」「能耗成本」都先被目录别名接走，走不到这里。
    ("能耗", METRIC_TOTAL_ENERGY["metricRef"]),
    # 成本与 PUE
    ("能耗成本", METRIC_ENERGY_COST["metricRef"]),
    ("电费", METRIC_ENERGY_COST["metricRef"]),
    ("电能使用效率", METRIC_PUE["metricRef"]),
)


@dataclass(frozen=True)
class _Window:
    """报表时间窗。"""

    unit: str
    count: int
    offset: int
    timezone: str

    @property
    def label(self) -> str:
        return f"{self.count} {self.unit}"


def _default_window() -> _Window:
    """默认时间窗：昨日一整个自然日（与旧流程默认一致）。"""
    from app.services.edit_time_resolver import DEFAULT_TIMEZONE

    return _Window(unit="DAY", count=1, offset=-1, timezone=DEFAULT_TIMEZONE)


def _resolve_window(time_range: Any) -> _Window:
    """把方案里的时间范围解析成时间窗。

    优先复用草稿组装层的时间窗解析 —— 同一句「最近 7 天」在两个流程里
    必须落在同一个窗口上，否则用户看到的报表和设计对不上。
    """
    from app.services.report_draft_service import resolve_time_window

    try:
        resolved = resolve_time_window(time_range)
    except Exception:  # noqa: BLE001
        logger.exception("[report_template] 时间窗解析失败")
        resolved = None
    if not isinstance(resolved, dict):
        return _default_window()
    unit = str(resolved.get("unit") or "DAY").upper()
    if unit not in {"DAY", "WEEK", "MONTH"}:
        unit = "DAY"
    count = int(resolved.get("count") or 1)
    offset = resolved.get("offset")
    if offset is None:
        offset = -count
    timezone = str(resolved.get("timezone") or _default_window().timezone)
    return _Window(unit=unit, count=max(1, count), offset=int(offset), timezone=timezone)


def _resolve_room(subject_text: Any, subject_id: Any = None) -> tuple[str | None, str | None]:
    """按方案里的统计对象确定机房。

    返回 ``(room_id, 原因)``。原因非空表示**接不到数** ——
    但那不是拒绝生成模板的理由：区域会转成占位，模板照常出。
    """
    candidates = authorized_subject_ids("machine_room")
    if not candidates:
        return None, "当前账号没有可统计的机房授权范围。"

    # 请求上下文里的对象优先（它是服务端权威值）。
    if subject_id:
        normalized = str(subject_id).strip()
        if is_subject_authorized("machine_room", normalized):
            return normalized, None

    text = str(subject_text or "").strip()
    if not text:
        return candidates[0], None

    for room_id in candidates:
        name = resolve_room_name(room_id)
        if text == room_id or text == name or (text and text in name):
            return room_id, None
    return None, f"当前账号没有「{text}」的授权范围。"


def _match_metric(intents: list[str]) -> tuple[str | None, dict[str, Any] | None, str | None]:
    """按区域里的指标意图匹配指标口径。

    两层匹配：

        1. 指标目录自己的别名（``match_metric_by_alias``）；
        2. 目录别名之外的**常见措辞**（``_INTENT_PATTERNS``）。

    第二层是必要的：方案里的指标意图是模型写的自由措辞（「总能耗」、
    「设备耗电量」），而目录里的别名是给旧流程用的词表（「总耗电」、
    「设备耗电」）。用户说的是同一件事，不能因为用词不同就把一个
    **算得出数的**指标判成「没有对应指标」。

    匹配不到口径不算错误 —— 那个区域会变成占位，并说明
    「当前能力里没有这个指标」。
    """
    for intent in intents:
        ref = match_metric_by_alias(intent)
        if not ref:
            ref = _match_intent_by_pattern(intent)
        if not ref:
            continue
        metric = get_metric(ref)
        if metric is None:
            return None, None, f"指标「{intent}」没有登记，算不出数值。"
        return ref, metric, None
    return None, None, None


def _match_intent_by_pattern(text: str) -> str | None:
    """按常见措辞匹配指标口径。

    取**最长**匹配：「设备耗电总量」归设备级，「总耗电」归总量级。
    """
    normalized = (text or "").lower()
    if not normalized:
        return None
    best: tuple[int, str] | None = None
    for pattern, metric_ref in _INTENT_PATTERNS:
        if pattern.lower() in normalized:
            score = len(pattern)
            if best is None or score > best[0]:
                best = (score, metric_ref)
    return best[1] if best else None


def _metric_is_backed(metric: Mapping[str, Any]) -> tuple[bool, str | None]:
    """这个指标当前算不算得出数。

    指标登记了不等于能出数：还要看它的底层字段在数据源目录里没有。
    PUE 就是这种 —— 登记在册，但数据源没有 ``pue`` 字段，算不出来。
    """
    field_ref = str(metric.get("fieldRef") or "")
    if field_ref and field_ref in _SOURCE_FIELDS:
        return True, None
    label = str(metric.get("label") or metric.get("metricRef") or "该指标")
    return False, f"「{label}」口径待核实：当前数据源没有登记这个字段，算不出数值。"


def classify_metric_kind(*, intent: str, metric_ref: str | None = None) -> str:
    """把方案的指标意图归一化成**语义量身份**。

    身份来自**指标口径**；口径对不上时再按方案的指标意图归一化 ——
    「实时功率」「告警列表」「机柜状态」这些系统还没接通的量，身份是
    **认得出来的**，区域因此能标「能力待接通」，而不是只说「数据待连接」。

    认不出来时给 ``generic``：陌生指标照样有明确标注的示例，
    **不按区域标题猜真实口径**。
    """
    if metric_ref and metric_ref in METRIC_KIND_BY_REF:
        return METRIC_KIND_BY_REF[metric_ref]
    text = (intent or "").strip().lower()
    if not text:
        return "generic"
    if "功率" in text:
        return "power"
    if "告警" in text or "报警" in text:
        return "alarms"
    if ("机柜" in text or "机架" in text or "柜位" in text) and "状态" in text:
        return "cabinet_status"
    if "电费" in text or "成本" in text:
        return "cost"
    if "pue" in text or "电能使用效率" in text:
        return "pue"
    if "能耗" in text or "耗电" in text or "用电" in text or "电量" in text:
        return "energy"
    return "generic"


def _column_key(section_id: str, column_id: str, index: int) -> str:
    """缺键时按板块与栏目确定性生成取值键。"""
    if _COLUMN_KEY_PATTERN.match(column_id):
        return column_id
    safe_section = re.sub(r"[^A-Za-z0-9_]", "_", section_id) or "section"
    return f"{safe_section}_col_{index + 1}"


def _unique_key(key: str, used: set[str]) -> str:
    """取值键不能重复（显示名可以重复，键不行）。"""
    if key not in used:
        return key
    index = 2
    while f"{key}_{index}" in used:
        index += 1
    return f"{key}_{index}"


def _normalize_columns(
    section: Mapping[str, Any], *, section_id: str,
) -> list[TemplateColumn]:
    """把方案板块声明的栏目整理成**定稿列**。

    - 顺序一律按方案声明，**不排序、不按标题猜列**；
    - 缺 ``id`` / ``fieldRef`` 时按板块与栏目确定性补齐；
    - 取值键重复时确定性改键（显示名可以重复，键不行）。
    """
    raw = section.get("columns")
    if not isinstance(raw, (list, tuple)):
        return []
    columns: list[TemplateColumn] = []
    used_keys: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            continue
        column_id = str(item.get("id") or "").strip() or f"col_{index + 1}"
        label = str(item.get("label") or "").strip() or column_id
        field_ref = str(item.get("fieldRef") or "").strip() or _column_key(
            section_id, column_id, index + 1
        )
        field_ref = _unique_key(field_ref, used_keys)
        used_keys.add(field_ref)
        extra = {
            key: value for key, value in item.items()
            if key not in {"id", "label", "fieldRef", "format"}
        }
        columns.append(
            TemplateColumn(
                id=column_id,
                label=label,
                fieldRef=field_ref,
                format=item.get("format") if isinstance(item.get("format"), Mapping) else None,
                **extra,
            )
        )
    return columns


def _text_payload(section: Mapping[str, Any], room_name: str) -> dict[str, Any]:
    """文字块：本来就是说明文字，没有数值。"""
    content = str(section.get("description") or section.get("title") or "")
    return {"rendered": content, "template": content}


def _build_region(
    section: Mapping[str, Any],
    *,
    index: int,
    room_id: str | None,
    room_reason: str | None,
    window: _Window,
) -> TemplateRegion:
    """把方案里的一个板块变成一个模板区域。

    这里**只做能力核对，不取数**：判定指标能不能匹配上、能力支不支持、
    统计对象在不在授权范围内，并把方案声明的**定稿栏目**原样透传下来。

    定义层面不产生任何业务数值 —— 数值由草稿阶段的模拟业务数据渲染产生
    （ADR 0004）。这一层给的是**绑定描述符**与**定稿栏目**。
    """
    section = dict(section or {})
    section_id = str(section.get("id") or f"section_{index + 1}")
    title = str(section.get("title") or f"板块 {index + 1}")
    # 第二道闸（#39）：组件类型**不猜、不兜底**。缺失或非法都硬失败，
    # 并且错误信息要点名是哪个板块 —— 静默补成 metric_card 会让草稿
    # 长成另一个样子（「占位 ≠ 改掉组件类型」）。
    raw_type = section.get("componentType")
    component_type = str(getattr(raw_type, "value", raw_type) or "").strip()
    if component_type not in COMPONENT_TYPE_VALUES:
        raise PlanContractError(
            f"板块 {index + 1}「{title}」的组件类型非法：{component_type or '（缺失）'}；"
            f"合法取值为 {' / '.join(COMPONENT_TYPE_VALUES)}。",
            section_id=section_id, section_title=title, section_index=index,
        )
    intents = [
        str(item).strip()
        for item in (section.get("metricIntents") or [])
        if str(item).strip()
    ]
    # 取数板块必须有指标意图；纯说明板块（text）不强制 —— 它本来就没有指标。
    if component_type != "text" and not intents:
        raise PlanContractError(
            f"板块 {index + 1}「{title}」是取数板块（{component_type}），"
            f"必须声明至少一条指标意图（metricIntents）。",
            section_id=section_id, section_title=title, section_index=index,
        )
    # 方案声明的定稿栏目：顺序与 id/label/fieldRef 原样带下来，
    # 转换层不按标题猜列、不替换列顺序。
    columns = _normalize_columns(section, section_id=section_id)

    def placeholder(
        reason: str,
        metric_refs: list[str] | None = None,
        *,
        capability_pending: bool = False,
    ) -> TemplateRegion:
        return TemplateRegion(
            id=f"region_{index + 1}",
            sectionId=section_id,
            title=title,
            componentType=component_type,
            metricIntents=intents,
            metricRefs=metric_refs or [],
            columns=columns,
            dataStatus=RegionDataStatus.PLACEHOLDER.value,
            placeholderReason=reason,
            # 能力待接通与「授权范围」「绑定三态」相互独立：
            # 指标口径算不出来 → True；统计对象越权 → False。
            capabilityPending=capability_pending,
        )

    if component_type == "text":
        return TemplateRegion(
            id=f"region_{index + 1}",
            sectionId=section_id,
            title=title,
            componentType=component_type,
            metricIntents=intents,
            dataStatus=RegionDataStatus.TEXT.value,
            data=_text_payload(section, resolve_room_name(room_id) if room_id else ""),
        )

    metric_ref, metric, intent_reason = _match_metric(intents)
    if metric_ref is None or metric is None:
        # 目录里没有这个指标 —— 身份认得出来就标「能力待接通」，
        # 认不出来给 generic（照实标，不编口径）。
        return placeholder(
            intent_reason or "数据待连接：当前已接通的能力里没有对应指标。",
            capability_pending=True,
        )

    backed, reason = _metric_is_backed(metric)
    if not backed:
        # 指标登记在册但底层字段没有 —— 系统还算不出这个数（能力待接通）。
        return placeholder(reason or "数据待连接。", [metric_ref], capability_pending=True)

    if room_id is None:
        # 统计对象越权 —— 这是**授权范围**问题，不是能力缺失。
        return placeholder(
            room_reason or "数据待连接：统计对象不在授权范围内。",
            [metric_ref],
            capability_pending=False,
        )

    # 到这里说明：指标匹配上了、能力支持、统计对象也在授权范围内。
    #
    # 但这里**不取真实数**：模板阶段只核对「能力是否可用、指标是否绑定」。
    # 区域带的是**绑定描述符** —— 指标绑定 + 时间窗 + 定稿栏目，
    # 不含任何业务数值。
    metric_label = str((metric or {}).get("label") or title)
    metric_unit = str((metric or {}).get("unit") or "")
    return TemplateRegion(
        id=f"region_{index + 1}",
        sectionId=section_id,
        title=title,
        componentType=component_type,
        metricIntents=intents,
        metricRefs=[metric_ref],
        columns=columns,
        # bound = 指标已绑定、能力可用；**不代表已经取到真实数**。
        dataStatus=RegionDataStatus.BOUND.value,
        data={
            # 取数状态标记：还没取真实数。
            "bindingStatus": "pending_execution",
            # 指标绑定（与 metricRefs 一致，便于转换层直接取用）
            "metricRef": metric_ref,
            # 展示用标签与单位（不含任何数值）
            "label": metric_label,
            "unit": metric_unit,
            # 时间窗：方案时间参数的协议形状，供取数用
            "timeWindow": {
                "type": "CALENDAR",
                "timezone": window.timezone,
                "unit": window.unit,
                "count": window.count,
                "offset": window.offset,
            },
            "windowLabel": window.label,
            # 这个区域的数值**是模拟的**（ADR 0004）：草稿阶段不接真实数，
            # 屏上那一屏由模拟业务数据渲染，接入真实数据后自动替换。
            "pendingReason": "本区域数值为模拟业务数据，接入真实数据后自动替换。",
        },
        # 取数来源与数据性质披露保留：数值就来自这个模拟源
        dataSource=DATA_SOURCE,
        dataSourceLabel=DATA_SOURCE_LABEL,
    )


def build_template_design(
    plan: Mapping[str, Any],
    *,
    subject_id: Any = None,
) -> TemplateDesign:
    """从已确认的文字方案生成模板设计。

    **能力不足 → 占位（不抛）；契约非法 → 硬失败（抛）**：

    - 指标接不上、统计对象越权、时间窗算不出来 → 该区域转成占位并写明原因，
      方案与板块一个都不丢（缺数据不该让整份模板消失）。
    - 组件类型缺失 / 非法、取数板块没有指标意图 → 抛 ``PlanContractError``，
      错误信息点名是哪个板块。这不是「缺数据」，是**方案本身不合法**，
      猜一个类型补上去只会让草稿长成另一个样子（#39）。

    这一步**不取真实数、不算数值**：区域带的是指标绑定与时间窗与**定稿栏目**，
    数值由草稿阶段的模拟业务数据渲染产生（ADR 0004）。
    """
    plan = dict(plan or {})
    requirements = dict(plan.get("requirements") or {})
    sections = list(plan.get("sections") or [])

    topic = str(plan.get("topic") or "报表")
    plan_id = str(plan.get("id") or "")
    plan_revision = int(plan.get("revision") or 1)

    room_id, room_reason = _resolve_room(requirements.get("subject"), subject_id)
    window = _resolve_window(requirements.get("timeRange"))

    regions: list[TemplateRegion] = []
    for index, section in enumerate(sections):
        regions.append(
            _build_region(
                section,
                index=index,
                room_id=room_id,
                room_reason=room_reason,
                window=window,
            )
        )

    if not regions:
        regions = [
            TemplateRegion(
                id="region_1",
                sectionId=None,
                title=topic,
                componentType="text",
                dataStatus=RegionDataStatus.TEXT.value,
                data={"rendered": "方案里没有可生成的板块。"},
            )
        ]

    # 整体状态 = **指标绑定情况**的分布，不再代表「已经取到几个区域的数据」。
    # bound = 所有区域都绑上了指标；partial = 一部分绑上、一部分占位；
    # placeholder = 全占位。
    bound = [r for r in regions if r.dataStatus == RegionDataStatus.BOUND.value]
    placeholders = [
        r for r in regions if r.dataStatus == RegionDataStatus.PLACEHOLDER.value
    ]
    if placeholders and bound:
        overall = "partial"
    elif placeholders:
        overall = "placeholder"
    else:
        overall = "bound"

    pending_notes = [
        f"「{region.title}」{region.placeholderReason}"
        for region in placeholders
        if region.placeholderReason
    ]

    return TemplateDesign(
        id=f"{TEMPLATE_ID_PREFIX}{uuid.uuid4().hex[:12]}",
        revision=1,
        planId=plan_id,
        planRevision=plan_revision,
        title=topic,
        regions=regions,
        layout={
            "columns": 2,
            "items": [
                {"regionId": region.id, "column": index % 2, "row": index // 2}
                for index, region in enumerate(regions)
            ],
        },
        referenceTemplates=list(plan.get("referenceTemplates") or []),
        dataStatus=overall,
        pendingNotes=pending_notes,
    )
