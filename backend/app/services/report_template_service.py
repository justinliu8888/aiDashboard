"""从**已确认的文字方案**生成模板设计（规格 #18 / 工单 02）。

用户口径（需求访谈 Q13）：

    「先别加了吧，数据库里有啥数据就展示啥，如果没有的就按 Q11 和 Q12 的方案来。」

所以这一步**不是**「一律出占位」，而是：

    1. 把方案的每个板块变成一个可视区域；
    2. 逐区域核对**当前已接通的能力**（目录 + 指标 + 授权范围）；
    3. 接得到的区域写明**绑定了哪个指标**、**用哪个时间窗**，
       并标明数据来源性质；
    4. 接不到的区域保留占位，写明原因（能力待接通 / 口径待核实 /
       授权范围未覆盖）。

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
from app.services.data_source_mode import (
    SourceMode,
    resolve_source_mode,
)
from app.services.metric_mock_service import get_metric, match_metric_by_alias
from app.services.mock_data_service import resolve_room_name
from app.services.scope_service import authorized_subject_ids, is_subject_authorized
from app.services.simulated_field_semantics import (
    KIND_GENERIC,
    intent_names_registered_metric,
    out_of_catalog_reason,
    resolve_metric_semantics,
)


logger = logging.getLogger(__name__)

#: 模板 id 前缀。与方案 id（rplan_）、旧草稿的待确认计划（plan_）都不同。
TEMPLATE_ID_PREFIX = "rtpl_"

#: 默认数据源：机房能耗目录。这是当前唯一有数据的源。
DEFAULT_SOURCE_REF = "catalog.machine_room.energy"

#: 数据来源标识与展示文案。界面必须把文案显示出来。
DATA_SOURCE = "mock_energy"
DATA_SOURCE_LABEL = "模拟数据（能耗 Mock 源，非生产事实表）"

#: 区域 ``pendingReason`` 文案 —— 模拟示例（ADR 0004）。**既有文案，逐字不改**。
PENDING_REASON_SIMULATED = "本区域数值为模拟业务数据，接入真实数据后自动替换。"

#: 区域 ``pendingReason`` 文案 —— 用户 JSON（工单 #66 F）。
#:
#: 用户 JSON 模式下这块区域的数值是**用户自己提供的那份数据**，不是造出来的
#: 模拟数据 —— 说「模拟业务数据」是**事实性错误**；说「接入真实数据后自动
#: 替换」同样是**不存在的事实**（用户的数据不是系统接通的业务库）。
#: 口径与界面一致：**用户提供的数据，未经业务核验**。
PENDING_REASON_USER_JSON = "本区域数值来自用户提供的数据，未经业务核验。"

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

# --------------------------------------------------------------------------
# 占位原因的两类：能力待接通 / 范围授权
# --------------------------------------------------------------------------
#
# 规格 #46 第 246 行：「能力待接通与范围授权、既有绑定三态相互独立；
# 不把所有占位原因都标为能力缺失」。
#
# 所以**占位**不等于**能力缺失**：
#   - 能力待接通 —— 这个指标当前还算不出来（口径没核实 / 目录里没这个指标）；
#   - 范围授权   —— 指标能力是通的，只是当前账号没有这个统计对象的授权。
#
# 两类**分别计数**，不混算。既有绑定（``dataStatus="bound"``）是第三态，
# 它不是占位。

#: 范围授权类占位的**唯一**原因文案来源。
#:
#: 统计对象越权时用它当 ``placeholderReason``；回复正文引用原因时
#: **沿用同一套说法**，不自造第二套。
SCOPE_PLACEHOLDER_REASON = (
    "统计对象不在授权范围内：该指标能力已接通，当前账号没有这个统计对象的授权。"
)

#: 判「是不是范围授权问题」用的关键词（历史结构缺标记时的兜底）。
#:
#: 与前端角标分类用的是同一套词（授权 / 权限 / 越权）—— 关键词稳，
#: 整句文案会变。
_SCOPE_KEYWORDS = ("授权", "权限", "越权")

#: 目录外指标的占位原因前缀。
#:
#: 这句只说**真实能力**还差什么（目录里没有登记这个指标），**不说**
#: 「模拟渲染做不到」—— 模拟模式下这一块照样出数、照样能看版式
#: （工单 #59 的验收：真实目录的限制不得被说成渲染限制）。
OUT_OF_CATALOG_REASON_PREFIX = "能力待接通："


def _reason_is_scope(reason: Any) -> bool:
    """占位原因是不是**范围授权**问题。"""
    text = str(reason or "")
    return any(keyword in text for keyword in _SCOPE_KEYWORDS)


def pending_reason(mode: Any = None) -> str:
    """区域 ``pendingReason`` 的文案（按**来源模式**分别说）。

    - 模拟示例（``simulated``）：既有文案，**逐字不改**（ADR 0004）。
    - 用户 JSON（``user_json``）：用户自己提供的数据，**未经业务核验**；
      不说「模拟业务数据」（不是事实），也不宣称已接入真实业务系统
      （同样不是事实）。

    ``mode`` 可以是 ``SourceMode``，也可以是它的字符串取值。
    """
    raw = mode.mode if isinstance(mode, SourceMode) else mode
    if resolve_source_mode(raw).is_user_json:
        return PENDING_REASON_USER_JSON
    return PENDING_REASON_SIMULATED


def region_data_status(region: Mapping[str, Any]) -> str:
    """区域的 ``dataStatus``。

    两层结构位置不同：模板层的区域把状态挂在区域上，定义层的区域挂在
    ``templateBinding`` 里（同一份语义的两种落点）。
    """
    if not isinstance(region, Mapping):
        return ""
    binding = region.get("templateBinding")
    if isinstance(binding, Mapping):
        status = str(binding.get("dataStatus") or "")
        if status:
            return status
    return str(region.get("dataStatus") or "")


def region_capability_pending(region: Mapping[str, Any]) -> bool:
    """区域是不是「能力待接通」（**不是**「范围授权」）。

    判据按优先级取，**都来自同一份结构**（不在文案层另算一套）：

    1. 定义层 ``templateBinding.previewSpec.capabilityPending``
       —— 区域级布尔，规格定义为各项指标 ``capabilityPending`` 的**逻辑或**；
    2. 模板层区域自身的 ``capabilityPending``；
    3. 历史结构两处都没有标记 → 按占位原因还原（与历史定义适配同一条规则：
       **授权范围不算能力缺失**）。

    统计对象越权时这里是 ``False`` —— 那个指标的能力是通的。
    """
    if not isinstance(region, Mapping):
        return False
    binding = region.get("templateBinding")
    binding = binding if isinstance(binding, Mapping) else {}
    spec = binding.get("previewSpec")
    if isinstance(spec, Mapping) and spec:
        if "capabilityPending" in spec:
            return bool(spec.get("capabilityPending"))
        metrics = spec.get("metrics")
        if isinstance(metrics, list) and metrics:
            # 区域能力布尔 = 各项指标能力布尔的逻辑或。
            return any(
                isinstance(item, Mapping) and item.get("capabilityPending") is True
                for item in metrics
            )
    if "capabilityPending" in region:
        return bool(region.get("capabilityPending"))
    # 历史结构：没有标记 → 按占位原因还原。
    reason = region.get("placeholderReason") or binding.get("placeholderReason")
    return not _reason_is_scope(reason)


def split_placeholder_regions(
    regions: Any,
) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    """把占位区域按原因类别拆开：``(能力待接通类, 范围授权类)``。

    非占位区域（既有绑定 / 纯说明区）不在这两类里 —— 三态相互独立，
    **不混算**。
    """
    capability: list[Mapping[str, Any]] = []
    scope: list[Mapping[str, Any]] = []
    for region in regions or []:
        if not isinstance(region, Mapping):
            continue
        if region_data_status(region) != "placeholder":
            continue
        if region_capability_pending(region):
            capability.append(region)
        else:
            scope.append(region)
    return capability, scope


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
    except Exception as exc:  # noqa: BLE001
        # 异常原文不进日志（#61）：留栈不留原文。
        logger.exception(
            "[report_template] 时间窗解析失败 errorType=%s",
            type(exc).__name__,
            exc_info=(type(exc), None, exc.__traceback__),
        )
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


def _candidate_refs(intent: str) -> list[str]:
    """这个指标意图**可能**命中的已登记指标引用（按优先级）。

    两层匹配（和 ``_match_metric`` 一样）：

        1. 指标目录自己的别名（``match_metric_by_alias``）；
        2. 目录别名之外的**常见措辞**（``_INTENT_PATTERNS``）。

    两层都给出来，是因为**两层都只是子串命中** —— 命中的引用还得过
    「口径对不对得上」那关（``_match_metric`` 里的判定）。
    """
    refs: list[str] = []
    alias_ref = match_metric_by_alias(intent)
    if alias_ref:
        refs.append(alias_ref)
    pattern_ref = _match_intent_by_pattern(intent)
    if pattern_ref and pattern_ref not in refs:
        refs.append(pattern_ref)
    return refs


def _match_metric(intents: list[str]) -> tuple[str | None, dict[str, Any] | None, str | None]:
    """按区域里的指标意图匹配指标口径。

    两层匹配：

        1. 指标目录自己的别名（``match_metric_by_alias``）；
        2. 目录别名之外的**常见措辞**（``_INTENT_PATTERNS``）。

    第二层是必要的：方案里的指标意图是模型写的自由措辞（「总能耗」、
    「设备耗电量」），而目录里的别名是给旧流程用的词表（「总耗电」、
    「设备耗电」）。用户说的是同一件事，不能因为用词不同就把一个
    **算得出数的**指标判成「没有对应指标」。

    **别名命中 ≠ 识别成功**（规格第 6 条 / 契约表 §3.5）：两层的匹配都是
    **子串** ——「设备耗电**率**」包含别名「设备耗电」，但方案要的是**比率**，
    不是 kWh 的耗电量。口径对不上就**不静默映射**到已登记指标：这一条意图
    按**目录外**处理（``metricRefs`` 为空、``bindingKind = "simulated"``），
    由既有那条「匹配不到口径」的出口接住。

    判定用的是**共享语义层**那一份谓词（``intent_names_registered_metric``），
    与编辑入口（``template_edit_engine``）**同一套** —— 两处各写一份必然漂移。

    **一条意图没命中，不许把整块板块的匹配都断掉**：这一条没登记就
    ``continue`` 接着看下一条 —— 一块板块可以有多条指标意图，后面那条
    可能命中的正是已登记指标。少了那一条，这块板块就会**整块**掉进
    「目录外」的兜底里，用户要的那块就白丢了。

    匹配不到口径不算错误 —— 那个区域会变成占位，并说明
    「当前能力里没有这个指标」。
    """
    for intent in intents:
        for ref in _candidate_refs(intent):
            metric = get_metric(ref)
            if metric is None:
                # 这条候选引用没登记 → **继续看下一条**，不因为它把整块
                # 板块的匹配都断掉（后面那条意图可能命中的正是已登记指标）。
                continue
            if not intent_names_registered_metric(intent, ref, {ref: metric}):
                # 口径对不上 → 这一条意图是**目录外指标**，不静默映射到
                # 已登记指标。继续看下一条意图有没有真命中的。
                continue
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


def _metric_is_backed(
    metric: Mapping[str, Any], *, source_mode: Any = None
) -> tuple[bool, str | None]:
    """这个指标当前**真实取数**算不算得出数。

    指标登记了不等于能出数：还要看它的底层字段在数据源目录里没有。
    PUE 就是这种 —— 登记在册，但数据源没有 ``pue`` 字段，真实取数算不出来。

    **这不是准入**：算不出来只标「口径待核实」，这一块照样生成、照样
    在屏上出数（模拟示例 / 用户 JSON）。所以这里的话**不能说「算不出
    数值」** —— 屏上正有数字，那句话会跟用户看到的东西打架；也不能说
    字段名（那是内部口径）。

    说法与**编辑入口**那一侧同源（``simulated_field_semantics`` 的目录外
    文案后半句）：同一句诉求，两个阶段说的是同一件事。
    """
    field_ref = str(metric.get("fieldRef") or "")
    if field_ref and field_ref in _SOURCE_FIELDS:
        return True, None
    label = str(metric.get("label") or metric.get("metricRef") or "该指标")
    return False, (
        f"「{label}」口径待核实：真实数据接通时需要先确认这个指标的口径；"
        f"{_simulated_block_note(source_mode)}。"
    )


def _simulated_block_note(mode: Any = None) -> str:
    """这一块的数值**是哪来的**（按**来源模式**分两句）。

    与 ``simulated_field_semantics.out_of_catalog_note`` 的后半句**同一套
    说法**：真实目录的限制**不得**被说成「模拟渲染做不到」—— 这一块在
    模拟示例下照样出数、照样能看版式（工单 #59）。
    """
    raw = mode.mode if isinstance(mode, SourceMode) else mode
    if resolve_source_mode(raw).is_user_json:
        return "当前这一块按你提供的 JSON 出数"
    return "当前这一块按模拟示例出数（已标注为模拟），不影响先看版式"


def classify_metric_kind(*, intent: str, metric_ref: str | None = None) -> str:
    """把方案的指标意图归一化成**语义量身份**。

    - **已登记指标** → 目录登记的量身份（``METRIC_KIND_BY_REF``），不猜；
    - **目录外指标** → 通用模拟策略（``generic``）或**形状型**身份
      （告警列表 / 机柜状态 / 实时功率 / PUE）。

    目录外指标**不给**能耗量族（``energy`` / ``cost``）：那两个量族是
    「总耗电」「总电费」这类**已登记指标**的量族，共用同一份基础样本 ——
    给了就等于把陌生指标静默映射成「总耗电」（规格决定 6：目录外指标默认
    走通用模拟策略）。

    认不出来时给 ``generic``：陌生指标照样有明确标注的示例，
    **不按区域标题猜真实口径**。
    """
    if metric_ref:
        if metric_ref in METRIC_KIND_BY_REF:
            return METRIC_KIND_BY_REF[metric_ref]
        # 目录里没有这条引用 → 通用模拟策略。**不按单位猜**：
        # 按单位猜就是把陌生指标静默映射成「总耗电」。
        return KIND_GENERIC
    return resolve_metric_semantics(intent=intent).kind


def _column_key(section_id: str, column_id: str, index: int) -> str:
    """缺键时按板块 ID 与栏目 ID 确定性生成取值键。

    形如 ``section_1_col_1`` —— 板块与栏目**两段都在**：不同板块的同名栏目
    不会撞键，键也一眼看得出属于哪块。
    """
    safe_section = re.sub(r"[^A-Za-z0-9_]", "_", section_id) or "section"
    safe_column = re.sub(r"[^A-Za-z0-9_]", "_", column_id)
    if not _COLUMN_KEY_PATTERN.match(safe_column):
        # 栏目 id 不像个标识符（空、纯数字、带非法字符）→ 用序号兜底。
        safe_column = f"col_{index}"
    return f"{safe_section}_{safe_column}"


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
    - 取值键重复时确定性改键（显示名可以重复，键不行）；
    - **只留契约里的键**（``id`` / ``label`` / ``fieldRef`` / ``format``）：
      模型多写的键不进模板设计，也不进落库定义。
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
        columns.append(
            TemplateColumn(
                id=column_id,
                label=label,
                fieldRef=field_ref,
                format=item.get("format") if isinstance(item.get("format"), Mapping) else None,
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
    source_mode: SourceMode,
) -> TemplateRegion:
    """把方案里的一个板块变成一个模板区域。

    这里**只做能力核对，不取数**：判定指标能不能匹配上、能力支不支持、
    统计对象在不在授权范围内，并把方案声明的**定稿栏目**原样透传下来。

    定义层面不产生任何业务数值 —— 数值由草稿阶段的模拟业务数据渲染产生
    （ADR 0004）。这一层给的是**绑定描述符**与**定稿栏目**。

    ``source_mode`` 是**服务端记录的来源模式**（模拟示例 / 用户 JSON）：
    它只影响**说明文案**（真实目录的限制在两种模式下怎么表述，
    以及区域 ``pendingReason`` 说的是「模拟业务数据」还是「用户提供的数据」），
    不放宽授权判定，也不改变「指标绑没绑上」这件事。
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

    metric_ref, metric, _ = _match_metric(intents)
    if metric_ref is None or metric is None:
        # 目录里没有这个指标 —— **模拟模式下这一块照样出数**（通用模拟策略）。
        #
        # 区域仍然是「能力待接通」（真实能力确实还差一个登记），但说明里
        # **不把真实目录的限制说成「模拟渲染做不到」**：模拟示例照常出数，
        # 用户先看到的是版式。
        #
        # 上游那句「算不出数值」也不能直接用 —— 它说的是**真实数据**算不出，
        # 而这一块在模拟模式下是有数的。两件事分开说。
        reason = out_of_catalog_reason(
            source_mode.mode, intent=intents[0] if intents else ""
        )
        return placeholder(
            reason,
            capability_pending=True,
        )

    backed, reason = _metric_is_backed(metric, source_mode=source_mode)
    if not backed:
        # 指标登记在册但真实取数算不出来（能力待接通）。
        #
        # **不拒绝生成**：这一块照样出数（模拟示例 / 用户 JSON），只是标
        # 「口径待核实」。原因文案按来源模式说清数值是哪来的 —— 真实
        # 取数的限制不得被说成「这一块没数」。
        return placeholder(
            reason or f"口径待核实：真实数据接通时需要先确认这个指标的口径；{_simulated_block_note(source_mode)}。",
            [metric_ref],
            capability_pending=True,
        )

    if room_id is None:
        # 统计对象越权 —— 这是**授权范围**问题，不是能力缺失。
        #
        # 原因文案走模块级的**唯一来源**（``SCOPE_PLACEHOLDER_REASON``），
        # 模板层与回复正文引用同一套说法，不各说各的。
        return placeholder(
            room_reason or SCOPE_PLACEHOLDER_REASON,
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
            # 这个区域的数值来源**按来源模式分别说**（ADR 0004 / 工单 #66 F）：
            # - 模拟示例：草稿阶段不接真实数，屏上那一屏由模拟业务数据渲染，
            #   接入真实数据后自动替换（既有文案，**逐字不改**）；
            # - 用户 JSON：数值来自**用户自己提供的那份数据**，未经业务核验
            #   —— 说「模拟业务数据」是说反话。
            "pendingReason": pending_reason(source_mode),
        },
        # 取数来源与数据性质披露保留：数值就来自这个模拟源
        dataSource=DATA_SOURCE,
        dataSourceLabel=DATA_SOURCE_LABEL,
    )


def build_template_design(
    plan: Mapping[str, Any],
    *,
    subject_id: Any = None,
    source_mode: Any = None,
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

    ``source_mode`` 是**服务端记录的来源模式**（模拟示例 / 用户 JSON）。
    不传就用默认口径（模拟示例）；它由调用方（数据输入那条链路）决定，
    **不靠标题、板块名或提示词猜**。
    """
    plan = dict(plan or {})
    requirements = dict(plan.get("requirements") or {})
    sections = list(plan.get("sections") or [])

    topic = str(plan.get("topic") or "报表")
    plan_id = str(plan.get("id") or "")
    plan_revision = int(plan.get("revision") or 1)

    room_id, room_reason = _resolve_room(requirements.get("subject"), subject_id)
    window = _resolve_window(requirements.get("timeRange"))
    mode = resolve_source_mode(source_mode or plan.get("sourceMode"))

    regions: list[TemplateRegion] = []
    for index, section in enumerate(sections):
        regions.append(
            _build_region(
                section,
                index=index,
                room_id=room_id,
                room_reason=room_reason,
                window=window,
                source_mode=mode,
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
