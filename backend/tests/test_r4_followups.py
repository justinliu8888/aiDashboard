"""code-review R4 三条待修（🟠-5 / 🟡-2 / 🟡-3）的回归用例。

三条都落在「用户看得见的行为」上：输入框里说了什么 → 屏幕上出现了什么 /
什么没出现。

    - 🟠-5：问题分析（问题反馈）这一轮**必须**与咨询走**同一套**只读
      归一化（契约表 §7.3 / ADR 0002）：业务对象取服务端可信记录，
      状态不推进、不产生待接受计划。
    - 🟡-2：「这个图是什么意思」要能被判成**咨询**，但带改动动词的
      「改成柱状图」**不能**。
    - 🟡-3：澄清额度键要实现契约表 v1.11.0 的裁定 —— 目标由「未知」
      变成「已知」算**同一件事**；换一件事仍能重新追问一轮。

用例不写死设备名与日期：卡片名、时间窗、粒度都从当天快照推导。
"""
from __future__ import annotations

import pytest

from app.services import routing_rules
from app.services.context_engine import UserIntent, classify_message_intent
from app.services.report_draft_service import build_definition_draft
from app.services.session_service import session_service


# 工单 #58 正文里的两句，逐字保留。
ISSUE_MESSAGE = "昨日能耗趋势好丑啊，一个点……"
CONSULT_MESSAGE = "冰箱耗电明细可以用其他展示方法吗？"


def _snap(definition):
    return {"reportDefinition": definition.model_dump(mode="json")}


def _post(post_chat, definition, message, *, state="USER_EDITING_TEMPLATE"):
    return post_chat(
        message, state=state, snapshots=_snap(definition)
    ).json()


def _draft_name(payload):
    """响应里回传的业务对象的名字。"""
    draft = (payload.get("domainObjects") or {}).get("reportDefinitionDraft") or {}
    return (draft.get("metadata") or {}).get("name")


# ==========================================================================
# 🟠-5 问题分析路径与咨询路径复用同一套只读归一化
# ==========================================================================


def test_issue_analysis_uses_the_same_business_object_as_consultation(post_chat):
    """同一个事实：两条路径回传的**业务对象**必须同源。

    服务端会话记录里有一份草稿，客户端送的是另一份（多标签页 /
    客户端带旧草稿都会这样）。ADR 0002 §4.2：业务对象取服务端可信对象，
    服务端没有时才回退请求快照。

    修之前：咨询回传服务端那份，问题分析回传客户端送来的那份。
    """
    server_definition = build_definition_draft("服务端权威草稿", "room_a")
    client_definition = build_definition_draft("客户端送来的草稿", "room_a")
    session_service.repository.upsert_session(
        session_id="conv_test",
        state="USER_EDITING_TEMPLATE",
        title="测试会话",
        snapshot={"definition": server_definition.model_dump(mode="json")},
    )

    consult = _post(post_chat, client_definition, CONSULT_MESSAGE)
    issue = _post(post_chat, client_definition, ISSUE_MESSAGE)

    assert _draft_name(consult) == "服务端权威草稿"
    assert _draft_name(issue) == "服务端权威草稿", (
        "问题分析这一轮没有走共享的只读归一化，仍在回传客户端送来的草稿"
    )


def test_issue_analysis_shares_the_readonly_answer_contract(post_chat):
    """问题分析轮与咨询轮遵守**同一套**只读不变量。

    判据：ADR 0002 §4.2 的服务端强制不变量由 ``_normalize_query_answer``
    统一落实 —— 同一份实现，不该有两份。
    """
    definition = build_definition_draft("A 机房能耗日报", "room_a")

    payloads = {}
    for message in (CONSULT_MESSAGE, ISSUE_MESSAGE):
        # 两条路径从**同一份**服务端状态出发，读到的事实状态才可比。
        session_service.repository.upsert_session(
            session_id="conv_test",
            state="USER_EDITING_TEMPLATE",
            title="测试会话",
            snapshot={"definition": definition.model_dump(mode="json")},
        )
        payload = _post(post_chat, definition, message)
        payloads[message] = payload

        assert payload["turnKind"] == "query_answer", message
        assert payload["toolRequests"] == [], message
        assert payload["uiActions"] == ["query_answer"], message
        assert payload["state"] == "USER_EDITING_TEMPLATE", message
        assert payload["domainObjects"].get("pendingEditPlan") is None, message
        assert payload["debug"]["queryMode"] is True, message
        # 四态事实语义：归一化时会读会话事实，并把它记在 debug 里。
        assert payload["debug"]["factStatus"] in {
            "FOUND", "ABSENT", "UNAVAILABLE", "FORBIDDEN"
        }, message

    # 同一个事实 → 两条路径读到的会话事实状态必须一致。
    assert payloads[CONSULT_MESSAGE]["debug"]["factStatus"] == "FOUND"
    assert (
        payloads[CONSULT_MESSAGE]["debug"]["factStatus"]
        == payloads[ISSUE_MESSAGE]["debug"]["factStatus"]
    )


def test_issue_analysis_does_not_rebuild_business_objects(post_chat):
    """只追加聊天：不重建业务对象、不改待确认计划、不占澄清额度。

    判据：ADR 0002 §6.2、契约表 §7.3「业务对象**不重建**」。
    """
    definition = build_definition_draft("A 机房能耗日报", "room_a")
    session_service.repository.upsert_session(
        session_id="conv_test",
        state="USER_EDITING_TEMPLATE",
        title="测试会话",
        snapshot={"definition": definition.model_dump(mode="json")},
    )
    before = session_service.repository.get_session("conv_test")

    payload = _post(post_chat, definition, ISSUE_MESSAGE)

    after = session_service.repository.get_session("conv_test")
    assert after.state == before.state
    assert (after.snapshot or {}).get("definition") == (
        before.snapshot or {}
    ).get("definition"), "只读轮不允许重建业务对象"
    assert after.pendingEditPlan is None
    assert payload["domainObjects"]["pendingEditPlan"] is None
    # 只读咨询 / 问题反馈不进入澄清流程（契约表 §7.4）。
    assert session_service.load_clarification("conv_test") is None
