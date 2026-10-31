"""工单 66C / GitHub #66：目录外指标的「澄清口径」收得了尾。

集成验收 C2 查出的真缺陷（🔴 阻塞）：**真实模型路径**
（``AGENT_IMPL=agentscope``）下，目录外新增指标的「澄清口径」菜单
**收不了尾** —— 模型自造的 slug 选项（``pc_energy_abs``）既过不了菜单
闸门（闸门只认 ``value``），也命中不了规则层的
``_match_clarification_choice``（按候选 **显示名** 子串匹配），
用户无论选 ``value`` 还是 ``label`` 都解不开。

修法：**口径选项必须来自规则层**。规则层判出该指标确实 ambiguous 时，
菜单的 ``option.value`` 取规则候选的 ``label``（耗电量 / 占比 / 功率）；
别的澄清路径（粒度 / 范围 / 参考对象等）照旧用模型给的选项。

测试分三层：

1. **规则层**：``ambiguous_caliber_candidates`` 给什么、给不给；
2. **装配层**：模型给的 slug 选项被换成规则候选（真实 ``POST /api/agent/chat``
   编排，替身业务 Agent 只复刻「模型自造 slug 选项」这一个现场）；
3. **脚本路径**：``FakeReportAgent`` 那条路径**逐字未变**。
"""
from __future__ import annotations

import logging
from typing import Any

import pytest

from app.schemas.agent import (
    AgentQuestion,
    AgentResponse,
    AgentState,
    QuestionOption,
)
from app.services.agent_service import agent_service
from app.services.out_of_catalog_candidate import ambiguous_caliber_candidates
from app.services.report_draft_service import build_definition_draft


#: 模型自造的选项（工单 #66 的缺陷现场）：slug 化的 value + 自造的 label。
_SLUG_OPTIONS = [
    QuestionOption(value="pc_energy_abs", label="电脑绝对能耗量"),
    QuestionOption(value="pc_energy_ratio", label="电脑能耗占比"),
]

#: 规则层候选（``simulated_field_semantics.clarification_candidates``）。
_RULE_LABELS = ["耗电量", "占比", "功率"]


class _SlugOptionAgent:
    """替身业务 Agent：只复刻「模型自造 slug 选项」这一个现场。

    **不是** ``FakeReportAgent`` 的实例 —— 真实模型路径正是这样走的：
    规则快路对 ambiguous 计划返回 ``None``，澄清菜单由模型给出。
    """

    def __init__(self, question_id: str = "template_edit_clarification") -> None:
        self.question_id = question_id
        self.calls: list[str] = []

    def handle(self, request) -> AgentResponse:
        self.calls.append(request.message)
        question = (
            "请确认你想新增的「电脑耗电率」具体指什么？"
            if self.question_id == "template_edit_clarification"
            else "请选择当前数据源支持的粒度："
        )
        return AgentResponse(
            conversationId=request.conversationId,
            state=AgentState.USER_EDITING_TEMPLATE,
            reply=question,
            questions=[
                AgentQuestion(
                    id=self.question_id,
                    prompt=question,
                    expects="SINGLE_CHOICE",
                    options=[option.model_copy() for option in _SLUG_OPTIONS],
                    selectionOnly=True,
                )
            ],
            uiActions=["ask_template_edit_detail"],
            debug={
                "editOutcome": "clarification",
                "editingIntent": "ADD_SECTION",
                "editingTargetRegionId": None,
            },
        )


@pytest.fixture()
def definition():
    return build_definition_draft("A 机房能耗日报", "room_a")


def _post(post_chat, definition, message: str, state: str = "USER_EDITING_TEMPLATE"):
    return post_chat(
        message,
        state=state,
        snapshots={"reportDefinition": definition.model_dump(mode="json")},
    ).json()


def _questions(body: dict[str, Any]) -> list[dict[str, Any]]:
    return list(body.get("questions") or [])


def _options(body: dict[str, Any]) -> list[dict[str, Any]]:
    for question in _questions(body):
        if question.get("options"):
            return list(question["options"])
    return []


# ==========================================================================
# 1. 规则层：口径选项来自规则候选，不是模型现编
# ==========================================================================


def test_rule_layer_gives_the_caliber_options_for_an_ambiguous_intent():
    """含义不清 → 给规则候选；含义清楚 → 什么都不给（调用方一律不动）。"""
    options = ambiguous_caliber_candidates("电脑耗电率")

    assert [item["value"] for item in options] == _RULE_LABELS
    # value 取候选的 **label** —— 与 _match_clarification_choice 命中的是
    # 同一份候选，用户选完就能解出候选。
    assert "耗电量" in options[0]["label"]
    assert "kWh" in options[0]["label"]
    # 含义清楚（目录外但口径不歧义）→ 不给口径选项。
    assert ambiguous_caliber_candidates("冰箱明细") == []
    assert ambiguous_caliber_candidates("电脑耗电功率") == []
    assert ambiguous_caliber_candidates("") == []


# ==========================================================================
# 2. 装配层：模型自造的 slug 选项被换成规则候选
# ==========================================================================


def test_model_slug_options_are_replaced_by_the_rule_caliber_options(
    monkeypatch, post_chat, definition
):
    """真实模型路径的现场：模型给 slug → 菜单必须是规则候选。"""
    monkeypatch.setattr(agent_service, "_agent", _SlugOptionAgent())

    body = _post(post_chat, definition, "新增一个电脑耗电率指标")

    question = next(
        q for q in _questions(body) if q["id"] == "template_edit_clarification"
    )
    assert question["expects"] == "SINGLE_CHOICE"
    assert question["selectionOnly"] is True
    values = [option["value"] for option in question["options"]]
    assert values == _RULE_LABELS
    # 模型自造的 slug 一个都不许留在菜单里。
    assert "pc_energy_abs" not in values
    assert "pc_energy_ratio" not in values
    assert all("pc_energy" not in str(option) for option in question["options"])
    assert (body.get("debug") or {}).get("caliberMenuSource") == "rule_candidates"


def test_choosing_a_rule_caliber_option_closes_the_clarification(
    monkeypatch, post_chat, definition
):
    """选一个规则口径 → 直接出待接受的候选，**不再追问**。"""
    monkeypatch.setattr(agent_service, "_agent", _SlugOptionAgent())

    first = _post(post_chat, definition, "新增一个电脑耗电率指标")
    value = _options(first)[0]["value"]

    body = _post(post_chat, definition, value)

    pending = (body.get("domainObjects") or {}).get("pendingEditPlan")
    assert pending is not None
    candidate = pending["operations"][0]["operation"]["metricCandidate"]
    # 用户拍板的那个口径**真的解开了**：候选不再是「含义不清」。
    assert candidate["status"] == "out_of_catalog"
    assert candidate["unit"] == "kWh"
    # 不再追问口径。
    assert [q["id"] for q in _questions(body)] == ["pending_edit_plan_confirm"]


def test_other_clarification_paths_keep_the_model_options(
    monkeypatch, post_chat, definition
):
    """别的澄清路径**照旧用模型给的选项**：一条都不许动。

    两条守卫各验一次：

    1. 规则层没判定 ambiguous（这里问法本身就不是口径问题）→ 不替换；
    2. 规则层判定了 ambiguous，但**问题不是口径澄清**（模型问的是粒度）
       → 也不替换。
    """
    agent = _SlugOptionAgent()
    monkeypatch.setattr(agent_service, "_agent", agent)

    body = _post(post_chat, definition, "帮我调一下")

    values = [option["value"] for option in _options(body)]
    assert values == ["pc_energy_abs", "pc_energy_ratio"]
    assert (body.get("debug") or {}).get("caliberMenuSource") is None

    # 口径确实 ambiguous，但模型这轮问的是粒度 → 选项仍然是模型的。
    agent = _SlugOptionAgent(question_id="compilation_grain")
    monkeypatch.setattr(agent_service, "_agent", agent)

    body = _post(post_chat, definition, "新增一个电脑耗电率指标")

    values = [option["value"] for option in _options(body)]
    assert values == ["pc_energy_abs", "pc_energy_ratio"]
    assert (body.get("debug") or {}).get("caliberMenuSource") is None


# ==========================================================================
# 3. 脚本路径（FakeReportAgent）：**逐字未变**
# ==========================================================================


def test_script_path_clarification_is_unchanged(post_chat, definition):
    """脚本路径那条不受本次修复影响：菜单、口径问句、自由文本都照旧。"""
    body = _post(post_chat, definition, "新增一个电脑耗电率指标")

    assert body["reply"] == (
        "「电脑耗电率」这个说法有多种口径，你要哪一种：耗电量（kWh）、占比（%）、功率（kW）？"
    )
    question = _questions(body)[0]
    assert question["id"] == "template_edit_action"
    assert question["expects"] == "SINGLE_CHOICE"
    assert question["selectionOnly"] is False
    assert question["lockScope"] == "none"
    assert [option["value"] for option in question["options"]] == [
        "删除这个 section", "改标题", "改指标", "改数据源",
        "改展示方式", "改时间范围", "改 compilation 粒度",
    ]
    assert (body.get("domainObjects") or {}).get("pendingEditPlan") is None

    # 脚本路径的澄清是自由文本（不锁菜单）：「耗电量」直接走通。
    body = _post(post_chat, definition, "耗电量")

    pending = (body.get("domainObjects") or {}).get("pendingEditPlan")
    assert pending is not None
    candidate = pending["operations"][0]["operation"]["metricCandidate"]
    assert candidate["status"] == "out_of_catalog"


def test_caliber_menu_log_keeps_no_user_words(monkeypatch, post_chat, definition, caplog):
    """菜单替换那条日志不许带**用户原话**（#61 AC#1）。

    指标意图是从用户原话里摘出来的（「电脑耗电率」）→ 只留长度；
    选项值是**规则层封闭词表**（耗电量 / 占比 / 功率），不是用户原话 → 留着有排查价值。
    """
    caplog.set_level(logging.INFO, logger="app")
    monkeypatch.setattr(agent_service, "_agent", _SlugOptionAgent())

    _post(post_chat, definition, "新增一个电脑耗电率指标")

    text = caplog.text
    assert "step=caliber_menu_replaced" in text
    assert "intentLength=" in text
    assert "电脑耗电率" not in text
    assert "耗电量" in text
