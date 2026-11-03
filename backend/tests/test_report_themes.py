"""共享主题专项测试（P4，契约 0002 §8）。

纪律：
    - 纯逻辑用例**不碰数据库**；
    - 落库用例只用 `tmp_path` 里的**临时 SQLite**，**绝不碰**用户库
      （`backend/data/haewu.db`）。
"""
from __future__ import annotations

import copy

import pytest

from app.config import get_settings
from app.schemas.report_definition import ReportDefinitionDraft
from app.services.report_theme_service import (
    DEFAULT_THEME_ID,
    InMemoryThemeStorage,
    THEME_MANAGED_STYLE_KEYS,
    ThemeConflictError,
    ThemeModule,
    ThemeValidationError,
    theme_service,
)
from app.storage.draft_workspace_sqlite import DraftWorkspaceSqlite
from app.storage.sqlite_store import SqliteSessionRepository


# ---------- 夹具 ----------

@pytest.fixture()
def module() -> ThemeModule:
    """独立的主题模块（内存存储），用例之间互不影响。"""
    return ThemeModule(storage=InMemoryThemeStorage())


def _definition() -> dict:
    """一份带容器 + 叶子、含旧外观覆盖的最小定义。"""
    return {
        "schemaVersion": "1.0",
        "metadata": {"name": "机房能耗日报", "revision": 3},
        "page": {"arrangement": {"mode": "GRID", "columns": 12, "gap": {"value": 12, "unit": "PX"}}},
        "theme": {"themeRef": "theme_default", "version": "1.0"},
        "regions": [
            {
                "id": "group_1",
                "name": "对比组",
                "role": "DETAIL",
                "layout": {"placement": {"type": "GRID", "column": 1, "columnSpan": 12, "row": 2}},
                "component": {"type": "CONTAINER", "arrangement": {"mode": "GRID", "columns": 2, "direction": "HORIZONTAL"}},
                "style": {"textColor": "#111111", "keepMe": "ancestor-only"},
                "children": [
                    {
                        "id": "leaf_1",
                        "name": "总耗电",
                        "role": "SUMMARY",
                        "layout": {"placement": {"type": "GRID", "column": 1, "columnSpan": 6, "row": 1}},
                        "data": {"inputs": {"main": {"type": "DATASET", "datasetRef": "ds_1"}}},
                        "binding": {"metricRefs": [{"ref": "metric_total", "version": "1.0"}]},
                        "format": {"kind": "NUMBER", "precision": 2},
                        "style": {"textColor": "#FF0000", "borderRadius": 12, "keepMe": "region-only"},
                        "component": {
                            "type": "KPI",
                            "metric": {"format": {"kind": "PERCENT", "precision": 1}},
                            "columns": [{"id": "c1", "label": "值", "fieldRef": "energyKwh", "format": {"kind": "CURRENCY"}}],
                        },
                    }
                ],
            },
            {
                "id": "leaf_2",
                "name": "趋势",
                "layout": {"placement": {"type": "GRID", "column": 1, "columnSpan": 12, "row": 3}},
                "data": {"inputs": {"main": {"type": "DATASET", "datasetRef": "ds_1"}}},
                "component": {"type": "CHART", "chartType": "line"},
            },
        ],
    }


def _leaf(definition: dict, region_id: str) -> dict:
    for region in definition["regions"]:
        if region.get("id") == region_id:
            return region
        for child in region.get("children") or []:
            if child.get("id") == region_id:
                return child
    raise AssertionError(f"区域不存在：{region_id}")


# ---------- 1. 预置主题从资源文件加载 ----------

def test_builtin_themes_loaded_from_resource():
    themes = theme_service.list_themes()
    assert len(themes) >= 4, "预置主题至少 4 个"

    by_id = {item["themeId"]: item for item in themes}
    for required in ("theme_default", "theme_dark", "theme_soft", "theme_print"):
        assert required in by_id, f"缺少预置主题 {required}"

    for item in themes:
        assert set(item) >= {"themeId", "name", "builtin", "themeRevision", "updatedAt"}

    for theme_id in ("theme_default", "theme_dark", "theme_soft", "theme_print"):
        theme = theme_service.get_theme(theme_id)
        assert theme is not None
        assert theme["themeId"] == theme_id
        assert theme["name"]
        assert theme["tokens"], "预置主题必须有 tokens"
        assert theme["defaultStyle"], "预置主题必须有 defaultStyle"
        # tokens 覆盖契约要求的全部 token
        assert {
            "colorBackground", "colorSurface", "colorText", "colorTextMuted",
            "colorPrimary", "colorBorder", "colorAccent", "fontFamily",
            "fontSizeBase", "lineHeightBase", "radiusBase", "gapBase", "spacingUnit",
        } <= set(theme["tokens"])

    # 主题之间风格真的不同
    default_tokens = theme_service.get_theme("theme_default")["tokens"]
    dark_tokens = theme_service.get_theme("theme_dark")["tokens"]
    soft_tokens = theme_service.get_theme("theme_soft")["tokens"]
    print_tokens = theme_service.get_theme("theme_print")["tokens"]
    assert default_tokens["colorBackground"] != dark_tokens["colorBackground"]
    assert default_tokens["radiusBase"] != soft_tokens["radiusBase"]
    assert print_tokens["radiusBase"] == 0
    assert print_tokens["colorText"] == "#000000"


# ---------- 2. 回退默认主题 ----------

def test_resolve_theme_falls_back_to_default():
    # 没有 theme 字段
    resolved = theme_service.resolve_theme({"regions": []})
    assert resolved["themeId"] == DEFAULT_THEME_ID

    # themeRef 指向不存在的主题
    resolved = theme_service.resolve_theme({"theme": {"themeRef": "theme_not_exist"}})
    assert resolved["themeId"] == DEFAULT_THEME_ID
    assert resolved["tokens"]
    assert resolved["defaultStyle"]


# ---------- 3. 从定义读 themeRef ----------

def test_resolve_theme_reads_definition_theme_ref():
    definition = _definition()
    definition["theme"] = {"themeRef": "theme_dark", "version": "1.0"}
    resolved = theme_service.resolve_theme(definition)
    assert resolved["themeId"] == "theme_dark"
    assert resolved["name"] == "深色主题"
    assert resolved["tokens"]["colorBackground"] == "#14171C"
    assert set(resolved) == {"themeId", "themeRevision", "name", "tokens", "defaultStyle"}


# ---------- 4. 应用主题只清主题域覆盖 ----------

def test_apply_theme_clears_managed_style_overrides_only():
    definition = _definition()
    applied = theme_service.apply_theme(definition, "theme_dark")

    leaf_style = _leaf(applied.model_dump(), "leaf_1").get("style", {})
    # 主题域键被清掉
    assert "textColor" not in leaf_style
    assert "borderRadius" not in leaf_style
    # 不在主题域的键保持不动
    assert leaf_style.get("keepMe") == "region-only"
    # 祖先区域的主题域键同样被清
    group_style = _leaf(applied.model_dump(), "group_1").get("style", {})
    assert "textColor" not in group_style
    assert group_style.get("keepMe") == "ancestor-only"

    # 主题引用写进定义
    assert applied.theme["themeRef"] == "theme_dark"


# ---------- 5. 不动布局 / 数据 / FormatSpec ----------

def test_apply_theme_does_not_touch_layout_or_data_or_format():
    definition = _definition()
    applied = theme_service.apply_theme(definition, "theme_print").model_dump()

    for region_id in ("group_1", "leaf_1", "leaf_2"):
        before = _leaf(definition, region_id)
        after = _leaf(applied, region_id)
        assert before.get("layout") == after.get("layout")
        assert before.get("data") == after.get("data")
        assert before.get("binding") == after.get("binding")
        assert before.get("format") == after.get("format")

    before_component = _leaf(definition, "leaf_1")["component"]
    after_component = _leaf(applied, "leaf_1")["component"]
    assert before_component["arrangement" if "arrangement" in before_component else "type"] == (
        after_component["arrangement" if "arrangement" in after_component else "type"]
    )
    assert before_component["metric"] == after_component["metric"]
    assert before_component["columns"] == after_component["columns"]


# ---------- 6. 返回新定义，不改入参 ----------

def test_apply_theme_returns_new_definition_without_mutating_input():
    definition = _definition()
    snapshot = copy.deepcopy(definition)

    applied = theme_service.apply_theme(definition, "theme_dark")

    assert isinstance(applied, ReportDefinitionDraft)
    assert applied is not definition
    assert definition == snapshot, "入参定义不得被修改"
    assert _leaf(definition, "leaf_1")["style"]["textColor"] == "#FF0000"
    assert applied.theme["themeRef"] == "theme_dark"


# ---------- 6b. 展示执行层与主题服务用同一份「主题管理的键」 ----------

def test_presentation_layer_uses_the_same_theme_managed_style_keys():
    """展示执行层（SET_THEME）与主题服务认**同一批**键。

    两边各写一份的话，改了一边忘了另一边 → 主题清了颜色却没清圆角。
    """
    from app.services.report_presentation_service import (
        THEME_MANAGED_STYLE_KEYS as PRESENTATION_KEYS,
    )

    assert set(PRESENTATION_KEYS) == set(THEME_MANAGED_STYLE_KEYS)


def test_presentation_set_theme_clears_the_same_overrides_as_the_theme_module():
    """SET_THEME（展示执行链）清掉的覆盖与主题模块一致。"""
    from app.services.report_presentation_service import apply_presentation

    definition = _definition()
    via_module = theme_service.apply_theme(definition, "theme_dark").model_dump()
    via_operation = apply_presentation(
        ReportDefinitionDraft.model_validate(definition),
        [{"op": "SET_THEME", "params": {"themeId": "theme_dark"}}],
    ).definition.model_dump()

    for region_id in ("group_1", "leaf_1"):
        assert _leaf(via_operation, region_id).get("style") == (
            _leaf(via_module, region_id).get("style")
        )
    # 格式 / 布局 / 数据两边都不动
    for region_id in ("group_1", "leaf_1", "leaf_2"):
        assert _leaf(via_operation, region_id).get("format") == (
            _leaf(definition, region_id).get("format")
        )
        assert _leaf(via_operation, region_id).get("layout") == (
            _leaf(definition, region_id).get("layout")
        )
        assert _leaf(via_operation, region_id).get("data") == (
            _leaf(definition, region_id).get("data")
        )


# ---------- 6c. 应用主题清**全部实际样式层**（R06 第二轮） ----------

def _four_layer_leaf(definition: dict) -> dict:
    """给叶子卡补上四层样式（每层都带主题管理的旧键 + 非管理扩展键）。"""
    leaf = _leaf(definition, "leaf_1")
    leaf["styleRefs"] = [
        {"backgroundColor": "#00FF00", "keepRef": "ref-ext"},
        {"borderRadius": 8, "keepRef2": "ref-ext-2"},
    ]
    leaf["component"]["style"] = {"fontSize": 20, "keepComponent": "component-ext"}
    leaf["component"]["styleHints"] = [
        {"when": "RED", "style": {"opacity": 0.5, "keepHint": "hint-ext"}}
    ]
    return leaf


def test_apply_theme_clears_the_theme_managed_keys_in_every_style_layer():
    """主题服务这一侧也清**四层**：只清一层的话换主题看着没生效。"""
    definition = _definition()
    _four_layer_leaf(definition)

    applied = theme_service.apply_theme(definition, "theme_dark").model_dump()
    leaf = _leaf(applied, "leaf_1")

    # 1. region.style
    assert leaf["style"] == {"keepMe": "region-only"}
    # 2. region.styleRefs[]
    assert leaf["styleRefs"] == [
        {"keepRef": "ref-ext"},
        {"keepRef2": "ref-ext-2"},
    ]
    # 3. region.component.style
    assert leaf["component"]["style"] == {"keepComponent": "component-ext"}
    # 4. region.component.styleHints[].style
    assert leaf["component"]["styleHints"][0]["style"] == {"keepHint": "hint-ext"}
    # 格式 / 布局 / 数据不动
    assert leaf["format"] == {"kind": "NUMBER", "precision": 2}
    assert leaf["data"] == _leaf(definition, "leaf_1")["data"]


def test_apply_theme_does_not_modify_a_shared_style_source():
    """共享样式源不被改动：清的是区域自己那一份（不就地改共享字典）。"""
    from app.services.report_theme_service import clear_theme_managed_style_layers

    shared = {"textColor": "#FF0000", "keepShared": "shared-ext"}
    region = {"id": "leaf_1", "style": shared, "styleRefs": [shared]}

    changes = clear_theme_managed_style_layers(region)

    # 两层都清干净了
    assert region["style"] == {"keepShared": "shared-ext"}
    assert region["styleRefs"] == [{"keepShared": "shared-ext"}]
    # **共享源本身一个字节没动**
    assert shared == {"textColor": "#FF0000", "keepShared": "shared-ext"}
    # 每一层各记一条改动
    assert [path for path, _before, _after in changes] == [
        "/style",
        "/styleRefs/0",
    ]


def test_apply_theme_leaves_an_untouched_shared_style_source_alone():
    """主题服务这一侧：共享样式源同样不被改动。"""
    definition = _definition()
    leaf = _leaf(definition, "leaf_1")
    shared = {"textColor": "#FF0000", "keepShared": "shared-ext"}
    leaf["style"] = shared
    leaf["styleRefs"] = [shared]

    applied = theme_service.apply_theme(definition, "theme_dark").model_dump()
    cleared = _leaf(applied, "leaf_1")

    assert cleared["style"] == {"keepShared": "shared-ext"}
    assert cleared["styleRefs"] == [{"keepShared": "shared-ext"}]
    assert shared == {"textColor": "#FF0000", "keepShared": "shared-ext"}


# ---------- 7. 先摘要后提交 ----------

def test_save_theme_returns_impact_summary_before_commit(module: ThemeModule):
    tokens = {"colorBackground": "#FFFFFF", "colorText": "#123456"}

    summary = module.save_theme("我的主题", tokens)
    assert summary["requiresConfirm"] is True
    assert summary["affectedCount"] == 0
    assert summary["affectedDrafts"] == []
    assert summary["themeRevision"] == 0
    assert summary["nextRevision"] == 1
    assert summary["themeId"]

    # 只算摘要，不落库
    assert module.get_theme(summary["themeId"]) is None

    saved = module.commit_theme_save(
        summary["themeId"],
        name="我的主题",
        tokens=tokens,
        expected_revision=0,
    )
    assert saved["themeRevision"] == 1
    assert saved["tokens"]["colorText"] == "#123456"

    stored = module.get_theme(summary["themeId"])
    assert stored is not None
    assert stored["themeRevision"] == 1

    # 版本冲突
    with pytest.raises(Exception) as excinfo:
        module.commit_theme_save(
            summary["themeId"],
            name="我的主题",
            tokens=tokens,
            expected_revision=0,
        )
    assert getattr(excinfo.value, "code", "") == "theme_revision_conflict"


# ---------- 8. 白名单校验 ----------

def test_theme_validation_rejects_unknown_token():
    with pytest.raises(ThemeValidationError) as excinfo:
        theme_service.save_theme("坏主题", {"colorBackground": "#FFFFFF", "evilToken": 1})
    assert excinfo.value.code == "theme_validation_failed"

    with pytest.raises(ThemeValidationError):
        theme_service.save_theme("坏主题", {"colorBackground": "url(https://evil)"})
    with pytest.raises(ThemeValidationError):
        theme_service.save_theme("坏主题", {"colorBackground": "<script>"})
    with pytest.raises(ThemeValidationError):
        theme_service.save_theme("坏主题", {"colorBackground": "#FFFFFF"}, default_style={"unknownKey": 1})
    with pytest.raises(ThemeValidationError):
        theme_service.save_theme("坏主题", {"colorBackground": "#FFFFFF"}, default_style={"textAlign": "MIDDLE"})

    # 预置主题里的 defaultStyle 必须都在白名单内
    assert set(theme_service.get_theme("theme_default")["defaultStyle"]) <= set(THEME_MANAGED_STYLE_KEYS)


# ---------- 9. 样式继承顺序 ----------

def test_resolve_region_style_merge_order():
    definition = _definition()
    resolved_theme = {
        "themeId": "theme_default",
        "themeRevision": 1,
        "name": "默认主题",
        "tokens": theme_service.get_theme("theme_default")["tokens"],
        "defaultStyle": theme_service.get_theme("theme_default")["defaultStyle"],
    }

    style = theme_service.resolve_region_style(definition, "leaf_1", resolved_theme=resolved_theme)

    # 主题 < 祖先 < 区域 < 组件字段
    assert style["textColor"] == "#FF0000", "区域 style 覆盖祖先"
    assert style["borderRadius"] == 12, "区域 style 覆盖主题"
    assert style["fontSize"] == {"value": 14, "unit": "PX"}, "主题默认值保留"
    assert style["backgroundColor"] == "#FFFFFF"

    # 组件字段样式优先级最高
    definition["regions"][0]["children"][0]["component"]["style"] = {"textColor": "#00FF00"}
    style = theme_service.resolve_region_style(definition, "leaf_1", resolved_theme=resolved_theme)
    assert style["textColor"] == "#00FF00"

    # 组件 styleHints 里的 StyleSpec 也参与合并（行级条件样式字符串忽略）
    definition["regions"][0]["children"][0]["component"]["styleHints"] = [
        {"fieldRef": "x", "comparison": "GT", "threshold": 1, "style": "RED"},
        {"fieldRef": "y", "style": {"textAlign": "CENTER"}},
    ]
    style = theme_service.resolve_region_style(definition, "leaf_1", resolved_theme=resolved_theme)
    assert style["textAlign"] == "CENTER"

    # 找不到的区域返回空
    assert theme_service.resolve_region_style(definition, "not_exist") == {}


# ---------- 10. 落库：SQLite 共享主题 ----------

@pytest.fixture()
def theme_store(tmp_path):
    """落在临时目录里的主题存储（**绝不碰** `backend/data/haewu.db`）。"""
    repo = SqliteSessionRepository(tmp_path / "themes.db")
    return DraftWorkspaceSqlite(repository=repo)


def _seed_draft(
    adapter: DraftWorkspaceSqlite,
    draft_id: str,
    *,
    name: str,
    theme_id: str,
    definition: dict | None = None,
) -> dict:
    """造一份草稿：对象行 + 当前版本定义（定义里引用 `theme_id`）。"""
    adapter.upsert_draft_object(
        draft_id=draft_id,
        name=name,
        session_id="s1",
        current_revision=1,
        undo_cursor=0,
    )
    body = definition if definition is not None else {
        "schemaVersion": "1.0",
        "theme": {"themeRef": theme_id, "version": "1.0"},
        "regions": [],
    }
    adapter.insert_draft_version(draft_id, 1, "edit", body, None, "baseline")
    return body


def test_theme_is_persisted_in_sqlite_not_in_memory(tmp_path):
    """保存后换一个连接 / 换一个模块实例，仍能读到 —— 证明落库不是内存。"""
    db_path = tmp_path / "persisted.db"
    first = ThemeModule(storage=DraftWorkspaceSqlite(repository=SqliteSessionRepository(db_path)))

    summary = first.save_theme("持久化主题", {"colorText": "#123456"})
    first.commit_theme_save(
        summary["themeId"],
        name="持久化主题",
        tokens={"colorText": "#123456"},
        expected_revision=0,
    )

    # 换一条连接 = 换一个进程 / 重启一次
    reopened = ThemeModule(storage=DraftWorkspaceSqlite(repository=SqliteSessionRepository(db_path)))

    stored = reopened.get_theme(summary["themeId"])
    assert stored is not None, "主题必须落库，换实例也要读得到"
    assert stored["name"] == "持久化主题"
    assert stored["tokens"]["colorText"] == "#123456"
    assert stored["themeRevision"] == 1


def test_affected_drafts_lists_drafts_using_the_theme(theme_store: DraftWorkspaceSqlite):
    """影响清单：找出当前版本定义里引用该主题的草稿。"""
    _seed_draft(theme_store, "d1", name="日报", theme_id="theme_shared")
    _seed_draft(theme_store, "d2", name="周报", theme_id="theme_shared")
    _seed_draft(theme_store, "d3", name="月报", theme_id="theme_other")

    affected = theme_store.affected_drafts("theme_shared")
    assert [item["draftId"] for item in affected] == ["d1", "d2"]
    assert {item["name"] for item in affected} == {"日报", "周报"}
    assert theme_store.affected_drafts("theme_not_used") == []

    # 服务层的影响摘要走同一份清单
    module = ThemeModule(storage=theme_store)
    summary = module.save_theme("共享主题", {"colorText": "#123456"}, theme_id="theme_shared")
    assert summary["affectedCount"] == 2
    assert {item["draftId"] for item in summary["affectedDrafts"]} == {"d1", "d2"}


def test_resolve_theme_reads_the_stored_version(theme_store: DraftWorkspaceSqlite):
    """库里主题改了，resolve 必须给新版本 —— 这是联动的核心。"""
    module = ThemeModule(storage=theme_store)
    definition = {"theme": {"themeRef": "theme_live", "version": "1.0"}, "regions": []}

    module.commit_theme_save(
        "theme_live", name="联动主题", tokens={"colorText": "#111111"}, expected_revision=0
    )
    resolved = module.resolve_theme(definition)
    assert resolved["themeId"] == "theme_live"
    assert resolved["themeRevision"] == 1
    assert resolved["tokens"]["colorText"] == "#111111"

    # 存第二个版本：版本号 +1，解析立刻拿到新值
    saved = module.commit_theme_save(
        "theme_live", name="联动主题", tokens={"colorText": "#222222"}, expected_revision=1
    )
    assert saved["themeRevision"] == 2
    resolved = module.resolve_theme(definition)
    assert resolved["themeRevision"] == 2
    assert resolved["tokens"]["colorText"] == "#222222"


def test_commit_theme_save_bumps_revision(theme_store: DraftWorkspaceSqlite):
    """每存一次，主题版本 +1。"""
    module = ThemeModule(storage=theme_store)
    module.commit_theme_save("theme_rev", name="版本", tokens={"colorText": "#111111"})
    assert module.get_theme("theme_rev")["themeRevision"] == 1
    module.commit_theme_save("theme_rev", name="版本", tokens={"colorText": "#222222"})
    assert module.get_theme("theme_rev")["themeRevision"] == 2


def test_commit_theme_save_conflict_raises_conflict_error(theme_store: DraftWorkspaceSqlite):
    """乐观锁：expected_revision 对不上 → ThemeConflictError。"""
    module = ThemeModule(storage=theme_store)
    module.commit_theme_save("theme_lock", name="锁", tokens={"colorText": "#111111"})

    with pytest.raises(ThemeConflictError) as excinfo:
        module.commit_theme_save(
            "theme_lock", name="锁", tokens={"colorText": "#222222"}, expected_revision=0
        )
    assert excinfo.value.code == "theme_revision_conflict"
    assert excinfo.value.details["currentRevision"] == 1
    # 冲突不写库：版本号没动
    assert module.get_theme("theme_lock")["themeRevision"] == 1


def test_builtin_theme_default_style_survives_in_sqlite(theme_store: DraftWorkspaceSqlite):
    """预置主题落库后 defaultStyle 不丢。"""
    module = ThemeModule(storage=theme_store)
    builtin = module.get_theme("theme_default")
    assert builtin and builtin["defaultStyle"], "预置主题必须有 defaultStyle"

    # 预置主题没存过：库里读不到，回落资源
    assert theme_store.load_theme("theme_default") is None
    assert module.get_theme("theme_default")["defaultStyle"] == builtin["defaultStyle"]

    # 存一个新版本（不传 defaultStyle）→ 预置的 defaultStyle 保留
    module.commit_theme_save(
        "theme_default", name="默认主题", tokens={"colorText": "#101010"}, expected_revision=1
    )
    stored = theme_store.load_theme("theme_default")
    assert stored is not None
    assert stored["themeRevision"] == 2
    assert stored["defaultStyle"] == builtin["defaultStyle"]
    assert module.get_theme("theme_default")["defaultStyle"] == builtin["defaultStyle"]


def test_saving_theme_does_not_touch_draft_definitions(theme_store: DraftWorkspaceSqlite):
    """联动只更新主题这一份共享值 —— 草稿定义一个字都不动。"""
    definition = _seed_draft(
        theme_store,
        "d1",
        name="日报",
        theme_id="theme_shared",
        definition={
            "schemaVersion": "1.0",
            "theme": {"themeRef": "theme_shared", "version": "1.0"},
            "regions": [{"id": "leaf_1", "name": "总耗电"}],
        },
    )
    before = theme_store.get_draft_version("d1", 1)["definition"]

    module = ThemeModule(storage=theme_store)
    module.commit_theme_save(
        "theme_shared", name="共享主题", tokens={"colorText": "#333333"}, expected_revision=0
    )

    after = theme_store.get_draft_version("d1", 1)["definition"]
    assert after == before == definition, "保存主题不得改草稿定义"

    # 联动 = 解析期生效：草稿下次 resolve 拿到新主题
    resolved = module.resolve_theme(before)
    assert resolved["themeId"] == "theme_shared"
    assert resolved["tokens"]["colorText"] == "#333333"


def test_module_level_service_follows_session_repository(monkeypatch, tmp_path):
    """模块级 theme_service 按调用时跟随会话仓库 —— 测试落临时库，不碰用户库。"""
    from app.services.session_service import session_service

    repo = SqliteSessionRepository(tmp_path / "sessions_theme.db")
    monkeypatch.setattr(session_service, "_repo", repo)
    assert repo.db_path != get_settings().session_db_path, "测试不得写用户库"

    summary = theme_service.save_theme("跟随仓库", {"colorText": "#445566"})
    theme_service.commit_theme_save(
        summary["themeId"],
        name="跟随仓库",
        tokens={"colorText": "#445566"},
        expected_revision=0,
    )

    row = repo.conn.execute(
        "SELECT theme_id, theme_revision FROM draft_themes WHERE theme_id = ?",
        (summary["themeId"],),
    ).fetchone()
    assert row is not None, "主题必须落在会话仓库指向的库里"
    assert int(row["theme_revision"]) == 1

    # 解析也走库里那份
    resolved = theme_service.resolve_theme({"theme": {"themeRef": summary["themeId"]}})
    assert resolved["tokens"]["colorText"] == "#445566"
