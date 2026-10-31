"""R1 §3.4：确定性快路回答里带命令动词时，必须如实说明「这一轮没有执行」。

背景（代码审查 R1 §3.4）：用户在编辑态选中一张卡片说
「**导出**这个可以展示吗」，意图门判成 MODIFY（明确命令），确定性快路内部
又用另一套谓词把它改判成「只读提问」→ 界面只回答了展示方式，
「导出」这半句**既没执行、也没提示**，等于说假话。

本文件钉住三件事：
    1. 快路给出回答时，句子里带的命令动词必须被如实点名说明「没执行」；
    2. 快路**原有**回答逐字保留（只许在末尾追加）；
    3. 意图门（``routing_rules.is_readonly_question``）的判定逐条不变。

接口测试走聊天 HTTP 入口，不测私有函数。
"""
from __future__ import annotations

import pytest

import app.services.agent_service as agent_service_module
from app.services import routing_rules
from app.services.report_draft_service import build_definition_draft


@pytest.fixture()
def definition():
    return build_definition_draft("A 机房能耗日报", "room_a")


def _snap(definition):
    return {"reportDefinition": definition.model_dump(mode="json")}


def _ask(post_chat, definition, message, selected="region_total_energy"):
    return post_chat(
        message,
        state="USER_EDITING_TEMPLATE",
        snapshots=_snap(definition),
        selectedRegionId=selected,
    ).json()


#: 审查点名的三句 + 一句改动动词对照。
COMMAND_CASES = [
    ("导出这个可以展示吗", "导出"),
    ("保存这个可以展示吗", "保存"),
    ("发布这个可以展示吗", "发布"),
    ("重新开始这个可以展示吗", "重新开始"),
]


# ---------- 1. 命令动词必须被点名说明「没执行」 ----------


@pytest.mark.parametrize("message,verb", COMMAND_CASES)
def test_command_verb_is_reported_as_not_executed(post_chat, definition, message, verb):
    """命令那半句被静默丢掉 = 说假话：这一轮必须如实说明没执行。"""
    response = _ask(post_chat, definition, message)
    reply = response["reply"]

    assert "没有执行" in reply
    assert f"你说的「{verb}」" in reply
    assert "我这一轮只做了回答" in reply
    # 只提「没执行」这一层意思：有没有导出按钮这类事实没核实过，不许替产品下结论。
    assert "界面" not in reply
    assert "做不了" not in reply
    # 这一轮仍然没有改草稿、也没有产生待接受的修改计划。
    assert response["domainObjects"]["pendingEditPlan"] is None
    assert response["domainObjects"]["reportDefinitionDraft"]["metadata"]["revision"] == 0


def test_multiple_command_verbs_are_all_reported(post_chat, definition):
    """一条消息里有多个没执行的命令动词时都提。"""
    reply = _ask(post_chat, definition, "导出并保存这个可以展示吗")["reply"]

    assert "「导出」" in reply
    assert "「保存」" in reply
    assert "没有执行" in reply


# ---------- 2. 原有回答逐字保留 ----------


def test_original_answer_is_kept_verbatim(post_chat, definition):
    """只许在末尾追加那一句实话：原有的话逐字仍在。"""
    baseline = _ask(post_chat, definition, "这个可以展示吗")["reply"]

    for message, _ in COMMAND_CASES:
        reply = _ask(post_chat, definition, message)["reply"]
        assert reply.startswith(baseline), message
        assert "当前可用的展示方式" in reply
        assert "草稿没有变化，也没有生成待接受的修改计划" in reply


def test_readonly_question_without_command_verb_has_no_not_executed_note(
    post_chat, definition
):
    """没有命令动词的普通提问：不许多出「没执行」那一句。"""
    reply = _ask(post_chat, definition, "这个可以展示吗")["reply"]

    assert "没有执行" not in reply
    assert "当前可用的展示方式" in reply


# ---------- 3. 改动动词：快路不回答，不许出现「没执行」 ----------


def test_change_command_is_not_answered_by_readonly_fast_path(post_chat, definition):
    """「改成柱状图」是改动动词 → 走编辑分支；那条路上不许提「没执行」。"""
    response = _ask(post_chat, definition, "改成柱状图")

    assert response["debug"].get("editOutcome") != "readonly_answer"
    assert "没有执行" not in response["reply"]


def test_command_verb_executed_by_edit_branch_has_no_not_executed_note(
    post_chat, definition
):
    """命令确实被这一轮执行了（删除分支出了候选计划）→ 不许提「没执行」。"""
    response = _ask(post_chat, definition, "删除这个可以展示吗")

    assert response["debug"].get("editOutcome") == "candidate"
    assert response["domainObjects"]["pendingEditPlan"] is not None
    assert "没有执行" not in response["reply"]


# ---------- 4. 意图门没被动 ----------


@pytest.mark.parametrize(
    "message,expected",
    [
        ("你有未完成的草稿吗？", True),
        ("刚才那份做到哪了？", True),
        ("你支持分钟趋势吗？", True),
        ("把趋势图改成折线图", False),
        ("生成 A 机房昨日能耗日报", False),
        ("今天天气怎么样", False),
        ("导出这个可以展示吗", False),
        ("保存这个可以展示吗", False),
        ("发布这个可以展示吗", False),
        ("重新开始这个可以展示吗", False),
    ],
)
def test_intent_gate_readonly_question_is_unchanged(message, expected):
    """意图门那一套的判定逐条不变（这轮没有动它）。"""
    assert routing_rules.is_readonly_question(message) is expected


# ---------- 5. 词表归属收口 ----------


def test_readonly_word_tables_are_owned_by_routing_rules():
    """两张表搬到 routing_rules 独家持有；agent_service 不再各留一份。"""
    assert routing_rules.READONLY_QUESTION_WORDS == (
        "哪些", "什么", "怎么", "能不能", "可不可以", "能否", "是否",
        "有没有", "支持", "吗", "？", "?",
    )
    assert routing_rules.EDIT_COMMAND_WORDS == (
        "改成", "改为", "换成", "换为", "调成", "设为", "设定为",
    )
    assert not hasattr(agent_service_module, "READONLY_QUESTION_WORDS")
    assert not hasattr(agent_service_module, "EDIT_COMMAND_WORDS")


def test_command_verbs_in_reports_each_verb_once():
    """长词优先：「重新生成」里含「生成」，只算一个。"""
    assert routing_rules.command_verbs_in("重新生成这个可以展示吗") == ("重新生成",)
    assert routing_rules.command_verbs_in("导出并保存这个可以展示吗") == ("导出", "保存")
    assert routing_rules.command_verbs_in("这个可以展示吗") == ()
