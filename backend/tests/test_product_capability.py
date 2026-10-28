"""实际产品能力事实模块的专项测试（工单 04 / GitHub #16）。

这些断言只针对**对外行为**：能力事实的结果形状与语义边界。
不依赖具体措辞，也不依赖模型是否调用了本工具。
"""
from __future__ import annotations

import asyncio
import copy

import pytest

from app.schemas.agent import AgentState
from app.services import product_capability_service as capability
from app.tools import check_product_capability_tool as capability_tool
from app.tools import registry
from app.tools.base import ToolCategory
from app.tools.context import (
    ToolContext,
    bind_tool_context,
    clear_tool_context,
)
from app.tools.registry import (
    bind_lookup_failure,
    clear_lookup_failure,
    take_lookup_failure,
)


AUTHORIZED_PROJECT = "project_001"
UNAUTHORIZED_PROJECT = "project_999"
AUTHORIZED_ROOM = "room_a"

DEFAULT_SOURCE = "catalog.machine_room.energy"
EAS_SOURCE = "eas_energy_detail"
DAILY_SUMMARY_SOURCE = "catalog.machine_room.energy_daily_summary"

TOTAL_ENERGY_METRIC = "metric.machine_room.total_energy"
DEVICE_ENERGY_METRIC = "metric.machine_room.device_energy"
COST_METRIC = "metric.machine_room.energy_cost"
PUE_METRIC = "metric.machine_room.pue"


@pytest.fixture(autouse=True)
def _clean_context():
    clear_tool_context()
    yield
    clear_tool_context()


def _describe(**kwargs):
    """默认在授权范围内查能力；用例可覆盖任一参数。"""
    params = {
        "project_id": AUTHORIZED_PROJECT,
        "subject_ref": "machine_room",
        "subject_id": AUTHORIZED_ROOM,
    }
    params.update(kwargs)
    return capability.describe_capability(**params)


def _bind_scope(*, project_id: str = AUTHORIZED_PROJECT, subject_id: str = AUTHORIZED_ROOM):
    bind_tool_context(
        ToolContext(
            state=AgentState.ANALYZING,
            subject_ref="machine_room",
            subject_id=subject_id,
            project_id=project_id,
        )
    )


# ---------- 目录登记 ≠ 产品执行支持 ----------

def test_default_source_does_not_support_minute_grain():
    """默认机房能耗源只登记到小时和天，分钟粒度不能承诺。"""
    result = _describe(source_ref=DEFAULT_SOURCE, grain_unit="MINUTE")

    assert result["outcome"] == "REGISTERED_NOT_SUPPORTED"
    assert result["catalog"]["registered"] is True
    assert result["catalog"]["supportedGrains"] == ["HOUR", "DAY"]
    assert result["execution"]["grain"]["supported"] is False
    assert result["execution"]["grain"]["reason"]
    assert any("HOUR" in item for item in result["alternatives"])


def test_eas_detail_source_supports_minute_but_preview_stays_simulated():
    """EAS 明细源支持分钟粒度，但预览用的是模拟业务数据，不是真实 EAS 取数。"""
    result = _describe(source_ref=EAS_SOURCE, grain_unit="MINUTE")

    assert result["outcome"] == "OK"
    assert result["catalog"]["registered"] is True
    assert "MINUTE" in result["catalog"]["supportedGrains"]
    assert result["execution"]["grain"]["supported"] is True
    assert result["dataSource"]["mode"] == "SIMULATED"
    assert result["dataSource"]["productionConnected"] is False


def test_cost_metric_is_registered_but_generation_cannot_render_it():
    """成本在指标目录里登记了，但当前生成模板出不了这个数。"""
    result = _describe(metric_ref=COST_METRIC)

    assert result["outcome"] == "REGISTERED_NOT_SUPPORTED"
    assert result["metric"]["registered"] is True
    assert result["metric"]["label"] == "能耗成本"
    assert result["execution"]["generation"]["supported"] is False
    assert result["execution"]["generation"]["reason"]
    # 替代必须有查证依据：来自生成链路自己的报错，不是随口编的
    assert any(TOTAL_ENERGY_METRIC in item for item in result["alternatives"])
    assert any(DEVICE_ENERGY_METRIC in item for item in result["alternatives"])


def test_pue_metric_is_registered_but_generation_cannot_render_it():
    result = _describe(metric_ref=PUE_METRIC)

    assert result["outcome"] == "REGISTERED_NOT_SUPPORTED"
    assert result["metric"]["registered"] is True
    assert result["metric"]["label"] == "PUE"
    assert result["execution"]["generation"]["supported"] is False


def test_supported_metric_is_reported_as_generation_supported():
    result = _describe(metric_ref=TOTAL_ENERGY_METRIC)

    assert result["outcome"] == "OK"
    assert result["metric"]["registered"] is True
    assert result["metric"]["unit"] == "kWh"
    assert result["execution"]["generation"]["supported"] is True
    assert result["execution"]["preview"]["supported"] is True


# ---------- 确实没有 / 无权限，不能混为一谈 ----------

def test_unregistered_metric_is_reported_as_not_registered():
    result = _describe(metric_ref="metric.machine_room.temperature")

    assert result["outcome"] == "NOT_REGISTERED"
    assert result["metric"]["registered"] is False


def test_unknown_source_is_reported_as_not_registered():
    result = _describe(source_ref="catalog.unknown.source")

    assert result["outcome"] == "NOT_REGISTERED"
    assert result["catalog"]["registered"] is False
    assert result["alternatives"]


def test_unauthorized_source_does_not_leak_capability_details():
    """越权时不能返回该数据源的粒度、字段或描述。"""
    result = _describe(
        source_ref=EAS_SOURCE,
        project_id=UNAUTHORIZED_PROJECT,
    )

    assert result["outcome"] == "NOT_AUTHORIZED"
    assert result["catalog"]["registered"] is False
    assert result["catalog"]["supportedGrains"] == []
    assert result["catalog"]["fields"] == []
    assert result["catalog"]["description"] is None


def test_unauthorized_metric_is_reported_as_not_authorized():
    result = _describe(
        metric_ref=COST_METRIC,
        project_id=UNAUTHORIZED_PROJECT,
    )

    assert result["outcome"] == "NOT_AUTHORIZED"
    assert result["metric"]["registered"] is False
    assert result["metric"]["label"] is None


# ---------- 区块与字段限制 ----------

def test_daily_summary_source_has_no_device_dimension():
    """日汇总源没有设备维度，设备排名出不来；时间字段也不是 timestamp。"""
    result = _describe(source_ref=DAILY_SUMMARY_SOURCE)

    assert result["catalog"]["hasDeviceDimension"] is False
    assert result["catalog"]["timeField"] == "logicalDate"
    assert result["execution"]["components"]["ranking"] is False
    assert result["execution"]["componentReasons"]["ranking"]
    assert result["execution"]["components"]["total"] is True


def test_supported_grain_does_not_imply_every_component_works():
    """粒度支持不等于排名、趋势都能生成。"""
    result = _describe(source_ref=EAS_SOURCE, grain_unit="MINUTE")

    assert result["execution"]["grain"]["supported"] is True
    assert result["execution"]["components"]["trend"] is True
    assert result["execution"]["components"]["ranking"] is True
    assert result["execution"]["components"].keys() >= {"total", "ranking", "trend"}


# ---------- 缺上下文 ----------

def test_missing_context_reports_product_range_and_asks_clarification():
    """既没给数据源也没给指标时，给出确定范围并提出必要澄清。"""
    result = _describe()

    assert result["outcome"] == "MISSING_CONTEXT"
    assert result["clarification"]["question"]
    assert "sourceRef" in result["clarification"]["missing"]

    grain_support = result["scopeSummary"]["grainSupport"]
    assert EAS_SOURCE in grain_support["MINUTE"]
    assert DEFAULT_SOURCE not in grain_support["MINUTE"]

    metrics = {
        item["metricRef"]: item for item in result["scopeSummary"]["metrics"]
    }
    assert metrics[COST_METRIC]["generationSupported"] is False
    assert metrics[TOTAL_ENERGY_METRIC]["generationSupported"] is True


# ---------- 工具契约与只读边界 ----------

def test_tool_declares_read_only_fact_contract():
    assert capability_tool.NAME == "check_product_capability"
    assert capability_tool.CATEGORY == ToolCategory.FACT
    assert capability_tool.IS_READ_ONLY is True
    assert capability_tool.APPLICABLE_STATES
    assert callable(capability_tool.run)


def test_tool_requires_bound_scope():
    """没绑定本轮上下文就查不了能力。"""
    with pytest.raises(PermissionError):
        capability_tool.run(sourceRef=EAS_SOURCE)


def test_tool_returns_same_outcome_vocabulary_as_service():
    _bind_scope()
    result = capability_tool.run(sourceRef=EAS_SOURCE, grainUnit="MINUTE")

    assert result["outcome"] == "OK"
    assert result["execution"]["grain"]["supported"] is True
    assert result["dataSource"]["productionConnected"] is False


def test_tool_failure_is_not_reported_as_unsupported(monkeypatch):
    """工具失败必须抛错，不能降级成「不支持」或「数据不存在」。"""
    _bind_scope()

    def _boom(*_args, **_kwargs):
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr(capability.catalog_mock_service, "get_catalog", _boom)

    with pytest.raises(RuntimeError):
        capability_tool.run(sourceRef=EAS_SOURCE)


def test_capability_query_does_not_change_draft_or_session():
    """能力查询是只读的：草稿内容和版本都不能动。"""
    definition = {
        "metadata": {"name": "A 机房能耗日报", "revision": 3},
        "regions": [{"id": "region_total_energy", "role": "total"}],
    }
    before = copy.deepcopy(definition)
    bind_tool_context(
        ToolContext(
            state=AgentState.ANALYZING,
            subject_ref="machine_room",
            subject_id=AUTHORIZED_ROOM,
            project_id=AUTHORIZED_PROJECT,
            definition=definition,
            base_definition_revision=3,
        )
    )

    result = capability_tool.run(sourceRef=EAS_SOURCE, grainUnit="MINUTE")

    assert definition == before
    assert result["dataSource"]["mode"] == "SIMULATED"
    # 结果里不能夹带草稿、预览或待确认计划
    assert "definition" not in result
    assert "reportDefinitionDraft" not in result
    assert "pendingChoice" not in result
    assert "preview" not in result


# ---------- 与共用链路的契约 ----------

def test_outcome_vocabulary_is_stable():
    """outcome 五态是给路由层用的契约，含义不能漂。"""
    assert capability.OUTCOME_OK == "OK"
    assert capability.OUTCOME_REGISTERED_NOT_SUPPORTED == "REGISTERED_NOT_SUPPORTED"
    assert capability.OUTCOME_NOT_REGISTERED == "NOT_REGISTERED"
    assert capability.OUTCOME_NOT_AUTHORIZED == "NOT_AUTHORIZED"
    assert capability.OUTCOME_MISSING_CONTEXT == "MISSING_CONTEXT"


def test_tool_satisfies_registry_tool_contract():
    """注册进统一工具注册表时必须能通过契约校验。"""
    from app.tools.registry import conclusion_states

    assert capability_tool.PARAMETERS.get("type") == "object"
    assert conclusion_states(capability_tool) == ()
    for state in capability_tool.APPLICABLE_STATES:
        assert state in {item.value for item in AgentState}


def test_edit_chain_does_not_claim_more_than_generation():
    """目录登记不等于能改：编辑链路的可改范围不能宽于生成链路。"""
    result = _describe(metric_ref=COST_METRIC)

    assert result["execution"]["generation"]["supported"] is False
    assert result["execution"]["edit"]["supported"] is False
    assert result["execution"]["preview"]["supported"] is False


def test_unknown_source_does_not_offer_unauthorized_alternatives():
    """无权限时不能把看不到的数据源推荐出去。"""
    result = _describe(
        source_ref="catalog.unknown.source",
        project_id=UNAUTHORIZED_PROJECT,
    )

    assert result["outcome"] == "NOT_REGISTERED"
    assert result["alternatives"] == []


def test_missing_context_lists_only_authorized_sources():
    _describe()  # 授权范围内的范围
    result = _describe(project_id=UNAUTHORIZED_PROJECT)

    assert result["outcome"] == "MISSING_CONTEXT"
    assert result["scopeSummary"]["sources"] == []
    assert result["scopeSummary"]["metrics"] == []


# ---------- 走 AgentScope 真实调用路径 ----------

def _run(coro):
    """同步跑协程（项目未装 pytest-asyncio）。"""
    return asyncio.run(coro)


async def _drain(gen):
    chunks = []
    async for chunk in gen:
        chunks.append(chunk)
    return chunks


def test_toolkit_call_returns_capability_facts():
    """模型通过 AgentScope 真实调用本工具时，拿到的是同一套事实。"""
    import json

    from agentscope.message import ToolCallBlock
    from agentscope.state import AgentState as ScopeAgentState

    async def scenario():
        _bind_scope()
        toolkit = await registry.to_agentscope_toolkit_async([capability_tool])
        call = ToolCallBlock(
            type="tool_call",
            id="call_capability",
            name="check_product_capability",
            input=json.dumps(
                {"sourceRef": EAS_SOURCE, "grainUnit": "MINUTE"}
            ),
        )
        chunks = await _drain(toolkit.call_tool(call, ScopeAgentState()))
        payload = json.loads(chunks[0].content[0].text)

        assert payload["outcome"] == "OK"
        assert payload["execution"]["grain"]["supported"] is True
        assert payload["dataSource"]["productionConnected"] is False

    _run(scenario())


def test_toolkit_call_reports_failure_without_claiming_unsupported(monkeypatch):
    """工具失败必须让 Agent 看得见，不能降级成「不支持」。"""
    import json

    from agentscope.message import ToolCallBlock
    from agentscope.state import AgentState as ScopeAgentState

    def fail(*_args, **_kwargs):
        raise RuntimeError("临时查证失败")

    monkeypatch.setattr(capability.catalog_mock_service, "get_catalog", fail)

    async def scenario():
        _bind_scope()
        bind_lookup_failure()
        try:
            toolkit = await registry.to_agentscope_toolkit_async([capability_tool])
            call = ToolCallBlock(
                type="tool_call",
                id="call_capability_error",
                name="check_product_capability",
                input=json.dumps({"sourceRef": EAS_SOURCE}),
            )
            chunks = await _drain(toolkit.call_tool(call, ScopeAgentState()))
            payload = json.loads(chunks[0].content[0].text)

            assert "error" in payload
            assert "outcome" not in payload
            assert take_lookup_failure() is True
        finally:
            clear_lookup_failure()

    _run(scenario())

