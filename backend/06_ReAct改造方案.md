# ReAct 改造方案（按状态机盘点 + 按 Agent 划分）

> 这份文档讨论的是**大模型的 ReAct 架构**（Reason + Act + Observation），不是前端的 React.js。
>
> 上游文档：
> - [05_功能盘点_按状态机.md](file:///Users/peiliu/Desktop/海悟/20261012%20智能报表功能/ai功能/backend/05_功能盘点_按状态机.md) —— 状态机事实基线，节点职责与待做项以它为准。
> - [04_AgentScope接入方案.md](file:///Users/peiliu/Desktop/海悟/20261012%20智能报表功能/ai功能/backend/04_AgentScope接入方案.md) —— 已有架构决策：AgentScope + 火山方舟 + 单 Agent + Skills/Tools。
>
> 阅读顺序：先看第 1 章结论 → 第 3 章按状态机逐节点盘点 → 第 4 章按 Agent 划分 → 第 5 章多 Agent 合理性 → 第 6~9 章工程约束与落地。

***

## 1. 结论速览

### 1.1 一句话结论

**我们已经在用 AgentScope 的 ReAct 内核，但业务上跑的是"伪 ReAct"**：工具调用顺序写死在 Prompt 里，Observation 没有真正决定下一步动作和最终参数。所以要改造的不是"接不接 ReAct"，而是"让 Observation 真正闭环"。

节点结论分三档：

- **完整 ReAct**：节点 1 / 3 / 4 / 8 —— 输入开放、结果依赖多步查证。
- **受约束 ReAct（理解与规划用 ReAct，执行仍用确定性引擎）**：节点 5.5 模板编辑 —— 用户说得含糊时需要查草稿、查能力才能理解，但改草稿这一步必须确定性。
- **不用 ReAct**：节点 5 / 6 / 7 / 9 / 10 / 11 / 12 —— 确定性分流与流水线。

### 1.2 "伪 ReAct"的三条证据（阶段 0 已处理）

| # | 证据 | 代码位置 | 后果 | 阶段 0 处理结果 |
| --- | --- | --- | --- | --- |
| 1 | Prompt 写死工具调用顺序：`search_catalog → search_metric → check_capability → build_definition_draft` | [report_generation.md](file:///Users/peiliu/Desktop/海悟/20261012%20智能报表功能/ai功能/backend/app/agents/prompts/report_generation.md) 的"工具清单 / 场景走法"段 | 模型是"填表"，不是"决策"，ReAct 退化成一次函数调用链 | ✅ 已修复：Prompt 改为"想 → 查 → 定"工作方式，不再规定顺序；工具清单只说"什么时候用"，场景段只给**参考路径**并明确"哪一步查不过就停下来" |
| 2 | `build_definition_draft` 的关键参数来自请求上下文或兜底值，不来自工具查询结果 | [agentscope_report_agent.py](file:///Users/peiliu/Desktop/海悟/20261012%20智能报表功能/ai功能/backend/app/agents/agentscope_report_agent.py) `_assemble_generate_response` | 查了目录和指标，但查询结果没有反向影响草稿内容，等于白查 | ✅ 已修复：新增 `submit_draft_plan` 终结工具，`reportName` / `roomId` / `includeTrend` 由模型查证后**主动提交**，不再从请求上下文兜底 |
| 3 | 工具 Observation 没有被消费：`check_capability` 的结果在最终组装阶段被丢弃 | 同上 | "分钟级不支持"这类判断只能靠 Prompt 文本硬编，而不是靠观察得出 | ✅ 已修复：Prompt 明确"工具返回 `supported: false` 就别硬上"；新增 `submit_blocked` 终结工具，拦截理由与替代方案由模型根据 Observation 产出 |

**还剩一条"半伪 ReAct"，阶段 1 处理**：

| # | 证据 | 代码位置 | 阶段 1 处理方案 |
| --- | --- | --- | --- |
| 4 | 工具返回的 Observation 只是"读到了"，没有**结构化落进最终产物**：`search_metric` 查到的指标口径 / 单位、`check_capability` 查到的 `supportedGrains`，都不出现在草稿里 | [build_definition_draft_tool.py](file:///Users/peiliu/Desktop/海悟/20261012%20智能报表功能/ai功能/backend/app/tools/build_definition_draft_tool.py) 只接受 `reportName` / `roomId` / `includeTrend` | 扩展 `submit_draft_plan` 载荷为带 `regions` 的完整结构（`role` / `metricRef` / `sourceRef` / `componentType` / `grainUnit` / `timeWindow`），由 `build_definition_draft` 按结构组装，让"查到的"变成"写进草稿的" |

### 1.3 节点级结论一览

| 节点 | 状态 | 是否需要 ReAct | 承载者 | 优先级 |
| --- | --- | --- | --- | --- |
| 1 | `ANALYZING` | **需要（核心）** | 业务 Agent | P0 |
| 2 | `NEEDS_CLARIFICATION` | **部分需要**（查了再问 / 查了再回填） | 业务 Agent | P1 |
| 3 | `BLOCKED_OR_UNSUPPORTED` | **需要**（自主产拦截理由与替代方案） | 业务 Agent | P1 |
| 4 | `DEFINITION_DRAFT_READY` | **需要（核心）**（AI 自产修改方案 / ChangeSet） | 业务 Agent | P0 |
| 5 | `WAITING_USER_CONFIRM` | 不需要（确定性分流） | AgentService | — |
| 5.5 | `USER_EDITING_TEMPLATE` | **受约束 ReAct**（理解与规划用 ReAct，执行用确定性引擎） | 业务 Agent（编辑模式）+ TemplateEditEngine | P1 |
| 6 | `SAVE_DEFINITION_DRAFT` | 不需要 | AgentService + Java | — |
| 7 | `VALIDATING` | 不需要 | 校验服务 | — |
| 8 | `VALIDATION_FAILED` | **需要**（读问题 → 定修复 → 重校验闭环） | 业务 Agent | P1 |
| 9 | `PREVIEWING` | 不需要 | 预览引擎 | — |
| 10 | `PREVIEW_READY` | 不需要（可选一次性解读调用） | 规则 / 单次 LLM | — |
| 11 | `RENDERING` | 不需要 | 前端 | — |
| 12 | `READY` | 不需要 | AgentService | — |

### 1.4 多 Agent 的结论

**现在放 4 类 Agent（Concierge / Deep Route / 业务 Agent / Template Edit）数量上是合理的，但它们不是同一个层级的 4 个 Agent。** 具体判断：

| 角色 | 现在的形态 | 本质 | 建议 |
| --- | --- | --- | --- |
| Concierge | 独立 LLM 调用 | **分类器**（轻量接待） | 保留，但明确它是"快路径分类器"，不是 Agent |
| Deep Route | 独立 LLM 调用 | **路由器**（单次结构化决策） | 保留，但明确它是"路由器"，不是 Agent |
| 业务 Agent | AgentScope Agent + Skills | **唯一的 ReAct 承载者** | 承载全部 ReAct 职责（生成 + 编辑模式） |
| Template Edit | 规则引擎 + 语义兜底 | **确定性执行引擎 + 受约束 ReAct 规划器** | 执行体是 Engine（改草稿必须确定性），但**理解与规划要用 ReAct**（用户说不清时需查证） |

**关键提醒**：Concierge 和 Deep Route 是**两段串行的模型调用**，叠加起来是一次请求要打两次 LLM。这是延迟和成本的主要来源，而不是"Agent 太多"。真正需要收敛的是"路由层"，不是"Agent 层"。

详见 [第 5 章](#5-多-agent-是否合理)。

***

## 2. 术语与判定标准

### 2.1 什么是"真 ReAct"

一次 ReAct 循环至少满足：

```text
Reason   : 模型根据「用户输入 + 已收集事实 + 当前状态」决定下一步动作
Act      : 调用一个工具
Observe  : 工具返回事实，进入上下文
→ 循环：下一次 Reason 必须被上一次的 Observe 改变
→ 终止：模型主动调用一个"提交结果"工具，或达到步数/预算上限
```

**判定一条链路是不是 ReAct，只需问一句：Observation 有没有改变下一次动作或最终参数？**
没改变 → 伪 ReAct（脚本化 CoT）。

### 2.2 三类环节的归属原则

| 环节类型 | 判定 | 归属 |
| --- | --- | --- |
| **不确定的语义决策** | 需要理解、可能有多种路径、结果影响后续 | LLM / Agent |
| **确定的事实查询与动作执行** | 参数明确、结果唯一、可复现 | Tool / Service |
| **硬约束与规则校验** | 权限、状态机、schema、必选锁 | 代码 / Pydantic |

一句话：**LLM 只产候选计划，事实和最终结构由目录 / 指标 / 能力服务和 Pydantic / 业务规则校验。**

### 2.3 状态机边界（不可动摇）

不管怎么改造，以下三条继续由后端代码执行，不交给模型：

1. `ALLOWED_TRANSITIONS` 状态转移白名单（[agent.py](file:///Users/peiliu/Desktop/海悟/20261012%20智能报表功能/ai功能/backend/app/schemas/agent.py) 的 `ALLOWED_TRANSITIONS`）——在 [agent_service.py](file:///Users/peiliu/Desktop/海悟/20261012%20智能报表功能/ai功能/backend/app/services/agent_service.py) 的 `chat()` 末尾校验。
2. 必选下拉锁：未提交合法选项时不允许自由文本绕过（[agent_service.py](file:///Users/peiliu/Desktop/海悟/20261012%20智能报表功能/ai功能/backend/app/services/agent_service.py) 的 pending choice 分支）。
3. 保存 / 校验 / 预览 / 渲染的自动推进（`_auto_advance`），见 [agent_service.py](file:///Users/peiliu/Desktop/海悟/20261012%20智能报表功能/ai功能/backend/app/services/agent_service.py)。

### 2.4 谁是谁：AgentService 本质就是一个 ChatOrchestrator

> 这一节专门澄清一个容易混淆的点。**`AgentService` 不是 AgentScope 实现的，也不是一个 Agent。**

**它是什么**：我们自己写的代码，本质是一个 **ChatOrchestrator（对话编排器）**，也就是"你画的状态机的执行器"。

状态机只规定了**有哪些状态、状态之间能怎么跳**；但流程本身不会自己跑，得有人执行 —— `AgentService` 就是那个人。

**它不是什么**：

- **不是 AgentScope 的东西**。整个项目 import AgentScope 的只有 2 个文件：[agentscope_report_agent.py](file:///Users/peiliu/Desktop/海悟/20261012%20智能报表功能/ai功能/backend/app/agents/agentscope_report_agent.py)（`Agent` / `VolcengineChatModel` / `Msg`）和 [tools/registry.py](file:///Users/peiliu/Desktop/海悟/20261012%20智能报表功能/ai功能/backend/app/tools/registry.py)（`Toolkit` / `FunctionTool`）。`agent_service.py` 里一行 AgentScope 都没用。
- **不是 Agent**。它不思考、不调模型、不理解语义，只做分发和校验。
- **不是状态机本身**。状态机是数据（`ALLOWED_TRANSITIONS` 白名单 + `AgentState` 枚举），`AgentService` 是执行这份数据的代码。

**名字为什么容易误导**：`AgentService` 里的 "Agent" 指的是"AI 对话功能"这个业务域，不是"Agent 这个技术角色"。更准确的名字是 `ChatOrchestrator` 或 `ReportFlowOrchestrator`，但改名会牵动 import，**建议不改名**，只在文档和交接时说清楚它不是 Agent。

**完整调用链**：

```text
前端 POST /api/agent/chat
  ↓
routers/agent.py              ← 仅 HTTP 入口
  ↓
agent_service.py（ChatOrchestrator · 我们自己写的 · 确定性）
  ├─ 协议版本检查
  ├─ 必选下拉锁（未选 → 顶回去）
  ├─ __advance__ → 保存 / 校验 / 预览 / 渲染（无模型）
  ├─ TemplateEditEngine（规则优先，模型兜底）
  ├─ ContextEngine 路由（Concierge → Deep Router）
  └─ 业务 Agent          ← AgentScope 只出现在这一层
       ├─ AgentScopeReportAgent（真 ReAct，跑 AgentScope 内核）
       └─ FakeReportAgent（关键词规则，降级用）
  ↓
ALLOWED_TRANSITIONS 校验（不合法 → INVALID_STATE_TRANSITION）
```

**一句话**：**AgentScope 是这条链最末端的一个可替换零件；`AgentService` 是把状态机写成代码的那层胶水。** 换掉业务 Agent 的实现（真模型 / Fake / 别的框架），`AgentService` 一行都不用改 —— 这也是为什么 ReAct 改造全部收敛在业务 Agent 内部。

***

## 3. 按状态机逐节点盘点

> 每个节点统一按六个维度写：**是否需要 ReAct → 理由 → 承载者 → 工具 / Observation → 结构化终结 → 确定性边界与降级**。

---

### 节点 1 · `ANALYZING`（理解用户意图）

**是否需要 ReAct：需要，且是全部节点里最核心的一个。**

**理由**

这是唯一一个"输入高度不确定、但最终产物高度确定"的节点。输入侧用户可能说"A 机房昨天能耗"、可能说"把能耗日报改成最近 7 天"、可能说"你们这支持分钟级吗"；输出侧永远是一份符合 schema 的 `ReportDefinitionDraft`。这种"输入开放、输出封闭"的场景正是 ReAct 的适用区。

更关键的是 05 文档定下的**"先看模版"原则**：用户没给信息时，也应该先查授权且可用的目录和能力，而不是让模型猜。这天然要求"查 → 看结果 → 再决定"的多步循环。

**当前实现的问题（阶段 0 已修）**

- ~~工具顺序写死在 Prompt~~ → ✅ 已改为"想 → 查 → 定"，不再规定顺序。
- ~~业务 Agent 输入只有 6 个上下文字段，没有当前草稿、没有 `RouteDecision.collectedInfo`~~ → ✅ 已补：`_compose_user_message` 现在带**当前草稿摘要 + 路由已收集信息 + 用户选中的区块**（见 [4.3](#43-业务-agentagentscopereportagent)）。
- ~~每次 `_build_agent()` 都新建 Agent，跨轮无记忆~~ → ⚠️ **部分修复**：`collectedInfo` 已落 Session snapshot 跨轮恢复；但草稿本身仍靠前端带回的快照，不是后端缓存。

**承载者**：业务 Agent（AgentScope `Agent`）

**工具与 Observation**

| 工具 | 用途 | Observation 形态 | 边界 |
| --- | --- | --- | --- |
| `search_catalog` | 查授权且可用的数据源 | `{items: [{sourceRef, sourceVersion, subjectRef, supportedGrains, fields}]}` | 真实接入后必须分页 / 裁剪 |
| `search_metric` | 查指标口径与单位 | `{items: [{metricRef, metricVersion, unit, dimensions}]}` | 同上 |
| `check_capability` | 查粒度 / 编译能力 | `{sourceRef, grainUnit, supported: bool, supportedGrains: [...]}` | 只允许返回登记过的能力 |
| `get_current_draft` | 读当前草稿快照 | `{regions: [{id, title, role, componentType, metricRef, sourceRef, timeWindow}]}` | 只读 |
| `submit_draft_plan` | **终结工具**：提交结构化计划 | `{reportName, regions: [{role, metricRef, sourceRef, componentType, chartKind, timeWindow, grainUnit}]}` | 提交后立即终止循环 |

Observation 有界化要求：目录和指标在真实规模下可能是几百条。返回必须**分页 + 裁剪 + 计数**（例如 `{"items": [...20条], "total": 312, "truncated": true}`），否则上下文直接爆炸。

**结构化终结**

新增 `submit_draft_plan` 终结工具，替代当前的"最终输出自由文本 JSON"（[agentscope_report_agent.py](file:///Users/peiliu/Desktop/海悟/20261012%20智能报表功能/ai功能/backend/app/agents/agentscope_report_agent.py) 的 `_parse_plan` 回落路径）。

终结工具相比 AgentScope `structured_schema` 的优势：可以**在同一次调用里同时表达"结果"和"依据"**，并且能被后端直接捕获去走确定性组装链路。

```text
submit_draft_plan(
  reportName: str,
  regions: [
    { role, metricRef, sourceRef, componentType, chartKind?, timeWindow?, grainUnit? }
  ],
  reply: str,
  usedDefaults: [ { field, value, reason } ]     # 系统暂选项，节点 4 要展示
)
```

`usedDefaults` 是新增字段，对应 05 文档节点 1 第 7 条"标明用户指定项与系统暂选项"。

**确定性边界与降级**

- 提交后由 `build_definition_draft` 确定性函数组装完整 draft（沿用现有 [build_definition_draft_tool.py](file:///Users/peiliu/Desktop/海悟/20261012%20智能报表功能/ai功能/backend/app/tools/build_definition_draft_tool.py) 的做法），模型不直接产 `ReportDefinitionDraft` JSON。
- `metricRef` / `sourceRef` 必须能在草稿查询结果中命中，命中不了就退回追问，不允许编造。
- LLM 不可用或超时时降级 `FakeReportAgent`（现状保留）。但 `FakeReportAgent` 只作为降级和 pytest 基准，**不再往里加新业务规则**。

**优先级：P0**（阶段 0 的主战场）

---

### 节点 2 · `NEEDS_CLARIFICATION`（信息不足追问）

**是否需要 ReAct：部分需要。**

**理由**

节点 2 本身是"展示追问卡片 + 收回答案"，这部分完全不需要 LLM。但**"到底缺什么"这个判断需要 ReAct 支撑**：是缺对象？缺时间范围？缺指标？还是"其实不缺，随便给个默认值就能生成"？

05 文档第 7 条已经写明：**零信息时也应先查可用候选**。现在的实现是 Deep Router 单次输出 `missingInfo`，没查过任何东西就断言缺什么。

**承载者**：业务 Agent（在节点 1 内部完成"查完再问"），AgentService 只负责渲染 `questions`

**工具与 Observation**

复用节点 1 的只读工具。关键变化是**新增一个终结方式**：

```text
submit_clarification(
  missingInfo: [ {field, label, type: SINGLE_CHOICE|MULTI_CHOICE|TIME_RANGE|TEXT, options?: [...]} ],
  nextQuestion: str,
  autoFillable: { field, value, reason }      # 可以用默认值直接生成的项
)
```

`autoFillable` 让"先看模版"路径成立：某些字段有唯一合理默认值时，模型直接填并标注为系统暂选，而不是硬追问。

**结构化终结**：`submit_clarification`，取代现在 Deep Router 的 `missingInfo` / `nextQuestion` 自由结构。

**确定性边界与降级**

- 追问最多 2 轮（05 节点 2 第 7 条），由 AgentService 计数，超限直接进入"用默认值生成"。
- 必选下拉的选项值必须来自目录 / 指标查询结果，**不允许模型自造选项值**。
- 单选 / 多选 / 日期范围 / 文本四种控件的 `type` 由后端映射，前端只渲染。

**优先级：P1**（阶段 0 顺带做，因为和节点 1 是同一次循环）

---

### 节点 3 · `BLOCKED_OR_UNSUPPORTED`（权限 / 能力拦截）

**是否需要 ReAct：需要。**

**理由**

05 节点 3 第 3 条明确写了"**让 AI 自己产出拦截结果，而不是降级到 FakeAgent**"——这是待做项。

现在的拦截是关键词硬编的：`FakeReportAgent` 里 `_PAT_CROSS_PROJECT` / `_PAT_MINUTE` / `_PAT_ANOMALY_ONLY` 三个正则，替代方案更是写死的 `["改为小时趋势", "改为日趋势"]`。

真 ReAct 该做的事：**先查，再拦，再给路**。

```text
用户要分钟级 → 查能力(check_capability, MINUTE) → 发现不支持
            → 查这个源支持哪些粒度(supportedGrains)
            → 生成"改成小时级 / 改成天级"这种从真实能力清单里长出来的替代方案
```

**承载者**：业务 Agent

**工具与 Observation**

| 工具 | 用途 |
| --- | --- |
| `check_capability` | 判断目标粒度是否支持 |
| `search_catalog` | 查替代数据源 |
| `search_metric` | 查替代指标 |
| `submit_blocked` | **终结工具**：拦截理由 + 替代方案 |

```text
submit_blocked(
  layer: PERMISSION | CAPABILITY | UNSUPPORTED,
  reason: str,
  alternatives: [
    { label, kind: GRAIN|SOURCE|METRIC|SCOPE, payload: {...}, groundedBy: "tool_name" }
  ]
)
```

**确定性边界与降级**

- `alternatives` 每一项的 `payload` 必须能在工具返回结果中找到对应实体；`groundedBy` 记录来源工具，后端做一次回查校验，对不上就丢弃该项。
- 权限拦截只允许 `PERMISSION` 层由后端权威判定，模型只负责解释。
- 没有可用替代时 `alternatives` 为空，前端显示"结束会话"。

**优先级：P1**（阶段 2）

---

### 节点 4 · `DEFINITION_DRAFT_READY`（草稿或修改方案已生成）

**是否需要 ReAct：需要，和节点 1 并列核心。**

**理由**

这是 05 节点 4 的第 2、3 条待做项：**AI 产"修改方案"**（例如改 7 天、删趋势图）。这件事的本质是：

```text
读旧定义（哪些 region、什么指标、什么时间窗）
  → 理解用户诉求（要改哪块、改成什么）
  → 查新值是否可用（这个指标存在吗？这个粒度支持吗？）
  → 产出改动清单（ChangeSet）
```

这是一个天然的 4 步 ReAct 循环，而且每一步的 Observation 都会改变下一步。

**当前实现的问题**

`FakeReportAgent` 里 `_PAT_TREND_7D` / `_PAT_REMOVE_TREND` 两条正则，加上 `report_draft_service` 里写死的 `build_change_set_trend_7d` / `build_change_set_remove_trend`。只能改两种，且改法是固定的。

**承载者**：业务 Agent

**工具与 Observation**

| 工具 | 用途 |
| --- | --- |
| `get_current_draft` | 读旧定义（必须给 regionId / title / role / metricRef / sourceRef / timeWindow / grain） |
| `search_metric` | 校验新指标存在且有权限 |
| `check_capability` | 校验新粒度 / 时间窗支持 |
| `submit_change_plan` | **终结工具**：产出改动清单 |

```text
submit_change_plan(
  baseDefinitionRevision: int,
  reportName?: str,
  operations: [
    { op: ADD_REGION|REMOVE_REGION|MODIFY_REGION,
      regionId?, metricRef?, sourceRef?, componentType?, chartKind?,
      timeWindow?, grainUnit?, title?,
      reason: str }        # 面向用户的"为什么改"
  ],
  reply: str
)
```

`reason` 字段直接喂给前端的 diff 三栏视图（旧 / 新 / 原因），这是 05 节点 4 第 6 条要的效果。

**确定性边界与降级**

- ChangeSet 的**最终形态由确定性代码生成**（沿用 `report_draft_service` 的组装模式），模型只产 `operations`，后端算 diff 路径、算 revision、写 `ChangeSet`。
- `operations` 里的 `metricRef` / `sourceRef` 必须在工具结果中命中；`baseDefinitionRevision` 不匹配时拒绝并提示重新拉取。
- 修改方案不落库，和草稿一样只在浏览器侧持有，直到用户确认。

**优先级：P0**（阶段 1，和节点 1 一起构成"生成 + 修改"两大能力）

---

### 节点 5 · `WAITING_USER_CONFIRM`（等待用户确认）

**是否需要 ReAct：不需要。**

**理由**

这个节点只做三件事：展示"同意 / 修改 / 重新开始"必选下拉、把草稿快照带回后端、在用户选择后决定下一个状态。全是确定性分流。

**承载者**：AgentService

**边界**

- 必选下拉锁（选项未提交时拒绝自由文本）继续在 [agent_service.py](file:///Users/peiliu/Desktop/海悟/20261012%20智能报表功能/ai功能/backend/app/services/agent_service.py) 的 pending choice 分支执行，不交给模型。
- "重新开始"→ 清空草稿回到节点 1；"修改"→ 节点 5.5；"同意"→ 节点 6。
- 05 节点 5 的待做项 8（点"重新开始"清空草稿）属于产品逻辑，不是 ReAct 问题。

**优先级：不改**

---

### 节点 5.5 · `USER_EDITING_TEMPLATE`（用户编辑模版）

**是否需要 ReAct：需要，但分两层 ——「理解与规划」用 ReAct，「执行」保持确定性。**

**理由**

早期链路是：

```text
规则解析 (TemplateEditEngine.parse)
  → 规则不够 → 语义兜底 (ContextEngine.build_template_edit_plan)
  → 归一化话术再走一遍规则解析
  → grounded 校验 (_is_grounded_edit)
  → 确定性应用 (TemplateEditEngine.apply)
```

生产路径现已改为规则快路径 + 业务 Agent 编辑模式 ReAct；上述一次性规范化代码保留兼容，但不再作为生产编辑兜底。

**用户说不清楚的时候，纯规则引擎确实不够。** 规则能匹配的前提是句式已知；一旦用户用指代（"那个耗电图"）、用模糊偏好（"改好看点"）、或者一次改好几件事，规则就落到 `UNKNOWN`，只能干巴巴地追问。

**关键是要把"说不清楚"分成四类**（这决定了该用规则、该用 ReAct、还是该停下来问）：

| 用户说法 | 属于 | 该怎么处理 |
| --- | --- | --- |
| "删掉趋势图"，目标唯一 | ① 规则可解 | 规则直接生成 `EditPlan`，**不调模型** |
| "把那个耗电图改成小时级" | ② **需要查证** | **ReAct**：查当前有哪些图 → 查该数据源是否支持小时粒度 → 查该源下可用指标 → 产出候选 `EditPlan` |
| "这个图改好看点" | ③ **真正不明确** | ReAct 可以查当前图表和可用展示形式，但"好看"有多种解释 → **给选项追问，不擅自改** |
| 完全跑题 / 胡言乱语 | ④ 不构成编辑指令 | 不强行解释成编辑，澄清或交回路由层 |

结论：

- **ReAct 解决的是第 ② 类** —— 需要查事实、补上下文才能理解的模糊。
- **ReAct 代替不了用户决定第 ③ 类** —— 真正的偏好歧义只能追问。
- 第 ① 类继续走规则，不进模型（又快又准）。
- 第 ④ 类不该被编辑链路硬吞。

**承载者**：业务 Agent（编辑模式，规划）+ TemplateEditEngine（执行）

**工具与 Observation**

| 工具 | 用途 |
| --- | --- |
| `get_current_region` | 读单个 region 详情（不是整个草稿） |
| `get_current_draft` | 读草稿概览（有哪些 region / 图表 / 指标） |
| `search_catalog` | 查目标数据源 |
| `search_metric` | 查该源下的可用指标 |
| `check_capability` | 查粒度支持 |
| `submit_edit_plan` | **终结工具**：产出候选 `EditPlan` 或追问方案 |

```text
submit_edit_plan(
  intent: EditIntent,
  target: { regionId, matchedText, confidence },
  operation: EditOperation,
  needsClarification: bool,
  clarificationQuestion: str,
  options?: [{ value, label }]  # 歧义时给出可选话术
)
```

`options` 字段是新增的：第 ③ 类情况下模型把可能的解释列成选项交给用户选，而不是写一句"你想怎么改"。

**结构化终结**：`submit_edit_plan` 的参数直接就是现有 [edit_plan.py](file:///Users/peiliu/Desktop/海悟/20261012%20智能报表功能/ai功能/backend/app/schemas/edit_plan.py) 的 `EditPlan` 结构 + 两个补充字段，不需要新的 schema。

**确定性边界与降级（这一条不能松）**

- **模型只产候选 `EditPlan`，永远不直接改草稿。**
- `submit_edit_plan` 之后，由确定性层验证标题确实来自原话、目标存在、展示形态和目录/指标授权、字段/粒度兼容，再由 `TemplateEditEngine.apply()` 修改。旧 `_is_grounded_edit` 仅服务于已停用的一次性规范化路径，不能当作新 ReAct 计划的完整护栏。
- 目标 region 必须能在当前草稿中找到；凭空指定的 regionId 一律拒绝。
- **规则优先原则保留**，但它的定位要修正：**规则是"快路径"，不是"能力上限"**。规则能直接确定的不走模型；规则定不下来就升级到 ReAct 查证，而不是直接抛追问。
- ReAct 查证后仍然定不下来的（第 ③ 类），才产出带 `options` 的追问。
- 多轮缺参自动合并（05 节点 5.5 的待做项）正好由 ReAct 的跨轮上下文解决，但状态必须外置到会话，不能只靠 AgentScope Agent 内存。

**优先级：P1**（阶段 1）

---

### 节点 6 · `SAVE_DEFINITION_DRAFT`（保存草稿）

**是否需要 ReAct：不需要。**

**理由**：保存是幂等的状态转移 + 副作用调用，没有不确定性决策空间。

**承载者**：AgentService + `DefinitionSaveService`（后续换 Java）

**边界**：保存成功后发 `SAVE_DEFINITION_DRAFT` + `VALIDATE_DEFINITION` 两个 toolRequest，自动推进到节点 7。这条链路一行模型都不用碰。

**优先级：不改**

---

### 节点 7 · `VALIDATING`（后端校验定义）

**是否需要 ReAct：不需要。**

**理由**：5 层结构化校验（结构 / 字段 / 指标 / 权限 / 能力）每层的判定都是确定的布尔判断。交给模型只会引入"看起来对但其实错"的校验。

**承载者**：校验服务（当前 Python 本地，未来 Java）

**优先级：不改**

---

### 节点 8 · `VALIDATION_FAILED`（校验失败待修正）

**是否需要 ReAct：需要。**

**理由**

这是 05 节点 8 第 3 条的待做项："**「采纳建议修改」按钮一键改**"。

真 ReAct 的形态是闭环：

```text
读校验问题（结构化 ValidationIssue 列表）
  → 判断哪条能自动修、哪条要改定义、哪条只能问用户
  → 查可行解（这个指标有没有替代？这个粒度支持吗？）
  → 产出修复方案
  → 重新校验
  → 通过就回节点 7 继续，失败再循环（有上限）
```

**承载者**：业务 Agent（修复方案）+ 校验服务（重新校验）

**工具与 Observation**

| 工具 | 用途 |
| --- | --- |
| `get_validation_result` | 读结构化校验问题 |
| `search_metric` / `search_catalog` / `check_capability` | 为错误项找可行替代 |
| `submit_fix_plan` | **终结工具**：产出修复方案 |

```text
submit_fix_plan(
  fixes: [ { issueId, action: REPLACE_METRIC|CHANGE_GRAIN|REMOVE_REGION|ASK_USER, payload: {...}, reason } ],
  remainingQuestions: [ { issueId, question } ]
)
```

**确定性边界与降级**

- 修复方案不直接写库，必须**重新走一遍校验**才允许推进，这是 ReAct 循环的收敛条件。
- 修复轮数上限 **2 轮**，超限转 `ASK_USER`。
- `ASK_USER` 的问题必须来自后端可枚举的合法选项（和必选下拉锁配合）。
- 权限类错误不可自动修复，只能解释 + 走拦截。

**优先级：P1**（阶段 2，与节点 3 同批）

---

### 节点 9 · `PREVIEWING`（发起预览运行）

**是否需要 ReAct：不需要。**

**理由**：给定定义 → 跑预览 → 产数据。完全确定。

**承载者**：预览引擎（当前 `mock_data_service`）

**优先级：不改**

---

### 节点 10 · `PREVIEW_READY`（预览结果就绪）

**是否需要 ReAct：不需要（最多一次无工具的 LLM 调用）。**

**理由**：这里只是把预览数据讲成人话——总耗电、同比、用电大户、峰值时段、数据质量提示。这是**纯生成任务，没有工具调用需求**，ReAct 的循环在这里没有意义。

**承载者**：规则生成（当前）/ 单次 LLM 调用（未来）

**边界**：LLM 只能读 `QualitySummary` 和预览数据做转述，**不允许引入预览数据里没有的数字**。这条要靠"只给数据不给工具"来保证——没有工具就没有新的 Observation 来源。

**优先级：不改**（可选优化：把规则解读换成一次 LLM 调用，体验更好）

---

### 节点 11 · `RENDERING`（渲染报表预览）

**是否需要 ReAct：不需要。**

**理由**：前端 ECharts 渲染，AI 不参与。

**优先级：不改**

---

### 节点 12 · `READY`（完整预览完成）

**是否需要 ReAct：不需要。**

**理由**：必选下拉（继续修改 / 发布 / 导出）+ 状态保持。

**优先级：不改**

***

## 4. 按 Agent 划分盘点

> 状态机视角回答"每个节点谁来做"，这一章回答"每一类 Agent 自己该怎么改造"。

---

### 4.1 Concierge（轻量接待员）

**定位**：每条用户消息进来后的第一道判断，产出 `ConciergeDecision`，只有两个取值：`DIRECT_REPLY`（活人感回复，不改业务状态）和 `DEEP_ROUTE`（交给重路由）。

**当前实现**

[context_engine.py](file:///Users/peiliu/Desktop/海悟/20261012%20智能报表功能/ai功能/backend/app/services/context_engine.py) 的 `_run_concierge`。独立一次 `_chat_json_stream`，`temperature=0.2`，`response_format={"type":"json_object"}`，超时 `VOLC_CONTEXT_CONCIERGE_TIMEOUT`（默认 30s）。

**是否需要 ReAct：不需要，而且不应该有。**

**理由**

1. Concierge 的决策空间是**封闭的两分类**，不存在"多步探索才能决定"的成分。
2. 给它任何工具都等于让它去做 Deep Router 的事，职责立刻糊掉。
3. 它现在的价值是**快**——"你好 / 在吗 / 你能做什么"这类不该触发业务处理的输入在这里被拦掉，省掉后面所有成本。

**改造建议**

| 项 | 建议 |
| --- | --- |
| 模型 | 保持独立配置（`VOLC_CONTEXT_CONCIERGE_MODEL`），允许用更小的模型 |
| 温度 | `0.2` → `0`，分类任务不需要发散 |
| 超时 | 30s → 10s。Concierge 是快路径，超时应该比业务 Agent 更短，快速失败后直接走 DEEP_ROUTE |
| 输出 | 已经是结构化 `ConciergeDecision`，保持 |
| 失败处理 | 现状已经是"失败即 DEEP_ROUTE"，合理，保持 |

**唯一需要留意的一点**：Concierge 和 Deep Route 是**串行两次模型调用**。真实耗时是两次之和。在改造前应该先量一下这个叠加成本，再决定要不要合并（见 [5.3](#53-收敛建议)）。

---

### 4.2 Deep Route（重路由器）

**定位**：产出结构化 `RouteDecision`——判断是否打断流程、是否缺信息、目标 state 是什么、要不要追问。

**当前实现**

[context_engine.py](file:///Users/peiliu/Desktop/海悟/20261012%20智能报表功能/ai功能/backend/app/services/context_engine.py) 的 `route`。`temperature=0`，`response_format=json_object`，超时 `VOLC_CONTEXT_ROUTER_TIMEOUT`（默认 30s）。`_compose_input` 只给**前 12 个 region 的摘要**。

**是否需要 ReAct：不需要作为路由器；但需要工具支撑它的"缺信息"判断。**

**理由**

路由决策本身是**一次性分类**：给定 state + message + context，输出一个 `RouteDecision`。这是分类器，不是 Agent。

但 05 文档节点 1 第 7 条要求"零信息时也先查可用候选"。这意味着路由阶段（尤其是判定"缺什么信息"时）需要事实输入。两种做法：

| 做法 | 说明 | 评价 |
| --- | --- | --- |
| A. 给 Deep Route 加只读工具，做 1-2 步 ReAct | 路由阶段可能打一次甚至多次模型 | ❌ 路由是热路径，加循环会把延迟放大到不可接受 |
| B. Deep Route 只做粗判，把"缺什么"交给业务 Agent 的 ReAct 去查 | 路由只判断"要不要问、要不要打断"，具体缺项由业务 Agent 查完再定 | ✅ **推荐** |

**改造建议**

| 项 | 建议 | 状态 |
| --- | --- | --- |
| 职责收窄 | `requiredInfo` / `missingInfo` 从"断言缺什么"降级为"提示可能缺什么"；真实缺项由业务 Agent 查证 | 待做 |
| `collectedInfo` 传递 | ~~只进 Debug Trace，没传给业务 Agent~~ → **已打通**：路由后写回 `request.collectedInfo`，业务 Agent 的 `_compose_user_message` 以"已收集的信息"进模型输入 | ✅ 已完成 |
| `collectedInfo` 跨轮 | 路由决策前从 Session snapshot 恢复历史槽位，路由后把**历史槽位 + 本轮新槽位**合并写回快照 | ✅ 已完成 |
| 草稿摘要 | `_summarize_definition` 截断到 12 个 region，改成"优先给用户正在编辑的那个 region + 其余计数"，避免截断掉关键信息 | 待做 |
| `CONTINUE_CURRENT` 语义 | ~~直接 `return None` 放行~~ → **已改为放行并附带路由收集到的信息** | ✅ 已完成 |

**`collectedInfo` 的字段定位（重要）**：它是**服务端内部中间产物**，不是客户端协议字段。

- 语义上属于 Deep Route 的产物，只是"顺手"喂给业务 Agent；
- 在 `AgentRequest` 上用 Pydantic `PrivateAttr + property` 实现（[agent.py](file:///Users/peiliu/Desktop/海悟/20261012%20智能报表功能/ai功能/backend/app/schemas/agent.py)）：
  - 客户端提交 `collectedInfo` **不生效**（不是 Pydantic 字段，额外键被忽略）；
  - 只有后端在路由后能赋值；
  - `model_dump()` **不暴露**，不会外泄到协议响应。
- 所以 [01_接口协议.md](file:///Users/peiliu/Desktop/海悟/20261012%20智能报表功能/ai功能/backend/01_接口协议.md) 的 `AgentRequest` 字段表**不需要**加它。

---

### 4.3 业务 Agent（AgentScopeReportAgent）

**定位**：唯一真正跑 AgentScope ReAct 内核的角色。

**当前实现**

[agentscope_report_agent.py](file:///Users/peiliu/Desktop/海悟/20261012%20智能报表功能/ai功能/backend/app/agents/agentscope_report_agent.py)：
- 每次 `handle` 新建一个 Agent + Toolkit（AgentScope Agent 有对话内部状态，不适合跨请求复用）。
- `agent.reply(user_msg)` 跑 AgentScope 的 ReAct 内核；优先消费**终结工具提交槽**，没有提交才回落到旧的文本 JSON 解析；解析失败修复重试一次。
- Toolkit 按**当前节点**筛选工具（`tools_for_state(request.state)`），模型看不到这个节点不该调的工具。
- 输入除了用户消息，还带**当前草稿摘要 + 路由已收集信息 + 用户选中的区块**。
- 异常和初始化失败都降级 `FakeReportAgent`。

**是否需要 ReAct：需要，这就是要改造成"真 ReAct"的主体。**

**改造清单（阶段 0）**

| # | 改造项 | 现状 | 目标 | 状态 |
| --- | --- | --- | --- | --- |
| 1 | 去掉写死的工具顺序 | Prompt 明确写死 | 改为"根据缺失信息自主选择工具，Observation 决定下一步" | ✅ 已完成 |
| 2 | 增加终结工具 | 靠最终文本 JSON + `_parse_plan` | 6 个 `submit_*` 终结工具 | ✅ 已完成 |
| 3 | 工具集分层 | 4 个工具全是 `is_read_only=True` | 终结工具单独标记权限语义 | ✅ 已完成 |
| 4 | 输入补全 | 只有 6 个字段 | 加上：当前草稿摘要、`RouteDecision.collectedInfo`、用户选中的区块 | ✅ 已完成（"最近 N 轮对话摘要"待做） |
| 5 | 跨轮记忆 | 每次新建 Agent，跨轮无状态 | 会话态外置（见 [6.2](#62-跨轮记忆与状态外置)） | ⚠️ 部分：`collectedInfo` 已落 Session snapshot；草稿仍靠前端带回 |
| 6 | 步数与预算 | 无限制（AgentScope 默认 `max_iters=50`） | `ReActConfig(max_iters=...)` 显式设小值 | ✅ 已完成 |
| 7 | Observation 裁剪 | `search_catalog` 返回完整 items | 分页 + 裁剪 + 总数标记 | ✅ 已完成 |
| 8 | 降级策略 | 降级 `FakeReportAgent` | 保留，但 `FakeReportAgent` 冻结为降级 + 测试基准 | ✅ 已完成 |
| 9 | 节点级工具白名单 | 所有节点工具集相同 | 按 `request.state` 筛选后再注册进 Toolkit | ✅ 已完成 |
| 10 | 项目授权边界 | 模型可查任意数据源 | 工具层按 `projectId` / `subjectId` 过滤，越权直接查不到 | ✅ 已完成 |

**工具集（已落地）**

13 个工具已全部实现，统一收在 [app/tools/](file:///Users/peiliu/Desktop/海悟/20261012%20智能报表功能/ai功能/backend/app/tools) 目录，**一个工具一个文件**：

| 工具 | 分类 | 用途 | 状态 |
| --- | --- | --- | --- |
| `search_catalog` | FACT | 查授权且可用的数据源 | ✅ 已实现 |
| `search_metric` | FACT | 查指标口径、单位、维度 | ✅ 已实现 |
| `check_capability` | FACT | 查粒度 / 编译 / 渲染能力 | ✅ 已实现 |
| `get_current_draft` | FACT | 读当前草稿概览 | ✅ 已实现 |
| `get_current_region` | FACT | 读单个 region（编辑语境） | ✅ 已实现 |
| `get_validation_result` | FACT | 读结构化校验问题 | ✅ 已实现 |
| `build_definition_draft` | BUILDER | 确定性组装完整草稿 | ✅ 已实现 |
| `submit_draft_plan` | TERMINAL | 提交生成计划（节点 1） | ✅ 已实现 |
| `submit_clarification` | TERMINAL | 提交追问方案（节点 2） | ✅ 已实现 |
| `submit_blocked` | TERMINAL | 提交拦截理由与替代方案（节点 3） | ✅ 已实现 |
| `submit_change_plan` | TERMINAL | 提交修改方案（节点 4） | ✅ 已实现 |
| `submit_edit_plan` | TERMINAL | 提交候选 EditPlan（节点 5.5） | ✅ 已实现 |
| `submit_fix_plan` | TERMINAL | 提交校验修复方案（节点 8） | ✅ 已实现 |

**工具契约（统一，7 个字段）**

每个工具文件都暴露同一组常量（[base.py](file:///Users/peiliu/Desktop/海悟/20261012%20智能报表功能/ai功能/backend/app/tools/base.py)）：

```text
NAME               工具名（中台 skill id）
DESCRIPTION        工具描述（直接给模型看）
PARAMETERS         JSON Schema（入参契约）
CATEGORY           FACT / BUILDER / TERMINAL
IS_READ_ONLY       只读语义；终结工具为 False
APPLICABLE_STATES  适用节点；空表示全节点可用
CONCLUSION_STATES  终结工具可能产生的结论状态（只有终结工具有）
run(**kwargs)      执行体，返回 dict
```

**`CONCLUSION_STATES` 为什么用复数**

同一个终结工具可能产生不止一个结论状态：

| 工具 | 结论状态 | 分叉条件 |
| --- | --- | --- |
| `submit_clarification` | `NEEDS_CLARIFICATION` / `ANALYZING` | 带 `missingInfo` → 追问；不带（闲聊/引导）→ 停在 `ANALYZING` |
| `submit_edit_plan` | `USER_EDITING_TEMPLATE` | 计划清楚 → 交给确定性引擎执行；说不清 → 留在编辑态追问 |

写成元组才能把这层分叉显式化，也才能对**每个结论分支逐个**校验合法性。

**节点白名单与合法转移的关系（核心不变量）**

> **工具在某节点可用 ⇔ 它的结论状态在该节点是合法转移。**

合法转移由 `ALLOWED_TRANSITIONS` 唯一决定（[agent.py](file:///Users/peiliu/Desktop/海悟/20261012%20智能报表功能/ai功能/backend/app/schemas/agent.py)），`is_conclusion_legal()` 封装判定：保持原状态算合法，其余必须在允许集合里。

这条不变量由 `registry.validate_tool_contracts()` 自动校验，有一条测试守着：

```python
def test_tool_contracts_are_self_consistent():
    problems = registry.validate_tool_contracts()
    assert problems == [], "工具契约不自洽：\n" + "\n".join(problems)
```

违反它意味着**模型能挑中一个落不下去的分支** —— 比如在 `VALIDATION_FAILED` 提交一个 `WAITING_USER_CONFIRM` 的结论。

**为上 skill 中台预留**

契约和实现分离：`registry.export_manifest()` 导出中台上架清单（名字 / 描述 / 入参 schema / 分类 / 权限语义 / 适用节点 / 结论状态），迁中台时**只换实现不改契约**。清单导出示例：

```text
registry.export_manifest()        → dict
registry.export_manifest_json()   → JSON 文本
```

清单里每个工具一条：

```json
{
  "name": "submit_edit_plan",
  "description": "提交对当前草稿某个区块的编辑计划……",
  "parameters": { "type": "object", "properties": { "...": {} } },
  "category": "TERMINAL",
  "readOnly": false,
  "applicableStates": ["WAITING_USER_CONFIRM", "USER_EDITING_TEMPLATE"],
  "conclusionStates": ["USER_EDITING_TEMPLATE", "NEEDS_CLARIFICATION"]
}
```

契约版本 `TOOL_CONTRACT_VERSION` 当前为 `1.1.0`（`1.0.0` → `1.1.0` 的唯一变更是新增 `conclusionStates`）。

**读草稿类工具的状态来源**

`get_current_draft` / `get_current_region` / `get_validation_result` 需要"当前这一轮"的草稿和校验结果，这些不是模型参数而是**会话状态**。走 [context.py](file:///Users/peiliu/Desktop/海悟/20261012%20智能报表功能/ai功能/backend/app/tools/context.py) 的 ContextVar 绑定（与 `session_context` 同一套做法），业务 Agent 每轮 bind / clear。

**终结工具的提交机制**

终结工具把结论写进**提交槽**（[submission.py](file:///Users/peiliu/Desktop/海悟/20261012%20智能报表功能/ai功能/backend/app/tools/submission.py)），业务 Agent 在 ReAct 循环结束后统一取走：

- 载荷先用 Pydantic 校验，**不合法就返回错误让模型重试**，不让非法结构流到确定性层；
- 同一轮内**只认第一次提交**，模型重复调用不会覆盖已定结论；
- Agent 侧已接上：`_plan_from_submission()` 优先消费提交结果，没有提交才回落到旧的文本 JSON 解析。

**关键设计：终结工具按节点分流**

一个 Agent 打天下，但终结工具的选择由 `request.state` 决定可用集合。这样既保持单 Agent，又不让模型在错误的节点提交错误的产物。

`tools_for_state(state)` 的实际结果（`APPLICABLE_STATES` 声明在 [base.py](file:///Users/peiliu/Desktop/海悟/20261012%20智能报表功能/ai功能/backend/app/tools/base.py)）：

```text
ANALYZING             → submit_draft_plan / submit_clarification / submit_blocked / submit_change_plan
NEEDS_CLARIFICATION   → submit_draft_plan / submit_clarification
DEFINITION_DRAFT_READY → submit_change_plan
WAITING_USER_CONFIRM  → submit_clarification / submit_blocked / submit_change_plan / submit_edit_plan
USER_EDITING_TEMPLATE → submit_edit_plan
VALIDATION_FAILED     → submit_fix_plan
其余节点              → （无终结工具，流水线节点不需要模型决策）
```

这套集合不是拍脑袋定的，它由上面那条不变量推导出来：**每个终结工具的 `APPLICABLE_STATES` 恰好是它那些 `CONCLUSION_STATES` 合法可落的节点集合**。改 `ALLOWED_TRANSITIONS` 就必须同步改工具白名单，`validate_tool_contracts()` 会把不一致直接报出来。

**编辑模式（`USER_EDITING_TEMPLATE`）的额外约束**

模板编辑虽然复用业务 Agent，但它是**独立的一条链路**，需要额外圈定：

| 约束 | 说明 |
| --- | --- |
| 工具白名单 | 编辑模式只给 `get_current_region` / `get_current_draft` / `search_catalog` / `search_metric` / `check_capability` / `submit_edit_plan`，**不给** `submit_draft_plan` 等生成类终结工具 |
| 独立 Prompt | 编辑 Prompt 只描述"读当前草稿 → 查证 → 提候选计划"，不混入生成逻辑 |
| 更小步数预算 | `max_iters=4`（见 [6.1](#61-预算与终止)），编辑场景不需要太多轮 |
| 允许澄清 | `submit_edit_plan` 走 `needsClarification=true` + `options` 分支，把歧义交回用户 |
| 允许放弃 | 完全不构成编辑指令时，交回路由层，不硬编一个 `EditPlan` |

这样既避免"为了改一句图，把整个生成工具链都挂上"，又保留了 ReAct 查证能力。

---

### 4.4 Template Edit（模板编辑）

**定位**：承接节点 5.5，把用户的编辑话术变成对草稿的实际修改。

**当前生产实现（三段式）**

1. **规则优先**：`TemplateEditEngine.parse()`（[template_edit_engine.py](file:///Users/peiliu/Desktop/海悟/20261012%20智能报表功能/ai功能/backend/app/services/template_edit_engine.py)）用规则解析，规则能确定就直接产出 `EditPlan`，不调模型。
2. **ReAct 查证**：规则返回 `UNKNOWN` / 置信度不足 / 缺参数时，保留编辑态，跳过通用路由；业务 Agent 使用编辑专用 Prompt 和节点工具白名单查事实，以 `submit_edit_plan` 提交候选。
3. **确定性应用**：`TemplateEditEngine.apply()` 校验目标、mock 授权范围、数据源/指标字段及粒度后改草稿、生成 `ChangeSet`。不明确的偏好以候选选项留在编辑态追问。

**生产切换已完成代码接线**：`AgentService._try_handle_template_edit()` 只截获明确规则快路径；模糊编辑交给业务 Agent 的受约束 ReAct。旧 `ContextEngine.build_template_edit_plan()` 留作兼容代码，不再是生产模糊编辑主路径。真实 LLM 的改名与候选选择已通过冒烟；复杂多步查证仍需验收。

**是否需要 ReAct：需要 —— 但只有"理解与规划"这一层用 ReAct，"改草稿"这一层必须是确定性引擎。**

**分工原则**

```text
理解与规划（ReAct）  →  用户到底要改哪块、改成什么、参数从哪来
执行（确定性 Engine） →  真正把草稿改掉、生成 ChangeSet
```

**为什么理解层必须用 ReAct**

纯规则引擎的能力上限是"句式已知"。用户一旦用指代（"那个耗电图"）、模糊偏好（"改好看点"）、或一次说多件事，规则就落到 `UNKNOWN`；而现在的语义兜底只是**一次性模型解析**（[context_engine.py](file:///Users/peiliu/Desktop/海悟/20261012%20智能报表功能/ai功能/backend/app/services/context_engine.py) 的 `build_template_edit_plan`），它拿不到"当前草稿里到底有哪些图""这个数据源支不支持小时粒度"，只能干巴巴追问。

**ReAct 补的就是这块**：让模型能查、能比、能确认参数从哪来。

**为什么执行层必须是 Engine**

- 授权和字段校验必须留在 `TemplateEditEngine.apply()`。模型可以查和提计划，但不能直接改草稿。
- 高频编辑动作（删 section、改标题、改图表类型）规则已经覆盖，规则**更快、更准、更可控**，走模型循环是倒退。

**改造建议**

| 项 | 建议 |
| --- | --- |
| 执行体 | 保持 `TemplateEditEngine` 为执行体，不动 |
| 规则层 | 继续扩张规则覆盖（成本低、收益确定），但**定位改成"快路径"而不是"能力上限"** —— 规则定不下来就升级到 ReAct 查证，而不是直接抛追问 |
| 语义层 | 从"一次性规范化"升级为"**受约束的 ReAct 规划器**"：允许查 `get_current_region` / `get_current_draft` / `check_capability` / `search_metric`，但只准通过 `submit_edit_plan` 交回候选计划或追问方案 |
| 歧义处理 | 新增 `options` 字段：真正不明确的偏好（"改好看点"）列候选选项交给用户选，**不擅自改** |
| 跑题处理 | 不构成编辑指令的输入（胡言乱语）不被编辑链路硬吞，澄清或交回路由层 |
| 校验层 | `TemplateEditEngine.apply()` 必须按授权范围、目标、字段与形态复核；复杂计划的事实来源追溯仍待补强 |
| 跨轮 | 05 节点 5.5 的"多轮缺参自动合并"由 ReAct 上下文解决，但草稿和待补参数必须外置到会话 |
| 独立 Agent？ | **先不拆**。作为业务 Agent 的"编辑模式"，用工具白名单 + 更小的步数预算 + 独立 Prompt 圈定边界；等编辑场景复杂到和生成流程互相干扰，再拆独立 Agent |

**边界重申**

```text
LLM（ReAct 规划）  →  候选 EditPlan  →  确定性授权/字段/形态校验  →  TemplateEditEngine.apply()  →  草稿 + ChangeSet
        ↑ 只到这一步为止                      ↑ 硬护栏
```

***

## 5. 多 Agent 是否合理

### 5.1 直接回答

**数量上合理，层次上需要重新表述。**

你现在有 4 类角色，但从"是不是需要自主循环 + 多步工具调用"这个标准看，它们的性质完全不同：

```text
Concierge   →  分类器（一次调用，一个封闭标签）
Deep Route  →  路由器（一次调用，一个结构化决策）
业务 Agent   →  真正的 Agent（多步、Observation 驱动、结果不确定）
Template Edit →  确定性执行引擎 + 一个受约束的 ReAct 规划器
```

所以准确的表述不是"我们有 4 个 Agent"，而是：

> **我们有 1 个 Agent、2 个分类器/路由器、1 个确定性执行引擎 + 1 个受约束的 ReAct 规划器（复用业务 Agent 的编辑模式）。**

这个表述本身就消除了"Agent 是不是太多了"的疑问——真正自主决策的只有一个。

### 5.2 为什么这个划分是合理的

| 判据 | 是否满足 | 说明 |
| --- | --- | --- |
| 关注点是否真的不同 | ✅ | 接待、路由、生成、编辑，四件事的输入输出、失败模式、可观测性都不同 |
| 是否需要不同的工具集 | ✅ | 业务 Agent 要查目录和指标；Template Edit 编辑模式要读 region 详情 + 查能力；路由器不要工具 |
| 是否需要不同的模型 | ✅ | Concierge 可以用小模型，Deep Router 要稳，业务 Agent 要强 |
| 是否需要独立的降级 | ✅ | Concierge 挂 → 直接 DEEP_ROUTE；业务 Agent 挂 → FakeAgent；路由挂 → 保守兜底 |
| 是否会被同一个 Prompt 硬塞在一起 | ❌ 不会 | 四者的 system prompt 完全不同，塞一起反而互相干扰 |

### 5.3 收敛建议

**不建议做的事**：把 Concierge 和 Deep Route 合并成一个 Agent。

理由：它们的价值差在于**快慢分层**。Concierge 处理"你好 / 你能做什么"这类高频无业务消息，用小模型 + 短超时快速拦掉；Deep Route 处理复杂语义判定，用强模型 + 长超时。一个 Agent 同时干这两件事，就只能被最弱的那一档拖住——要么变慢，要么变笨。

**真正该收敛的是三处信息断层，不是 Agent 数量**：

| # | 问题 | 原状 | 当前状态 |
| --- | --- | --- | --- |
| 1 | `collectedInfo` 断层 | Deep Route 收集的信息只进 Debug，业务 Agent 拿不到 | ✅ **已修**：路由后写回 `request.collectedInfo`，业务 Agent 的 `_compose_user_message` 以"已收集的信息"进模型输入；并在下一轮请求开始时从 Session snapshot 恢复 |
| 2 | 草稿摘要断层 | 业务 Agent 输入里没有当前草稿 | ✅ **已修**：`_compose_user_message` 补当前草稿摘要（`_summarize_draft`），另外有 `get_current_draft` 工具让模型随时读全文 |
| 3 | 跨轮记忆断层 | 每次请求新建 AgentScope Agent，跨轮无状态 | ⚠️ **部分修**：`collectedInfo` 已落 Session snapshot 跨轮恢复；**草稿本身仍靠前端带回的快照**，最近 N 轮对话摘要仍未做 |

三处断层里前两处已经闭环。第三处剩下的缺口是"多轮对话摘要"和"草稿的后端权威副本"，见 [6.2](#62-跨轮记忆与状态外置)。

### 5.4 目标架构

```text
AgentService（= ChatOrchestrator · 唯一状态编排器 · 确定性 · 我们自己写的）
│
├── RoutingLayer（路由层 · 两次串行 LLM 调用）
│    ├── Concierge Fast Path   小模型 / 短超时 / DIRECT_REPLY | DEEP_ROUTE
│    └── Deep Router           强模型 / 长超时 / RouteDecision
│         └── 输出 RouteDecision（含 collectedInfo）──┐
│                                                   │ 上下文贯通
├── TemplateEditEngine（确定性执行体）                │
│    ├── Rule Parser（快路径 · 优先）                │
│    └── ReAct 规划器（受约束 · 查证用）──┐          │
│                                     │              │
├── Business Agent（唯一 ReAct 承载者）◄──────────────┘
│    ├── Read-only Tools（facts）
│    │     search_catalog / search_metric / check_capability
│    │     get_current_draft / get_current_region / get_validation_result
│    └── Termination Tools（按 state 分流）
│          submit_draft_plan / submit_clarification / submit_blocked
│          submit_change_plan / submit_edit_plan / submit_fix_plan
│
└── Deterministic Pipelines（无模型）
      SAVE → VALIDATE → PREVIEW → RENDER → READY
```

### 5.5 一句话

> **Agent 数量不多，但"路由层两次串行调用"和"上下文三处断层"是真问题。前者砍合并成本，后者必须修。**

***

## 6. 工具、Observation 与工程约束

### 6.1 预算与终止

| 项 | 建议值 | 说明 |
| --- | --- | --- |
| `ReActConfig.max_iters` | 生成场景 **6**；**编辑场景 4**；校验修复 **5** | AgentScope 默认 50 太宽松，报表场景用不了 50 步 |
| `structured_output_grace_iters` | 2 | 触顶后额外的收尾机会 |
| Concierge 超时 | 10s | 快路径，超时要短 |
| Deep Router 超时 | 30s | 保持 |
| 业务 Agent 超时 | 60s（现有 `VOLC_TIMEOUT=30` 需上调） | 多步循环耗时更长 |
| 编辑场景超时 | 30s | 编辑通常查 1-3 步就够，比生成短 |
| 单次 Observation 大小 | 目录 / 指标列表 ≤ 20 条 + `total` + `truncated` | 防止上下文爆炸 |
| 工具异常 | 转成 `{"error": "..."}` 但**截断到 200 字符** | ✅ 已实现（[registry.py](file:///Users/peiliu/Desktop/海悟/20261012%20智能报表功能/ai功能/backend/app/tools/registry.py) 的 `_make_function_tool`） |

**终止的三条路径**

```text
1. 正常终止  → 调用终结工具（submit_*）
2. 步数触顶  → 强制要求终结（AgentScope structured_output_grace_iters 机制），仍失败则降级
3. 异常终止  → 超时 / 模型异常 → 降级 FakeAgent，节点 1 保留原草稿
```

### 6.2 跨轮记忆与状态外置

现状问题：`AgentScopeReportAgent._build_agent()` 每次新建 Agent，Agent 内部对话状态不跨请求。

改造方案——**会话态外置，Agent 无状态**：

```text
SessionStore（按 conversationId）
  ├── routeHistory        最近 N 次 RouteDecision
  ├── collectedInfo       累计收集到的信息（跨轮累加）   ← 已落地
  ├── pendingPlan         未确认的计划 / EditPlan
  ├── currentDraft        当前草稿快照（前端也会带回，做双向校验）
  ├── clarificationCount  追问轮数（上限 2）
  └── fixLoopCount        修复轮数（上限 2）
```

**当前已落地的部分（`collectedInfo` 跨轮闭环）**

```text
上一轮结束
  → session_service.record_turn(...) 写 snapshot（含 collectedInfo）
下一轮开始
  → agent_service.chat(): request.collectedInfo = session_service.load_collected_info(session_key)
  → 路由层在历史槽位基础上补全本轮新槽位（_merge_collected：历史值优先，新值只补空缺）
  → 路由后写回 request.collectedInfo
  → 业务 Agent 的 _compose_user_message 把它作为"已收集的信息"进模型输入
```

存储落点：`snapshot` 是 [sqlite_store.py](file:///Users/peiliu/Desktop/海悟/20261012%20智能报表功能/ai功能/backend/app/storage/sqlite_store.py) 的 JSON 字段，`collectedInfo` 直接存在里面，不新增表。

**⚠️ 已知风险（阶段 1 处理）**：snapshot 目前**没有绑定 `projectId`**。同一个 `conversationId` 如果切换项目，上一轮的槽位会被原样恢复，可能把 A 项目的线索带进 B 项目。修法有两种：

- 快照里同时存 `projectId`，恢复时比对不一致就丢弃 `collectedInfo`；
- 或者把 `projectId` 拼进 session key。

原则：**跨项目绝不共享槽位**，因为槽位里可能有项目内部的对象名。

**还没做的部分**

- `routeHistory`：路由历史还没进 snapshot，模型看不到"上几轮路由判断过什么"。
- `clarificationCount` / `fixLoopCount`：预算计数器还没落到后端，目前靠模型自觉。
- `currentDraft`：草稿仍以**前端带回的快照为准**，后端没有权威副本。这一点是现有协议的好设计，暂时保持；但要意识到**后端无法保证草稿没被前端改过**。
- 最近 N 轮对话摘要：未做。

原则：

- Agent 每次新建，但通过**注入**拿到会话态（现状 `_compose_user_message` 就是注入点，扩展它）。
- 草稿以**前端带回的快照为准**，后端缓存只做交叉校验，不做唯一真相源——这是现有协议的好设计，保持。
- 追问轮数、修复轮数这类**预算计数器放后端**，不能靠模型自觉。

### 6.3 工具权限语义

工具已统一收进 [app/tools/](file:///Users/peiliu/Desktop/海悟/20261012%20智能报表功能/ai功能/backend/app/tools) 目录，权限语义按工具自身声明区分（[base.py](file:///Users/peiliu/Desktop/海悟/20261012%20智能报表功能/ai功能/backend/app/tools/base.py) 的 `IS_READ_ONLY`，注册时由 [registry.py](file:///Users/peiliu/Desktop/海悟/20261012%20智能报表功能/ai功能/backend/app/tools/registry.py) 读取）：

| 类型 | `is_read_only` | 说明 |
| --- | --- | --- |
| 只读工具 | `True` | 目录、指标、能力、草稿、校验结果 |
| 终结工具 | `False` | 它们不是"只读"——虽然不写库，但会**决定业务走向**，权限检查应当走正常流程 |

不要图省事把终结工具也标 `is_read_only=True`。

#### 6.3.1 数据范围授权（项目 / 业务对象）

`is_read_only` 说的是"会不会改东西"，**不等于"能看哪些东西"**。查数据的能力必须再叠一层范围授权。

```text
ToolScope(project_id, subject_ref, subject_id)   ← 由 ToolContext 绑定
  ↓
require_scope()                                     ← 未绑定直接 PermissionError
  ↓
scope_service.is_item_visible(...)                   ← 项目 / 实例 / subjectRef 三重过滤
```

具体规则（[scope_service.py](file:///Users/peiliu/Desktop/海悟/20261012%20智能报表功能/ai功能/backend/app/services/scope_service.py)）：

| 规则 | 说明 |
| --- | --- |
| `project_scoped` 标签 | 带这个标签的目录 / 指标，未授权 `projectId` 一律不可见 |
| `subjectRef` 不匹配 | 请求的 `subjectRef` 和条目的 `subjectRef` 不一致 → 不可见 |
| 实例未授权 | 条目绑定了业务对象（`machine_room`）但请求的 `subjectId` 不在授权列表 → 不可见 |

**已经堵上的越权口子**（有测试守着）：

| 场景 | 行为 |
| --- | --- |
| 未绑定 `ToolContext` 就查目录 / 指标 | 抛 `PermissionError`，不是返回空 |
| 查未授权项目下的数据源 | 查不到（返回空 + `total=0`） |
| 查未授权实例下的指标 | 查不到 |
| **已知 `sourceRef` 直接查能力** | 越权时返回 `supported: false` + `supportedGrains: []`，**不回显该源的真实能力** |

最后一条最容易漏：列表查询过滤了不等于"按 ID 直接查"也过滤了。探测性查询必须同样按范围裁剪，否则模型可以用二分探测把未授权源的能力摸出来。

**`projectId` 不必暴露给模型**：它是授权依据，不是业务参数。模型只需要说"查能耗指标"，范围由后端从 `ToolContext` 取。暴露给模型反而给了它"猜别的项目 ID"的入口。

**⚠️ 阶段 1 待补**：`TemplateEditEngine.apply()` 目前直接调 `get_catalog()` / `get_metric()`，**没有继承工具层的授权范围**。这意味着编辑链路的目录 / 指标读取绕过了 `scope_service`。修法是让 Engine 也从 `ToolContext` 取范围，或者把它降级为"只接受已由工具授权过的引用"。

### 6.4 Observation 契约

每条 Observation 必须满足：

| 要求 | 说明 |
| --- | --- |
| 可判定真假 | 工具查到的实体才能进草稿，查不到就明确说查不到 |
| 有界 | 列表必带 `total` 和 `truncated`；单条错误信息截断 |
| 可回溯 | 终结工具的参数能追溯到具体某个工具的某条 Observation |
| 不重复 | 同一轮里已经查过的内容不再重复返回 |

### 6.5 Prompt 重写要点

`report_generation.md` 现在的问题不只是"顺序写死"，还有：

- 用 `mode: GENERATE/FALLBACK/CHITCHAT` 三分法描述所有场景，**表达力不够**——修改、拦截、追问、修复这四类全被压进 `FALLBACK`。
- 示例里 `roomId` 直接写死 `"room_a"`，但要求"从上下文 subjectId 取"，**自相矛盾**。
- "本阶段不处理该意图"这种话写在 system prompt 里，模型会把它当成业务规则。

改成：Prompt 只描述**角色、可用工具、边界**，具体行为由工具描述 + 终结工具的 schema 引导。

***

## 7. 分阶段落地计划

### 阶段 0：把业务 Agent 从"伪 ReAct"改成"Observation 驱动"

**目标**：让 Observation 真正影响下一步和最终参数。这一步不做完，后面全是空中楼阁。

| # | 任务 | 交付物 |
| --- | --- | --- |
| 0.1 | 重写 `report_generation.md`：去顺序、去 mode 三分法 | ✅ 已完成（[report_generation.md](file:///Users/peiliu/Desktop/海悟/20261012%20智能报表功能/ai功能/backend/app/agents/prompts/report_generation.md) 改为"想→查→定" + 工具清单 + 场景走法） |
| 0.2 | 新增 `submit_draft_plan` 终结工具 + Tool 权限语义调整 | ✅ 已完成（[app/tools/](file:///Users/peiliu/Desktop/海悟/20261012%20智能报表功能/ai功能/backend/app/tools) 13 个工具 + 提交槽） |
| 0.3 | 业务 Agent 输入补全：当前草稿摘要 + `collectedInfo` | ✅ 已完成（`_compose_user_message` + `_summarize_draft`） |
| 0.4 | `RouteDecision.collectedInfo` 打通到业务 Agent | ✅ 已完成（`agent_service` 放行时写回 `request.collectedInfo`） |
| 0.5 | `ReActConfig(max_iters=6)` + 超时配置 | ✅ 已完成（[config.py](file:///Users/peiliu/Desktop/海悟/20261012%20智能报表功能/ai功能/backend/app/config.py) 的 `react_max_iters` / `react_max_iters_edit` / `react_max_iters_fix`；`_build_react_config` 按节点给预算） |
| 0.6 | Observation 分页 / 裁剪 | ✅ 已完成（`observation_max_items` + `observation_error_max_chars`；目录/指标工具支持 `limit` 参数，返回带 `truncated` 标记） |
| 0.7 | 端到端验证：目录查询结果变化 → 草稿内容跟着变 | ✅ 已完成（[test_react_stage0.py](file:///Users/peiliu/Desktop/海悟/20261012%20智能报表功能/ai功能/backend/tests/test_react_stage0.py) 17 项；[test_tools.py](file:///Users/peiliu/Desktop/海悟/20261012%20智能报表功能/ai功能/backend/tests/test_tools.py) 44 项） |
| 0.8 | 终结工具 → 协议响应组装 | ✅ 已完成（`_dispatch_submission` 分发 6 个终结工具；无 `missingInfo` 的澄清提交按闲聊处理；`intent=UNKNOWN` 的编辑提交转为追问） |
| 0.9 | 节点级工具白名单接入 Toolkit | ✅ 已完成（`tools_for_state(request.state)` 注册进 AgentScope Toolkit，模型看不到当前节点不该调的工具） |
| 0.10 | 终结工具多结论状态声明 + 契约自洽校验 | ✅ 已完成（`CONCLUSION_STATES` + `validate_tool_contracts()`） |
| 0.11 | 目录 / 指标 / 能力查询接入项目与实例授权 | ✅ 已完成（`ToolContext` + `scope_service`，含已知 `sourceRef` 的越权探测） |

**验收标准**

验收要分三层看，**不能把单元测试当成真实模型行为已验收**：

**① 代码级验收（单测可证明，已完成）**

| # | 标准 | 覆盖 |
| --- | --- | --- |
| 1 | 终结工具提交能被捕获并分发到对应终态 | `test_react_stage0.py` |
| 2 | `intent=UNKNOWN` 的编辑提交转成追问，不误改草稿 | `test_react_stage0.py` |
| 3 | 有效编辑计划落到 `USER_EDITING_TEMPLATE` 并带 `ChangeSet` | `test_react_stage0.py` |
| 4 | 表达含糊时给候选选项（`SINGLE_CHOICE` + options），不替用户决定 | `test_react_stage0.py` |
| 5 | 修复重试提示只列当前节点可调用的终结工具 | `test_react_stage0.py` |
| 6 | Toolkit 只注册当前节点的工具 | `test_tools.py` |
| 7 | 工具契约自洽（白名单 ⇔ 合法转移） | `test_tools.py` |
| 8 | 未授权项目 / 实例查不到；已知 `sourceRef` 越权探测不回显能力 | `test_tools.py` |
| 9 | 未绑定 `ToolContext` 查询直接 `PermissionError` | `test_tools.py` |

**② 真实 LLM 行为验收（❌ 尚未做）**

以下三条**必须拿真实模型跑**，单测证明不了：

- [ ] 删掉 Prompt 固定顺序后，模型在"指标查不到"时**真的会**去换 `search_metric` 的参数重试，而不是编一个。
- [ ] `check_capability` 返回不支持时，模型**真的不会**走生成路径。
- [ ] 同一句话 + 同一目录状态 → 草稿稳定；目录状态变了 → 草稿跟着变。

跑法：`scripts/smoke_llm.py`，对比关键场景输出。**这三项没跑过之前，不能说阶段 0 的"行为"已验收。**

**③ 真实目录 / 权限验收（❌ 尚未做）**

- [ ] 接真实目录服务后，`search_catalog` / `search_metric` 返回真实授权范围。
- [ ] 真实权限体系下，未授权对象的探测路径全部封死。

当前用的是 mock 目录（`app/data/machine_room_energy.py`）和 mock 授权，**只能证明"授权逻辑写得对"，不能证明"接了真实服务也对"**。

**阶段 0 已落地的代码（可直接跳着看）**

| 改动 | 文件 | 作用 |
| --- | --- | --- |
| Prompt 重写 | [report_generation.md](file:///Users/peiliu/Desktop/海悟/20261012%20智能报表功能/ai功能/backend/app/agents/prompts/report_generation.md) | 去顺序、去 mode 三分法；改为"想→查→定" + 终结工具收尾 |
| 终结工具提交槽 | [submission.py](file:///Users/peiliu/Desktop/海悟/20261012%20智能报表功能/ai功能/backend/app/tools/submission.py) | `ContextVar` 做请求级隔离；同一轮只认第一次提交；提交前 Pydantic 校验载荷 |
| 工具上下文 | [context.py](file:///Users/peiliu/Desktop/海悟/20261012%20智能报表功能/ai功能/backend/app/tools/context.py) | 让 `get_current_draft` / `get_current_region` / `get_validation_result` 能读到本轮状态，不用把大对象塞进模型参数 |
| 13 个工具 | [app/tools/](file:///Users/peiliu/Desktop/海悟/20261012%20智能报表功能/ai功能/backend/app/tools) | FACT（6）/ BUILDER（1）/ TERMINAL（6）三类，统一契约 + 中台清单导出 |
| ReAct 预算 | [config.py](file:///Users/peiliu/Desktop/海悟/20261012%20智能报表功能/ai功能/backend/app/config.py) + `_build_react_config` | 生成 6 步 / 编辑 4 步 / 修复 5 步，远低于 AgentScope 默认 50 |
| Observation 有界化 | `observation_max_items` / `observation_error_max_chars` | 目录、指标支持 `limit`，返回 `{items, total, truncated}`；工具异常文本按字符截断 |
| 终结工具 → 协议响应 | `_dispatch_submission` | 6 个终结工具各自映射到对应节点状态；无 `missingInfo` 的澄清按闲聊处理；`intent=UNKNOWN` 的编辑转追问 |
| 输入补全 | `_compose_user_message` / `_summarize_draft` | 给模型三样它看不到的东西：当前草稿摘要、路由已收集信息、用户选中区块 |
| 节点级白名单 | `tools_for_state` + Toolkit 注册 | 模型只看到当前节点合法的工具 |
| 多结论声明 | `CONCLUSION_STATES` + `validate_tool_contracts()` | 终结工具声明自己可能落哪些状态；白名单与 `ALLOWED_TRANSITIONS` 不一致时直接报错 |
| 范围授权 | `ToolContext` + `scope_service` | 目录 / 指标 / 能力查询按 `projectId` / `subjectId` 过滤；未绑定上下文直接拒绝 |

**回归与现场验证**：全量 **171 passed**。真实 LLM 的“改名成”编辑和“改好看点”候选选择闭环已通过。展示候选以 Engine 登记的实际能力为准，不使用模型编造的平滑、渐变等未实现效果；真实目录权限仍待验收。

**阶段 1 生产接线已完成**：明确规则继续走 Engine 快路径；其余编辑消息保留 `USER_EDITING_TEMPLATE`，由业务 Agent 查草稿、区块、目录、指标和能力后提交计划。局部追问不跳通用澄清态；成功修改使用现有前端确认协议。

现场验证：真实模型已通过“改名成”编辑冒烟，返回 `editPlanSource=react`、新草稿及 `template_edit_confirm`。AgentScope 工具在子任务中执行，终结工具提交槽现用请求级共享容器避免 `ContextVar.set()` 无法回传父任务。仍待验证多步 Observation 如何改变行动，以及真实目录服务接入后的授权边界。

---

### 阶段 1：节点 4 修改方案 + 节点 5.5 受约束 ReAct 编辑

**目标**：AI 自产修改方案（05 节点 4 待做第 2/3 条）+ 编辑场景能查证、能澄清、能合并缺参（05 节点 5.5 待做）。

| # | 任务 | 交付物 |
| --- | --- | --- |
| 1.1 | `submit_change_plan` 终结工具 + `build_change_set` 确定性组装 | ChangeSet 生成链路（终结工具 ✅ 已完成） |
| 1.2 | `get_current_draft` / `get_current_region` 工具 | ✅ 已完成 |
| 1.3 | `submit_edit_plan` 终结工具，接入现有 `EditPlan` schema | ✅ 已完成（[schemas.py](file:///Users/peiliu/Desktop/海悟/20261012%20智能报表功能/ai功能/backend/app/tools/schemas.py) 的 `EditPlanPayload`） |
| 1.4 | 节点 5.5 语义兜底升级为受约束 ReAct 查证 | ✅ 已接入生产分流；真实 LLM 行为待验收 |
| 1.5 | 编辑模式独立 Prompt + 工具白名单 + `max_iters=4` | ✅ 已接入业务 Agent 编辑模式 |
| 1.6 | `submit_edit_plan` 增加 `options` 字段，歧义时给候选选项 | ✅ 已完成 |
| 1.7 | 规则层保留快路径，规则定不下来时升级到 ReAct | ✅ 生产分流已接通 |
| 1.8 | `usedDefaults` 字段，节点 4 区分用户指定项与系统暂选项 | 协议扩展 |

**验收标准**

- [ ] "把趋势改成最近 7 天"能产出正确的 ChangeSet（regionId / path / reason）。
- [ ] "把那个耗电图改成小时级"能**先查证**（当前有哪些图 / 该源支持哪些粒度），再产出候选计划，而不是直接追问。
- [ ] "把设备排名的数据源换成 EAS 能耗明细，粒度改小时"能自动补齐参数；补不齐时**问得具体**（不是"你想怎么改"）。
- [ ] "这个图改好看点"会给出**候选选项**让用户选，不会擅自改。
- [ ] 一句跑题的话不会被硬编成一个 `EditPlan`。
- [ ] 草稿 diff 三栏（旧 / 新 / 原因）能正常渲染。

---

### 阶段 2：节点 3 自产拦截 + 节点 8 修复闭环

**目标**：05 节点 3 待做第 3 条 + 节点 8 待做第 3 条。

| # | 任务 | 交付物 |
| --- | --- | --- |
| 2.1 | `submit_blocked` 终结工具 + grounded 替代方案校验 | 拦截链路 |
| 2.2 | `check_capability` 返回 `supportedGrains` 供模型构造替代 | 能力接口扩展 |
| 2.3 | `submit_fix_plan` 终结工具 + 重新校验闭环 | 修复循环 |
| 2.4 | 修复轮数上限 2 轮，超限转 `ASK_USER` | 预算控制 |
| 2.5 | `FakeReportAgent` 冻结（不再加业务规则） | 代码约定 |

**验收标准**

- [ ] "看分钟级"能被拦，且给出的替代方案**真的在 `supportedGrains` 里**。
- [ ] 校验失败能一键采纳建议并自动重新校验。
- [ ] 修复循环不会无限转（2 轮后停下来问人）。

---

### 阶段 3：路由层优化（可选，最后做）

**目标**：降低路由层两次串行调用的成本。

候选方案：

| 方案 | 做法 | 代价 |
| --- | --- | --- |
| A | Concierge / Deep Router **并行**跑，谁的置信度高用谁 | 两个结果仲裁逻辑 |
| B | 规则前置：明显是闲聊的走规则，只有模糊的才打 Concierge | 规则维护成本 |
| C | Concierge 用更小的模型 + 更短超时 | 分类准确率下降 |

**建议**：先量数据再决定。当前 Concierge 30s 超时是明显偏大的，先调到 10s 大概率就能解决大部分体感延迟问题。

***

## 8. 验收标准与观测指标

### 8.1 功能验收（对照 05 文档待做项）

| 05 节点 | 待做项 | 对应本方案 | 阶段 | 状态 |
| --- | --- | --- | --- | --- |
| 1 | 已给条件约束检索，缺项从授权且可用的目录 / 数据中选默认值 | `submit_draft_plan.usedDefaults` | 0 | 工具已就位，字段待加 |
| 1 | 真实目录和权限检索 | `search_catalog` 接真实目录 | 0 | ✅ 授权逻辑已接真实范围；❌ 目录源仍是 mock |
| 2 | 追问最多 2 轮 | `clarificationCount` 后端计数 | 0 | ❌ 未做 |
| 2 | 用户答案自动带回下一轮 | 会话态外置 + `collectedInfo` | 0 | ✅ `collectedInfo` 跨轮恢复已落地 |
| 3 | AI 自己产出拦截结果 | `submit_blocked` | 2 | 工具已就位，链路未跑通 |
| 4 | AI 产修改方案 | `submit_change_plan` | 1 | 工具已就位，`operations → ChangeSet` 确定性组装未做 |
| 5.5 | 跨轮缺参自动合并 | 受约束 ReAct 查证 + 会话态外置 | 1 | ❌ 未做 |
| 5.5 | 用户说不清时的处理 | 查证优先 → 歧义给 `options` → 跑题交回路由 | 1 | ✅ 候选选项已实现；❌ 生产路径未切 |
| 8 | 「采纳建议修改」一键改 | `submit_fix_plan` + 重校验 | 2 | 工具已就位，重校验闭环未做 |

> 阶段列是"计划在哪做"，状态列是"现在做到哪"。**"工具已就位"不等于"链路已跑通"** —— 终结工具的实现和它在生产路径上的调用是两件事。

### 8.2 观测指标

现在已经有 Debug Trace 卡片，ReAct 化后需要新增的指标：

| 指标 | 采集点 | 用途 |
| --- | --- | --- |
| `react_iters` | 业务 Agent | 每轮实际步数分布，异常膨胀说明 Prompt 或工具设计有问题 |
| `react_tool_seq` | 业务 Agent | 工具调用序列，统计真实顺序，验证"顺序不再写死" |
| `react_tool_errors` | 工具 | 工具异常率 |
| `react_terminator` | 业务 Agent | 走哪个终结工具收尾 |
| `react_observation_bytes` | 工具 | Observation 体积，防止上下文爆炸 |
| `react_fallback` | 业务 Agent | 降级到 FakeAgent 的次数 |
| `react_budget_exhausted` | 业务 Agent | 步数 / 追问轮 / 修复轮触顶次数 |
| `route_concierge_hit` | 路由层 | Concierge 直接回复的占比，衡量快路径价值 |
| `route_collectedInfo_hits` | 路由层 | `collectedInfo` 实际被业务 Agent 复用的比例，验证打通是否有效 |

### 8.3 回归保护

- `FakeReportAgent` 冻结为降级 + pytest 基准，现有 `tests/` 全绿是每阶段的门槛。
- 状态机 `ALLOWED_TRANSITIONS` 相关的 `test_state_transitions.py` 不允许因为 ReAct 改造而放宽。
- 每次改 Prompt 都要跑 `scripts/smoke_llm.py`，对比关键场景的输出稳定性。

***

## 9. 不做什么（明确边界）

| 不做 | 原因 |
| --- | --- |
| 不把保存 / 校验 / 预览 / 渲染做成 Agent | 这些是确定性流水线，节点 6-12 一律不进 LLM |
| 不让模型直接产 `ReportDefinitionDraft` JSON | 模型只产候选计划，组装由确定性函数做 |
| 不让模型直接改草稿 | 统一走 `EditPlan → 校验 → apply`；ReAct 只做理解与规划 |
| 不绕过确定性校验 | 目标、原话中的标题、授权目录/指标、字段及展示形态由后端复核；复杂组合编辑的来源追溯仍需补齐 |
| 不给 Concierge / Deep Route 加工具 | 保持它们的分类器 / 路由器属性 |
| 不在 `FakeReportAgent` 里加新业务规则 | 它是降级和测试基准，不是第二大脑 |
| 不把 ReAct 铺到所有节点 | 12 个节点里只有 4 个完整 ReAct + 1 个受约束 ReAct 值得 |
| 不让编辑模式拿到生成类工具 | 避免"改一句话"触发整条生成工具链 |
| 不把编辑拆成独立 Agent（现阶段） | 先用业务 Agent 的编辑模式圈边界，等真的互相干扰再拆 |

***

## 10. 待确认问题

| # | 问题 | 说明 | 状态 |
| --- | --- | --- | --- |
| 1 | 业务 Agent 超时定多少 | 建议 60s，需要确认前端流式体验能接受 | 待定 |
| 2 | `max_iters` 定 6 是否够 | 依赖真实目录规模，建议阶段 0 埋点后调整 | 待真实 LLM 埋点后定 |
| 3 | `RouteDecision.collectedInfo` 的字段结构 | 现有是 `dict[str, Any]`，是否要定义成强类型 | **建议暂不改**：它是 Deep Route 的服务端中间产物，形状会随路由演进，强行定死反而拖累。强类型等它稳定后再抽 |
| 4 | 草稿快照的权威源 | 维持"前端带回为准"，还是改成后端为准 | **维持现状**（前端为准）。但要注意：后端无法保证草稿没被前端改过 |
| 5 | 路由层要不要做并行 | 阶段 3 前需要先量 Concierge / Deep Route 的实际耗时占比 | 待量数据 |
| 6 | 追问轮数上限是否放在 `RouteDecision` 里 | 现在 `RouteDecision` 没有轮数字段 | **建议不放**。轮数是后端预算，不是路由决策的一部分，应该放 Session 侧 |
| 7 | 编辑模式要不要独立成 Agent | 建议先做业务 Agent 的编辑模式（工具白名单 + 独立 Prompt）；等编辑场景复杂或与生成流程互相干扰再拆 | 待定（工具白名单已就位） |
| 8 | `EditPlan.options` 的形态 | 纯字符串列表，还是复用必选下拉的 `options` 结构（带 value / label） | **已定**：复用必选下拉的 `options` 结构（带 value / label），见 [4.3](#43-业务-agentagentscopereportagent) 的 `SUGGEST_VISUALIZATION` 分支。纯字符串列表前端渲染不出可选控件 |
| 9 | 编辑追问算不算 `NEEDS_CLARIFICATION` 轮次 | 节点 5.5 内部追问是否共用上限 2 轮 | 当前留在 `USER_EDITING_TEMPLATE`，不经过节点 2；编辑追问预算尚待定义 |
| 10 | Session snapshot 要不要绑定 `projectId` | 同一会话切项目会串槽位 | **建议必须绑**，见 [6.2](#62-跨轮记忆与状态外置) |
| 11 | TemplateEditEngine 怎么接授权范围 | `parse` / `apply` 显式接收 `ToolScope`，alias 只在可见集合中匹配，apply 前复核 | mock 授权已接；真实权限系统待验收 |
