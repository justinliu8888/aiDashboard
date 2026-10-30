"""工单 #59 独立验收修复：模拟字段描述的**五级兜底链**逐级钉住。

工单 #59 自己的 19 个用例只钉住了 ① 级（``declared``）。验收方用变异测试
证明：分别破坏 ② ``unit`` / ③ ``identity`` / ④ ``vocabulary`` 三级的判定，
那 19 个用例**仍然全绿** —— ②③④ 三级**没有任何回归防护**，M2-a / M2-b
两处契约偏离长期未被发现就是这么来的。

本文件给**每一级**补至少一条用例，并且每条都断言 ``semanticSource``
（只断言结果值的话，「两侧同样抛异常」会造成假绿）。破坏任意一级的判定，
对应用例**必须变红**。

另外补三条「不许回退到按标题猜」的护栏：

- 显示名里**含**「耗电」但不在封闭词表里 → 仍然 unresolved；
- 规范字段目录里的每一项都能查到值形态，封闭词表里的每一项都能查到
  **规范字段身份**（没有「看着像」的条目）；
- ③ 身份级**不遮蔽** ④ 词表级里更具体的词（「设备编号」不能因为身份
  叫 ``col_device`` 就显示成设备名）。

覆盖的验收点：

1. ① ``declared``：``valueType`` 直接采用；只声明 ``semanticKind`` 时按
   语义类别**推导**值形态（契约表 §3.4 的 ① 级）；
2. ② ``unit``：列声明的单位决定值形态；
3. ③ ``identity``：字段身份命中规范字段目录，**点号与下划线两套行键
   生成器都要能切出末段**（ADR 0004 §7.4）；
4. ④ ``vocabulary``：显示名**精确命中**产品栏目封闭词表；
5. ⑤ ``unresolved``：认不出来就明确记成 ``generic`` / ``text``，不猜；
6. 生产路径：真实业务列名出的是数值 / 合理值，不是文本占位；
7. 占位文案说清自己是占位。
"""
from __future__ import annotations

from typing import Any

import pytest

from app.services.preview_service import (
    build_preview_context,
    build_report_preview_payload,
)
from app.services.report_template_service import build_template_design
from app.services.simulated_data_service import PLACEHOLDER_VALUE_SUFFIX
from app.services.simulated_field_semantics import (
    CANONICAL_FIELD_CATALOG,
    PRODUCT_COLUMN_VOCABULARY,
    VALUE_TYPE_BY_SEMANTIC_KIND,
    VALUE_TYPE_COST,
    VALUE_TYPE_DEVICE_ID,
    VALUE_TYPE_DEVICE_NAME,
    VALUE_TYPE_ENERGY,
    VALUE_TYPE_PERCENT,
    VALUE_TYPE_POWER,
    VALUE_TYPE_STATUS,
    VALUE_TYPE_TEXT,
    VALUE_TYPE_TIME,
    VALUE_TYPE_VALUES,
    describe_simulated_field,
)
from app.services.template_definition_service import build_template_definition


#: 真实产品里模型会写的列名（取自本仓 fixture 与提示词示例）。
REAL_LABELS = [
    "耗电 (kWh)", "总耗电", "耗电", "耗电量", "设备名称", "告警级别", "时间",
    "机柜", "设备", "PUE", "本月用电量", "总耗电量", "设备能耗", "日增长率",
    "前一日耗电 (kWh)", "占机房总耗电比例 (%)", "负载率", "运行状态",
    "机柜名称", "设备编号", "设备类型", "告警时间", "状态",
]


def _describe(column: dict[str, Any]):
    return describe_simulated_field(column, region_id="region_1")


# --------------------------------------------------------------------------
# ① declared：列显式声明
# --------------------------------------------------------------------------


def test_declared_value_type_is_taken_directly():
    """① ``declared``：``valueType`` 直接采用。"""
    description = _describe(
        {
            "id": "col_x", "label": "神秘列", "fieldRef": "section_1_col_x",
            "valueType": VALUE_TYPE_ENERGY,
        }
    )

    assert description.semanticSource == "declared"
    assert description.valueType == VALUE_TYPE_ENERGY
    assert description.semanticKind == "energy"


def test_declared_semantic_kind_alone_derives_the_value_type():
    """① ``declared``：**只声明** ``semanticKind`` 时按语义类别推导值形态。

    契约表 §3.4 的 ① 级写的是「``columns[].valueType`` /
    ``columns[].semanticKind``」—— 两个都算显式声明。只声明
    ``semanticKind=energy`` 却给出 ``valueType=text``，语义类别与值形态
    自相矛盾。
    """
    description = _describe(
        {
            "id": "col_x", "label": "神秘列", "fieldRef": "section_1_col_x",
            "semanticKind": "energy",
        }
    )

    assert description.semanticSource == "declared"
    assert description.valueType == VALUE_TYPE_ENERGY
    assert description.semanticKind == "energy"


@pytest.mark.parametrize(
    "kind,expected",
    [
        ("energy", VALUE_TYPE_ENERGY),
        ("cost", VALUE_TYPE_COST),
        ("power", VALUE_TYPE_POWER),
        ("alarms", "level"),
        ("cabinet_status", VALUE_TYPE_STATUS),
        ("generic", VALUE_TYPE_TEXT),
    ],
)
def test_every_semantic_kind_derives_a_value_type(kind: str, expected: str):
    """语义类别 → 值形态的推导**全覆盖**：不留「推导不出来」的空洞。"""
    assert VALUE_TYPE_BY_SEMANTIC_KIND[kind] == expected

    description = _describe(
        {
            "id": "col_x", "label": "神秘列", "fieldRef": "section_1_col_x",
            "semanticKind": kind,
        }
    )

    assert description.semanticSource == "declared"
    assert description.valueType == expected
    assert description.semanticKind == kind


def test_declared_beats_the_closed_vocabulary():
    """① 优先于 ④：列显式声明了，就不看显示名。"""
    description = _describe(
        {
            "id": "col_x", "label": "总耗电量", "fieldRef": "section_1_col_x",
            "valueType": VALUE_TYPE_PERCENT,
        }
    )

    assert description.semanticSource == "declared"
    assert description.valueType == VALUE_TYPE_PERCENT


# --------------------------------------------------------------------------
# ② unit：列声明了单位
# --------------------------------------------------------------------------


def test_declared_unit_decides_the_value_type():
    """② ``unit``：列声明的单位决定值形态。"""
    description = _describe(
        {
            "id": "col_x", "label": "神秘列", "fieldRef": "section_1_col_x",
            "format": {"kind": "NUMBER", "precision": 1, "unitLabel": "kWh"},
        }
    )

    assert description.semanticSource == "unit"
    assert description.valueType == VALUE_TYPE_ENERGY


def test_declared_unit_is_read_from_the_top_level_unit_field_too():
    """② ``unit``：``unit`` 字段与 ``format.unitLabel`` 都算数。"""
    description = _describe(
        {
            "id": "col_x", "label": "神秘列", "fieldRef": "section_1_col_x",
            "unit": "元",
        }
    )

    assert description.semanticSource == "unit"
    assert description.valueType == VALUE_TYPE_COST


# --------------------------------------------------------------------------
# ③ identity：列的字段身份
# --------------------------------------------------------------------------


def test_field_identity_hits_the_canonical_catalog():
    """③ ``identity``：字段身份命中规范字段目录。"""
    description = _describe(
        {
            "id": "col_x", "label": "神秘列", "fieldRef": "section_1.energyKwh",
        }
    )

    assert description.semanticSource == "identity"
    assert description.valueType == VALUE_TYPE_ENERGY


def test_field_identity_tail_is_split_on_both_dot_and_underscore():
    """③ ``identity``：**两套行键生成器**都要能切出末段。

    行键有两套独立生成器（ADR 0004 §7.4）：方案层点号 ``section_1.col_1``、
    转换层补列下划线 ``section_1_col_1``。只切点号的话，生产路径的下划线
    取值键永远落不到 identity 级。
    """
    dotted = _describe(
        {"id": "col_x", "label": "神秘列", "fieldRef": "section_1.energyKwh"}
    )
    underscored = _describe(
        {"id": "col_x", "label": "神秘列", "fieldRef": "section_1_energyKwh"}
    )

    assert dotted.semanticSource == "identity"
    assert dotted.valueType == VALUE_TYPE_ENERGY
    assert underscored.semanticSource == "identity"
    assert underscored.valueType == VALUE_TYPE_ENERGY


def test_underscore_tail_of_a_generated_column_id_hits_the_catalog():
    """③ ``identity``：生产形态的取值键 ``section_1_col_energy`` 也能切出末段。"""
    description = _describe(
        {"id": "col_energy", "label": "总耗电量", "fieldRef": "section_1_col_energy"}
    )

    assert description.semanticSource == "identity"
    assert description.valueType == VALUE_TYPE_ENERGY


def test_identity_does_not_shadow_a_more_specific_vocabulary_term():
    """③ 不遮蔽 ④：「设备编号」不能因为身份叫 ``col_device`` 显示成设备名。"""
    description = _describe(
        {
            "id": "col_device", "label": "设备编号", "fieldRef": "section_1_col_device",
        }
    )

    assert description.semanticSource == "vocabulary"
    assert description.valueType == VALUE_TYPE_DEVICE_ID

    named = _describe(
        {
            "id": "col_device", "label": "设备名称", "fieldRef": "section_1_col_device",
        }
    )

    assert named.semanticSource == "vocabulary"
    assert named.valueType == VALUE_TYPE_DEVICE_NAME


# --------------------------------------------------------------------------
# ④ vocabulary：列的显示名命中封闭词表
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "label,expected",
    [
        ("总耗电量", VALUE_TYPE_ENERGY),
        ("本月用电量", VALUE_TYPE_ENERGY),
        ("峰值功率", VALUE_TYPE_POWER),
        ("电费", VALUE_TYPE_COST),
        ("日增长率", VALUE_TYPE_PERCENT),
        ("负载率", VALUE_TYPE_PERCENT),
        ("设备名称", VALUE_TYPE_DEVICE_NAME),
        ("告警时间", VALUE_TYPE_TIME),
    ],
)
def test_display_name_hits_the_closed_vocabulary(label: str, expected: str):
    """④ ``vocabulary``：显示名**精确命中**产品栏目封闭词表。"""
    description = _describe(
        {"id": "col_x", "label": label, "fieldRef": "section_1_col_x"}
    )

    assert description.semanticSource == "vocabulary"
    assert description.valueType == expected


def test_vocabulary_lookup_strips_only_a_known_unit_annotation():
    """④ ``vocabulary``：末尾的**已知单位**标注不是口径名的一部分。

    剥掉之后**仍然必须精确命中**封闭词表 —— 不做任何子串匹配。
    """
    with_unit = _describe(
        {"id": "col_x", "label": "耗电 (kWh)", "fieldRef": "section_1_col_x"}
    )
    without_unit = _describe(
        {"id": "col_x", "label": "耗电", "fieldRef": "section_1_col_x"}
    )

    assert with_unit.semanticSource == "vocabulary"
    assert with_unit.valueType == VALUE_TYPE_ENERGY
    assert without_unit.semanticSource == "vocabulary"
    assert without_unit.valueType == VALUE_TYPE_ENERGY

    # 单位表里**不认**的括号内容不剥：剥了也命中不了词表，结果一样是 unresolved。
    unknown_unit = _describe(
        {"id": "col_x", "label": "耗电 (桶)", "fieldRef": "section_1_col_x"}
    )
    assert unknown_unit.semanticSource == "unresolved"
    assert unknown_unit.valueType == VALUE_TYPE_TEXT


# --------------------------------------------------------------------------
# ⑤ unresolved：认不出来就说认不出来
# --------------------------------------------------------------------------


def test_unknown_display_name_stays_unresolved():
    """⑤ ``unresolved``：认不出来 → ``generic`` / ``text``，不猜。"""
    description = _describe(
        {"id": "col_x", "label": "某种没声明过的列", "fieldRef": "section_1_col_x"}
    )

    assert description.semanticSource == "unresolved"
    assert description.valueType == VALUE_TYPE_TEXT
    assert description.semanticKind == "generic"


def test_a_label_merely_containing_a_quantity_word_is_not_guessed():
    """**不许按标题猜**：名字里含「耗电」也不等于能耗列。

    这是「按列名猜」那条路径最典型的形态：换个显示名，值的量纲就变了。
    """
    description = _describe(
        {"id": "col_x", "label": "本月耗电量排行榜", "fieldRef": "section_1_col_x"}
    )

    assert description.semanticSource == "unresolved"
    assert description.valueType == VALUE_TYPE_TEXT


def test_every_vocabulary_entry_points_at_a_known_canonical_identity():
    """封闭词表里的每一项都能在规范字段目录里查到对应 —— 没有「看着像」的条目。"""
    for label, canonical in PRODUCT_COLUMN_VOCABULARY.items():
        assert canonical in CANONICAL_FIELD_CATALOG, label


def test_every_canonical_entry_has_a_known_value_type():
    """规范字段目录里的每一项的值形态都在取值域内。"""
    for identity, entry in CANONICAL_FIELD_CATALOG.items():
        assert entry["valueType"] in VALUE_TYPE_VALUES, identity


# --------------------------------------------------------------------------
# 生产路径：真实业务列名出的是数值 / 合理值，不是占位
# --------------------------------------------------------------------------


def _table_payload(labels: list[str]) -> tuple[dict[str, Any], dict[str, Any]]:
    columns = [
        {"id": f"col_{index}", "label": label}
        for index, label in enumerate(labels)
    ]
    plan = {
        "id": "rplan_ticket59", "revision": 1, "topic": "A 机房能耗日报",
        "requirements": {
            "metrics": ["总耗电"], "subject": "A 机房",
            "timeRange": "最近 7 天", "exclusions": [],
        },
        "sections": [
            {
                "id": "section_1", "title": "设备能耗", "metricIntents": ["总耗电"],
                "componentType": "table", "columns": columns,
            }
        ],
        "assumptions": [], "suggestions": [], "referenceTemplates": [],
    }
    template = build_template_design(plan, subject_id="room_a")
    definition = build_template_definition(
        template.model_dump(mode="json"), plan, "room_a"
    ).model_dump(mode="json")
    payload = build_report_preview_payload(definition, build_preview_context(definition))
    spec = definition["regions"][0]["templateBinding"]["previewSpec"]
    return spec, payload["regions"][0]["payload"]


def test_real_product_column_labels_do_not_fall_back_to_text_placeholders():
    """真实业务列名不再大面积退化成文本占位（工单 #59 验收的阻塞项 B1）。"""
    spec, payload = _table_payload(REAL_LABELS)

    assert len(spec["columns"]) == len(REAL_LABELS)
    row = payload["rows"][0]
    for column in spec["columns"]:
        value = row[column["fieldRef"]]
        assert not str(value).startswith("示例值"), (column["label"], value)


def test_real_product_column_labels_produce_numeric_or_sensible_values():
    """真实业务列名出的是数值或合理值（设备名 / 状态 / 时间…）。"""
    spec, payload = _table_payload(REAL_LABELS)

    row = payload["rows"][0]
    numeric = 0
    for column in spec["columns"]:
        value = row[column["fieldRef"]]
        if isinstance(value, (int, float)):
            numeric += 1
            continue
        # 非数值的必须是**有意义的文本**，不是占位。
        assert not str(value).startswith("示例值"), (column["label"], value)
        if str(value).strip() == "":
            # 时间列在没有时间上下文的表里本来就是空的（既有行为，
            # 见本票报告的「已知限制」）。别的列不许空。
            assert column["valueType"] == "time", (column["label"], value)
    assert numeric >= 10, f"只有 {numeric}/{len(REAL_LABELS)} 列出了数值"


def test_unresolved_column_says_it_is_a_placeholder():
    """占位文案要**说清是占位**（B1 ④）。"""
    spec, payload = _table_payload(["某种没声明过的列"])

    value = payload["rows"][0][spec["columns"][0]["fieldRef"]]
    assert str(value).startswith("示例值")
    assert PLACEHOLDER_VALUE_SUFFIX in str(value)
