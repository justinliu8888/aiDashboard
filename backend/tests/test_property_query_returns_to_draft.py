from app.services.session_service import session_service
from tests.test_night_bug_regressions import T01_SENTENCE, _definition, _enter_edit_step, _snap


def test_completed_property_query_returns_to_draft_menu(post_chat):
    definition = _definition()
    step = _enter_edit_step(post_chat, definition)
    response = post_chat(
        "告诉我“昨日能耗趋势”的位置、尺寸和当前样式",
        state="USER_EDITING_TEMPLATE", snapshots=_snap(definition),
        stepId=step["stepId"], stepVersion=step["stepVersion"],
    ).json()
    assert response["error"] is None
    assert response["turnKind"] == "query_answer"
    assert response["debug"].get("queryReturnedToDraft") is True
    assert response["debug"].get("editingTargetRegionId", "missing") is None
    assert response["questions"][0]["id"] == "template_edit_confirm"
    assert response["questions"][0]["stepKind"] == "menu"
    assert response["questions"][0]["stepId"] != step["stepId"]
    pending = session_service.load_pending_choice("conv_test")
    assert pending["question"]["id"] == "template_edit_confirm"
    snapshot = session_service.load_snapshot("conv_test")
    assert snapshot["definition"] == definition.model_dump(mode="json")
    assert not snapshot.get("editingTargetRegionId")


def test_property_query_preserves_a_real_pending_edit(post_chat):
    definition = _definition()
    step = _enter_edit_step(post_chat, definition)
    candidate = post_chat(
        T01_SENTENCE, state="USER_EDITING_TEMPLATE", snapshots=_snap(definition),
        stepId=step["stepId"], stepVersion=step["stepVersion"],
    ).json()
    assert candidate["error"] is None
    before = session_service.load_pending_plan("conv_test").model_dump(mode="json")
    menu = session_service.load_pending_choice("conv_test")
    response = post_chat(
        "告诉我昨日能耗趋势的位置、尺寸和当前样式",
        state="USER_EDITING_TEMPLATE", snapshots=_snap(definition),
    ).json()
    assert response["error"] is None
    assert not response["debug"].get("queryReturnedToDraft")
    assert session_service.load_pending_plan("conv_test").model_dump(mode="json") == before
    assert session_service.load_pending_choice("conv_test") == menu
