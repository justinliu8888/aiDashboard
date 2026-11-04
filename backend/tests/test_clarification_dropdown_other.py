from app.schemas.agent import AgentQuestion, QuestionOption, AgentResponse, AgentState
from app.services.agent_service import AgentService


def test_clarification_menu_reuses_edit_step_without_separate_text_card(monkeypatch):
    service = object.__new__(AgentService)
    step = service._edit_request_step_question("draft-current")
    monkeypatch.setattr(service, "_current_edit_step", lambda _: step)
    menu = AgentQuestion(
        id="clarify", prompt="Choose a format", expects="SINGLE_CHOICE",
        selectionOnly=True,
        options=[QuestionOption(value="A", label="A")],
    )
    response = AgentResponse(
        conversationId="conv", state=AgentState.USER_EDITING_TEMPLATE,
        reply="Choose a format", questions=[menu],
    )
    service._attach_edit_step("session", response)
    assert len(response.questions) == 1
    combined = response.questions[0]
    assert combined.options == menu.options
    assert combined.prompt == menu.prompt
    assert combined.allowsOther
    assert combined.id == step.id
    assert combined.stepId == step.stepId
    assert combined.stepVersion == step.stepVersion
    assert combined.stepKind == step.stepKind
    assert combined.stepDraftId == step.stepDraftId
    assert combined.allowedSubmissions == step.allowedSubmissions
    assert combined.lockScope == step.lockScope
    service._attach_edit_step("session", response)
    assert len(response.questions) == 1
