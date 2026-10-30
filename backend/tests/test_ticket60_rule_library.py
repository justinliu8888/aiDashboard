"""工单 #60：展示规则库与命中依据持久化。

这里测的是**外部可见的行为**，不是内部实现：

- 规则库是一份随代码维护的结构化规则表：每条规则有稳定 ID、适用条件、
  建议展示、禁用条件、优先级与解释模板；没有规则数据库、没有后台、
  没有自动学习。
- 选型理由来自**真实命中**：方案里写的理由与命中的规则对得上，命中依据
  （规则 ID + 命中细节）随方案一起持久化。
- 禁用条件真的生效：被否定的写法不算明确指定；不可渲染的类型不参与
  定稿；模型已经声明了栏目时系统不再补列。
- 规则版本随方案持久化；规则库升版后**恢复同一份方案不无声改型**，
  历史方案已经给出的理由也不被追溯改写。
- 不强行凑「指标卡 + 趋势 + 排名」三件套；选图不授权业务计算；只用当前
  可渲染的组件类型。
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
from pathlib import Path

import pytest

from app.schemas.report_plan import ComponentType, ReportPlan
from app.services import report_rule_library as rules
from app.services.report_rule_library import (
    RENDERABLE_COMPONENT_TYPES,
    RULE_LIBRARY_VERSION,
    all_rules,
    rule_library_version,
)
from app.services.report_plan_service import apply_candidate
from app.storage.report_design_repository import ReportDesignRepository
from app.storage.report_design_sqlite import SqliteReportDesignRepository
from app.storage.sqlite_store import SqliteSessionRepository


# ---------- 夹具 ----------

_RULES_DOC = (
    Path(__file__).resolve().parents[2]
    / "docs"
    / "rules"
    / "report-rule-library.md"
)


def _candidate_payload(**overrides):
    payload = {
        "topic": "PUE 专题报表",
        "requirements": {
            "metrics": ["PUE"],
            "subject": None,
            "timeRange": None,
            "exclusions": [],
        },
        "sections": [
            {"title": "PUE 总览", "metricIntents": ["PUE"], "componentType": "metric_card"}
        ],
        "assumptions": [],
        "suggestions": [],
        "referenceTemplates": [],
    }
    payload.update(overrides)
    return payload


def _section(title: str, component_type: str, **extra):
    section = {
        "title": title,
        "metricIntents": [title],
        "componentType": component_type,
    }
    section.update(extra)
    return section


@pytest.fixture()
def store(tmp_path):
    """真实的设计持久化（独立临时 SQLite）。"""
    session_repo = SqliteSessionRepository(tmp_path / "design.db")
    repo: ReportDesignRepository = SqliteReportDesignRepository(
        connection=session_repo.conn, lock=threading.RLock()
    )
    repo.initialize_schema()
    yield repo
    session_repo.conn.close()


def _write_turn(session_id: str):
    def _callback(connection: sqlite3.Connection) -> None:
        now = "2026-10-30T00:00:00"
        connection.execute(
            "INSERT OR IGNORE INTO sessions (id, title, state, message_count,"
            " created_at, updated_at) VALUES (?, '新对话', 'ANALYZING', 0, ?, ?)",
            (session_id, now, now),
        )
        connection.execute(
            "INSERT INTO messages (session_id, seq, role, content, created_at)"
            " VALUES (?, 1, 'assistant', ?, ?)",
            (session_id, "方案已生成", now),
        )

    return _callback


def _commit(store, session_id: str, message_id: str, plan: ReportPlan) -> None:
    claim = store.claim_operation(session_id, message_id, None)
    store.commit_plan(
        claim["operationId"],
        None,
        plan.model_dump(mode="json"),
        _write_turn(session_id),
    )


# ---------- 1. 规则库是一份带稳定 ID 的结构化规则表 ----------


def test_every_rule_carries_the_six_required_fields():
    """稳定 ID、适用条件、建议展示、禁用条件、优先级、解释模板，一条不缺。"""
    for rule in all_rules():
        assert rule.rule_id, rule
        assert rule.family, rule
        assert isinstance(rule.priority, int), rule
        assert rule.suggested_display, rule
        assert rule.explain_template, rule
        assert isinstance(rule.applies_when, tuple), rule
        assert isinstance(rule.disabled_when, tuple), rule


def test_rule_ids_are_unique_and_stable():
    """规则 ID 不重复，且都带 ``DR-`` 前缀 —— 命中依据靠它回溯。"""
    ids = [rule.rule_id for rule in all_rules()]

    assert len(ids) == len(set(ids))
    assert all(rule_id.startswith("DR-") for rule_id in ids)


def test_the_priority_is_an_explicit_field_not_an_implied_tuple_order():
    """优先级写在规则上，而且三档选型优先级是显式的数字。"""
    by_id = {rule.rule_id: rule for rule in all_rules()}

    assert by_id["DR-EXPLICIT-BAR-CHART"].priority == 300
    assert by_id["DR-MAP-COMPARISON"].priority == 200
    assert by_id["DR-FALLBACK-MODEL"].priority == 100


def test_the_intent_rules_keep_their_precedence_as_an_explicit_order():
    """展示目的关键词的优先级是显式的，且与既有判定顺序一致。"""
    by_id = {rule.rule_id: rule for rule in all_rules()}

    assert by_id["DR-INTENT-COMPOSITION"].priority > by_id["DR-INTENT-COMPARISON"].priority
    assert by_id["DR-INTENT-COMPARISON"].priority > by_id["DR-INTENT-TREND"].priority
    assert by_id["DR-INTENT-TREND"].priority > by_id["DR-INTENT-RECORDS"].priority
    assert by_id["DR-INTENT-RECORDS"].priority > by_id["DR-INTENT-TOTAL"].priority


def test_the_rule_library_is_code_not_a_database():
    """规则库不建库、不建后台：模块里没有任何落库调用。"""
    source = Path(rules.__file__).read_text(encoding="utf-8")

    for forbidden in ("sqlite3", "INSERT INTO", "CREATE TABLE", "requests", "fastapi"):
        assert forbidden not in source, forbidden


def test_the_rule_library_has_a_versioned_document_that_matches_the_table():
    """规则库有一份带版本号的文档，文档里的版本与规则 ID 与表一致。"""
    doc = _RULES_DOC.read_text(encoding="utf-8")

    assert rule_library_version() in doc
    for rule in all_rules():
        assert rule.rule_id in doc, rule.rule_id


def test_the_document_lists_each_rule_with_the_same_priority_and_template():
    """文档里那一条规则，写着的优先级与解释模板与代码一致。"""
    doc = _RULES_DOC.read_text(encoding="utf-8")

    for rule in all_rules():
        rows = [
            line
            for line in doc.splitlines()
            if line.startswith(f"| `{rule.rule_id}` |")
        ]
        assert rows, rule.rule_id
        row = rows[0]
        assert f"| {rule.priority} |" in row, (rule.rule_id, row)
        assert rule.explain_template in row, (rule.rule_id, row)


# ---------- 2. 理由来自真实命中 ----------


def test_the_selection_reason_comes_from_the_rule_that_fired():
    """板块的选型理由与它记录的规则对得上 —— 理由是规则渲染出来的。"""
    payload = _candidate_payload()
    payload["sections"] = [
        _section("各机房耗电对比", "metric_card", displayIntent="comparison"),
    ]

    plan = apply_candidate(payload, plan_id="plan_060")

    evidence = plan.selectionEvidence[0]
    assert evidence.ruleId == "DR-MAP-COMPARISON"
    assert plan.sections[0].selectionReason == "按展示目的「比较不同对象」选柱状图。"
    assert "比较不同对象" in plan.sections[0].selectionReason


def _rendered_by(template: str, text: str) -> bool:
    """这段理由是不是这条规则的解释模板渲染出来的。

    模板里的占位符（``{intent_label}`` 之类）换成通配，其余字面量逐字比对 ——
    这样「理由是这条规则自己说的」可以机械地查，不靠人眼。
    """
    pattern = re.sub(r"\\\{[a-zA-Z_]+\\\}", ".+?", re.escape(template))
    return re.fullmatch(pattern, text) is not None


def test_every_section_records_a_rule_that_exists_in_the_library():
    """每块板块的命中依据都能在规则库里查到那条规则 —— ID 不是编出来的。"""
    payload = _candidate_payload()
    payload["sections"] = [
        _section("总耗电", "metric_card", displayIntent="total"),
        _section("耗电趋势", "metric_card", displayIntent="trend"),
        _section("各机房耗电对比", "metric_card", displayIntent="comparison"),
        _section("耗电构成", "metric_card", displayIntent="composition"),
        _section("当前告警列表", "table", displayIntent="records"),
        _section("说明：本表为模拟数据", "text", metricIntents=[]),
    ]

    plan = apply_candidate(payload, plan_id="plan_060")
    known = {rule.rule_id for rule in all_rules()}

    assert len(plan.selectionEvidence) == len(plan.sections)
    for evidence in plan.selectionEvidence:
        assert evidence.ruleId in known, evidence.ruleId


def test_the_reason_of_every_section_is_that_rule_own_explanation():
    """每块板块的理由都是**它自己那条规则**的解释模板渲染出来的。

    理由不是事后编的 —— 换个说法都对不上那条规则自己的模板。
    """
    payload = _candidate_payload()
    payload["sections"] = [
        _section("总耗电", "metric_card", displayIntent="total"),
        _section("耗电趋势", "metric_card", displayIntent="trend"),
        _section("各机房耗电对比", "metric_card", displayIntent="comparison"),
        _section("耗电构成", "metric_card", displayIntent="composition"),
        _section("说明：本表为模拟数据", "text", metricIntents=[]),
    ]

    plan = apply_candidate(payload, plan_id="plan_060")
    by_id = {rule.rule_id: rule for rule in all_rules()}
    evidence_by_section = {item.sectionId: item for item in plan.selectionEvidence}

    for section in plan.sections:
        evidence = evidence_by_section[section.id]
        rule = by_id[evidence.ruleId]
        assert _rendered_by(rule.explain_template, section.selectionReason), (
            evidence.ruleId,
            section.selectionReason,
        )


def test_the_hit_evidence_records_the_premise_that_fired():
    """命中依据写清楚是**哪个前提**成立的，不是一句「系统觉得」。"""
    payload = _candidate_payload()
    payload["sections"] = [
        _section("各机房耗电对比", "metric_card", displayIntent="comparison"),
    ]

    plan = apply_candidate(payload, plan_id="plan_060", current_request="各机房耗电对比用柱状图")

    evidence = plan.selectionEvidence[0]
    assert evidence.sectionId == plan.sections[0].id
    assert evidence.ruleId == "DR-EXPLICIT-BAR-CHART"
    assert "柱状图" in evidence.detail
    assert evidence.componentType == ComponentType.BAR_CHART


def test_the_evidence_names_the_keyword_that_fired_the_intent():
    """展示目的的命中依据要写清是**哪个关键词**读出来的。

    只写「展示目的=comparison」看不出依据来自哪里 —— 命中的关键词也要
    记下来，理由才真能一路追溯到真实命中。
    """
    payload = _candidate_payload()
    payload["sections"] = [
        _section("各机房耗电对比", "metric_card"),
    ]

    plan = apply_candidate(payload, plan_id="plan_060", current_request="看各机房的对比")

    evidence = plan.selectionEvidence[0]
    assert evidence.ruleId == "DR-MAP-COMPARISON"
    assert "对比" in evidence.detail


def test_the_evidence_says_the_display_intent_came_from_the_model_candidate():
    """展示目的来自模型的语义候选时，依据要写明这一点。"""
    payload = _candidate_payload()
    payload["sections"] = [
        _section("各机房耗电对比", "metric_card", displayIntent="comparison"),
    ]

    plan = apply_candidate(payload, plan_id="plan_060")

    evidence = plan.selectionEvidence[0]
    assert evidence.ruleId == "DR-MAP-COMPARISON"
    assert "模型给的语义候选" in evidence.detail


def test_the_most_specific_wording_wins_at_the_same_position():
    """同一位置命中多个写法时取**更具体**的那个。

    「用折线图」命中的是「折线图」那条规则，不是更短的「折线」——
    与改造前「长的先匹配」的口径一致，命中依据才指得准。
    """
    hit = rules.match_explicit_component_type("用折线图")

    assert hit is not None
    assert hit.rule_id == "DR-EXPLICIT-LINE-CHART"
    assert "折线图" in hit.detail


def test_the_carried_over_type_records_the_canonical_rule():
    """沿用上一版已指定的类型时，依据指向该类型的**正式叫法**那条规则。

    上一版用户明确说了「用指标卡」，这一版没改这块 → 类型沿用，依据仍
    指向「指标卡」那条规则，而不是同类型的另一个冷僻写法。
    """
    payload = _candidate_payload()
    payload["sections"] = [
        _section("总耗电", "metric_card"),
    ]
    previous = apply_candidate(
        payload, plan_id="plan_060", current_request="总耗电用指标卡"
    )
    assert previous.sections[0].explicitComponentType == ComponentType.METRIC_CARD

    updated = apply_candidate(
        _candidate_payload(sections=[_section("总耗电", "metric_card")]),
        previous=previous.model_dump(mode="json"),
        expected_revision=1,
    )

    # 用户没说改类型 → 沿用上一版；依据仍指向「指标卡」那条规则。
    assert updated.sections[0].explicitComponentType == ComponentType.METRIC_CARD
    assert updated.selectionEvidence[0].ruleId == "DR-EXPLICIT-METRIC-CARD"


def test_the_model_only_gives_a_semantic_candidate_the_server_finalizes():
    """模型给的展示目的只是语义候选，组件类型由服务端按规则定稿。"""
    payload = _candidate_payload()
    payload["sections"] = [
        _section("耗电情况", "metric_card", displayIntent="total", metricIntents=["耗电"]),
    ]

    plan = apply_candidate(payload, plan_id="plan_060")

    assert plan.sections[0].componentType == ComponentType.METRIC_CARD
    assert plan.sections[0].explicitComponentType is None
    assert plan.selectionEvidence[0].ruleId == "DR-MAP-TOTAL"


# ---------- 3. 禁用条件真的生效 ----------


def test_a_negated_type_word_is_not_an_explicit_instruction():
    """「不要柱状图」不是「用柱状图」：禁用条件挡住了它。"""
    payload = _candidate_payload()
    payload["sections"] = [
        _section("各机房耗电对比", "metric_card", displayIntent="comparison"),
    ]

    plan = apply_candidate(payload, plan_id="plan_060", current_request="不要柱状图")

    assert plan.sections[0].explicitComponentType is None
    assert plan.sections[0].componentType == ComponentType.BAR_CHART
    assert plan.selectionEvidence[0].ruleId == "DR-MAP-COMPARISON"


def test_a_plain_text_section_is_not_turned_into_a_chart():
    """纯说明板块不适用展示目的规则：不用请求里的话去猜它是哪张图。"""
    payload = _candidate_payload()
    payload["sections"] = [
        _section("说明：本表数据为模拟", "text", metricIntents=[]),
    ]

    plan = apply_candidate(payload, plan_id="plan_060", current_request="看总量")

    assert plan.sections[0].componentType == ComponentType.TEXT
    assert plan.sections[0].displayIntent is None
    assert plan.selectionEvidence[0].ruleId == "DR-FALLBACK-TEXT"


def test_a_type_that_is_not_renderable_does_not_win_the_selection(monkeypatch):
    """建议的类型当前不可渲染时，这条规则被禁用，落到下一档。"""
    mapping = tuple(
        rules.DisplayRule(
            rule_id=rule.rule_id,
            family=rule.family,
            priority=rule.priority,
            applies_when=rule.applies_when,
            suggested_display=ComponentType.TEXT.value,
            disabled_when=rule.disabled_when,
            explain_template=rule.explain_template,
            reason_template=rule.reason_template,
            match_values=rule.match_values,
            payload=rule.payload,
        )
        if rule.rule_id == "DR-MAP-COMPARISON"
        else rule
        for rule in rules.MAPPING_RULES
    )
    monkeypatch.setattr(rules, "MAPPING_RULES", mapping)

    payload = _candidate_payload()
    payload["sections"] = [
        _section("各机房耗电对比", "metric_card", displayIntent="comparison"),
    ]

    plan = apply_candidate(payload, plan_id="plan_060")

    # 映射规则被禁用（文字板块不是可渲染的数据展示），落到模型原选。
    assert plan.sections[0].componentType == ComponentType.METRIC_CARD
    assert plan.selectionEvidence[0].ruleId == "DR-FALLBACK-MODEL"


def test_the_column_preset_does_not_apply_when_the_model_declared_columns():
    """模型已经声明了栏目时，系统不再按板块语义补列。"""
    payload = _candidate_payload()
    payload["sections"] = [
        _section(
            "当前告警列表",
            "table",
            displayIntent="records",
            columns=[{"label": "设备名称"}, {"label": "时间"}],
        )
    ]

    plan = apply_candidate(payload, plan_id="plan_060")

    assert [column.label for column in plan.sections[0].columns] == ["设备名称", "时间"]


# ---------- 4. 命中依据与规则版本随方案持久化 ----------


def test_the_plan_carries_the_rule_version_and_the_hit_evidence():
    """方案自带规则版本与命中依据。"""
    payload = _candidate_payload()
    payload["sections"] = [
        _section("各机房耗电对比", "metric_card", displayIntent="comparison"),
    ]

    plan = apply_candidate(payload, plan_id="plan_060")

    assert plan.ruleVersion == RULE_LIBRARY_VERSION
    assert len(plan.selectionEvidence) == len(plan.sections)
    assert plan.selectionEvidence[0].sectionId == plan.sections[0].id


def test_the_rule_version_and_the_evidence_survive_a_save_and_a_reread():
    """存下来再读回来，规则版本与命中依据都不丢。"""
    payload = _candidate_payload()
    payload["sections"] = [
        _section("各机房耗电对比", "metric_card", displayIntent="comparison"),
    ]

    plan = apply_candidate(payload, plan_id="plan_060")
    reread = ReportPlan.model_validate(plan.model_dump(mode="json"))

    assert reread.ruleVersion == plan.ruleVersion
    assert reread.selectionEvidence == plan.selectionEvidence
    assert reread.sections[0].selectionReason == plan.sections[0].selectionReason


def test_the_plan_lands_in_the_store_with_its_rule_version_and_evidence(store):
    """方案落库时带着规则版本与命中依据。"""
    payload = _candidate_payload()
    payload["sections"] = [
        _section("各机房耗电对比", "metric_card", displayIntent="comparison"),
    ]
    plan = apply_candidate(payload, plan_id="plan_060")

    _commit(store, "sess_060", "msg_060", plan)

    state = store.load("sess_060")
    assert state is not None
    stored = state["reportPlan"]
    assert stored["ruleVersion"] == RULE_LIBRARY_VERSION
    assert stored["selectionEvidence"][0]["ruleId"] == "DR-MAP-COMPARISON"


# ---------- 5. 规则版本变化不追溯改写历史方案 ----------


def _upgraded_mapping_rules():
    """模拟「规则库升版」：比较目的改用折线图。"""
    return tuple(
        rules.DisplayRule(
            rule_id=rule.rule_id,
            family=rule.family,
            priority=rule.priority,
            applies_when=rule.applies_when,
            suggested_display=ComponentType.LINE_CHART.value,
            disabled_when=rule.disabled_when,
            explain_template=rule.explain_template,
            reason_template=rule.reason_template,
            match_values=rule.match_values,
            payload=rule.payload,
        )
        if rule.rule_id == "DR-MAP-COMPARISON"
        else rule
        for rule in rules.MAPPING_RULES
    )


def test_restoring_a_plan_after_the_rule_library_changed_does_not_retype_it(
    store, monkeypatch
):
    """规则库升版后打开同一份方案：组件类型、理由、规则版本都不变。

    恢复走的是**存下来的那份方案**，不重新跑一遍选型 —— 否则用户看到的
    报告会无声改型。
    """
    payload = _candidate_payload()
    payload["sections"] = [
        _section("各机房耗电对比", "metric_card", displayIntent="comparison"),
    ]
    plan = apply_candidate(payload, plan_id="plan_060")
    _commit(store, "sess_060", "msg_060", plan)

    monkeypatch.setattr(rules, "MAPPING_RULES", _upgraded_mapping_rules())
    monkeypatch.setattr(rules, "RULE_LIBRARY_VERSION", "9.9.9")

    state = store.load("sess_060")
    assert state is not None
    restored = state["reportPlan"]

    assert restored["sections"][0]["componentType"] == "bar_chart"
    assert restored["sections"][0]["selectionReason"] == "按展示目的「比较不同对象」选柱状图。"
    assert restored["ruleVersion"] == RULE_LIBRARY_VERSION
    assert restored["selectionEvidence"][0]["ruleId"] == "DR-MAP-COMPARISON"


def test_a_rule_library_change_does_not_rewrite_the_history(store, monkeypatch):
    """规则库升版不改写历史方案已经给出的理由 —— 只影响之后的新版本。"""
    payload = _candidate_payload()
    payload["sections"] = [
        _section("各机房耗电对比", "metric_card", displayIntent="comparison"),
    ]
    plan = apply_candidate(payload, plan_id="plan_060")
    _commit(store, "sess_060", "msg_060", plan)

    monkeypatch.setattr(rules, "MAPPING_RULES", _upgraded_mapping_rules())
    monkeypatch.setattr(rules, "RULE_LIBRARY_VERSION", "9.9.9")

    # 升版后新出的方案按新规则走，并记下新版本。
    updated = apply_candidate(
        _candidate_payload(
            sections=[
                _section("各机房耗电对比", "metric_card", displayIntent="comparison"),
            ]
        ),
        previous=plan.model_dump(mode="json"),
        expected_revision=1,
    )
    assert updated.ruleVersion == "9.9.9"
    assert updated.sections[0].componentType == ComponentType.LINE_CHART

    # 历史方案（库里那一份）一个字都没变。
    stored = store.load("sess_060")["reportPlan"]
    assert stored["ruleVersion"] == RULE_LIBRARY_VERSION
    assert stored["sections"][0]["componentType"] == "bar_chart"
    assert stored["sections"][0]["selectionReason"] == "按展示目的「比较不同对象」选柱状图。"


def test_an_old_plan_without_a_rule_version_is_still_readable():
    """老方案没有规则版本与命中依据 —— 照样读得懂，不迁移、不改写。"""
    old_plan = {
        "id": "plan_old",
        "revision": 1,
        "topic": "PUE 专题报表",
        "requirements": {
            "metrics": ["PUE"],
            "subject": None,
            "timeRange": None,
            "exclusions": [],
        },
        "sections": [
            {
                "id": "section_1",
                "title": "PUE 总览",
                "metricIntents": ["PUE"],
                "componentType": "metric_card",
                "displayIntent": None,
                "explicitComponentType": None,
                "selectionReason": "沿用模型给出的指标卡。",
                "columns": [],
            }
        ],
        "assumptions": [],
        "suggestions": [],
        "referenceTemplates": [],
    }

    plan = ReportPlan.model_validate(old_plan)

    assert plan.ruleVersion is None
    assert plan.selectionEvidence == []
    assert plan.sections[0].selectionReason == "沿用模型给出的指标卡。"


# ---------- 6. 不凑三件套、只用可渲染类型、选图不授权业务计算 ----------


def test_the_plan_does_not_grow_sections_the_user_did_not_ask_for():
    """要一块总量表就只给一块 —— 不凑「指标卡 + 趋势 + 排名」三件套。"""
    payload = _candidate_payload()
    payload["sections"] = [
        _section("总耗电", "metric_card", displayIntent="total"),
    ]

    plan = apply_candidate(payload, plan_id="plan_060")

    assert len(plan.sections) == 1
    assert plan.sections[0].componentType == ComponentType.METRIC_CARD


def test_only_renderable_component_types_are_selected():
    """只用当前可渲染的组件类型 —— 不扩图表库。"""
    payload = _candidate_payload()
    payload["sections"] = [
        _section("总耗电", "metric_card", displayIntent="total"),
        _section("耗电趋势", "metric_card", displayIntent="trend"),
        _section("各机房耗电对比", "metric_card", displayIntent="comparison"),
        _section("耗电构成", "metric_card", displayIntent="composition"),
    ]

    plan = apply_candidate(payload, plan_id="plan_060")

    for section in plan.sections:
        assert section.componentType in RENDERABLE_COMPONENT_TYPES


def test_selecting_a_chart_does_not_authorize_business_calculation():
    """选了图不等于授权了业务计算：指标意图与需求不被系统改写。"""
    payload = _candidate_payload()
    payload["sections"] = [
        _section("总耗电", "metric_card", displayIntent="total", metricIntents=["总耗电"]),
    ]

    plan = apply_candidate(payload, plan_id="plan_060")

    assert plan.sections[0].metricIntents == ["总耗电"]
    assert plan.requirements.metrics == ["PUE"]
    assert plan.requirements.exclusions == []


# ---------- 7. 规则库版本号本身 ----------


def test_the_rule_library_version_is_a_non_empty_version_string():
    """版本号得像个版本号 —— 方案靠它分辨「当时是哪一版规则」。"""
    version = rule_library_version()

    assert version
    assert version == RULE_LIBRARY_VERSION
    assert version[0].isdigit()


def test_the_stored_plan_json_is_plain_json():
    """落库的方案就是普通 JSON —— 命中依据与规则版本一起进去。"""
    payload = _candidate_payload()
    payload["sections"] = [
        _section("各机房耗电对比", "metric_card", displayIntent="comparison"),
    ]
    plan = apply_candidate(payload, plan_id="plan_060")

    dumped = json.loads(plan.model_dump_json())

    assert dumped["ruleVersion"] == RULE_LIBRARY_VERSION
    assert dumped["selectionEvidence"][0]["sectionId"] == plan.sections[0].id
