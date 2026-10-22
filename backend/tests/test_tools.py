"""工具层测试：契约一致性、清单导出、上下文读取、终结工具提交。"""
from __future__ import annotations

import asyncio

import pytest

from app.schemas.agent import AgentState
from app.services.report_draft_service import build_definition_draft
from app.tools import registry
from app.tools.base import ToolCategory, is_conclusion_legal
from app.tools.context import (
    ToolContext,
    bind_tool_context,
    clear_tool_context,
)
from app.tools.submission import (
    bind_submission,
    clear_submission,
    peek_submission,
    take_submission,
)
from app.tools.registry import bind_lookup_failure, clear_lookup_failure, take_lookup_failure


AUTHORIZED_PROJECT = "project_001"
UNAUTHORIZED_PROJECT = "project_999"
AUTHORIZED_ROOM = "room_a"
UNAUTHORIZED_ROOM = "room_b"


@pytest.fixture(autouse=True)
def _clean_context():
    bind_submission()
    clear_tool_context()
    yield
    clear_tool_context()
    clear_submission()


def _bind_scope(
    *,
    project_id: str = AUTHORIZED_PROJECT,
    subject_id: str = AUTHORIZED_ROOM,
    state: AgentState = AgentState.ANALYZING,
) -> None:
    """绑定一个带授权范围的工具上下文。

    查目录 / 查指标 / 查能力都要先过这一关，没有范围就查不到。
    """
    bind_tool_context(
        ToolContext(
            state=state,
            subject_ref="machine_room",
            subject_id=subject_id,
            project_id=project_id,
        )
    )


# ---------- 契约一致性 ----------

def test_every_tool_follows_the_contract():
    """每个工具都必须暴露统一契约，否则无法统一注册 / 上中台。"""
    for tool in registry.list_tools():
        assert tool.NAME, f"{tool.__name__} 缺少 NAME"
        assert tool.DESCRIPTION, f"{tool.NAME} 缺少 DESCRIPTION"
        assert tool.PARAMETERS.get("type") == "object", f"{tool.NAME} 入参不是 object"
        assert isinstance(tool.CATEGORY, ToolCategory), f"{tool.NAME} 缺少 CATEGORY"
        assert isinstance(tool.IS_READ_ONLY, bool), f"{tool.NAME} 缺少 IS_READ_ONLY"
        assert isinstance(tool.APPLICABLE_STATES, tuple), f"{tool.NAME} 缺少 APPLICABLE_STATES"
        assert callable(tool.run), f"{tool.NAME} 缺少 run()"


def test_tool_names_are_unique():
    names = [tool.NAME for tool in registry.list_tools()]
    assert len(names) == len(set(names))


def test_terminal_tools_are_not_read_only():
    """终结工具会写提交槽，权限语义不能沿用只读标记。"""
    for tool in registry.list_tools():
        if tool.CATEGORY == ToolCategory.TERMINAL:
            assert tool.IS_READ_ONLY is False, f"{tool.NAME} 终结工具不应标为只读"


def test_only_read_only_tools_are_fact_and_builder():
    for tool in registry.list_tools():
        if tool.CATEGORY in {ToolCategory.FACT, ToolCategory.BUILDER}:
            assert tool.IS_READ_ONLY is True, f"{tool.NAME} 查询类工具应标为只读"


def test_applicable_states_are_valid():
    valid = {state.value for state in AgentState}
    for tool in registry.list_tools():
        for state in tool.APPLICABLE_STATES:
            assert state in valid, f"{tool.NAME} 的 APPLICABLE_STATES 含非法状态 {state}"


def test_every_tool_declares_applicable_states():
    """白名单必须显式声明。

    空元组意味着"全节点可用"，那等于没有收窄 —— 节点级白名单就白做了。
    """
    for tool in registry.list_tools():
        assert tool.APPLICABLE_STATES, f"{tool.NAME} 没声明 APPLICABLE_STATES"


# ---------- 结论状态契约 ----------

def test_terminal_tools_declare_conclusion_states():
    """终结工具必须声明它能产出哪些结论状态。"""
    for tool in registry.list_tools():
        if tool.CATEGORY == ToolCategory.TERMINAL:
            assert registry.conclusion_states(tool), (
                f"{tool.NAME} 是终结工具但没声明 CONCLUSION_STATES"
            )


def test_non_terminal_tools_have_no_conclusion_states():
    for tool in registry.list_tools():
        if tool.CATEGORY != ToolCategory.TERMINAL:
            assert not registry.conclusion_states(tool), (
                f"{tool.NAME} 不是终结工具，不该声明 CONCLUSION_STATES"
            )


def test_tool_contracts_are_self_consistent():
    """工具可用节点 ⇔ 它的每个结论状态在该节点都是合法转移。"""
    problems = registry.validate_tool_contracts()
    assert problems == [], "工具契约不自洽：\n" + "\n".join(problems)


def test_conclusion_legality_matches_state_machine():
    """单独验证合法性判定本身，避免上面那条测试变成"自证"。"""
    assert is_conclusion_legal(
        AgentState.ANALYZING, AgentState.WAITING_USER_CONFIRM
    )
    assert not is_conclusion_legal(
        AgentState.VALIDATION_FAILED, AgentState.WAITING_USER_CONFIRM
    )
    # 保持原状态算合法
    assert is_conclusion_legal(
        AgentState.USER_EDITING_TEMPLATE, AgentState.USER_EDITING_TEMPLATE
    )


# ---------- 清单导出（中台上架用） ----------

def test_manifest_contains_every_tool():
    manifest = registry.export_manifest()
    assert manifest["contractVersion"]
    assert len(manifest["tools"]) == len(registry.list_tools())


def test_manifest_entry_shape():
    """中台按这份清单注册 skill，字段必须齐。"""
    for entry in registry.export_manifest()["tools"]:
        assert set(entry) == {
            "name",
            "description",
            "parameters",
            "category",
            "readOnly",
            "applicableStates",
            "conclusionStates",
        }
        assert entry["name"] and entry["description"]
        assert entry["parameters"]["type"] == "object"
        assert entry["category"] in {c.value for c in ToolCategory}


def test_manifest_exports_conclusion_states():
    """终结工具的结论状态要随清单导出，中台才知道这个 skill 能落成什么状态。"""
    entries = {e["name"]: e for e in registry.export_manifest()["tools"]}
    assert entries["submit_draft_plan"]["conclusionStates"] == [
        AgentState.WAITING_USER_CONFIRM.value
    ]
    assert entries["submit_edit_plan"]["conclusionStates"] == [
        AgentState.USER_EDITING_TEMPLATE.value,
    ]
    assert entries["submit_edit_answer"]["conclusionStates"] == [
        AgentState.USER_EDITING_TEMPLATE.value,
    ]
    assert entries["submit_edit_answer"]["readOnly"] is False
    assert entries["search_catalog"]["conclusionStates"] == []


def test_manifest_json_is_serializable():
    import json

    data = json.loads(registry.export_manifest_json())
    assert data["tools"]


# ---------- 按状态分流 ----------

def test_tools_for_state_narrows_by_node():
    """不同节点拿到的工具集合不同。"""
    analyzing = {t.NAME for t in registry.tools_for_state(AgentState.ANALYZING)}
    editing = {
        t.NAME for t in registry.tools_for_state(AgentState.USER_EDITING_TEMPLATE)
    }
    fixing = {t.NAME for t in registry.tools_for_state(AgentState.VALIDATION_FAILED)}

    # 生成态能交草稿计划，编辑态不能（会把用户改好的草稿覆盖掉）
    assert "submit_draft_plan" in analyzing
    assert "submit_draft_plan" not in editing
    assert "build_definition_draft" in analyzing
    assert "build_definition_draft" not in editing
    assert "submit_edit_plan" in editing
    assert "submit_edit_answer" in editing
    assert "submit_edit_answer" not in analyzing
    assert "submit_edit_answer" not in fixing
    assert "submit_clarification" not in editing

    # 校验失败节点只能交修复方案
    assert "submit_fix_plan" in fixing
    assert "submit_draft_plan" not in fixing
    assert "get_validation_result" in fixing
    assert "get_validation_result" not in analyzing


def test_tools_for_state_keeps_lookup_tools_everywhere():
    """只读事实工具任何业务节点都能用。"""
    for state in AgentState:
        names = {t.NAME for t in registry.tools_for_state(state)}
        if "search_catalog" in names:
            assert "search_metric" in names


def test_get_runner_finds_tool():
    runner = registry.get_runner("search_catalog")
    assert runner is not None
    _bind_scope()
    assert runner(sourceRef="catalog.machine_room.energy")["items"]


def test_get_runner_returns_none_for_unknown():
    assert registry.get_runner("not_a_tool") is None


# ---------- 只读工具的事实查询 ----------

def test_search_catalog_returns_items():
    _bind_scope()
    result = registry.get_runner("search_catalog")(subjectRef="machine_room")
    assert result["items"]
    assert result["total"] == len(result["items"])


def test_search_metric_returns_items():
    _bind_scope()
    result = registry.get_runner("search_metric")(subjectRef="machine_room")
    assert result["items"]
    assert result["total"] == len(result["items"])


def test_check_capability_reports_supported():
    _bind_scope()
    result = registry.get_runner("check_capability")(
        sourceRef="catalog.machine_room.energy",
        grainUnit="HOUR",
    )
    assert result["supported"] is True
    assert result["supportedGrains"]


def test_check_capability_reports_unsupported():
    """机房能耗源不支持分钟级，工具要如实回答。"""
    _bind_scope()
    result = registry.get_runner("check_capability")(
        sourceRef="catalog.machine_room.energy",
        grainUnit="MINUTE",
    )
    assert result["supported"] is False


def test_lookup_tools_require_scope():
    """没绑定上下文就查不了 —— 宁可查不到，也不能让模型问到不该看的数据。"""
    runner = registry.get_runner("search_catalog")
    with pytest.raises(PermissionError):
        runner(subjectRef="machine_room")


def test_search_catalog_hides_unauthorized_project():
    """项目不在授权范围内时，目录查不到。"""
    _bind_scope(project_id=UNAUTHORIZED_PROJECT)
    result = registry.get_runner("search_catalog")(subjectRef="machine_room")
    assert result["items"] == []
    assert result["total"] == 0


def test_search_metric_hides_unauthorized_project():
    _bind_scope(project_id=UNAUTHORIZED_PROJECT)
    result = registry.get_runner("search_metric")(subjectRef="machine_room")
    assert result["items"] == []


def test_search_catalog_hides_unauthorized_subject_instance():
    """业务对象实例不在授权范围内时，目录查不到。"""
    _bind_scope(subject_id=UNAUTHORIZED_ROOM)
    result = registry.get_runner("search_catalog")(subjectRef="machine_room")
    assert result["items"] == []


def test_check_capability_blocks_known_but_unauthorized_source():
    """模型已经知道 sourceRef 也不能越权探测能力。"""
    _bind_scope(project_id=UNAUTHORIZED_PROJECT)
    result = registry.get_runner("check_capability")(
        sourceRef="catalog.machine_room.energy",
        grainUnit="HOUR",
    )
    assert result["supported"] is False
    assert result["supportedGrains"] == []


def test_build_definition_draft_returns_both_drafts():
    result = registry.get_runner("build_definition_draft")(
        reportName="A 机房能耗日报",
        roomId="room_a",
    )
    assert result["policyDraft"]["name"] == "A 机房能耗日报"
    assert result["definitionDraft"]["regions"]


# ---------- 读草稿类工具 ----------

def test_get_current_draft_without_context():
    result = registry.get_runner("get_current_draft")()
    assert result["exists"] is False


def test_get_current_draft_with_context():
    definition = build_definition_draft("A 机房能耗日报", "room_a")
    bind_tool_context(
        ToolContext(state=AgentState.DEFINITION_DRAFT_READY, definition=definition)
    )
    result = registry.get_runner("get_current_draft")()
    assert result["exists"] is True
    assert result["reportName"] == "A 机房能耗日报"
    assert result["regions"]
    assert all("regionId" in r for r in result["regions"])


def test_get_current_region_found_and_missing():
    definition = build_definition_draft("A 机房能耗日报", "room_a")
    bind_tool_context(
        ToolContext(state=AgentState.USER_EDITING_TEMPLATE, definition=definition)
    )
    runner = registry.get_runner("get_current_region")

    found = runner(regionId="region_hourly_trend")
    assert found["found"] is True
    assert found["region"]["id"] == "region_hourly_trend"

    missing = runner(regionId="region_not_exist")
    assert missing["found"] is False
    assert missing["availableRegionIds"]


def test_get_validation_result_without_context():
    result = registry.get_runner("get_validation_result")()
    assert result["available"] is False


def test_get_validation_result_with_context():
    bind_tool_context(
        ToolContext(
            state=AgentState.VALIDATION_FAILED,
            last_validation_result={
                "passed": False,
                "issues": [
                    {
                        "layer": "STRUCTURE",
                        "code": "REGIONS_EMPTY",
                        "message": "报表没有任何区域，无法预览。",
                    }
                ],
            },
        )
    )
    result = registry.get_runner("get_validation_result")()
    assert result["available"] is True
    assert result["passed"] is False
    assert result["issues"][0]["code"] == "REGIONS_EMPTY"


# ---------- 终结工具 ----------

def test_submit_draft_plan_records_submission():
    result = registry.get_runner("submit_draft_plan")(
        reportName="A 机房能耗日报",
        roomId="room_a",
        includeTrend=True,
        reply="已生成草稿。",
    )
    assert result["accepted"] is True
    submission = peek_submission()
    assert submission["tool"] == "submit_draft_plan"
    assert submission["payload"]["reportName"] == "A 机房能耗日报"


def test_submit_clarification_records_missing_info():
    result = registry.get_runner("submit_clarification")(
        missingInfo=[
            {
                "field": "timeRange",
                "label": "时间范围",
                "expects": "SINGLE_CHOICE",
                "options": [{"value": "last7d", "label": "最近 7 天"}],
            }
        ],
        nextQuestion="要看哪段时间？",
    )
    assert result["accepted"] is True
    payload = peek_submission()["payload"]
    assert payload["nextQuestion"] == "要看哪段时间？"
    assert payload["missingInfo"][0]["expects"] == "SINGLE_CHOICE"


def test_submit_edit_plan_carries_options():
    """用户表达含糊时，模型要给出候选选项而不是自己决定。"""
    result = registry.get_runner("submit_edit_plan")(
        intent="SUGGEST_VISUALIZATION",
        target={"regionId": "region_total_energy"},
        operation={},
        options=[{"value": "BAR", "label": "柱状图"}],
    )
    assert result["accepted"] is True
    payload = peek_submission()["payload"]
    assert payload["options"][0]["value"] == "BAR"


def test_submit_edit_answer_only_accepts_answer_fields():
    runner = registry.get_runner("submit_edit_answer")
    result = runner(
        reply="当前数据源支持小时粒度。",
        facts=["check_capability 返回 HOUR"],
        limitations=["当前预览是 Mock"],
        suggestions=["可以申请改为小时趋势"],
    )
    assert result["accepted"] is True
    submission = peek_submission()
    assert submission["tool"] == "submit_edit_answer"
    assert submission["payload"]["facts"] == ["check_capability 返回 HOUR"]
    assert "operation" not in submission["payload"]
    assert "target" not in submission["payload"]
    assert "operations" not in registry.get_runner("submit_edit_answer").__code__.co_varnames


def test_submit_edit_answer_rejects_blank_reply():
    result = registry.get_runner("submit_edit_answer")(reply=" ")
    assert result["accepted"] is False
    assert peek_submission() is None


def test_submit_rejects_invalid_payload():
    """非法载荷不能流到确定性层。"""
    result = registry.get_runner("submit_draft_plan")(
        reportName="A 机房能耗日报",
        roomId="room_a",
        includeTrend="不是布尔值",
    )
    assert result["accepted"] is False
    assert result["error"]
    assert peek_submission() is None


def test_first_submission_wins():
    """模型重复提交时保留首个结论，避免覆盖已定结论。"""
    registry.get_runner("submit_draft_plan")(
        reportName="第一个", roomId="room_a"
    )
    registry.get_runner("submit_draft_plan")(
        reportName="第二个", roomId="room_a"
    )
    assert peek_submission()["payload"]["reportName"] == "第一个"


def test_take_submission_clears():
    registry.get_runner("submit_blocked")(
        message="该数据源不支持分钟粒度",
        alternatives=["改为小时粒度"],
    )
    taken = take_submission()
    assert taken["tool"] == "submit_blocked"
    assert peek_submission() is None


def test_child_task_submission_is_visible_to_parent():
    async def run():
        async def submit():
            return registry.get_runner("submit_edit_plan")(
                intent="RENAME_REPORT", operation={"newTitle": "新报表"}
            )

        result = await asyncio.create_task(submit())
        assert result["accepted"] is True
        assert take_submission()["payload"]["operation"]["newTitle"] == "新报表"
        assert peek_submission() is None

    asyncio.run(run())


# ---------- AgentScope Toolkit 装配 ----------

def _run(coro):
    """同步跑协程（项目未装 pytest-asyncio）。"""
    return asyncio.run(coro)


async def _drain(gen):
    chunks = []
    async for chunk in gen:
        chunks.append(chunk)
    return chunks


def test_toolkit_registers_all_tools():
    async def scenario():
        toolkit = await registry.to_agentscope_toolkit_async()
        for tool in registry.list_tools():
            registered = await toolkit.get_tool(tool.NAME)
            assert registered is not None, f"{tool.NAME} 没注册进 Toolkit"

    _run(scenario())


def test_toolkit_preserves_read_only_semantics():
    """终结工具的 is_read_only 必须是 False，权限语义不能一刀切。"""

    async def scenario():
        toolkit = await registry.to_agentscope_toolkit_async()
        fact = await toolkit.get_tool("search_catalog")
        terminal = await toolkit.get_tool("submit_blocked")
        assert fact.is_read_only is True
        assert terminal.is_read_only is False

    _run(scenario())


def test_toolkit_tool_call_works_end_to_end():
    """走 AgentScope 真实调用路径：ToolCallBlock → ToolChunk。"""
    import json

    from agentscope.message import ToolCallBlock
    from agentscope.state import AgentState as ScopeAgentState

    async def scenario():
        # 查目录必须先有授权范围，否则工具直接拒绝（PermissionError）
        _bind_scope()
        toolkit = await registry.to_agentscope_toolkit_async()
        state = ScopeAgentState()
        call = ToolCallBlock(
            type="tool_call",
            id="call_1",
            name="search_catalog",
            input=json.dumps({"subjectRef": "machine_room"}),
        )
        chunks = await _drain(toolkit.call_tool(call, state))
        payload = json.loads(chunks[0].content[0].text)
        assert payload["total"] > 0

    _run(scenario())


def test_toolkit_only_registers_tools_of_current_state():
    """节点级白名单要真正落到 Toolkit，模型看不到不该调的工具。"""

    async def scenario():
        toolkit = await registry.to_agentscope_toolkit_async(
            registry.tools_for_state(AgentState.ANALYZING)
        )
        assert await toolkit.get_tool("submit_draft_plan") is not None
        # 编辑态才有提交编辑计划的工具
        assert await toolkit.get_tool("submit_edit_plan") is None
        assert await toolkit.get_tool("submit_edit_answer") is None

        editing_toolkit = await registry.to_agentscope_toolkit_async(
            registry.tools_for_state(AgentState.USER_EDITING_TEMPLATE)
        )
        assert await editing_toolkit.get_tool("submit_edit_plan") is not None
        assert await editing_toolkit.get_tool("submit_edit_answer") is not None
        assert await editing_toolkit.get_tool("submit_draft_plan") is None

    _run(scenario())


def test_toolkit_terminal_tool_writes_submission():
    import json

    from agentscope.message import ToolCallBlock
    from agentscope.state import AgentState as ScopeAgentState

    async def scenario():
        toolkit = await registry.to_agentscope_toolkit_async()
        state = ScopeAgentState()
        call = ToolCallBlock(
            type="tool_call",
            id="call_2",
            name="submit_blocked",
            input=json.dumps(
                {"message": "不支持分钟粒度", "alternatives": ["改为小时粒度"]}
            ),
        )
        await _drain(toolkit.call_tool(call, state))
        submission = peek_submission()
        assert submission["tool"] == "submit_blocked"
        assert submission["payload"]["alternatives"] == ["改为小时粒度"]

    _run(scenario())


def test_fact_tool_failure_is_visible_to_agent(monkeypatch):
    import json

    from agentscope.message import ToolCallBlock
    from agentscope.state import AgentState as ScopeAgentState
    from app.tools import get_current_draft_tool

    def fail():
        raise RuntimeError("临时查证失败")

    monkeypatch.setattr(get_current_draft_tool, "run", fail)

    async def scenario():
        bind_lookup_failure()
        try:
            toolkit = await registry.to_agentscope_toolkit_async([get_current_draft_tool])
            call = ToolCallBlock(type="tool_call", id="call_fact_error",
                                 name="get_current_draft", input=json.dumps({}))
            chunks = await _drain(toolkit.call_tool(call, ScopeAgentState()))
            assert "error" in json.loads(chunks[0].content[0].text)
            assert take_lookup_failure() is True
        finally:
            clear_lookup_failure()

    _run(scenario())
