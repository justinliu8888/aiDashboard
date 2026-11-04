"""权威步骤契约：受限修改输入的**参数门禁**（K2 / K4）。

从 NEXT_ACTION 真正选「修改」进入**受限修改输入**这一步之后：

- **缺来源**的普通文字 → 拒绝（``STEP_REF_REQUIRED``）；
- **旧步骤 / 别的卡片**的关联 → 拒绝（``STEP_REF_MISMATCH``）；
- 正确的步骤关联 → 可执行（候选照常产生）。

拒绝时定义、待确认计划、步骤身份、工作区 revision、撤销游标一个都不动。
"""
from __future__ import annotations

import pytest

from app.services.report_draft_service import build_definition_draft
from app.services.session_service import session_service

PAGE_COMPACT = "页面紧凑一点，标题保留，其他内容按阅读顺序排列"

MENU_STEP_ID = "step_menu_seed"


@pytest.fixture()
def definition():
    return build_definition_draft("A 机房能耗日报", "room_a")


def _snap(definition):
    return {"reportDefinition": definition.model_dump(mode="json")}


def _seed_next_action_menu(state: str = "USER_EDITING_TEMPLATE") -> None:
    """把会话摆成「模板已生成、等待下一步操作选择」这一步。

    直接写库（而不是走设计流程）：这一步的**权威记录**就是
    ``pending_choice``，种子与��务端写出来的那一份同形。
    """
    session_service.repository.upsert_session(
        session_id="conv_test", state=state, snapshot={},
    )
    session_service.repository.set_pending_choice(
        "conv_test",
        {
            "state": state,
            "question": {
                "id": "template_edit_confirm",
                "prompt": "请选择下一步操作：",
                "expects": "SINGLE_CHOICE",
                "selectionOnly": True,
                "allowsOther": True,
                "otherLabel": "其他",
                "otherPlaceholder": "直接输入修改要求",
                "options": [
                    {"value": "同意", "label": "同意"},
                    {"value": "修改", "label": "修改"},
                    {"value": "重新开始", "label": "重新开始"},
                ],
                "lockScope": "input_and_menu",
                "stepId": MENU_STEP_ID,
                "stepVersion": 1,
                "stepKind": "menu",
            },
        },
    )


def _enter_edit_step(post_chat, definition, *, state: str = "USER_EDITING_TEMPLATE") -> dict:
    """真正选「修改」，进入受限修改输入这一步，返回它的步骤关联。"""
    _seed_next_action_menu(state)
    response = post_chat(
        "修改", state=state, snapshots=_snap(definition),
    ).json()
    assert response["error"] is None
    step = response["questions"][0]
    assert step["id"] == "template_edit_request"
    assert step["stepKind"] == "edit_request"
    assert step["lockScope"] == "input_and_menu"
    assert step["stepId"] and step["stepId"] != MENU_STEP_ID
    assert step["stepVersion"] == 1
    # 步骤是**持久**的权威记录：刷新后仍然受限。
    stored = session_service.load_pending_choice("conv_test")
    assert stored is not None
    assert stored["question"]["stepId"] == step["stepId"]
    assert stored["question"]["stepKind"] == "edit_request"
    return step


def test_modify_enters_restricted_step(post_chat, definition):
    """选「修改」→ 受限修改输入：普通聊天锁住，步骤身份持久记录。"""
    _seed_next_action_menu()
    response = post_chat(
        "修改", state="USER_EDITING_TEMPLATE", snapshots=_snap(definition),
    ).json()
    assert response["error"] is None
    assert response["state"] == "USER_EDITING_TEMPLATE"
    question = response["questions"][0]
    assert question["stepKind"] == "edit_request"
    assert question["lockScope"] == "input_and_menu"
    assert question["allowedSubmissions"] == ["edit_text"]
    assert question["stepId"]
    stored = session_service.load_pending_choice("conv_test")
    assert stored["question"]["stepId"] == question["stepId"]


def test_restricted_step_rejects_text_without_step_ref(post_chat, definition):
    """缺来源的普通文字 → 拒绝；现场一个字节都不变。"""
    step = _enter_edit_step(post_chat, definition)
    before = session_service.load_snapshot("conv_test")

    rejected = post_chat(
        PAGE_COMPACT, state="USER_EDITING_TEMPLATE", snapshots=_snap(definition),
    ).json()

    assert rejected["error"]["code"] == "STEP_REF_REQUIRED"
    assert (rejected.get("domainObjects") or {}).get("pendingEditPlan") is None
    # 现场不变：步骤还在、定义没变、没��候选计划。
    assert session_service.load_pending_choice("conv_test")["question"]["stepId"] == step["stepId"]
    assert session_service.load_pending_plan("conv_test") is None
    assert session_service.load_snapshot("conv_test")["definition"] == before["definition"]


def test_restricted_step_rejects_stale_step_ref(post_chat, definition):
    """旧步骤 / 别的卡片的关联 → 拒绝；现场不变。"""
    _enter_edit_step(post_chat, definition)
    before = session_service.load_snapshot("conv_test")

    rejected = post_chat(
        PAGE_COMPACT, state="USER_EDITING_TEMPLATE", snapshots=_snap(definition),
        stepId="step_from_an_older_card", stepVersion=1,
    ).json()

    assert rejected["error"]["code"] == "STEP_REF_MISMATCH"
    assert (rejected.get("domainObjects") or {}).get("pendingEditPlan") is None
    assert session_service.load_pending_plan("conv_test") is None
    assert session_service.load_snapshot("conv_test")["definition"] == before["definition"]


def test_restricted_step_rejects_wrong_step_version(post_chat, definition):
    """版本对不上 → 拒绝（步骤内容变了，旧版本的提交不算数）。"""
    step = _enter_edit_step(post_chat, definition)

    rejected = post_chat(
        PAGE_COMPACT, state="USER_EDITING_TEMPLATE", snapshots=_snap(definition),
        stepId=step["stepId"], stepVersion=99,
    ).json()

    assert rejected["error"]["code"] == "STEP_REF_MISMATCH"


def test_restricted_step_accepts_correct_step_ref(post_chat, definition):
    """正确的步骤关联 → 可执行：整页紧凑照常产出候选。"""
    step = _enter_edit_step(post_chat, definition)

    accepted = post_chat(
        PAGE_COMPACT, state="USER_EDITING_TEMPLATE", snapshots=_snap(definition),
        stepId=step["stepId"], stepVersion=step["stepVersion"],
    ).json()

    assert accepted["error"] is None
    plan = accepted["domainObjects"]["pendingEditPlan"]
    assert plan is not None
    # 摘要说清新值、排列方式与保留项。
    assert "单列连续排布" in plan["summary"]
    assert " PX" in plan["summary"]
    assert "保留标题" in plan["summary"]
    # 候选产生后，这一步被候选菜单接管。
    stored = session_service.load_pending_choice("conv_test")
    assert stored["question"]["id"] == "pending_edit_plan_confirm"


def test_candidate_phase_keeps_the_plan_against_ordinary_text(post_chat, definition):
    """候选期：普通文字不能替换候选（候选 id 不变）。"""
    step = _enter_edit_step(post_chat, definition)
    candidate = post_chat(
        PAGE_COMPACT, state="USER_EDITING_TEMPLATE", snapshots=_snap(definition),
        stepId=step["stepId"], stepVersion=step["stepVersion"],
    ).json()
    plan_id = candidate["domainObjects"]["pendingEditPlan"]["planId"]

    post_chat(
        "顺便把别的地方也改了", state="USER_EDITING_TEMPLATE", snapshots=_snap(definition),
    ).json()

    assert session_service.load_pending_plan("conv_test").planId == plan_id


def test_clarification_keeps_the_step_and_the_lock(post_chat, definition):
    """说不清时这一步**不结束**：步骤还在，刷新后仍然受限。"""
    step = _enter_edit_step(post_chat, definition)

    asked = post_chat(
        "改一下", state="USER_EDITING_TEMPLATE", snapshots=_snap(definition),
        stepId=step["stepId"], stepVersion=step["stepVersion"],
    ).json()

    assert asked["error"] is None
    stored = session_service.load_pending_choice("conv_test")
    assert stored is not None
    assert stored["question"]["stepId"] == step["stepId"]
    assert stored["question"]["stepKind"] == "edit_request"
