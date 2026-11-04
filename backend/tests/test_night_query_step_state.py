"""属性查询的规则判定与状态流转。

用户要求：位置、尺寸和样式只需回答；完成后回到草稿级，不再追问修改。
真正的修改请求、模型说明和技术失败仍保留各自的步骤处理规则。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from app.schemas.agent import AgentState  # noqa: E402
from app.services import routing_rules  # noqa: E402
from app.services.agent_service import agent_service  # noqa: E402
from app.services.context_engine import classify_message_intent  # noqa: E402
from app.services.session_service import session_service  # noqa: E402
from tests.test_night_bug_regressions import (  # noqa: E402
    T01_SENTENCE,
    _definition,
    _enter_edit_step,
    _snap,
)

#: 用户现场那句话（去掉中文引号不影响判定，两种写法都测）。
QUESTION_SENTENCES = (
    "告诉我“昨日能耗趋势”的位置、尺寸和当前样式",
    "告诉我昨日能耗趋势的位置、尺寸和当前样式",
)


# ==========================================================================
# 一、规则层：陈述式问句要算提问，指令不能被误判成提问
# ==========================================================================

@pytest.mark.parametrize("sentence", QUESTION_SENTENCES)
def test_statement_question_is_readonly_question(sentence: str) -> None:
    """「告诉我 X 的位置、尺寸和当前样式」是**提问**，不是修改请求。"""
    assert routing_rules.is_readonly_question(sentence) is True
    assert routing_rules.looks_like_information_request(sentence) is True


@pytest.mark.parametrize("sentence", QUESTION_SENTENCES)
def test_statement_question_classifies_as_ask_question(sentence: str) -> None:
    """意图门（编辑态那一条）必须把它判成 ASK_QUESTION。

    判错的后果就是现场那个现象：掉进编辑快路 → 目标被释放（回到草稿级）
    → 回答之后跳出一张修改要求卡。
    """
    intent = classify_message_intent(sentence, subject_required=False)
    assert intent.value == "ASK_QUESTION"


@pytest.mark.parametrize(
    "sentence",
    [
        # 改了字：带改动动词 → 仍然是指令。
        "告诉我把总耗电概览改成深色卡片",
        # 既有指令不能因为这次放宽而被误判成提问。
        "把总耗电概览改成深色卡片",
        "把总耗电概览调宽一点",
        "给这份报表应用暗色主题",
    ],
)
def test_commands_are_not_misread_as_questions(sentence: str) -> None:
    """放宽的是「没有疑问标记」这一条，**不是**把指令也放进来。"""
    assert routing_rules.is_readonly_question(sentence) is False
    assert routing_rules.looks_like_information_request(sentence) is False


def test_generation_request_is_not_misread_as_question() -> None:
    """「给我一份…」是**产出请求**，不能被当成要信息。"""
    assert routing_rules.is_readonly_question("给我一份设备耗电明细") is False
    assert routing_rules.looks_like_information_request("给我一份设备耗电明细") is False


def test_asking_without_report_object_is_not_a_question() -> None:
    """没有业务对象的「告诉我…」不是在问报表。

    「请告诉我叫什么」是**向用户要信息**（例如主题名），不是提问 ——
    判错就会把要信息的请求当成咨询 answered 掉。
    """
    assert routing_rules.looks_like_information_request("请告诉我叫什么") is False


# ==========================================================================
# 二、受限修改输入那一步：问一句只读问题，**不动**现场
# ==========================================================================

def _step_ref(step: dict[str, Any]) -> dict[str, Any]:
    return {"stepId": step["stepId"], "stepVersion": step.get("stepVersion")}


@pytest.mark.parametrize("sentence", QUESTION_SENTENCES)
def test_property_question_inside_edit_step_returns_to_draft(post_chat, sentence: str) -> None:
    """属性查询只回答、不产生候选，完成后回到草稿菜单。

    这是用户现场那一句的完整回放（HTTP 层）。
    """
    definition = _definition()
    step = _enter_edit_step(post_chat, definition)
    before = definition.model_dump(mode="json")

    response = post_chat(
        sentence,
        state="USER_EDITING_TEMPLATE",
        snapshots=_snap(definition),
        **_step_ref(step),
    ).json()

    # 只读问答契约：这一轮**只回答**。
    assert response.get("error") is None, response.get("error")
    assert response["turnKind"] == "query_answer", response["turnKind"]
    assert response["uiActions"] == ["query_answer"], response["uiActions"]
    # 没有候选、没有待接受计划。
    assert not (response["domainObjects"] or {}).get("pendingEditPlan")
    # 状态**不推进也不回退**。
    assert response["state"] == "USER_EDITING_TEMPLATE", response["state"]
    # 草稿一个字都没动。
    assert session_service.load_snapshot("conv_test")["definition"] == before

    assert response["questions"][0]["id"] == "template_edit_confirm"
    assert response["questions"][0]["stepId"] != step["stepId"]
    pending = session_service.load_pending_choice("conv_test")
    assert pending
    assert pending["question"]["id"] == "template_edit_confirm"


def test_completed_property_question_releases_target(post_chat) -> None:
    """用户明确要求：属性查询回答完毕后释放临时目标，回到草稿级。"""
    definition = _definition()
    step = _enter_edit_step(post_chat, definition)

    response = post_chat(
        QUESTION_SENTENCES[0],
        state="USER_EDITING_TEMPLATE",
        snapshots=_snap(definition),
        **_step_ref(step),
    ).json()

    debug = response.get("debug") or {}
    assert debug.get("queryReturnedToDraft") is True
    assert debug.get("editingTargetRegionId", "missing") is None


def test_edit_request_inside_edit_step_still_produces_candidate(post_chat) -> None:
    """同一张卡里提**真修改要求** → 仍然出候选（没被这次放宽误伤）。"""
    definition = _definition()
    step = _enter_edit_step(post_chat, definition)

    accepted = post_chat(
        T01_SENTENCE,
        state="USER_EDITING_TEMPLATE",
        snapshots=_snap(definition),
        **_step_ref(step),
    ).json()

    assert accepted.get("error") is None, accepted.get("error")
    plan = (accepted["domainObjects"] or {}).get("pendingEditPlan")
    assert plan is not None, "合法的修改要求必须仍然能产出候选"
    assert plan["operations"][0]["operation"]["presentationOperations"]


# ==========================================================================
# 三、模型支路：属性查询完成后同样回到草稿菜单
# ==========================================================================

def test_property_answer_from_model_returns_to_draft(post_chat, monkeypatch) -> None:
    """注入模型说明响应，验证状态收敛，不测试真实模型的回答质量。"""
    from app.agents.fake_report_agent import FakeReportAgent
    from app.schemas.agent import AgentResponse

    definition = _definition()
    step = _enter_edit_step(post_chat, definition)

    class _AnswerOnlyAgent(FakeReportAgent):
        def handle(self, request):  # noqa: D102
            return AgentResponse(
                conversationId=request.conversationId,
                state=AgentState.USER_EDITING_TEMPLATE,
                reply="已查到那张卡的位置、尺寸和当前样式。",
                designSessionId=request.context.designSessionId,
                debug={"editOutcome": "answer"},
            )

        async def ahandle(self, request):  # noqa: D102
            return self.handle(request)

    monkeypatch.setattr(agent_service, "_agent", _AnswerOnlyAgent())

    response = post_chat(
        QUESTION_SENTENCES[0],
        state="USER_EDITING_TEMPLATE",
        snapshots=_snap(definition),
        **_step_ref(step),
    ).json()

    assert response.get("error") is None, response.get("error")
    assert response["debug"].get("queryReturnedToDraft") is True
    assert response["questions"][0]["id"] == "template_edit_confirm"
    assert response["questions"][0]["stepId"] != step["stepId"]
