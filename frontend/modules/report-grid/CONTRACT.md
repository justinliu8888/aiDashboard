# G 独立网格模块 · 冻结契约

日期：2026-10-28 · 集成分支 `feature/grid-prep-g` · 固定基线 `732dfa72e0f423cb82cecddc82629ea1e8f62eba`

本文件是本次预备开发的**唯一契约来源**。两个子 agent 按本文件并行开发，代码审查以本文件为 Spec。

本模块是**可复用的网格几何与视图模块**，不是 TemplateDesign、公共 HTTP 协议或完整 #27/#28 交付。
不接主工作台、后端、自动保存、AI、模板绑定与数据预览。宿主负责设计、保存与 AI。

## 1. 公开 Interface（冻结，不得改动签名）

### 1.1 布局计算（纯函数，无 DOM、无全局状态）

```js
resolveGridLayout({ items, intent, config })
  -> { kind: "success", placements, affectedRegionIds }
   | { kind: "invalid", code, message }
```

| 导出 | 说明 |
| --- | --- |
| `resolveGridLayout(input)` | 唯一主入口 |
| `DEFAULT_GRID_CONFIG` | 默认展示配置（只读常量） |
| `GRID_COLUMNS` | 固定列数 `12` |

文件：`frontend/modules/report-grid/layout/grid-layout.mjs`（ESM，Node 24 与浏览器 `type="module"` 均可直接加载）。

### 1.2 页面挂载

```js
mountReportGrid(root, { items, readonly, config, renderContent, onIntent, onSelect })
  -> { update(view), destroy() }
```

文件：`frontend/modules/report-grid/page/report-grid.mjs`（ESM）。
`page/report-grid.mjs` **只允许**从 `../layout/grid-layout.mjs` 导入 `resolveGridLayout`、`DEFAULT_GRID_CONFIG`、`GRID_COLUMNS` 三个名字，不得改动布局模块文件。

页面模块的**公开导出只有 `mountReportGrid`**：样式表由宿主自行引入
`frontend/modules/report-grid/styles/report-grid.css`，模块不注入、不打包样式。

### 1.3 数据形状

```js
// item（独立视图对象，不是模板对象）
{
  regionId: string,               // 稳定 ID，非空；改标题/改类型/移动都不改变
  placement: {
    type: "GRID",                 // 固定
    column: number,               // 1 基整数，1..12
    row: number,                  // 1 基正整数；行数随内容增长，无上限
    columnSpan: number,           // 正整数，column + columnSpan - 1 <= 12
    rowSpan: number,              // 正整数
  },
  minColumnSpan?: number,         // 可选，覆盖 config.defaultMinColumnSpan
  minRowSpan?: number,            // 可选，覆盖 config.defaultMinRowSpan
  label?: string,                 // 仅供渲染兜底，宿主内容仍由 renderContent 提供
}

// intent
null | undefined                   // 初始化
{ kind: "move",   regionId, column, row }
{ kind: "resize", regionId, column?, row?, columnSpan?, rowSpan? }

// 成功返回
{
  kind: "success",
  placements: [{ regionId, placement: { type: "GRID", column, row, columnSpan, rowSpan } }],  // 全部 item，顺序同 items
  affectedRegionIds: string[],    // 见下方「受影响集合」定义
}

// 失败返回
{ kind: "invalid", code, message }
```

#### 受影响集合（affectedRegionIds）

受影响集合 = **目标 + 实际被让位的区域 + 因输入非法而被补齐的区域**。

- 目标**确实发生变化时**排在第一；目标落位与输入相同则不在集合内（它没有变化）。
- 手势路径下，其余按「原 row → 原 column → regionId」确定性排序。
- 初始化路径（`intent` 为空）下按 `items` 顺序，与 §2.2「按 items 顺序补齐」一致。
- **合法且未被让位的区域不在集合内**（无关组件不动）。
- 初始化补齐项计入集合的原因：父规格要求「保存前的初始化…按现有组件顺序补齐」，
  补齐结果必须能随意图回到宿主并被保存，否则非法 placement 会一直留在宿主模型里。
  冻结接口没有单独的「补齐结果」通道，因此随意图一并回传。
- 集合为空表示本次手势没有产生任何变化，宿主据此不提交任何修改。

### 1.4 配置（`DEFAULT_GRID_CONFIG`）

```js
{
  columns: 12,             // 固定 12
  rowUnitPx: 48,           // 行单位（像素）
  gapPx: 12,               // 行列间距
  defaultMinColumnSpan: 1, // 默认最小跨列
  defaultMinRowSpan: 1,    // 默认最小跨行
}
```

`config` 传入时与默认值浅合并。**这些是展示用预备配置，不代表生产最小尺寸或完整 TemplateDesign 已冻结。**

`columns` **固定为 12**，不随 `config` 变化：布局层与页面层都按 12 列校验与渲染，
传入其他值按 12 处理，避免渲染口径与几何校验口径不一致。

## 2. 布局语义（冻结）

1. **输入不就地修改**：不得修改 `items`、其 `placement` 或任何嵌套对象；返回值是新对象。
2. **初始化**（`intent` 为空）：保留已有合法信息，只为**缺失或非法**的位置与尺寸按 `items` 顺序补齐；`affectedRegionIds` 只含被补齐的 ID。已有合法信息一项不改。
3. **move**：目标落到请求的 `(column, row)`；目标优先占据落位。
4. **resize**：`column/row/columnSpan/rowSpan` 中缺省项沿用目标当前值。
5. **碰撞让位**：与目标落位冲突的组件**保持横向位置（column/columnSpan 不变），只向下让位**到第一个合法行；连锁碰撞按「原 row → 原 column → regionId」确定性顺序处理。无冲突组件**完全不动**。
6. **不向上填空**：不得做全局紧凑排版、任意交换或向上填补空位。缩小或删除后保留空位。
7. **最小尺寸**：目标跨列/跨行不得小于其生效最小值（item 覆盖优先，否则取 config 默认）。
8. **越界**：`column + columnSpan - 1 > columns` 判为越界。
9. **确定性**：同一输入与配置必须产生同一输出。

### 2.1 错误码（冻结，code 为稳定可辨识字符串）

| code | 触发条件 |
| --- | --- |
| `INVALID_ITEMS` | `items` 不是数组 |
| `INVALID_PLACEMENT` | item 缺 `regionId`、`regionId` 非字符串/空串，或 `placement.type` 不是 `"GRID"` |
| `DUPLICATE_REGION_ID` | `items` 中出现重复 `regionId` |
| `UNKNOWN_REGION` | intent 目标的 `regionId` 不在 `items` 中 |
| `INVALID_INTENT` | `intent.kind` 不是 `move` / `resize`，或缺 `regionId` |
| `OUT_OF_BOUNDS` | 目标落位横向越界（`column + columnSpan - 1 > columns`），或 column/row 非正整数 |
| `MIN_SIZE_VIOLATION` | 目标跨列/跨行小于生效最小尺寸 |

校验顺序：`INVALID_ITEMS` → `INVALID_PLACEMENT` → `DUPLICATE_REGION_ID` → `INVALID_INTENT` → `UNKNOWN_REGION` → 尺寸与越界。
`message` 为中文可读说明，仅用于调试与宿主提示，**不得**作为判定依据。

## 3. 页面行为（冻结）

1. **readonly 缺省为 `true`**；只有宿主显式传 `readonly: false` 才进入编辑态。
2. **只读仍可选择与查看**：点击区域触发 `onSelect(regionId)`；只读态不显示拖拽手柄与网格线。
3. **一次手势一次回调**：`pointerup` 结束一次有效手势时，`onIntent` **只调用一次**；`pointermove` 过程**不得**回调。
4. **回调载荷**：
   ```js
   {
     kind: "move" | "resize",
     regionId,                                  // 目标
     placements: [{ regionId, placement }],      // 仅受影响区域的完整 placement
     affectedRegionIds: string[],
   }
   ```
   `placements` 只含**受影响区域**（目标 + 实际让位者 + 因输入非法而被补齐的区域，见「受影响集合」），
   不含合法且未被让位的无关组件。`resolveGridLayout` 返回的 `placements` 才是全量集合。
5. **零变更不回调**：若 `affectedRegionIds` 为空（结果与输入一致），**不调用** `onIntent`，视图回到输入布局。
6. **输入不被冒充为设计**：`onIntent` 是布局**提案**，不含 `operationId` / `revision`，不发 HTTP，不表示已保存。回调后视图以**宿主输入**为准。
7. **落位示意仅是临时状态**：手势过程中展示落位示意（目标新落位 + 让位者新位置），视觉上可区分。松手后模块回到输入布局，由宿主 `update(view)` 写入新视图。宿主未调用 `update` 时不保留内部位置。
8. **只读中途加锁**：`update({ readonly: true })` 发生在手势中途 → 立即取消手势、丢弃落位示意、**不发**完成回调。
9. **取消**：`pointercancel`、Escape 键、`destroy()` → 取消手势、丢弃落位示意、不发回调。
10. **`update(view)`**：`view = { items, readonly?, config? }`；替换输入视图并重绘；取消进行中的手势；`readonly` 缺省沿用当前值。若当前选中区域在新 items 中消失，调用 `onSelect(null)`；否则不调用 `onSelect`。
11. **`destroy()`**：释放本模块的事件监听、观察器与指针状态，只清理自有 DOM；之后不再响应任何指针事件，**不再触发任何回调**（无重复回调、无旧事件）。
12. **选择**：`onSelect(regionId)` / 取消选择时 `onSelect(null)`；重复点击同一区域不重复触发。
13. **作用域**：所有 CSS 限定在本模块根节点 `.rg-root` 之下，不影响全局工作台样式。
14. **内容插槽**：区域内容由 `renderContent(item, { regionId, selected, readonly })` 提供（可返回 `Node`、HTML 字符串或 `undefined`）。模块**不复制**完整报表 renderer，不自带 echarts。

## 4. DOM 契约（冻结，供 harness 与检查脚本定位）

| 选择器 | 含义 |
| --- | --- |
| `.rg-root` | 模块根节点（挂载后加在 `root` 上），带 `data-rg-root` |
| `[data-rg-editable="true"]` | 根节点处于编辑态 |
| `[data-rg-readonly="true"]` | 根节点处于只读态 |
| `.rg-grid` | 网格容器，带 `data-rg-grid` |
| `.rg-region` | 区域元素，带 `data-region-id="<id>"` 与 `data-rg-region="<id>"` |
| `.rg-region[data-rg-selected="true"]` | 选中态 |
| `.rg-region[data-rg-dragging="true"]` | 拖拽/缩放中的目标区域 |
| `.rg-drop-ghost` | 落位示意层，带 `data-rg-drop-ghost="<id>"` |
| `.rg-handle[data-rg-handle="e\|s\|se"]` | 缩放手柄（右侧 / 底部 / 右下角） |

**术语**：本模块把拖拽过程中显示的落位提示称为「**落位示意**」（DOM 类名 `.rg-drop-ghost`），
与 `GLOSSARY.md` 里的「报表预览」不是同一概念，不要混用；
harness 里的「本地视图」指前端内存中的 items 缓存，与「真实模型」无关。

区域用 CSS Grid 定位：`grid-column: var(--rg-column) / span var(--rg-column-span)`，
`grid-row: var(--rg-row) / span var(--rg-row-span)`。
**窗口变窄只重算渲染尺寸，不改坐标**（CSS Grid 自动重排，不重写 placement）。

## 5. 文件所有权（冻结）

| 路径 | 归属 |
| --- | --- |
| `frontend/modules/report-grid/CONTRACT.md` | 协调者（只读引用） |
| `frontend/modules/report-grid/layout/**` | **GA 布局计算 agent** |
| `frontend/modules/report-grid/tests/layout/**` | **GA** |
| `frontend/modules/report-grid/page/**` | **GB 页面与交互 agent** |
| `frontend/modules/report-grid/styles/**` | **GB** |
| `frontend/modules/report-grid/harness/**` | **GB** |
| `frontend/modules/report-grid/checks/**` | **GB** |
| `frontend/modules/report-grid/tests/page/**` | **GB** |

**双方都不得**：改 `package.json` / 锁文件 / 公共页面 / 后端 / `checks.json` / `flows/**` / 既有检查脚本；不得触碰 `frontend/modules/report-grid/` 以外任何文件；不得启动 FastAPI、访问模型或数据库。

## 6. 端口与资源

| 用途 | 端口 | 运行目录 |
| --- | --- | --- |
| GA 自检 | 8931 | `.agent-runtime/grid-prep-g/layout/` |
| GB 自检 | 8930 | `.agent-runtime/grid-prep-g/page/` |
| 协调者合并后组合验证 | 8930 | `.agent-runtime/grid-prep-g/combined/` |

启动前必须复查端口占用；冲突则报告，**不得**停止他人进程。
浏览器使用检查脚本自带的临时 Chrome profile 与随机 CDP 端口，不接管 9222 或他人页面。

## 7. 验证命令（自有 harness，不碰集成线）

检查产物的**默认输出目录**是仓库根的 `.selfcheck/report-grid/`（已被根 `.gitignore` 忽略，
不会污染 worktree）。任务期的日志、截图与报告请用 `--out-dir` 显式指向临时资源目录
`/Users/peiliu/Desktop/海悟/20261012 智能报表功能/.agent-runtime/grid-prep-g/`。

```bash
# 单元测试（Node 内置 test runner，无第三方依赖）
node --test frontend/modules/report-grid/tests/layout/*.test.mjs frontend/modules/report-grid/tests/page/*.test.mjs

# 静态自检
node /Users/peiliu/Desktop/海悟/.trae/skills/frontend-self-check/scripts/selfcheck.mjs \
  --config frontend/modules/report-grid/checks/selfcheck.json \
  --out-dir /Users/peiliu/Desktop/海悟/20261012\ 智能报表功能/.agent-runtime/grid-prep-g/combined/selfcheck

# 交互冒烟（选择 / 只读切换 / 重建）
node /Users/peiliu/Desktop/海悟/.trae/skills/frontend-interaction-check/scripts/interact.mjs \
  --config frontend/modules/report-grid/checks/interaction.flow.json \
  --out-dir /Users/peiliu/Desktop/海悟/20261012\ 智能报表功能/.agent-runtime/grid-prep-g/combined/interaction

# 真实鼠标拖拽 / 缩放（必须记录起终坐标、回调次数、JS 错误、截图）
node frontend/modules/report-grid/checks/drag-check.mjs \
  --out-dir /Users/peiliu/Desktop/海悟/20261012\ 智能报表功能/.agent-runtime/grid-prep-g/combined/drag
```

拖拽验证**不得**用直接调用布局方法、改 DOM 或注入最终快照冒充。断言通过 ≠ 拖拽通过。

`checks/drag-check.mjs` 自带一份精简 CDP 客户端，**不复用** `.trae/skills/_shared/cdp.mjs`：
后者在仓库之外，引用它需要把本机绝对路径写进仓库文件。保持自包含是有意的取舍，
后续若共享库迁入仓库，可改为 import 复用。

端口占用约定：三个检查脚本启动静态服务前都会复查端口；
**端口被占用时报错退出，不停止任何他人进程**（`drag-check` 还会确认自己起的子进程仍存活，
避免把别人的服务当成本次服务去驱动）。换端口请用 `--port`。

## 8. 不在本次范围

改标题、切图表类型、删除、自动保存、服务端版本/幂等/编辑锁、AI 计划、停止提交核对、模板绑定与数据预览、撤销/重做/版本恢复——全部留给后续工单接线。

本次**不显示**这些能力已可用。
