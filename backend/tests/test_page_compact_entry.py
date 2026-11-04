"""整页紧凑 / 按阅读顺序重排的入口回归（T1 专项）。

背景：用户在工作台提交「页面紧凑一点，标题保留，其他内容按阅读顺序排列」，
系统回了「请告诉我是哪一张卡片」—— 整页诉求被当成了对某张卡的抱怨。

    - ``routing_rules._RE_ISSUE_POINT_COUNT`` 把「紧凑**一点**」里的「一点」
      当成了「只有一个数据点」；
    - 「排列 / 紧凑」这类词不在改动动词表里，于是既不是命令、也不是提问，
      只能落进「问题反馈」那条只读解释分支。

这里锁定两件事：

    1. 规则层：整页紧凑 / 重排是**明确的整页命令**，能力咨询与单卡范围不算；
    2. 候选层：它产出**具体**的展示操作（整页单列连续排布 + 顶层卡片改流式定位），
       并且整批能原子执行。
"""
from __future__ import annotations

import copy
import json

import pytest

from app.schemas.edit_plan import EditIntent
from app.schemas.report_definition import ReportDefinitionDraft
from app.services import routing_rules
from app.services.context_engine import classify_message_intent, UserIntent
from app.services.template_edit_engine import template_edit_engine


#: 用户原句。
COMPACT = "页面紧凑一点，标题保留，其他内容按阅读顺序排列"


def _definition(page: dict | None = None) -> ReportDefinitionDraft:
    """一份三卡草稿：标题区 + 总耗电卡（4/12 列）+ 排名表 + 趋势图。"""
    return ReportDefinitionDraft.model_validate({
        "metadata": {"name": "A 机房 昨日能耗日报", "revision": 3},
        "page": page if page is not None else {},
        "regions": [
            {
                "id": "region_title", "name": "A 机房 昨日能耗日报",
                "role": "REPORT_TITLE",
                "layout": {"placement": {"type": "GRID", "column": 1, "columnSpan": 12}},
                "component": {"type": "TEXT"},
            },
            {
                "id": "region_total", "name": "总耗电", "role": "SUMMARY",
                "layout": {"placement": {"type": "GRID", "column": 1, "columnSpan": 4}},
                "context": {"timeWindow": {"kind": "YESTERDAY"}},
                "component": {"type": "METRIC_CARD", "metric": {
                    "id": "m_total", "label": "总耗电", "fieldRef": "energyKwh",
                    "format": {"kind": "NUMBER", "precision": 1, "unitLabel": "kWh"},
                }},
            },
            {
                "id": "region_ranking", "name": "设备耗电排名", "role": "DETAIL",
                "layout": {"placement": {"type": "GRID", "column": 1, "columnSpan": 12}},
                "context": {"timeWindow": {"kind": "YESTERDAY"}, "grain": "HOUR"},
                "component": {"type": "TABLE"},
            },
            {
                "id": "region_trend", "name": "能耗趋势", "role": "DETAIL",
                "layout": {"placement": {"type": "GRID", "column": 1, "columnSpan": 12}},
                "context": {"timeWindow": {"kind": "YESTERDAY"}, "grain": "HOUR"},
                "component": {"type": "CHART"},
            },
        ],
    })


def _parse(message=COMPACT, definition=None, selected="region_trend"):
    return template_edit_engine.parse(
        message=message,
        definition=definition if definition is not None else _definition(),
        selected_region_id=selected,
    )


# --------------------------------------------------------------------------
# A. 规则层
# --------------------------------------------------------------------------

@pytest.mark.parametrize("message", [
    COMPACT,
    "整页紧凑一些，保留标题",
    "把整页排版调整得紧凑些",
    "页面紧凑一点，可以吗？",
])
def test_page_compact_command_is_recognized(message):
    assert routing_rules.detect_page_compact_request(message)


@pytest.mark.parametrize("message", [
    "支持紧凑排版吗？",          # 能力咨询
    "紧凑排版可以吗",            # 能力咨询
    "紧凑排版是什么？",          # 能力咨询
    "趋势只有一个点",            # 真实数据点数反馈
    "这两张卡片紧凑一点",        # 单卡范围
    "第三张紧凑一点",            # 单卡范围（位置指代）
    "前 3 张紧凑一点",           # 单卡范围（位置指代）
])
def test_consultation_data_feedback_and_card_scope_are_not_page_commands(message):
    assert not routing_rules.detect_page_compact_request(message)


def test_page_compact_is_an_operation_command_not_issue_feedback():
    assert routing_rules.has_operation_command(COMPACT)
    assert not routing_rules.detect_issue_feedback(COMPACT, subject_required=False)
    assert not routing_rules.is_readonly_question(COMPACT)
    assert classify_message_intent(COMPACT, subject_required=False) == UserIntent.MODIFY


def test_capability_question_stays_readonly():
    """能力咨询照旧可问：不能被新的命令判定吞掉。"""
    assert routing_rules.is_readonly_question("支持紧凑排版吗？")
    assert not routing_rules.has_operation_command("支持紧凑排版吗？")
    assert classify_message_intent("支持紧凑排版吗？") == UserIntent.ASK_QUESTION


def test_point_count_feedback_is_still_issue_feedback():
    """真实的「只有一个数据点」反馈照旧识别。"""
    assert routing_rules.detect_issue_feedback("趋势只有一个点", subject_required=False)
    assert classify_message_intent("趋势只有一个点", subject_required=False) == UserIntent.ISSUE_FEEDBACK


# --------------------------------------------------------------------------
# B. 候选生成
# --------------------------------------------------------------------------

def _expected_operations(gap: int) -> list[dict]:
    return [
        {
            "op": "SET_PAGE",
            "params": {"arrangement": {
                "mode": "FLOW", "columns": 1, "direction": "VERTICAL",
                "gap": {"value": gap, "unit": "PX"},
            }},
        },
        {"op": "SET_PLACEMENT", "params": {
            "regionId": "region_title",
            "placement": {"type": "FLOW", "align": "STRETCH"},
        }},
        {"op": "SET_PLACEMENT", "params": {
            "regionId": "region_total",
            "placement": {"type": "FLOW", "align": "STRETCH"},
        }},
        {"op": "SET_PLACEMENT", "params": {
            "regionId": "region_ranking",
            "placement": {"type": "FLOW", "align": "STRETCH"},
        }},
        {"op": "SET_PLACEMENT", "params": {
            "regionId": "region_trend",
            "placement": {"type": "FLOW", "align": "STRETCH"},
        }},
    ]


def test_parse_produces_concrete_page_operations_without_mutation():
    definition = _definition()
    before = copy.deepcopy(definition.model_dump())
    plan = _parse(definition=definition)
    assert plan.intent == EditIntent.APPLY_PRESENTATION
    assert not plan.needsClarification
    assert plan.confidence == pytest.approx(0.85)
    assert plan.operation.presentationOperations == _expected_operations(12)
    # 解析只产出计划：定义一个字节都不动。
    assert definition.model_dump() == before


@pytest.mark.parametrize("selected", [None, "region_total", "region_trend"])
def test_selected_region_never_shrinks_the_page_scope(selected):
    """旧选中卡片不能把整页请求缩成单卡。"""
    plan = _parse(selected=selected)
    assert plan.intent == EditIntent.APPLY_PRESENTATION
    operations = plan.operation.presentationOperations
    touched = [op["params"]["regionId"] for op in operations if op["op"] == "SET_PLACEMENT"]
    assert touched == ["region_title", "region_total", "region_ranking", "region_trend"]


def test_summary_states_new_gap_arrangement_and_retention():
    plan = _parse()
    summary = plan.summary
    # 新间距数字必须出现（默认当前值 16 → 12）。
    assert "12" in summary
    assert "16" in summary
    # 排列方式。
    assert "单列连续排布" in summary
    # 保留项：标题与阅读顺序。
    assert "标题" in summary
    assert "阅读顺序" in summary


def test_current_gap_is_the_base_and_never_grows():
    definition = _definition(page={
        "arrangement": {"mode": "GRID", "columns": 12, "direction": "HORIZONTAL",
                        "gap": {"value": 24, "unit": "PX"}},
    })
    plan = _parse(definition=definition)
    assert plan.operation.presentationOperations[0]["params"]["arrangement"]["gap"] == {
        "value": 20, "unit": "PX",
    }


def test_already_tight_gap_is_kept_not_increased():
    definition = _definition(page={
        "arrangement": {"mode": "FLOW", "columns": 1, "direction": "VERTICAL",
                        "gap": {"value": 3, "unit": "PX"}},
    })
    plan = _parse(definition=definition)
    assert plan.operation.presentationOperations[0]["params"]["arrangement"]["gap"] == {
        "value": 3, "unit": "PX",
    }


def test_container_region_only_changes_its_own_placement():
    definition = _definition()
    definition.regions.append({
        "id": "group_1", "name": "概览组",
        "layout": {"placement": {"type": "GRID", "column": 1, "columnSpan": 12}},
        "component": {"type": "CONTAINER"},
        "children": [
            {"id": "group_1_child", "name": "组内卡片",
             "layout": {"placement": {"type": "GRID", "column": 1, "columnSpan": 6}},
             "component": {"type": "METRIC_CARD"}},
        ],
    })
    plan = _parse(definition=definition)
    touched = [op["params"]["regionId"]
               for op in plan.operation.presentationOperations if op["op"] == "SET_PLACEMENT"]
    assert "group_1" in touched
    assert "group_1_child" not in touched


# --------------------------------------------------------------------------
# C. 执行链路
# --------------------------------------------------------------------------

def test_apply_executes_the_batch_atomically():
    definition = _definition()
    before = copy.deepcopy(definition.model_dump())
    plan = _parse(definition=definition)
    result = template_edit_engine.apply(plan=plan, definition=definition)
    assert not result.needsClarification
    assert result.changeSet is not None
    # 执行后的回话也要说清楚「旧值 → 新值」。
    assert "12" in result.reply
    assert "16" in result.reply
    after = result.draftAfterEdit.model_dump()
    assert after["page"]["arrangement"] == {
        "mode": "FLOW", "columns": 1, "direction": "VERTICAL",
        "gap": {"value": 12, "unit": "PX"},
    }
    for region in after["regions"]:
        assert region["layout"]["placement"] == {"type": "FLOW", "align": "STRETCH"}
    # 最小变更：除 page.arrangement 与顶层 layout.placement 外一律不动。
    # （metadata.revision 由引擎统一 +1，是既有约定，不算改动。）
    stripped_before = copy.deepcopy(before)
    stripped_after = copy.deepcopy(after)
    for doc in (stripped_before, stripped_after):
        doc.pop("page")
        doc["metadata"].pop("revision", None)
        for region in doc["regions"]:
            region.get("layout", {}).pop("placement", None)
    assert stripped_after == stripped_before
    # 标题文字、id、样式、口径都还在。
    assert after["regions"][0]["name"] == "A 机房 昨日能耗日报"
    assert after["regions"][0]["id"] == "region_title"
    assert after["regions"][1]["context"]["timeWindow"] == {"kind": "YESTERDAY"}
    assert after["regions"][1]["component"]["metric"]["fieldRef"] == "energyKwh"


def test_apply_fails_atomically_when_one_operation_is_invalid():
    definition = _definition()
    before = copy.deepcopy(definition.model_dump())
    plan = _parse(definition=definition)
    plan.operation.presentationOperations[-1]["params"]["regionId"] = "region_not_exist"
    result = template_edit_engine.apply(plan=plan, definition=definition)
    assert result.needsClarification
    assert result.changeSet is None
    assert result.draftAfterEdit.model_dump() == before


def test_workbench_ask_produces_a_candidate_instead_of_asking_which_card(post_chat):
    """端到端：工作台里说这句，要拿到**候选**，不是「请告诉我是哪一张卡片」。"""
    definition = _definition().model_dump(mode="json")
    response = post_chat(
        COMPACT, state="USER_EDITING_TEMPLATE", snapshots={"reportDefinition": definition},
    ).json()
    pending = response["domainObjects"]["pendingEditPlan"]
    assert pending is not None, response
    operations = pending["operations"][0]["operation"]["presentationOperations"]
    assert operations[0]["op"] == "SET_PAGE"
    assert operations[0]["params"]["arrangement"]["mode"] == "FLOW"
    # 摘要里说清楚新间距与保留项。
    assert "12" in pending["summary"]
    assert "标题" in pending["summary"]
    # 还没接受修改：草稿不动。
    assert response["domainObjects"]["reportDefinitionDraft"] == definition
    # 不要再问「是哪一张卡片」。
    assert "哪一张卡片" not in json.dumps(response, ensure_ascii=False)
