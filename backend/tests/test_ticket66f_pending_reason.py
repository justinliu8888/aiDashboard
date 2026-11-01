"""工单 #66 F：区域 ``pendingReason`` 按来源模式分别说（不说反话）。

**问题**：用户 JSON 模式（``sourceMode = "user_json"``）下，模板区域里的
``pendingReason`` 仍写着「本区域数值为模拟业务数据，接入真实数据后自动替换。」
—— 那块区域的数值是**用户自己提供的那份数据**，不是造出来的模拟数据。
这句文案随 ``domainObjects.templateDesign`` 发到前端，用户在界面上看到
「模拟业务数据」= **说假话**；工单 #66 的验收标准也明写 JSON 模式下界面要标
「用户提供的数据，未经业务核验」，且**不宣称已连接真实业务库**。

**两个缺口一起补**：

1. 模板层（``report_template_service``）的区域 ``pendingReason`` 硬编码了
   模拟口径 → 改成按**服务端记录的来源模式**分别说；
2. 调用方（``report_design_service``）调 ``build_template_design`` 时
   **没传** ``source_mode`` → 模式落默认口径（模拟示例）。

**模拟示例模式的既有文案逐字未改**（既有业务输出一个字都不许动）。

用例走**真实构造路径**：模板层直接调 ``build_template_design``，
链路层走真实 ``POST /api/agent/chat`` + 真实编排 + 真实 SQLite
（独立临时库），只替换外部模型调用（``tests/support/acceptance_harness.py``）。
"""
from __future__ import annotations

import json
from typing import Any

from app.services.report_template_service import build_template_design
from tests.support.acceptance_harness import (
    ScriptedModel,
    acceptance_harness,
    plan_candidate,
)


#: 模拟示例模式下的既有文案（ADR 0004）。**逐字**照抄 —— 用来钉住「没被改」。
SIMULATED_PENDING_REASON = "本区域数值为模拟业务数据，接入真实数据后自动替换。"

#: 用户 JSON 模式下**不允许**出现的宣称（工单 #66 的验收标准）。
FORBIDDEN_IN_USER_JSON = (
    "模拟业务数据",
    "接入真实数据后自动替换",
    "已连接真实业务",
    "已接入真实业务",
    "真实业务库",
)


def _rows(count: int = 2) -> list[dict[str, Any]]:
    return [
        {
            "ts": f"2024-01-0{index + 1}",
            "device": f"机柜{index + 1}",
            "energy": float(index + 1),
        }
        for index in range(count)
    ]


def _json_text(rows: list[dict[str, Any]] | None = None) -> str:
    return json.dumps(rows if rows is not None else _rows(), ensure_ascii=False)


def _plan() -> dict[str, Any]:
    """一份带取数板块的方案（板块能绑上指标 → 区域是 bound → 有 pendingReason）。"""
    return {
        "id": "rplan_66f", "revision": 1, "topic": "A 机房能耗日报",
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


def _template_pending_reasons(template: dict[str, Any]) -> list[str]:
    """模板设计里各区域的 ``data.pendingReason``（只取有值的）。"""
    reasons = []
    for region in template.get("regions") or []:
        reason = (region.get("data") or {}).get("pendingReason")
        if reason:
            reasons.append(str(reason))
    return reasons


def _definition_pending_reasons(draft: dict[str, Any]) -> list[str]:
    """报表定义草稿里各区域 ``templateBinding.pendingReason``（只取有值的）。"""
    reasons = []
    for region in draft.get("regions") or []:
        reason = (region.get("templateBinding") or {}).get("pendingReason")
        if reason:
            reasons.append(str(reason))
    return reasons


# --------------------------------------------------------------------------
# 模板层：按来源模式分别说
# --------------------------------------------------------------------------


def test_simulated_mode_pending_reason_is_verbatim_unchanged() -> None:
    """模拟示例模式：``pendingReason`` **逐字**等于改前那句（既有业务输出不动）。"""
    design = build_template_design(_plan(), subject_id="room_a")
    reasons = _template_pending_reasons(design.model_dump(mode="json"))

    assert reasons, "取数板块应当是 bound 区域，带 pendingReason"
    assert reasons == [SIMULATED_PENDING_REASON], reasons


def test_module_constants_match_the_pinned_wording() -> None:
    """模块常量与用例里钉住的字面量一致（常量被改，这条会红）。

    两条文案各自只有**一处**来源：模板层模块级常量。
    """
    from app.services.report_template_service import (
        PENDING_REASON_SIMULATED,
        PENDING_REASON_USER_JSON,
    )

    assert PENDING_REASON_SIMULATED == SIMULATED_PENDING_REASON
    assert "未经业务核验" in PENDING_REASON_USER_JSON
    assert "模拟业务数据" not in PENDING_REASON_USER_JSON


def test_user_json_mode_pending_reason_says_user_provided_data() -> None:
    """用户 JSON 模式：说「用户提供的数据，未经业务核验」，不说模拟业务数据。"""
    design = build_template_design(
        _plan(), subject_id="room_a", source_mode="user_json"
    )
    reasons = _template_pending_reasons(design.model_dump(mode="json"))

    assert reasons, "取数板块应当是 bound 区域，带 pendingReason"
    for reason in reasons:
        assert "模拟业务数据" not in reason, reason
        assert "未经业务核验" in reason, reason
        assert "用户提供的数据" in reason, reason
        for forbidden in FORBIDDEN_IN_USER_JSON:
            assert forbidden not in reason, reason


def test_user_json_mode_pending_reason_does_not_claim_a_real_data_connection() -> None:
    """用户 JSON 模式：不宣称已接入真实业务系统（那同样是不存在的事实）。"""
    design = build_template_design(
        _plan(), subject_id="room_a", source_mode="user_json"
    )
    reasons = _template_pending_reasons(design.model_dump(mode="json"))

    assert reasons
    for reason in reasons:
        assert "接入真实数据后自动替换" not in reason, reason
        assert "已连接真实业务" not in reason, reason
        assert "已接入真实业务" not in reason, reason


def test_source_mode_is_recorded_not_guessed_from_the_title() -> None:
    """来源模式**只认服务端记录的值**：不传就走默认口径（模拟示例）。"""
    from app.services.report_template_service import PENDING_REASON_USER_JSON

    # 方案标题里带「用户 JSON」字样 —— 但服务端没记录来源模式，
    # 就**不猜**：走默认口径（模拟示例），文案逐字保持既有那句。
    plan = _plan()
    plan["topic"] = "用户 JSON 数据报表"
    design = build_template_design(plan, subject_id="room_a")
    reasons = _template_pending_reasons(design.model_dump(mode="json"))

    assert reasons == [SIMULATED_PENDING_REASON], reasons
    assert PENDING_REASON_USER_JSON not in reasons


# --------------------------------------------------------------------------
# 调用方：来源模式真的传进去了
# --------------------------------------------------------------------------


def test_user_json_mode_pending_reason_through_the_real_confirm_flow() -> None:
    """真实链路：用户贴 JSON → 确认方案 → 区域 ``pendingReason`` 不说模拟数据。

    这一条同时钉住两个缺口：模板层按模式说 + 调用方**把模式传进去**
    （不传就落默认口径，这里会红）。
    """
    model = ScriptedModel(plan_candidate())
    with acceptance_harness(model=model) as harness:
        plan_payload = harness.send(f"这是昨天的数据 {_json_text()}")
        confirmed = harness.confirm_plan(plan_payload["domainObjects"]["reportPlan"])

    domain = confirmed["domainObjects"]
    # 前提：这一轮确实是用户 JSON 模式（定义里服务端记录的那一份）。
    assert domain["reportDefinitionDraft"]["metadata"]["sourceMode"] == "user_json"

    reasons = _template_pending_reasons(domain["templateDesign"])
    assert reasons, domain["templateDesign"]
    for reason in reasons:
        assert "模拟业务数据" not in reason, reason
        assert "未经业务核验" in reason, reason
        assert "用户提供的数据" in reason, reason
        for forbidden in FORBIDDEN_IN_USER_JSON:
            assert forbidden not in reason, reason

    # 定义层（草稿）里的区域绑定说的是同一句 —— 两端口径一致。
    definition_reasons = _definition_pending_reasons(domain["reportDefinitionDraft"])
    assert definition_reasons == reasons, (definition_reasons, reasons)


def test_simulated_mode_pending_reason_through_the_real_confirm_flow() -> None:
    """真实链路：模拟示例模式的 ``pendingReason`` 逐字未改。"""
    model = ScriptedModel(plan_candidate())
    with acceptance_harness(model=model) as harness:
        plan_payload = harness.send("生成 A 机房最近 7 天能耗报表")
        confirmed = harness.confirm_plan(plan_payload["domainObjects"]["reportPlan"])

    domain = confirmed["domainObjects"]
    assert domain["reportDefinitionDraft"]["metadata"]["sourceMode"] == "simulated"

    reasons = _template_pending_reasons(domain["templateDesign"])
    assert reasons, domain["templateDesign"]
    assert reasons == [SIMULATED_PENDING_REASON], reasons

    definition_reasons = _definition_pending_reasons(domain["reportDefinitionDraft"])
    assert definition_reasons == reasons, (definition_reasons, reasons)


def test_user_json_mode_reply_and_region_text_agree() -> None:
    """两端口径一致：回复与区域 ``pendingReason`` 都按用户 JSON 口径说。"""
    model = ScriptedModel(plan_candidate())
    with acceptance_harness(model=model) as harness:
        plan_payload = harness.send(f"这是昨天的数据 {_json_text()}")
        confirmed = harness.confirm_plan(plan_payload["domainObjects"]["reportPlan"])

    reply = confirmed["reply"]
    assert "用户提供的数据，未经业务核验" in reply, reply

    reasons = _template_pending_reasons(confirmed["domainObjects"]["templateDesign"])
    assert reasons
    for reason in reasons:
        assert "未经业务核验" in reason, reason
        assert "模拟业务数据" not in reason, reason
