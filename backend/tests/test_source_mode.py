"""工单 59 / GitHub #59：数据来源模式与目录外模拟的能力层。

这一票把「这份报表的数字从哪来」变成**服务端记录的一等事实**
（``sourceMode``），并把造数从「按列名猜」改成「按描述造」。

覆盖的验收点：

1. 来源模式三处同名同类型：定义级 ``metadata.sourceMode``、区域级
   ``templateBinding.sourceMode``、载荷级 ``regions[].payload.sourceMode``；
2. 老数据默认：没有 ``sourceMode`` → ``simulated``；没有 ``semanticKind`` /
   ``valueType`` → ``generic`` / ``text``，**不回退到按列名猜**；
3. 目录外指标（如「电脑耗电率」）在模拟模式下**不阻断**：出数、载荷带来源
   与能力标注、定义里不落任何业务数值；
4. 目录外指标**不静默映射**到已登记指标（不伪造 ``metricRef``、不共享
   能耗量的那份基础样本）；
5. 含义不清 → 给出最关键的语义区别（澄清），按**显式假设**出数并写明
   「仅模拟展示、业务口径待确认」，用户原话保留；
6. 造数器按描述造数：``_column_value_kind`` 已删除，值形态由描述决定；
7. 确定性：同一份定义 + 同一时间窗 → 数字恒定；区域级噪声各自不同；
8. 校验层：区域级来源模式与定义级不一致 → 校验失败；非法取值 → 失败；
9. 能力文案：真实目录的限制**不得**被说成「模拟渲染做不到」。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from app.services.data_source_mode import (
    DEFAULT_SOURCE_MODE,
    SOURCE_MODE_KEY,
    SOURCE_MODE_SIMULATED,
    SOURCE_MODE_USER_JSON,
    normalize_source_mode,
    resolve_source_mode,
    source_mode_of_definition,
)
from app.services.preview_service import (
    build_preview_context,
    build_report_preview_payload,
)
from app.services.report_template_service import build_template_design
from app.services.simulated_data_service import generate_simulated_data
from app.services.simulated_field_semantics import (
    SIMULATION_ASSUMPTION_NOTE,
    VALUE_TYPE_ENERGY,
    VALUE_TYPE_TEXT,
    describe_simulated_field,
    resolve_metric_semantics,
)
from app.services.template_definition_service import build_template_definition
from app.services.validation_service import validate_definition


BACKEND_DIR = Path(__file__).resolve().parent.parent

#: 造数器源码（本票删掉了「按列名猜」那条路径）。
_GENERATOR_SOURCE = BACKEND_DIR / "app" / "services" / "simulated_data_service.py"


# --------------------------------------------------------------------------
# 造数据
# --------------------------------------------------------------------------


def _plan(sections: list[dict[str, Any]], *, revision: int = 1) -> dict[str, Any]:
    return {
        "id": "rplan_source_mode", "revision": revision, "topic": "A 机房能耗日报",
        "requirements": {
            "metrics": ["总耗电"], "subject": "A 机房",
            "timeRange": "最近 7 天", "exclusions": [],
        },
        "sections": sections,
        "assumptions": [], "suggestions": [], "referenceTemplates": [],
    }


def _metric_card_section(
    section_id: str = "section_total",
    title: str = "总耗电概览",
    intents: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "id": section_id, "title": title,
        "metricIntents": intents if intents is not None else ["总耗电"],
        "componentType": "metric_card",
    }


def _build(
    sections: list[dict[str, Any]], *, source_mode: str | None = None,
    plan: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """方案 → 模板 → 定义草稿（走真实的两跳）。"""
    plan = plan or _plan(sections)
    template = build_template_design(
        plan, subject_id="room_a", source_mode=source_mode
    )
    draft = build_template_definition(
        template.model_dump(mode="json"), plan, "room_a", source_mode=source_mode
    )
    return plan, draft.model_dump(mode="json")


def _preview(definition: dict[str, Any]) -> dict[str, Any]:
    """按定义生成示意载荷（走真实的载荷装配）。"""
    context = build_preview_context(definition)
    return build_report_preview_payload(definition, context)


# --------------------------------------------------------------------------
# 1. 来源模式是一等事实
# --------------------------------------------------------------------------


def test_source_mode_is_recorded_in_three_places_with_the_same_value():
    """定义级 / 区域级 / 载荷级：同名同类型，取值域一致。"""
    _, definition = _build([_metric_card_section()], source_mode=SOURCE_MODE_USER_JSON)

    assert definition["metadata"][SOURCE_MODE_KEY] == SOURCE_MODE_USER_JSON

    region = definition["regions"][0]
    binding = region["templateBinding"]
    assert binding[SOURCE_MODE_KEY] == SOURCE_MODE_USER_JSON
    assert binding["previewSpec"][SOURCE_MODE_KEY] == SOURCE_MODE_USER_JSON

    payload = _preview(definition)
    region_payload = payload["regions"][0]["payload"]
    # 载荷级：来源模式随载荷走。
    assert region_payload[SOURCE_MODE_KEY] == SOURCE_MODE_USER_JSON
    # ``dataSource`` 语义不动：载荷仍由模拟造数器产出。
    assert region_payload["dataSource"] == "simulated"


def test_source_mode_is_never_guessed_from_titles_or_prompts():
    """判定只认服务端记录的值：标题里写「模拟」也不改判定。"""
    section = _metric_card_section(title="模拟示例总耗电概览")
    _, definition = _build([section])

    assert definition["metadata"][SOURCE_MODE_KEY] == DEFAULT_SOURCE_MODE
    assert normalize_source_mode(definition["metadata"][SOURCE_MODE_KEY]) == (
        SOURCE_MODE_SIMULATED
    )


def test_old_definition_without_source_mode_falls_back_to_simulated():
    """老定义没有记录 → 按默认口径（模拟示例）处理，不是「谁声明的」。"""
    _, definition = _build([_metric_card_section()])
    definition["metadata"].pop(SOURCE_MODE_KEY, None)
    for region in definition["regions"]:
        region["templateBinding"].pop(SOURCE_MODE_KEY, None)
        region["templateBinding"]["previewSpec"].pop(SOURCE_MODE_KEY, None)

    assert source_mode_of_definition(definition).recorded is False
    payload = _preview(definition)
    assert payload["regions"][0]["payload"][SOURCE_MODE_KEY] == SOURCE_MODE_SIMULATED


# --------------------------------------------------------------------------
# 2. 旧数据默认：没有描述就是 generic / text，不按列名猜
# --------------------------------------------------------------------------


def test_column_without_description_defaults_to_generic_text():
    """认不出来就说「语义未声明」：``generic`` / ``text``，不猜。"""
    description = describe_simulated_field(
        {"id": "col_x", "label": "某种没声明过的列", "fieldRef": "col_x"}
    )

    assert description.semanticKind == "generic"
    assert description.valueType == VALUE_TYPE_TEXT


def test_declared_value_type_wins_over_everything_else():
    """显式声明的值形态优先：描述说了算，标题不算数。"""
    description = describe_simulated_field(
        {
            "id": "col_energy", "label": "神秘列", "fieldRef": "col_energy",
            "valueType": VALUE_TYPE_ENERGY,
        }
    )

    assert description.valueType == VALUE_TYPE_ENERGY
    assert description.semanticKind == "energy"


def test_generator_no_longer_guesses_by_column_name():
    """按列名猜的那条路径已经删掉了（契约表 §3.2）。"""
    source = _GENERATOR_SOURCE.read_text(encoding="utf-8")
    assert "_column_value_kind" not in source


# --------------------------------------------------------------------------
# 3 & 4. 目录外指标：模拟模式下不阻断、出数、不静默映射
# --------------------------------------------------------------------------


def test_out_of_catalog_metric_is_not_blocked_and_still_produces_numbers():
    """「电脑耗电率」在模拟模式下照样出数，标注也在。"""
    section = _metric_card_section(
        "section_extra", "电脑耗电率概览", intents=["电脑耗电率"]
    )
    _, definition = _build([section])

    spec = definition["regions"][0]["templateBinding"]["previewSpec"]
    metric = spec["metrics"][0]
    # 目录外：走通用模拟策略，**不伪造真实 metricRef**。
    assert metric["kind"] == "generic"
    assert metric["bindingKind"] == "simulated"
    assert metric["simulatedId"].startswith("simulated.")
    assert not metric["metricKey"].startswith("metric.machine_room.")

    payload = _preview(definition)
    region = payload["regions"][0]
    assert region["componentType"] == "METRIC_CARD"
    # 出数了（不是空壳、不是占位文字）。
    assert isinstance(region["payload"]["value"], (int, float))
    # 载荷带来源与能力标注。
    assert region["payload"][SOURCE_MODE_KEY] == SOURCE_MODE_SIMULATED
    assert region["capabilityPending"] is True


def test_out_of_catalog_metric_does_not_share_the_energy_base_sample():
    """不静默映射到「总耗电」：目录外指标不共享能耗量的那份基础样本。"""
    sections = [
        _metric_card_section("section_total", "总耗电概览", intents=["总耗电"]),
        _metric_card_section(
            "section_extra", "电脑耗电率概览", intents=["电脑耗电率"]
        ),
    ]
    _, definition = _build(sections)

    payloads = generate_simulated_data(
        definition, build_preview_context(definition)
    )
    total = payloads["region_1"]["value"]
    extra = payloads["region_2"]["value"]
    assert total != extra


def test_definition_records_no_business_values():
    """目录外指标也只进**结构**：定义里不落任何业务数值。"""
    section = _metric_card_section(
        "section_extra", "电脑耗电率概览", intents=["电脑耗电率"]
    )
    _, definition = _build([section])

    region = definition["regions"][0]
    for key in ("value", "values", "yValues", "xLabels", "rows", "points"):
        assert key not in region, key
    for key in ("value", "values", "yValues", "xLabels", "rows", "points"):
        assert key not in region["component"], key


# --------------------------------------------------------------------------
# 5. 含义不清：澄清 + 显式假设
# --------------------------------------------------------------------------


def test_ambiguous_intent_asks_the_key_question_and_states_the_assumption():
    """含义不清 → 问最关键的语义区别，按显式假设出数，用户原话保留。"""
    section = _metric_card_section(
        "section_extra", "电脑耗电率概览", intents=["电脑耗电率"]
    )
    _, definition = _build([section])

    spec = definition["regions"][0]["templateBinding"]["previewSpec"]
    metric = spec["metrics"][0]
    # 用户原话保留：系统不悄悄把「电脑耗电率」改名成别的指标。
    assert metric["label"] == "电脑耗电率"

    clarification = spec["clarification"]
    assert clarification is not None
    question = clarification["question"]
    for label in ("耗电量", "占比", "功率"):
        assert label in question, question

    # 显式假设写进说明，不当成结论。
    reason = str(metric["pendingReason"] or "")
    assert SIMULATION_ASSUMPTION_NOTE in reason, reason


def test_unambiguous_out_of_catalog_metric_has_no_clarification():
    """含义清楚就不追问：只标口径待确认。"""
    semantics = resolve_metric_semantics(intent="客流量")

    assert semantics.status == "out_of_catalog"
    assert semantics.clarification is None


# --------------------------------------------------------------------------
# 7. 确定性
# --------------------------------------------------------------------------


def test_same_definition_and_window_generate_identical_numbers():
    """同一份定义 + 同一时间窗 → 数字恒定。"""
    section = _metric_card_section(
        "section_extra", "电脑耗电率概览", intents=["电脑耗电率"]
    )
    _, definition = _build([section])
    context = build_preview_context(definition)

    first = generate_simulated_data(definition, context)
    second = generate_simulated_data(definition, context)

    assert first["region_1"]["value"] == second["region_1"]["value"]


def test_out_of_catalog_regions_in_the_same_report_differ():
    """两个目录外指标落在不同区域 → 各自的区域级噪声不同。"""
    sections = [
        _metric_card_section("section_a", "甲号耗电率", intents=["甲耗电率"]),
        _metric_card_section("section_b", "乙号耗电率", intents=["乙耗电率"]),
    ]
    _, definition = _build(sections)
    payloads = generate_simulated_data(
        definition, build_preview_context(definition)
    )

    assert payloads["region_1"]["value"] != payloads["region_2"]["value"]


# --------------------------------------------------------------------------
# 8. 校验层：来源模式必须一致
# --------------------------------------------------------------------------


def _validation_codes(definition: dict[str, Any]) -> set[str]:
    result = validate_definition(definition)
    return {issue.code for issue in result.issues}


def test_region_source_mode_must_match_the_definition_level():
    """区域级 ≠ 定义级 → 校验失败（一份报表不混两种来源的数字）。"""
    _, definition = _build([_metric_card_section()])
    definition["metadata"][SOURCE_MODE_KEY] = SOURCE_MODE_SIMULATED
    definition["regions"][0]["templateBinding"][SOURCE_MODE_KEY] = (
        SOURCE_MODE_USER_JSON
    )

    assert "SOURCE_MODE_MISMATCH" in _validation_codes(definition)


def test_illegal_source_mode_fails_validation():
    """认不出来的来源模式不许混进定义。"""
    _, definition = _build([_metric_card_section()])
    definition["metadata"][SOURCE_MODE_KEY] = "user-json"

    assert "SOURCE_MODE_INVALID" in _validation_codes(definition)


def test_consistent_source_mode_passes_the_source_mode_rules():
    """三处一致 → 不报来源模式的问题。"""
    _, definition = _build([_metric_card_section()], source_mode=SOURCE_MODE_USER_JSON)

    codes = _validation_codes(definition)
    assert "SOURCE_MODE_MISMATCH" not in codes
    assert "SOURCE_MODE_INVALID" not in codes


# --------------------------------------------------------------------------
# 9. 能力文案：真实目录的限制 ≠ 模拟渲染做不到
# --------------------------------------------------------------------------


def test_capability_wording_separates_real_limits_from_rendering_limits():
    """目录里没有登记 ≠ 模拟渲染做不到。"""
    from app.services.product_capability_service import describe_capability

    result = describe_capability(metric_ref="metric.machine_room.not_registered")

    assert result["outcome"] == "NOT_REGISTERED"
    assert "不是模拟渲染的限制" in result["summary"], result["summary"]
    assert any("不是模拟渲染的限制" in item for item in result["mustState"])


def test_out_of_catalog_region_reason_does_not_claim_rendering_failure():
    """区域说明里不许出现「模拟渲染做不到」这种话。"""
    section = _metric_card_section(
        "section_extra", "电脑耗电率概览", intents=["电脑耗电率"]
    )
    _, definition = _build([section])

    reason = str(
        definition["regions"][0]["templateBinding"]["placeholderReason"] or ""
    )
    assert "能力待接通" in reason, reason
    assert "模拟渲染做不到" not in reason, reason
    # 真实目录的限制照实说，模拟照样出数也照实说。
    assert "没有登记" in reason, reason
    assert "按模拟示例出数" in reason, reason


# --------------------------------------------------------------------------
# 归一化口径
# --------------------------------------------------------------------------


def test_normalize_source_mode_rejects_unknown_values():
    """来源模式要么是记录的事实，要么就没有 —— **不猜**。"""
    assert normalize_source_mode("simulated") == SOURCE_MODE_SIMULATED
    assert normalize_source_mode("user_json") == SOURCE_MODE_USER_JSON
    assert normalize_source_mode("按标题猜的") is None
    assert normalize_source_mode(None) is None
    assert resolve_source_mode(None).recorded is False
