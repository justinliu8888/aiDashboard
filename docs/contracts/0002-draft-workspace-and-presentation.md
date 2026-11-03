# 契约 0002：对话式排版、草稿工作区与共享主题（v1，冻结）

日期：2026-11-02。阶段：第 02 阶段实施期冻结（C00）。
本契约只声明**本批新增**的契约面；已有 `0001-统一开发契约表.md` 的字段与语义继续有效。
术语沿用 `GLOSSARY.md`：**外观分组 / 取消分组 / 删除整组内容 / 隐藏内容**。

---

## 0. 本批的事实基线（实施期实测）

- `ReportDefinitionDraft.regions` 是**扁平列表**（`backend/app/schemas/report_definition.py:23`）。仓库里**不存在**容器 / 分组 / 隐藏 / 撤销的任何实现（`CONTAINER`、`children`、`parentId`、`undo`、`redo` 在 `backend/app` 全部 0 命中）。
- 造数种子是 `definitionId|definitionVersion|generatorVersion|start|end|regionId|metricKey`（`backend/app/services/simulated_data_service.py:207-229`、`:1220`），**regionId 在种子串里**。
- 载荷 `reportPreviewPayload` = `{previewContext, regions[]}`，regions 每项 7 个键，**按 `regionId` 关联定义里的 `id`**（`backend/app/services/preview_service.py:1442-1522`）。
- 造数与载荷都**只遍历顶层 `regions`**（`simulated_data_service.py:1997`、`preview_service.py:1374`/`:1482`）。
- 接受链路：`agent_service._handle_pending_edit_choice`（`backend/app/services/agent_service.py:2002-2173`）→ `template_edit_engine.apply` → `_build_template_edit_response`；**落库发生在路由器**：`routers/agent.py:202` `_persist_turn` → `session_service.record_turn` → `sessions.snapshot`。
- SQLite：`backend/app/storage/sqlite_store.py:25-69` 的 `CREATE TABLE IF NOT EXISTS` DDL + `_migrate()` 补列（`:143-155`）。库路径 `AI_SESSION_DB_PATH`，默认 `backend/data/haewu.db`（`backend/app/config.py:152-155`）。
- 前端面板唯一入口 `renderReportPanel(highlightRegionIds)`（`frontend/pages/workbench.js:3463`），数据源 `session.reportPreviewPayload`（扁平 `regions[]`），叶子渲染 `renderRegionCard(region, definition, payload, totalsByUnit)`（`:3794`）。
- 前端模块模式是 UMD-lite 双导出 shim：`window.WorkbenchPlan` / `module.exports`（`frontend/pages/workbench_plan.js:26-31`）。
- `agent_service.py`、`session_service.py`、`edit_reference_resolver.py` 已由关联工单 A 包交付并留下三态契约；`workbench.js` / `workbench.html` 已由 C 包交付（见 OUT `00-P0开工登记.md`）。**本批沿用该三态，不引入第二个"选中卡片"字段。**

---

## 1. 区域树：容器与叶子

### 1.1 结构

`definition.regions` 仍是**顶层区域列表**；容器区域的子区域放在容器自己的 `children` 里：

```jsonc
{
  "id": "group_1",
  "name": "对比组",
  "role": "DETAIL",
  "layout": { "placement": { "type": "GRID", "column": 1, "columnSpan": 12, "row": 2 } },
  "component": {
    "type": "CONTAINER",
    "arrangement": {
      "mode": "GRID",          // GRID | FLOW
      "columns": 2,            // GRID 有效列数（1–12）
      "direction": "HORIZONTAL", // HORIZONTAL | VERTICAL
      "gap": { "value": 16, "unit": "PX" }
    }
  },
  "children": [ { "id": "region_a", ... }, { "id": "region_b", ... } ]
}
```

- **容器区域**：`component.type == "CONTAINER"`，**不造数、不出载荷条目、不出卡片数值**。
- **叶子区域**：非 CONTAINER 区域。叶子在定义树里出现的位置决定阅读顺序。
- **旧平面定义原样兼容**：没有 `children` 的定义仍按扁平列表渲染，**不为展示批量重写历史定义**。
- **ID 规则**：移动保留 `id`；复制生成新 `id`；容器 `id` 由服务端生成（`group_<hex8>`）。

### 1.2 递归工具（P1 拥有，P0/P2/P3/P4 只读消费）

`backend/app/services/report_presentation_service.py` 提供**唯一**一套递归工具，其他包不得另写一份：

| 函数 | 签名 | 说明 |
| --- | --- | --- |
| `iter_regions(definition)` | `-> Iterator[tuple[dict, list[dict]]]` | 按阅读顺序产出 `(region, ancestors)`，含容器 |
| `iter_leaves(definition)` | `-> Iterator[dict]` | 只产出叶子 |
| `find_region(definition, region_id)` | `-> dict \| None` | 递归按 id 找 |
| `region_parent(definition, region_id)` | `-> dict \| None` | 父容器 |
| `region_ancestors(definition, region_id)` | `-> list[dict]` | 从根到父 |
| `region_index_path(definition, region_id)` | `-> list[int]` | 树内路径（用于 JSON Pointer） |
| `is_container(region)` | `-> bool` | |
| `container_of(definition, region_id)` | `-> dict \| None` | 区域所在的容器（根返回 None） |
| `effective_simulation_identity(region)` | `-> str` | `previewSpec.simulationIdentity` 或回退 `region.id` |

**JSON Pointer 路径规则**：区域路径写作 `/regions/<regionId>`（沿用既有约定，见 `template_edit_engine.py:3466`），容器子区域写作 `/regions/<containerId>/children/<childIndex>`。

---

## 2. 数据身份：simulationIdentity

### 2.1 字段

`region.templateBinding.previewSpec.simulationIdentity: str`（**本批拟新增**，服务端维护）。

- **缺省兼容**：老卡片没有这个字段时，`effective_simulation_identity(region)` 回退取其原 `regionId`。
- **写回规则**：只有当值**不等于** `region.id` 时才落库（复制件才需要写），避免给历史区域批量加字段。

### 2.2 语义（强约束）

| 动作 | regionId | simulationIdentity | 种子 | 结论 |
| --- | --- | --- | --- | --- |
| 移动 / 成组 / 改样式 | 不变 | 不变 | 不变 | 数值不漂 |
| 复制 | **新** | **沿用源的有效身份** | 与源相同 | 复制的示例值一致 |
| 删除 | — | — | — | — |

**种子改造（P0 执行）**：`simulated_data_service` 的区域级种子材料由 `region_id` 改为**有效数据身份**（`simulationIdentity` 优先，回退 `regionId`）；载荷关联仍用 `regionId`。老卡片有效身份 = `regionId`，**数值与改造前完全一致**。

**重复展示不参与总计翻倍**：复制件在 `previewSpec.duplicateOfRegionId` 标记源区域 id；前端 `buildTotalsByUnit` 排除带该标记的区域。

---

## 3. 展示操作批次（P1 拥有）

### 3.1 新 Intent

`schemas/edit_plan.py` 新增 `EditIntent.APPLY_PRESENTATION = "APPLY_PRESENTATION"`。
`EditOperation` 新增字段：

```python
presentationOperations: list[dict[str, Any]] = Field(default_factory=list)
```

每项是**受约束的白名单操作**（不是任意属性路径、不是任意 CSS 文本、不是脚本）：

| `op` | 允许参数 | 说明 |
| --- | --- | --- |
| `CREATE_GROUP` | `groupId`, `name`, `description`, `role`, `memberRegionIds[]`, `arrangement{mode,columns,direction,gap}` | 新建容器并把成员移入 |
| `SET_GROUP_ARRANGEMENT` | `groupId`, `arrangement{...}` | 改组内模式 / 列数 / 方向 / 间距 |
| `MOVE_REGION` | `regionId`, `targetGroupId`(null=根), `index` | 移入 / 移出 / 跨组 / 排序 |
| `UNGROUP` | `groupId` | **取消分组：保留子卡与相对顺序** |
| `DELETE_GROUP` | `groupId`, `force` | 删除整组内容；`force=false` 且有引用时先报影响 |
| `COPY_REGION` | `regionId`, `newRegionId` | 复制：新 id、沿用源数据身份与口径 |
| `DELETE_REGION` | `regionId`, `force` | 删卡片；引用影响先提示 |
| `HIDE_REGION` / `SHOW_REGION` | `regionId` | 写 `region.hidden=true/false`（**显示隐藏开关不写定义**） |
| `SET_PLACEMENT` | `regionId`, `placement{...}` | 见 §3.3 |
| `SET_STYLE` | `regionId`, `style{...}` | 见 §3.4 |
| `SET_FORMAT` | `regionId`, `format{...}` | 见 §3.5 |
| `SET_PAGE` | `webMaxWidth`, `arrangement{mode,columns,gap}` | 页面最大宽度 + 根排列 |
| `SET_METADATA` | `name`, `description`, `tags[]` | **metadata 只开放这三个** |
| `SET_THEME` | `themeId` | 应用共享主题 |

**取值域白名单**（越界即整批失败，不做部分应用）：

- `arrangement.mode`：`GRID` | `FLOW`
- `arrangement.columns`：整数 `1..12`
- `arrangement.direction`：`HORIZONTAL` | `VERTICAL`
- `arrangement.gap`：`{value: 整数 0..96, unit: "PX"}`
- `placement.type`：`GRID` | `FLOW` | `ABSOLUTE`
- `page.webMaxWidth`：`{value: 整数 640..2560, unit: "PX"}`

### 3.2 纯计算 Interface

```python
def apply_presentation(
    draft: ReportDefinitionDraft,
    operations: list[dict[str, Any]],
    *,
    base_revision: int | None = None,
) -> PresentationResult
```

`PresentationResult`（`backend/app/schemas/report_presentation.py`）：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `ok` | `bool` | 整批成功与否 |
| `definition` | `ReportDefinitionDraft` | **失败时 == 入参原对象内容**（整批失败不留半份定义） |
| `changeSet` | `ChangeSet \| None` | 成功时给出；失败为 None |
| `affectedRegionIds` | `list[str]` | 受影响区域（含新建容器 id） |
| `issues` | `list[dict]` | 校验/布局问题（`{code, message, regionId?}`） |
| `dataBindingChanged` | `bool` | 纯展示操作**必须恒为 False** |
| `failure` | `str` | 面向用户的失败说明 |
| `reason` | `str` | 失败分类（`no_target` / `invalid_operation` / `out_of_range` / `cycle` / `unknown_region` / `reference_blocked`） |

**纯函数约束**：不调用模型、不取数、不碰数据库、不碰前端、不写全局状态。整批在一个深拷贝上执行，成功才返回新对象。

### 3.3 placement 取值域

- `GRID`：`{type:"GRID", column:int1..12, columnSpan:int1..12, row:int?>=1, rowSpan:int?1..12, minHeight?}`
- `FLOW`：`{type:"FLOW", align?: "START"|"CENTER"|"END"|"STRETCH", width?: {value:整数,unit:"PX"|"PERCENT"}, minHeight?}`
- `ABSOLUTE`：`{type:"ABSOLUTE", x:int, y:int, width:int, height:int, zIndex:int}`

`column + columnSpan - 1` 不得超过父容器有效列数（根容器列数取 `page.arrangement.columns`，默认 12）。

### 3.4 style 取值域

`{fontFamily?:str, fontSize?:{value:int,unit:"PX"}, fontWeight?:int 100..900, textColor?:str, backgroundColor?:str, textAlign?: "LEFT"|"CENTER"|"RIGHT", verticalAlign?: "TOP"|"MIDDLE"|"BOTTOM", lineHeight?:{value:number,unit:"PX"|"PERCENT"}, padding?:{value:int,unit:"PX"}, margin?:{value:int,unit:"PX"}, border?:{width:int,style:"SOLID"|"DASHED"|"DOTTED"|"NONE",color:str}, borderRadius?:int, opacity?:number 0..1}`

**拒绝**：任意 CSS 文本、未知键、`url()`、`expression(`、`javascript:`、`<`/`>`。

### 3.5 format 取值域

`{kind?: "NUMBER"|"PERCENT"|"CURRENCY"|"DURATION"|"DATE"|"DATETIME"|"TEXT", precision?:int 0..6, unitLabel?:str, thousandSeparator?:bool, dateTimePattern?:str, nullText?:str, roundingMode?: "HALF_UP"|"HALF_DOWN"|"FLOOR"|"CEIL"}`

`unitLabel` 只允许**当前单位或空**——不允许把 `Wh` 改名成 `kWh`（单位换算不在本批）。

### 3.6 引擎接线（P1）

`template_edit_engine._apply_one_intent` 新增分支 → `_apply_presentation`。
`apply()` 的复合分支（`plan.operations`）不变；`APPLY_PRESENTATION` **不**进 `COMPOSABLE_INTENTS`（它自己就是一批原子操作）。
`parse()` 新增自然语言规则分支，把"组成一组 / 并排 / 间距 / 取消分组 / 隐藏 / 复制"等表达映射成上述白名单操作；**模糊尺寸由 AI 给具体建议**再接受。

---

## 4. 载荷与造数（P0 执行，P1 提供工具）

1. `preview_service._prepare_definition_for_simulation` 改为**递归**处理容器子树（对叶子调 `adapt_preview_spec`）。
2. `preview_service.build_report_preview_payload` 改为**递归**产出 `regions[]`：**只有叶子进载荷**；容器不进载荷。
3. `simulated_data_service.generate_simulated_data` 改为**递归**产出 `{regionId: payload}`；**容器跳过造数**。
4. 区域级种子材料改用有效数据身份（§2.2）。
5. **旧平面定义零影响**：递归实现对"没有 children"的定义与改造前逐字节一致。

> 组内子卡遗漏是本批 V1 阻塞项，不留给正式测试。

---

## 5. 前端展示模块（P3 拥有）

### 5.1 模块契约

沿用现有 UMD-lite 双导出 shim（照抄 `frontend/pages/workbench_plan.js:26-31`）：

```javascript
window.WorkbenchPresentation = {
  MODULE_NAME: "workbench-presentation",
  MODULE_ATTR: "data-presentation",
  render(root, view, { renderLeaf }),
  clear(root),
  // 测试与接线用的纯函数
  buildTree(definition),
  resolveArrangement(region),
  mergeStyle(styleChain, resolvedTheme),
};
```

`render(root, { definition, previewPayload, resolvedTheme, showHidden }, { renderLeaf })`：
- `root`：挂载容器（`#preview-root`）。
- `definition`：权威定义（**树由它决定**，不再由扁平载荷顺序决定）。
- `previewPayload`：数据来源，按 `regionId` 关联叶子。
- `resolvedTheme`：服务端解析好的主题（`{themeId, themeRevision, tokens, defaultStyle}`）。
- `showHidden`：是否显示隐藏内容（开关**不写定义**）。
- `renderLeaf(leafRegion, payloadRegion, ctx)`：由 P0 注入的既有叶子渲染（复用 `renderRegionCard`）。

**模块职责边界**：只管排版 / 样式 / ResizeObserver 清理；**不发请求、不改会话状态、不造数**。

### 5.2 DOM 契约（供自检与交互断言）

| 元素 | 标记 |
| --- | --- |
| 容器分组 | `<section data-presentation="group" data-region-id="<containerId>" data-arrangement-mode="GRID" data-arrangement-columns="2" data-gap="16">` |
| 叶子卡片 | 由 `renderLeaf` 产出，主脚本沿用既有 `data-region-id`；新模块在其外层包 `<div data-presentation="leaf" data-region-id="...">` |
| 隐藏内容（仅 showHidden 模式） | `data-presentation="hidden-leaf"` + `data-hidden="true"` + 半透明 |
| 组内网格 | 容器内 `display:grid; grid-template-columns: repeat(<columns>, minmax(0,1fr)); gap:<gap>px` |
| 流式 | `data-arrangement-mode="FLOW"` → `display:flex; flex-direction: row|column` |

### 5.3 样式继承顺序（P3 实现，P4 提供主题）

主题默认 → 祖先区域样式 → 当前区域 `styleRefs`/`style` → 组件字段样式。
（与协议 §5.2 一致；条件样式不在本批。）

### 5.4 生命周期

`clear(root)` 释放 ResizeObserver 与事件监听；`render` 前先 `clear`。
ECharts 尺寸变化后由主脚本 `renderReportPanel` 末尾的 `requestAnimationFrame` 统一 `chart.resize()`；**新模块额外注册 ResizeObserver**，在容器尺寸变化时触发一次 resize（现有代码没有 window resize 监听，容器变化不会自动 resize）。

---

## 6. 草稿工作区与持久提交（P2 拥有）

### 6.1 新表（`storage/sqlite_store.py` 的 `_SCHEMA` 追加，`CREATE TABLE IF NOT EXISTS`）

```sql
CREATE TABLE IF NOT EXISTS draft_objects (
    draft_id        TEXT PRIMARY KEY,
    name            TEXT NOT NULL DEFAULT '',
    description     TEXT NOT NULL DEFAULT '',
    session_id      TEXT,
    current_revision INTEGER NOT NULL DEFAULT 0,
    saved_revision  INTEGER,
    undo_cursor     INTEGER NOT NULL DEFAULT 0,
    theme_id        TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS draft_versions (
    draft_id    TEXT NOT NULL,
    revision    INTEGER NOT NULL,
    kind        TEXT NOT NULL,          -- 'edit' | 'saved'
    definition_json TEXT NOT NULL,
    snapshot_json   TEXT,
    label       TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    PRIMARY KEY (draft_id, revision)
);
CREATE TABLE IF NOT EXISTS draft_history (
    draft_id  TEXT NOT NULL,
    seq       INTEGER NOT NULL,
    revision  INTEGER NOT NULL,
    action    TEXT NOT NULL,            -- 'commit' | 'undo' | 'redo' | 'save' | 'save_as'
    label     TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    PRIMARY KEY (draft_id, seq)
);
CREATE TABLE IF NOT EXISTS draft_tasks (
    task_id      TEXT PRIMARY KEY,
    draft_id     TEXT NOT NULL,
    operation_id TEXT NOT NULL DEFAULT '',
    state        TEXT NOT NULL,         -- RUNNING|STOPPING|STOPPED|COMPLETED|ROLLED_BACK|FAILED
    checkpoint_json TEXT,
    commit_token TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS draft_themes (
    theme_id       TEXT PRIMARY KEY,
    theme_revision INTEGER NOT NULL DEFAULT 1,
    name           TEXT NOT NULL,
    tokens_json    TEXT NOT NULL,
    builtin        INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);
```

- **迁移前备份用户库**；只新增表，**不批量改历史模板、旧坐标、旧会话**。
- `sqlite_store.py` 只有 P2 写；P0 改 `session_service` 时**只调用 Interface**，不自行写状态。

### 6.2 `DraftWorkspace` Interface（`services/draft_workspace.py`）

| 方法 | 说明 |
| --- | --- |
| `list_drafts() -> list[dict]` | 只返回 `draftId` / `name` / `updatedAt` |
| `load_draft(draft_id) -> WorkspaceState \| None` | |
| `commit_edit(draft_id, *, definition, snapshot, expected_revision, label) -> WorkspaceState` | **一次接受 = 一个修改单位**：写定义 + 写版本 + 写历史 + 推进 undo 指针，**同一事务** |
| `save(draft_id, *, expected_revision) -> WorkspaceState` | 显式保存版：`savedRevision` 前进，**不自动发布** |
| `undo(draft_id) -> WorkspaceState` | 游标前移，恢复上一完成单位；**revision 仍单调递增**（不回退乐观锁数值） |
| `redo(draft_id) -> WorkspaceState` | |
| `save_as(draft_id, *, name) -> WorkspaceState` | 新 draftId；**不复制聊天、redo、旧修改历史**；可共用同一主题引用 |
| `begin_task / checkpoint_task / complete_task / stop_task` | 任务状态机与检查点 |
| `acquire(draft_id, owner) / release(draft_id, owner)` | 同草稿写互斥 |

`WorkspaceState` 至少含：`draftId`、`revision`、`savedRevision`、`definition`、`businessState`、`prompt`、`pendingChoice`、`pendingEditPlan`、`undoPointer`、`redoPointer`、`activeTask`、`themeId`、`updatedAt`。

### 6.3 版本语义

- `metadata.revision` = 当前编辑版本；`savedRevision` = 显式保存版版本；二者**不同义**。
- 撤销移动历史游标并恢复内容，但**写入版本仍单调递增**。
- 新修改提交**清掉旧 redo 分支**。
- 重开恢复同一游标与菜单，**不自动续跑**。

---

## 7. 工作区路由（P0 拥有 `routers/drafts.py`）

| 方法 | 路径 | body | 返回 |
| --- | --- | --- | --- |
| GET | `/api/drafts` | — | `[{draftId, name, updatedAt}]` |
| GET | `/api/drafts/{draftId}` | — | `WorkspaceState` |
| POST | `/api/drafts/{draftId}/actions` | `{action: "SAVE"\|"UNDO"\|"REDO"\|"SAVE_AS", expectedRevision?: int, name?: str}` | `WorkspaceState` |
| POST | `/api/draft-tasks/{taskId}/stop` | — | `{state, choices: ["继续完成剩下的工作","撤销（回到上一步）"]}` |
| POST | `/api/draft-tasks/{taskId}/resume` | — | `{state}` |

服务端检查 revision / 锁；**原对话入口继续提候选与接受，不并存两套提交逻辑**。

---

## 8. 共享主题（P4 拥有）

### 8.1 主题对象

`themeId`、`themeRevision`、`name`、`tokens`（白名单）、`defaultStyle`（白名单）。
**预置主题**在 `backend/app/resources/report_themes.json`（P4 新增）。

### 8.2 `ThemeModule`（`services/report_theme_service.py`）

| 方法 | 说明 |
| --- | --- |
| `list_themes() -> list[dict]` | 预置 + 自定义 |
| `get_theme(theme_id) -> dict \| None` | |
| `resolve_theme(definition, *, theme_id=None) -> dict` | **服务端解析**，返回 `{themeId, themeRevision, tokens, defaultStyle}` |
| `apply_theme(definition, theme_id) -> ReportDefinitionDraft` | 应用主题：覆盖**主题管理的旧外观设置**，此后允许新的局部覆盖；**不改布局 / 统计 / FormatSpec** |
| `save_theme(name, tokens, *, theme_id=None) -> dict` | 新建或新版本；先返回影响摘要并确认，再原子版本提交 |

### 8.3 联动更新

模板保存后**使用该主题的报表一起更新外观**（联动，不要求逐份重新应用）。
联动写共享值，不逐份改草稿 JSON；撤销用**版本化补偿**，不以单份草稿旧 JSON 覆盖其他对象后来的更新。

### 8.4 前端

前端**不拼主题注册表**；`resolvedTheme` 由服务端下发并传到渲染器。
其他报表打开 / 刷新时**不能停在旧缓存**。

---

## 9. 本批不做什么

不扩展组件目录、不做统计口径编辑、不做条件样式、不做地图 / 透视等高级组件、不做打印导出 / 纸张分页、不做发布、不做运营调度。
E07 本批只保护既有历史 / 结果不被覆盖，**不借其"发布不可变"文案实现新发布系统**。
不增加拖拽或通用属性面板（ADR 0003 的"无拖拽"决定继续有效）。
显示 / 隐藏开关**不写报表定义**。

---

## 10. 文件独占（本次实施）

| 包 | 独占文件 |
| --- | --- |
| P0 接线 | `services/agent_service.py`、`services/context_engine.py`、`services/edit_reference_resolver.py`、`agents/agentscope_report_agent.py`、`agents/prompts/template_edit.md`、`tools/submit_edit_plan_tool.py`、`schemas/agent.py`、`routers/agent.py`、`routers/sessions.py`、**新增 `routers/drafts.py`**、`main.py`、`services/session_service.py`、`services/preview_service.py`、`services/simulated_data_service.py`、`services/report_draft_service.py`、`services/template_definition_service.py`、`frontend/pages/workbench.js`、`frontend/pages/workbench.html`、`docs/contracts/0002-*.md`、`docs/adr/0005-*.md` |
| P1 展示执行 | `schemas/edit_plan.py`、`services/template_edit_engine.py`、`services/validation_service.py`、**新增 `schemas/report_presentation.py`**、**新增 `services/report_presentation_service.py`**、`tests/test_report_presentation.py` |
| P2 工作区持久化 | `services/definition_save_service.py`、**新增 `services/draft_workspace.py`**、**新增 `storage/draft_workspace_sqlite.py`**、`storage/sqlite_store.py`（仅 P2 写迁移 / 连接扩展）、`tests/test_draft_workspace.py` |
| P3 前端展示 | **新增 `frontend/pages/workbench_presentation.js`**、**新增 `frontend/pages/workbench_presentation.css`**、**新增 `frontend/tests/workbench_presentation.test.mjs`** |
| P4 主题 | **新增 `services/report_theme_service.py`**、**新增 `resources/report_themes.json`**、`tests/test_report_themes.py` |

依赖方向：**P0 → P1 → (P3, P4)**；**P2 ← P4**（主题持久化用 P2 的存储）；P1 **不依赖** P2/P3/P4。

---

## Suggested Skills

- 写契约与 ADR：`writing-for-agents`（本文件按其结构写）。
- 术语冲突时：`domain-modeling`（`GLOSSARY.md` 已有外观分组 / 取消分组 / 删除整组内容 / 隐藏内容）。
- 改完前端：`frontend-self-check` → `frontend-interaction-check`（P0 执行，范围按 OUT 固定流程）。
- 共享文件出现新改动：`diagnosing-bugs`（先复现再改）。
