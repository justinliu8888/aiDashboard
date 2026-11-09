"""编辑编排层：追加指标、补充只更新提案、接受后回下一步菜单。

覆盖四件事（与前端无关，后端能作证的那些）：

    1. 追加指标：往已有卡片里**加**指标，原有指标保留；占位 / 非占位
       **记在定义里**（``displayMode``），供渲染层读；
    2. 模拟示例模式下，真实字段准入**让位**：加 PUE 不会再被
       「当前数据源没有这个字段」挡回去；
    3. 用户批准之后同一轮执行完：响应带新草稿、新示意载荷和下一步菜单；
    4. 卡片里的「其他」自由文本只更新待确认提案，不自动修改草稿。
"""
from __future__ import annotations

from typing import Any

import pytest

from app.schemas.edit_plan import EditIntent, EditPlan
from app.services.preview_service import (
    build_preview_context,
    build_report_preview_payload,
)
from app.services.report_draft_service import build_policy_draft
from app.services.report_template_service import build_template_design
from app.services.session_service import session_service
from app.services.template_definition_service import build_template_definition
from app.services.template_edit_engine import EditResult, template_edit_engine
from app.tools.context import ToolScope

SCOPE = ToolScope(
    project_id="project_001",
    subject_ref="machine_room",
    subject_id="room_a",
)

APPEND_MESSAGE = (
    "核心运行指标总览还需要包括总实时功率、运行中的服务器数和告警总数"
)

REGION_ID = "region_1"
REGION_NAME = "核心运行指标总览"


def _plan_document() -> dict[str, Any]:
    return {
        "topic": "A 机房能耗日报",
        "requirements": {
            "metrics": ["总耗电"], "subject": None,
            "timeRange": None, "exclusions": [],
        },
        "sections": [
            {
                "id": "section_total", "title": REGION_NAME,
                "metricIntents": ["总耗电"], "componentType": "metric_card",
            },
        ],
        "assumptions": [], "suggestions": [], "referenceTemplates": [],
    }


@pytest.fixture()
def definition():
    """走真实两跳（方案 → 模板 → 定义）的草稿：区域带 templateBinding。"""
    plan = _plan_document()
    template = build_template_design(plan, subject_id="room_a")
    return build_template_definition(
        template.model_dump(mode="json"), plan, "room_a",
    )


def _snap(definition) -> dict[str, Any]:
    return {"reportDefinition": definition.model_dump(mode="json")}


def _region(definition: dict[str, Any], region_id: str) -> dict[str, Any]:
    return next(
        item for item in definition["regions"] if item["id"] == region_id
    )


def _preview(definition: dict[str, Any]) -> dict[str, Any]:
    context = build_preview_context(definition)
    return build_report_preview_payload(definition, context)


# --------------------------------------------------------------------------
# 1. 追加指标：保留原有指标 + 占位模式记进定义
# --------------------------------------------------------------------------


def test_append_metrics_keeps_the_existing_metric(definition):
    """往卡片里加指标 = **追加**：原有那一项一个字节都不动。"""
    plan = template_edit_engine.parse(
        message=APPEND_MESSAGE,
        definition=definition,
        selected_region_id=None,
        scope=SCOPE,
    )

    assert plan.intent == EditIntent.ADD_METRIC
    assert plan.needsClarification is False
    assert [item.label for item in plan.operation.addMetrics] == [
        "总实时功率", "运行中的服务器数", "告警总数",
    ]

    result = template_edit_engine.apply(
        plan=plan, definition=definition, scope=SCOPE,
    )

    assert result.needsClarification is False
    before = _region(definition.model_dump(mode="json"), REGION_ID)
    after = _region(result.draftAfterEdit.model_dump(mode="json"), REGION_ID)
    # 原有指标还在（指标卡本体没动）。
    assert after["component"]["metric"] == before["component"]["metric"]
    # 新指标追加在后面。
    added = after["component"]["columns"]
    assert [item["label"] for item in added] == [
        "总实时功率", "运行中的服务器数", "告警总数",
    ]
    # 列的形状是渲染层约定的那一份。
    for column in added:
        assert set(column) == {
            "fieldRef", "label", "format", "displayMode", "bindingKind",
        }
        assert column["bindingKind"] == "simulated"
        assert column["displayMode"] == "simulated"
        assert "unitLabel" in column["format"]
    # 两处都要有：region 的列与 previewSpec 的列。
    spec = after["templateBinding"]["previewSpec"]
    assert spec["columns"] == added
    # 原有那一项的描述也还在（追加不是替换）。
    assert spec["metrics"][0]["label"] == "总耗电"


def test_placeholder_mode_is_recorded_in_the_definition(definition):
    """用户明确要「先占位」→ 新指标只显示名称，不塞数值（记在定义里）。"""
    plan = template_edit_engine.parse(
        message=APPEND_MESSAGE,
        definition=definition,
        selected_region_id=None,
        scope=SCOPE,
    )
    for addition in plan.operation.addMetrics:
        addition.displayMode = "placeholder_only"

    result = template_edit_engine.apply(
        plan=plan, definition=definition, scope=SCOPE,
    )

    assert result.needsClarification is False
    after = _region(result.draftAfterEdit.model_dump(mode="json"), REGION_ID)
    assert all(
        column["displayMode"] == "placeholder_only"
        for column in after["component"]["columns"]
    )
    assert all(
        column["displayMode"] == "placeholder_only"
        for column in after["templateBinding"]["previewSpec"]["columns"]
    )


def test_placeholder_only_applies_to_the_new_metrics(definition):
    """占位只影响**新加**的那些：原有指标照常出数。"""
    plan = template_edit_engine.parse(
        message=APPEND_MESSAGE,
        definition=definition,
        selected_region_id=None,
        scope=SCOPE,
    )
    for addition in plan.operation.addMetrics:
        addition.displayMode = "placeholder_only"
    result = template_edit_engine.apply(
        plan=plan, definition=definition, scope=SCOPE,
    )

    payload = _preview(result.draftAfterEdit.model_dump(mode="json"))
    region = next(
        item for item in payload["regions"] if item["regionId"] == REGION_ID
    )["payload"]
    values = {
        item["label"]: item.get("value") for item in region.get("metrics") or []
    }
    placeholders = {
        item["label"]: item.get("placeholderOnly")
        for item in region.get("metrics") or []
    }
    # 原有指标照常出数。
    assert values["总耗电"] is not None
    assert placeholders["总耗电"] is not True
    # 新加的那几个只显示名称。
    for label in ("总实时功率", "运行中的服务器数", "告警总数"):
        assert values[label] is None
        assert placeholders[label] is True


def test_appending_several_metrics_is_atomic(definition):
    """一次追加多个：全部成功才生效；任一步失败则整体不生效。"""
    plan = template_edit_engine.parse(
        message=APPEND_MESSAGE,
        definition=definition,
        selected_region_id=None,
        scope=SCOPE,
    )
    # 认不出来的指标 → 整批不生效，原草稿不变。
    plan.operation.addMetrics[1].label = ""
    plan.operation.addMetrics[1].intent = ""
    plan.operation.addMetrics[1].metricCandidate = None
    plan.operation.addMetrics[1].metricRef = None

    result = template_edit_engine.apply(
        plan=plan, definition=definition, scope=SCOPE,
    )

    assert result.needsClarification is True
    assert result.draftAfterEdit.model_dump(mode="json") == definition.model_dump(mode="json")


# --------------------------------------------------------------------------
# 2. 模拟示例模式：真实字段准入让位
# --------------------------------------------------------------------------


def test_adding_pue_is_not_blocked_by_the_real_field_gate(definition):
    """加 PUE 不再被「当前数据源没有这个字段」挡回去。"""
    plan = template_edit_engine.parse(
        message="加 PUE", definition=definition, selected_region_id=None, scope=SCOPE,
    )
    assert plan.intent == EditIntent.ADD_SECTION

    result = template_edit_engine.apply(
        plan=plan, definition=definition, scope=SCOPE,
    )

    assert result.needsClarification is False, result.clarificationQuestion
    assert result.changeSet is not None
    assert "请先选择兼容数据源" not in result.reply
    # 目录外指标**不伪造** metricRef。
    assert plan.operation.metricRef is None
    assert plan.operation.metricIntent == "PUE"


def test_a_registered_metric_without_a_field_falls_back_to_a_virtual_one(definition):
    """目录里有、但当前数据源没这个字段的指标 → 降级成虚拟指标，不拒绝。"""
    plan = EditPlan.model_validate({
        "isTemplateEdit": True,
        "intent": "ADD_SECTION",
        "confidence": 0.9,
        "target": {"regionId": "", "matchedText": "", "confidence": 0.0},
        "operation": {
            "sectionTitle": "PUE",
            "metricRef": "metric.machine_room.pue",
            "metricVersion": "1.0",
            "sourceRef": "catalog.machine_room.energy",
        },
    })

    result = template_edit_engine.apply(
        plan=plan, definition=definition, scope=SCOPE,
    )

    assert result.needsClarification is False, result.clarificationQuestion
    assert plan.operation.metricRef is None
    assert plan.operation.metricIntent == "PUE"


# --------------------------------------------------------------------------
# 3. 批准之后：同一轮执行完，返回下一步菜单
# --------------------------------------------------------------------------


def test_accepting_a_plan_returns_the_new_draft_and_next_action_menu(
    post_chat, definition,
):
    """接受只批准修改，下一步菜单决定是否确认整份草稿。"""
    candidate = post_chat(
        APPEND_MESSAGE,
        state="USER_EDITING_TEMPLATE",
        snapshots=_snap(definition),
    ).json()

    assert candidate["uiActions"] == ["pending_edit_plan"]
    menu = candidate["questions"][0]
    assert menu["id"] == "pending_edit_plan_confirm"
    # 这张卡片提供「其他」：用户在卡片里写纠正 = 回答这张卡。
    assert menu["allowsOther"] is True
    assert menu["otherLabel"] == "其他"
    assert menu["lockScope"] == "input_and_menu"
    # 建议文字说清楚：哪张卡片、哪几个指标、什么展示形式、统计哪段时间。
    assert "追加" in candidate["reply"]
    assert "总实时功率" in candidate["reply"]
    assert REGION_NAME in candidate["reply"]

    accepted = post_chat(
        "接受修改",
        state="USER_EDITING_TEMPLATE",
        snapshots=_snap(definition),
    ).json()

    assert accepted["state"] == "USER_EDITING_TEMPLATE"
    # 新草稿已经落到响应里。
    region = _region(
        accepted["domainObjects"]["reportDefinitionDraft"], REGION_ID,
    )
    assert [item["label"] for item in region["component"]["columns"]] == [
        "总实时功率", "运行中的服务器数", "告警总数",
    ]
    # 新示意载荷也带回来了（右侧靠它刷新）。
    assert accepted["reportPreviewPayload"] is not None
    # 这是下一步菜单，不是对刚接受的修改重复批准。
    assert accepted["questions"][0]["id"] == "template_edit_confirm"
    assert accepted["questions"][0]["prompt"] == "请选择下一步操作："
    assert accepted["questions"][0]["allowsOther"] is True
    assert "confirm_template_edit" not in accepted["uiActions"]


# --------------------------------------------------------------------------
# 4. 卡片里的「其他」= 对当前问题的纠正（同一个待办）
# --------------------------------------------------------------------------


@pytest.mark.parametrize("correction", ["先占位，只显示名称", "这批指标先占位，只显示名称就行"])
def test_other_text_updates_proposal_without_applying(post_chat, definition, correction):
    """占位补充只更新提案，接受之后才改草稿。"""
    candidate = post_chat(
        APPEND_MESSAGE,
        state="USER_EDITING_TEMPLATE",
        snapshots=_snap(definition),
    ).json()
    assert candidate["domainObjects"]["pendingEditPlan"] is not None

    corrected = post_chat(
        correction,
        state="USER_EDITING_TEMPLATE",
        snapshots=_snap(definition),
    ).json()

    assert corrected["state"] == "USER_EDITING_TEMPLATE"
    region = _region(
        corrected["domainObjects"]["reportDefinitionDraft"], REGION_ID,
    )
    assert not region["component"].get("columns")
    assert corrected["domainObjects"]["reportDefinitionDraft"] == definition.model_dump(mode="json")
    assert corrected["domainObjects"]["changeSet"] is None
    assert corrected["reportPreviewPayload"] is None
    assert corrected["domainObjects"]["pendingEditPlan"] is not None
    assert corrected["domainObjects"]["pendingEditPlan"]["planId"] != candidate["domainObjects"]["pendingEditPlan"]["planId"]
    assert "只显示名称" in corrected["reply"]
    assert [item["value"] for item in corrected["questions"][0]["options"]] == [
        "接受修改", "撤回上一步", "继续修改",
    ]
    assert session_service.load_pending_plan("conv_test") is not None
    accepted = post_chat(
        "接受修改", state="USER_EDITING_TEMPLATE", snapshots=_snap(definition),
    ).json()
    region = _region(accepted["domainObjects"]["reportDefinitionDraft"], REGION_ID)
    assert all(
        column["displayMode"] == "placeholder_only"
        for column in region["component"]["columns"]
    )
    assert accepted["reportPreviewPayload"] is not None
    preview = next(
        item for item in accepted["reportPreviewPayload"]["regions"]
        if item["regionId"] == REGION_ID
    )["payload"]
    values = {item["label"]: item.get("value") for item in preview["metrics"]}
    assert values["总耗电"] is not None
    for label in ("总实时功率", "运行中的服务器数", "告警总数"):
        assert values[label] is None
    assert accepted["questions"][0]["id"] == "template_edit_confirm"
    assert accepted["domainObjects"]["reportDefinitionDraft"]["metadata"]["revision"] == 1
    assert accepted["domainObjects"]["pendingEditPlan"] is None
    assert session_service.load_pending_choice("conv_test")["question"]["id"] == "template_edit_confirm"
    assert session_service.load_pending_plan("conv_test") is None


def test_next_action_other_input_enters_editing(post_chat, definition):
    post_chat(APPEND_MESSAGE, state="USER_EDITING_TEMPLATE", snapshots=_snap(definition))
    accepted = post_chat("接受修改", state="USER_EDITING_TEMPLATE", snapshots=_snap(definition)).json()
    draft = accepted["domainObjects"]["reportDefinitionDraft"]
    candidate = post_chat(
        "核心运行指标总览再加 平均温度", state="USER_EDITING_TEMPLATE",
        snapshots={"reportDefinition": draft},
    ).json()
    assert candidate["uiActions"] == ["pending_edit_plan"]
    assert candidate["domainObjects"]["pendingEditPlan"] is not None
    assert candidate["domainObjects"]["reportDefinitionDraft"] == draft


def test_withdraw_discards_pending_proposal_not_draft(post_chat, definition):
    post_chat(APPEND_MESSAGE, state="USER_EDITING_TEMPLATE", snapshots=_snap(definition))
    post_chat("先占位，只显示名称", state="USER_EDITING_TEMPLATE", snapshots=_snap(definition))
    withdrawn = post_chat("撤回上一步", state="USER_EDITING_TEMPLATE", snapshots=_snap(definition)).json()
    assert withdrawn["domainObjects"]["reportDefinitionDraft"] == definition.model_dump(mode="json")
    assert withdrawn["domainObjects"]["changeSet"] is None
    assert session_service.load_pending_plan("conv_test") is None


def test_continue_modifying_does_not_apply_proposal(post_chat, definition):
    post_chat(APPEND_MESSAGE, state="USER_EDITING_TEMPLATE", snapshots=_snap(definition))
    continued = post_chat("继续修改", state="USER_EDITING_TEMPLATE", snapshots=_snap(definition)).json()
    assert continued["domainObjects"]["reportDefinitionDraft"] == definition.model_dump(mode="json")
    assert continued["domainObjects"]["changeSet"] is None
    assert continued["questions"][0]["id"] == "template_edit_action"
    assert session_service.load_pending_plan("conv_test") is None


@pytest.mark.parametrize("correction", ["再加 平均温度", "带数值"])
def test_other_supplements_never_call_apply(post_chat, definition, monkeypatch, correction):
    post_chat(APPEND_MESSAGE, state="USER_EDITING_TEMPLATE", snapshots=_snap(definition))

    def forbidden_apply(**kwargs):
        pytest.fail("补充提案不得执行 apply")

    monkeypatch.setattr(template_edit_engine, "apply", forbidden_apply)
    corrected = post_chat(correction, state="USER_EDITING_TEMPLATE", snapshots=_snap(definition)).json()
    assert corrected["uiActions"] == ["pending_edit_plan"]
    assert corrected["domainObjects"]["reportDefinitionDraft"] == definition.model_dump(mode="json")
    assert session_service.repository.get_session("conv_test").snapshot["definition"] == definition.model_dump(mode="json")
    pending = session_service.load_pending_plan("conv_test")
    assert pending is not None
    if correction.startswith("再加"):
        assert "平均温度" in corrected["reply"]
        assert len(pending.model_extra["editPlan"]["operation"]["addMetrics"]) == 4


def test_stale_supplement_does_not_change_the_draft(post_chat, definition):
    post_chat(APPEND_MESSAGE, state="USER_EDITING_TEMPLATE", snapshots=_snap(definition))
    stale = definition.model_copy(deep=True)
    stale.metadata["revision"] = 7
    corrected = post_chat(
        "先占位，只显示名称", state="USER_EDITING_TEMPLATE", snapshots=_snap(stale),
    ).json()
    assert corrected["error"]["code"] == "STALE_EDIT_PLAN"
    assert corrected["domainObjects"]["reportDefinitionDraft"] == definition.model_dump(mode="json")
    assert session_service.load_pending_plan("conv_test") is None


def test_append_preview_is_virtual_except_explicit_name_placeholders(post_chat, definition):
    post_chat(APPEND_MESSAGE, state="USER_EDITING_TEMPLATE", snapshots=_snap(definition))
    accepted = post_chat("接受修改", state="USER_EDITING_TEMPLATE", snapshots=_snap(definition)).json()
    region = _region(accepted["domainObjects"]["reportDefinitionDraft"], REGION_ID)
    additions = region["templateBinding"]["previewSpec"]["metrics"][1:]
    assert additions
    assert all(item["capabilityPending"] is False for item in additions)
    assert all("待接通" not in item["pendingReason"] for item in additions)
    preview = next(item for item in accepted["reportPreviewPayload"]["regions"] if item["regionId"] == REGION_ID)
    assert preview["dataSource"] == "simulated"
    values = {item["label"]: item.get("value") for item in preview["payload"]["metrics"]}
    for label in ("总实时功率", "运行中的服务器数", "告警总数"):
        assert values[label] is not None


@pytest.mark.parametrize("message", ["加 PUE", "把核心运行指标总览的指标换成冰箱温度"])
def test_virtual_metric_edits_remove_real_connection_wording(post_chat, definition, message):
    candidate = post_chat(message, state="USER_EDITING_TEMPLATE", snapshots=_snap(definition)).json()
    assert candidate["domainObjects"]["pendingEditPlan"] is not None
    accepted = post_chat("接受修改", state="USER_EDITING_TEMPLATE", snapshots=_snap(definition)).json()
    assert accepted.get("error") is None
    changed_id = accepted["domainObjects"]["changeSet"]["changed"][0]["regionId"]
    region = _region(accepted["domainObjects"]["reportDefinitionDraft"], changed_id)
    binding = region["templateBinding"]
    for text in (accepted["reply"], binding["placeholderReason"], binding["pendingReason"]):
        assert "待接通" not in text
        assert "接不到真实数据" not in text
        assert "接入真实数据" not in text
    assert binding["previewSpec"]["capabilityPending"] is False
    assert all(item["capabilityPending"] is False for item in binding["previewSpec"]["metrics"])
    preview = next(item for item in accepted["reportPreviewPayload"]["regions"] if item["regionId"] == changed_id)
    assert preview["dataSource"] == "simulated"
    assert preview["capabilityPending"] is False


def test_a_readonly_question_during_a_pending_plan_is_not_a_correction(
    post_chat, definition,
):
    """提问不是纠正：照旧走只读查询，待确认计划原样留着。"""
    post_chat(
        APPEND_MESSAGE,
        state="USER_EDITING_TEMPLATE",
        snapshots=_snap(definition),
    )

    asked = post_chat(
        "你有未完成的草稿吗？",
        state="USER_EDITING_TEMPLATE",
        snapshots=_snap(definition),
    ).json()

    assert asked["turnKind"] == "query_answer"
    assert asked["domainObjects"]["pendingEditPlan"] is None
    assert asked["domainObjects"]["reportDefinitionDraft"]["metadata"]["revision"] == 0


# --------------------------------------------------------------------------
# 5. 保存不占用草稿自己的版本号
# --------------------------------------------------------------------------


def test_saving_does_not_take_over_the_draft_revision(post_chat, definition):
    """改两次 → 保存 → 第三次编辑**仍然被接受**。

    草稿自己的版本号（``metadata.revision``）是编辑链的版本：每改一次
    +1。保存次数是另一套计数器 —— 两者混用会把版本号拽回去，下一次编辑
    就被当成旧草稿拦掉。
    """
    policy = build_policy_draft("A 机房能耗日报").model_dump(mode="json")

    # 第一次编辑
    post_chat(
        APPEND_MESSAGE, state="USER_EDITING_TEMPLATE", snapshots=_snap(definition),
    )
    first = post_chat(
        "接受修改", state="USER_EDITING_TEMPLATE", snapshots=_snap(definition),
    ).json()
    draft1 = first["domainObjects"]["reportDefinitionDraft"]
    assert draft1["metadata"]["revision"] == 1

    # 第二次编辑
    post_chat(
        "核心运行指标总览再加 平均温度",
        state="USER_EDITING_TEMPLATE",
        snapshots={"reportDefinition": draft1},
    )
    second = post_chat(
        "接受修改",
        state="USER_EDITING_TEMPLATE",
        snapshots={"reportDefinition": draft1},
    ).json()
    draft2 = second["domainObjects"]["reportDefinitionDraft"]
    assert draft2["metadata"]["revision"] == 2

    # 保存
    saved = post_chat(
        "同意",
        state="USER_EDITING_TEMPLATE",
        snapshots={
            "reportPolicy": policy,
            "reportDefinition": draft2,
        },
    ).json()
    # 第二批新契约：保存走 save_definition_draft 工具请求，
    # 会话状态停在 ANALYZING（后续推进由前端驱动）。
    assert saved["state"] == "ANALYZING"
    assert "save_definition_draft" in saved["uiActions"]
    saved_draft = saved["domainObjects"]["reportDefinitionDraft"]
    # 版本号没被保存次数占用：保存次数另记一个字段。
    assert saved_draft["metadata"]["revision"] == 2
    assert saved_draft["metadata"]["savedRevision"] == 1

    # 第三次编辑：仍然被接受（没被当成旧草稿拦掉），版本号接着往上走。
    candidate = post_chat(
        "核心运行指标总览再加 湿度",
        state="USER_EDITING_TEMPLATE",
        snapshots={"reportDefinition": saved_draft},
    ).json()
    assert candidate.get("error") is None
    assert candidate["domainObjects"]["pendingEditPlan"] is not None

    third = post_chat(
        "接受修改",
        state="USER_EDITING_TEMPLATE",
        snapshots={"reportDefinition": saved_draft},
    ).json()
    assert third.get("error") is None
    assert third["domainObjects"]["reportDefinitionDraft"]["metadata"]["revision"] == 3


def test_the_save_reply_does_not_expose_internal_identifiers(post_chat, definition):
    """保存那一轮的回话是**业务语言**：不出现定义 id / 版本号。"""
    policy = build_policy_draft("A 机房能耗日报").model_dump(mode="json")

    saved = post_chat(
        "同意",
        state="USER_EDITING_TEMPLATE",
        snapshots={
            "reportPolicy": policy,
            "reportDefinition": definition.model_dump(mode="json"),
        },
    ).json()

    reply = saved["reply"]
    assert "revision" not in reply
    assert "def_" not in reply
    assert "草稿" in reply
    # 内部标识仍然在 toolRequests 里（前端要用得到）。
    assert saved["toolRequests"][0]["arguments"]["definitionId"]


# --------------------------------------------------------------------------
# 6. 接受时校验失败 → 给一张新的追问卡
# --------------------------------------------------------------------------


def test_accepting_a_corrected_plan_that_fails_validation_gives_a_new_card(
    post_chat, definition, monkeypatch,
):
    """补充不执行；接受后校验失败时作废旧计划并追问。"""
    post_chat(
        APPEND_MESSAGE, state="USER_EDITING_TEMPLATE", snapshots=_snap(definition),
    )
    assert session_service.load_pending_choice("conv_test") is not None

    def _still_needs_more(*, plan, definition, scope):
        return EditResult(
            plan=plan,
            draftBeforeEdit=definition,
            draftAfterEdit=definition,
            needsClarification=True,
            clarificationQuestion="这次改动的目标还不明确，请再说一下要改哪一处。",
        )

    monkeypatch.setattr(template_edit_engine, "apply", _still_needs_more)

    corrected = post_chat(
        "这批指标先占位，只显示名称就行",
        state="USER_EDITING_TEMPLATE",
        snapshots=_snap(definition),
    ).json()
    assert corrected["questions"][0]["id"] == "pending_edit_plan_confirm"
    assert session_service.load_pending_plan("conv_test") is not None
    corrected = post_chat(
        "接受修改", state="USER_EDITING_TEMPLATE", snapshots=_snap(definition),
    ).json()

    questions = corrected["questions"]
    assert questions, "追问要给一张卡，不能只回一句话"
    # 新卡不是原来那张「确认修改」卡：接受失败后给回**草稿级合法菜单**
    # （同意 / 修改 / 重新开始）—— 用户有明确、合法的下一步。
    assert questions[0]["id"] != "pending_edit_plan_confirm"
    assert questions[0]["id"] == "template_edit_confirm"
    assert [o["value"] for o in questions[0]["options"]] == ["同意", "修改", "重新开始"]
    # 旧卡作废：原来的「确认修改」计划消费掉了，待决项换成**新的合法菜单**
    # （不是没有待决项 —— 用户必须有明确的下一步）。
    assert session_service.load_pending_plan("conv_test") is None
    pending = session_service.load_pending_choice("conv_test")
    assert pending is not None
    assert pending["question"]["id"] == "template_edit_confirm"
    # 草稿没有被动过。
    assert corrected["domainObjects"]["reportDefinitionDraft"]["metadata"]["revision"] == 0
