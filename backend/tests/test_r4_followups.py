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

from app.schemas.fact import RECORDED_STAGE_DISCLOSURE
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


# ==========================================================================
# 🟠-5 的第二道边界：阶段边界校正（`_ensure_recorded_stage_disclosure`）
# ==========================================================================

#: 反馈路径上「问进度 + 说不满」的组合形状。
#:
#: 这个形状是**真**会触发那道校正的（不是造出来的假场景）：
#:   - `detect_issue_feedback` 命中（「好丑」是不满表达）→ 走问题分析；
#:   - `is_progress_question` 命中（「到哪一步」）→ 该说清边界；
#:   - 分析结论里没有编实时进度 → 走「补上口径句」这一支；
#:   - `queryOutcome` 既不是澄清也不是技术失败 → 这一支不提前返回。
PROGRESS_ISSUE_MESSAGE = "进度到哪一步了？看起来好丑"

#: 纯咨询形状：同样在问进度，走咨询路径。
PROGRESS_CONSULT_MESSAGE = "进度到哪一步了？"


def test_issue_analysis_path_also_gets_the_recorded_stage_disclosure(post_chat):
    """问题反馈路径上，`_ensure_recorded_stage_disclosure` **真的生效**。

    ADR 0002 §4.2：「**问进度时**，reply 必须同时说清两件事：这是**已记录**
    的状态、**不是实时进度**……服务端在模型漏说时补上 —— 不能只靠模型
    自觉」。

    复核 F-3：这条以前**没有任何用例钉住** —— 把反馈路径上的这道校正
    摘掉，全绿（测试盲区）。
    """
    definition = build_definition_draft("A 机房能耗日报", "room_a")

    payload = _post(post_chat, definition, PROGRESS_ISSUE_MESSAGE)

    # 先确认这一轮**走的是反馈路径**（不是咨询路径，也不是编辑路径）。
    assert payload["debug"].get("userIntent") == "ISSUE_FEEDBACK", (
        f"这一轮没走问题反馈路径：debug={payload['debug']}"
    )
    assert payload["debug"].get("editOutcome") == "issue_analysis"
    assert payload["turnKind"] == "query_answer"
    assert payload["uiActions"] == ["query_answer"]

    # 边界校正生效：服务端补上了「已记录 / 不是实时进度」这句口径。
    assert RECORDED_STAGE_DISCLOSURE in payload["reply"], (
        "问题反馈路径上没有做阶段边界校正"
    )


def test_consultation_path_gets_the_same_recorded_stage_disclosure(post_chat):
    """咨询路径上的同一道校正 —— 两条路径共用同一份实现。"""
    definition = build_definition_draft("A 机房能耗日报", "room_a")

    payload = _post(post_chat, definition, PROGRESS_CONSULT_MESSAGE)

    assert payload["debug"].get("userIntent") != "ISSUE_FEEDBACK", (
        "这一轮没走咨询路径"
    )
    assert RECORDED_STAGE_DISCLOSURE in payload["reply"], (
        "咨询路径上没有做阶段边界校正"
    )


def test_the_recorded_stage_disclosure_is_not_added_where_it_does_not_belong(post_chat):
    """问的不是进度 → **不补**这句边界（口径不能加错地方）。

    ``_ensure_recorded_stage_disclosure`` 的第三个边界：问草稿、问能力
    不补这条边界。这条断言同时把「校正的作用条件」钉住 —— 不是
    「凡是只读轮都补一句」。
    """
    definition = build_definition_draft("A 机房能耗日报", "room_a")

    payload = _post(post_chat, definition, ISSUE_MESSAGE)

    assert payload["debug"].get("userIntent") == "ISSUE_FEEDBACK"
    assert RECORDED_STAGE_DISCLOSURE not in payload["reply"], (
        "不是问进度的一轮不该补进度边界"
    )


# ==========================================================================
# 🟡-2 「这个图是什么意思」要能指向业务对象
# ==========================================================================

#: 带改动动词的说法 —— 它们**是指令**，不是提问。
CHANGE_COMMANDS = ("改成柱状图", "新建一张图", "把图删掉")


def test_graph_wording_is_treated_as_a_business_object():
    """「图」能指向报表业务对象 → 这句话是咨询。"""
    assert routing_rules.is_readonly_question("这个图是什么意思") is True
    assert (
        classify_message_intent(
            "这个图是什么意思", subject_required=False
        ) is UserIntent.ASK_QUESTION
    )


def test_change_commands_mentioning_graph_are_not_consultations():
    """带改动动词的说法仍然**不是**咨询（既有四条规则挡着）。

    钉住这一点：给业务对象词表补了「图」之后，不能把
    「改成柱状图」「新建一张图」「把图删掉」也变成咨询。
    """
    for message in CHANGE_COMMANDS:
        assert routing_rules.is_readonly_question(message) is False, message
        assert (
            classify_message_intent(
                message, subject_required=False
            ) is not UserIntent.ASK_QUESTION
        ), message


def test_unrelated_graph_words_are_not_business_objects():
    """含「图」但与报表无关的词，整词都**不**是报表业务对象。

    复核 F-2：单字「图」的子串匹配把「图腾」「意图」「图案」「版图」
    判成了咨询 —— 用户问与报表毫无关系的事，系统却当成「问当前报表」
    的咨询，答出一段关于报表草稿的话。
    """
    for message in (
        "这本图书是什么意思",
        "这张地图是什么意思",
        "图腾是什么意思",
        "意图是什么",
        "图案是什么意思",
        "版图是什么意思",
        "这个图标是什么意思",
        "这张图片是什么意思",
        "这个蓝图是什么意思",
        "这个企图是什么意思",
        "这张图纸是什么意思",
        "这个图章是什么意思",
        "这个图钉是什么意思",
        "这个构图是什么意思",
    ):
        assert routing_rules.is_readonly_question(message) is False, message

    # 词表的**内容**也要钉住 —— 只遍历表本身的话，从表里删掉一个词
    # 没人拦（遍历会跟着变短，看起来还是绿的）。
    for word in (
        "图书", "地图", "图标", "图片", "蓝图", "企图",
        "图腾", "图案", "版图", "意图",
        "图纸", "图章", "图钉", "构图",
    ):
        assert word in routing_rules._NON_REPORT_GRAPH_WORDS, word


def test_every_excluded_graph_word_is_not_a_business_object():
    """排除词表里**每一个**词都钉住 —— 以后新收进来的词自动覆盖。

    这张表是**封闭词表**（需随产品词表同步）。表的内容由
    ``test_unrelated_graph_words_are_not_business_objects`` 逐条钉住；
    这里遍历表本身，保证新收进来的词也不会把无关句判成咨询。
    """
    for word in routing_rules._NON_REPORT_GRAPH_WORDS:
        message = f"这个{word}是什么意思"
        assert routing_rules.is_readonly_question(message) is False, message


def test_graph_business_objects_are_still_consultations():
    """加了排除词不许把**真·报表对象**一起打掉（复核 F-2 的另一半）。

    不能改成「独立成词」的正则判定：「这张图」的「图」前面是量词「张」，
    独立成词会把刚修好的形状打回去。
    """
    for message in (
        "这个图是什么意思",
        "这张图有什么问题",
        "这张图的数据怎么来的",
        "图书库存那张图是什么意思",
        "这个图表是什么意思",
    ):
        assert routing_rules.is_readonly_question(message) is True, message
        assert (
            classify_message_intent(
                message, subject_required=False
            ) is UserIntent.ASK_QUESTION
        ), message


def test_graph_wording_answers_without_touching_the_draft(post_chat):
    """「这个图是什么意思」→ 只读回答：不动草稿、不产生计划、不占额度。"""
    definition = build_definition_draft("A 机房能耗日报", "room_a")

    payload = _post(post_chat, definition, "这个图是什么意思")

    assert payload["debug"]["contextEngineIntent"] == "ASK_QUESTION"
    assert payload["uiActions"] == ["query_answer"]
    assert payload["turnKind"] == "query_answer"
    assert payload["domainObjects"].get("pendingEditPlan") is None
    # 咨询不占澄清额度（契约表 §7.4）。
    assert session_service.load_clarification("conv_test") is None


def test_graph_wording_with_change_verb_still_edits(post_chat):
    """带改动动词的说法仍然走编辑流程，不被当成咨询。"""
    definition = build_definition_draft("A 机房能耗日报", "room_a")

    payload = _post(post_chat, definition, "把图删掉")

    assert payload["debug"].get("contextEngineIntent") != "ASK_QUESTION"
    assert payload["uiActions"] != ["query_answer"]


# ==========================================================================
# 🟡-3 澄清额度键：「目标由未知变已知」算同一件事
# ==========================================================================


def test_target_becoming_known_continues_the_same_item(post_chat):
    """第一轮没说改哪一块 → 第二轮把目标补上 → **同一件事**，不再追问。

    契约表 §7.4（v1.11.0 统筹裁定）：「目标由『未知』变成『已知』，
    是同一事项的延续，不是新事项」—— 额度键沿用上一轮的键，轮次接着算。

    修之前：第二轮的键从 `CHANGE_VISUALIZATION:` 变成
    `CHANGE_VISUALIZATION:region_hourly_trend` → 按「键变了 = 新事项」
    轮次从 0 起 → 又问了一轮，规格第 23 条守不住。
    """
    definition = build_definition_draft("A 机房能耗日报", "room_a")

    first = _post(post_chat, definition, "改成柱状图")

    # 第一轮真的抛出了追问，额度记在「意图:目标为空」的键上。
    assert "你想把哪个 section" in first["reply"]
    assert first["uiActions"] != ["template_edit_clarification_limit"]
    state = session_service.load_clarification("conv_test")
    assert state.key == "CHANGE_VISUALIZATION:"
    assert state.rounds == 1

    second = _post(post_chat, definition, "把小时趋势改成柱状图")

    # 第二轮把目标补上了 → 同一件事 → 额度接着算 → 不再追问。
    assert second["uiActions"] == ["template_edit_clarification_limit"]
    assert "追问过 1 轮" in second["reply"]
    assert second["questions"] == []
    state = session_service.load_clarification("conv_test")
    # 额度用尽时，这一轮**解析出来的目标要写回额度键**（契约表 §7.4）：
    # 键从「意图:」变成「意图:目标卡片」。
    assert state.key == "CHANGE_VISUALIZATION:region_hourly_trend", (
        "额度用尽时要把已解析的目标写回额度键"
    )
    assert state.rounds == 1, "轮次接着算，不该重开"


def test_the_same_item_is_not_asked_a_second_time(post_chat):
    """同一件事**只被追问一轮**（复核 F-1）。

    三轮形状：r1 追问 → r2（用户把目标补上）被额度挡住 → r3（同一句再说
    一遍）**不再被追问**。

    修之前：r2 被挡住时额度键还停在「目标为空」的旧键上，而
    ``_derive_clarification_key`` 的沿用分支要求 ``pendingEditRequest``
    还在（r2 刚把它清掉）→ r3 退回「键变了 = 新事项」→ 又问了一轮。
    """
    definition = build_definition_draft("A 机房能耗日报", "room_a")

    first = _post(post_chat, definition, "改成柱状图")
    assert first["uiActions"] != ["template_edit_clarification_limit"]
    assert first["questions"], "第一轮应该真的追问"

    second = _post(post_chat, definition, "把小时趋势改成柱状图")
    assert second["uiActions"] == ["template_edit_clarification_limit"]

    third = _post(post_chat, definition, "把小时趋势改成柱状图")

    # 同一件事 → 额度已经用完 → 不许再追问一轮。
    assert third["uiActions"] == ["template_edit_clarification_limit"], (
        f"同一件事又被追问了一轮：uiActions={third['uiActions']}"
    )
    assert third["questions"] == [], "同一件事又被追问了一轮"
    assert "ask_template_edit_detail" not in (third["uiActions"] or [])
    assert "template_editing" not in (third["uiActions"] or [])
    state = session_service.load_clarification("conv_test")
    assert state.key == "CHANGE_VISUALIZATION:region_hourly_trend"
    assert state.rounds == 1, "同一件事的额度不该被重开"


def test_the_exhausted_reply_does_not_claim_the_target_is_unknown(post_chat):
    """被挡住时的文案不许说假话（契约表 §8 不变量 8）。

    用户明明点名了「小时趋势」，同一条回复里再说「还是没能确定」就是
    自相矛盾 —— 这一轮**目标已经定下来**，只是这个改法行不通。
    """
    definition = build_definition_draft("A 机房能耗日报", "room_a")

    _post(post_chat, definition, "改成柱状图")
    second = _post(post_chat, definition, "把小时趋势改成柱状图")

    assert "还是没能确定" not in second["reply"], (
        "目标已经定下来，却说「还是没能确定」"
    )
    # 真正的原因照原样引用，不改写、不总结。
    assert "「小时趋势」的数据形态不适合这种展示方式" in second["reply"]
    # 额度用完了这一事实要说清楚。
    assert "额度用完了" in second["reply"]
    # 边界那句逐字保留。
    assert "当前报表保持原样，草稿没有修改，也没有替你接受任何改动。" in second["reply"]


def test_the_exhausted_reply_still_says_unknown_when_target_is_unknown(post_chat):
    """目标**还没定下来**时，「还是没能确定」的口径保留。"""
    definition = build_definition_draft("A 机房能耗日报", "room_a")

    _post(post_chat, definition, "改成柱状图")
    second = _post(post_chat, definition, "改成柱状图")

    assert second["uiActions"] == ["template_edit_clarification_limit"]
    assert "还是没能确定" in second["reply"]
    assert "当前报表保持原样，草稿没有修改，也没有替你接受任何改动。" in second["reply"]


def test_a_different_card_after_exhaustion_can_still_be_asked(post_chat):
    """额度用尽后**换另一张卡片** → 仍能重新追问一轮（复核 F-1 另一半）。

    额度按「事项」计：换一张卡片就是另一件事，轮次从 0 起。
    """
    definition = build_definition_draft("A 机房能耗日报", "room_a")

    _post(post_chat, definition, "改成柱状图")
    _post(post_chat, definition, "把小时趋势改成柱状图")
    assert session_service.load_clarification("conv_test").rounds == 1

    other = _post(post_chat, definition, "把总耗电改成柱状图")

    assert other["uiActions"] != ["template_edit_clarification_limit"], (
        "换了一张卡片却被上一张卡片的额度挡住了"
    )
    assert other["questions"], "换一张卡片应该重新被追问"
    assert "追问过 1 轮" not in other["reply"]
    state = session_service.load_clarification("conv_test")
    assert state.key == "CHANGE_VISUALIZATION:region_total_energy"
    assert state.rounds == 1, "新事项的轮次从 0 起算成 1"


def test_a_different_intent_can_still_be_asked_once(post_chat):
    """换一件事（意图变了）→ 仍能重新追问一轮。

    契约表 §7.4：这**不**放宽「换一件事要能重新追问」。
    """
    definition = build_definition_draft("A 机房能耗日报", "room_a")

    first = _post(post_chat, definition, "改成柱状图")
    assert "你想把哪个 section" in first["reply"]

    second = _post(post_chat, definition, "把粒度改成按月")

    assert second["uiActions"] != ["template_edit_clarification_limit"], (
        "新事项被上一事项的澄清额度挡住了"
    )
    assert "追问过 1 轮" not in second["reply"]
    assert second["questions"], "新事项应该重新被追问"
    state = session_service.load_clarification("conv_test")
    assert state.key == "CHANGE_COMPILATION_GRAIN:"
    assert state.rounds == 1


def test_a_different_target_card_is_a_new_item(post_chat):
    """目标换了**另一张卡片** → 仍是新事项 → 仍能重新追问一轮。

    契约表 §7.4 v1.11.0：「意图变了、或目标换了另一张卡片，都是新事项，
    轮次照旧从 0 起」。
    """
    definition = build_definition_draft("A 机房能耗日报", "room_a")

    first = _post(post_chat, definition, "把小时趋势改成柱状图")
    assert first["uiActions"] != ["template_edit_clarification_limit"]
    state = session_service.load_clarification("conv_test")
    assert state.key == "CHANGE_VISUALIZATION:region_hourly_trend"

    second = _post(post_chat, definition, "把总耗电改成柱状图")

    assert second["uiActions"] != ["template_edit_clarification_limit"], (
        "换了一张卡片却被上一张卡片的额度挡住了"
    )
    state = session_service.load_clarification("conv_test")
    assert state.key == "CHANGE_VISUALIZATION:region_total_energy"
    assert state.rounds == 1


def test_the_same_item_with_a_known_target_is_still_blocked(post_chat):
    """同一张卡片重复要求 → 仍然只问一轮（既有行为不许被改坏）。"""
    definition = build_definition_draft("A 机房能耗日报", "room_a")

    first = _post(post_chat, definition, "把小时趋势改成柱状图")
    assert first["uiActions"] != ["template_edit_clarification_limit"]

    second = _post(post_chat, definition, "把小时趋势改成柱状图")

    assert second["uiActions"] == ["template_edit_clarification_limit"]
    assert "追问过 1 轮" in second["reply"]
    state = session_service.load_clarification("conv_test")
    assert state.key == "CHANGE_VISUALIZATION:region_hourly_trend"
    assert state.rounds == 1


def test_a_new_item_is_not_blocked_by_an_exhausted_one(post_chat):
    """上一件事的额度用完了，也不能挡住**下一件事**。

    这是 v1.4.1 之前的老 bug（「把键钉死在旧键上」），修的时候不许带回来。
    """
    definition = build_definition_draft("A 机房能耗日报", "room_a")

    _post(post_chat, definition, "改成柱状图")
    _post(post_chat, definition, "把小时趋势改成柱状图")
    assert session_service.load_clarification("conv_test").rounds == 1

    other = _post(post_chat, definition, "把粒度改成按月")

    assert other["uiActions"] != ["template_edit_clarification_limit"]
    assert "追问过 1 轮" not in other["reply"]
    assert other["questions"], "新事项应该重新被追问"


def test_consultation_still_does_not_consume_the_quota(post_chat):
    """咨询轮不占额度（契约表 §7.4）—— 这条既有行为不许被改坏。"""
    definition = build_definition_draft("A 机房能耗日报", "room_a")

    for _ in range(3):
        payload = _post(post_chat, definition, "这个图是什么意思")
        assert payload["uiActions"] == ["query_answer"]

    assert session_service.load_clarification("conv_test") is None
