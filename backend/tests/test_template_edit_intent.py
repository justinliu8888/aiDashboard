"""工单 #58：意图四分类与「一个点」误判修复的验收用例。

覆盖工单 #58 的九条验收标准。每条用例都对着**用户看得见的行为**断言：
输入框里说了什么 → 屏幕上出现了什么 / 什么没出现。

用例不写死设备名与日期：卡片名、时间窗、粒度都从当天快照推导。
"""
from __future__ import annotations

import re

import pytest

from app.schemas.agent import AgentState
from app.schemas.edit_plan import EditIntent
from app.services import routing_rules
from app.services.agent_service import TEMPLATE_EDIT_HINT_WORDS, agent_service
from app.services.context_engine import (
    UserIntent,
    classify_message_intent,
    context_engine,
)
from app.services.edit_time_resolver import (
    extract_change_value,
    resolve_time_expression,
)
from app.services.report_draft_service import build_definition_draft
from app.services.report_issue_analysis import analyze_region
from app.services.session_service import session_service
from app.services.template_edit_engine import template_edit_engine
from app.tools.context import ToolScope


SCOPE = ToolScope(project_id="project_001", subject_ref="machine_room", subject_id="room_a")

# 工单正文里的三句话，逐字保留。
FEEDBACK_MESSAGE = "昨日能耗趋势好丑啊，一个点……"
GRAIN_COMMAND_MESSAGE = "把昨日能耗趋势改成按小时展示"
CONSULT_MESSAGE = "冰箱耗电明细可以用其他展示方法吗？"


@pytest.fixture()
def definition():
    return build_definition_draft("A 机房能耗日报", "room_a")


def _snap(definition):
    return {"reportDefinition": definition.model_dump(mode="json")}


def _post(post_chat, definition, message, *, state="USER_EDITING_TEMPLATE", **kwargs):
    return post_chat(
        message,
        state=state,
        snapshots=_snap(definition),
        **kwargs,
    ).json()


def _trend_region(definition):
    return next(
        region for region in definition.regions if region.get("role") == "TREND"
    )


# ==========================================================================
# AC1 意图体系扩展「问题反馈」与「目录外新增」两类，沿用既有两层结构
# ==========================================================================


def test_user_intent_extends_the_existing_enum():
    """既有九类一个不动，只新增两类；不另起一套平行枚举。"""
    original = {
        "CONFIRM", "MODIFY", "REJECT", "ASK_QUESTION", "CHITCHAT",
        "CANCEL", "NEW_TASK", "PROVIDE_INFO", "UNKNOWN",
    }
    values = {item.value for item in UserIntent}

    assert original <= values, "既有的九类意图不能被改名或删掉"
    assert "ISSUE_FEEDBACK" in values
    assert "OUT_OF_CATALOG_ADD" in values
    assert len(values) == len(original) + 2, "只新增两类，不另起体系"


def test_rule_layer_detects_issue_feedback_and_out_of_catalog_add():
    """规则层能分别认出「问题反馈」与「目录外新增」。"""
    assert routing_rules.detect_issue_feedback(FEEDBACK_MESSAGE) is True
    assert routing_rules.detect_out_of_catalog_add(
        "新增一个设备温度指标",
        known_metrics=["总耗电", "设备耗电", "能耗成本", "PUE"],
    ) is True
    # 已登记的指标不算目录外新增。
    assert routing_rules.detect_out_of_catalog_add(
        "新增一个总耗电指标",
        known_metrics=["总耗电", "设备耗电", "能耗成本", "PUE"],
    ) is False


def test_model_layer_prompts_know_the_two_new_intents():
    """模型深路由那一层也要认得新意图，否则两层结构是断的。"""
    from app.services import context_engine as ce

    assert "ISSUE_FEEDBACK" in ce._SYSTEM_PROMPT
    assert "OUT_OF_CATALOG_ADD" in ce._SYSTEM_PROMPT
    assert "ISSUE_FEEDBACK" in ce._CONCIERGE_PROMPT


def test_prompt_enum_count_matches_the_enum():
    """提示词里写死的枚举个数必须与实际枚举一致。

    #58 加了两个新意图，把「11 个值」改了一半 —— JSON 输出模板里还写着
    「上面 9 个枚举值之一」。发给模型的文本自相矛盾，可能让模型漏掉新意图。
    """
    from app.services import context_engine as ce

    count = len(UserIntent)
    assert count == 11, "既有九类 + 本轮新增两类 = 11"

    # 提示词里出现的枚举个数必须只有一个，而且等于实际枚举个数。
    claimed = set(re.findall(r"(\d+)\s*个(?:值|枚举值)", ce._CONCIERGE_PROMPT))
    assert claimed == {str(count)}, f"提示词里的枚举个数与实际不符：{claimed}"


def test_coerce_intent_maps_new_aliases():
    assert ce_coerce("FEEDBACK") is UserIntent.ISSUE_FEEDBACK
    assert ce_coerce("ADD_OUT_OF_CATALOG") is UserIntent.OUT_OF_CATALOG_ADD
    # 枚举外的值仍然落 UNKNOWN，不炸。
    assert ce_coerce("NOT_A_REAL_INTENT") is UserIntent.UNKNOWN


def ce_coerce(value):
    from app.services.context_engine import coerce_intent

    return coerce_intent(value)


# ==========================================================================
# AC2 意图识别排在任何会产生编辑候选的规则之前
# ==========================================================================


def test_intent_gate_runs_before_the_deterministic_edit_fast_path(
    post_chat, definition, monkeypatch
):
    """意图识别在编辑快路**之前**：反馈消息根本不会进解析器。"""

    def _boom(**_kwargs):
        raise AssertionError("意图识别没排在编辑快路之前：反馈消息被送进了解析器")

    monkeypatch.setattr(template_edit_engine, "parse", _boom)

    payload = _post(post_chat, definition, FEEDBACK_MESSAGE)

    assert payload["domainObjects"]["pendingEditPlan"] is None
    assert payload["debug"]["userIntent"] == UserIntent.ISSUE_FEEDBACK.value


def test_intent_gate_also_covers_the_model_edit_branch(
    post_chat, definition, monkeypatch
):
    """模型编辑支路（ReAct）同样被意图门覆盖：咨询不进模型编辑。"""

    class ExplodingAgent:
        async def ahandle(self, request):
            raise AssertionError("咨询不应进入模型编辑支路")

    monkeypatch.setattr(agent_service, "_agent", ExplodingAgent())

    payload = _post(post_chat, definition, CONSULT_MESSAGE)

    assert payload["domainObjects"]["pendingEditPlan"] is None
    assert payload["domainObjects"]["changeSet"] is None


# ==========================================================================
# AC3 「昨日能耗趋势好丑啊，一个点……」不产生任何修改计划，改走问题分析
# ==========================================================================


def test_issue_feedback_produces_no_edit_plan(post_chat, definition):
    """用户说「好丑啊，一个点」→ 屏幕上只有一段分析，没有待确认计划。"""
    payload = _post(post_chat, definition, FEEDBACK_MESSAGE)

    assert payload["domainObjects"]["pendingEditPlan"] is None
    assert payload["domainObjects"]["changeSet"] is None
    assert payload["uiActions"] == ["query_answer"]
    assert payload["turnKind"] == "query_answer"
    assert payload["state"] == "USER_EDITING_TEMPLATE"
    assert definition.metadata["revision"] == 0
    assert session_service.load_pending_plan("conv_test") is None


def test_issue_feedback_reads_actual_definition_window_and_grain(
    post_chat, definition
):
    """分析读的是**实际定义**：时间窗、粒度、渲染点数都来自当前草稿。"""
    payload = _post(post_chat, definition, FEEDBACK_MESSAGE)

    analysis = payload["debug"]["issueAnalysis"]
    region = _trend_region(definition)
    window = region["context"]["timeWindow"]["value"]
    grain = region["context"]["grain"]["value"]["unit"]

    assert analysis["regionId"] == region["id"]
    assert analysis["grainLabel"] == "小时"
    assert analysis["windowLabel"]
    assert analysis["expectedPoints"] == 24, "1 天 × 24 小时 = 24 个点"
    assert analysis["renderedPoints"] == 24, "渲染载荷里实际渲染出的点数"
    assert analysis["evidenceSufficient"] is True
    # 答复里说的是这张卡片的真实状态，不是用户话术里的关键词。
    assert "小时趋势" in payload["reply"]
    assert "草稿没有变化" in payload["reply"]


def test_issue_feedback_does_not_claim_a_time_window_change(
    post_chat, definition
):
    """反馈句里的「昨日」是卡片名的一部分，不能被当成要写进去的时间窗。

    答复里可以说这张卡片**现在**的时间窗是什么（那是事实），
    但不能出现「把时间窗改成……」这类动作。
    """
    payload = _post(post_chat, definition, FEEDBACK_MESSAGE)

    assert payload["domainObjects"]["pendingEditPlan"] is None
    assert "时间窗改成" not in payload["reply"]
    assert "改成昨天" not in payload["reply"]


# ==========================================================================
# AC4 时间窗与粒度的新值只来自明确的改动动词片段 / 澄清答案 / 已确认上下文
# ==========================================================================


def test_change_value_fragment_needs_an_explicit_change_verb():
    """没有改动动词就没有新值片段（不再回退整句）。"""
    assert extract_change_value(FEEDBACK_MESSAGE) == ""
    assert extract_change_value("昨日能耗趋势") == ""
    assert extract_change_value("把趋势改成最近 7 天") == "最近 7 天"


def test_card_name_time_word_is_only_for_locating_the_object(definition):
    """卡片名里的时间词只用来定位对象，不是新值。"""
    plan = template_edit_engine.parse(
        message=FEEDBACK_MESSAGE,
        definition=definition,
        selected_region_id=None,
        scope=SCOPE,
    )

    assert plan.intent != EditIntent.CHANGE_TIME_RANGE
    assert plan.operation.timeWindow is None


def test_clarification_answer_is_a_valid_source_of_the_new_value(
    post_chat, definition
):
    """有效的澄清答案是新值的合法来源（规格第 48 条）。"""
    first = _post(post_chat, definition, "把趋势改成昨天下午")

    options = first["questions"][0]["options"]
    assert options, "时段表达必须给出具体候选让用户选"
    assert first["domainObjects"]["pendingEditPlan"] is None

    second = _post(post_chat, definition, options[0]["value"])

    plan = second["domainObjects"]["pendingEditPlan"]
    assert plan is not None
    value = plan["operations"][0]["operation"]["timeWindow"]["value"]
    assert value["type"] == "FIXED_INTERVAL"
    assert definition.metadata["revision"] == 0


def test_card_name_grain_word_does_not_become_a_new_grain(definition):
    """「小时趋势」里的「小时」是卡片名，不是要写进去的粒度。"""
    plan = template_edit_engine.parse(
        message="把能耗趋势的标题改成小时趋势",
        definition=definition,
        selected_region_id=None,
        scope=SCOPE,
    )

    assert plan.intent == EditIntent.RENAME_SECTION
    assert plan.operation.grain is None


def test_grain_new_value_comes_from_the_change_fragment(definition):
    """明确改动动词片段里的粒度才是新值。"""
    plan = template_edit_engine.parse(
        message=GRAIN_COMMAND_MESSAGE,
        definition=definition,
        selected_region_id=None,
        scope=SCOPE,
    )

    assert plan.intent == EditIntent.CHANGE_COMPILATION_GRAIN
    assert plan.operation.grain["unit"] == "HOUR"
    assert plan.operation.timeWindow is None


# ==========================================================================
# AC5 两个时间表达解析器共用同一口径；入口提示词不再收裸时间词
# ==========================================================================


def test_both_time_parsers_agree_on_the_same_fragment():
    """编辑引擎与时间表达解析器对同一片段给出同一个窗口。"""
    for fragment in ("最近 7 天", "昨天", "本周"):
        engine_window, engine_label = template_edit_engine._parse_time_range(fragment)
        resolution = resolve_time_expression(fragment)

        assert resolution.status == "RESOLVED"
        assert engine_window == resolution.timeWindow
        assert engine_label == resolution.label


def test_entry_hint_words_do_not_contain_bare_time_words():
    """入口提示词表不再因为句子里出现一个时间词就把整句送进编辑快路。"""
    for word in ("昨日", "本周", "本月", "最近 7 天", "最近 30 天"):
        assert word not in TEMPLATE_EDIT_HINT_WORDS


def test_time_word_alone_does_not_enter_the_edit_fast_path(
    post_chat, definition
):
    """待确认态下，一句只带时间词的话不会变成修改计划。"""
    payload = _post(
        post_chat,
        definition,
        "昨日",
        state="WAITING_USER_CONFIRM",
    )

    assert payload["domainObjects"]["pendingEditPlan"] is None


# ==========================================================================
# AC6 「把昨日能耗趋势改成按小时展示」产生粒度候选，不把时间窗改成昨天
# ==========================================================================


def test_grain_command_produces_grain_candidate_not_time_window(
    post_chat, definition
):
    payload = _post(post_chat, definition, GRAIN_COMMAND_MESSAGE)

    plan = payload["domainObjects"]["pendingEditPlan"]
    assert plan is not None
    assert plan["operations"][0]["intent"] == "CHANGE_COMPILATION_GRAIN"
    assert "timeWindow" not in plan["operations"][0]["operation"]
    assert plan["operations"][0]["operation"]["grain"]["unit"] == "HOUR"
    # 接受前定义不变。
    assert payload["domainObjects"]["reportDefinitionDraft"]["metadata"]["revision"] == 0
    assert definition.metadata["revision"] == 0


def test_grain_command_reports_capability_limit_when_unsupported(definition):
    """数据源不支持该粒度时给明确的能力限制，不静默改。"""
    plan = template_edit_engine.parse(
        message="把趋势图改成按分钟粒度",
        definition=definition,
        selected_region_id=None,
        scope=SCOPE,
    )

    assert plan.intent == EditIntent.CHANGE_COMPILATION_GRAIN
    assert plan.needsClarification is True
    assert "不支持" in plan.clarificationQuestion
    assert "粒度" in plan.clarificationQuestion


# ==========================================================================
# AC7 「冰箱耗电明细可以用其他展示方法吗？」得到针对当前板块的有依据回答
# ==========================================================================


def test_consultation_answers_for_the_current_card(post_chat, definition):
    payload = _post(post_chat, definition, CONSULT_MESSAGE)

    assert payload["domainObjects"]["pendingEditPlan"] is None
    assert payload["domainObjects"]["changeSet"] is None
    assert definition.metadata["revision"] == 0
    # 回答要指明说的是哪一块，并给出这块当前支持的方式。
    detail_region = next(
        region for region in definition.regions if region.get("role") == "DETAIL"
    )
    assert detail_region["name"] in payload["reply"]
    assert "草稿没有变化" in payload["reply"]


def test_consultation_does_not_consume_clarification_quota(
    post_chat, definition
):
    """咨询不占澄清额度（规格第 14、25 条）。"""
    for _ in range(4):
        payload = _post(post_chat, definition, CONSULT_MESSAGE)
        assert payload["domainObjects"]["pendingEditPlan"] is None

    assert session_service.load_clarification("conv_test") is None


# ==========================================================================
# AC8 没有疑问标记的反馈也能被识别为问题反馈
# ==========================================================================


def test_feedback_without_question_marker_is_issue_feedback():
    """「好丑啊，一个点……」没有问号，但它不是提问，也不是指令。"""
    assert routing_rules.is_readonly_question(FEEDBACK_MESSAGE) is False
    assert routing_rules.has_operation_command(FEEDBACK_MESSAGE) is False
    assert routing_rules.detect_issue_feedback(FEEDBACK_MESSAGE) is True
    assert (
        classify_message_intent(FEEDBACK_MESSAGE, subject_required=False)
        is UserIntent.ISSUE_FEEDBACK
    )


def test_feedback_without_question_marker_does_not_reach_the_fast_path(
    post_chat, definition, monkeypatch
):
    def _boom(**_kwargs):
        raise AssertionError("没有疑问标记的反馈掉进了编辑快路")

    monkeypatch.setattr(template_edit_engine, "parse", _boom)

    payload = _post(post_chat, definition, FEEDBACK_MESSAGE)

    assert payload["debug"]["userIntent"] == UserIntent.ISSUE_FEEDBACK.value
    assert payload["domainObjects"]["pendingEditPlan"] is None


# ==========================================================================
# AC9 规则自设的高置信度不替代意图判断；缺证据就说缺证据
# ==========================================================================


def test_rule_confidence_does_not_override_the_intent(definition):
    """时间窗规则自认为 0.88，也不能盖过意图判断。"""
    plan = template_edit_engine.parse(
        message=FEEDBACK_MESSAGE,
        definition=definition,
        selected_region_id=None,
        scope=SCOPE,
    )

    # 就算规则判出了操作，置信度也不能高到越过意图这一关：
    # 这里根本不该解析出任何「新值」。
    assert plan.operation.timeWindow is None
    assert not plan.operation.timeWindowLabel


def test_issue_analysis_without_evidence_says_so(definition):
    """定位不到是哪张卡片时说证据不足，并给针对性的澄清。"""
    analysis = analyze_region(definition=definition, region=None)

    assert analysis.evidence_sufficient is False
    assert "草稿没有变化" in analysis.summary


def test_issue_analysis_asks_which_card_when_nothing_matches(
    post_chat, definition
):
    payload = _post(post_chat, definition, "好丑啊，一个点……")

    assert payload["domainObjects"]["pendingEditPlan"] is None
    assert "请告诉我是哪一张卡片" in payload["reply"]
    assert definition.metadata["revision"] == 0


# ==========================================================================
# 澄清额度：一轮、按事项计（规格第 13~15、19 条）
# ==========================================================================


def test_clarification_quota_is_one_round_per_item(post_chat, definition):
    """同一项修改只追问一轮；换一项才重新计。"""
    from app.schemas.session import MAX_CLARIFICATION_ROUNDS

    assert MAX_CLARIFICATION_ROUNDS == 1

    replies = []
    for _ in range(3):
        replies.append(_post(post_chat, definition, "改一下")["reply"])

    assert "追问过 1 轮" in replies[-1]
    assert session_service.load_clarification("conv_test").rounds == 1


def test_out_of_catalog_add_reaches_the_edit_fast_path(post_chat, definition):
    """目录外新增**不再被意图门挡在编辑快路之外**（工单 #65）。

    工单 #65 把目录外候选层实现**在编辑引擎里**；意图门一旦跳过快路，
    候选层就永远走不到，「不再以「未登记」拒绝」这条 AC 对最典型的说法
    就不成立。

    这里断言的是**用户看得见的行为**：目录外新增说出的是一份**待接受
    的修改计划**（不是拒绝、不是空白），计划里的指标是**模拟候选**。
    """
    payload = _post(post_chat, definition, "新增一个设备温度指标")

    # 这句话确实被判成目录外新增（规则层）：否则下面那份候选无从谈起。
    assert classify_message_intent(
        "新增一个设备温度指标",
        known_metrics=["总耗电", "设备耗电", "能耗成本", "PUE"],
    ) is UserIntent.OUT_OF_CATALOG_ADD

    plan = payload["domainObjects"]["pendingEditPlan"]
    assert plan is not None, "目录外新增必须产出待接受的修改计划，而不是被拒绝"
    assert payload["uiActions"] == ["pending_edit_plan"]
    # 接受之前草稿一个字节都没动。
    assert payload["domainObjects"]["changeSet"] is None
    assert definition.metadata["revision"] == 0

    operation = plan["operations"][0]["operation"]
    # operation 落进计划时是 exclude_none 掉的：metricRef 没有就是没有。
    assert operation.get("metricRef") is None, "目录外指标不伪造 metricRef"
    assert operation["metricIntent"] == "设备温度"
    assert operation["metricCandidate"]["status"] == "out_of_catalog"
    assert "未登记" not in payload["reply"]


def test_rate_metric_add_is_classified_as_out_of_catalog_add():
    """「新增一个设备耗电率指标」在意图层就是**目录外新增**（工单 #65c）。

    「设备耗电率」不是已登记指标名（它只是**包含**别名「设备耗电」）。
    意图层已经认得出这一点 —— 差的是引擎那一步：别名命中不等于识别成功。
    """
    assert classify_message_intent(
        "新增一个设备耗电率指标",
        known_metrics=["总耗电", "设备耗电", "能耗成本", "PUE"],
    ) is UserIntent.OUT_OF_CATALOG_ADD


# ==========================================================================
# P1 澄清额度真正按「事项」计（契约表 §7.2 / §7.4）
# ==========================================================================
#
# 修复前：额度用完一轮后，换**任何一件新的修改**都被回成
# 「这项修改我已经追问过 1 轮」—— 上一轮的额度键被钉死，新事项再也追不到。
# 修复后：额度键由**当前这一轮**的「意图:目标卡片」推导，
# 键变了 → 新事项 → 自然重开；键没变 → 同一件事 → 第二轮仍被挡住。


def test_same_item_second_ask_is_still_blocked(post_chat, definition):
    """同一件事连问两次：第二次仍被额度上限挡住（只问一轮）。"""
    first = _post(post_chat, definition, "删除")

    assert "你想删掉哪张卡片" in first["reply"], "第一轮应该真的抛出追问"
    assert first["uiActions"] != ["template_edit_clarification_limit"]

    second = _post(post_chat, definition, "删除")

    assert "追问过 1 轮" in second["reply"]
    assert second["questions"] == []
    assert second["uiActions"] == ["template_edit_clarification_limit"]
    # 额度没有被重开：同一项仍然只问过一轮。
    assert session_service.load_clarification("conv_test").rounds == 1


def test_new_item_reopens_the_clarification_quota(post_chat, definition):
    """换一件事：额度自然重开，仍然能追问（契约表 §7.4）。

    这是修复前被挡住的那条路：第一轮追问过「删除哪个 section」之后，
    用户改口提另一件事（改粒度），以前会被回成「追问过 1 轮」。
    """
    first = _post(post_chat, definition, "删除")

    assert "你想删掉哪张卡片" in first["reply"]
    assert session_service.load_clarification("conv_test").key.startswith(
        "DELETE_SECTION"
    )

    # 换一件完全不同的事：改另一张卡片的编译粒度。
    second = _post(post_chat, definition, "把小时趋势改成按秒粒度")

    assert second["uiActions"] != ["template_edit_clarification_limit"], (
        "新事项被上一事项的澄清额度挡住了"
    )
    assert "追问过 1 轮" not in second["reply"]
    assert second["questions"], "新事项应该重新被追问"
    # 额度记到了**新事项**的键上。
    state = session_service.load_clarification("conv_test")
    assert state.key.startswith("CHANGE_COMPILATION_GRAIN")
    assert state.rounds == 1


def test_new_item_after_the_quota_is_exhausted_can_still_be_asked(
    post_chat, definition
):
    """额度用尽之后换一件事，仍然能追问（额度用尽不清掉额度本身）。"""
    _post(post_chat, definition, "删除")
    exhausted = _post(post_chat, definition, "删除")

    assert "追问过 1 轮" in exhausted["reply"]

    # 换一件事：不受上一件事已经用尽的额度影响。
    second = _post(post_chat, definition, "把小时趋势改成按秒粒度")

    assert second["uiActions"] != ["template_edit_clarification_limit"]
    assert "追问过 1 轮" not in second["reply"]
    assert second["questions"], "新事项应该重新被追问"


# ==========================================================================
# P3 问题反馈识别：不能只靠一张固定词表
# ==========================================================================
#
# 验收方实测：旧词表之外的常见抱怨句（「颜色太扎眼了」「一眼看过去很费劲」）
# 一律被判成 UNKNOWN → 掉进编辑快路 → 反问「请告诉我要怎么改当前草稿」。
# 修法是给抱怨句加**结构性特征**（程度副词 + 评价性形容词、否定式体验说法），
# 同时守住「正常新增 / 改名 / 删除 / 换图 / 改时间范围 / 改粒度」这条边界。

#: 验收方实测掉进编辑快路的 9 句抱怨：都没有疑问标记，也都不在旧词表里。
COMPLAINT_SAMPLES = (
    "颜色太扎眼了",
    "趋势太单调了",
    "这个布局太挤了",
    "数字太大了没法看",
    "一眼看过去很费劲",
    "配色很丑",
    "看不下去",
    "有点糊",
    "体验很差",
)

#: 验收方确认过的正常修改请求：不能被误判成问题反馈。
LEGITIMATE_CHANGE_REQUESTS = (
    "新增一个总耗电卡片",
    "新增一个设备耗电排名卡片",
    "新增一个能耗成本卡片",
    "把小时趋势的标题改成能耗趋势",
    "删除这个卡片",
    "把趋势图换成柱状图",
    "把时间范围改成最近 7 天",
    "把粒度改成按秒",
)


@pytest.mark.parametrize("message", COMPLAINT_SAMPLES)
def test_common_complaints_are_issue_feedback(message):
    """常见抱怨句（没有疑问标记、词表之外）也判成问题反馈。"""
    assert routing_rules.detect_issue_feedback(
        message, subject_required=False
    ) is True
    assert (
        classify_message_intent(message, subject_required=False)
        is UserIntent.ISSUE_FEEDBACK
    )


@pytest.mark.parametrize("message", COMPLAINT_SAMPLES)
def test_complaint_does_not_produce_a_pending_edit_plan(
    post_chat, definition, message
):
    """抱怨句端到端：只给分析，不产生待接受计划，不改定义。"""
    payload = _post(post_chat, definition, message)

    assert payload["turnKind"] == "query_answer"
    assert payload["uiActions"] == ["query_answer"]
    assert payload["domainObjects"]["pendingEditPlan"] is None
    assert payload["domainObjects"]["changeSet"] is None
    assert payload["debug"]["userIntent"] == UserIntent.ISSUE_FEEDBACK.value
    assert definition.metadata["revision"] == 0
    assert session_service.load_pending_plan("conv_test") is None


@pytest.mark.parametrize("message", LEGITIMATE_CHANGE_REQUESTS)
def test_legitimate_change_requests_are_not_issue_feedback(message):
    """正常的新增 / 改名 / 删除 / 换图 / 改时间范围 / 改粒度 仍然判明确修改。"""
    intent = classify_message_intent(message, subject_required=False)

    assert intent is not UserIntent.ISSUE_FEEDBACK, "正常修改请求被当成了抱怨"
    assert intent is UserIntent.MODIFY


@pytest.mark.parametrize(
    ("message", "expected_intent"),
    [
        ("新增一个总耗电卡片", "ADD_SECTION"),
        ("新增一个设备耗电排名卡片", "ADD_SECTION"),
        ("新增一个能耗成本卡片", "ADD_SECTION"),
        ("把小时趋势的标题改成能耗趋势", "RENAME_SECTION"),
    ],
)
def test_legitimate_change_requests_still_produce_a_plan(
    post_chat, definition, message, expected_intent
):
    """端到端：正常修改仍然产出待接受计划（没有被当抱怨吞掉）。"""
    payload = _post(post_chat, definition, message)

    assert payload["turnKind"] != "query_answer"
    plan = payload["domainObjects"]["pendingEditPlan"]
    assert plan is not None
    assert plan["operations"][0]["intent"] == expected_intent
    assert definition.metadata["revision"] == 0


@pytest.mark.parametrize(
    "message",
    [
        "删除这个卡片",
        "把趋势图换成柱状图",
        "把时间范围改成最近 7 天",
        "把粒度改成按秒",
    ],
)
def test_legitimate_change_requests_are_not_answered_as_query(
    post_chat, definition, message
):
    """这些请求走的是**修改 / 追问**链路，不是只读答复链路。"""
    payload = _post(post_chat, definition, message)

    assert payload["turnKind"] != "query_answer"
    assert payload["uiActions"] != ["query_answer"]
    assert payload["domainObjects"]["changeSet"] is None
    assert definition.metadata["revision"] == 0


@pytest.mark.parametrize(
    "message",
    [
        "把趋势卡片标题改成能耗卡片",
        "把设备耗电排名卡片换成柱状图",
        "新增一个总耗电趋势卡片",
    ],
)
def test_object_names_that_also_look_like_complaints_are_not_issue_feedback(
    message,
):
    """「趋势」「排名」「卡片」这些词既可能是抱怨，也可能是**对象名**。

    抱怨句被识别成问题反馈的同时，正常的新增 / 改名 / 换图不能被误伤。
    """
    intent = classify_message_intent(message, subject_required=False)

    assert intent is not UserIntent.ISSUE_FEEDBACK
    assert intent is UserIntent.MODIFY


# ==========================================================================
# P2 粒度新值的来源：与时间窗同口径（契约表 §7 / AC4）
# ==========================================================================
#
# 独立验收实测到的两条失败：
#   「把小时趋势的标题改一下」 → intent=CHANGE_COMPILATION_GRAIN grain=HOUR
#   「小时趋势太单调了」        → intent=CHANGE_COMPILATION_GRAIN grain=HOUR
#
# 根因：粒度侧还留着**整句回退**（没有改动动词时整句都当诉求），
# 并且守卫把卡片名里的「小时」也算成粒度话题。
#
# 下面这段用例补的正是**没有改动动词**的句式 —— #58 自带的粒度用例
# 只测了有改动动词的句式，整句回退那一支一直没被覆盖。

#: 没有改动动词的句子：粒度新值不能来自整句。
NO_CHANGE_VERB_MESSAGES = (
    "把小时趋势的标题改一下",
    "小时趋势太单调了",
    "小时趋势这张卡片不太行",
)


@pytest.mark.parametrize("message", NO_CHANGE_VERB_MESSAGES)
def test_card_name_grain_word_without_change_verb_is_not_a_grain_change(
    definition, message
):
    """卡片名里的「小时」是卡片名，不是要写进去的粒度。

    没有改动动词 ⇒ 没有新值：这句话既不该改粒度，也不该带出粒度值。
    """
    plan = template_edit_engine.parse(
        message=message,
        definition=definition,
        selected_region_id=None,
        scope=SCOPE,
    )

    assert plan.intent != EditIntent.CHANGE_COMPILATION_GRAIN
    assert plan.operation.grain is None


@pytest.mark.parametrize(
    ("message", "expected_grain"),
    [
        ("把粒度改成按小时", "HOUR"),
        ("改成按天粒度", "DAY"),
        ("把小时趋势的粒度改成按小时粒度", "HOUR"),
    ],
)
def test_explicit_grain_naming_still_produces_a_grain_change(
    definition, message, expected_grain
):
    """明确点名的写法仍然是粒度新值 —— 收紧没有把正常的路堵死。"""
    plan = template_edit_engine.parse(
        message=message,
        definition=definition,
        selected_region_id="region_hourly_trend",
        scope=SCOPE,
    )

    assert plan.intent == EditIntent.CHANGE_COMPILATION_GRAIN
    assert plan.operation.grain["unit"] == expected_grain


def test_unsupported_grain_gives_capability_limit(definition):
    """数据源不支持该粒度时给能力限制，不静默改。"""
    plan = template_edit_engine.parse(
        message="把粒度改成按分钟粒度",
        definition=definition,
        selected_region_id="region_hourly_trend",
        scope=SCOPE,
    )

    assert plan.intent == EditIntent.CHANGE_COMPILATION_GRAIN
    assert plan.needsClarification is True
    assert "不支持" in plan.clarificationQuestion


def test_unknown_grain_name_gives_capability_limit(definition):
    """目录里没有的粒度（「按秒」）走粒度话题 + 能力限制，不硬造一个粒度。"""
    plan = template_edit_engine.parse(
        message="把粒度改成按秒",
        definition=definition,
        selected_region_id="region_hourly_trend",
        scope=SCOPE,
    )

    assert plan.intent == EditIntent.CHANGE_COMPILATION_GRAIN
    assert plan.operation.grain is None
    assert plan.needsClarification is True
    assert "粒度" in plan.clarificationQuestion


def test_asking_which_grains_are_supported_has_no_new_value(definition):
    """「能按哪些粒度看？」是粒度话题，但**没有新值** —— 不硬造一个粒度。"""
    plan = template_edit_engine.parse(
        message="能按哪些粒度看？",
        definition=definition,
        selected_region_id="region_hourly_trend",
        scope=SCOPE,
    )

    assert plan.intent == EditIntent.CHANGE_COMPILATION_GRAIN
    assert plan.operation.grain is None
    assert plan.needsClarification is True
    assert "粒度" in plan.clarificationQuestion


def test_clarification_answer_whole_sentence_is_the_new_value(definition):
    """澄清答案：整句就是新值（「按小时」→ HOUR）。

    澄清答案由调用方把整句塞进 ``change_value``，走的是「有改动片段」
    这一档（与时间窗同口径），不受整句回退收紧的影响。
    """
    plan = template_edit_engine.parse(
        message="按小时",
        definition=definition,
        selected_region_id="region_hourly_trend",
        scope=SCOPE,
        change_value="按小时",
    )

    assert plan.intent == EditIntent.CHANGE_COMPILATION_GRAIN
    assert plan.operation.grain["unit"] == "HOUR"


def test_bare_word_clarification_answer_is_not_a_new_value(definition):
    """裸词「小时」按 ``explicit_only`` 的既有口径：**不是新值**。

    裸词与卡片名里的「小时」分不开，只认明确点名的写法；
    宁可再问一次，也不硬写一个粒度。
    """
    plan = template_edit_engine.parse(
        message="小时",
        definition=definition,
        selected_region_id="region_hourly_trend",
        scope=SCOPE,
        change_value="小时",
    )

    assert plan.operation.grain is None
    assert plan.intent != EditIntent.CHANGE_COMPILATION_GRAIN


def test_time_range_command_is_not_hijacked_by_grain_logic(definition):
    """「把时间范围改成最近 7 天」仍是时间窗修改，不被粒度逻辑误伤。"""
    plan = template_edit_engine.parse(
        message="把时间范围改成最近 7 天",
        definition=definition,
        selected_region_id="region_hourly_trend",
        scope=SCOPE,
    )

    assert plan.intent == EditIntent.CHANGE_TIME_RANGE
    assert plan.operation.timeWindow is not None
    assert plan.operation.grain is None


def test_delete_command_is_not_hijacked_by_grain_logic(definition):
    """「删除某个卡片」仍是删除，不受影响。"""
    plan = template_edit_engine.parse(
        message="删除某个卡片",
        definition=definition,
        selected_region_id="region_hourly_trend",
        scope=SCOPE,
    )

    assert plan.intent == EditIntent.DELETE_SECTION
    assert plan.operation.grain is None


def test_complaint_about_hourly_trend_produces_no_pending_edit_plan(
    post_chat, definition
):
    """端到端：抱怨「小时趋势太单调了」不产生待接受计划，也不改草稿。"""
    payload = _post(post_chat, definition, "小时趋势太单调了")

    assert payload["domainObjects"]["pendingEditPlan"] is None
    assert payload["domainObjects"]["changeSet"] is None
    assert "pending_edit_plan" not in payload["uiActions"]
    assert definition.metadata["revision"] == 0


def test_clarification_answer_lands_the_grain_end_to_end(post_chat, definition):
    """端到端：追问粒度之后从菜单里选粒度，仍然落地 HOUR。

    追问是**单选菜单**，答案就是菜单项的值（「按小时粒度」——
    明确点名的写法），走的是「有改动片段」这一档，不受整句回退收紧影响。
    """
    first = _post(post_chat, definition, "把小时趋势的粒度调一下")

    assert first["domainObjects"]["pendingEditPlan"] is None
    question = first["questions"][0]
    assert question["id"] == "compilation_grain"
    values = [option["value"] for option in question["options"]]
    assert "按小时粒度" in values

    second = _post(post_chat, definition, "按小时粒度")

    plan = second["domainObjects"]["pendingEditPlan"]
    assert plan is not None
    assert plan["operations"][0]["intent"] == "CHANGE_COMPILATION_GRAIN"
    assert plan["operations"][0]["operation"]["grain"]["unit"] == "HOUR"
    assert definition.metadata["revision"] == 0
