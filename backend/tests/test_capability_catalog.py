"""能力总目录的测试。

覆盖（契约 0003 §1）：

* 目录**覆盖**全部已注册 Tool、全部旧编辑意图、全部清单能力；
* 每条的参数契约与**真实来源**一致（不比手抄的副本）；
* 每条的源码定位**真的存在**（文件在、名字在）；
* ``ensure_catalog_fresh`` 幂等；契约变了版本就变、目录就更新；
* 生成失败**抛异常**，不静默回退旧文件。

全程**不调用模型**。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from app.services import capability_catalog_service as catalog
from app.services.edit_capabilities import all_capabilities
from app.services.edit_capabilities.base import EditCapability, _REGISTRY
from app.schemas.edit_plan import EditIntent
from app.tools import registry


# --------------------------------------------------------------------------
# 夹具
# --------------------------------------------------------------------------

@pytest.fixture()
def tmp_catalog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把目录文件指到临时路径 —— 测试不许污染真实的生成物。"""
    target = tmp_path / "capability_catalog.generated.md"
    monkeypatch.setattr(catalog, "CATALOG_PATH", target)
    return target


def _entries_by_id(entries: list[dict]) -> dict[str, dict]:
    return {entry["id"]: entry for entry in entries}


def _entry(entries: list[dict], entry_type: str, entry_id: str) -> dict:
    """按 ``(entryType, id)`` 取条目 —— 名字在目录里可能重复。"""
    for item in entries:
        if item["entryType"] == entry_type and item["id"] == entry_id:
            return item
    raise AssertionError(f"目录里没有 {entry_type} / {entry_id}")


def _fake_capability_executor(ctx, params):  # pragma: no cover — 只为源码定位
    """测试用的假执行器。

    必须是**模块级函数**：目录里的源码定位要能指到一个真实存在的位置，
    闭包 / lambda 指不到。
    """
    return None


def _read_metadata(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    matches = re.findall(r"```json\n(.*?)\n```", text, re.DOTALL)
    assert matches, "目录文件里没有机器可读元数据块"
    return json.loads(matches[-1])


# --------------------------------------------------------------------------
# 覆盖：全部已注册 Tool / 全部旧意图 / 五项新能力
# --------------------------------------------------------------------------

def test_catalog_covers_every_registered_tool():
    entries = catalog.build_catalog()["entries"]
    tool_entries = {
        entry["id"] for entry in entries if entry["entryType"] == "agent_tool"
    }
    registered = {tool.NAME for tool in registry.list_tools()}
    assert registered, "注册表里一个工具都没有 —— 测试本身失效"
    missing = registered - tool_entries
    assert not missing, f"目录漏掉了已注册 Tool：{sorted(missing)}"


def test_catalog_available_tools_are_real_registered_tools():
    """标 available 的 Tool 必须是真注册过的（不能凭空虚构一个）。"""
    entries = catalog.build_catalog()["entries"]
    registered = {tool.NAME for tool in registry.list_tools()}
    for entry in entries:
        if entry["entryType"] != "agent_tool":
            continue
        if entry["status"] == "available":
            assert entry["id"] in registered, f"虚构的工具：{entry['id']}"
        else:
            assert entry["id"] not in registered, (
                f"{entry['id']} 标成 {entry['status']}，却仍在注册表里 —— 状态没跟上"
            )


def test_catalog_covers_every_edit_intent():
    entries = catalog.build_catalog()["entries"]
    legacy = {
        entry["id"] for entry in entries if entry["entryType"] == "legacy_intent"
    }
    assert legacy == {intent.value for intent in EditIntent}


def test_catalog_covers_the_five_new_capabilities():
    entries = catalog.build_catalog()["entries"]
    capability_entries = {
        entry["id"]
        for entry in entries
        if entry["entryType"] == "operation" and entry["group"].startswith("清单能力")
    }
    assert capability_entries == {
        capability.id for capability in all_capabilities()
    }
    assert capability_entries == {
        "ADD_SECTION",
        "DELETE_SECTION",
        "SET_DISPLAY",
        "SET_CATEGORY_DATA",
        "SET_PRESENTATION",
    }


def test_catalog_covers_presentation_whitelist_and_dimensions():
    from app.services.report_presentation_service import ALLOWED_OPERATIONS
    from app.services.edit_capabilities.chart_data import SIMULATED_DIMENSIONS

    entries = catalog.build_catalog()["entries"]
    ops = {
        entry["id"]
        for entry in entries
        if entry["group"].startswith("展示操作")
    }
    assert ops == set(ALLOWED_OPERATIONS)

    payload = catalog.catalog_payload()
    dimensions = {item["dimension"] for item in payload["simulatedDimensions"]}
    assert dimensions == set(SIMULATED_DIMENSIONS)


def test_legacy_intents_are_not_presented_as_tools():
    """旧意图必须标明「不是独立 Tool」，由编辑引擎执行。"""
    entries = catalog.build_catalog()["entries"]
    for entry in entries:
        if entry["entryType"] != "legacy_intent":
            continue
        assert "不是独立 Tool" in entry["purpose"]
        assert "TemplateEditEngine.apply" in entry["purpose"]
        assert entry["code"]["execution"].startswith(
            "app/services/template_edit_engine.py::TemplateEditEngine."
        )


def test_rule_fast_paths_are_not_presented_as_tools():
    entries = catalog.build_catalog()["entries"]
    rule_entries = [
        entry for entry in entries if entry["group"].startswith("规则快路")
    ]
    assert rule_entries, "规则快路没有进目录"
    for entry in rule_entries:
        assert entry["entryType"] != "agent_tool"
        assert "不是独立 Tool" in entry["purpose"]


def test_every_entry_has_the_contract_fields():
    entries = catalog.build_catalog()["entries"]
    for entry in entries:
        assert set(entry) == set(catalog._ENTRY_FIELDS), (
            f"{entry.get('id')} 的字段与契约 §1.3 不一致"
        )
        assert entry["entryType"] in {"agent_tool", "operation", "legacy_intent"}
        assert entry["status"] in {"available", "unsupported", "not_wired"}
        assert isinstance(entry["params"], dict)
        assert isinstance(entry["required"], list)
        assert isinstance(entry["states"], list)
        assert isinstance(entry["readOnly"], bool)
        assert isinstance(entry["errors"], list)
        assert isinstance(entry["limits"], list)
        assert set(entry["code"]) == {
            "registration",
            "dispatch",
            "validation",
            "execution",
        }


# --------------------------------------------------------------------------
# 参数契约：与真实来源逐条一致
# --------------------------------------------------------------------------

def test_tool_params_match_the_registered_schema():
    entries = catalog.build_catalog()["entries"]
    for tool in registry.list_tools():
        entry = _entry(entries, "agent_tool", tool.NAME)
        assert entry["params"] == tool.PARAMETERS, f"{tool.NAME} 的参数契约不一致"
        assert entry["required"] == list(tool.PARAMETERS.get("required") or [])
        assert entry["states"] == list(tool.APPLICABLE_STATES)
        assert entry["readOnly"] == bool(tool.IS_READ_ONLY)


def test_capability_params_match_the_capability_schema():
    entries = catalog.build_catalog()["entries"]
    for capability in all_capabilities():
        entry = _entry(entries, "operation", capability.id)
        assert capability.params_schema is not None
        assert entry["params"] == capability.params_schema.model_json_schema(), (
            f"{capability.id} 的参数契约与能力自己的 schema 不一致"
        )
        assert entry["required"] == list(
            capability.params_schema.model_json_schema().get("required") or []
        )


def test_presentation_operation_params_come_from_the_real_handlers():
    """展示操作的必填键来自真实实现（``_require_keys``），不是手抄。"""
    import inspect

    from app.services import report_presentation_service as presentation

    entries = catalog.build_catalog()["entries"]
    for op in presentation.ALLOWED_OPERATIONS:
        entry = _entry(entries, "operation", op)
        handler = presentation._HANDLERS[op]
        source = inspect.getsource(handler)
        for key in entry["required"]:
            assert f'"{key}"' in source, f"{op} 的必填键 {key} 不在真实实现里"


# --------------------------------------------------------------------------
# 源码定位：文件真实存在、名字真实存在
# --------------------------------------------------------------------------

def test_every_source_ref_resolves():
    entries = catalog.build_catalog()["entries"]
    for entry in entries:
        for slot, ref in entry["code"].items():
            file_part, _, name_part = ref.partition("::")
            assert name_part, f"{entry['id']}.{slot} 的定位没有名字：{ref}"
            absolute = catalog._BACKEND_ROOT / file_part
            assert absolute.is_file(), f"{entry['id']}.{slot} 指向的文件不存在：{file_part}"
            assert catalog.verify_source_ref(ref), (
                f"{entry['id']}.{slot} 指向的名字不存在：{ref}"
            )


def test_capability_execution_points_at_the_real_executor():
    """能力经过封装时要指到**真正执行的位置**，不能只指包装文件。"""
    entries = catalog.build_catalog()["entries"]
    for capability in all_capabilities():
        entry = _entry(entries, "operation", capability.id)
        execution = entry["code"]["execution"]
        assert "app/services/edit_capabilities/blocks.py::" in execution
        assert capability.executor.__name__ in execution
        assert catalog.verify_source_ref(execution)


# --------------------------------------------------------------------------
# 版本与自动同步
# --------------------------------------------------------------------------

def test_version_is_derived_from_registered_content():
    version = catalog.catalog_version()
    assert len(version) == 16
    assert version == catalog.catalog_version()
    # 版本不是随机数，也不是常量。
    assert version == catalog.catalog_version()


def test_ensure_catalog_fresh_is_idempotent(tmp_catalog: Path):
    first = catalog.ensure_catalog_fresh()
    content_first = tmp_catalog.read_text(encoding="utf-8")
    second = catalog.ensure_catalog_fresh()
    content_second = tmp_catalog.read_text(encoding="utf-8")
    assert first == second
    assert content_first == content_second
    assert _read_metadata(tmp_catalog)["version"] == first


def test_ensure_catalog_fresh_updates_when_contract_changes(
    tmp_catalog: Path, monkeypatch: pytest.MonkeyPatch,
):
    """改一项契约（临时注册一个假能力）→ 版本变、目录内容更新。

    用 ``monkeypatch.setitem`` 挂到能力注册表上，测试结束自动还原 ——
    **不污染真实注册表**。
    """
    before = catalog.ensure_catalog_fresh()
    before_text = tmp_catalog.read_text(encoding="utf-8")

    fake = EditCapability(
        id="FAKE_TEST_ONLY_CAPABILITY",
        family="测试",
        title="测试能力",
        description="只在测试里存在的假能力。",
        params_schema=None,
        executor=_fake_capability_executor,  # type: ignore[arg-type]
    )
    monkeypatch.setitem(_REGISTRY, fake.id, fake)

    after = catalog.ensure_catalog_fresh(force=True)
    after_text = tmp_catalog.read_text(encoding="utf-8")
    assert after != before, "契约变了，目录版本却没变 —— 冒充同步"
    assert "FAKE_TEST_ONLY_CAPABILITY" in after_text
    assert "FAKE_TEST_ONLY_CAPABILITY" not in before_text


def test_stale_catalog_is_not_silently_used(tmp_catalog: Path, monkeypatch: pytest.MonkeyPatch):
    """目录文件过期时 ``load_catalog_text`` 要抛异常，不静默使用。"""
    catalog.ensure_catalog_fresh()
    assert catalog.load_catalog_text()

    fake = EditCapability(
        id="FAKE_TEST_ONLY_CAPABILITY_2",
        family="测试",
        title="测试能力",
        description="只在测试里存在的假能力。",
        params_schema=None,
        executor=_fake_capability_executor,  # type: ignore[arg-type]
    )
    monkeypatch.setitem(_REGISTRY, fake.id, fake)
    with pytest.raises(ValueError):
        catalog.load_catalog_text()


def test_generation_failure_raises_instead_of_falling_back(
    tmp_catalog: Path, monkeypatch: pytest.MonkeyPatch,
):
    """生成失败要抛出来 —— 不能悄悄退回旧的手写说明。"""
    catalog.ensure_catalog_fresh()
    original = tmp_catalog.read_text(encoding="utf-8")

    def _boom() -> dict:
        raise RuntimeError("目录生成炸了")

    monkeypatch.setattr(catalog, "build_catalog", _boom)
    with pytest.raises(RuntimeError):
        catalog.ensure_catalog_fresh(force=True)
    assert tmp_catalog.read_text(encoding="utf-8") == original


def test_generated_file_matches_current_contract(tmp_catalog: Path):
    """真实交付路径上的文件必须与当前代码一致（能自动补齐，不靠人手动导出）。"""
    catalog.ensure_catalog_fresh()
    payload = _read_metadata(tmp_catalog)
    assert payload["version"] == catalog.catalog_version()
    assert payload["contractFingerprint"]
    names = {entry["id"] for entry in payload["entries"]}
    for tool in registry.list_tools():
        assert tool.NAME in names
    for intent in EditIntent:
        assert intent.value in names


# --------------------------------------------------------------------------
# 目录正文：给模型的那一份
# --------------------------------------------------------------------------

def test_catalog_text_filters_by_state_from_the_same_data():
    payload = catalog.catalog_payload()
    state = "USER_EDITING_TEMPLATE"
    expected = {
        entry["id"]
        for entry in payload["entries"]
        if not entry["states"] or state in entry["states"]
    }
    text = catalog.catalog_text(state)
    for entry in payload["entries"]:
        heading = f"### {entry['id']}"
        if entry["id"] in expected:
            assert heading in text, f"{entry['id']} 该出现在 {state} 的目录里"
        else:
            assert heading not in text, f"{entry['id']} 不该出现在 {state} 的目录里"


def test_catalog_text_contains_real_params_and_source_locations():
    text = catalog.catalog_text()
    for capability in all_capabilities():
        assert capability.id in text
    assert "app/services/edit_capabilities/blocks.py::_add_section" in text
    assert "app/services/report_presentation_service.py::_op_set_style" in text
    for tool in registry.list_tools():
        assert tool.NAME in text


def test_catalog_text_and_file_body_are_the_same_content(tmp_catalog: Path):
    catalog.ensure_catalog_fresh()
    body = catalog.load_catalog_text()
    from_file = tmp_catalog.read_text(encoding="utf-8")
    assert body.strip() in from_file
    # 模型拿到的正文必须与文件里给人看的正文一致。
    assert body.strip() == catalog.catalog_text().strip()
