"""模拟字段**描述**：造数器的输入是描述，不是列名（工单 #59）。

**为什么要有描述**

造数器原来靠「把显示名和 ``fieldRef`` 拼起来做子串匹配」判断这一列该出
什么值（``能耗`` → 耗电量、``率`` → 百分比、``状态`` → 运行状态……）。
那是对**标题**的猜测：换个列名值就变了，改个字段名也变了，而且一旦猜错，
屏上的数字就是错的（把占比算成 kWh、把负载率算成台数）。

本模块把这件事倒过来：**先有描述，再造数**。描述是服务端记录的事实，
字段名与取值域一律照**统一开发契约表**
（``docs/contracts/0001-统一开发契约表.md`` §3）::

    {
        "semanticKind": "energy",                          # 语义类别（业务口径）
        "valueType": "energy",                             # 值形态（造数器按它造）
        "deterministicScope": {"scope": "region",
                               "seedKey": "region_1"},      # 确定性上下文
    }

指标侧另有三个字段（契约表 §3.1）：``bindingKind``（``catalog`` / ``simulated``）、
``simulatedId``（目录外指标的稳定模拟身份）、``deterministicScope``。

**描述不覆盖真实数据绑定**：``columns[].id`` 是列身份，
``columns[].fieldRef`` 是取值键（``section_N.col_N``），它们与将来接真数时的
「列 → 数据集字段」映射是两回事（ADR 0004 §7.3）。

**语义与值形态从哪来（优先级从高到低）**

1. ``declared``   —— 列自己**显式声明**了 ``valueType`` / ``semanticKind``；
   只声明 ``semanticKind`` 时按语义类别**推导**对应值形态
   （``energy`` → ``energy``、``cost`` → ``cost``…），不再先判「未声明」；
2. ``unit``       —— 列**声明了单位**（``format.unitLabel``），单位决定值形态；
3. ``identity``   —— 列的**字段身份**命中规范字段目录里**能唯一确定量口径**
   的**具体条目**（``energyKwh`` / ``costYuan`` / ``powerKw`` / ``deviceName``…）；
   **通用量词不算**（``energy`` / ``cost`` / ``power`` / ``kwh`` / ``kw`` /
   ``yuan`` / ``percent`` / ``count`` / ``load`` —— 只说明「是个量」，
   不说明「是哪个量」）；
4. ``vocabulary`` —— 列的**显示名**精确命中产品栏目词表（封闭词表，不是模糊匹配）；
5. ``unresolved`` —— 以上都不成立 → 明确记成「语义未声明」：``semanticKind``
   记 ``generic``、``valueType`` 记 ``text``，**不猜**。

第 3 条查的是**目录**（字段身份 → 值形态），不是拿标题做子串匹配；第 4 条
是**精确命中封闭词表**（产品自己那几套预设栏目），也不是模糊匹配。词表里
没有的名字一律落到第 5 条。老定义没有 ``semanticKind`` / ``valueType`` 时
按 ``generic`` / ``text`` 处理，**不回退到按列名猜**。

**为什么第 3 条要收紧**（契约表 §3.4 ③，v1.18.0）：第 3 条的级序**优先于**
第 4 条 —— 身份一命中就**遮蔽**显示名。于是身份 token 必须**比显示名更具体**
才配得上这个优先级：``col_power`` + 「负载率」按 ``power`` 认会给出
215.85 kW（该是百分比），``col_load`` + 「设备名称」按 ``load`` 认会给出
百分比（该是设备名）。**占位变错数字，比占位更坏。** 只认具体条目是**收紧**，
不是恢复按列名猜。

**两条切分 / 查表的细则（都只做「切」与「查」，不做「猜」）**

- **③ 的末段切分同时支持点号与下划线**：行键有**两套独立生成器**
  （ADR 0004 §7.4）—— 方案层点号 ``section_1.col_1``、转换层补列下划线
  ``section_1_col_1``。两套都要能切出末段去查规范字段目录，否则生产路径
  的取值键永远切不出字段身份（``section_1_col_energy`` 的末段是 ``energy``）。
- **④ 查词表前只剥离「已知单位」的括号标注**：显示名末尾的 ``(kWh)`` /
  ``（%）`` 是**单位标注**（系统另有单位字段记录它），不是口径名的一部分。
  剥掉之后**仍然必须精确命中封闭词表** —— 任何子串匹配都不做。

**指标语义（目录外指标）**

目录外指标**默认走通用模拟策略**（``generic``）：不静默映射到「总耗电」这类
已登记指标，也不共享能耗量的那份基础样本。含义不清时（``电脑耗电率``）
给出**最关键的语义区别**（耗电量？占比？功率？），用户仍要求先做时按
**显式假设**出数，并写明「仅模拟展示、业务口径待确认」。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping


# ---------------------------------------------------------------- 语义类别
#
# 语义类别 = 这一列/这一指标是**哪一类业务量**。取值域与 ``previewSpec.kind``
# 一致（契约表 §3.2）：``generic`` = 认得出来是模拟示例，但认不出业务口径。

SEMANTIC_KIND_ENERGY = "energy"
SEMANTIC_KIND_COST = "cost"
SEMANTIC_KIND_PUE = "pue"
SEMANTIC_KIND_POWER = "power"
SEMANTIC_KIND_ALARMS = "alarms"
SEMANTIC_KIND_CABINET_STATUS = "cabinet_status"
SEMANTIC_KIND_GENERIC = "generic"

#: 语义类别的取值域（固定）。
SEMANTIC_KIND_VALUES = (
    SEMANTIC_KIND_ENERGY,
    SEMANTIC_KIND_COST,
    SEMANTIC_KIND_PUE,
    SEMANTIC_KIND_POWER,
    SEMANTIC_KIND_ALARMS,
    SEMANTIC_KIND_CABINET_STATUS,
    SEMANTIC_KIND_GENERIC,
)

#: 旧别名（量身份 → 语义类别）。同一套取值，只是叫法不同。
KIND_ENERGY = SEMANTIC_KIND_ENERGY
KIND_COST = SEMANTIC_KIND_COST
KIND_PUE = SEMANTIC_KIND_PUE
KIND_POWER = SEMANTIC_KIND_POWER
KIND_ALARMS = SEMANTIC_KIND_ALARMS
KIND_CABINET_STATUS = SEMANTIC_KIND_CABINET_STATUS
KIND_GENERIC = SEMANTIC_KIND_GENERIC


# ---------------------------------------------------------------- 值形态
#
# 值形态 = 这一列的**值长什么样**。造数器只认这一组取值；取值域照抄既有
# ``simulated_data_service._column_value_kind`` 的判定结果集（契约表 §3.2），
# 只是把「按列名猜」换成「按描述读」。

VALUE_TYPE_LEVEL = "level"
VALUE_TYPE_STATUS = "status"
VALUE_TYPE_TIME = "time"
VALUE_TYPE_DEVICE_ID = "device_id"
VALUE_TYPE_DEVICE_TYPE = "device_type"
VALUE_TYPE_DEVICE_NAME = "device_name"
VALUE_TYPE_PUE = "pue"
VALUE_TYPE_POWER = "power"
VALUE_TYPE_COST = "cost"
VALUE_TYPE_ENERGY = "energy"
VALUE_TYPE_PERCENT = "percent"
VALUE_TYPE_COUNT = "count"
VALUE_TYPE_DURATION = "duration"
VALUE_TYPE_TEMPERATURE = "temperature"
VALUE_TYPE_VOLTAGE = "voltage"
VALUE_TYPE_CURRENT = "current"
VALUE_TYPE_DESCRIPTION = "description"
VALUE_TYPE_TEXT = "text"

#: 值形态的取值域（18 个，固定）。
VALUE_TYPE_VALUES = (
    VALUE_TYPE_LEVEL,
    VALUE_TYPE_STATUS,
    VALUE_TYPE_TIME,
    VALUE_TYPE_DEVICE_ID,
    VALUE_TYPE_DEVICE_TYPE,
    VALUE_TYPE_DEVICE_NAME,
    VALUE_TYPE_PUE,
    VALUE_TYPE_POWER,
    VALUE_TYPE_COST,
    VALUE_TYPE_ENERGY,
    VALUE_TYPE_PERCENT,
    VALUE_TYPE_COUNT,
    VALUE_TYPE_DURATION,
    VALUE_TYPE_TEMPERATURE,
    VALUE_TYPE_VOLTAGE,
    VALUE_TYPE_CURRENT,
    VALUE_TYPE_DESCRIPTION,
    VALUE_TYPE_TEXT,
)

#: 值形态 → 语义类别。认得出的量（能耗 / 电费 / PUE / 功率）直接对上；
#: 其余（占比、计数、时长、温度……）**认不出业务口径** → ``generic``。
SEMANTIC_KIND_BY_VALUE_TYPE: dict[str, str] = {
    VALUE_TYPE_ENERGY: SEMANTIC_KIND_ENERGY,
    VALUE_TYPE_COST: SEMANTIC_KIND_COST,
    VALUE_TYPE_PUE: SEMANTIC_KIND_PUE,
    VALUE_TYPE_POWER: SEMANTIC_KIND_POWER,
}

#: 语义类别 → 值形态。**只声明了语义类别**时按它推导值形态
#: （契约表 §3.4 的 ① 级：``columns[].valueType`` / ``columns[].semanticKind``
#: 都算「显式声明」）。
#:
#: 推导依据是**语义类别自己的含义**，不是猜：
#:
#: - 能耗 / 电费 / PUE / 功率：量纲唯一（kWh / 元 / 无量纲 / kW）；
#: - ``alarms`` → ``level``：告警块里唯一有固定取值域的列是**告警级别**；
#: - ``cabinet_status`` → ``status``：机柜状态块的列形态就是状态；
#: - ``generic`` → ``text``：认不出业务口径 → 明确记成文本占位。
VALUE_TYPE_BY_SEMANTIC_KIND: dict[str, str] = {
    SEMANTIC_KIND_ENERGY: VALUE_TYPE_ENERGY,
    SEMANTIC_KIND_COST: VALUE_TYPE_COST,
    SEMANTIC_KIND_PUE: VALUE_TYPE_PUE,
    SEMANTIC_KIND_POWER: VALUE_TYPE_POWER,
    SEMANTIC_KIND_ALARMS: VALUE_TYPE_LEVEL,
    SEMANTIC_KIND_CABINET_STATUS: VALUE_TYPE_STATUS,
    SEMANTIC_KIND_GENERIC: VALUE_TYPE_TEXT,
}


# ---------------------------------------------------------------- 绑定与作用域

#: 指标绑定的两种形态（契约表 §3.1 / §3.5）。
BINDING_KIND_CATALOG = "catalog"
BINDING_KIND_SIMULATED = "simulated"
BINDING_KIND_VALUES = (BINDING_KIND_CATALOG, BINDING_KIND_SIMULATED)

#: 确定性作用域的两种取值（契约表 §3.3）。
SCOPE_REGION = "region"
SCOPE_METRIC_FAMILY = "metric_family"
SCOPE_VALUES = (SCOPE_REGION, SCOPE_METRIC_FAMILY)


# ---------------------------------------------------------------- 规范字段目录
#
# **字段身份** → 值形态。产品自己的数据集字段与默认栏目都落在这些身份上，
# 所以「身份 → 值形态」是一张**目录**，不是猜测。
#
# 目录的准入规则（三条，缺一不可）：
#
# 1. **精确查表**：键就是**字段身份**（列 ``id`` / 取值键 ``fieldRef`` 本身
#    或它的末段），命不中就是命不中 —— 不做子串匹配、不做关键词猜测；
# 2. **不与产品栏目词表打架**：③ 身份级**优先于**④ 词表级，所以一个比
#    词表更**笼统**的身份 token 不能进目录（否则「设备编号」列会因为身份
#    叫 ``col_device`` 而显示成设备**名**）。没有 ``device`` / ``name``
#    这种笼统的实体词 —— 那一类交给④ 封闭词表；
# 3. **通用量词不算「量口径」**（契约表 §3.4 ③，v1.18.0 收紧）：
#    ``energy`` / ``cost`` / ``power`` / ``kwh`` / ``kw`` / ``yuan`` 只说明
#    「是个量」，不说明「是哪个量」—— 按它们认定会把该出百分比 / 设备名的
#    列说成另一种量（例 ``col_power`` + 「负载率」→ power）。**占位变错数字，
#    比占位更坏。** 这一类 token 已从目录里**整个去掉**。
#
# 目录里的每一条都**能唯一确定量口径**（kWh 的耗电量、元的电费、kW 的功率、
# 设备名 / 设备编号 / 告警级别……），所以每一项都能被③ 直接采用。

CANONICAL_FIELD_CATALOG: dict[str, dict[str, str]] = {
    "timestamp": {"valueType": VALUE_TYPE_TIME},
    "time": {"valueType": VALUE_TYPE_TIME},
    "energyKwh": {"valueType": VALUE_TYPE_ENERGY, "unit": "kWh"},
    "costYuan": {"valueType": VALUE_TYPE_COST, "unit": "元"},
    "energyCost": {"valueType": VALUE_TYPE_COST, "unit": "元"},
    "powerKw": {"valueType": VALUE_TYPE_POWER, "unit": "kW"},
    "pueValue": {"valueType": VALUE_TYPE_PUE},
    "pue": {"valueType": VALUE_TYPE_PUE},
    "deviceId": {"valueType": VALUE_TYPE_DEVICE_ID},
    "deviceName": {"valueType": VALUE_TYPE_DEVICE_NAME},
    "deviceType": {"valueType": VALUE_TYPE_DEVICE_TYPE},
    "cabinetName": {"valueType": VALUE_TYPE_DEVICE_NAME},
    "cabinetStatus": {"valueType": VALUE_TYPE_STATUS},
    "level": {"valueType": VALUE_TYPE_LEVEL},
    "status": {"valueType": VALUE_TYPE_STATUS},
    "description": {"valueType": VALUE_TYPE_DESCRIPTION},
    "duration": {"valueType": VALUE_TYPE_DURATION},
    "percent": {"valueType": VALUE_TYPE_PERCENT, "unit": "%"},
    "count": {"valueType": VALUE_TYPE_COUNT},
    "temperature": {"valueType": VALUE_TYPE_TEMPERATURE},
    "voltage": {"valueType": VALUE_TYPE_VOLTAGE},
    "current": {"valueType": VALUE_TYPE_CURRENT},
    # 机柜负载率：产品里这一列的身份就叫 load，量纲是百分比。
    "load": {"valueType": VALUE_TYPE_PERCENT, "unit": "%"},
}


#: ③ 身份级**只认**这些条目（契约表 §3.4 ③：能**唯一确定量口径**的具体条目）。
#:
#: ③ 用的是**字段身份**（列的 ``id`` / 取值键的末段），而③ 的级序**优先于**
#: ④ 显示名词表 —— 身份一命中就**遮蔽**显示名。于是身份 token 必须**比
#: 显示名更具体**才配得上这个优先级：
#:
#: - **具体字段身份**（``energyKwh`` / ``costYuan`` / ``energyCost`` /
#:   ``powerKw`` / ``pueValue`` / ``deviceName`` / ``cabinetName`` …）
#:   —— 名字里就带着「是哪个量」或「是哪个实体」，命中即是对的；
#: - **只说明「是个量」/「是个比率」/「是个计数」/「是个负载」**的裸词
#:   （``percent`` / ``count`` / ``load``）—— **不认**：它们遮蔽显示名的
#:   代价是**把对的说成错的**（例 ``col_count`` + 「告警级别」→ count、
#:   ``col_load`` + 「设备名称」→ percent）。
#:
#: 这一条是**收紧**，不是恢复按列名猜：③ 仍然只查表、命中不了就是命中不了。
IDENTITY_MATCHABLE_IDENTITIES: frozenset[str] = frozenset(
    {
        "timestamp",
        "time",
        "energyKwh",
        "costYuan",
        "energyCost",
        "powerKw",
        "pueValue",
        "pue",
        "deviceId",
        "deviceName",
        "deviceType",
        "cabinetName",
        "cabinetStatus",
        "level",
        "status",
        "description",
        "duration",
        "temperature",
        "voltage",
        "current",
    }
)


#: 产品栏目词表：显示名 → **规范字段身份**。
#:
#: 封闭词表、**精确匹配**：产品自己那几套预设栏目（告警列表、机柜状态、
#: 设备能耗）用的就是这几个名字。命中不了的显示名一律落到「语义未声明」，
#: 绝不按子串猜。
#:
#: 词表里装的是**产品真实的栏目名**：既包括既有预设栏目，也包括产品自己在
#: 用（报表草稿、指标口径）的那些列名（总耗电量 / 本月用电量 / 峰值功率 /
#: 电费 / 日增长率…）。每一项都能在 :data:`CANONICAL_FIELD_CATALOG` 里查到
#: 对应的规范字段身份 —— **不是「看着像」**。
PRODUCT_COLUMN_VOCABULARY: dict[str, str] = {
    # 设备与机柜
    "设备名称": "deviceName",
    "设备": "deviceName",
    "机柜名称": "deviceName",
    "机柜": "cabinetName",
    "设备编号": "deviceId",
    "设备类型": "deviceType",
    # 时间
    "时间": "timestamp",
    "日期": "timestamp",
    "告警时间": "timestamp",
    # 告警与状态
    "告警级别": "level",
    "告警描述": "description",
    "状态": "status",
    "运行状态": "status",
    "备注": "description",
    "描述": "description",
    # 用电量（产品总量口径）
    "耗电量": "energyKwh",
    "耗电": "energyKwh",
    "总耗电": "energyKwh",
    "总耗电量": "energyKwh",
    "本月用电量": "energyKwh",
    "用电量": "energyKwh",
    "设备能耗": "energyKwh",
    "前一日耗电": "energyKwh",
    # 成本
    "电费": "costYuan",
    "能耗成本": "costYuan",
    # 功率
    "峰值功率": "powerKw",
    "平均功率": "powerKw",
    "待机功率": "powerKw",
    # 比例
    "负载率": "load",
    "日增长率": "percent",
    "占机房总耗电比例": "percent",
    # 其它量
    "PUE": "pueValue",
    "电能使用效率": "pueValue",
    "温度": "temperature",
    "电压": "voltage",
    "电流": "current",
    "数量": "count",
    "持续时长": "duration",
}


#: **声明的单位** → 值形态。单位是方案声明的事实，不是猜出来的。
UNIT_VALUE_TYPES: dict[str, str] = {
    "kwh": VALUE_TYPE_ENERGY,
    "kw h": VALUE_TYPE_ENERGY,
    "元": VALUE_TYPE_COST,
    "cny": VALUE_TYPE_COST,
    "rmb": VALUE_TYPE_COST,
    "kw": VALUE_TYPE_POWER,
    "%": VALUE_TYPE_PERCENT,
    "％": VALUE_TYPE_PERCENT,
    "℃": VALUE_TYPE_TEMPERATURE,
    "°c": VALUE_TYPE_TEMPERATURE,
    "v": VALUE_TYPE_VOLTAGE,
    "a": VALUE_TYPE_CURRENT,
    "条": VALUE_TYPE_COUNT,
    "个": VALUE_TYPE_COUNT,
    "次": VALUE_TYPE_COUNT,
    "台": VALUE_TYPE_COUNT,
    "分钟": VALUE_TYPE_DURATION,
    "min": VALUE_TYPE_DURATION,
    "小时": VALUE_TYPE_DURATION,
    "h": VALUE_TYPE_DURATION,
}


#: 显示名末尾的**单位标注**：``耗电 (kWh)`` / ``日增长率 (%)``。
#:
#: 查封闭词表前先剥掉它 —— 单位是**标注**，不是口径名的一部分。剥掉之后
#: 仍然必须**精确命中**词表，所以这不是「按标题猜」。
_UNIT_SUFFIX_PATTERN = re.compile(
    r"^(?P<name>.+?)\s*[（(]\s*(?P<unit>[^（()）]+?)\s*[）)]$"
)


# ---------------------------------------------------------------- 描述


def deterministic_scope(scope: str, seed_key: str) -> dict[str, str]:
    """确定性上下文（契约表 §3.3）。

    只声明「这个字段的数值依赖哪一层作用域」，**不重复存时间窗** ——
    时间窗的权威落点仍然是 ``previewContext``。
    """
    return {"scope": scope, "seedKey": str(seed_key or "")}


@dataclass(frozen=True)
class SimulatedFieldDescription:
    """一个模拟字段的描述（服务端记录的事实）。"""

    semanticKind: str
    valueType: str
    deterministicScope: dict[str, str] = field(default_factory=dict)
    unit: str = ""
    semanticSource: str = "unresolved"

    def as_dict(self) -> dict[str, Any]:
        """落进 ``previewSpec.columns[]`` 的字段（契约表 §3.1 的三个新字段）。"""
        return {
            "semanticKind": self.semanticKind,
            "valueType": self.valueType,
            "deterministicScope": dict(self.deterministicScope),
        }


def _declared_unit(column: Mapping[str, Any]) -> str:
    """列**声明**的单位（``unit`` 或 ``format.unitLabel``）。"""
    unit = str(column.get("unit") or "").strip()
    if unit:
        return unit
    fmt = column.get("format")
    if isinstance(fmt, Mapping):
        return str(fmt.get("unitLabel") or "").strip()
    return ""


def _identity_tails(value: str) -> list[str]:
    """字段身份的候选：整串、**点号末段**、**下划线末段**。

    行键有**两套独立生成器**（ADR 0004 §7.4）：方案层点号
    ``section_1.col_1``、转换层补列下划线 ``section_1_col_1``。两套都要能
    切出末段去查规范字段目录 —— 只切点号的话，生产路径的下划线取值键
    永远切不出字段身份（``section_1_col_energy`` 的末段是 ``energy``）。
    """
    tails: list[str] = [value]
    dot_tail = value.rsplit(".", 1)[-1]
    if dot_tail and dot_tail != value:
        tails.append(dot_tail)
    split_tail = re.split(r"[._]", value)[-1]
    if split_tail and split_tail not in tails:
        tails.append(split_tail)
    return tails


def _canonical_identities(column: Mapping[str, Any]) -> list[str]:
    """列的**身份**候选：栏目 id、取值键、取值键的末段。"""
    identities: list[str] = []
    for key in ("id", "fieldRef"):
        value = str(column.get(key) or "").strip()
        if not value:
            continue
        for candidate in _identity_tails(value):
            if candidate and candidate not in identities:
                identities.append(candidate)
    return identities


def _vocabulary_keys(label: str) -> list[str]:
    """查封闭词表用的**候选显示名**：原样，以及剥掉末尾单位标注之后的样子。

    显示名末尾的 ``(kWh)`` / ``（%）`` 是**单位标注**（系统另有单位字段记录
    它），不是口径名的一部分 —— 查词表前先剥掉，剥完**仍然必须精确命中**
    封闭词表。只剥离**单位表里认识**的单位；不认识的一律原样留着。
    """
    text = str(label or "").strip()
    keys: list[str] = []
    if text:
        keys.append(text)
    match = _UNIT_SUFFIX_PATTERN.match(text)
    if match:
        unit = match.group("unit").strip()
        name = match.group("name").strip()
        if name and name not in keys and unit.lower() in UNIT_VALUE_TYPES:
            keys.append(name)
    return keys


def _declared_value_type(column: Mapping[str, Any]) -> str:
    """列**显式声明**的值形态。"""
    declared = str(column.get("valueType") or "").strip().lower()
    return declared if declared in VALUE_TYPE_VALUES else ""


def _declared_semantic_kind(column: Mapping[str, Any]) -> str:
    """列**显式声明**的语义类别。"""
    declared = str(column.get("semanticKind") or "").strip().lower()
    return declared if declared in SEMANTIC_KIND_VALUES else ""


def describe_simulated_field(
    column: Mapping[str, Any] | None,
    *,
    region_id: str = "",
    section_id: str = "",
) -> SimulatedFieldDescription:
    """把一列**声明**成一份模拟字段描述。

    只用声明过的事实：显式 ``valueType`` / ``semanticKind``、声明的单位、
    字段身份目录、产品词表。都不成立时明确记成「语义未声明」，不猜。
    """
    column = column if isinstance(column, Mapping) else {}
    unit = _declared_unit(column)
    semantic_kind = _declared_semantic_kind(column)
    value_type = _declared_value_type(column)
    source = "declared"

    if not value_type and semantic_kind:
        # ① 列只声明了**语义类别**：按语义类别推导对应值形态。
        #    （契约表 §3.4 的 ① 级：valueType 与 semanticKind 都算显式声明；
        #    只声明 semanticKind 时不能先判「未声明」，否则语义类别与值形态
        #    会自相矛盾 —— semanticKind=energy 却给出 valueType=text。）
        value_type = VALUE_TYPE_BY_SEMANTIC_KIND.get(semantic_kind, "")

    if not value_type:
        # 2. 声明的单位
        kind_by_unit = UNIT_VALUE_TYPES.get(unit.strip().lower())
        if kind_by_unit:
            value_type = kind_by_unit
            source = "unit"
        else:
            # 3. 字段身份命中规范字段目录（**只认能唯一确定量口径的具体条目**）
            for candidate in _canonical_identities(column):
                entry = CANONICAL_FIELD_CATALOG.get(candidate)
                if entry and candidate in IDENTITY_MATCHABLE_IDENTITIES:
                    value_type = entry["valueType"]
                    unit = unit or entry.get("unit", "")
                    source = "identity"
                    break
            else:
                # 4. 产品栏目词表（精确命中）
                label = str(column.get("label") or "").strip()
                for key in _vocabulary_keys(label):
                    canonical = PRODUCT_COLUMN_VOCABULARY.get(key)
                    entry = CANONICAL_FIELD_CATALOG.get(canonical or "")
                    if entry:
                        value_type = entry["valueType"]
                        unit = unit or entry.get("unit", "")
                        source = "vocabulary"
                        break
                else:
                    # 5. 语义未声明：明确记成 generic / text，**不猜**。
                    value_type = VALUE_TYPE_TEXT
                    source = "unresolved"

    if not semantic_kind:
        semantic_kind = SEMANTIC_KIND_BY_VALUE_TYPE.get(value_type, SEMANTIC_KIND_GENERIC)

    return SimulatedFieldDescription(
        semanticKind=semantic_kind,
        valueType=value_type,
        deterministicScope=deterministic_scope(SCOPE_REGION, region_id or section_id),
        unit=unit,
        semanticSource=source,
    )


def describe_simulated_columns(
    columns: Iterable[Mapping[str, Any]] | None,
    *,
    region_id: str = "",
    section_id: str = "",
) -> list[dict[str, Any]]:
    """给一组列**逐列**写描述（落进 ``previewSpec.columns[]``）。"""
    described: list[dict[str, Any]] = []
    for column in columns or []:
        if not isinstance(column, Mapping):
            continue
        record = dict(column)
        record.update(
            describe_simulated_field(
                column, region_id=region_id, section_id=section_id
            ).as_dict()
        )
        described.append(record)
    return described


def description_of(column: Mapping[str, Any] | None) -> SimulatedFieldDescription:
    """读一列**已经记录**的描述；没记录就按声明事实解析一份。

    读历史定义时用（那些定义里没有描述），不因此崩、也不因此猜标题。
    """
    column = column if isinstance(column, Mapping) else {}
    value_type = _declared_value_type(column)
    semantic_kind = _declared_semantic_kind(column)
    if value_type:
        scope = column.get("deterministicScope")
        return SimulatedFieldDescription(
            semanticKind=semantic_kind
            or SEMANTIC_KIND_BY_VALUE_TYPE.get(value_type, SEMANTIC_KIND_GENERIC),
            valueType=value_type,
            deterministicScope=dict(scope)
            if isinstance(scope, Mapping)
            else deterministic_scope(SCOPE_REGION, ""),
            unit=_declared_unit(column),
            semanticSource="declared",
        )
    return describe_simulated_field(column)


# ---------------------------------------------------------------- 量族


def metric_family_seed_key(kind: str, metric_key: str, unit: str) -> str:
    """量族标识：这个指标是**由哪个基础量算出来的**。

    同比（``yoyPercent``）按量族给，不是按区域给 —— 同一个量的同比只能有
    一个，否则同一份报表内部自相矛盾。

    判据（与造数器一致，全仓库只有这一处来源）：

    1. ``energy``（耗电）与 ``cost``（电费 = 耗电 × 单价）是**同一个基础量**
       → 同一族（``energy``）：总耗电卡与总电费卡的同比必须相同；
    2. ``pue`` / ``power`` / ``alarms`` / ``cabinet_status`` 各自一族；
    3. ``generic``（业务口径认不出来）先看**单位**：``kWh`` 是能耗、``元`` 是
       能耗折算出来的电费 → 都归能耗族；其余按 ``metricKey`` 分族。
    """
    semantic = str(kind or "").strip().lower()
    if semantic in (SEMANTIC_KIND_ENERGY, SEMANTIC_KIND_COST):
        return "energy"
    if semantic == SEMANTIC_KIND_GENERIC:
        text = str(unit or "").strip().lower()
        if text in ("kwh", "元", "cny", "rmb"):
            return "energy"
        return f"generic:{metric_key}"
    return semantic


# ---------------------------------------------------------------- 指标语义


#: 目录外指标：写进方案与定义的**假设标注**。
#:
#: 口径没确认之前，这一块只是「先看版式」，不是业务结论。
SIMULATION_ASSUMPTION_NOTE = "仅模拟展示、业务口径待确认"

#: 含义不清的判定词表。
_RATE_MARKERS = ("率", "占比", "比例", "比率", "份额")
_QUANTITY_MARKERS = ("耗电", "能耗", "用电", "电量", "消耗", "功率", "负载")


@dataclass(frozen=True)
class MetricSemantics:
    """一个指标意图的**语义判定**结果。"""

    kind: str
    status: str
    unit: str = ""
    note: str | None = None
    clarification: dict[str, Any] | None = None

    @property
    def is_registered(self) -> bool:
        return self.status == "registered"

    @property
    def is_ambiguous(self) -> bool:
        return self.status == "ambiguous"


def intent_is_ambiguous(intent: str) -> bool:
    """这个指标意图的**含义**是不是不清。

    「率」类说法要问清楚分母或量纲：``电脑耗电率`` 可能是耗电量、占比，也
    可能是功率 —— 三者的数字完全不同，不能替他选一个还当结论给。
    """
    text = str(intent or "").strip()
    if not text:
        return False
    has_rate = any(marker in text for marker in _RATE_MARKERS)
    has_quantity = any(marker in text for marker in _QUANTITY_MARKERS)
    return has_rate and has_quantity


def clarification_candidates(intent: str) -> list[dict[str, Any]]:
    """含义不清时，**最关键的语义区别**。"""
    text = str(intent or "")
    if any(marker in text for marker in ("耗电", "能耗", "用电", "电量", "消耗")):
        return [
            {
                "key": "energy_quantity",
                "label": "耗电量",
                "unit": "kWh",
                "hint": "一段时间内消耗的电能",
            },
            {
                "key": "share",
                "label": "占比",
                "unit": "%",
                "hint": "占总量的比例",
            },
            {
                "key": "power",
                "label": "功率",
                "unit": "kW",
                "hint": "瞬时功率",
            },
        ]
    if "功率" in text:
        return [
            {
                "key": "power",
                "label": "功率",
                "unit": "kW",
                "hint": "瞬时功率",
            },
            {
                "key": "energy_quantity",
                "label": "耗电量",
                "unit": "kWh",
                "hint": "一段时间内消耗的电能",
            },
            {
                "key": "share",
                "label": "占比",
                "unit": "%",
                "hint": "占总量的比例",
            },
        ]
    return [
        {
            "key": "value",
            "label": "数值",
            "unit": "",
            "hint": "按该指标自己的单位出数",
        },
        {
            "key": "share",
            "label": "占比",
            "unit": "%",
            "hint": "占总量的比例",
        },
        {
            "key": "count",
            "label": "数量",
            "unit": "个",
            "hint": "计数",
        },
    ]


def clarification_question(intent: str, candidates: list[dict[str, Any]]) -> str:
    """澄清那句话：只问**最关键**的区别，不问别的。"""
    options = "、".join(
        f"{item['label']}（{item['unit']}）" if item.get("unit") else str(item["label"])
        for item in candidates
    )
    return f"「{intent}」这个说法有多种口径，你要哪一种：{options}？"


def resolve_metric_semantics(
    *,
    intent: str,
    registered_kind: str | None = None,
    registered_unit: str = "",
) -> MetricSemantics:
    """判定一个指标意图的语义（**目录外指标走通用模拟策略**）。

    - 已登记指标 → 目录的量身份与单位，不改；
    - 目录外指标 → ``generic``：**不静默映射**到「总耗电」这类已有指标，
      也不共享能耗量的那份基础样本；
    - 目录外且含义不清 → 记成 ``ambiguous``，给出澄清选项，并按**显式假设**
      出数（假设写进备注，用户原话保留）。
    """
    text = str(intent or "").strip()
    if registered_kind:
        return MetricSemantics(
            kind=registered_kind,
            status="registered",
            unit=registered_unit,
        )

    # 形状型语义身份（告警列表 / 机柜状态 / 实时功率 / PUE）：
    # 它们定义的是**这一块的形态**，不是某个已登记指标，所以不构成
    # 「静默映射到已有指标」。
    if "告警" in text or "报警" in text:
        return MetricSemantics(kind=SEMANTIC_KIND_ALARMS, status="out_of_catalog", unit="")
    if ("机柜" in text or "机架" in text or "柜位" in text) and "状态" in text:
        return MetricSemantics(
            kind=SEMANTIC_KIND_CABINET_STATUS, status="out_of_catalog", unit=""
        )
    if "pue" in text.lower() or "电能使用效率" in text:
        return MetricSemantics(kind=SEMANTIC_KIND_PUE, status="out_of_catalog", unit="")
    if "功率" in text:
        return MetricSemantics(
            kind=SEMANTIC_KIND_POWER,
            status="out_of_catalog",
            unit="kW",
            note=(
                f"{SIMULATION_ASSUMPTION_NOTE}：按「实时功率（kW）」示例出数，"
                "真实口径待确认。"
            ),
        )

    # 能耗量族（耗电 / 能耗 / 电费 / 成本）是**已登记指标**的量族：
    # 目录外不给这个身份 —— 那就是静默映射到「总耗电」。
    candidates = clarification_candidates(text)
    assumed = candidates[0] if candidates else None
    unit = str((assumed or {}).get("unit") or "")
    label = str((assumed or {}).get("label") or "")

    if intent_is_ambiguous(text):
        return MetricSemantics(
            kind=SEMANTIC_KIND_GENERIC,
            status="ambiguous",
            unit=unit,
            note=(
                f"{SIMULATION_ASSUMPTION_NOTE}：暂按「{label}"
                + (f"（{unit}）" if unit else "")
                + "」示例出数，口径确认后再改。"
            ),
            clarification={
                "question": clarification_question(text, candidates),
                "candidates": candidates,
            },
        )

    if unit:
        return MetricSemantics(
            kind=SEMANTIC_KIND_GENERIC,
            status="out_of_catalog",
            unit=unit,
            note=(
                f"{SIMULATION_ASSUMPTION_NOTE}：暂按「{label}（{unit}）」示例出数，"
                "真实口径待确认。"
            ),
        )

    return MetricSemantics(
        kind=SEMANTIC_KIND_GENERIC, status="out_of_catalog", unit=""
    )


# ---------------------------------------------------------- 别名命中 ≠ 识别成功
#
# **形状型**口径（告警 / 机柜状态 / 功率 / PUE）—— 它们定义的是指标的
# **形态**，不是某个已登记指标的值口径（kWh / 元）。
_SHAPE_METRIC_KINDS = (
    SEMANTIC_KIND_ALARMS,
    SEMANTIC_KIND_CABINET_STATUS,
    SEMANTIC_KIND_POWER,
    SEMANTIC_KIND_PUE,
)


def intent_names_registered_metric(
    intent: str, metric_ref: str, metrics: Mapping[str, Mapping[str, Any]],
) -> bool:
    """用户说的指标名**本身**是不是那个已登记指标（**识别成功**）。

    **别名命中 ≠ 识别成功**（规格第 6 条 / 契约表 §3.5）。别名的匹配是
    **子串**：「设备耗电率」包含别名「设备耗电」—— 但用户问的是**比率**，
    不是 kWh 的耗电量。口径对不上就**不能**静默映射到已登记指标，得按
    目录外处理（先澄清口径，或标显式假设）。

    口径对不上有两种情形：

    1. **口径不清**（「设备耗电率」到底是耗电量、占比还是功率？）——
       已登记指标的口径是定死的（kWh / 元 / PUE），说不清就不可能是它；
    2. **口径是另一种形状**（功率 kW / 告警 / 机柜状态 / PUE）——
       与命中的已登记指标形状对不上时，说的是另一个指标。

    「设备耗电」「设备耗电排名」这类**只加了展示要求**的说法，量口径
    没变 → 仍然命中已登记指标（既有行为不变）。

    **判定谓词只有这一份**：编辑侧（``template_edit_engine``）与方案侧
    （``report_template_service``）用**同一套** —— 两处各写一份必然漂移。
    """
    text = str(intent or "").strip()
    if not text:
        return False
    semantics = resolve_metric_semantics(intent=text)
    if semantics.status == "ambiguous":
        # 口径不清：说不清是哪个口径，就不是口径定死的已登记指标。
        return False
    if semantics.kind in _SHAPE_METRIC_KINDS:
        label = str((metrics.get(metric_ref) or {}).get("label") or "")
        metric_kind = resolve_metric_semantics(intent=label).kind if label else ""
        if metric_kind != semantics.kind:
            # 形状对不上（说功率却命中了 kWh 的耗电）→ 是另一个指标。
            return False
    return True


def simulated_metric_id(section_id: str, index: int) -> str:
    """目录外指标的**稳定模拟身份**（契约表 §3.5）。

    它**不是** ``metricRef``：真实目录里没有这个指标，不能拿一个像模像样
    的引用冒充。同一份方案里同一个板块的第几个指标 → 同一个身份。
    """
    slug = "".join(
        char if char.isalnum() else "_" for char in str(section_id or "")
    ).strip("_").lower() or "section"
    return f"simulated.{slug}.{int(index) + 1}"


# ---------------------------------------------------------------- 目录外文案


def out_of_catalog_note(mode: str = "simulated") -> str:
    """目录外指标的区域说明（**按来源模式**分文案）。

    真实目录的限制**不得**被说成「模拟渲染做不到」—— 模拟模式下这一块照样
    出数、照样能看版式。
    """
    if mode == "user_json":
        return (
            "真实指标目录里没有登记这个指标：接入真实数据时需要先补登记；"
            "当前这一块按你提供的 JSON 出数。"
        )
    return (
        "真实指标目录里没有登记这个指标：接入真实数据时需要先补登记；"
        "当前这一块按模拟示例出数（已标注为模拟），不影响先看版式。"
    )


#: 目录外指标的占位原因前缀。这句只说**真实能力**还差什么。
OUT_OF_CATALOG_REASON_PREFIX = "能力待接通："


def out_of_catalog_reason(mode: str = "simulated", *, intent: str = "") -> str:
    """目录外指标的区域说明全文（模板层与转换层共用同一句）。

    模板层与转换层各写一句的话，两处口径就会漂 —— 全仓库只有这一处来源。
    """
    reason = OUT_OF_CATALOG_REASON_PREFIX + out_of_catalog_note(mode)
    semantics = resolve_metric_semantics(intent=intent)
    if semantics.note:
        reason = f"{reason} {semantics.note}"
    if semantics.clarification:
        question = str(semantics.clarification.get("question") or "").strip()
        if question:
            reason = f"{reason} {question}"
    return reason
