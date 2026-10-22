"""第一版闭环链路的端到端用例。

按《20261022 第一版闭环范围与验收记录》Q11 / Q17 / Q18 / Q19 覆盖：
    - 纯提问只作答，不产候选计划、不改草稿、不占澄清轮次（Q19）
    - 候选计划 -> 用户接受 -> 确定性落草稿；接受前草稿不变（Q11）
    - 组合修改一次接受、原子生效，不做一半（Q11）
    - 同一项修改最多追问三轮（Q18）
    - 草稿版本变了以后，旧计划必须被拦住（Q17）
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.services.report_draft_service import build_definition_draft
from app.services.session_service import session_service
from app.services.validation_service import validate_definition


@pytest.fixture()
def definition():
    return build_definition_draft("A 机房能耗日报", "room_a")


def _snap(definition):
    return {"reportDefinition": definition.model_dump(mode="json")}


def _accept_options(payload):
    question = payload["questions"][0]
    assert question["id"] == "pending_edit_plan_confirm"
    assert question["selectionOnly"] is True
    return [item["value"] for item in question["options"]]


# ---------- Q19：纯提问只作答 ----------


def test_readonly_question_answers_without_pending_plan(post_chat, definition):
    """纯提问只作答，不弹修改菜单、不改草稿。"""
    response = post_chat(
        "这个能按分钟看吗？",
        state="USER_EDITING_TEMPLATE",
        snapshots=_snap(definition),
        selectedRegionId="region_hourly_trend",
    ).json()

    assert response["state"] == "USER_EDITING_TEMPLATE"
    assert response["domainObjects"]["pendingEditPlan"] is None
    assert response["domainObjects"]["changeSet"] is None
    assert response["domainObjects"]["reportDefinitionDraft"]["metadata"]["revision"] == 0
    assert response["questions"] == []
    assert "不支持分钟粒度" in response["reply"]
    assert session_service.load_pending_choice("conv_test") is None
    assert definition.metadata["revision"] == 0


def test_readonly_question_without_target_does_not_turn_into_edit(post_chat, definition):
    response = post_chat(
        "这个能按分钟看吗？",
        state="USER_EDITING_TEMPLATE",
        snapshots=_snap(definition),
    ).json()
    assert "请指出" in response["reply"]
    assert response["questions"] == []
    assert response["domainObjects"]["pendingEditPlan"] is None
    assert session_service.load_pending_plan("conv_test") is None
    assert session_service.load_clarification("conv_test") is None


def test_readonly_question_does_not_consume_clarification_rounds(
    post_chat, definition
):
    """纯提问不占「同一项修改最多三轮」的澄清轮次。"""
    for _ in range(4):
        response = post_chat(
            "这个能按分钟看吗？",
            state="USER_EDITING_TEMPLATE",
            snapshots=_snap(definition),
            selectedRegionId="region_hourly_trend",
        ).json()
        assert response["domainObjects"]["pendingEditPlan"] is None
        assert "三轮" not in response["reply"]
    assert session_service.load_clarification("conv_test") is None

    # 轮次没被占掉：真的提出修改要求时仍然能正常追问。
    asked = post_chat(
        "改一下",
        state="USER_EDITING_TEMPLATE",
        snapshots=_snap(definition),
    ).json()
    assert asked["state"] == "USER_EDITING_TEMPLATE"


def test_explicit_change_command_still_produces_candidate(post_chat, definition):
    """带改动动词的句子是下指令，不是提问：照样给候选计划。"""
    response = post_chat(
        "把趋势图改成按分钟粒度",
        state="USER_EDITING_TEMPLATE",
        snapshots=_snap(definition),
        selectedRegionId="region_hourly_trend",
    ).json()

    assert response["uiActions"] == ["pending_edit_plan"]
    assert response["domainObjects"]["pendingEditPlan"] is not None
    assert response["domainObjects"]["reportDefinitionDraft"]["metadata"]["revision"] == 0


def test_readonly_visualization_question_only_answers(post_chat, definition):
    response = post_chat(
        "设备耗电排名可以怎么展示？",
        state="USER_EDITING_TEMPLATE",
        snapshots=_snap(definition),
    ).json()
    assert "柱状图" in response["reply"]
    assert response["questions"] == []
    assert response["domainObjects"]["pendingEditPlan"] is None
    assert response["domainObjects"]["changeSet"] is None
    assert session_service.load_pending_choice("conv_test") is None


def test_grain_name_does_not_override_followup_choice(post_chat, definition):
    response = post_chat(
        "把小时趋势改成按天粒度",
        state="USER_EDITING_TEMPLATE",
        snapshots=_snap(definition),
    ).json()
    assert response["domainObjects"]["pendingEditPlan"]["operations"][0]["operation"]["grain"]["unit"] == "DAY"


# ---------- Q11：候选 -> 接受 -> 落草稿 ----------


def test_candidate_requires_acceptance_before_draft_changes(post_chat, definition):
    """候选阶段草稿不动；点「接受修改」之后才改，且只改一次。"""
    candidate = post_chat(
        "报表名称改成新日报",
        state="USER_EDITING_TEMPLATE",
        snapshots=_snap(definition),
    ).json()

    assert _accept_options(candidate) == ["接受修改", "继续调整", "取消"]
    assert candidate["domainObjects"]["reportDefinitionDraft"]["metadata"]["name"] == (
        "A 机房能耗日报"
    )

    accepted = post_chat(
        "接受修改",
        state="USER_EDITING_TEMPLATE",
        snapshots=_snap(definition),
    ).json()

    assert accepted["domainObjects"]["reportDefinitionDraft"]["metadata"]["name"] == "新日报"
    assert accepted["domainObjects"]["reportDefinitionDraft"]["metadata"]["revision"] == 1
    assert accepted["domainObjects"]["changeSet"] is not None


def test_duplicate_acceptance_does_not_apply_twice(post_chat, definition):
    """重复点「接受修改」不会改两次。"""
    post_chat(
        "报表名称改成新日报",
        state="USER_EDITING_TEMPLATE",
        snapshots=_snap(definition),
    )
    accepted = post_chat(
        "接受修改",
        state="USER_EDITING_TEMPLATE",
        snapshots=_snap(definition),
    ).json()
    assert accepted["domainObjects"]["reportDefinitionDraft"]["metadata"]["revision"] == 1

    again = post_chat(
        "接受修改",
        state="USER_EDITING_TEMPLATE",
        snapshots=_snap(definition),
    ).json()
    assert again["uiActions"] == ["selection_required"]
    assert session_service.repository.get_session("conv_test").snapshot["definition"]["metadata"]["revision"] == 1


def test_cancelling_candidate_leaves_draft_untouched(post_chat, definition):
    """点「取消」：草稿不变，待决计划清掉。"""
    post_chat(
        "报表名称改成新日报",
        state="USER_EDITING_TEMPLATE",
        snapshots=_snap(definition),
    )
    cancelled = post_chat(
        "取消",
        state="USER_EDITING_TEMPLATE",
        snapshots=_snap(definition),
    ).json()

    assert cancelled["domainObjects"]["reportDefinitionDraft"]["metadata"]["name"] == (
        "A 机房能耗日报"
    )
    assert cancelled["domainObjects"]["reportDefinitionDraft"]["metadata"]["revision"] == 0
    assert cancelled["domainObjects"]["pendingEditPlan"] is None


def test_composite_change_is_applied_atomically(post_chat, definition):
    """换数据源 + 改粒度是一组：接受后一次全改，不做一半。"""
    candidate = post_chat(
        "把趋势图的数据源换成 EAS 能耗明细，并把粒度改成按分钟级",
        state="USER_EDITING_TEMPLATE",
        snapshots=_snap(definition),
        selectedRegionId="region_hourly_trend",
    ).json()

    assert candidate["uiActions"] == ["pending_edit_plan"]
    assert candidate["domainObjects"]["changeSet"] is None

    accepted = post_chat(
        "接受修改",
        state="USER_EDITING_TEMPLATE",
        snapshots=_snap(definition),
        selectedRegionId="region_hourly_trend",
    ).json()

    draft = accepted["domainObjects"]["reportDefinitionDraft"]
    trend = next(item for item in draft["regions"] if item["id"] == "region_hourly_trend")
    dataset_ref = trend["data"]["inputs"]["main"]["datasetRef"]
    assert draft["datasets"][dataset_ref]["sourceRef"] == "eas_energy_detail"
    assert trend["context"]["grain"]["value"]["unit"] == "MINUTE"
    # 一次接受只升一版。
    assert draft["metadata"]["revision"] == 1


def test_invalid_composite_request_never_changes_only_the_source(post_chat, definition):
    candidate = post_chat(
        "把趋势图的数据源换成机房能耗数据，并把粒度改成按分钟级",
        state="USER_EDITING_TEMPLATE",
        snapshots=_snap(definition),
        selectedRegionId="region_hourly_trend",
    ).json()
    assert candidate["domainObjects"]["pendingEditPlan"] is None
    assert candidate["domainObjects"]["changeSet"] is None
    assert session_service.load_pending_plan("conv_test") is None
    assert session_service.repository.get_session("conv_test").snapshot["definition"]["metadata"]["revision"] == 0


def test_fixed_interval_acceptance_survives_validation(post_chat, definition):
    candidate = post_chat(
        "把趋势图改成昨天下午两点到六点",
        state="USER_EDITING_TEMPLATE",
        snapshots=_snap(definition),
        selectedRegionId="region_hourly_trend",
    ).json()
    assert candidate["domainObjects"]["pendingEditPlan"] is not None
    accepted = post_chat(
        "接受修改",
        state="USER_EDITING_TEMPLATE",
        snapshots=_snap(definition),
        selectedRegionId="region_hourly_trend",
    ).json()
    draft = accepted["domainObjects"]["reportDefinitionDraft"]
    window = next(r for r in draft["regions"] if r["id"] == "region_hourly_trend")["context"]["timeWindow"]
    assert window["mode"] == "REPLACE"
    assert window["value"]["type"] == "FIXED_INTERVAL"
    assert window["value"]["timezone"] == "Asia/Shanghai"
    start = datetime.fromisoformat(window["value"]["start"])
    end = datetime.fromisoformat(window["value"]["end"])
    assert end - start == timedelta(hours=4)
    assert (start.hour, end.hour) == (14, 18)
    assert validate_definition(draft).passed


def test_invalid_fixed_interval_is_rejected_by_shape_validation(definition):
    trend = next(r for r in definition.regions if r["id"] == "region_hourly_trend")
    trend["context"]["timeWindow"] = {
        "mode": "REPLACE",
        "value": {
            "type": "FIXED_INTERVAL", "timezone": "Asia/Shanghai",
            "start": "2026-10-21T18:00:00+08:00",
            "end": "2026-10-21T14:00:00+08:00",
        },
    }
    result = validate_definition(definition.model_dump(mode="json"))
    assert not result.passed
    assert any(issue.code == "FIXED_INTERVAL_INVALID" for issue in result.issues)


# ---------- Q17：过期计划必须被拦住 ----------


def test_stale_plan_is_blocked_after_draft_changed(post_chat, definition):
    """草稿版本变了以后，旧计划不能再照旧执行。"""
    post_chat(
        "报表名称改成新日报",
        state="USER_EDITING_TEMPLATE",
        snapshots=_snap(definition),
    )
    accepted = post_chat(
        "接受修改",
        state="USER_EDITING_TEMPLATE",
        snapshots=_snap(definition),
    ).json()
    assert accepted["domainObjects"]["reportDefinitionDraft"]["metadata"]["revision"] == 1

    # 接受后有必选菜单，先明确选择继续修改，再生成第二个计划。
    resumed = post_chat(
        "修改",
        state="USER_EDITING_TEMPLATE",
        snapshots={"reportDefinition": accepted["domainObjects"]["reportDefinitionDraft"]},
    ).json()
    assert resumed["uiActions"] != ["selection_required"]
    candidate = post_chat(
        "把设备耗电排名改成柱状图",
        state="USER_EDITING_TEMPLATE",
        snapshots={
            "reportDefinition": accepted["domainObjects"]["reportDefinitionDraft"]
        },
    ).json()
    assert candidate["domainObjects"]["pendingEditPlan"] is not None
    # 拿旧草稿（revision 0）去接受，版本对不上，必须被拦住。
    stale = post_chat(
        "接受修改",
        state="USER_EDITING_TEMPLATE",
        snapshots=_snap(definition),
    ).json()

    assert stale["error"]["code"] == "STALE_EDIT_PLAN"
    assert session_service.load_pending_plan("conv_test") is None
    assert session_service.repository.get_session("conv_test").snapshot["definition"]["metadata"]["revision"] == 1


# ---------- Q18：追问轮次上限 ----------


def test_same_edit_is_clarified_at_most_three_rounds(post_chat, definition):
    """同一项修改最多追问三轮，之后说清卡点，不猜着改。"""
    replies = []
    for _ in range(4):
        response = post_chat(
            "改一下",
            state="USER_EDITING_TEMPLATE",
            snapshots=_snap(definition),
        ).json()
        replies.append(response["reply"])

    assert "三轮" in replies[-1]
    assert replies[-1] != replies[-2]
