"""K4 隔离会话播种：一份草稿 + 合法 NEXT_ACTION 菜单（不人工种 EditPlan）。

用法：
    AI_SESSION_DB_PATH=/tmp/haewu-page-edit-lock.db \
      /opt/anaconda3/bin/python3.13 backend/scripts/seed_page_compact_session.py

只播种**前置状态**（草稿、策略、草稿工作区基线、NEXT_ACTION 菜单），
**不**播种任何修改计划 —— 候选必须由真实自然语言入口产生。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

SESSION_ID = os.getenv("SEED_SESSION_ID", "sess_page_compact_k4")
CONVERSATION_ID = os.getenv("SEED_CONVERSATION_ID", "conv_page_compact_k4")
STATE = "USER_EDITING_TEMPLATE"
MENU_STEP_ID = "step_seed_next_action"


def main() -> int:
    from app.services.draft_workspace import draft_workspace
    from app.services.report_draft_service import build_definition_draft
    from app.services.session_service import session_service

    definition = build_definition_draft("A 机房能耗日报", "room_a")
    policy = None
    try:
        from app.services.report_policy_service import build_report_policy  # type: ignore
        policy = build_report_policy(definition)
    except Exception:  # noqa: BLE001 - 策略可有可无
        policy = None

    # 草稿工作区基线：与真实链路里「草稿第一次落库」同形。
    draft_id = f"draft_seed_{os.getpid()}"
    draft_workspace.create_draft(
        draft_id,
        session_id=SESSION_ID,
        name="A 机房能耗日报",
        definition=definition.model_dump(mode="json"),
    )

    snapshot = {
        "definition": definition.model_dump(mode="json"),
        "draftId": draft_id,
        "collectedInfo": {},
    }
    if policy is not None:
        snapshot["policy"] = policy.model_dump(mode="json")

    # 重播幂等：先清掉这个会话的旧消息。
    #
    # 每次交互跑一遍就会往会话里追加消息（用户输入 + 助手回复），
    # 不清的话刷新回放会把**同一张菜单**画好几遍，页面上的
    # .question-other / .question-select 会越堆越多，
    # 浏览器级验证就会打在历史副本上而不是当前那张卡上。
    repo = session_service.repository
    repo.list_messages(SESSION_ID)  # 确保连接已建立
    conn = getattr(repo, "_conn", None)
    if conn is not None:
        with repo._lock:
            conn.execute("DELETE FROM messages WHERE session_id = ?", (SESSION_ID,))
            conn.commit()

    session_service.repository.upsert_session(
        session_id=SESSION_ID, state=STATE, snapshot=snapshot,
    )
    # 合法 NEXT_ACTION 菜单：带步骤身份（服务端那一份权威记录同形）。
    menu_question = {
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
    }
    session_service.repository.set_pending_choice(
        SESSION_ID,
        {"state": STATE, "question": menu_question},
    )
    # 聊天历史里那条「画出这张菜单」的助手消息。
    #
    # 刷新后前端是**回放历史消息**把菜单画出来的（服务端 pending_choice 只
    # 负责恢复选择锁）—— 没有这条消息，页面上就只有锁、没有可点的菜单。
    # 这不是「人工种修改计划」：候选仍然必须由真实自然语言入口产生。
    session_service.repository.append_message(
        session_id=SESSION_ID,
        role="assistant",
        content="报表草稿已生成，右侧可以直接编辑。",
        payload={
            "conversationId": CONVERSATION_ID,
            "state": "WAITING_USER_CONFIRM",
            "reply": "报表草稿已生成，右侧可以直接编辑。",
            "questions": [menu_question],
            "uiActions": ["template_editing"],
            "domainObjects": {
                "reportDefinitionDraft": definition.model_dump(mode="json"),
            },
        },
    )
    print(f"seeded sessionId={SESSION_ID} conversationId={CONVERSATION_ID} draftId={draft_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
