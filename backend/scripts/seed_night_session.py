"""夜间专项（D 包）隔离会话播种：一份基础草稿 + 合法 NEXT_ACTION 菜单。

用法：

    # 单个会话
    AI_SESSION_DB_PATH=/tmp/haewu-night.db \
      /opt/anaconda3/bin/python3.13 backend/scripts/seed_night_session.py

    # 派生多个互不干扰的会话（每个 T 项一条独立草稿）
    AI_SESSION_DB_PATH=/tmp/haewu-night.db SEED_VARIANTS=t01,t04,t05,t09,t12 \
      /opt/anaconda3/bin/python3.13 backend/scripts/seed_night_session.py

    # 覆盖会话 / 会话名
    AI_SESSION_DB_PATH=/tmp/haewu-night.db \
      SEED_SESSION_ID=sess_night_t01 SEED_CONVERSATION_ID=conv_night_t01 \
      /opt/anaconda3/bin/python3.13 backend/scripts/seed_night_session.py

只播种**前置状态**（草稿、策略、草稿工作区基线、NEXT_ACTION 菜单、画菜单的
助手消息），**绝不**播种任何修改计划 —— 候选必须由真实自然语言入口产生。
这一点是硬要求：手工种候选会把「人工种植」冒充成「中文入口成功证据」。

隔离：
- 独立数据库（``AI_SESSION_DB_PATH``，默认 ``/tmp/haewu-night.db``）；
- 独立 sessionId / conversationId / draftId（每个 variant 一套）。

幂等：重复跑会先清掉该会话的旧消息，并重建同名草稿对象 —— 页面上的
``.question-select`` 不会越堆越多，浏览器级验证打在当前那张卡上。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

#: 默认会话标识（可用环境变量覆盖）。
BASE_SESSION_ID = os.getenv("SEED_SESSION_ID", "sess_night_d")


def _default_conversation_id(session_id: str) -> str:
    """会话名空间换了，会话名（conversationId）也跟着换。

    只覆盖 ``SEED_SESSION_ID`` 时 conversationId 必须跟着走 —— 否则两条不同
    名空间的会话会共用同一个 conversationId，在同一份库里互相踩。
    """
    if session_id.startswith("sess_"):
        return "conv_" + session_id[len("sess_"):]
    return "conv_" + session_id


#: 默认会话名（可用环境变量覆盖）。
BASE_CONVERSATION_ID = os.getenv("SEED_CONVERSATION_ID") or _default_conversation_id(BASE_SESSION_ID)

#: 隔离数据库。必须在**导入 app 配置之前**生效，所以这里只是读环境变量，
#: 真正的落库路径由 ``app.config`` 在 import 时读同一个环境变量。
DEFAULT_DB_PATH = "/tmp/haewu-night.db"

STATE = "USER_EDITING_TEMPLATE"
MENU_STEP_ID = "step_seed_night_next_action"

#: 保留的区域：**报表标题 + 三张正文卡**。
#:
#: 正文卡口径 = 前端可见正文卡片（REPORT_TITLE 不计入，见
#: ``app/agents/prompts/template_edit.md``「隐藏 REPORT_TITLE 不计入」）。
#:
#: - ``region_total_energy``    总耗电    METRIC_CARD / SUMMARY / columnSpan 4  → **可增宽样本**
#: - ``region_device_ranking``  设备耗电排名 TABLE      / DETAIL   / columnSpan 12 → **已满宽样本**
#: - ``region_hourly_trend``    能耗趋势   CHART      / TREND     / columnSpan 12 → **已满宽样本**
#:
#: ``build_definition_draft`` 还会产出 ``region_data_quality`` / ``region_analysis``
#: 两个辅助区 —— 它们**不是**本次三张正文卡，按契约收敛掉；两者的
#: ``intentBindings`` 本来就没有引用（只有总览 / 排名 / 趋势三条），
#: 所以删掉不会留下悬空引用。
KEEP_REGION_IDS = (
    "region_report_title",
    "region_total_energy",
    "region_device_ranking",
    "region_hourly_trend",
)

#: 卡片名改成**用户页面上的真名**（``01-手动验收bug列表.md`` 记录的真实名称）。
#:
#: ``build_definition_draft`` 的默认名是「总耗电 / 能耗趋势」；用户截图上
#: 实际叫「总耗电概览 / 能耗趋势分析」。**只改 name，不改 id / component /
#: 数据绑定 / placement** —— id 是稳定标识，改了会让断言与既有链路对不上。
#:
#: 这一步还是 T01「兼测名称与说明书不同」的前提：用户原话说的是
#: 「昨日能耗总览」，页面上的名字是「总耗电概览」，两者不一致 ——
#: 名字不一致正是那条澄清路径的触发条件。
REGION_DISPLAY_NAMES = {
    "region_total_energy": "总耗电概览",
    "region_hourly_trend": "能耗趋势分析",
}

REPORT_NAME = "A 机房能耗日报"


def _variants() -> list[str]:
    """要派生几套独立会话。

    ``SEED_VARIANTS`` 是逗号分隔的短名列表（``t01,t04``）。
    没给就只播种**一套**（基础会话）。
    """
    raw = os.getenv("SEED_VARIANTS", "").strip()
    if not raw:
        return [""]
    out: list[str] = []
    for item in raw.split(","):
        key = item.strip()
        if key and key not in out:
            out.append(key)
    return out or [""]


def _build_definition():
    """基础草稿定义：报表标题 + 三张正文卡。"""
    from app.services.report_draft_service import build_definition_draft

    definition = build_definition_draft(REPORT_NAME, "room_a")
    payload = definition.model_dump(mode="json")
    regions = payload.get("regions") or []
    kept = [r for r in regions if str(r.get("id")) in KEEP_REGION_IDS]
    missing = {rid for rid in KEEP_REGION_IDS} - {str(r.get("id")) for r in kept}
    if missing:
        # 宁可失败也不要种一份少区域的定义 —— 断言会拿它当基线。
        raise SystemExit(
            "build_definition_draft 没有产出预期区域："
            + "、".join(sorted(missing))
        )
    payload["regions"] = kept
    # 卡片名换成用户页面上的真名（只改 name，不动 id / 数据绑定 / placement）。
    for region in payload["regions"]:
        display_name = REGION_DISPLAY_NAMES.get(str(region.get("id")))
        if display_name:
            region["name"] = display_name
    return payload


def _seed_one(variant: str) -> tuple[str, str, str]:
    """种一套独立会话，返回 ``(sessionId, conversationId, draftId)``。"""
    from app.services.draft_workspace import draft_workspace
    from app.services.session_service import session_service

    if variant:
        session_id = f"{BASE_SESSION_ID}_{variant}"
        conversation_id = f"{BASE_CONVERSATION_ID}_{variant}"
        draft_id = f"draft_seed_night_{variant}"
    else:
        session_id = BASE_SESSION_ID
        conversation_id = BASE_CONVERSATION_ID
        draft_id = "draft_seed_night"

    definition = _build_definition()

    policy = None
    try:
        from app.services.report_policy_service import build_report_policy  # type: ignore

        policy = build_report_policy(definition)
    except Exception:  # noqa: BLE001 - 策略可有可无
        policy = None

    # ---- 幂等：同名草稿对象重建 ----
    #
    # ``create_draft`` 遇到同名对象会抛 ``DraftAlreadyExists``；
    # 重复跑种子时先删掉旧对象（含版本 / 历史 / 锁），再建基线。
    try:
        if draft_workspace.load_draft(draft_id) is not None:
            draft_workspace.adapter.delete_draft_object(draft_id)
    except Exception:  # noqa: BLE001 - 旧库结构不认这个表就跳过
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

    # ---- 幂等：先清掉这个会话的旧消息 ----
    #
    # 每次交互跑一遍就会往会话里追加消息（用户输入 + 助手回复），
    # 不清的话刷新回放会把**同一张菜单**画好几遍。
    repo = session_service.repository
    repo.list_messages(session_id)  # 确保连接已建立
    conn = getattr(repo, "_conn", None)
    if conn is not None:
        with repo._lock:
            conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            conn.commit()

    session_service.repository.upsert_session(
        session_id=session_id, state=STATE, snapshot=snapshot,
    )
    # 清掉上一轮遗留的**待确认计划**。
    #
    # 待确认计划存在 ``sessions.pending_edit_plan`` 独立列里，和
    # ``pending_choice`` 是两处真相：只重置 pending_choice 是不够的 ——
    # 服务端见到待确认计划就以「待确认计划」菜单为准，把 NEXT_ACTION 菜单
    # 顶掉，页面上点「修改」不再被当成模板编辑入口（会掉进普通上下文路由）。
    # 种子必须把会话复位成**干净基线**，否则同一会话第二次跑必然失效。
    session_service.clear_pending_plan(session_id)
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
        session_id,
        {"state": STATE, "question": menu_question},
    )
    # 聊天历史里那条「画出这张菜单」的助手消息。
    #
    # 刷新后前端是**回放历史消息**把菜单画出来的（服务端 pending_choice 只
    # 负责恢复选择锁）—— 没有这条消息，页面上就只有锁、没有可点的菜单。
    # 这不是「人工种修改计划」：候选仍然必须由真实自然语言入口产生。
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
    db_path = os.getenv("AI_SESSION_DB_PATH", DEFAULT_DB_PATH)
    print(f"db={db_path}")
    for variant in _variants():
        session_id, conversation_id, draft_id = _seed_one(variant)
        print(
            f"seeded sessionId={session_id} "
            f"conversationId={conversation_id} draftId={draft_id}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
