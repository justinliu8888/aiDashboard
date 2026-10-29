"""报表示意载荷（#51）：按定义生成、随确认响应返回、状态与回复口径。

覆盖契约：

- 方案确认的**同一轮**响应里就带回 ``reportPreviewPayload``，不用二次请求；
- ``previewContext`` 八个字段，**首次确认冻结**、此后按定义版本**复用**，
  跨天、刷新、重启都不变；
- ``regions[]`` 带 ``role``，``REPORT_TITLE`` 不进 regions；
- 数值型组件的载荷带 ``unit``；表格列的 ``fieldRef`` 对得上行键；
- 造数失败 → **明确失败响应**，不退回空表、不给半份载荷；
- 模拟业务结果**不落库**（会话快照、消息全响应都没有），恢复时按定义与
  上下文**重新生成**，数字完全一致；
- 回复口径反转：不再出现「待试运行产生」「保留占位」「Mock 模拟数据」。
"""
from __future__ import annotations

import json
from typing import Any

import pytest

from app.services.preview_service import (
    PREVIEW_CONTEXT_FIELDS,
    PreviewContextError,
    build_preview_context,
    build_report_preview_payload,
)
from app.services.session_service import session_service
from tests.test_report_template_generation import (
    RecordingModel,
    _body,
    _candidate,
    _post,
    design_deps,
    install_model,
)


def _plan(client, monkeypatch, *candidates):
    model = install_model(monkeypatch, RecordingModel(*(candidates or [_candidate()])))
    plan = _post(client, "生成 A 机房最近 7 天能耗报表").json()["domainObjects"]["reportPlan"]
    return model, plan


def _confirm_plan(client, plan, message_id="confirm_plan"):
    """点「确认方案」：同一轮就出草稿、示意载荷与下一步菜单。"""
    return _post(client, "确认", message_id=message_id, context={
        "designAction": "CONFIRM_PLAN",
        "planId": plan["id"],
        "basePlanRevision": plan["revision"],
    }).json()


def _regions_by_role(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {region["role"]: region for region in payload["regions"]}


# --------------------------------------------------------------------------
# 1. 同一轮响应里就带着载荷
# --------------------------------------------------------------------------


def test_confirm_plan_returns_report_preview_payload_in_the_same_turn(client, monkeypatch):
    """点一次「确认方案」，同一轮响应里就带着示意载荷 —— 不用二次请求。"""
    _, plan = _plan(client, monkeypatch)

    payload = _confirm_plan(client, plan)

    preview = payload["reportPreviewPayload"]
    assert preview is not None
    assert preview["regions"]
    region = preview["regions"][0]
    assert region["regionId"]
    assert region["name"]
    assert region["role"]
    assert region["componentType"] == "METRIC_CARD"
    # 每个非标题板块都出数，没有「暂无数值」占位。
    assert isinstance(region["payload"]["value"], (int, float))
    assert region["payload"]["value"] is not None


def test_preview_context_has_exactly_the_eight_fields(client, monkeypatch):
    """previewContext 固定八个字段，且只放上下文、不放业务数值。"""
    _, plan = _plan(client, monkeypatch)

    context = _confirm_plan(client, plan)["reportPreviewPayload"]["previewContext"]

    assert set(context) == set(PREVIEW_CONTEXT_FIELDS)
    assert len(context) == 8
    # 只放上下文：没有数值载荷、没有业务对象。
    assert "regions" not in context
    assert "value" not in context
    assert "rows" not in context
    # 窗口左闭右开。
    assert context["windowStart"] < context["windowEnd"]


# --------------------------------------------------------------------------
# 2. 冻结与复用
# --------------------------------------------------------------------------


def test_preview_context_is_frozen_and_reused_on_repeated_confirm(client, monkeypatch):
    """重复确认复用**同一份**上下文 —— 锚点不变，数字不跳。"""
    _, plan = _plan(client, monkeypatch)

    first = _confirm_plan(client, plan, "confirm_1")["reportPreviewPayload"]
    second = _confirm_plan(client, plan, "confirm_2")["reportPreviewPayload"]

    assert first["previewContext"] == second["previewContext"]
    assert first["regions"][0]["payload"]["value"] == second["regions"][0]["payload"]["value"]


def test_preview_context_is_persisted_and_carried_over(client, monkeypatch):
    """上下文随会话快照落库；后续轮次原样带过，不重新生成。"""
    _, plan = _plan(client, monkeypatch)

    confirmed = _confirm_plan(client, plan)
    context = confirmed["reportPreviewPayload"]["previewContext"]

    record = session_service.repository.get_session("sess_tpl_1")
    assert record.snapshot["previewContext"] == context


def test_new_definition_version_gets_a_new_context(client, monkeypatch):
    """定义版本变了 → 新上下文（旧版本的上下文保留，不覆盖新模板）。"""
    from app.services.preview_service import resolve_preview_context

    _, plan = _plan(client, monkeypatch)
    confirmed = _confirm_plan(client, plan)
    first = confirmed["reportPreviewPayload"]["previewContext"]
    definition = confirmed["domainObjects"]["reportDefinitionDraft"]

    # 同一个定义版本 → **复用**同一份上下文（跨天、刷新、重启都走这一条）。
    reused = resolve_preview_context(definition, plan=plan, existing=first)
    assert reused == first

    # 定义版本变了 → 新上下文。
    newer = dict(definition)
    newer["metadata"] = {
        **definition["metadata"],
        "templateBinding": {
            **definition["metadata"]["templateBinding"],
            "definitionVersion": int(first["definitionVersion"]) + 1,
        },
    }
    renewed = resolve_preview_context(newer, plan=plan, existing=first)
    assert renewed["definitionVersion"] != first["definitionVersion"]
    assert renewed["definitionId"] == first["definitionId"]


def test_invalid_timezone_is_reported_not_silently_utc(client, monkeypatch):
    """时区非法 → 明确报错，不静默按 UTC。"""
    _, plan = _plan(client, monkeypatch)
    definition = _confirm_plan(client, plan)["domainObjects"]["reportDefinitionDraft"]
    definition["defaults"]["timeWindow"]["timezone"] = "Mars/Olympus"

    with pytest.raises(PreviewContextError) as excinfo:
        build_preview_context(definition)

    assert "时区" in str(excinfo.value)


def test_grain_unit_out_of_range_is_reported(client, monkeypatch):
    """粒度越界 → 明确报错。"""
    _, plan = _plan(client, monkeypatch)
    definition = _confirm_plan(client, plan)["domainObjects"]["reportDefinitionDraft"]

    with pytest.raises(PreviewContextError) as excinfo:
        build_preview_context(definition, grain_unit="FORTNIGHT")

    assert "粒度" in str(excinfo.value)


# --------------------------------------------------------------------------
# 3. 载荷形状
# --------------------------------------------------------------------------


def test_regions_carry_role_and_report_title_is_excluded(client, monkeypatch):
    """``role`` 必须带；标题区不出数、不进 regions。"""
    _, plan = _plan(client, monkeypatch)
    definition = _confirm_plan(client, plan)["domainObjects"]["reportDefinitionDraft"]
    # 造一个标题区：标题区永远不该出现在 regions[] 里。
    definition["regions"].insert(0, {
        "id": "region_title",
        "name": "报表标题",
        "role": "REPORT_TITLE",
        "component": {"type": "TEXT", "content": {"format": "PLAIN", "template": "标题"}},
        "context": {},
    })

    context = build_preview_context(definition)
    payload = build_report_preview_payload(definition, context)

    assert payload["regions"]
    assert all(region["role"] != "REPORT_TITLE" for region in payload["regions"])
    assert all(region["role"] for region in payload["regions"])
    assert all("regionId" in region for region in payload["regions"])
    assert all("componentType" in region for region in payload["regions"])


def test_numeric_payloads_carry_unit(client, monkeypatch):
    """数值型组件的载荷一定带 ``unit``。"""
    candidate = _candidate(metrics=["总耗电"], sections=[
        {
            "id": "section_total", "title": "总耗电概览",
            "metricIntents": ["总耗电"], "componentType": "metric_card",
        },
        {
            "id": "section_trend", "title": "总耗电趋势",
            "metricIntents": ["总耗电"], "componentType": "line_chart",
        },
    ])
    _, plan = _plan(client, monkeypatch, candidate)

    payload = _confirm_plan(client, plan)["reportPreviewPayload"]

    by_type = {region["componentType"]: region for region in payload["regions"]}
    assert "METRIC_CARD" in by_type
    assert "CHART" in by_type
    assert by_type["METRIC_CARD"]["payload"]["unit"] == "kWh"
    # 图表的 unit 由并行的 04b 补丁补上；这里要求组装层**兜底**补齐。
    assert "unit" in by_type["CHART"]["payload"]
    assert by_type["CHART"]["payload"]["unit"]


def test_table_columns_field_ref_matches_row_keys(client, monkeypatch):
    """表格列的 ``fieldRef`` 必须对得上行对象的键。"""
    candidate = _candidate(metrics=["设备耗电"], sections=[{
        "id": "section_devices", "title": "设备耗电明细",
        "metricIntents": ["设备耗电"], "componentType": "table",
    }])
    _, plan = _plan(client, monkeypatch, candidate)

    payload = _confirm_plan(client, plan)["reportPreviewPayload"]

    tables = [r for r in payload["regions"] if r["componentType"] == "TABLE"]
    assert tables
    table = tables[0]["payload"]
    assert table["columns"]
    assert table["rows"]
    for column in table["columns"]:
        assert column["id"] and column["label"] and column["fieldRef"]
        for row in table["rows"]:
            assert column["fieldRef"] in row


def test_regions_have_no_placeholder_numbers(client, monkeypatch):
    """每个非标题板块都出数，没有「暂无数值」占位。"""
    _, plan = _plan(client, monkeypatch)

    payload = _confirm_plan(client, plan)["reportPreviewPayload"]

    for region in payload["regions"]:
        assert region["payload"].get("placeholder") is not True
        assert "value" in region["payload"]


# --------------------------------------------------------------------------
# 4. 造数失败 → 明确失败
# --------------------------------------------------------------------------


def test_simulated_data_error_becomes_explicit_failure_response(client, monkeypatch):
    """造数失败 → 明确失败响应：不退回空表、不给半份载荷。"""
    from app.services import report_design_service as design_module

    _, plan = _plan(client, monkeypatch)

    original = design_module.build_report_preview_payload

    def boom(definition, context, **kwargs):
        from app.services.simulated_data_service import SimulatedDataError

        raise SimulatedDataError("MISSING_COLUMNS", "区域 'region_1' 是表格，但没有声明任何列。")

    monkeypatch.setattr(design_module, "build_report_preview_payload", boom)

    payload = _confirm_plan(client, plan)

    # 明确失败：错误码与原因都在。
    assert payload["error"] is not None
    assert payload["error"]["code"] == "MISSING_COLUMNS"
    assert "没有声明任何列" in payload["error"]["message"]
    assert "示意数据没有生成出来" in payload["reply"]
    # 不给半份载荷。
    assert payload["reportPreviewPayload"] is None

    monkeypatch.setattr(design_module, "build_report_preview_payload", original)


def test_unsupported_component_type_is_reported_as_failure(client, monkeypatch):
    """组件类型不认识 → 明确失败（UNSUPPORTED_COMPONENT）。"""
    _, plan = _plan(client, monkeypatch)
    definition = _confirm_plan(client, plan)["domainObjects"]["reportDefinitionDraft"]
    definition["regions"][0]["component"]["type"] = "RADAR"

    context = build_preview_context(definition)

    from app.services.simulated_data_service import SimulatedDataError

    with pytest.raises(SimulatedDataError) as excinfo:
        build_report_preview_payload(definition, context)
    assert excinfo.value.code == "UNSUPPORTED_COMPONENT"


def test_unresolved_grain_is_reported_as_failure(client, monkeypatch):
    """粒度解析不了 → 明确失败（UNRESOLVED_GRAIN）。"""
    _, plan = _plan(client, monkeypatch)
    definition = _confirm_plan(client, plan)["domainObjects"]["reportDefinitionDraft"]
    context = build_preview_context(definition)
    # WEEK 是 previewContext 的合法取值，但造数器解析不了 —— 必须明确失败。
    context["grainUnit"] = "WEEK"

    from app.services.simulated_data_service import SimulatedDataError

    with pytest.raises(SimulatedDataError) as excinfo:
        build_report_preview_payload(definition, context)
    assert excinfo.value.code == "UNRESOLVED_GRAIN"


# --------------------------------------------------------------------------
# 5. 存储隔离
# --------------------------------------------------------------------------


def test_persisted_snapshot_has_allowed_keys_only_and_no_simulated_payload(client, monkeypatch):
    """会话快照：按允许列表写入，**不含**模拟载荷。"""
    _, plan = _plan(client, monkeypatch)

    confirmed = _confirm_plan(client, plan)

    record = session_service.repository.get_session("sess_tpl_1")
    snapshot = record.snapshot
    # 模拟载荷不落库。
    assert "reportPreviewPayload" not in snapshot
    # 允许列表里的键在。
    for key in ("definition", "policy", "reportPlan", "templateDesign",
                "designPhase", "previewContext"):
        assert key in snapshot, key
    # 快照里没有任何模拟业务数值。
    blob = json.dumps(snapshot, ensure_ascii=False)
    value = confirmed["reportPreviewPayload"]["regions"][0]["payload"]["value"]
    assert str(value) not in blob


def test_existing_preview_payload_semantics_are_not_broken(client, monkeypatch):
    """``previewPayload`` 仍是**试运行结果**，语义不变、互不覆盖。"""
    _, plan = _plan(client, monkeypatch)
    _confirm_plan(client, plan)

    # 走既有「同意 → 校验 → 试运行」链路。
    _post(client, "同意", message_id="save_draft")
    for state in ("VALIDATING", "PREVIEWING", "PREVIEW_READY"):
        _post(client, "__advance__", message_id="advance_" + state)

    detail = client.get("/api/sessions/sess_tpl_1").json()
    snapshot = detail["snapshot"]
    # 试运行结果还在（这是 previewPayload，不是 reportPreviewPayload）。
    assert snapshot["previewPayload"]["regions"]
    assert snapshot["previewPayload"]["regions"][0]["payload"]
    # 两者并存、互不覆盖。
    assert snapshot["reportPreviewPayload"]["regions"]


def test_message_payload_does_not_persist_simulated_values(client, monkeypatch):
    """消息全响应里**没有**模拟业务数值（上下文保留）。"""
    _, plan = _plan(client, monkeypatch)

    confirmed = _confirm_plan(client, plan)

    messages = session_service.repository.list_messages("sess_tpl_1")
    assistant = [m for m in messages if m.role == "assistant"]
    assert assistant
    for message in assistant:
        payload = message.payload or {}
        stored = payload.get("reportPreviewPayload")
        if stored is None:
            continue
        # 只留上下文，区域数值不留。
        assert "regions" not in stored
        assert stored.get("previewContext")
    value = confirmed["reportPreviewPayload"]["regions"][0]["payload"]["value"]
    for message in assistant:
        assert str(value) not in json.dumps(message.payload or {}, ensure_ascii=False)


def test_session_read_regenerates_identical_numbers_without_writing_back(client, monkeypatch):
    """恢复会话：按定义与上下文重新生成，数字一致，且**不写回**快照。"""
    _, plan = _plan(client, monkeypatch)

    confirmed = _confirm_plan(client, plan)
    original = confirmed["reportPreviewPayload"]

    detail = client.get("/api/sessions/sess_tpl_1").json()
    regenerated = detail["snapshot"]["reportPreviewPayload"]

    assert regenerated["previewContext"] == original["previewContext"]
    assert regenerated["regions"][0]["payload"]["value"] == (
        original["regions"][0]["payload"]["value"]
    )
    # 落库的快照里仍然没有模拟载荷。
    record = session_service.repository.get_session("sess_tpl_1")
    assert "reportPreviewPayload" not in record.snapshot


def test_session_read_without_definition_returns_no_payload(client, monkeypatch):
    """没有有效定义 → 不返回无关的示意。"""
    _post(client, "随便聊聊", message_id="chat_only")

    detail = client.get("/api/sessions/sess_tpl_1").json()

    assert not (detail.get("snapshot") or {}).get("reportPreviewPayload")


def test_legacy_session_without_context_is_derived_read_only(client, monkeypatch):
    """历史会话没有上下文 → 按已有创建时间只读推导，不自动确认、不迁移。"""
    _, plan = _plan(client, monkeypatch)
    _confirm_plan(client, plan)

    # 抹掉上下文，模拟改造前落库的会话。
    record = session_service.repository.get_session("sess_tpl_1")
    snapshot = dict(record.snapshot)
    snapshot.pop("previewContext", None)
    session_service.repository.upsert_session(
        session_id="sess_tpl_1", state=record.state, snapshot=snapshot,
    )

    detail = client.get("/api/sessions/sess_tpl_1").json()

    payload = detail["snapshot"]["reportPreviewPayload"]
    assert payload["previewContext"]
    assert payload["regions"]
    # 推导出来的上下文**没有写回**持久化快照。
    record = session_service.repository.get_session("sess_tpl_1")
    assert "previewContext" not in record.snapshot


# --------------------------------------------------------------------------
# 6. 确认路径全覆盖
# --------------------------------------------------------------------------


def _assert_carries_payload(payload: dict[str, Any], label: str) -> None:
    preview = payload.get("reportPreviewPayload")
    assert preview is not None, f"{label} 这条路径没有带示意载荷"
    assert preview["regions"], f"{label} 这条路径的载荷是空的"
    assert preview["previewContext"]


def test_every_confirm_path_carries_the_preview_payload(client, monkeypatch):
    """**每一条**确认路径都要带示意载荷 —— 漏一条，那条路径右侧就是空壳。

    逐条核对 ``_draft_response`` 的 5 个调用点：

    1. ``confirm_template`` 会话已在 ``DRAFT_READY``（历史「确认模板」入口）；
    2. ``_handle_confirm_action`` 已有确认关系（重复点确认按钮）；
    3. ``_handle_confirm_action`` 同一条消息重发（幂等认领命中已成功）；
    4. ``_handle_confirm_action`` 新确认（主路径）；
    5. ``_handle_confirm_action`` 别的进程已提交同一版方案。

    挂载点在 ``_draft_response`` 函数体内统一做，所以 5 条路径自动覆盖；
    下面逐条**验**一遍，别只信「自动覆盖」。
    """
    from app.services import report_design_service as design_module

    _, plan = _plan(client, monkeypatch)

    # 路径④：新确认（主路径）—— 新草稿，designOutcome 是 draft_ready。
    first = _confirm_plan(client, plan, "confirm_path_4")
    _assert_carries_payload(first, "路径④ 新确认")
    assert first["debug"]["designOutcome"] == "draft_ready"

    # 路径②：重复点确认按钮（新消息 id，但这一版方案已经确认过）。
    second = _confirm_plan(client, plan, "confirm_path_2")
    _assert_carries_payload(second, "路径② 重复确认")
    assert second["debug"]["designOutcome"] == "draft_duplicate"

    # 路径③：同一条消息重发（幂等认领命中「已成功」）。
    third = _confirm_plan(client, plan, "confirm_path_4")
    _assert_carries_payload(third, "路径③ 幂等重发")
    assert third["debug"]["designOutcome"] == "draft_duplicate"

    # 路径①：历史「确认模板」入口，会话已在 DRAFT_READY。
    fourth = _post(client, "确认模板", message_id="confirm_template_path", context={
        "designAction": "CONFIRM_TEMPLATE",
        "templateId": first["domainObjects"]["templateDesign"]["id"],
        "baseTemplateRevision": first["domainObjects"]["templateDesign"]["revision"],
    }).json()
    _assert_carries_payload(fourth, "路径① 确认模板（DRAFT_READY）")
    assert fourth["debug"]["designOutcome"] == "draft_duplicate"

    # 路径⑤：别的进程已经提交过同一版方案。
    # 用**另一个会话**：上面这一版方案已经确认过，走不到提交那一步。
    committed = {"called": False}

    def fake_commit(
        operation_id, expected_revision, template, definition, write_turn=None
    ):
        committed["called"] = True
        return {
            "kind": "committed",
            "alreadyCommitted": True,
            "template": template,
            "definitionDraft": definition,
        }

    monkeypatch.setattr(
        design_module.report_design_service.repository,
        "commit_template_and_definition",
        fake_commit,
    )
    other_plan = _post(
        client, "生成 A 机房最近 7 天能耗报表",
        session_id="sess_tpl_path5", message_id="plan_path_5",
    ).json()["domainObjects"]["reportPlan"]
    fifth = _post(client, "确认", session_id="sess_tpl_path5", message_id="confirm_path_5",
                  context={
                      "designAction": "CONFIRM_PLAN",
                      "planId": other_plan["id"],
                      "basePlanRevision": other_plan["revision"],
                  }).json()
    assert committed["called"], "路径⑤ 没有真的走到 alreadyCommitted 分支"
    _assert_carries_payload(fifth, "路径⑤ alreadyCommitted")
    assert fifth["debug"]["designOutcome"] == "draft_duplicate"


def test_confirm_template_legacy_path_carries_the_preview_payload(client, monkeypatch):
    """历史 TEMPLATE_READY 会话走「确认模板」的新建分支，也要带载荷。"""
    from app.services.report_template_service import build_template_design

    _, plan = _plan(client, monkeypatch)
    template = build_template_design(plan, subject_id="room_a").model_dump(mode="json")
    conn = session_service.repository.conn
    conn.execute(
        "INSERT INTO report_design_templates"
        " (session_id, template_id, template_revision, plan_id, plan_revision,"
        "  data_status, template_json, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "sess_tpl_1", template["id"], template["revision"], plan["id"],
            plan["revision"], template.get("dataStatus"),
            json.dumps(template, ensure_ascii=False), "2026-10-29T00:00:00",
            "2026-10-29T00:00:00",
        ),
    )
    conn.execute(
        "UPDATE report_design_sessions SET design_phase = 'TEMPLATE_READY'"
        " WHERE session_id = ?", ("sess_tpl_1",),
    )
    conn.commit()

    payload = _post(client, "确认模板", message_id="confirm_template_legacy", context={
        "designAction": "CONFIRM_TEMPLATE",
        "templateId": template["id"],
        "baseTemplateRevision": template["revision"],
    }).json()

    _assert_carries_payload(payload, "确认模板（TEMPLATE_READY）")


# --------------------------------------------------------------------------
# 7. 回复口径反转
# --------------------------------------------------------------------------


def test_reply_no_longer_uses_the_old_wording(client, monkeypatch):
    """旧口径全部作废：不再说「待试运行产生」「保留占位」。"""
    _, plan = _plan(client, monkeypatch)

    reply = _confirm_plan(client, plan)["reply"]

    assert "待试运行" not in reply
    assert "保留占位" not in reply
    assert "占位原因" not in reply
    assert "Mock 模拟数据" not in reply
    assert "模拟业务数据" in reply


def test_preview_reply_does_not_claim_a_successful_trial_run(client, monkeypatch):
    """示意生成**不等于**校验成功，也不设置试运行成功状态。"""
    _, plan = _plan(client, monkeypatch)

    payload = _confirm_plan(client, plan)

    assert payload["state"] == "WAITING_USER_CONFIRM"
    assert payload["designPhase"] == "DRAFT_READY"
    assert "试运行成功" not in payload["reply"]
    assert "校验通过" not in payload["reply"]


# --------------------------------------------------------------------------
# 8. 方案侧（票 02）的接线：原始请求、澄清通道、栏目修不了
# --------------------------------------------------------------------------


def test_apply_candidate_receives_original_and_current_request(client, monkeypatch):
    """方案服务要拿到三样输入：原始请求、本轮请求、已有方案。

    少了前两样，用户明确指定的展示类型（"用柱状图"）与展示目的就都
    判不出来 —— 服务端会全部退回模型原选。
    """
    from app.services import report_design_service as design_module

    captured: dict[str, Any] = {}
    original = design_module.apply_candidate

    def spy(candidate, previous=None, **kwargs):
        captured.clear()
        captured.update(kwargs)
        captured["previous"] = previous
        return original(candidate, previous=previous, **kwargs)

    monkeypatch.setattr(design_module, "apply_candidate", spy)

    # 第一轮：还没有历史，原始请求为空 —— 本轮请求就是用户原话。
    _post(client, "生成 A 机房最近 7 天能耗报表", message_id="req_1")
    assert captured["current_request"] == "生成 A 机房最近 7 天能耗报表"
    assert captured["original_request"] is None

    # 第二轮（修改）：原始请求 = 会话里用户**最初**的那句原话。
    _post(client, "改成柱状图", message_id="req_2")
    assert captured["original_request"] == "生成 A 机房最近 7 天能耗报表"
    assert captured["current_request"] == "改成柱状图"
    # 已有方案也传下去了。
    assert captured["previous"] is not None


def test_plan_clarification_required_goes_to_the_clarification_channel(client, monkeypatch):
    """缺栏目且推不出来 → 走**澄清通道**，不当成普通失败。"""
    from app.services import report_design_service as design_module
    from app.schemas.report_plan import PlanClarificationRequired

    question = (
        "「运维工单明细」这张表要展示哪些栏目？"
        "说清楚列名就行（例如「设备名称、时间、告警级别」）。"
    )

    def boom(*args, **kwargs):
        raise PlanClarificationRequired(question, question=question)

    monkeypatch.setattr(design_module, "apply_candidate", boom)

    payload = _post(client, "生成运维工单报表", message_id="clarify_1").json()

    # 追问走澄清通道：状态是待澄清，文案就是那句具体问题。
    assert payload["state"] == "NEEDS_CLARIFICATION"
    assert payload["reply"] == question
    assert "plan_clarification" in payload["uiActions"]
    # 追问**不是**失败：没有 error。
    assert payload.get("error") is None
    # 方案没有落库（还在等用户拍板）。
    assert payload["domainObjects"]["reportPlan"] is None


def test_plan_columns_unresolved_fails_with_a_retryable_error(client, monkeypatch):
    """栏目修不了 → 明确失败并允许重试（消息里带「请重试」）。"""
    from app.services import report_design_service as design_module
    from app.schemas.report_plan import PlanColumnsUnresolvedError

    def boom(*args, **kwargs):
        raise PlanColumnsUnresolvedError("表格栏目结构不合法，补齐也失败。")

    monkeypatch.setattr(design_module, "apply_candidate", boom)

    payload = _post(client, "生成运维工单报表", message_id="unresolved_1").json()

    assert payload["error"] is not None
    assert payload["error"]["code"] == "PLAN_COLUMNS_UNRESOLVED"
    assert "请重试" in payload["error"]["message"]
    assert "plan_retry" in payload["uiActions"]
    # 不给一张空表还宣称成功。
    assert payload["domainObjects"]["reportPlan"] is None
