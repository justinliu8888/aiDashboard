"""工单 #66 D：修 C2 集成验收查出的两条缺陷。

- **F-2**：目录外**新增**（以及换指标成目录外）的区域，
  ``templateBinding.pendingReason`` 写死 ``None`` —— 区域级占位文案是空的，
  而同一块区域的指标级文案却在（"能力待接通：…"），两处口径不一致。
  修法：复用模板层的**唯一**出处
  ``report_template_service.pending_reason(mode.mode)``，两种来源模式的
  文案**逐字不改**。

- **F-3**：编辑态「接受修改」那一轮响应里 ``reportPreviewPayload`` 是
  ``null`` → 前端 ``applyReportPreviewPayload`` 收到 null，右侧保持旧载荷
  不刷新。修法：复用设计轮**同一个**载荷生成入口
  ``report_design_service._attach_report_preview_payload``（内部走
  ``preview_service.build_report_preview_payload``），按**新定义**生成，
  **只多挂这一个字段**，载荷**不落库**。

测试分两层：

- **服务层**（编辑引擎真实链路）：F-2 的四种口径 + 指标级未变 +
  方案侧回归保护；
- **接口层**（真实 ``POST /api/agent/chat``、真实编排、真实服务、独立临时
  SQLite，只替换外部模型调用，见 ``tests/support/acceptance_harness.py``）：
  F-3 的载荷非 null / 含新区域 / 与读会话载荷一致 / 没有多出字段 / 不落库。
"""
from __future__ import annotations

import json
from typing import Any

from app.schemas.agent import AgentResponse
from app.services.report_draft_service import build_definition_draft
from app.services.report_template_service import (
    PENDING_REASON_SIMULATED,
    PENDING_REASON_USER_JSON,
    build_template_design,
    pending_reason,
)
from app.services.template_edit_engine import template_edit_engine
from app.tools.context import ToolScope
from tests.support.acceptance_harness import (
    ScriptedModel,
    acceptance_harness,
    plan_candidate,
)
from tests.support.step_contract import step_ref


SCOPE = ToolScope(
    project_id="project_001", subject_ref="machine_room", subject_id="room_a"
)

#: 模拟示例模式下的既有文案（ADR 0004）。**逐字**照抄 —— 用来钉住「没被改」。
SIMULATED_PENDING_REASON = PENDING_REASON_SIMULATED
#: 用户 JSON 模式下的既有文案（工单 #66 F）。同样逐字照抄。
USER_JSON_PENDING_REASON = PENDING_REASON_USER_JSON

#: 用户 JSON 模式下**不允许**出现的宣称（工单 #66 的验收标准）。
FORBIDDEN_IN_USER_JSON = (
    "模拟业务数据",
    "接入真实数据后自动替换",
    "已连接真实业务",
    "已接入真实业务",
    "真实业务库",
)

#: 指标级占位原因的前缀（目录外指标）。**一个字都不许改**。
OUT_OF_CATALOG_REASON_PREFIX = "能力待接通："


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------


def _definition():
    return build_definition_draft("A 机房能耗日报", "room_a")


def _apply(definition, message: str, selected: str | None = None):
    plan = template_edit_engine.parse(
        message=message,
        definition=definition,
        selected_region_id=selected,
        scope=SCOPE,
    )
    return template_edit_engine.apply(
        plan=plan, definition=definition, scope=SCOPE
    )


def _user_json_metadata() -> dict[str, Any]:
    """一份**用户 JSON 模式**的 metadata（#64 写进来的样子）。"""
    return {
        "sourceMode": "user_json",
        "effectiveCombination": {
            "planId": "rplan_66d",
            "planRevision": 1,
            "definitionId": "rdef_66d",
            "definitionVersion": 1,
            "sourceMode": "user_json",
            "inputId": "json_input_66d",
            "inputVersion": 1,
            "fieldMapping": [],
            "processingRules": [],
        },
    }


def _added_region(result, before_ids: set[str]) -> dict[str, Any]:
    """接受之后**新出现**的那块区域（目录外新增的那一块）。"""
    regions = result.draftAfterEdit.model_dump(mode="json")["regions"]
    added = [
        region for region in regions if region.get("id") not in before_ids
    ]
    assert added, regions
    return added[0]


def _region_pending_reason(region: dict[str, Any]) -> Any:
    return (region.get("templateBinding") or {}).get("pendingReason")


def _metric_pending_reasons(region: dict[str, Any]) -> list[Any]:
    binding = region.get("templateBinding") or {}
    metrics = (binding.get("previewSpec") or {}).get("metrics") or []
    return [
        metric.get("pendingReason")
        for metric in metrics
        if isinstance(metric, dict)
    ]


def _json_rows() -> str:
    return json.dumps(
        [
            {"ts": "2024-01-01", "device": "机柜1", "energy": 1.5},
            {"ts": "2024-01-02", "device": "机柜2", "energy": 2.5},
        ],
        ensure_ascii=False,
    )


def _plan() -> dict[str, Any]:
    return {
        "id": "rplan_66d", "revision": 1, "topic": "A 机房能耗日报",
        "requirements": {
            "metrics": ["总耗电"], "subject": "A 机房",
            "timeRange": "最近 7 天", "exclusions": [],
        },
        "sections": [
            {"id": "section_total", "title": "总耗电概览",
             "metricIntents": ["总耗电"], "componentType": "metric_card"},
        ],
        "assumptions": [], "suggestions": [], "referenceTemplates": [],
    }


# --------------------------------------------------------------------------
# F-2：目录外新增区域的区域级 pendingReason
# --------------------------------------------------------------------------


def test_added_out_of_catalog_region_carries_the_region_level_pending_reason():
    """目录外新增区域：区域级 ``pendingReason`` **逐字**等于既有那句。

    修前这里是 ``None``（C2 F-2）—— 区域级占位文案是空的。
    """
    definition = _definition()
    before_ids = {region["id"] for region in definition.model_dump(mode="json")["regions"]}

    result = _apply(definition, "加个冰箱明细")
    added = _added_region(result, before_ids)

    assert _region_pending_reason(added) == SIMULATED_PENDING_REASON


def test_added_out_of_catalog_region_pending_reason_comes_from_the_single_source():
    """区域级文案**只有一个出处**：模板层的 ``pending_reason(mode)``。"""
    definition = _definition()
    before_ids = {region["id"] for region in definition.model_dump(mode="json")["regions"]}

    result = _apply(definition, "加个冰箱明细")
    added = _added_region(result, before_ids)

    binding = added["templateBinding"]
    assert binding["sourceMode"] == "simulated"
    assert _region_pending_reason(added) == pending_reason(binding["sourceMode"])


def test_added_out_of_catalog_region_in_user_json_mode_says_user_provided_data():
    """用户 JSON 模式：区域级说「用户提供的数据，未经业务核验」，不说模拟数据。"""
    definition = _definition()
    metadata = dict(definition.metadata or {})
    metadata.update(_user_json_metadata())
    definition.metadata = metadata
    before_ids = {region["id"] for region in definition.model_dump(mode="json")["regions"]}

    result = _apply(definition, "加个冰箱明细")
    added = _added_region(result, before_ids)

    reason = _region_pending_reason(added)
    assert reason == USER_JSON_PENDING_REASON, reason
    for forbidden in FORBIDDEN_IN_USER_JSON:
        assert forbidden not in reason, reason


def test_changed_metric_to_out_of_catalog_also_carries_the_region_level_reason():
    """**换指标**成目录外指标同样要带区域级文案（另一处写死 None 的地方）。"""
    definition = _definition()
    before_ids = {region["id"] for region in definition.model_dump(mode="json")["regions"]}

    result = _apply(
        definition,
        "把设备耗电排名的指标换成电脑耗电率 耗电量",
        selected="region_device_ranking",
    )
    assert result.needsClarification is False
    updated = result.draftAfterEdit.model_dump(mode="json")
    target = next(
        region for region in updated["regions"]
        if region["id"] == "region_device_ranking"
    )

    assert _region_pending_reason(target) == SIMULATED_PENDING_REASON


def test_metric_level_pending_reason_is_not_touched():
    """**指标级** ``previewSpec.metrics[].pendingReason`` 一个字都没改。"""
    definition = _definition()
    before_ids = {region["id"] for region in definition.model_dump(mode="json")["regions"]}

    result = _apply(definition, "加个冰箱明细")
    added = _added_region(result, before_ids)

    reasons = _metric_pending_reasons(added)
    assert reasons, added["templateBinding"]["previewSpec"]
    for reason in reasons:
        assert reason.startswith(OUT_OF_CATALOG_REASON_PREFIX), reason
    # 指标级说的是「能力待接通」，**不是**区域级那句 —— 两处口径不同、各说各的。
    assert SIMULATED_PENDING_REASON not in reasons


def test_existing_regions_keep_their_pending_reason_after_an_edit():
    """编辑不动已有区域的区域级文案。"""
    definition = _definition()
    before = {
        region["id"]: _region_pending_reason(region)
        for region in definition.model_dump(mode="json")["regions"]
    }

    result = _apply(definition, "加个冰箱明细")
    after = {
        region["id"]: _region_pending_reason(region)
        for region in result.draftAfterEdit.model_dump(mode="json")["regions"]
    }

    for region_id, reason in before.items():
        assert after[region_id] == reason, (region_id, reason, after[region_id])


def test_template_side_region_pending_reason_is_verbatim_unchanged():
    """方案侧（模板层）区域的区域级文案**逐字**未改 —— 回归保护。"""
    design = build_template_design(_plan(), subject_id="room_a")
    reasons = [
        (region.get("data") or {}).get("pendingReason")
        for region in design.model_dump(mode="json")["regions"]
    ]
    reasons = [reason for reason in reasons if reason]

    assert reasons, design.model_dump(mode="json")["regions"]
    assert reasons == [SIMULATED_PENDING_REASON], reasons


def test_template_side_region_pending_reason_in_user_json_mode_is_unchanged():
    """方案侧用户 JSON 模式的区域级文案也**逐字**未改 —— 回归保护。"""
    design = build_template_design(
        _plan(), subject_id="room_a", source_mode="user_json"
    )
    reasons = [
        (region.get("data") or {}).get("pendingReason")
        for region in design.model_dump(mode="json")["regions"]
    ]
    reasons = [reason for reason in reasons if reason]

    assert reasons, design.model_dump(mode="json")["regions"]
    for reason in reasons:
        assert reason == USER_JSON_PENDING_REASON, reason


# --------------------------------------------------------------------------
# F-3：编辑「接受修改」那一轮带上示意载荷（接口层真实链路）
# --------------------------------------------------------------------------


def _drive_to_edit_accept(harness, *, user_json: bool = False) -> tuple[dict[str, Any], dict[str, Any]]:
    """走真实链路到「接受修改」那一轮，返回 ``(确认方案那轮, 接受修改那轮)``。

    出方案 → 确认方案 → 点「修改」→ 提目录外新增 → 点「接受修改」。
    """
    if user_json:
        plan_payload = harness.send(f"这是昨天的数据 {_json_rows()}")
    else:
        plan_payload = harness.send("生成 A 机房最近 7 天能耗报表")

    confirmed = harness.confirm_plan(plan_payload["domainObjects"]["reportPlan"])
    # 点「修改」进入**受限修改输入**这一步：服务端持久记下步骤身份，
    # 下一轮提交必须带上它（权威步骤契约）。
    modify = harness.send("修改", state="WAITING_USER_CONFIRM")
    candidate = harness.send(
        "加个冰箱明细", state="USER_EDITING_TEMPLATE", context=step_ref(modify),
    )
    assert (candidate.get("domainObjects") or {}).get("pendingEditPlan") is not None

    accepted = harness.send("接受修改", state="USER_EDITING_TEMPLATE")
    return confirmed, accepted


def test_edit_accept_response_carries_the_preview_payload_with_the_new_region():
    """「接受修改」那一轮：载荷**非 null**，且**含新增的那块区域**。"""
    with acceptance_harness(model=ScriptedModel(plan_candidate())) as harness:
        _confirmed, accepted = _drive_to_edit_accept(harness)

        payload = accepted.get("reportPreviewPayload")
        assert payload is not None, accepted
        region_ids = [region["regionId"] for region in payload["regions"]]
        assert len(region_ids) == 2, region_ids
        assert any("冰箱" in region_id for region_id in region_ids), region_ids
        # 新增那块区域真的**出了数**（不是空壳卡片）。
        added = next(
            region for region in payload["regions"]
            if "冰箱" in region["regionId"]
        )
        assert added["payload"]["rows"], added


def test_edit_accept_payload_matches_the_restored_session_payload():
    """这一轮的载荷与**读会话**时重新生成的载荷**完全一致**。

    刷新页面看到的右侧，和接受修改当轮看到的右侧，是同一份数字。
    """
    with acceptance_harness(model=ScriptedModel(plan_candidate())) as harness:
        _confirmed, accepted = _drive_to_edit_accept(harness)
        restored = (harness.read_session().get("snapshot") or {}).get(
            "reportPreviewPayload"
        )

    assert restored is not None
    assert accepted["reportPreviewPayload"] == restored


def test_edit_accept_payload_shares_the_design_round_context():
    """载荷的 ``previewContext`` 与设计轮**同一份**（定义身份没变 → 不误杀）。

    前端 ``isStaleReportPayload`` 用
    ``binding.definitionVersion ?? meta.revision`` 校对；
    后端 ``resolve_preview_context`` 用 ``binding.definitionVersion``。
    两边同源同值 → 这份载荷不会被当成过期版本丢掉。
    """
    with acceptance_harness(model=ScriptedModel(plan_candidate())) as harness:
        confirmed, accepted = _drive_to_edit_accept(harness)

        design_context = (confirmed.get("reportPreviewPayload") or {}).get(
            "previewContext"
        ) or {}
        accept_context = (accepted.get("reportPreviewPayload") or {}).get(
            "previewContext"
        ) or {}
        stored = harness.persisted_snapshot()
        binding = (
            ((stored.get("definition") or {}).get("metadata") or {})
            .get("templateBinding")
            or {}
        )

    assert design_context, confirmed.get("reportPreviewPayload")
    assert accept_context, accepted.get("reportPreviewPayload")
    # 定义身份：两轮一致，且与库里登记的那一份一致。
    assert accept_context["definitionId"] == design_context["definitionId"]
    assert accept_context["definitionVersion"] == design_context["definitionVersion"]
    assert accept_context["definitionId"] == binding["definitionId"]
    assert accept_context["definitionVersion"] == binding["definitionVersion"]
    # 时间窗也复用同一份（跨轮数字不跳）。
    assert accept_context["windowStart"] == design_context["windowStart"]
    assert accept_context["windowEnd"] == design_context["windowEnd"]


def test_edit_accept_response_carries_no_extra_top_level_field():
    """只**多挂** ``reportPreviewPayload``：顶层没有多出任何别的字段。"""
    with acceptance_harness(model=ScriptedModel(plan_candidate())) as harness:
        _confirmed, accepted = _drive_to_edit_accept(harness)

    assert set(accepted) == set(AgentResponse.model_fields), sorted(accepted)
    assert "reportPreviewPayload" in accepted
    # 载荷结构与设计轮同构（同一套生成入口的产物）。
    assert set(accepted["reportPreviewPayload"]) == {"previewContext", "regions"}
    assert set(accepted["reportPreviewPayload"]["regions"][0]) == {
        "regionId", "name", "role", "componentType",
        "dataSource", "capabilityPending", "payload",
    }


def test_edit_accept_payload_is_not_persisted():
    """载荷**不落库**：会话快照与落库消息里都没有模拟业务数值。"""
    with acceptance_harness(model=ScriptedModel(plan_candidate())) as harness:
        _confirmed, accepted = _drive_to_edit_accept(harness)

        stored = harness.persisted_snapshot()
        messages = [
            message.payload
            for message in harness.assistant_messages()
            if message.payload
        ]

    # 快照：只有上下文，没有载荷。
    assert "reportPreviewPayload" not in stored
    assert "previewContext" in stored
    # 落库的 assistant 消息：载荷里的**数值**被剥掉，只留上下文。
    for payload in messages:
        preview = payload.get("reportPreviewPayload")
        if isinstance(preview, dict):
            assert "regions" not in preview, preview
            assert set(preview) == {"previewContext"}, preview
    # 响应里确实有数值（证明上面剥掉的是数值，不是响应本身没带）。
    assert accepted["reportPreviewPayload"]["regions"]


def test_edit_accept_payload_in_user_json_mode_is_not_null():
    """用户 JSON 模式：编辑接受轮同样带载荷（不为 null）。"""
    with acceptance_harness(model=ScriptedModel(plan_candidate())) as harness:
        _confirmed, accepted = _drive_to_edit_accept(harness, user_json=True)

    payload = accepted.get("reportPreviewPayload")
    assert payload is not None, accepted
    assert len(payload["regions"]) == 2, payload["regions"]


# --------------------------------------------------------------------------
# F-3 判据 2：「点同意」那一轮（_save_placeholder）也要带载荷
# --------------------------------------------------------------------------


def _drive_to_agree(harness, *, user_json: bool = False) -> dict[str, Any]:
    """走真实链路到「点同意」那一轮，返回那一轮的响应体。

    出方案 → 确认方案 → 点「修改」→ 提目录外新增 → 点「接受修改」→ 点「同意」。
    """
    _confirmed, _accepted = _drive_to_edit_accept(harness, user_json=user_json)
    return harness.send("同意", state="USER_EDITING_TEMPLATE")


def test_agree_response_carries_the_preview_payload_with_the_new_region():
    """「点同意」那一轮：载荷**非 null**，且**含新增的那块区域**。

    工单 #66 C2 F-3 判据 2：界面上的「点同意」走的是
    ``_save_placeholder``（``SAVE_DEFINITION_DRAFT``）那一轮 —— 草稿刚在这
    一轮落库，右侧该显示新载荷，不该等整页刷新。
    """
    with acceptance_harness(model=ScriptedModel(plan_candidate())) as harness:
        agreed = _drive_to_agree(harness)

        assert agreed.get("state") == "SAVE_DEFINITION_DRAFT", agreed
        payload = agreed.get("reportPreviewPayload")
        assert payload is not None, agreed
        region_ids = [region["regionId"] for region in payload["regions"]]
        assert len(region_ids) == 2, region_ids
        assert any("冰箱" in region_id for region_id in region_ids), region_ids
        added = next(
            region for region in payload["regions"]
            if "冰箱" in region["regionId"]
        )
        assert added["payload"]["rows"], added


def test_agree_payload_context_matches_the_saved_definition():
    """载荷里的定义版本与**落库后**那份定义**一致**（前端不会判成过期版本）。"""
    with acceptance_harness(model=ScriptedModel(plan_candidate())) as harness:
        agreed = _drive_to_agree(harness)

        payload = agreed.get("reportPreviewPayload") or {}
        context = payload.get("previewContext") or {}
        draft = (
            (agreed.get("domainObjects") or {}).get("reportDefinitionDraft") or {}
        )
        metadata = draft.get("metadata") or {}
        binding = metadata.get("templateBinding") or {}

    assert context, payload
    assert context["definitionId"] == binding["definitionId"], (
        context, binding,
    )
    assert context["definitionVersion"] == binding["definitionVersion"], (
        context, binding,
    )
    # 前端的口径：binding.definitionVersion ?? metadata.revision
    frontend_version = (
        binding["definitionVersion"]
        if binding.get("definitionVersion") is not None
        else metadata.get("revision")
    )
    assert context["definitionVersion"] == frontend_version


def test_agree_payload_shares_the_context_of_the_edit_accept_round():
    """「点同意」与「接受修改」两轮**共用同一份**冻结上下文（数字不跳）。"""
    with acceptance_harness(model=ScriptedModel(plan_candidate())) as harness:
        _confirmed, accepted = _drive_to_edit_accept(harness)
        agreed = harness.send("同意", state="USER_EDITING_TEMPLATE")

        accept_context = (accepted.get("reportPreviewPayload") or {}).get(
            "previewContext"
        ) or {}
        agree_context = (agreed.get("reportPreviewPayload") or {}).get(
            "previewContext"
        ) or {}

    assert accept_context and agree_context
    assert agree_context["definitionId"] == accept_context["definitionId"]
    assert agree_context["definitionVersion"] == accept_context["definitionVersion"]
    assert agree_context["windowStart"] == accept_context["windowStart"]
    assert agree_context["windowEnd"] == accept_context["windowEnd"]


def test_agree_payload_matches_the_restored_session_payload():
    """「点同意」那一轮的载荷与**读会话**时重新生成的载荷一致。"""
    with acceptance_harness(model=ScriptedModel(plan_candidate())) as harness:
        agreed = _drive_to_agree(harness)
        restored = (harness.read_session().get("snapshot") or {}).get(
            "reportPreviewPayload"
        )

    assert restored is not None
    assert agreed["reportPreviewPayload"] == restored


def test_agree_payload_is_not_persisted():
    """「点同意」那一轮的载荷**不落库**（模拟业务数值不进会话库）。"""
    with acceptance_harness(model=ScriptedModel(plan_candidate())) as harness:
        agreed = _drive_to_agree(harness)

        stored = harness.persisted_snapshot()
        messages = [
            message.payload
            for message in harness.assistant_messages()
            if message.payload
        ]

    assert "reportPreviewPayload" not in stored
    for payload in messages:
        preview = payload.get("reportPreviewPayload")
        if isinstance(preview, dict):
            assert "regions" not in preview, preview
            assert set(preview) == {"previewContext"}, preview
    assert agreed["reportPreviewPayload"]["regions"]


def test_agree_response_carries_no_extra_top_level_field():
    """「点同意」那一轮：顶层**没有**多出任何别的字段（只多挂载荷）。"""
    with acceptance_harness(model=ScriptedModel(plan_candidate())) as harness:
        agreed = _drive_to_agree(harness)

    assert set(agreed) == set(AgentResponse.model_fields), sorted(agreed)
    assert set(agreed["reportPreviewPayload"]) == {"previewContext", "regions"}


def test_save_round_without_a_draft_carries_no_payload():
    """**没有草稿可保存**的那一轮（协议错误）明确**不带**载荷。

    ``_save_placeholder`` 的错误分支（``SAVE_MISSING_DRAFT``）不该凭空造一份
    示意 —— 那一轮连定义都没有。
    """
    with acceptance_harness(model=ScriptedModel(plan_candidate())) as harness:
        agreed = _drive_to_agree(harness)
        assert agreed.get("reportPreviewPayload") is not None

        # 绕过草稿再点一次「同意」：服务端存着的草稿被清掉 → 走错误分支。
        record = harness.session_record()
        stored = dict(record.snapshot or {})
        stored.pop("definition", None)
        stored.pop("policy", None)
        harness.repository.upsert_session(
            session_id=harness.session_id,
            state=record.state,
            snapshot=stored,
        )

        missing = harness.send("同意", state="USER_EDITING_TEMPLATE")

    assert missing.get("reportPreviewPayload") is None, missing
    assert missing.get("error"), missing
