"""夜间专项（20261104 能力格式编辑）隔离会话播种：一份带「告警列表」的草稿。

用途：

    # 一套会话
    AI_SESSION_DB_PATH=/tmp/haewu-night-20261104.db \
      /opt/anaconda3/bin/python3 backend/scripts/seed_alarm_edit_session.py

    # 派生多个互不干扰的会话（每个 T 项一条独立草稿）
    AI_SESSION_DB_PATH=/tmp/haewu-night-20261104.db SEED_VARIANTS=t01,t02,t05 \
      /opt/anaconda3/bin/python3 backend/scripts/seed_alarm_edit_session.py

只播种**前置状态**（草稿、策略、草稿工作区基线、NEXT_ACTION 菜单、画菜单的
助手消息），**绝不**播种任何修改计划 —— 候选必须由真实自然语言入口产生。

隔离：
- 独立数据库（``AI_SESSION_DB_PATH``，默认 ``/tmp/haewu-night-20261104.db``）；
- 独立 sessionId / conversationId / draftId（每个 variant 一套）。

幂等：重复跑会先清掉该会话的旧消息，并重建同名草稿对象。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

BASE_SESSION_ID = os.getenv("SEED_SESSION_ID", "sess_night_1104")
DEFAULT_DB_PATH = "/tmp/haewu-night-20261104.db"

STATE = "USER_EDITING_TEMPLATE"
MENU_STEP_ID = "step_seed_1104_next_action"
REPORT_NAME = "A 机房能耗日报"


def _conversation_id(session_id: str) -> str:
    if session_id.startswith("sess_"):
        return "conv_" + session_id[len("sess_"):]
    return "conv_" + session_id


def _variants() -> list[str]:
    raw = os.getenv("SEED_VARIANTS", "").strip()
    if not raw:
        return [""]
    out: list[str] = []
    for item in raw.split(","):
        key = item.strip()
        if key and key not in out:
            out.append(key)
    return out or [""]


def _alarm_list_region() -> dict:
    """「告警列表」区块：本次需求要**替换掉**的那一块。

    它是一张**表格**（明细列表），不是图表 —— 替换后应当变成两张独立的
    分类统计图，旧表格消失。
    """
    return {
        "id": "region_alarm_list",
        "name": "告警列表",
        "role": "DETAIL",
        "layout": {"placement": {"type": "GRID", "column": 1, "columnSpan": 12}},
        "data": {
            "inputs": {"main": {"type": "SIMULATED", "periodRef": "CURRENT"}},
            "pipeline": {
                "nodes": [
                    {
                        "id": "n_alarm_rows",
                        "type": "ROWS",
                        "inputs": ["input:main"],
                    }
                ],
                "outputs": {
                    "main": {
                        "nodeRef": "node:n_alarm_rows",
                        "expectedFields": {
                            "alertTime": {"dataType": "DATETIME", "unit": ""},
                            "alertLevel": {"dataType": "STRING", "unit": ""},
                        },
                    }
                },
            },
        },
        "binding": {"outputRef": "output:main"},
        "component": {
            "type": "TABLE",
            "title": "告警列表",
            "columns": [
                {
                    "id": "col_time",
                    "label": "告警时间",
                    "fieldRef": "alertTime",
                    "format": {"kind": "DATETIME"},
                },
                {
                    "id": "col_level",
                    "label": "告警等级",
                    "fieldRef": "alertLevel",
                    "format": {"kind": "TEXT"},
                },
            ],
            "rowNumber": True,
        },
        "templateBinding": {
            "previewSpec": {
                "kind": "generic",
                "columns": [
                    {
                        "id": "col_time",
                        "label": "告警时间",
                        "fieldRef": "alertTime",
                        "format": {"kind": "DATETIME"},
                        "bindingKind": "simulated",
                    },
                    {
                        "id": "col_level",
                        "label": "告警等级",
                        "fieldRef": "alertLevel",
                        "format": {"kind": "TEXT"},
                        "bindingKind": "simulated",
                    },
                ],
            }
        },
    }


def _build_definition() -> dict:
    """基础草稿：报表标题 + 告警列表 + 两张既有卡片。"""
    from app.services.report_draft_service import build_definition_draft

    definition = build_definition_draft(REPORT_NAME, "room_a")
    payload = definition.model_dump(mode="json")
    keep = (
        "region_report_title",
        "region_total_energy",
        "region_hourly_trend",
    )
    regions = [r for r in payload.get("regions") or [] if str(r.get("id")) in keep]
    missing = {rid for rid in keep} - {str(r.get("id") or "") for r in regions}
    if missing:
        raise SystemExit(
            "build_definition_draft 没有产出预期区域：" + "、".join(sorted(missing))
        )
    for region in regions:
        if region["id"] == "region_total_energy":
            region["name"] = "总耗电概览"
        if region["id"] == "region_hourly_trend":
            region["name"] = "能耗趋势分析"
    # 告警列表插在总耗电概览**后面**（正文卡的阅读顺序）。
    insert_at = 1
    for index, region in enumerate(regions):
        if region["id"] == "region_total_energy":
            insert_at = index + 1
            break
    regions.insert(insert_at, _alarm_list_region())
    payload["regions"] = regions
    return payload


def _seed_one(variant: str) -> tuple[str, str, str]:
    from app.services.draft_workspace import draft_workspace
    from app.services.session_service import session_service

    if variant:
        session_id = f"{BASE_SESSION_ID}_{variant}"
        conversation_id = f"{_conversation_id(BASE_SESSION_ID)}_{variant}"
        draft_id = f"draft_seed_1104_{variant}"
    else:
        session_id = BASE_SESSION_ID
        conversation_id = _conversation_id(BASE_SESSION_ID)
        draft_id = "draft_seed_1104"

    definition = _build_definition()

    policy = None
    try:
        from app.services.report_policy_service import build_report_policy  # type: ignore

        policy = build_report_policy(definition)
    except Exception:  # noqa: BLE001
        policy = None

    try:
        if draft_workspace.load_draft(draft_id) is not None:
            draft_workspace.adapter.delete_draft_object(draft_id)
    except Exception:  # noqa: BLE001
        pass

    draft_workspace.create_draft(
        draft_id,
        session_id=session_id,
        name=REPORT_NAME,
        definition=definition,
    )

    snapshot = {
        "definition": definition,
        "draftId": draft_id,
        "collectedInfo": {},
    }
    if policy is not None:
        snapshot["policy"] = policy.model_dump(mode="json")

    repo = session_service.repository
    repo.list_messages(session_id)
    conn = getattr(repo, "_conn", None)
    if conn is not None:
        with repo._lock:
            conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            conn.commit()

    session_service.repository.upsert_session(
        session_id=session_id, state=STATE, snapshot=snapshot,
    )
    session_service.clear_pending_plan(session_id)

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
        session_id,
        {"state": STATE, "question": menu_question},
    )
    session_service.repository.append_message(
        session_id=session_id,
        role="assistant",
        content="报表草稿已生成，右侧可以直接编辑。",
        payload={
            "conversationId": conversation_id,
            "state": "WAITING_USER_CONFIRM",
            "reply": "报表草稿已生成，右侧可以直接编辑。",
            "questions": [menu_question],
            "uiActions": ["template_editing"],
            "domainObjects": {
                "reportDefinitionDraft": definition,
            },
        },
    )
    return session_id, conversation_id, draft_id


def main() -> int:
    os.environ.setdefault("AI_SESSION_DB_PATH", DEFAULT_DB_PATH)
    variants = _variants()
    for variant in variants:
        session_id, conversation_id, draft_id = _seed_one(variant)
        print(
            f"[seed] sessionId={session_id} conversationId={conversation_id} "
            f"draftId={draft_id} db={os.environ['AI_SESSION_DB_PATH']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
