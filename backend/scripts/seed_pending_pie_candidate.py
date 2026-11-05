"""为**浏览器交互检查**准备一份「待确认」现场（**零模型调用**）。

用途：

    AI_SESSION_DB_PATH=/tmp/haewu-night-1104.db \
      /opt/anaconda3/bin/python3 backend/scripts/seed_pending_pie_candidate.py \
      --variant t04ui

产出（与服务端真实路径同构）：

* 一条带「告警列表」的草稿（``seed_alarm_edit_session`` 播的）；
* 一份**已检查候选**：走真实工具链（``submit_edit_plan`` → 隔离试做 → 已检查候选）；
* 会话快照里的**修改事项**（工具自己写的）；
* 「待确认修改」那张菜单锁（服务端 ``_finalize_template_edit`` 落库的那张）。

为什么不用真实模型：真实模型调用预算已经用完（3/3），而且这条现场要的是
**「待确认」这个状态本身**，不是模型怎么说话。

隔离：独立数据库（``AI_SESSION_DB_PATH``）+ 独立 sessionId / draftId，
不碰用户正在跑的草稿。
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = Path(__file__).resolve().parent
for path in (str(BACKEND_DIR), str(SCRIPTS_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)


def _candidate_card_question(step_id: str) -> dict:
    """服务端候选卡片的原样结构（``_assemble_capability_candidate`` 用的那张）。"""
    return {
        "id": "pending_edit_plan_confirm",
        "prompt": "确认这次修改：",
        "expects": "SINGLE_CHOICE",
        "selectionOnly": True,
        "lockScope": "input_and_menu",
        "allowsOther": True,
        "otherLabel": "其他",
        "otherPlaceholder": "也可以直接在这里写要怎么改",
        "options": [
            {"value": value, "label": value}
            for value in ("接受修改", "继续调整", "取消")
        ],
        "stepId": step_id,
        "stepVersion": 1,
        "stepKind": "menu",
    }


def _record_candidate_turn(
    session_id: str,
    conversation_id: str,
    payload: dict,
    step_id: str,
    scope: dict,
) -> None:
    """把「待确认候选」那一轮写进会话历史。

    走服务端自己的 ``record_turn``：确认文字取**试做核验出来的事实**
    （``summarize_facts``），候选载荷取**真实工具链产出的已检查候选**，
    卡片结构与服务端 ``_assemble_capability_candidate`` 一致。

    唯一不是模型产出的，是这句用户输入 —— 模型预算已用完（3/3），
    而这条现场要的是**「待确认」这个状态本身**，不是模型怎么说话。
    """
    from app.schemas.agent import (
        AgentQuestion,
        AgentResponse,
        AgentResponseDomainObjects,
        AgentState,
        QuestionOption,
    )
    from app.schemas.edit_plan import EditPlan
    from app.schemas.session import PendingEditPlan
    from app.services.edit_trial_service import summarize_facts
    from app.services.session_service import session_service

    facts = dict(payload.get("editFacts") or {})
    summary = str(payload.get("summary") or "") or summarize_facts(facts)
    plan = EditPlan.model_validate(payload)
    revision = int(facts.get("baseRevision") or 0)
    primary = str(facts.get("primaryTargetRegionId") or plan.target.regionId or "")

    candidate = PendingEditPlan(
        planId=payload.get("planId") or "",
        draftRevision=revision,
        targetRegionId=primary,
        operations=[step.model_dump(mode="json") for step in plan.operations],
        summary=summary,
        editPlan=plan.model_dump(mode="json"),
        editFacts=facts,
        checked=True,
        projectId=scope["projectId"],
        subjectRef=scope["subjectRef"],
        subjectId=scope["subjectId"],
    )
    question = _candidate_card_question(step_id)
    response = AgentResponse(
        conversationId=conversation_id,
        state=AgentState.USER_EDITING_TEMPLATE,
        reply=f"待确认的修改：\n{summary}\n接受后才会修改草稿。",
        domainObjects=AgentResponseDomainObjects(pendingEditPlan=candidate),
        questions=[
            AgentQuestion(
                id=question["id"],
                prompt=question["prompt"],
                expects="SINGLE_CHOICE",
                selectionOnly=True,
                lockScope="input_and_menu",
                allowsOther=True,
                otherLabel=question["otherLabel"],
                otherPlaceholder=question["otherPlaceholder"],
                stepId=step_id,
                stepVersion=1,
                stepKind="menu",
                options=[
                    QuestionOption(value=opt["value"], label=opt["label"])
                    for opt in question["options"]
                ],
            )
        ],
        uiActions=["pending_edit_plan", "template_editing"],
    )
    session_service.record_turn(
        session_id=session_id,
        user_text="将告警列表改成告警等级分布和告警区域分布",
        response=response,
        strict=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--variant", default="t04ui")
    parser.add_argument("--step-id", default="step_night_1104_pending_ui")
    args = parser.parse_args()

    os.environ.setdefault("AI_SESSION_DB_PATH", "/tmp/haewu-night-1104.db")

    from run_night_1104_flow import (
        _alarm_pie_ops,
        _build_checked_candidate,
        _save_candidate,
        _set_pending_edit_menu,
    )
    from seed_alarm_edit_session import _seed_one

    session_id, conversation_id, draft_id = _seed_one(args.variant)
    built = _build_checked_candidate(session_id, _alarm_pie_ops())
    if not built["submission"]:
        print(f"[seed-pending] 没能形成已检查候选：{built['toolResult']}")
        return 1
    # 作用域 = **浏览器**真正会发的那一套（前端 DEFAULT_CONTEXT）。
    # 作用域对不上，服务端会以「修改范围与生成计划时不一致」拒收。
    scope = {
        "projectId": "project_001",
        "subjectRef": "machine_room",
        "subjectId": "room_a",
    }
    candidate = _save_candidate(session_id, built["submission"], scope=scope)
    _set_pending_edit_menu(session_id, args.step_id)
    _record_candidate_turn(
        session_id, conversation_id, built["submission"]["payload"], args.step_id, scope
    )

    from app.services.edit_item_service import load_edit_item
    from app.services.session_service import session_service

    item = load_edit_item(session_id) or {}
    pending = session_service.load_pending_plan(session_id)
    choice = session_service.load_pending_choice(session_id)
    print(
        f"[seed-pending] sessionId={session_id} conversationId={conversation_id} "
        f"draftId={draft_id} planId={candidate.get('planId')} "
        f"pendingPlanId={getattr(pending, 'planId', None)} "
        f"itemOperations={len(item.get('operations') or [])} "
        f"menu={(choice or {}).get('question', {}).get('id')}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
