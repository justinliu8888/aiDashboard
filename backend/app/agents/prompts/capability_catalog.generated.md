# 报表 Agent 能力总目录

> **本文件是自动生成物，请勿手改。**由 `backend/app/services/capability_catalog_service.py` 从**真实注册项**生成：已注册 Tool、清单能力、旧编辑意图、展示操作白名单、规则快路。参数契约直接取注册项自己的 JSON Schema，不手抄、不另维护参数表。

- **目录版本**（contractFingerprint 派生）：`72d0cc0c0c8adcc7`
- **契约指纹**：`72d0cc0c0c8adcc71374496f9491b273b3364e5529346c7a16b231a40dc33a4a`
- **生成时间**：2026-11-09T03:46:37+00:00
- **条目总数**：74（Agent Tool 20 / 清单能力与展示操作、规则快路 37 / 旧编辑意图 17）

## 入口类型说明

- `agent_tool` —— **模型可以直接调用的 Tool**，已注册在 `app/tools/registry.py` 的 `_TOOLS` 里。
- `operation` —— **操作清单里的能力**，由后端执行，**不是独立 Tool**：模型在 `submit_edit_plan` 的 `operations` 里给出 `{"op": "<能力名>", "target": {...}, "params": {...}}`。
- `legacy_intent` —— **旧格式编辑意图**，由 `template_edit_engine.TemplateEditEngine.apply` 执行，**不是独立 Tool**。

> 目录里**名字可能重复**：`ADD_SECTION` / `DELETE_SECTION` 既是清单能力，也是旧编辑意图 —— 两者的入口形式完全不同。**唯一键是 `(entryType, id)`**，标题里带分组以便区分。

## 一、已注册 Agent Tool（模型可直接调用）

### search_catalog

- **入口类型**：`agent_tool` —— Agent Tool —— 模型可直接调用
- **分组**：工具 / 事实查询（只读）
- **状态**：`available`
- **用途**：查询当前项目授权范围内可用的数据目录（数据源）。参数 sourceRef 查具体数据源；参数 subjectRef 按业务对象过滤。返回的数据源包含 supportedGrains（支持的时间粒度）和 fields（可用字段），生成报表前必须先确认目标数据源存在且支持所需粒度。
- **参数**（JSON Schema，取自真实契约）：

```json
{
  "type": "object",
  "properties": {
    "sourceRef": {
      "type": "string",
      "description": "可选，数据源 ID"
    },
    "subjectRef": {
      "type": "string",
      "description": "可选，业务对象，如 machine_room"
    },
    "limit": {
      "type": "integer",
      "description": "可选，最多返回几条；超出会标 truncated=true 并给出 total"
    }
  }
}
```

- **必填参数**：（无）
- **目标 / 引用要求**：sourceRef / subjectRef 可空；不传时返回当前账号可见的全部目录。
- **返回值**：{items: 目录项[], total: 总数, truncated: 是否被裁剪}
- **适用节点**：ANALYZING、NEEDS_CLARIFICATION、DEFINITION_DRAFT_READY、WAITING_USER_CONFIRM、USER_EDITING_TEMPLATE、VALIDATION_FAILED、SAVE_DEFINITION_DRAFT、VALIDATING、PREVIEWING、PREVIEW_READY、RENDERING、READY、INTERRUPT_CONFIRMING、BLOCKED_OR_UNSUPPORTED
- **只读**：是
- **副作用**：只读：不改草稿、不写业务数据。
- **主要错误**：
  - 没有授权范围时直接报错，不返回空列表冒充「没有目录」
  - 目录服务不可用时抛错，由统一注册层转成 error 结果（不降级成空结果）
- **运行限制**：
  - 结果按 observation_max_items 裁剪，可用 limit 参数指定条数
- **源码定位**：
  - registration：`app/tools/registry.py::_TOOLS`
  - dispatch：`app/tools/search_catalog_tool.py::run`
  - validation：`app/tools/search_catalog_tool.py::PARAMETERS`
  - execution：`app/services/scope_service.py::filter_visible`

### search_metric

- **入口类型**：`agent_tool` —— Agent Tool —— 模型可直接调用
- **分组**：工具 / 事实查询（只读）
- **状态**：`available`
- **用途**：查询当前项目授权范围内**已登记**的指标口径。参数 metricRef 查具体指标；参数 subjectRef 按业务对象过滤。返回的指标包含 unit（单位）和 fieldRef（底层字段）——这些是真实口径，查到的指标可以直接用。查不到**不代表**用户不能提这个指标：当前是模拟业务数据模式，用户口述、目录里没有的指标按「目录外模拟指标」处理即可（服务端给它补「仅模拟展示、业务口径待确认」标注），不需要等管理员先登记，也不要因此拒绝用户的请求。
- **参数**（JSON Schema，取自真实契约）：

```json
{
  "type": "object",
  "properties": {
    "metricRef": {
      "type": "string",
      "description": "可选，指标 ID"
    },
    "subjectRef": {
      "type": "string",
      "description": "可选，业务对象"
    },
    "limit": {
      "type": "integer",
      "description": "可选，最多返回几条；超出会标 truncated=true 并给出 total"
    }
  }
}
```

- **必填参数**：（无）
- **目标 / 引用要求**：metricRef / subjectRef 可空；不传时返回当前账号可见的全部指标。
- **返回值**：{items: 指标项[], total: 总数, truncated: 是否被裁剪}
- **适用节点**：ANALYZING、NEEDS_CLARIFICATION、DEFINITION_DRAFT_READY、WAITING_USER_CONFIRM、USER_EDITING_TEMPLATE、VALIDATION_FAILED、SAVE_DEFINITION_DRAFT、VALIDATING、PREVIEWING、PREVIEW_READY、RENDERING、READY、INTERRUPT_CONFIRMING、BLOCKED_OR_UNSUPPORTED
- **只读**：是
- **副作用**：只读：不改草稿、不写业务数据。
- **主要错误**：
  - 没有授权范围时直接报错，不返回空列表冒充「没有指标」
  - 指标目录不可用时抛错，由统一注册层转成 error 结果
- **运行限制**：
  - 结果按 observation_max_items 裁剪，可用 limit 参数指定条数
- **源码定位**：
  - registration：`app/tools/registry.py::_TOOLS`
  - dispatch：`app/tools/search_metric_tool.py::run`
  - validation：`app/tools/search_metric_tool.py::PARAMETERS`
  - execution：`app/services/scope_service.py::filter_visible`

### check_capability

- **入口类型**：`agent_tool` —— Agent Tool —— 模型可直接调用
- **分组**：工具 / 事实查询（只读）
- **状态**：`available`
- **用途**：检查数据源是否支持某个时间粒度，返回 supported 和该数据源支持的粒度列表。若用户要求的粒度不支持，不要硬改，应改用支持的粒度或换数据源，并在回复中说明原因。
- **参数**（JSON Schema，取自真实契约）：

```json
{
  "type": "object",
  "properties": {
    "sourceRef": {
      "type": "string",
      "description": "数据源 ID"
    },
    "grainUnit": {
      "type": "string",
      "enum": [
        "MINUTE",
        "HOUR",
        "DAY",
        "WEEK",
        "MONTH"
      ],
      "description": "时间粒度"
    }
  },
  "required": [
    "sourceRef",
    "grainUnit"
  ]
}
```

- **必填参数**：sourceRef、grainUnit
- **目标 / 引用要求**：sourceRef 必须是目录里真实存在的数据源 id；grainUnit 取粒度单位。
- **返回值**：{sourceRef, grainUnit, supported: bool, supportedGrains: [...]}
- **适用节点**：ANALYZING、NEEDS_CLARIFICATION、DEFINITION_DRAFT_READY、WAITING_USER_CONFIRM、USER_EDITING_TEMPLATE、VALIDATION_FAILED、SAVE_DEFINITION_DRAFT、VALIDATING、PREVIEWING、PREVIEW_READY、RENDERING、READY、INTERRUPT_CONFIRMING、BLOCKED_OR_UNSUPPORTED
- **只读**：是
- **副作用**：只读：不改草稿、不写业务数据。
- **主要错误**：
  - 数据源不存在或越权 → supported=false 且不返回任何能力细节（不泄漏越权信息）
- **运行限制**：
  - 一次只核对一个数据源的一个粒度
  - grainUnit 取值域：MINUTE、HOUR、DAY、WEEK、MONTH
- **源码定位**：
  - registration：`app/tools/registry.py::_TOOLS`
  - dispatch：`app/tools/check_capability_tool.py::run`
  - validation：`app/tools/check_capability_tool.py::PARAMETERS`
  - execution：`app/services/catalog_mock_service.py::get_catalog`

### get_current_draft

- **入口类型**：`agent_tool` —— Agent Tool —— 模型可直接调用
- **分组**：工具 / 事实查询（只读）
- **状态**：`available`
- **用途**：读取当前对话的报表草稿概览：报表名、版本、包含哪些 region（区块）及各自类型，以及这份草稿还差什么才能往下走。事实来自服务端会话记录：返回的 status 是事实状态，ABSENT 表示当前对话确实没有草稿，UNAVAILABLE 表示暂时无法确认（读不到），两者不能混为一谈。用户在说'把趋势图改成 7 天''删掉某个卡片'时，先调用它确认目标区块是否存在，再决定改哪个 region。
- **参数**（JSON Schema，取自真实契约）：

```json
{
  "type": "object",
  "properties": {}
}
```

- **必填参数**：（无）
- **目标 / 引用要求**：无参数。
- **返回值**：当前草稿定义：regions（含正文卡片 id 清单）、可用共享主题 availableThemes、统计口径与时间范围等
- **适用节点**：ANALYZING、NEEDS_CLARIFICATION、DEFINITION_DRAFT_READY、WAITING_USER_CONFIRM、USER_EDITING_TEMPLATE、VALIDATION_FAILED、SAVE_DEFINITION_DRAFT、VALIDATING、PREVIEWING、PREVIEW_READY、RENDERING、READY、INTERRUPT_CONFIRMING、BLOCKED_OR_UNSUPPORTED
- **只读**：是
- **副作用**：只读：不改草稿、不写业务数据。
- **主要错误**：
  - 当前没有草稿 → 返回 found=false 与原因，不编造草稿内容
- **运行限制**：
  - 只读当前会话的草稿，不跨会话
- **源码定位**：
  - registration：`app/tools/registry.py::_TOOLS`
  - dispatch：`app/tools/get_current_draft_tool.py::run`
  - validation：`app/tools/get_current_draft_tool.py::PARAMETERS`
  - execution：`app/services/session_fact_service.py::SessionFactService.load`

### get_current_region

- **入口类型**：`agent_tool` —— Agent Tool —— 模型可直接调用
- **分组**：工具 / 事实查询（只读）
- **状态**：`available`
- **用途**：读取当前草稿中某个 region（区块）的完整内容，包含数据源、指标、时间范围、粒度和展示方式。用户要修改某个具体区块时，先用 get_current_draft 找到 regionId，再用本工具看细节。
- **参数**（JSON Schema，取自真实契约）：

```json
{
  "type": "object",
  "properties": {
    "regionId": {
      "type": "string",
      "description": "目标区块 ID，先从 get_current_draft 拿到"
    }
  },
  "required": [
    "regionId"
  ]
}
```

- **必填参数**：regionId
- **目标 / 引用要求**：regionId 必须来自 get_current_draft 返回的区块 id（不按标题猜）。
- **返回值**：{found: bool, region: 区块详情}；找不到时附 availableRegionIds
- **适用节点**：DEFINITION_DRAFT_READY、WAITING_USER_CONFIRM、USER_EDITING_TEMPLATE、VALIDATION_FAILED
- **只读**：是
- **副作用**：只读：不改草稿、不写业务数据。
- **主要错误**：
  - 没有草稿上下文 → found=false + 原因
  - regionId 不存在 → found=false + 可用区块 id 清单
- **运行限制**：
  - 一次只读一个区块
- **源码定位**：
  - registration：`app/tools/registry.py::_TOOLS`
  - dispatch：`app/tools/get_current_region_tool.py::run`
  - validation：`app/tools/get_current_region_tool.py::PARAMETERS`
  - execution：`app/tools/context.py::current_tool_context`

### get_current_session_status

- **入口类型**：`agent_tool` —— Agent Tool —— 模型可直接调用
- **分组**：工具 / 事实查询（只读）
- **状态**：`available`
- **用途**：读取当前对话服务端已记录的业务阶段和待确认事项：businessStage（已记录的阶段，不是实时任务状态）、pendingEditPlan（待确认修改计划）、pendingChoice（必须选择的菜单）、savedDefinition（已保存的定义）、preview（已生成的报表预览）、clarificationRounds（同一项修改已追问的轮数）。返回的 status 是事实状态：ABSENT 表示当前对话确实没有这些内容，UNAVAILABLE 表示暂时无法确认。不要把读不到说成没有，也不要据此编造实时进度、百分比或预计完成时间。
- **参数**：与上文「get_current_draft」的参数契约完全相同（完整内容见文件末尾机器可读元数据的 `entries` 字段）
- **必填参数**：（无）
- **目标 / 引用要求**：无参数。
- **返回值**：{status, scope, sessionExists, businessStage, hasDraft, reportName, draftRevision, pendingSteps, pendingEditPlan, pendingChoice, savedDefinition, preview, clarificationRounds, source, reason}
- **适用节点**：ANALYZING、NEEDS_CLARIFICATION、DEFINITION_DRAFT_READY、WAITING_USER_CONFIRM、USER_EDITING_TEMPLATE、VALIDATION_FAILED、SAVE_DEFINITION_DRAFT、VALIDATING、PREVIEWING、PREVIEW_READY、RENDERING、READY、INTERRUPT_CONFIRMING、BLOCKED_OR_UNSUPPORTED
- **只读**：是
- **副作用**：只读：不改草稿、不写业务数据。
- **主要错误**：
  - 会话记录不存在 → 返回 status 与 reason，不报错
- **运行限制**：
  - 只读当前会话
- **源码定位**：
  - registration：`app/tools/registry.py::_TOOLS`
  - dispatch：`app/tools/get_current_session_status_tool.py::run`
  - validation：`app/tools/get_current_session_status_tool.py::PARAMETERS`
  - execution：`app/services/session_fact_service.py::SessionFactService.load`

### get_validation_result

- **入口类型**：`agent_tool` —— Agent Tool —— 模型可直接调用
- **分组**：工具 / 事实查询（只读）
- **状态**：`available`
- **用途**：读取当前草稿的校验结果，返回每条问题的 code、所在路径、原因和修改建议。校验没过时要修复草稿，先调用它拿到真实问题，再据此提出修改方案，不要凭空猜测哪里有问题。
- **参数**：与上文「get_current_draft」的参数契约完全相同（完整内容见文件末尾机器可读元数据的 `entries` 字段）
- **必填参数**：（无）
- **目标 / 引用要求**：无参数。
- **返回值**：{available: bool, passed: bool, issues: 问题[], total, truncated}
- **适用节点**：VALIDATION_FAILED
- **只读**：是
- **副作用**：只读：不改草稿、不写业务数据。
- **主要错误**：
  - 当前没有校验上下文 → available=false + 原因
  - 还没有跑过校验 → available=false + 原因
- **运行限制**：
  - 只在 VALIDATION_FAILED 节点可用
- **源码定位**：
  - registration：`app/tools/registry.py::_TOOLS`
  - dispatch：`app/tools/get_validation_result_tool.py::run`
  - validation：`app/tools/get_validation_result_tool.py::PARAMETERS`
  - execution：`app/tools/context.py::current_tool_context`

### get_recorded_progress

- **入口类型**：`agent_tool` —— Agent Tool —— 模型可直接调用
- **分组**：工具 / 事实查询（只读）
- **状态**：`available`
- **用途**：读取当前对话已记录的业务进度：已记录阶段、报表草稿（名称与版本）、待确认修改计划、已保存定义、报表预览，以及可确定的下一步。用户问「刚才那份做到哪了」「现在什么状态」「进度如何」时调用。结果只反映服务端已记录的阶段，不是实时任务监控：没有百分比，也没有预计完成时间。查进度不需要也不能调用保存、校验或预览。
- **参数**：与上文「get_current_draft」的参数契约完全相同（完整内容见文件末尾机器可读元数据的 `entries` 字段）
- **必填参数**：（无）
- **目标 / 引用要求**：无参数；会话 id 取自请求级上下文。
- **返回值**：{status: FOUND/ABSENT/UNAVAILABLE/FORBIDDEN, ...}（四态事实状态）
- **适用节点**：ANALYZING、NEEDS_CLARIFICATION、DEFINITION_DRAFT_READY、WAITING_USER_CONFIRM、USER_EDITING_TEMPLATE、VALIDATION_FAILED、SAVE_DEFINITION_DRAFT、VALIDATING、PREVIEWING、PREVIEW_READY、RENDERING、READY、INTERRUPT_CONFIRMING、BLOCKED_OR_UNSUPPORTED
- **只读**：是
- **副作用**：只读：不改草稿、不写业务数据。
- **主要错误**：
  - 上下文缺失 → UNAVAILABLE；越权 → FORBIDDEN，不返回细节
- **运行限制**：
  - 只读当前会话的已记录进度
- **源码定位**：
  - registration：`app/tools/registry.py::_TOOLS`
  - dispatch：`app/tools/get_recorded_progress_tool.py::run`
  - validation：`app/tools/get_recorded_progress_tool.py::PARAMETERS`
  - execution：`app/services/recorded_progress_service.py::describe_recorded_progress`

### check_product_capability

- **入口类型**：`agent_tool` —— Agent Tool —— 模型可直接调用
- **分组**：工具 / 事实查询（只读）
- **状态**：`available`
- **用途**：核对一项报表能力在当前产品链路的真实边界：目录是否登记、生成/编辑/预览链路是否真的支持、预览数值来自模拟业务数据还是已接通生产取数。用户问「支持分钟趋势吗」「能耗成本或 PUE 能出吗」「这个源能做趋势和排名吗」时用它。只回答能力边界，不创建、不修改任何报表。返回 outcome 区分：OK（支持）、REGISTERED_NOT_SUPPORTED（目录登记但链路不支持）、NOT_REGISTERED（授权目录里没有）、NOT_AUTHORIZED（无权限）、MISSING_CONTEXT（上下文不足，已给产品范围）。
- **参数**（JSON Schema，取自真实契约）：

```json
{
  "type": "object",
  "properties": {
    "sourceRef": {
      "type": "string",
      "description": "可选，数据源 ID，如 catalog.machine_room.energy 或 eas_energy_detail"
    },
    "metricRef": {
      "type": "string",
      "description": "可选，指标 ID，如 metric.machine_room.total_energy"
    },
    "grainUnit": {
      "type": "string",
      "enum": [
        "MINUTE",
        "HOUR",
        "DAY",
        "WEEK",
        "MONTH"
      ],
      "description": "可选，时间粒度"
    },
    "component": {
      "type": "string",
      "enum": [
        "total",
        "ranking",
        "trend",
        "quality",
        "analysis"
      ],
      "description": "可选，报表区块"
    }
  }
}
```

- **必填参数**：（无）
- **目标 / 引用要求**：sourceRef / metricRef / grainUnit / component 均可空，按传入项核对。
- **返回值**：{status: 四态事实状态, outcome: 领域结论, ...}
- **适用节点**：ANALYZING、NEEDS_CLARIFICATION、DEFINITION_DRAFT_READY、WAITING_USER_CONFIRM、USER_EDITING_TEMPLATE、VALIDATION_FAILED、SAVE_DEFINITION_DRAFT、VALIDATING、PREVIEWING、PREVIEW_READY、RENDERING、READY、INTERRUPT_CONFIRMING、BLOCKED_OR_UNSUPPORTED
- **只读**：是
- **副作用**：只读：不改草稿、不写业务数据。
- **主要错误**：
  - 授权范围缺失 → MISSING_CONTEXT（事实查到，限制在别处披露）
  - 未登记 → NOT_REGISTERED（折算为 ABSENT）；越权 → NOT_AUTHORIZED（折算为 FORBIDDEN）
- **运行限制**：
  - 只核对能力边界，不执行修改
  - grainUnit 取值域：MINUTE、HOUR、DAY、WEEK、MONTH
  - component 取值域：total、ranking、trend、quality、analysis
- **源码定位**：
  - registration：`app/tools/registry.py::_TOOLS`
  - dispatch：`app/tools/check_product_capability_tool.py::run`
  - validation：`app/tools/check_product_capability_tool.py::PARAMETERS`
  - execution：`app/services/product_capability_service.py::describe_capability`

### list_simulated_dimensions

- **入口类型**：`agent_tool` —— Agent Tool —— 模型可直接调用
- **分组**：工具 / 事实查询（只读）
- **状态**：`available`
- **用途**：列出当前可用的**模拟分类维度**（如告警等级、告警区域、告警类型），每个维度有哪些分类、各是多少模拟数值。新增分类统计图表（饼图 / 柱状图）时先用它确认维度名，再把 dimension 填进新增图表的参数里。同时列出当前可绑定的**模拟单值字段**（指标卡 / 指标组 / 文本占位符用）与**注册图标**（指标图标用）—— 先查清单再填 fieldRef / iconRef，不要凭名字编字段或图标。这些数值都是**模拟**的，不接真实告警源。
- **参数**：与上文「get_current_draft」的参数契约完全相同（完整内容见文件末尾机器可读元数据的 `entries` 字段）
- **必填参数**：（无）
- **目标 / 引用要求**：无参数。
- **返回值**：{status, dimensions: 维度[], payloadContract: 载荷契约, note}
- **适用节点**：WAITING_USER_CONFIRM、USER_EDITING_TEMPLATE
- **只读**：是
- **副作用**：只读：不改草稿、不写业务数据。
- **主要错误**：
  - 维度目录不可用时抛错，由统一注册层转成 error 结果
- **运行限制**：
  - 维度只有三个（alertLevel / alertRegion / alertType）
  - 数值均为**模拟**数据，载荷带 simulated 标记，不接真实告警源
- **源码定位**：
  - registration：`app/tools/registry.py::_TOOLS`
  - dispatch：`app/tools/list_simulated_dimensions_tool.py::run`
  - validation：`app/tools/list_simulated_dimensions_tool.py::PARAMETERS`
  - execution：`app/services/edit_capabilities/chart_data.py::dimension_catalog`

### search_edit_experience

- **入口类型**：`agent_tool` —— Agent Tool —— 模型可直接调用
- **分组**：工具 / 事实查询（只读）
- **状态**：`available`
- **用途**：检索已批准且未禁用的编辑经验（方法 + 适用条件）。
- **参数**（JSON Schema，取自真实契约）：

```json
{
  "type": "object",
  "properties": {
    "query": {
      "type": "string",
      "description": "用户本次修改要求的关键词"
    },
    "maxResults": {
      "type": "integer",
      "description": "最多返回几条，默认 3，上限 3"
    }
  },
  "required": [
    "query"
  ]
}
```

- **必填参数**：query
- **目标 / 引用要求**：见参数契约。
- **返回值**：事实查询结果；具体字段见该工具的实现。
- **适用节点**：USER_EDITING_TEMPLATE、WAITING_USER_CONFIRM
- **只读**：是
- **副作用**：只读：不改草稿、不写业务数据。
- **主要错误**：
  - 没有授权范围时直接报错，不返回空结果冒充「查不到」
  - 工具异常会被统一注册层截断成 error 结果返回
- **运行限制**：
  - 结果条数受 observation_max_items 约束
- **源码定位**：
  - registration：`app/tools/registry.py::_TOOLS`
  - dispatch：`app/tools/search_edit_experience_tool.py::run`
  - validation：`app/tools/search_edit_experience_tool.py::PARAMETERS`
  - execution：`app/services/edit_experience_service.py::search_experiences`

### build_definition_draft

- **入口类型**：`agent_tool` —— Agent Tool —— 模型可直接调用
- **分组**：工具 / 确定性组装
- **状态**：`available`
- **用途**：根据报表计划组装完整的 ReportPolicyDraft 和 ReportDefinitionDraft。Agent 必须通过本工具生成草稿，禁止自行拼接协议结构。用户明确指定的数据源、粒度、时间窗、过滤、TopN、派生列必须通过参数传进来，不要依赖默认值；条件查不到或不支持时本工具会报错，此时应改条件或明确告知用户不支持。
- **参数**（JSON Schema，取自真实契约）：

```json
{
  "type": "object",
  "properties": {
    "reportName": {
      "type": "string",
      "description": "报表名称"
    },
    "roomId": {
      "type": "string",
      "description": "机房 ID，如 room_a"
    },
    "includeTrend": {
      "type": "boolean",
      "description": "是否包含小时趋势图",
      "default": true
    },
    "sourceRef": {
      "type": "string",
      "description": "数据源 ID，如 catalog.machine_room.energy 或 eas_energy_detail。用户点名了数据源就必须原样传入。"
    },
    "grainUnit": {
      "type": "string",
      "enum": [
        "MINUTE",
        "HOUR",
        "DAY",
        "WEEK",
        "MONTH"
      ],
      "description": "时间粒度。用户说“分钟/小时/每天”时必须传。"
    },
    "timeWindow": {
      "type": "object",
      "description": "相对时间窗，形如 {\"unit\":\"DAY\",\"count\":7,\"offset\":-7} 表示过去 7 个完整自然日；offset 省略时按 -count 推导。",
      "properties": {
        "unit": {
          "type": "string",
          "enum": [
            "MINUTE",
            "HOUR",
            "DAY",
            "WEEK",
            "MONTH"
          ]
        },
        "count": {
          "type": "integer",
          "minimum": 1
        },
        "offset": {
          "type": "integer"
        },
        "timezone": {
          "type": "string"
        }
      }
    },
    "metricRefs": {
      "type": "array",
      "items": {
        "type": "string"
      },
      "description": "指标引用列表，如 metric.machine_room.total_energy"
    },
    "componentRequests": {
      "type": "array",
      "items": {
        "type": "string"
      },
      "description": "要生成的区块：total / ranking / trend / quality / analysis。用户明确说“只要某几块”时按白名单传；可写成 \"trend:DAY\" 指定粒度。"
    },
    "excludedRegions": {
      "type": "array",
      "items": {
        "type": "string"
      },
      "description": "用户明确不要的区块，例如 ranking / trend / analysis"
    },
    "filters": {
      "type": "array",
      "description": "过滤条件，形如 [{\"fieldRef\":\"deviceName\",\"op\":\"IN\",\"values\":[\"CRAC-01 空调\"]}]",
      "items": {
        "type": "object",
        "properties": {
          "fieldRef": {
            "type": "string"
          },
          "op": {
            "type": "string",
            "enum": [
              "IN",
              "NOT_IN",
              "EQ",
              "NE",
              "GT",
              "LT"
            ]
          },
          "values": {
            "type": "array",
            "items": {
              "type": "string"
            }
          }
        },
        "required": [
          "fieldRef"
        ]
      }
    },
    "limit": {
      "type": "integer",
      "description": "排名显示行数（如 Top3 传 3）"
    },
    "topN": {
      "type": "integer",
      "description": "排名显示行数（与 limit 等价）"
    },
    "derivedColumns": {
      "type": "array",
      "description": "派生列。kind 取 PREVIOUS_PERIOD（前一日值）/ GROWTH_RATE（增长率）/ SHARE_OF_TOTAL（占机房总耗电比例）",
      "items": {
        "type": "object",
        "properties": {
          "id": {
            "type": "string"
          },
          "label": {
            "type": "string"
          },
          "fieldRef": {
            "type": "string"
          },
          "kind": {
            "type": "string",
            "enum": [
              "PREVIOUS_PERIOD",
              "GROWTH_RATE",
              "SHARE_OF_TOTAL"
            ]
          },
          "sourceFieldRef": {
            "type": "string"
          },
          "format": {
            "type": "object"
          }
        },
        "required": [
          "kind"
        ]
      }
    },
    "styleHints": {
      "type": "array",
      "description": "条件样式，形如 [{\"targetRegionId\":\"region_device_ranking\",\"fieldRef\":\"growthRatePercent\",\"comparison\":\"GT\",\"threshold\":20,\"style\":\"RED\"}]",
      "items": {
        "type": "object",
        "properties": {
          "targetRegionId": {
            "type": "string"
          },
          "fieldRef": {
            "type": "string"
          },
          "comparison": {
            "type": "string",
            "enum": [
              "GT",
              "GTE",
              "LT",
              "LTE",
              "EQ"
            ]
          },
          "threshold": {
            "type": "number"
          },
          "style": {
            "type": "string"
          }
        },
        "required": [
          "fieldRef"
        ]
      }
    },
    "capabilityEvidence": {
      "type": "object",
      "description": "模型查证到的能力证据（如 check_capability 的返回），原样留档"
    }
  },
  "required": [
    "reportName",
    "roomId"
  ]
}
```

- **必填参数**：reportName、roomId
- **目标 / 引用要求**：按草稿计划的载荷组装。
- **返回值**：组装出的草稿定义与 ChangeSet（服务端现场，不改正式草稿）。
- **适用节点**：ANALYZING、NEEDS_CLARIFICATION
- **只读**：是
- **副作用**：在服务端现场组装草稿，不直接改正式草稿。
- **主要错误**：
  - 组装失败（口径不兼容等）→ 抛错，由统一注册层转成 error 结果
- **运行限制**：
  - 只在 ANALYZING / NEEDS_CLARIFICATION 节点可用
  - grainUnit 取值域：MINUTE、HOUR、DAY、WEEK、MONTH
- **源码定位**：
  - registration：`app/tools/registry.py::_TOOLS`
  - dispatch：`app/tools/build_definition_draft_tool.py::run`
  - validation：`app/tools/build_definition_draft_tool.py::PARAMETERS`
  - execution：`app/services/report_draft_service.py::build_policy_draft`

### submit_draft_plan

- **入口类型**：`agent_tool` —— Agent Tool —— 模型可直接调用
- **分组**：工具 / 终结提交
- **状态**：`available`
- **用途**：提交本轮生成的报表计划，这是本轮的最终结论，调用后不要再调用其他工具。reportName 是报表名，roomId 是机房 ID，includeTrend 表示是否包含趋势图。确认数据源、指标、粒度都查得到之后再提交。用户明确说过的条件必须一并提交，不要只报名字：sourceRef（数据源）、grainUnit（粒度）、timeWindow（时间窗）、metricRefs（指标）、componentRequests（要哪些区块）、excludedRegions（不要哪些区块）、filters（过滤）、topN（排名行数）、derivedColumns（派生列）、styleHints（条件样式）。
- **参数**（JSON Schema，取自真实契约）：

```json
{
  "$defs": {
    "DerivedColumnSpec": {
      "description": "一个派生列（上一周期值 / 增长率 / 占比等）。",
      "properties": {
        "id": {
          "default": "",
          "title": "Id",
          "type": "string"
        },
        "label": {
          "default": "",
          "title": "Label",
          "type": "string"
        },
        "fieldRef": {
          "default": "",
          "title": "Fieldref",
          "type": "string"
        },
        "kind": {
          "default": "",
          "title": "Kind",
          "type": "string"
        },
        "sourceFieldRef": {
          "default": "energyKwh",
          "title": "Sourcefieldref",
          "type": "string"
        },
        "format": {
          "additionalProperties": true,
          "title": "Format",
          "type": "object"
        }
      },
      "title": "DerivedColumnSpec",
      "type": "object"
    },
    "FilterSpec": {
      "description": "一条过滤条件。",
      "properties": {
        "fieldRef": {
          "default": "",
          "title": "Fieldref",
          "type": "string"
        },
        "op": {
          "default": "IN",
          "title": "Op",
          "type": "string"
        },
        "values": {
          "items": {
            "type": "string"
          },
          "title": "Values",
          "type": "array"
        }
      },
      "title": "FilterSpec",
      "type": "object"
    },
    "StyleHintSpec": {
      "description": "条件样式：例如\"日增长率严格超过 20% 的行标红\"。",
      "properties": {
        "targetRegionId": {
          "default": "",
          "title": "Targetregionid",
          "type": "string"
        },
        "rule": {
          "default": "HIGHLIGHT_ABOVE_THRESHOLD",
          "title": "Rule",
          "type": "string"
        },
        "fieldRef": {
          "default": "",
          "title": "Fieldref",
          "type": "string"
        },
        "comparison": {
          "default": "GT",
          "title": "Comparison",
          "type": "string"
        },
        "threshold": {
          "default": 0.0,
          "title": "Threshold",
          "type": "number"
        },
        "style": {
          "default": "RED",
          "title": "Style",
          "type": "string"
        }
      },
      "title": "StyleHintSpec",
      "type": "object"
    }
  },
  "description": "节点 1：生成报表的计划。\n\n旧字段（reportName / roomId / includeTrend）保持不变；\n新字段全部可选，用于把路由层已经解析出来的结构化条件\n（数据源 / 粒度 / 时间窗 / 指标 / 过滤 / TopN / 派生列 / 排除区域）\n完整地带进确定性组装层，不再被默认值覆盖。",
  "properties": {
    "reportName": {
      "title": "Reportname",
      "type": "string"
    },
    "roomId": {
      "title": "Roomid",
      "type": "string"
    },
    "includeTrend": {
      "default": true,
      "title": "Includetrend",
      "type": "boolean"
    },
    "reply": {
      "default": "",
      "title": "Reply",
      "type": "string"
    },
    "sourceRef": {
      "anyOf": [
        {
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "default": null,
      "title": "Sourceref"
    },
    "grainUnit": {
      "anyOf": [
        {
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "default": null,
      "title": "Grainunit"
    },
    "timeWindow": {
      "anyOf": [
        {
          "additionalProperties": true,
          "type": "object"
        },
        {
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "default": null,
      "title": "Timewindow"
    },
    "metricRefs": {
      "items": {
        "type": "string"
      },
      "title": "Metricrefs",
      "type": "array"
    },
    "componentRequests": {
      "items": {
        "type": "string"
      },
      "title": "Componentrequests",
      "type": "array"
    },
    "excludedRegions": {
      "items": {
        "type": "string"
      },
      "title": "Excludedregions",
      "type": "array"
    },
    "filters": {
      "items": {
        "$ref": "#/$defs/FilterSpec"
      },
      "title": "Filters",
      "type": "array"
    },
    "limit": {
      "anyOf": [
        {
          "type": "integer"
        },
        {
          "type": "null"
        }
      ],
      "default": null,
      "title": "Limit"
    },
    "topN": {
      "anyOf": [
        {
          "type": "integer"
        },
        {
          "type": "null"
        }
      ],
      "default": null,
      "title": "Topn"
    },
    "derivedColumns": {
      "items": {
        "$ref": "#/$defs/DerivedColumnSpec"
      },
      "title": "Derivedcolumns",
      "type": "array"
    },
    "styleHints": {
      "items": {
        "$ref": "#/$defs/StyleHintSpec"
      },
      "title": "Stylehints",
      "type": "array"
    },
    "capabilityEvidence": {
      "additionalProperties": true,
      "title": "Capabilityevidence",
      "type": "object"
    }
  },
  "required": [
    "reportName",
    "roomId"
  ],
  "title": "DraftPlanPayload",
  "type": "object"
}
```

- **必填参数**：reportName、roomId
- **目标 / 引用要求**：无（面向用户的生成计划）。
- **返回值**：{accepted: true, tool: 'submit_draft_plan'}
- **适用节点**：ANALYZING、NEEDS_CLARIFICATION
- **只读**：否
- **副作用**：写本轮**提交槽**（会话内），本轮结论由业务 Agent 取走；草稿的正式改动由用户接受候选后的事务落库。
- **主要错误**：
  - 载荷不合法 → {accepted: false, error: '载荷不合法：...'}
- **运行限制**：
  - 终结工具：一轮只认第一次提交
- **源码定位**：
  - registration：`app/tools/registry.py::_TOOLS`
  - dispatch：`app/tools/submit_draft_plan_tool.py::run`
  - validation：`app/tools/schemas.py::DraftPlanPayload`
  - execution：`app/tools/submission.py::submit_payload`

### submit_clarification

- **入口类型**：`agent_tool` —— Agent Tool —— 模型可直接调用
- **分组**：工具 / 终结提交
- **状态**：`available`
- **用途**：提交需要向用户追问的信息，这是本轮的最终结论，调用后不要再调用其他工具。missingInfo 列出缺哪些信息（expects 指明是文本、单选、多选还是日期范围），nextQuestion 是要问用户的具体问题，能自动回填的放进 autoFillable。
- **参数**（JSON Schema，取自真实契约）：

```json
{
  "$defs": {
    "ClarificationField": {
      "description": "需要补的一个信息项。",
      "properties": {
        "field": {
          "title": "Field",
          "type": "string"
        },
        "label": {
          "default": "",
          "title": "Label",
          "type": "string"
        },
        "expects": {
          "default": "TEXT",
          "title": "Expects",
          "type": "string"
        },
        "options": {
          "items": {
            "$ref": "#/$defs/QuestionOption"
          },
          "title": "Options",
          "type": "array"
        },
        "required": {
          "default": true,
          "title": "Required",
          "type": "boolean"
        }
      },
      "required": [
        "field"
      ],
      "title": "ClarificationField",
      "type": "object"
    },
    "QuestionOption": {
      "properties": {
        "value": {
          "title": "Value",
          "type": "string"
        },
        "label": {
          "title": "Label",
          "type": "string"
        }
      },
      "required": [
        "value",
        "label"
      ],
      "title": "QuestionOption",
      "type": "object"
    }
  },
  "description": "节点 2：缺信息，需要追问。",
  "properties": {
    "missingInfo": {
      "items": {
        "$ref": "#/$defs/ClarificationField"
      },
      "title": "Missinginfo",
      "type": "array"
    },
    "nextQuestion": {
      "default": "",
      "title": "Nextquestion",
      "type": "string"
    },
    "autoFillable": {
      "anyOf": [
        {
          "additionalProperties": true,
          "type": "object"
        },
        {
          "type": "null"
        }
      ],
      "default": null,
      "title": "Autofillable"
    },
    "reply": {
      "default": "",
      "title": "Reply",
      "type": "string"
    }
  },
  "title": "ClarificationPayload",
  "type": "object"
}
```

- **必填参数**：（无）
- **目标 / 引用要求**：无。
- **返回值**：{accepted: true, tool: 'submit_clarification'}
- **适用节点**：ANALYZING、NEEDS_CLARIFICATION、WAITING_USER_CONFIRM
- **只读**：否
- **副作用**：写本轮**提交槽**（会话内），本轮结论由业务 Agent 取走；草稿的正式改动由用户接受候选后的事务落库。
- **主要错误**：
  - 载荷不合法 → {accepted: false, error: '载荷不合法：...'}
- **运行限制**：
  - 终结工具：一轮只认第一次提交
- **源码定位**：
  - registration：`app/tools/registry.py::_TOOLS`
  - dispatch：`app/tools/submit_clarification_tool.py::run`
  - validation：`app/tools/schemas.py::ClarificationPayload`
  - execution：`app/tools/submission.py::submit_payload`

### submit_blocked

- **入口类型**：`agent_tool` —— Agent Tool —— 模型可直接调用
- **分组**：工具 / 终结提交
- **状态**：`available`
- **用途**：提交无法处理的结论（能力不支持 / 权限越界 / 诉求超出范围），这是本轮的最终结论，调用后不要再调用其他工具。message 说清原因，alternatives 给出用户可选的替代方案。
- **参数**（JSON Schema，取自真实契约）：

```json
{
  "description": "节点 3：能力不支持 / 权限越界 / 无法处理。",
  "properties": {
    "layer": {
      "default": "CAPABILITY",
      "title": "Layer",
      "type": "string"
    },
    "code": {
      "default": "",
      "title": "Code",
      "type": "string"
    },
    "message": {
      "default": "",
      "title": "Message",
      "type": "string"
    },
    "alternatives": {
      "items": {
        "type": "string"
      },
      "title": "Alternatives",
      "type": "array"
    },
    "reply": {
      "default": "",
      "title": "Reply",
      "type": "string"
    }
  },
  "title": "BlockedPayload",
  "type": "object"
}
```

- **必填参数**：（无）
- **目标 / 引用要求**：无。
- **返回值**：{accepted: true, tool: 'submit_blocked'}
- **适用节点**：ANALYZING、WAITING_USER_CONFIRM
- **只读**：否
- **副作用**：写本轮**提交槽**（会话内），本轮结论由业务 Agent 取走；草稿的正式改动由用户接受候选后的事务落库。
- **主要错误**：
  - 载荷不合法 → {accepted: false, error: '载荷不合法：...'}
- **运行限制**：
  - 终结工具：代表本轮判定为不支持 / 被拦截
- **源码定位**：
  - registration：`app/tools/registry.py::_TOOLS`
  - dispatch：`app/tools/submit_blocked_tool.py::run`
  - validation：`app/tools/schemas.py::BlockedPayload`
  - execution：`app/tools/submission.py::submit_payload`

### submit_change_plan

- **入口类型**：`agent_tool` —— Agent Tool —— 模型可直接调用
- **分组**：工具 / 终结提交
- **状态**：`available`
- **用途**：提交对当前草稿的修改方案，这是本轮的最终结论，调用后不要再调用其他工具。每处修改给出 regionId（改哪个区块）、path（改哪个属性）、after（改成什么）和 summary（为什么这么改）。
- **参数**（JSON Schema，取自真实契约）：

```json
{
  "$defs": {
    "ChangeProposal": {
      "description": "一处修改提议。",
      "properties": {
        "regionId": {
          "title": "Regionid",
          "type": "string"
        },
        "path": {
          "title": "Path",
          "type": "string"
        },
        "after": {
          "default": null,
          "title": "After"
        },
        "summary": {
          "default": "",
          "title": "Summary",
          "type": "string"
        }
      },
      "required": [
        "regionId",
        "path"
      ],
      "title": "ChangeProposal",
      "type": "object"
    }
  },
  "description": "节点 4：对已有草稿的修改方案。",
  "properties": {
    "baseDefinitionRevision": {
      "default": 0,
      "title": "Basedefinitionrevision",
      "type": "integer"
    },
    "changes": {
      "items": {
        "$ref": "#/$defs/ChangeProposal"
      },
      "title": "Changes",
      "type": "array"
    },
    "reply": {
      "default": "",
      "title": "Reply",
      "type": "string"
    }
  },
  "title": "ChangePlanPayload",
  "type": "object"
}
```

- **必填参数**：（无）
- **目标 / 引用要求**：修改方案的目标与操作。
- **返回值**：{accepted: true, tool: 'submit_change_plan'}
- **适用节点**：ANALYZING、DEFINITION_DRAFT_READY、WAITING_USER_CONFIRM
- **只读**：否
- **副作用**：写本轮**提交槽**（会话内），本轮结论由业务 Agent 取走；草稿的正式改动由用户接受候选后的事务落库。
- **主要错误**：
  - 载荷不合法 → {accepted: false, error: '载荷不合法：...'}
- **运行限制**：
  - 终结工具：一轮只认第一次提交
- **源码定位**：
  - registration：`app/tools/registry.py::_TOOLS`
  - dispatch：`app/tools/submit_change_plan_tool.py::run`
  - validation：`app/tools/schemas.py::ChangePlanPayload`
  - execution：`app/tools/submission.py::submit_payload`

### submit_edit_answer

- **入口类型**：`agent_tool` —— Agent Tool —— 模型可直接调用
- **分组**：工具 / 终结提交
- **状态**：`available`
- **用途**：查证当前草稿、授权数据源和能力后，只回答用户的问题。facts 填已核实事实，limitations 填限制，suggestions 只能提出建议；不得提交目标修改、操作或声称草稿已更改。调用后不要再调用工具。
- **参数**（JSON Schema，取自真实契约）：

```json
{
  "additionalProperties": false,
  "description": "编辑态查询的只回答结论，不承载任何修改操作。",
  "properties": {
    "reply": {
      "minLength": 1,
      "title": "Reply",
      "type": "string"
    },
    "facts": {
      "items": {
        "type": "string"
      },
      "title": "Facts",
      "type": "array"
    },
    "limitations": {
      "items": {
        "type": "string"
      },
      "title": "Limitations",
      "type": "array"
    },
    "suggestions": {
      "items": {
        "type": "string"
      },
      "title": "Suggestions",
      "type": "array"
    }
  },
  "required": [
    "reply"
  ],
  "title": "EditAnswerPayload",
  "type": "object"
}
```

- **必填参数**：reply
- **目标 / 引用要求**：无（只回答，不承载修改操作）。
- **返回值**：{accepted: true, tool: 'submit_edit_answer'}
- **适用节点**：WAITING_USER_CONFIRM、USER_EDITING_TEMPLATE
- **只读**：否
- **副作用**：写本轮**提交槽**（会话内），本轮结论由业务 Agent 取走；草稿的正式改动由用户接受候选后的事务落库。
- **主要错误**：
  - 载荷不合法 → {accepted: false, error: '载荷不合法：...'}
- **运行限制**：
  - 终结工具：只回答，不创建 EditPlan
- **源码定位**：
  - registration：`app/tools/registry.py::_TOOLS`
  - dispatch：`app/tools/submit_edit_answer_tool.py::run`
  - validation：`app/tools/schemas.py::EditAnswerPayload`
  - execution：`app/tools/submission.py::submit_payload`

### submit_edit_plan

- **入口类型**：`agent_tool` —— Agent Tool —— 模型可直接调用
- **分组**：工具 / 终结提交
- **状态**：`available`
- **用途**：提交对当前草稿的编辑计划，这是本轮的最终结论。两种提交方式：（1）**能力格式**（推荐）：operations 里每一项形如 {"op": "<能力名>", "target": {"regionId" 或 "ref"}, "params": {...}},服务端会先在草稿副本上**整批试做**；新增区块时在 params.ref 里起一个引用名，后续步骤用 target.ref 指它；试做失败会返回**具体错误**（哪一步、哪个目标、哪条约束、为什么，外加字段路径、收到的类型、要求的类型、该填在哪、最小结构示例），请按反馈改好 operations 后**再次调用本工具**，不要换别的出口；试做通过才形成待确认候选。（2）单步格式：target 指明改哪个区块（regionId），operation 指明怎么改。**能力名、参数、支持范围、分类维度与源码位置见本轮下发的能力总目录**（编辑说明末尾），不要凭记忆猜能力名或参数。needsClarification 为 true 时说明还缺参数并给出 clarificationQuestion。用户表达含糊（比如'改好看点'）时，把候选解释放进 options 让用户选，不要自己替用户决定。包装能力（子操作要放在它的 params 里，不要放在顶层）：**包装能力 SET_PRESENTATION**：它的 params.operations 是**非空列表**，{"op": "<操作名>", "params": {...}}；允许的 op：CREATE_GROUP、SET_GROUP_ARRANGEMENT、MOVE_REGION、UNGROUP、DELETE_GROUP、COPY_REGION、DELETE_REGION、HIDE_REGION、SHOW_REGION、SET_PLACEMENT、SET_STYLE、SET_FORMAT、SET_PAGE、SET_METADATA、SET_THEME。这些 op **不能**直接作为 operations 的顶层项 —— 顶层项是 SET_PRESENTATION 自己，它的子操作写在 params.operations 里。
- **参数**（JSON Schema，取自真实契约）：

```json
{
  "$defs": {
    "EditIntent": {
      "enum": [
        "RENAME_REPORT",
        "DELETE_SECTION",
        "DELETE_METRIC",
        "RENAME_SECTION",
        "ADD_SECTION",
        "ADD_METRIC",
        "CHANGE_DATA_SOURCE",
        "CHANGE_VISUALIZATION",
        "SUGGEST_VISUALIZATION",
        "CHANGE_COMPILATION_GRAIN",
        "CHANGE_TIME_RANGE",
        "CHANGE_METRIC",
        "CHANGE_TOP_N",
        "CHANGE_SORT",
        "APPLY_PRESENTATION",
        "SAVE_THEME",
        "UNKNOWN"
      ],
      "title": "EditIntent",
      "type": "string"
    },
    "EditMetricAddition": {
      "description": "要**追加**到某个已有区块里的一个指标。\n\n「追加」与「替换」是两种不同的意图：追加**保留**该区块原有指标，\n把新指标加进去；替换才把原来的换掉。\n\n每个待追加指标至少能表达：用户原话意图、显示名、单位、展示模式。",
      "properties": {
        "intent": {
          "default": "",
          "title": "Intent",
          "type": "string"
        },
        "label": {
          "default": "",
          "title": "Label",
          "type": "string"
        },
        "unit": {
          "default": "",
          "title": "Unit",
          "type": "string"
        },
        "displayMode": {
          "default": "simulated",
          "title": "Displaymode",
          "type": "string"
        },
        "metricRef": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Metricref"
        },
        "metricCandidate": {
          "anyOf": [
            {
              "additionalProperties": true,
              "type": "object"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Metriccandidate"
        },
        "note": {
          "default": "",
          "title": "Note",
          "type": "string"
        }
      },
      "title": "EditMetricAddition",
      "type": "object"
    },
    "EditMetricDeletion": {
      "description": "要**删除**的一个指标 —— 卡片内的**一个指标实例**。\n\n「删掉某个指标」与「删掉整张卡片」是两件事（契约表 §4.3）：目标必须\n指向卡片内**真实存在**的那一项指标。\n\n身份用**稳定标识**记（``metricKey`` / ``simulatedId`` / ``fieldRef``），\n**不记数组下标** —— 删掉中间一项之后下标会漂移；``label`` 只是给人看\n的显示名，**不作为定位依据**（同名指标要澄清，不能删第一条匹配的）。",
      "properties": {
        "metricKey": {
          "default": "",
          "title": "Metrickey",
          "type": "string"
        },
        "simulatedId": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Simulatedid"
        },
        "fieldRef": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Fieldref"
        },
        "label": {
          "default": "",
          "title": "Label",
          "type": "string"
        },
        "isPrimary": {
          "default": false,
          "title": "Isprimary",
          "type": "boolean"
        },
        "intent": {
          "default": "",
          "title": "Intent",
          "type": "string"
        }
      },
      "title": "EditMetricDeletion",
      "type": "object"
    },
    "EditOperation": {
      "description": "具体改成什么。字段按 intent 不同用到的子集不同；这里统一松结构承载。",
      "properties": {
        "newTitle": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Newtitle"
        },
        "componentType": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Componenttype"
        },
        "chartKind": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Chartkind"
        },
        "sectionTitle": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Sectiontitle"
        },
        "insertAfterRegionId": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Insertafterregionid"
        },
        "sourceRef": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Sourceref"
        },
        "sourceVersion": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Sourceversion"
        },
        "fieldMappingRequired": {
          "anyOf": [
            {
              "type": "boolean"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Fieldmappingrequired"
        },
        "metricRef": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Metricref"
        },
        "metricVersion": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Metricversion"
        },
        "metricIntent": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Metricintent"
        },
        "metricCandidate": {
          "anyOf": [
            {
              "additionalProperties": true,
              "type": "object"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Metriccandidate"
        },
        "baseDefinitionRevision": {
          "anyOf": [
            {
              "type": "integer"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Basedefinitionrevision"
        },
        "addMetrics": {
          "items": {
            "$ref": "#/$defs/EditMetricAddition"
          },
          "title": "Addmetrics",
          "type": "array"
        },
        "deleteMetrics": {
          "items": {
            "$ref": "#/$defs/EditMetricDeletion"
          },
          "title": "Deletemetrics",
          "type": "array"
        },
        "componentSelection": {
          "anyOf": [
            {
              "additionalProperties": true,
              "type": "object"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Componentselection"
        },
        "timeWindow": {
          "anyOf": [
            {
              "additionalProperties": true,
              "type": "object"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Timewindow"
        },
        "timeWindowLabel": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Timewindowlabel"
        },
        "grain": {
          "anyOf": [
            {
              "additionalProperties": true,
              "type": "object"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Grain"
        },
        "supported": {
          "anyOf": [
            {
              "type": "boolean"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Supported"
        },
        "supportedGrains": {
          "items": {
            "type": "string"
          },
          "title": "Supportedgrains",
          "type": "array"
        },
        "topN": {
          "anyOf": [
            {
              "type": "integer"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Topn"
        },
        "sortField": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Sortfield"
        },
        "sortDirection": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Sortdirection"
        },
        "dataShape": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Datashape"
        },
        "displayOptions": {
          "items": {
            "additionalProperties": true,
            "type": "object"
          },
          "title": "Displayoptions",
          "type": "array"
        },
        "disabledOptions": {
          "items": {
            "additionalProperties": true,
            "type": "object"
          },
          "title": "Disabledoptions",
          "type": "array"
        },
        "markdownPreview": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Markdownpreview"
        },
        "reason": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Reason"
        },
        "presentationOperations": {
          "items": {
            "additionalProperties": true,
            "type": "object"
          },
          "title": "Presentationoperations",
          "type": "array"
        },
        "themeOperations": {
          "items": {
            "additionalProperties": true,
            "type": "object"
          },
          "title": "Themeoperations",
          "type": "array"
        }
      },
      "title": "EditOperation",
      "type": "object"
    },
    "EditOperationStep": {
      "description": "组合修改中的一步：一次原子修改里的一个确定性动作。\n\n单项编辑不需要它；只有「换源 + 改粒度」这类组合计划才用到。\n\n两种形态：\n\n- **旧形态**（``intent`` + ``operation``）：既有编辑引擎的组合步骤，\n  所有步骤共用**同一个**顶层 target。\n- **新形态**（``op`` + ``target`` + ``params``）：能力目录的操作项，\n  **每一步有自己的目标**（既有区块或本次新建的稳定引用）。",
      "properties": {
        "intent": {
          "$ref": "#/$defs/EditIntent",
          "default": "UNKNOWN"
        },
        "operation": {
          "$ref": "#/$defs/EditOperation"
        },
        "summary": {
          "default": "",
          "title": "Summary",
          "type": "string"
        },
        "op": {
          "default": "",
          "title": "Op",
          "type": "string"
        },
        "target": {
          "$ref": "#/$defs/EditOperationTarget"
        },
        "params": {
          "additionalProperties": true,
          "title": "Params",
          "type": "object"
        }
      },
      "title": "EditOperationStep",
      "type": "object"
    },
    "EditOperationTarget": {
      "description": "一步操作的**目标**。\n\n两种写法，指向的都是**这一次修改现场**里的区块：\n\n- ``regionId``：权威草稿里**已有**的区块；\n- ``ref``：本次修改里**新建**的区块的稳定引用名（ADD_SECTION 时给出，\n  后续步骤用它指这一块）。\n\n不按标题猜目标：解析不出来就是错。",
      "properties": {
        "regionId": {
          "default": "",
          "title": "Regionid",
          "type": "string"
        },
        "ref": {
          "default": "",
          "title": "Ref",
          "type": "string"
        }
      },
      "title": "EditOperationTarget",
      "type": "object"
    },
    "EditTarget": {
      "description": "用户想改哪个 section。",
      "properties": {
        "regionId": {
          "default": "",
          "title": "Regionid",
          "type": "string"
        },
        "matchedText": {
          "default": "",
          "title": "Matchedtext",
          "type": "string"
        },
        "confidence": {
          "default": 0.0,
          "title": "Confidence",
          "type": "number"
        }
      },
      "title": "EditTarget",
      "type": "object"
    },
    "QuestionOption": {
      "properties": {
        "value": {
          "title": "Value",
          "type": "string"
        },
        "label": {
          "title": "Label",
          "type": "string"
        }
      },
      "required": [
        "value",
        "label"
      ],
      "title": "QuestionOption",
      "type": "object"
    }
  },
  "description": "节点 5.5：模板编辑计划。\n\n继承现有 ``EditPlan``，只多一个 ``options``：用户表达含糊时\n（例如\"改好看点\"）把候选解释列出来交给用户选，而不是擅自改。",
  "properties": {
    "isTemplateEdit": {
      "default": true,
      "title": "Istemplateedit",
      "type": "boolean"
    },
    "intent": {
      "$ref": "#/$defs/EditIntent",
      "default": "UNKNOWN"
    },
    "confidence": {
      "default": 0.0,
      "title": "Confidence",
      "type": "number"
    },
    "target": {
      "$ref": "#/$defs/EditTarget"
    },
    "operation": {
      "$ref": "#/$defs/EditOperation"
    },
    "needsClarification": {
      "default": false,
      "title": "Needsclarification",
      "type": "boolean"
    },
    "clarificationQuestion": {
      "default": "",
      "title": "Clarificationquestion",
      "type": "string"
    },
    "operations": {
      "items": {
        "$ref": "#/$defs/EditOperationStep"
      },
      "title": "Operations",
      "type": "array",
      "description": "能力格式的操作清单：每一项形如 {\"op\": \"<能力名>\", \"target\": {\"regionId\" 或 \"ref\"}, \"params\": {...}}。**包装能力**（子操作要放在它 params 里的列表字段中）不能把子操作直接写成本列表的顶层项 —— 见本轮能力总目录里每项能力的「提交位置」。"
    },
    "baseCombination": {
      "anyOf": [
        {
          "additionalProperties": true,
          "type": "object"
        },
        {
          "type": "null"
        }
      ],
      "default": null,
      "title": "Basecombination"
    },
    "editFacts": {
      "anyOf": [
        {
          "additionalProperties": true,
          "type": "object"
        },
        {
          "type": "null"
        }
      ],
      "default": null,
      "title": "Editfacts"
    },
    "originalRequest": {
      "default": "",
      "title": "Originalrequest",
      "type": "string"
    },
    "summary": {
      "default": "",
      "title": "Summary",
      "type": "string"
    },
    "options": {
      "items": {
        "$ref": "#/$defs/QuestionOption"
      },
      "title": "Options",
      "type": "array"
    },
    "reply": {
      "default": "",
      "title": "Reply",
      "type": "string"
    }
  },
  "title": "EditPlanPayload",
  "type": "object"
}
```

- **必填参数**：（无）
- **目标 / 引用要求**：target.regionId（或 target.ref 指向本次新增区块）；能力格式里每一步的 target 各自独立
- **返回值**：{accepted: true, tool: 'submit_edit_plan', checked, summary, resolvedOperations}；能力格式试做失败 → {accepted: false, error, errors, hint}
- **适用节点**：WAITING_USER_CONFIRM、USER_EDITING_TEMPLATE
- **只读**：否
- **副作用**：写本轮**提交槽**（会话内），本轮结论由业务 Agent 取走；草稿的正式改动由用户接受候选后的事务落库。
- **主要错误**：
  - 载荷不合法 → {accepted: false, error: '载荷不合法：...'}
  - 能力格式试做失败 → 返回**具体**错误（哪一步、哪个目标、哪条约束、为什么），草稿不变，按反馈改正 operations 后再次调用
  - 服务端读不到当前草稿 → {accepted: false, error}，草稿不变
- **运行限制**：
  - 终结工具：一轮只认第一次提交
  - 能力格式整批试做：任一步失败则整批不生效
- **源码定位**：
  - registration：`app/tools/registry.py::_TOOLS`
  - dispatch：`app/tools/submit_edit_plan_tool.py::run`
  - validation：`app/tools/schemas.py::EditPlanPayload`
  - execution：`app/services/edit_trial_service.py::run_trial`

### submit_fix_plan

- **入口类型**：`agent_tool` —— Agent Tool —— 模型可直接调用
- **分组**：工具 / 终结提交
- **状态**：`available`
- **用途**：提交校验问题的修复方案，这是本轮的最终结论，调用后不要再调用其他工具。先用 get_validation_result 拿到真实问题，再针对每条问题给出修复动作，不要凭空猜测哪里有问题。
- **参数**（JSON Schema，取自真实契约）：

```json
{
  "$defs": {
    "FixProposal": {
      "description": "针对一条校验问题的修复动作。",
      "properties": {
        "issueCode": {
          "title": "Issuecode",
          "type": "string"
        },
        "regionId": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Regionid"
        },
        "action": {
          "default": "",
          "title": "Action",
          "type": "string"
        },
        "summary": {
          "default": "",
          "title": "Summary",
          "type": "string"
        }
      },
      "required": [
        "issueCode"
      ],
      "title": "FixProposal",
      "type": "object"
    }
  },
  "description": "节点 8：校验失败后的修复方案。",
  "properties": {
    "fixes": {
      "items": {
        "$ref": "#/$defs/FixProposal"
      },
      "title": "Fixes",
      "type": "array"
    },
    "reply": {
      "default": "",
      "title": "Reply",
      "type": "string"
    }
  },
  "title": "FixPlanPayload",
  "type": "object"
}
```

- **必填参数**：（无）
- **目标 / 引用要求**：针对校验问题清单的修复动作。
- **返回值**：{accepted: true, tool: 'submit_fix_plan'}
- **适用节点**：VALIDATION_FAILED
- **只读**：否
- **副作用**：写本轮**提交槽**（会话内），本轮结论由业务 Agent 取走；草稿的正式改动由用户接受候选后的事务落库。
- **主要错误**：
  - 载荷不合法 → {accepted: false, error: '载荷不合法：...'}
- **运行限制**：
  - 终结工具：只在 VALIDATION_FAILED 节点可用
- **源码定位**：
  - registration：`app/tools/registry.py::_TOOLS`
  - dispatch：`app/tools/submit_fix_plan_tool.py::run`
  - validation：`app/tools/schemas.py::FixPlanPayload`
  - execution：`app/tools/submission.py::submit_payload`

### submit_query_answer

- **入口类型**：`agent_tool` —— Agent Tool —— 模型可直接调用
- **分组**：工具 / 终结提交
- **状态**：`available`
- **用途**：提交对用户问题的只读回答：facts 填已核实的事实，limitations 填限制与无法确认的原因，suggestions 只给建议。回答必须区分报表草稿、待确认修改计划和报表预览，不能把未确认的内容说成已经出数，也不能编造实时进度、百分比或预计完成时间。提交后本轮结束，不会生成、修改、保存、校验或预览任何报表。问题指向的对象不明确时，把 needsClarification 设为 true 并在clarificationQuestion 里问一个具体问题，不要替用户选择操作。
- **参数**（JSON Schema，取自真实契约）：

```json
{
  "additionalProperties": false,
  "description": "只读查询的答案提交（工单 #13 / #14 / #15 / #16 共用）。\n\n与 ``EditAnswerPayload`` 的区别：它适用于**所有**业务节点，而不只是编辑态；\n它还带一个「需要澄清」的分支，用来在只读提问上下文里做针对性澄清，\n而不是把用户推去补生成条件。",
  "properties": {
    "reply": {
      "minLength": 1,
      "title": "Reply",
      "type": "string"
    },
    "facts": {
      "items": {
        "type": "string"
      },
      "title": "Facts",
      "type": "array"
    },
    "limitations": {
      "items": {
        "type": "string"
      },
      "title": "Limitations",
      "type": "array"
    },
    "suggestions": {
      "items": {
        "type": "string"
      },
      "title": "Suggestions",
      "type": "array"
    },
    "needsClarification": {
      "default": false,
      "title": "Needsclarification",
      "type": "boolean"
    },
    "clarificationQuestion": {
      "default": "",
      "title": "Clarificationquestion",
      "type": "string"
    }
  },
  "required": [
    "reply"
  ],
  "title": "QueryAnswerPayload",
  "type": "object"
}
```

- **必填参数**：reply
- **目标 / 引用要求**：无。
- **返回值**：{accepted: true, tool: 'submit_query_answer'}
- **适用节点**：ANALYZING、NEEDS_CLARIFICATION、DEFINITION_DRAFT_READY、WAITING_USER_CONFIRM、USER_EDITING_TEMPLATE、VALIDATION_FAILED、SAVE_DEFINITION_DRAFT、VALIDATING、PREVIEWING、PREVIEW_READY、RENDERING、READY、INTERRUPT_CONFIRMING、BLOCKED_OR_UNSUPPORTED
- **只读**：否
- **副作用**：写本轮**提交槽**（会话内），本轮结论由业务 Agent 取走；草稿的正式改动由用户接受候选后的事务落库。
- **主要错误**：
  - 载荷不合法 → {accepted: false, error: '载荷不合法：...'}
- **运行限制**：
  - 终结工具：只在只读查询模式下注册（IS_QUERY_ONLY）
  - 只回答，不推进业务状态（结论状态 SAME）
- **源码定位**：
  - registration：`app/tools/registry.py::_TOOLS`
  - dispatch：`app/tools/submit_query_answer_tool.py::run`
  - validation：`app/tools/schemas.py::QueryAnswerPayload`
  - execution：`app/tools/submission.py::submit_payload`

## 二、清单能力（操作清单里的能力，由后端执行）

### ADD_SECTION（清单能力 / 新增与删除区块）

- **入口类型**：`operation` —— 后端执行的操作 —— 不是独立 Tool
- **分组**：清单能力 / 新增与删除区块
- **状态**：`available`
- **用途**：在草稿里新增一个区块（图表或文本）。图表必须给 chartKind（LINE / BAR / PIE / AREA）；分类统计图给 dimension（模拟分类维度）或 categories（分类与数值）。要用 ref 给这个新区块起一个稳定引用名，后续步骤用它指这块；replaceRegionId 让新区块插到那一块原来的位置。 （**操作清单里的能力，不是独立 Tool**：模型在 submit_edit_plan 的 operations 里给出 op + target + params，由后端在草稿副本上执行。）**提交位置**：submit_edit_plan 的 operations 里直接给一项 {"op": "<能力名>", "target": {...}, "params": {...}}。**包装要求**：{"op": "ADD_SECTION", "params": {"title": "<title>"}}。
- **参数**（JSON Schema，取自真实契约）：

```json
{
  "type": "object",
  "description": "ADD_SECTION 的参数对象。",
  "properties": {
    "title": {
      "type": "string",
      "minLength": 1
    },
    "componentType": {
      "type": "string",
      "description": "组件类型（TEXT / METRIC_CARD / TABLE / CHART）",
      "enum": [
        "ANALYSIS",
        "CHART",
        "CONTAINER",
        "DATA_QUALITY",
        "METRIC_CARD",
        "METRIC_GROUP",
        "TABLE",
        "TEXT"
      ],
      "default": "CHART"
    },
    "chartKind": {
      "type": "string",
      "description": "图表类型（componentType=CHART 时必填）",
      "enum": [
        "AREA",
        "BAR",
        "DONUT",
        "LINE",
        "PIE",
        "STACKED_BAR"
      ],
      "default": ""
    },
    "role": {
      "type": "string",
      "default": "DETAIL"
    },
    "dimension": {
      "type": "string",
      "description": "模拟分类维度（key 或业务别名）",
      "enum": [
        "alertLevel",
        "alertRegion",
        "alertType"
      ],
      "default": ""
    },
    "categories": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "label": {
            "type": "string",
            "minLength": 1
          },
          "value": {
            "type": "number",
            "default": 0
          }
        },
        "additionalProperties": false
      }
    },
    "unit": {
      "type": "string",
      "default": ""
    },
    "replaceRegionId": {
      "type": "string",
      "default": ""
    },
    "afterRegionId": {
      "type": "string",
      "default": ""
    },
    "beforeRegionId": {
      "type": "string",
      "default": ""
    },
    "ref": {
      "type": "string",
      "default": ""
    }
  },
  "required": [
    "title"
  ]
}
```

- **必填参数**：title
- **目标 / 引用要求**：每一步各自带 target：target.regionId 指向草稿里已有区块，target.ref 指向本次新增区块（ADD_SECTION 的 params.ref 起名）。
- **返回值**：CapabilityOutcome：ok / message / detail（含 regionId、title、chartKind）；试做失败时返回具体错误（哪一步、哪个目标、哪条约束、为什么，外加字段路径、收到的类型、要求的类型、该填在哪、最小示例）
- **适用节点**：WAITING_USER_CONFIRM、USER_EDITING_TEMPLATE
- **只读**：否
- **副作用**：只改**服务端草稿的隔离副本**；试做通过并由用户接受后，才在正式事务里写入草稿（版本 +1）。
- **主要错误**：
  - 能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=unsupported_component_type
  - 能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=unknown_dimension
  - 能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=missing_statistic
  - 能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=duplicate_ref
  - 能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=missing_chart_kind
  - 能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=unsupported_chart_kind
  - 能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=unknown_anchor
  - 参数不合法 → reason=invalid_params，不执行
  - 目标引用不认识 → reason=unknown_ref / missing_target / unknown_region
- **运行限制**：
  - 不需要目标（自己新建区块 / 整批作用于草稿）
  - 新增的区块可用 params.ref 起稳定引用名，后续步骤用 target.ref 指它
  - title 至少 1 个字符
  - componentType 取值域：ANALYSIS、CHART、CONTAINER、DATA_QUALITY、METRIC_CARD、METRIC_GROUP、TABLE、TEXT
  - chartKind 取值域：AREA、BAR、DONUT、LINE、PIE、STACKED_BAR
  - dimension 取值域：alertLevel、alertRegion、alertType
  - 提交位置：submit_edit_plan 的 operations 里直接给一项 {"op": "<能力名>", "target": {...}, "params": {...}}
  - 包装要求：{"op": "ADD_SECTION", "params": {"title": "<title>"}}
  - 最小结构示例（只给格式）：{"op": "ADD_SECTION", "params": {"title": "<title>"}}
- **源码定位**：
  - registration：`app/services/edit_capabilities/base.py::register`
  - dispatch：`app/tools/submit_edit_plan_tool.py::run`
  - validation：`app/services/edit_capabilities/base.py::validate_params`
  - execution：`app/services/edit_capabilities/blocks.py::_add_section`

### DELETE_SECTION（清单能力 / 新增与删除区块）

- **入口类型**：`operation` —— 后端执行的操作 —— 不是独立 Tool
- **分组**：清单能力 / 新增与删除区块
- **状态**：`available`
- **用途**：删除草稿里的一个区块。目标用 target.regionId 或 target.ref 指定。 （**操作清单里的能力，不是独立 Tool**：模型在 submit_edit_plan 的 operations 里给出 op + target + params，由后端在草稿副本上执行。）**提交位置**：submit_edit_plan 的 operations 里直接给一项 {"op": "<能力名>", "target": {...}, "params": {...}}。**包装要求**：{"op": "DELETE_SECTION", "target": {"regionId": "<regionId>"}, "params": {}}。
- **参数**（JSON Schema，取自真实契约）：

```json
{
  "type": "object",
  "description": "DELETE_SECTION 的参数对象。",
  "properties": {}
}
```

- **必填参数**：（无）
- **目标 / 引用要求**：每一步各自带 target：target.regionId 指向草稿里已有区块，target.ref 指向本次新增区块（ADD_SECTION 的 params.ref 起名）。
- **返回值**：CapabilityOutcome：ok / message / detail（含 regionId、title、chartKind）；试做失败时返回具体错误（哪一步、哪个目标、哪条约束、为什么，外加字段路径、收到的类型、要求的类型、该填在哪、最小示例）
- **适用节点**：WAITING_USER_CONFIRM、USER_EDITING_TEMPLATE
- **只读**：否
- **副作用**：只改**服务端草稿的隔离副本**；试做通过并由用户接受后，才在正式事务里写入草稿（版本 +1）。
- **主要错误**：
  - 能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=unknown_region
  - 参数不合法 → reason=invalid_params，不执行
  - 目标引用不认识 → reason=unknown_ref / missing_target / unknown_region
- **运行限制**：
  - 每一步都要有明确目标：target.regionId（草稿里已有区块）或 target.ref（本次新增区块的引用名）
  - 提交位置：submit_edit_plan 的 operations 里直接给一项 {"op": "<能力名>", "target": {...}, "params": {...}}
  - 包装要求：{"op": "DELETE_SECTION", "target": {"regionId": "<regionId>"}, "params": {}}
  - 最小结构示例（只给格式）：{"op": "DELETE_SECTION", "target": {"regionId": "<regionId>"}, "params": {}}
- **源码定位**：
  - registration：`app/services/edit_capabilities/base.py::register`
  - dispatch：`app/tools/submit_edit_plan_tool.py::run`
  - validation：`app/services/edit_capabilities/base.py::validate_params`
  - execution：`app/services/edit_capabilities/blocks.py::_delete_section`

### SET_DISPLAY

- **入口类型**：`operation` —— 后端执行的操作 —— 不是独立 Tool
- **分组**：清单能力 / 设置统计与展示
- **状态**：`available`
- **用途**：改一个区块的展示类型。componentType 取 TEXT / METRIC_CARD / TABLE / CHART；componentType=CHART 时必须给 chartKind（LINE / BAR / PIE / AREA）。 （**操作清单里的能力，不是独立 Tool**：模型在 submit_edit_plan 的 operations 里给出 op + target + params，由后端在草稿副本上执行。）**提交位置**：submit_edit_plan 的 operations 里直接给一项 {"op": "<能力名>", "target": {...}, "params": {...}}。**包装要求**：{"op": "SET_DISPLAY", "target": {"regionId": "<regionId>"}, "params": {}}。
- **参数**（JSON Schema，取自真实契约）：

```json
{
  "type": "object",
  "description": "SET_DISPLAY 的参数对象。",
  "properties": {
    "componentType": {
      "type": "string",
      "description": "组件类型（TEXT / METRIC_CARD / TABLE / CHART）",
      "enum": [
        "ANALYSIS",
        "CHART",
        "CONTAINER",
        "DATA_QUALITY",
        "METRIC_CARD",
        "METRIC_GROUP",
        "TABLE",
        "TEXT"
      ],
      "default": ""
    },
    "chartKind": {
      "type": "string",
      "description": "图表类型（componentType=CHART 时必填）",
      "enum": [
        "AREA",
        "BAR",
        "DONUT",
        "LINE",
        "PIE",
        "STACKED_BAR"
      ],
      "default": ""
    }
  }
}
```

- **必填参数**：（无）
- **目标 / 引用要求**：每一步各自带 target：target.regionId 指向草稿里已有区块，target.ref 指向本次新增区块（ADD_SECTION 的 params.ref 起名）。
- **返回值**：CapabilityOutcome：ok / message / detail（含 regionId、title、chartKind）；试做失败时返回具体错误（哪一步、哪个目标、哪条约束、为什么，外加字段路径、收到的类型、要求的类型、该填在哪、最小示例）
- **适用节点**：WAITING_USER_CONFIRM、USER_EDITING_TEMPLATE
- **只读**：否
- **副作用**：只改**服务端草稿的隔离副本**；试做通过并由用户接受后，才在正式事务里写入草稿（版本 +1）。
- **主要错误**：
  - 能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=unknown_region
  - 能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=missing_component_type
  - 能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=unsupported_component_type
  - 能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=missing_chart_kind
  - 能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=unsupported_chart_kind
  - 参数不合法 → reason=invalid_params，不执行
  - 目标引用不认识 → reason=unknown_ref / missing_target / unknown_region
- **运行限制**：
  - 每一步都要有明确目标：target.regionId（草稿里已有区块）或 target.ref（本次新增区块的引用名）
  - componentType 取值域：ANALYSIS、CHART、CONTAINER、DATA_QUALITY、METRIC_CARD、METRIC_GROUP、TABLE、TEXT
  - chartKind 取值域：AREA、BAR、DONUT、LINE、PIE、STACKED_BAR
  - 提交位置：submit_edit_plan 的 operations 里直接给一项 {"op": "<能力名>", "target": {...}, "params": {...}}
  - 包装要求：{"op": "SET_DISPLAY", "target": {"regionId": "<regionId>"}, "params": {}}
  - 最小结构示例（只给格式）：{"op": "SET_DISPLAY", "target": {"regionId": "<regionId>"}, "params": {}}
- **源码定位**：
  - registration：`app/services/edit_capabilities/base.py::register`
  - dispatch：`app/tools/submit_edit_plan_tool.py::run`
  - validation：`app/services/edit_capabilities/base.py::validate_params`
  - execution：`app/services/edit_capabilities/blocks.py::_set_display`

### SET_CATEGORY_DATA

- **入口类型**：`operation` —— 后端执行的操作 —— 不是独立 Tool
- **分组**：清单能力 / 设置统计与展示
- **状态**：`available`
- **用途**：给一个图表区块设置分类统计口径：dimension（模拟分类维度，如告警等级 / 告警区域）或 categories（分类与数值）。数值是模拟的，会在图上标注。 （**操作清单里的能力，不是独立 Tool**：模型在 submit_edit_plan 的 operations 里给出 op + target + params，由后端在草稿副本上执行。）**提交位置**：submit_edit_plan 的 operations 里直接给一项 {"op": "<能力名>", "target": {...}, "params": {...}}。**包装要求**：{"op": "SET_CATEGORY_DATA", "target": {"regionId": "<regionId>"}, "params": {}}。
- **参数**（JSON Schema，取自真实契约）：

```json
{
  "type": "object",
  "description": "SET_CATEGORY_DATA 的参数对象。",
  "properties": {
    "dimension": {
      "type": "string",
      "description": "模拟分类维度（key 或业务别名）",
      "enum": [
        "alertLevel",
        "alertRegion",
        "alertType"
      ],
      "default": ""
    },
    "categories": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "label": {
            "type": "string",
            "minLength": 1
          },
          "value": {
            "type": "number",
            "default": 0
          }
        },
        "additionalProperties": false
      }
    },
    "unit": {
      "type": "string",
      "default": ""
    }
  }
}
```

- **必填参数**：（无）
- **目标 / 引用要求**：每一步各自带 target：target.regionId 指向草稿里已有区块，target.ref 指向本次新增区块（ADD_SECTION 的 params.ref 起名）。
- **返回值**：CapabilityOutcome：ok / message / detail（含 regionId、title、chartKind）；试做失败时返回具体错误（哪一步、哪个目标、哪条约束、为什么，外加字段路径、收到的类型、要求的类型、该填在哪、最小示例）
- **适用节点**：WAITING_USER_CONFIRM、USER_EDITING_TEMPLATE
- **只读**：否
- **副作用**：只改**服务端草稿的隔离副本**；试做通过并由用户接受后，才在正式事务里写入草稿（版本 +1）。
- **主要错误**：
  - 能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=unknown_region
  - 能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=unknown_dimension
  - 能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=missing_statistic
  - 能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=duplicate_category
  - 参数不合法 → reason=invalid_params，不执行
  - 目标引用不认识 → reason=unknown_ref / missing_target / unknown_region
- **运行限制**：
  - 每一步都要有明确目标：target.regionId（草稿里已有区块）或 target.ref（本次新增区块的引用名）
  - dimension 取值域：alertLevel、alertRegion、alertType
  - 提交位置：submit_edit_plan 的 operations 里直接给一项 {"op": "<能力名>", "target": {...}, "params": {...}}
  - 包装要求：{"op": "SET_CATEGORY_DATA", "target": {"regionId": "<regionId>"}, "params": {}}
  - 最小结构示例（只给格式）：{"op": "SET_CATEGORY_DATA", "target": {"regionId": "<regionId>"}, "params": {}}
- **源码定位**：
  - registration：`app/services/edit_capabilities/base.py::register`
  - dispatch：`app/tools/submit_edit_plan_tool.py::run`
  - validation：`app/services/edit_capabilities/base.py::validate_params`
  - execution：`app/services/edit_capabilities/blocks.py::_set_category_data`

### SET_PRESENTATION

- **入口类型**：`operation` —— 后端执行的操作 —— 不是独立 Tool
- **分组**：清单能力 / 设置样式
- **状态**：`available`
- **用途**：一批受约束的展示操作（分组 / 排版 / 样式 / 格式 / 页面）。**这是一个包装能力**：展示操作名不是顶层能力，只能放在它的 params.operations 里，每一项形如 {"op": "<展示操作名>", "params": {...}}；operations 是**非空列表**。允许的 op、每个 op 的必填 / 可选键与取值域见能力总目录的「展示操作白名单」一节（由展示服务的白名单自动生成）。**区域级格式（SET_FORMAT）写的是整块卡片的格式**：指标卡 / 指标组里某个指标项的数字显示格式请用 SET_METRIC_ITEM（指标级）—— 区域级格式会连带改掉这张卡上别的指标的单位。 （**操作清单里的能力，不是独立 Tool**：模型在 submit_edit_plan 的 operations 里给出 op + target + params，由后端在草稿副本上执行。）**提交位置**：submit_edit_plan 的 operations 里 SET_PRESENTATION 这一项的 params.operations 里（每一项形如 {"op": "<展示操作名>", "params": {...}}）。**包装要求**：{"op": "SET_PRESENTATION", "params": {"operations": [{"op": "CREATE_GROUP", "params": {"groupId": "<groupId>", "memberRegionIds": ["<memberRegionIds>"]}}]}}。
- **参数**（JSON Schema，取自真实契约）：

```json
{
  "type": "object",
  "description": "SET_PRESENTATION 的参数对象。",
  "properties": {
    "operations": {
      "type": "array",
      "description": "外观修改包里的操作名单：非空列表，每一项形如 {\"op\": \"<展示操作名>\", \"params\": {...}}",
      "minItems": 1,
      "items": {
        "type": "object",
        "properties": {
          "op": {
            "type": "string",
            "description": "展示操作名（包装能力里允许的那些）",
            "enum": [
              "CREATE_GROUP",
              "SET_GROUP_ARRANGEMENT",
              "MOVE_REGION",
              "UNGROUP",
              "DELETE_GROUP",
              "COPY_REGION",
              "DELETE_REGION",
              "HIDE_REGION",
              "SHOW_REGION",
              "SET_PLACEMENT",
              "SET_STYLE",
              "SET_FORMAT",
              "SET_PAGE",
              "SET_METADATA",
              "SET_THEME"
            ]
          },
          "params": {
            "type": "object",
            "description": "该展示操作的参数对象",
            "additionalProperties": false
          }
        },
        "additionalProperties": false
      }
    }
  }
}
```

- **必填参数**：（无）
- **目标 / 引用要求**：每一步各自带 target：target.regionId 指向草稿里已有区块，target.ref 指向本次新增区块（ADD_SECTION 的 params.ref 起名）。
- **返回值**：CapabilityOutcome：ok / message / detail（含 regionId、title、chartKind）；试做失败时返回具体错误（哪一步、哪个目标、哪条约束、为什么，外加字段路径、收到的类型、要求的类型、该填在哪、最小示例）
- **适用节点**：WAITING_USER_CONFIRM、USER_EDITING_TEMPLATE
- **只读**：否
- **副作用**：只改**服务端草稿的隔离副本**；试做通过并由用户接受后，才在正式事务里写入草稿（版本 +1）。
- **主要错误**：
  - 能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=missing_operations
  - 能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=invalid_presentation_ops
  - 能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=presentation_failed
  - 参数不合法 → reason=invalid_params，不执行
  - 目标引用不认识 → reason=unknown_ref / missing_target / unknown_region
- **运行限制**：
  - 不需要目标（自己新建区块 / 整批作用于草稿）
  - operations 至少 1 项
  - 提交位置：submit_edit_plan 的 operations 里 SET_PRESENTATION 这一项的 params.operations 里（每一项形如 {"op": "<展示操作名>", "params": {...}}）
  - 包装要求：{"op": "SET_PRESENTATION", "params": {"operations": [{"op": "CREATE_GROUP", "params": {"groupId": "<groupId>", "memberRegionIds": ["<memberRegionIds>"]}}]}}
  - 最小结构示例（只给格式）：{"op": "SET_PRESENTATION", "params": {"operations": [{"op": "CREATE_GROUP", "params": {"groupId": "<groupId>", "memberRegionIds": ["<memberRegionIds>"]}}]}}
- **源码定位**：
  - registration：`app/services/edit_capabilities/base.py::register`
  - dispatch：`app/tools/submit_edit_plan_tool.py::run`
  - validation：`app/services/edit_capabilities/base.py::validate_params`
  - execution：`app/services/edit_capabilities/blocks.py::_set_presentation`

### SET_DATA_BINDING

- **入口类型**：`operation` —— 后端执行的操作 —— 不是独立 Tool
- **分组**：清单能力 / 设置统计与展示
- **状态**：`available`
- **用途**：设置这个区域**展示**用的绑定：outputRef（换绑已登记的输出）、fieldAliases（展示字段名 → 实际结果字段名）、presentationFilters（只过滤展示记录）、sort / topN（展示排序与前 N 项）。这些**只改展示**：源输出、全量总计和其他区域的输入不变；未登记的引用会被拒绝，不会自动新增跨区域数据依赖。 （**操作清单里的能力，不是独立 Tool**：模型在 submit_edit_plan 的 operations 里给出 op + target + params，由后端在草稿副本上执行。）**提交位置**：submit_edit_plan 的 operations 里直接给一项 {"op": "<能力名>", "target": {...}, "params": {...}}。**包装要求**：{"op": "SET_DATA_BINDING", "target": {"regionId": "<regionId>"}, "params": {}}。
- **参数**（JSON Schema，取自真实契约）：

```json
{
  "type": "object",
  "description": "SET_DATA_BINDING 的参数对象。",
  "properties": {
    "outputRef": {
      "type": "string",
      "description": "改绑的输出引用（output:<name>）",
      "default": ""
    },
    "fieldAliases": {
      "type": "object",
      "description": "展示字段名 → 实际结果字段名，例如 {\"时间点\":\"timestamp\"}。右侧必须是已声明字段；也兼容可唯一识别的反向映射。",
      "additionalProperties": false
    },
    "clearFieldAliases": {
      "type": "boolean",
      "default": false
    },
    "presentationFilters": {
      "type": "array",
      "description": "只过滤展示记录。条件形如 {\"type\":\"CONDITION\",\"left\":{\"type\":\"FIELD\",\"fieldRef\":\"energyKwh\"},\"operator\":\"GT\",\"operands\":[{\"type\":\"LITERAL\",\"value\":400}]}；组用 type=GROUP、logic=AND/OR/NOT、conditions=[条件]。也接收 fieldRef/operator/value 简写，统一转成上述结构。",
      "items": {
        "type": "object",
        "properties": {
          "type": {
            "type": "string",
            "enum": [
              "CONDITION",
              "GROUP"
            ]
          },
          "left": {
            "type": "object",
            "properties": {
              "type": {
                "type": "string",
                "enum": [
                  "FIELD"
                ]
              },
              "fieldRef": {
                "type": "string",
                "description": "已声明的实际字段名"
              }
            },
            "additionalProperties": false
          },
          "operator": {
            "type": "string",
            "enum": [
              "EQ",
              "NE",
              "GT",
              "GE",
              "LT",
              "LE",
              "IN",
              "NOT_IN",
              "BETWEEN",
              "CONTAINS",
              "STARTS_WITH",
              "IS_NULL",
              "IS_NOT_NULL"
            ]
          },
          "operands": {
            "type": "array",
            "items": {
              "type": "object",
              "properties": {
                "type": {
                  "type": "string",
                  "enum": [
                    "LITERAL"
                  ]
                },
                "value": {
                  "type": "string",
                  "description": "字面量值；数字阈值用数字"
                }
              },
              "additionalProperties": false
            }
          },
          "logic": {
            "type": "string",
            "enum": [
              "AND",
              "OR",
              "NOT"
            ]
          },
          "conditions": {
            "type": "array",
            "items": {
              "type": "object"
            }
          }
        },
        "additionalProperties": false
      }
    },
    "clearPresentationFilters": {
      "type": "boolean",
      "default": false
    },
    "sort": {
      "type": "array",
      "description": "展示排序字段（fieldRef / direction / nulls）",
      "items": {
        "type": "object"
      }
    },
    "clearSort": {
      "type": "boolean",
      "default": false
    },
    "topN": {
      "type": "integer",
      "description": "展示前 N 项；只是展示裁剪，不限制取数"
    },
    "clearTopN": {
      "type": "boolean",
      "default": false
    }
  }
}
```

- **必填参数**：（无）
- **目标 / 引用要求**：每一步各自带 target：target.regionId 指向草稿里已有区块，target.ref 指向本次新增区块（ADD_SECTION 的 params.ref 起名）。
- **返回值**：CapabilityOutcome：ok / message / detail（含 regionId、title、chartKind）；试做失败时返回具体错误（哪一步、哪个目标、哪条约束、为什么，外加字段路径、收到的类型、要求的类型、该填在哪、最小示例）
- **适用节点**：WAITING_USER_CONFIRM、USER_EDITING_TEMPLATE
- **只读**：否
- **副作用**：只改**服务端草稿的隔离副本**；试做通过并由用户接受后，才在正式事务里写入草稿（版本 +1）。
- **主要错误**：
  - 能力失败抛 CapabilityError（带 step / op / target / constraint / reason）
  - 参数不合法 → reason=invalid_params，不执行
  - 目标引用不认识 → reason=unknown_ref / missing_target / unknown_region
- **运行限制**：
  - 每一步都要有明确目标：target.regionId（草稿里已有区块）或 target.ref（本次新增区块的引用名）
  - 提交位置：submit_edit_plan 的 operations 里直接给一项 {"op": "<能力名>", "target": {...}, "params": {...}}
  - 包装要求：{"op": "SET_DATA_BINDING", "target": {"regionId": "<regionId>"}, "params": {}}
  - 最小结构示例（只给格式）：{"op": "SET_DATA_BINDING", "target": {"regionId": "<regionId>"}, "params": {}}
- **源码定位**：
  - registration：`app/services/edit_capabilities/base.py::register`
  - dispatch：`app/tools/submit_edit_plan_tool.py::run`
  - validation：`app/services/edit_capabilities/base.py::validate_params`
  - execution：`app/services/edit_capabilities/binding_component.py::_set_data_binding`

### SET_CHART_KIND

- **入口类型**：`operation` —— 后端执行的操作 —— 不是独立 Tool
- **分组**：清单能力 / 设置统计与展示
- **状态**：`available`
- **用途**：改一个图表区块的图表类型：LINE / AREA / BAR / STACKED_BAR / PIE / DONUT。**只改图型**，分类、系列与数值一律不动；类型逐字保留（STACKED_BAR 不折成 BAR，DONUT 不折成 PIE）。饼图 / 环形图复用当前分类绑定，或当前输出已声明的分类字段与数值字段；已有分组聚合输出、SUM/COUNT 的离散时间桶也可直接切换，保持原标签和逐点数值。无需先调用 SET_CATEGORY_DATA；不得为了切换图型另填分类或数值。未聚合的连续时间序列与多系列不能直接转换。 （**操作清单里的能力，不是独立 Tool**：模型在 submit_edit_plan 的 operations 里给出 op + target + params，由后端在草稿副本上执行。）**提交位置**：submit_edit_plan 的 operations 里直接给一项 {"op": "<能力名>", "target": {...}, "params": {...}}。**包装要求**：{"op": "SET_CHART_KIND", "target": {"regionId": "<regionId>"}, "params": {"chartKind": "AREA"}}。
- **参数**（JSON Schema，取自真实契约）：

```json
{
  "type": "object",
  "description": "SET_CHART_KIND 的参数对象。",
  "properties": {
    "chartKind": {
      "type": "string",
      "description": "图表类型（LINE / AREA / BAR / STACKED_BAR / PIE / DONUT）",
      "enum": [
        "AREA",
        "BAR",
        "DONUT",
        "LINE",
        "PIE",
        "STACKED_BAR"
      ],
      "minLength": 1
    }
  },
  "required": [
    "chartKind"
  ]
}
```

- **必填参数**：chartKind
- **目标 / 引用要求**：每一步各自带 target：target.regionId 指向草稿里已有区块，target.ref 指向本次新增区块（ADD_SECTION 的 params.ref 起名）。
- **返回值**：CapabilityOutcome：ok / message / detail（含 regionId、title、chartKind）；试做失败时返回具体错误（哪一步、哪个目标、哪条约束、为什么，外加字段路径、收到的类型、要求的类型、该填在哪、最小示例）
- **适用节点**：WAITING_USER_CONFIRM、USER_EDITING_TEMPLATE
- **只读**：否
- **副作用**：只改**服务端草稿的隔离副本**；试做通过并由用户接受后，才在正式事务里写入草稿（版本 +1）。
- **主要错误**：
  - 能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=unknown_region
  - 能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=unsupported_chart_kind
  - 能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=incompatible_data_shape
  - 参数不合法 → reason=invalid_params，不执行
  - 目标引用不认识 → reason=unknown_ref / missing_target / unknown_region
- **运行限制**：
  - 每一步都要有明确目标：target.regionId（草稿里已有区块）或 target.ref（本次新增区块的引用名）
  - chartKind 取值域：AREA、BAR、DONUT、LINE、PIE、STACKED_BAR
  - chartKind 至少 1 个字符
  - 提交位置：submit_edit_plan 的 operations 里直接给一项 {"op": "<能力名>", "target": {...}, "params": {...}}
  - 包装要求：{"op": "SET_CHART_KIND", "target": {"regionId": "<regionId>"}, "params": {"chartKind": "AREA"}}
  - 最小结构示例（只给格式）：{"op": "SET_CHART_KIND", "target": {"regionId": "<regionId>"}, "params": {"chartKind": "AREA"}}
- **源码定位**：
  - registration：`app/services/edit_capabilities/base.py::register`
  - dispatch：`app/tools/submit_edit_plan_tool.py::run`
  - validation：`app/services/edit_capabilities/base.py::validate_params`
  - execution：`app/services/edit_capabilities/chart_component.py::_set_chart_kind`

### SET_CHART_ENCODINGS

- **入口类型**：`operation` —— 后端执行的操作 —— 不是独立 Tool
- **分组**：清单能力 / 设置统计与展示
- **状态**：`available`
- **用途**：设置图表的视觉通道绑定（给的是**完整清单**）：每一项形如 {"channel": "X", "fieldRef": "<字段名>", "format": {...}}；channel 取 X / Y / SERIES / COLOR / LABEL，每个通道最多一项。字段必须已登记或属于当前模拟字段目录；饼图 / 环形图没有横纵轴，只接受 COLOR 与 LABEL。 （**操作清单里的能力，不是独立 Tool**：模型在 submit_edit_plan 的 operations 里给出 op + target + params，由后端在草稿副本上执行。）**提交位置**：submit_edit_plan 的 operations 里直接给一项 {"op": "<能力名>", "target": {...}, "params": {...}}。**包装要求**：{"op": "SET_CHART_ENCODINGS", "target": {"regionId": "<regionId>"}, "params": {}}。
- **参数**（JSON Schema，取自真实契约）：

```json
{
  "type": "object",
  "description": "SET_CHART_ENCODINGS 的参数对象。",
  "properties": {
    "encodings": {
      "type": "array",
      "description": "完整的通道绑定清单：每一项形如 {\"channel\": \"X\", \"fieldRef\": \"<字段名>\", \"format\": {...}}；channel 取 X / Y / SERIES / COLOR / LABEL，每个通道最多一项。",
      "items": {
        "type": "object",
        "properties": {
          "channel": {
            "type": "string",
            "description": "视觉通道（X / Y / SERIES / COLOR / LABEL）",
            "enum": [
              "X",
              "Y",
              "SERIES",
              "COLOR",
              "LABEL"
            ]
          },
          "fieldRef": {
            "type": "string",
            "description": "数据字段名（必须已登记或属于模拟字段目录）"
          },
          "format": {
            "type": "object",
            "description": "该通道的显示格式（可选）",
            "additionalProperties": false
          }
        },
        "additionalProperties": false
      }
    }
  }
}
```

- **必填参数**：（无）
- **目标 / 引用要求**：每一步各自带 target：target.regionId 指向草稿里已有区块，target.ref 指向本次新增区块（ADD_SECTION 的 params.ref 起名）。
- **返回值**：CapabilityOutcome：ok / message / detail（含 regionId、title、chartKind）；试做失败时返回具体错误（哪一步、哪个目标、哪条约束、为什么，外加字段路径、收到的类型、要求的类型、该填在哪、最小示例）
- **适用节点**：WAITING_USER_CONFIRM、USER_EDITING_TEMPLATE
- **只读**：否
- **副作用**：只改**服务端草稿的隔离副本**；试做通过并由用户接受后，才在正式事务里写入草稿（版本 +1）。
- **主要错误**：
  - 能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=unknown_region
  - 能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=invalid_encoding
  - 能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=missing_channel
  - 能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=unsupported_channel
  - 能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=duplicate_channel
  - 能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=channel_not_applicable
  - 参数不合法 → reason=invalid_params，不执行
  - 目标引用不认识 → reason=unknown_ref / missing_target / unknown_region
- **运行限制**：
  - 每一步都要有明确目标：target.regionId（草稿里已有区块）或 target.ref（本次新增区块的引用名）
  - 提交位置：submit_edit_plan 的 operations 里直接给一项 {"op": "<能力名>", "target": {...}, "params": {...}}
  - 包装要求：{"op": "SET_CHART_ENCODINGS", "target": {"regionId": "<regionId>"}, "params": {}}
  - 最小结构示例（只给格式）：{"op": "SET_CHART_ENCODINGS", "target": {"regionId": "<regionId>"}, "params": {}}
- **源码定位**：
  - registration：`app/services/edit_capabilities/base.py::register`
  - dispatch：`app/tools/submit_edit_plan_tool.py::run`
  - validation：`app/services/edit_capabilities/base.py::validate_params`
  - execution：`app/services/edit_capabilities/chart_component.py::_set_chart_encodings`

### SET_CHART_AXES

- **入口类型**：`operation` —— 后端执行的操作 —— 不是独立 Tool
- **分组**：清单能力 / 设置统计与展示
- **状态**：`available`
- **用途**：设置图表坐标轴：axes 的键是 'x' 或 'y'，值形如 {"title": "...", "minimum": 0, "maximum": 100, "format": {...}, "scale": "LINEAR"}；给哪个轴就改哪个轴，没给的轴保持原样（clearAxes=true 先清空全部）。scale 取 LINEAR / LOG / TIME / CATEGORY；下限不能高于上限。饼图 / 环形图没有坐标轴，设不了。 （**操作清单里的能力，不是独立 Tool**：模型在 submit_edit_plan 的 operations 里给出 op + target + params，由后端在草稿副本上执行。）**提交位置**：submit_edit_plan 的 operations 里直接给一项 {"op": "<能力名>", "target": {...}, "params": {...}}。**包装要求**：{"op": "SET_CHART_AXES", "target": {"regionId": "<regionId>"}, "params": {}}。
- **参数**（JSON Schema，取自真实契约）：

```json
{
  "type": "object",
  "description": "SET_CHART_AXES 的参数对象。",
  "properties": {
    "axes": {
      "type": "object",
      "description": "要设置的坐标轴：键是 'x' 或 'y'，值形如 {\"title\": \"...\", \"minimum\": 0, \"maximum\": 100, \"format\": {...}, \"scale\": \"LINEAR\"}；给哪个轴就改哪个轴，没给的轴保持原样。",
      "additionalProperties": false
    },
    "clearAxes": {
      "type": "boolean",
      "description": "是否先清空全部坐标轴（true = 清空后再应用 axes 里的内容）",
      "default": false
    }
  }
}
```

- **必填参数**：（无）
- **目标 / 引用要求**：每一步各自带 target：target.regionId 指向草稿里已有区块，target.ref 指向本次新增区块（ADD_SECTION 的 params.ref 起名）。
- **返回值**：CapabilityOutcome：ok / message / detail（含 regionId、title、chartKind）；试做失败时返回具体错误（哪一步、哪个目标、哪条约束、为什么，外加字段路径、收到的类型、要求的类型、该填在哪、最小示例）
- **适用节点**：WAITING_USER_CONFIRM、USER_EDITING_TEMPLATE
- **只读**：否
- **副作用**：只改**服务端草稿的隔离副本**；试做通过并由用户接受后，才在正式事务里写入草稿（版本 +1）。
- **主要错误**：
  - 能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=unknown_region
  - 能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=axes_not_supported
  - 能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=unknown_axis
  - 参数不合法 → reason=invalid_params，不执行
  - 目标引用不认识 → reason=unknown_ref / missing_target / unknown_region
- **运行限制**：
  - 每一步都要有明确目标：target.regionId（草稿里已有区块）或 target.ref（本次新增区块的引用名）
  - 提交位置：submit_edit_plan 的 operations 里直接给一项 {"op": "<能力名>", "target": {...}, "params": {...}}
  - 包装要求：{"op": "SET_CHART_AXES", "target": {"regionId": "<regionId>"}, "params": {}}
  - 最小结构示例（只给格式）：{"op": "SET_CHART_AXES", "target": {"regionId": "<regionId>"}, "params": {}}
- **源码定位**：
  - registration：`app/services/edit_capabilities/base.py::register`
  - dispatch：`app/tools/submit_edit_plan_tool.py::run`
  - validation：`app/services/edit_capabilities/base.py::validate_params`
  - execution：`app/services/edit_capabilities/chart_component.py::_set_chart_axes`

### SET_CHART_LEGEND

- **入口类型**：`operation` —— 后端执行的操作 —— 不是独立 Tool
- **分组**：清单能力 / 设置统计与展示
- **状态**：`available`
- **用途**：设置图例开关与稳定系列配色：legend 是布尔值（不填 = 不改，协议里图例只有开关，没有位置与方向）；seriesColors 是「稳定系列键 → 颜色」的映射（颜色写十六进制、rgb()/hsl()、颜色关键字或主题 token 名，不接受外链）；系列键必须是这个图表真实存在的系列（clearSeriesColors=true 先清空）。 （**操作清单里的能力，不是独立 Tool**：模型在 submit_edit_plan 的 operations 里给出 op + target + params，由后端在草稿副本上执行。）**提交位置**：submit_edit_plan 的 operations 里直接给一项 {"op": "<能力名>", "target": {...}, "params": {...}}。**包装要求**：{"op": "SET_CHART_LEGEND", "target": {"regionId": "<regionId>"}, "params": {}}。
- **参数**（JSON Schema，取自真实契约）：

```json
{
  "type": "object",
  "description": "SET_CHART_LEGEND 的参数对象。",
  "properties": {
    "legend": {
      "type": "boolean",
      "description": "是否显示图例（不填 = 不改；协议里图例只有开关，没有位置 / 方向）"
    },
    "seriesColors": {
      "type": "object",
      "description": "稳定系列键 → 颜色（十六进制 / rgb() / 颜色关键字或主题 token 名）",
      "additionalProperties": false
    },
    "clearSeriesColors": {
      "type": "boolean",
      "description": "是否先清空已有配色（true = 清空后再应用 seriesColors）",
      "default": false
    }
  }
}
```

- **必填参数**：（无）
- **目标 / 引用要求**：每一步各自带 target：target.regionId 指向草稿里已有区块，target.ref 指向本次新增区块（ADD_SECTION 的 params.ref 起名）。
- **返回值**：CapabilityOutcome：ok / message / detail（含 regionId、title、chartKind）；试做失败时返回具体错误（哪一步、哪个目标、哪条约束、为什么，外加字段路径、收到的类型、要求的类型、该填在哪、最小示例）
- **适用节点**：WAITING_USER_CONFIRM、USER_EDITING_TEMPLATE
- **只读**：否
- **副作用**：只改**服务端草稿的隔离副本**；试做通过并由用户接受后，才在正式事务里写入草稿（版本 +1）。
- **主要错误**：
  - 能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=unknown_region
  - 能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=unknown_series
  - 能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=invalid_series_color
  - 参数不合法 → reason=invalid_params，不执行
  - 目标引用不认识 → reason=unknown_ref / missing_target / unknown_region
- **运行限制**：
  - 每一步都要有明确目标：target.regionId（草稿里已有区块）或 target.ref（本次新增区块的引用名）
  - 提交位置：submit_edit_plan 的 operations 里直接给一项 {"op": "<能力名>", "target": {...}, "params": {...}}
  - 包装要求：{"op": "SET_CHART_LEGEND", "target": {"regionId": "<regionId>"}, "params": {}}
  - 最小结构示例（只给格式）：{"op": "SET_CHART_LEGEND", "target": {"regionId": "<regionId>"}, "params": {}}
- **源码定位**：
  - registration：`app/services/edit_capabilities/base.py::register`
  - dispatch：`app/tools/submit_edit_plan_tool.py::run`
  - validation：`app/services/edit_capabilities/base.py::validate_params`
  - execution：`app/services/edit_capabilities/chart_component.py::_set_chart_legend`

### SET_CHART_TOOLTIP

- **入口类型**：`operation` —— 后端执行的操作 —— 不是独立 Tool
- **分组**：清单能力 / 设置统计与展示
- **状态**：`available`
- **用途**：设置提示框里**额外**显示的字段：tooltipFields 给的是完整清单（字段必须已登记或属于当前模拟字段目录；clearTooltipFields=true 清空）。基础提示（分类名与数值）永远保留，不会被关掉。 （**操作清单里的能力，不是独立 Tool**：模型在 submit_edit_plan 的 operations 里给出 op + target + params，由后端在草稿副本上执行。）**提交位置**：submit_edit_plan 的 operations 里直接给一项 {"op": "<能力名>", "target": {...}, "params": {...}}。**包装要求**：{"op": "SET_CHART_TOOLTIP", "target": {"regionId": "<regionId>"}, "params": {}}。
- **参数**（JSON Schema，取自真实契约）：

```json
{
  "type": "object",
  "description": "SET_CHART_TOOLTIP 的参数对象。",
  "properties": {
    "tooltipFields": {
      "type": "array",
      "description": "提示里要额外显示的字段名（非空时按此完整清单替换）",
      "items": {
        "type": "string"
      }
    },
    "clearTooltipFields": {
      "type": "boolean",
      "description": "是否清空提示字段（true = 清空；基础提示不受影响）",
      "default": false
    }
  }
}
```

- **必填参数**：（无）
- **目标 / 引用要求**：每一步各自带 target：target.regionId 指向草稿里已有区块，target.ref 指向本次新增区块（ADD_SECTION 的 params.ref 起名）。
- **返回值**：CapabilityOutcome：ok / message / detail（含 regionId、title、chartKind）；试做失败时返回具体错误（哪一步、哪个目标、哪条约束、为什么，外加字段路径、收到的类型、要求的类型、该填在哪、最小示例）
- **适用节点**：WAITING_USER_CONFIRM、USER_EDITING_TEMPLATE
- **只读**：否
- **副作用**：只改**服务端草稿的隔离副本**；试做通过并由用户接受后，才在正式事务里写入草稿（版本 +1）。
- **主要错误**：
  - 能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=unknown_region
  - 参数不合法 → reason=invalid_params，不执行
  - 目标引用不认识 → reason=unknown_ref / missing_target / unknown_region
- **运行限制**：
  - 每一步都要有明确目标：target.regionId（草稿里已有区块）或 target.ref（本次新增区块的引用名）
  - 提交位置：submit_edit_plan 的 operations 里直接给一项 {"op": "<能力名>", "target": {...}, "params": {...}}
  - 包装要求：{"op": "SET_CHART_TOOLTIP", "target": {"regionId": "<regionId>"}, "params": {}}
  - 最小结构示例（只给格式）：{"op": "SET_CHART_TOOLTIP", "target": {"regionId": "<regionId>"}, "params": {}}
- **源码定位**：
  - registration：`app/services/edit_capabilities/base.py::register`
  - dispatch：`app/tools/submit_edit_plan_tool.py::run`
  - validation：`app/services/edit_capabilities/base.py::validate_params`
  - execution：`app/services/edit_capabilities/chart_component.py::_set_chart_tooltip`

### SET_CHART_REFERENCE_LINES

- **入口类型**：`operation` —— 后端执行的操作 —— 不是独立 Tool
- **分组**：清单能力 / 设置统计与展示
- **状态**：`available`
- **用途**：设置参考线：referenceLines 是「名称 → 表达式」，形如 {"type": "LITERAL", "value": 100}（本批只支持常量数值表达式，数值按图表数值轴的单位理解）；参考线挂在有单一数值轴的图型上，饼图 / 环形图没有轴，设不了；名称不能重复（clearReferenceLines=true 先清空）。 （**操作清单里的能力，不是独立 Tool**：模型在 submit_edit_plan 的 operations 里给出 op + target + params，由后端在草稿副本上执行。）**提交位置**：submit_edit_plan 的 operations 里直接给一项 {"op": "<能力名>", "target": {...}, "params": {...}}。**包装要求**：{"op": "SET_CHART_REFERENCE_LINES", "target": {"regionId": "<regionId>"}, "params": {}}。
- **参数**（JSON Schema，取自真实契约）：

```json
{
  "type": "object",
  "description": "SET_CHART_REFERENCE_LINES 的参数对象。",
  "properties": {
    "referenceLines": {
      "type": "object",
      "description": "参考线：名称 → 表达式，形如 {\"type\": \"LITERAL\", \"value\": 100}；数值按图表数值轴的单位理解。",
      "additionalProperties": false
    },
    "clearReferenceLines": {
      "type": "boolean",
      "description": "是否先清空已有参考线（true = 清空后再应用 referenceLines）",
      "default": false
    }
  }
}
```

- **必填参数**：（无）
- **目标 / 引用要求**：每一步各自带 target：target.regionId 指向草稿里已有区块，target.ref 指向本次新增区块（ADD_SECTION 的 params.ref 起名）。
- **返回值**：CapabilityOutcome：ok / message / detail（含 regionId、title、chartKind）；试做失败时返回具体错误（哪一步、哪个目标、哪条约束、为什么，外加字段路径、收到的类型、要求的类型、该填在哪、最小示例）
- **适用节点**：WAITING_USER_CONFIRM、USER_EDITING_TEMPLATE
- **只读**：否
- **副作用**：只改**服务端草稿的隔离副本**；试做通过并由用户接受后，才在正式事务里写入草稿（版本 +1）。
- **主要错误**：
  - 能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=unknown_region
  - 能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=reference_axis_ambiguous
  - 能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=invalid_reference_line
  - 能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=duplicate_reference_line
  - 参数不合法 → reason=invalid_params，不执行
  - 目标引用不认识 → reason=unknown_ref / missing_target / unknown_region
- **运行限制**：
  - 每一步都要有明确目标：target.regionId（草稿里已有区块）或 target.ref（本次新增区块的引用名）
  - 提交位置：submit_edit_plan 的 operations 里直接给一项 {"op": "<能力名>", "target": {...}, "params": {...}}
  - 包装要求：{"op": "SET_CHART_REFERENCE_LINES", "target": {"regionId": "<regionId>"}, "params": {}}
  - 最小结构示例（只给格式）：{"op": "SET_CHART_REFERENCE_LINES", "target": {"regionId": "<regionId>"}, "params": {}}
- **源码定位**：
  - registration：`app/services/edit_capabilities/base.py::register`
  - dispatch：`app/tools/submit_edit_plan_tool.py::run`
  - validation：`app/services/edit_capabilities/base.py::validate_params`
  - execution：`app/services/edit_capabilities/chart_component.py::_set_chart_reference_lines`

### SET_TABLE_COLUMNS

- **入口类型**：`operation` —— 后端执行的操作 —— 不是独立 Tool
- **分组**：清单能力 / 设置统计与展示
- **状态**：`available`
- **用途**：设置表格的**列清单**（完整清单：给了就是全部，顺序即显示顺序）。每一项形如 {"id": "col_1", "label": "机房名称", "fieldRef": "roomName", "format": {"kind": "TEXT"}, "width": {"value": 120, "unit": "PX"}, "style": {"textAlign": "LEFT"}, "headerPath": ["能耗", "总量"]}；给了 id 就是改这一列（id 必须已存在），不给 id 就是新增一列。标题（label）与取值字段（fieldRef）**互相独立**；清单里没有的列**不再显示**，但源字段、输出与指标一个都不删。**只改其中几列时，清单里其余列的相对顺序要照抄当前表格的顺序**（先按 get_current_region 给的顺序列出，再改你要改的那几项）—— **不要按 id 重排**：清单顺序就是显示顺序，按 id 排会把用户刚放好的列挪走。 （**操作清单里的能力，不是独立 Tool**：模型在 submit_edit_plan 的 operations 里给出 op + target + params，由后端在草稿副本上执行。）**提交位置**：submit_edit_plan 的 operations 里直接给一项 {"op": "<能力名>", "target": {...}, "params": {...}}。**包装要求**：{"op": "SET_TABLE_COLUMNS", "target": {"regionId": "<regionId>"}, "params": {}}。
- **参数**（JSON Schema，取自真实契约）：

```json
{
  "type": "object",
  "description": "SET_TABLE_COLUMNS 的参数对象。",
  "properties": {
    "columns": {
      "type": "array",
      "description": "完整的列清单（顺序即显示顺序）：每一项形如 {\"id\": \"col_1\", \"label\": \"机房名称\", \"fieldRef\": \"roomName\", \"format\": {\"kind\": \"TEXT\"}, \"width\": {\"value\": 120, \"unit\": \"PX\"}, \"style\": {\"textAlign\": \"LEFT\"}, \"headerPath\": [\"能耗\", \"总量\"]}；给了 id 就是改这一列（id 必须已存在），不给 id 就是新增一列。清单里没有的列**不再显示**（源字段、输出与指标一个都不删）。",
      "items": {
        "type": "object",
        "properties": {
          "id": {
            "type": "string",
            "description": "列的稳定 id（改已有列时给，新增列不给）",
            "default": ""
          },
          "label": {
            "type": "string",
            "description": "列标题（与取值字段互相独立）",
            "default": ""
          },
          "fieldRef": {
            "type": "string",
            "description": "取值字段名（必须已登记）",
            "default": ""
          },
          "format": {
            "type": "object",
            "description": "这一列的显示格式（可选；不给则沿用原格式）",
            "additionalProperties": false
          },
          "width": {
            "type": "object",
            "description": "列宽 {\"value\": 120, \"unit\": \"PX\"}（可选）",
            "additionalProperties": false
          },
          "style": {
            "type": "object",
            "description": "这一列的样式（可选；只认受控的外观键）",
            "additionalProperties": false
          },
          "headerPath": {
            "type": "array",
            "description": "多层表头路径（可选；每一层一段表头文字；[] 表示清空）",
            "items": {
              "type": "object"
            }
          }
        },
        "additionalProperties": false
      }
    }
  }
}
```

- **必填参数**：（无）
- **目标 / 引用要求**：每一步各自带 target：target.regionId 指向草稿里已有区块，target.ref 指向本次新增区块（ADD_SECTION 的 params.ref 起名）。
- **返回值**：CapabilityOutcome：ok / message / detail（含 regionId、title、chartKind）；试做失败时返回具体错误（哪一步、哪个目标、哪条约束、为什么，外加字段路径、收到的类型、要求的类型、该填在哪、最小示例）
- **适用节点**：WAITING_USER_CONFIRM、USER_EDITING_TEMPLATE
- **只读**：否
- **副作用**：只改**服务端草稿的隔离副本**；试做通过并由用户接受后，才在正式事务里写入草稿（版本 +1）。
- **主要错误**：
  - 能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=unknown_region
  - 能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=no_changes
  - 能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=empty_column_list
  - 能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=unknown_column_id
  - 能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=missing_column_label
  - 能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=missing_column_field_ref
  - 参数不合法 → reason=invalid_params，不执行
  - 目标引用不认识 → reason=unknown_ref / missing_target / unknown_region
- **运行限制**：
  - 每一步都要有明确目标：target.regionId（草稿里已有区块）或 target.ref（本次新增区块的引用名）
  - 提交位置：submit_edit_plan 的 operations 里直接给一项 {"op": "<能力名>", "target": {...}, "params": {...}}
  - 包装要求：{"op": "SET_TABLE_COLUMNS", "target": {"regionId": "<regionId>"}, "params": {}}
  - 最小结构示例（只给格式）：{"op": "SET_TABLE_COLUMNS", "target": {"regionId": "<regionId>"}, "params": {}}
- **源码定位**：
  - registration：`app/services/edit_capabilities/base.py::register`
  - dispatch：`app/tools/submit_edit_plan_tool.py::run`
  - validation：`app/services/edit_capabilities/base.py::validate_params`
  - execution：`app/services/edit_capabilities/table_component.py::_set_table_columns`

### SET_TABLE_DISPLAY

- **入口类型**：`operation` —— 后端执行的操作 —— 不是独立 Tool
- **分组**：清单能力 / 设置统计与展示
- **状态**：`available`
- **用途**：设置表格的**展示方式**：groupRowsBy（按字段分组显示行 —— 只是排版，**不产生小计或聚合**；清空用 clearGroupRowsBy）、rowNumber（是否显示序号）、pageSize（网页展示每页行数 —— 只是展示分页，**不限制取数与导出**；清空用 clearPageSize）、frozenColumnCount（冻结前几列；清空用 clearFrozenColumnCount）。没给的项保持原样。 （**操作清单里的能力，不是独立 Tool**：模型在 submit_edit_plan 的 operations 里给出 op + target + params，由后端在草稿副本上执行。）**提交位置**：submit_edit_plan 的 operations 里直接给一项 {"op": "<能力名>", "target": {...}, "params": {...}}。**包装要求**：{"op": "SET_TABLE_DISPLAY", "target": {"regionId": "<regionId>"}, "params": {}}。
- **参数**（JSON Schema，取自真实契约）：

```json
{
  "type": "object",
  "description": "SET_TABLE_DISPLAY 的参数对象。",
  "properties": {
    "groupRowsBy": {
      "type": "array",
      "description": "按哪些字段分组显示行（只是展示分组，**不产生小计或聚合**）；清空用 clearGroupRowsBy",
      "items": {
        "type": "string"
      }
    },
    "clearGroupRowsBy": {
      "type": "boolean",
      "description": "是否清空行分组字段",
      "default": false
    },
    "rowNumber": {
      "type": "boolean",
      "description": "是否显示序号列（不填 = 不改）"
    },
    "pageSize": {
      "type": "integer",
      "description": "网页展示每页行数（只是**展示**分页，不限制取数与导出）"
    },
    "clearPageSize": {
      "type": "boolean",
      "description": "是否清掉每页行数（恢复成不分页）",
      "default": false
    },
    "frozenColumnCount": {
      "type": "integer",
      "description": "冻结前几列（不填 = 不改）"
    },
    "clearFrozenColumnCount": {
      "type": "boolean",
      "description": "是否清掉冻结列设置",
      "default": false
    }
  }
}
```

- **必填参数**：（无）
- **目标 / 引用要求**：每一步各自带 target：target.regionId 指向草稿里已有区块，target.ref 指向本次新增区块（ADD_SECTION 的 params.ref 起名）。
- **返回值**：CapabilityOutcome：ok / message / detail（含 regionId、title、chartKind）；试做失败时返回具体错误（哪一步、哪个目标、哪条约束、为什么，外加字段路径、收到的类型、要求的类型、该填在哪、最小示例）
- **适用节点**：WAITING_USER_CONFIRM、USER_EDITING_TEMPLATE
- **只读**：否
- **副作用**：只改**服务端草稿的隔离副本**；试做通过并由用户接受后，才在正式事务里写入草稿（版本 +1）。
- **主要错误**：
  - 能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=unknown_region
  - 能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=no_changes
  - 能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=invalid_page_size
  - 能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=invalid_frozen_column_count
  - 参数不合法 → reason=invalid_params，不执行
  - 目标引用不认识 → reason=unknown_ref / missing_target / unknown_region
- **运行限制**：
  - 每一步都要有明确目标：target.regionId（草稿里已有区块）或 target.ref（本次新增区块的引用名）
  - 提交位置：submit_edit_plan 的 operations 里直接给一项 {"op": "<能力名>", "target": {...}, "params": {...}}
  - 包装要求：{"op": "SET_TABLE_DISPLAY", "target": {"regionId": "<regionId>"}, "params": {}}
  - 最小结构示例（只给格式）：{"op": "SET_TABLE_DISPLAY", "target": {"regionId": "<regionId>"}, "params": {}}
- **源码定位**：
  - registration：`app/services/edit_capabilities/base.py::register`
  - dispatch：`app/tools/submit_edit_plan_tool.py::run`
  - validation：`app/services/edit_capabilities/base.py::validate_params`
  - execution：`app/services/edit_capabilities/table_component.py::_set_table_display`

### SET_TABLE_SUMMARIES

- **入口类型**：`operation` —— 后端执行的操作 —— 不是独立 Tool
- **分组**：清单能力 / 设置统计与展示
- **状态**：`available`
- **用途**：设置表格的**汇总引用**（完整清单：给了就是全部；清空用 clearSummaries）。每一项形如 {"label": "告警总数", "outputRef": "output:alarms", "fieldRef": "totalAlarmCount", "basis": "FULL_RESULT"}；outputRef 必须是这个区域**已登记**的计算输出（不会凭空生成汇总），basis 只能是 FULL_RESULT（全量口径）或 VISIBLE_ROWS（可见行口径）。 （**操作清单里的能力，不是独立 Tool**：模型在 submit_edit_plan 的 operations 里给出 op + target + params，由后端在草稿副本上执行。）**提交位置**：submit_edit_plan 的 operations 里直接给一项 {"op": "<能力名>", "target": {...}, "params": {...}}。**包装要求**：{"op": "SET_TABLE_SUMMARIES", "target": {"regionId": "<regionId>"}, "params": {}}。
- **参数**（JSON Schema，取自真实契约）：

```json
{
  "type": "object",
  "description": "SET_TABLE_SUMMARIES 的参数对象。",
  "properties": {
    "summaries": {
      "type": "array",
      "description": "完整的汇总清单：每一项形如 {\"label\": \"告警总数\", \"outputRef\": \"output:alarms\", \"fieldRef\": \"totalAlarmCount\", \"basis\": \"FULL_RESULT\"}；outputRef 必须是这个区域**已登记**的计算输出，basis 只能是 FULL_RESULT（全量口径）或 VISIBLE_ROWS（可见行口径）。",
      "items": {
        "type": "object",
        "properties": {
          "label": {
            "type": "string",
            "description": "汇总标签",
            "minLength": 1
          },
          "outputRef": {
            "type": "string",
            "description": "已登记的计算输出（output:<name>）",
            "default": ""
          },
          "fieldRef": {
            "type": "string",
            "description": "汇总字段名",
            "default": ""
          },
          "basis": {
            "type": "string",
            "description": "汇总口径（FULL_RESULT 全量口径 / VISIBLE_ROWS 可见行口径）",
            "default": ""
          }
        },
        "additionalProperties": false
      }
    },
    "clearSummaries": {
      "type": "boolean",
      "description": "是否清空全部汇总引用",
      "default": false
    }
  }
}
```

- **必填参数**：（无）
- **目标 / 引用要求**：每一步各自带 target：target.regionId 指向草稿里已有区块，target.ref 指向本次新增区块（ADD_SECTION 的 params.ref 起名）。
- **返回值**：CapabilityOutcome：ok / message / detail（含 regionId、title、chartKind）；试做失败时返回具体错误（哪一步、哪个目标、哪条约束、为什么，外加字段路径、收到的类型、要求的类型、该填在哪、最小示例）
- **适用节点**：WAITING_USER_CONFIRM、USER_EDITING_TEMPLATE
- **只读**：否
- **副作用**：只改**服务端草稿的隔离副本**；试做通过并由用户接受后，才在正式事务里写入草稿（版本 +1）。
- **主要错误**：
  - 能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=unknown_region
  - 能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=no_changes
  - 能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=unknown_output_ref
  - 参数不合法 → reason=invalid_params，不执行
  - 目标引用不认识 → reason=unknown_ref / missing_target / unknown_region
- **运行限制**：
  - 每一步都要有明确目标：target.regionId（草稿里已有区块）或 target.ref（本次新增区块的引用名）
  - 提交位置：submit_edit_plan 的 operations 里直接给一项 {"op": "<能力名>", "target": {...}, "params": {...}}
  - 包装要求：{"op": "SET_TABLE_SUMMARIES", "target": {"regionId": "<regionId>"}, "params": {}}
  - 最小结构示例（只给格式）：{"op": "SET_TABLE_SUMMARIES", "target": {"regionId": "<regionId>"}, "params": {}}
- **源码定位**：
  - registration：`app/services/edit_capabilities/base.py::register`
  - dispatch：`app/tools/submit_edit_plan_tool.py::run`
  - validation：`app/services/edit_capabilities/base.py::validate_params`
  - execution：`app/services/edit_capabilities/table_component.py::_set_table_summaries`

### SET_TEXT_CONTENT

- **入口类型**：`operation` —— 后端执行的操作 —— 不是独立 Tool
- **分组**：清单能力 / 设置统计与展示
- **状态**：`available`
- **用途**：设置文本区块的正文：template（正文文本）、format（PLAIN / MARKDOWN）、headingLevel（目录层级，不是字号）、bindings（占位符 → 表达式，形如 {"type": "FIELD", "fieldRef": "<字段名>"}）。**卡片标题与正文互不相干** —— 这里只改正文。模板里没有绑定的占位符不会报错，但会列在结果里；{{roomName}} 与 {{logicalDate}} 仍按固定替换生效。 （**操作清单里的能力，不是独立 Tool**：模型在 submit_edit_plan 的 operations 里给出 op + target + params，由后端在草稿副本上执行。）**提交位置**：submit_edit_plan 的 operations 里直接给一项 {"op": "<能力名>", "target": {...}, "params": {...}}。**包装要求**：{"op": "SET_TEXT_CONTENT", "target": {"regionId": "<regionId>"}, "params": {}}。
- **参数**（JSON Schema，取自真实契约）：

```json
{
  "type": "object",
  "description": "SET_TEXT_CONTENT 的参数对象。",
  "properties": {
    "template": {
      "type": "string",
      "description": "正文文本（空 = 不改）；里面的 {{占位符}} 按 bindings 取值",
      "default": ""
    },
    "clearTemplate": {
      "type": "boolean",
      "description": "是否清空正文（true = 清空后再应用 template）",
      "default": false
    },
    "format": {
      "type": "string",
      "description": "文本格式：PLAIN（纯文本，保留换行）或 MARKDOWN（受控 Markdown）",
      "default": ""
    },
    "headingLevel": {
      "type": "integer",
      "description": "目录层级（0 起的整数；不填 = 不改）—— 不是字号"
    },
    "clearHeadingLevel": {
      "type": "boolean",
      "description": "是否清空目录层级",
      "default": false
    },
    "bindings": {
      "type": "object",
      "description": "占位符 → 表达式：键是占位符名，值形如 {\"type\": \"FIELD\", \"fieldRef\": \"<字段名>\"}；给的项按名字合并进去，没给的保持原样。",
      "additionalProperties": false
    },
    "clearBindings": {
      "type": "boolean",
      "description": "是否先清空全部占位符绑定（true = 清空后再应用 bindings）",
      "default": false
    }
  }
}
```

- **必填参数**：（无）
- **目标 / 引用要求**：每一步各自带 target：target.regionId 指向草稿里已有区块，target.ref 指向本次新增区块（ADD_SECTION 的 params.ref 起名）。
- **返回值**：CapabilityOutcome：ok / message / detail（含 regionId、title、chartKind）；试做失败时返回具体错误（哪一步、哪个目标、哪条约束、为什么，外加字段路径、收到的类型、要求的类型、该填在哪、最小示例）
- **适用节点**：WAITING_USER_CONFIRM、USER_EDITING_TEMPLATE
- **只读**：否
- **副作用**：只改**服务端草稿的隔离副本**；试做通过并由用户接受后，才在正式事务里写入草稿（版本 +1）。
- **主要错误**：
  - 能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=unsupported_text_format
  - 能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=invalid_heading_level
  - 参数不合法 → reason=invalid_params，不执行
  - 目标引用不认识 → reason=unknown_ref / missing_target / unknown_region
- **运行限制**：
  - 每一步都要有明确目标：target.regionId（草稿里已有区块）或 target.ref（本次新增区块的引用名）
  - 提交位置：submit_edit_plan 的 operations 里直接给一项 {"op": "<能力名>", "target": {...}, "params": {...}}
  - 包装要求：{"op": "SET_TEXT_CONTENT", "target": {"regionId": "<regionId>"}, "params": {}}
  - 最小结构示例（只给格式）：{"op": "SET_TEXT_CONTENT", "target": {"regionId": "<regionId>"}, "params": {}}
- **源码定位**：
  - registration：`app/services/edit_capabilities/base.py::register`
  - dispatch：`app/tools/submit_edit_plan_tool.py::run`
  - validation：`app/services/edit_capabilities/base.py::validate_params`
  - execution：`app/services/edit_capabilities/text_metric.py::_set_text_content`

### SET_METRIC_ITEM

- **入口类型**：`operation` —— 后端执行的操作 —— 不是独立 Tool
- **分组**：清单能力 / 设置统计与展示
- **状态**：`available`
- **用途**：设置单指标卡：label（指标名称）、fieldRef（数值字段）、outputRef（覆盖区域默认输出，必须是本区域已登记的 output:<name>）、format（指标级显示格式）、comparisonFieldRef（对比字段）、iconRef（注册图标，只认注册清单里的键）。**改名称不碰数值、字段绑定、单位与格式。**对比字段与指标字段的单位必须一致，单位标签不许悄悄改。**指标卡 / 指标组的数字显示格式（精度、千分位、单位标签、空值文本）一律用本能力**：它写在指标项自己的 format 上；区域级的 SET_FORMAT 是整块卡片的格式，不是指标项的格式 —— 用它改指标卡会连带改掉这张卡上别的指标的单位。 （**操作清单里的能力，不是独立 Tool**：模型在 submit_edit_plan 的 operations 里给出 op + target + params，由后端在草稿副本上执行。）**提交位置**：submit_edit_plan 的 operations 里直接给一项 {"op": "<能力名>", "target": {...}, "params": {...}}。**包装要求**：{"op": "SET_METRIC_ITEM", "target": {"regionId": "<regionId>"}, "params": {}}。
- **参数**（JSON Schema，取自真实契约）：

```json
{
  "type": "object",
  "description": "SET_METRIC_ITEM 的参数对象。",
  "properties": {
    "label": {
      "type": "string",
      "description": "指标名称（空 = 不改）；改名字不碰数值、字段、单位与格式",
      "default": ""
    },
    "fieldRef": {
      "type": "string",
      "description": "数值字段名（必须已登记或属于当前模拟字段目录；空 = 不改）",
      "default": ""
    },
    "outputRef": {
      "type": "string",
      "description": "覆盖区域默认输出的引用（output:<name>，name 必须是本区域已登记的计算输出）",
      "default": ""
    },
    "clearOutputRef": {
      "type": "boolean",
      "description": "是否清空输出覆盖（回到区域默认输出）",
      "default": false
    },
    "format": {
      "type": "object",
      "description": "指标的显示格式（空 = 不改）：kind / precision / unitLabel / thousandSeparator / dateTimePattern / nullText / roundingMode",
      "additionalProperties": false
    },
    "clearFormat": {
      "type": "boolean",
      "description": "是否清空指标格式",
      "default": false
    },
    "comparisonFieldRef": {
      "type": "string",
      "description": "对比字段名（必须已登记；单位要与指标字段一致）",
      "default": ""
    },
    "clearComparisonFieldRef": {
      "type": "boolean",
      "description": "是否清空对比字段",
      "default": false
    },
    "iconRef": {
      "type": "string",
      "description": "注册图标引用（只认注册清单里的键；不接受图片 URL）",
      "default": ""
    },
    "clearIconRef": {
      "type": "boolean",
      "description": "是否清空图标",
      "default": false
    }
  }
}
```

- **必填参数**：（无）
- **目标 / 引用要求**：每一步各自带 target：target.regionId 指向草稿里已有区块，target.ref 指向本次新增区块（ADD_SECTION 的 params.ref 起名）。
- **返回值**：CapabilityOutcome：ok / message / detail（含 regionId、title、chartKind）；试做失败时返回具体错误（哪一步、哪个目标、哪条约束、为什么，外加字段路径、收到的类型、要求的类型、该填在哪、最小示例）
- **适用节点**：WAITING_USER_CONFIRM、USER_EDITING_TEMPLATE
- **只读**：否
- **副作用**：只改**服务端草稿的隔离副本**；试做通过并由用户接受后，才在正式事务里写入草稿（版本 +1）。
- **主要错误**：
  - 能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=incompatible_unit
  - 参数不合法 → reason=invalid_params，不执行
  - 目标引用不认识 → reason=unknown_ref / missing_target / unknown_region
- **运行限制**：
  - 每一步都要有明确目标：target.regionId（草稿里已有区块）或 target.ref（本次新增区块的引用名）
  - 提交位置：submit_edit_plan 的 operations 里直接给一项 {"op": "<能力名>", "target": {...}, "params": {...}}
  - 包装要求：{"op": "SET_METRIC_ITEM", "target": {"regionId": "<regionId>"}, "params": {}}
  - 最小结构示例（只给格式）：{"op": "SET_METRIC_ITEM", "target": {"regionId": "<regionId>"}, "params": {}}
- **源码定位**：
  - registration：`app/services/edit_capabilities/base.py::register`
  - dispatch：`app/tools/submit_edit_plan_tool.py::run`
  - validation：`app/services/edit_capabilities/base.py::validate_params`
  - execution：`app/services/edit_capabilities/text_metric.py::_set_metric_item`

### SET_METRIC_GROUP

- **入口类型**：`operation` —— 后端执行的操作 —— 不是独立 Tool
- **分组**：清单能力 / 设置统计与展示
- **状态**：`available`
- **用途**：设置多指标组：metrics 是该组的**完整**指标项清单（每项形如 {'id': 'm_1', 'label': '总耗电量', 'fieldRef': 'totalEnergyKwh', 'format': {'kind': 'NUMBER'}}）；带 id 的项按 id 就地更新，不带 id 的是新项，清单里没有的项从展示清单里移除（**不删任何源数据 / 输出 / 列 / 区块**）。**不传 metrics = 只改排版，组里已有的指标项原样保留** —— 只改列数 / 间距这类要求不要顺手把指标项清成 0 项。arrangement 设组内排版（mode / columns / direction / gap）。create=true 可把指标卡（或正文为空的文本区块）改成多指标组。 （**操作清单里的能力，不是独立 Tool**：模型在 submit_edit_plan 的 operations 里给出 op + target + params，由后端在草稿副本上执行。）**提交位置**：submit_edit_plan 的 operations 里直接给一项 {"op": "<能力名>", "target": {...}, "params": {...}}。**包装要求**：{"op": "SET_METRIC_GROUP", "target": {"regionId": "<regionId>"}, "params": {}}。
- **参数**（JSON Schema，取自真实契约）：

```json
{
  "type": "object",
  "description": "SET_METRIC_GROUP 的参数对象。",
  "properties": {
    "metrics": {
      "type": "array",
      "description": "该指标组的**完整**指标项清单：每一项形如 {\"id\": \"m_1\", \"label\": \"总耗电量\", \"fieldRef\": \"totalEnergyKwh\", \"format\": {\"kind\": \"NUMBER\", \"precision\": 1}}；带 id 的项按 id 就地更新（id 必须已存在），不带 id 的是新项；清单里没有的项从展示清单里移除（**不删任何源数据 / 输出 / 列 / 区块**）。**不传这一项 = 只改组内排版，组里已有的指标项原样保留**（缺省不等于「清空」——清空会把用户没提的指标项悄悄抹掉）。",
      "items": {
        "type": "object"
      }
    },
    "arrangement": {
      "type": "object",
      "description": "组内排版：{'mode': 'GRID'|'FLOW'|'ABSOLUTE', 'columns': 1..12, 'direction': 'VERTICAL'|'HORIZONTAL', 'gap': {'value': 0..96, 'unit': 'PX'|'PERCENT'}}",
      "additionalProperties": false
    },
    "create": {
      "type": "boolean",
      "description": "是否把目标区块改成多指标组（指标卡 → 指标组；正文为空的文本区块 → 指标组）",
      "default": false
    }
  }
}
```

- **必填参数**：（无）
- **目标 / 引用要求**：每一步各自带 target：target.regionId 指向草稿里已有区块，target.ref 指向本次新增区块（ADD_SECTION 的 params.ref 起名）。
- **返回值**：CapabilityOutcome：ok / message / detail（含 regionId、title、chartKind）；试做失败时返回具体错误（哪一步、哪个目标、哪条约束、为什么，外加字段路径、收到的类型、要求的类型、该填在哪、最小示例）
- **适用节点**：WAITING_USER_CONFIRM、USER_EDITING_TEMPLATE
- **只读**：否
- **副作用**：只改**服务端草稿的隔离副本**；试做通过并由用户接受后，才在正式事务里写入草稿（版本 +1）。
- **主要错误**：
  - 能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=incompatible_component_type
  - 能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=invalid_metric_item
  - 能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=unknown_metric_id
  - 能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=incompatible_unit
  - 参数不合法 → reason=invalid_params，不执行
  - 目标引用不认识 → reason=unknown_ref / missing_target / unknown_region
- **运行限制**：
  - 每一步都要有明确目标：target.regionId（草稿里已有区块）或 target.ref（本次新增区块的引用名）
  - 提交位置：submit_edit_plan 的 operations 里直接给一项 {"op": "<能力名>", "target": {...}, "params": {...}}
  - 包装要求：{"op": "SET_METRIC_GROUP", "target": {"regionId": "<regionId>"}, "params": {}}
  - 最小结构示例（只给格式）：{"op": "SET_METRIC_GROUP", "target": {"regionId": "<regionId>"}, "params": {}}
- **源码定位**：
  - registration：`app/services/edit_capabilities/base.py::register`
  - dispatch：`app/tools/submit_edit_plan_tool.py::run`
  - validation：`app/services/edit_capabilities/base.py::validate_params`
  - execution：`app/services/edit_capabilities/text_metric.py::_set_metric_group`

## 三、旧编辑意图（旧格式入口，由 TemplateEditEngine.apply 执行）

### RENAME_REPORT

- **入口类型**：`legacy_intent` —— 旧格式编辑意图 —— 不是独立 Tool
- **分组**：旧意图 / 报表与区块增删改名
- **状态**：`available`
- **用途**：改报表名称（不动任何区块）。 （**旧格式入口，不是独立 Tool**：模型在 submit_edit_plan 的 intent / target / operation 里给出，由 template_edit_engine.TemplateEditEngine.apply 执行。）
- **参数**（JSON Schema，取自真实契约）：

```json
{
  "$defs": {
    "EditMetricAddition": {
      "description": "要**追加**到某个已有区块里的一个指标。\n\n「追加」与「替换」是两种不同的意图：追加**保留**该区块原有指标，\n把新指标加进去；替换才把原来的换掉。\n\n每个待追加指标至少能表达：用户原话意图、显示名、单位、展示模式。",
      "properties": {
        "intent": {
          "default": "",
          "title": "Intent",
          "type": "string"
        },
        "label": {
          "default": "",
          "title": "Label",
          "type": "string"
        },
        "unit": {
          "default": "",
          "title": "Unit",
          "type": "string"
        },
        "displayMode": {
          "default": "simulated",
          "title": "Displaymode",
          "type": "string"
        },
        "metricRef": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Metricref"
        },
        "metricCandidate": {
          "anyOf": [
            {
              "additionalProperties": true,
              "type": "object"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Metriccandidate"
        },
        "note": {
          "default": "",
          "title": "Note",
          "type": "string"
        }
      },
      "title": "EditMetricAddition",
      "type": "object"
    },
    "EditMetricDeletion": {
      "description": "要**删除**的一个指标 —— 卡片内的**一个指标实例**。\n\n「删掉某个指标」与「删掉整张卡片」是两件事（契约表 §4.3）：目标必须\n指向卡片内**真实存在**的那一项指标。\n\n身份用**稳定标识**记（``metricKey`` / ``simulatedId`` / ``fieldRef``），\n**不记数组下标** —— 删掉中间一项之后下标会漂移；``label`` 只是给人看\n的显示名，**不作为定位依据**（同名指标要澄清，不能删第一条匹配的）。",
      "properties": {
        "metricKey": {
          "default": "",
          "title": "Metrickey",
          "type": "string"
        },
        "simulatedId": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Simulatedid"
        },
        "fieldRef": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Fieldref"
        },
        "label": {
          "default": "",
          "title": "Label",
          "type": "string"
        },
        "isPrimary": {
          "default": false,
          "title": "Isprimary",
          "type": "boolean"
        },
        "intent": {
          "default": "",
          "title": "Intent",
          "type": "string"
        }
      },
      "title": "EditMetricDeletion",
      "type": "object"
    }
  },
  "description": "具体改成什么。字段按 intent 不同用到的子集不同；这里统一松结构承载。",
  "properties": {
    "newTitle": {
      "anyOf": [
        {
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "default": null,
      "title": "Newtitle"
    },
    "componentType": {
      "anyOf": [
        {
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "default": null,
      "title": "Componenttype"
    },
    "chartKind": {
      "anyOf": [
        {
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "default": null,
      "title": "Chartkind"
    },
    "sectionTitle": {
      "anyOf": [
        {
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "default": null,
      "title": "Sectiontitle"
    },
    "insertAfterRegionId": {
      "anyOf": [
        {
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "default": null,
      "title": "Insertafterregionid"
    },
    "sourceRef": {
      "anyOf": [
        {
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "default": null,
      "title": "Sourceref"
    },
    "sourceVersion": {
      "anyOf": [
        {
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "default": null,
      "title": "Sourceversion"
    },
    "fieldMappingRequired": {
      "anyOf": [
        {
          "type": "boolean"
        },
        {
          "type": "null"
        }
      ],
      "default": null,
      "title": "Fieldmappingrequired"
    },
    "metricRef": {
      "anyOf": [
        {
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "default": null,
      "title": "Metricref"
    },
    "metricVersion": {
      "anyOf": [
        {
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "default": null,
      "title": "Metricversion"
    },
    "metricIntent": {
      "anyOf": [
        {
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "default": null,
      "title": "Metricintent"
    },
    "metricCandidate": {
      "anyOf": [
        {
          "additionalProperties": true,
          "type": "object"
        },
        {
          "type": "null"
        }
      ],
      "default": null,
      "title": "Metriccandidate"
    },
    "baseDefinitionRevision": {
      "anyOf": [
        {
          "type": "integer"
        },
        {
          "type": "null"
        }
      ],
      "default": null,
      "title": "Basedefinitionrevision"
    },
    "addMetrics": {
      "items": {
        "$ref": "#/$defs/EditMetricAddition"
      },
      "title": "Addmetrics",
      "type": "array"
    },
    "deleteMetrics": {
      "items": {
        "$ref": "#/$defs/EditMetricDeletion"
      },
      "title": "Deletemetrics",
      "type": "array"
    },
    "componentSelection": {
      "anyOf": [
        {
          "additionalProperties": true,
          "type": "object"
        },
        {
          "type": "null"
        }
      ],
      "default": null,
      "title": "Componentselection"
    },
    "timeWindow": {
      "anyOf": [
        {
          "additionalProperties": true,
          "type": "object"
        },
        {
          "type": "null"
        }
      ],
      "default": null,
      "title": "Timewindow"
    },
    "timeWindowLabel": {
      "anyOf": [
        {
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "default": null,
      "title": "Timewindowlabel"
    },
    "grain": {
      "anyOf": [
        {
          "additionalProperties": true,
          "type": "object"
        },
        {
          "type": "null"
        }
      ],
      "default": null,
      "title": "Grain"
    },
    "supported": {
      "anyOf": [
        {
          "type": "boolean"
        },
        {
          "type": "null"
        }
      ],
      "default": null,
      "title": "Supported"
    },
    "supportedGrains": {
      "items": {
        "type": "string"
      },
      "title": "Supportedgrains",
      "type": "array"
    },
    "topN": {
      "anyOf": [
        {
          "type": "integer"
        },
        {
          "type": "null"
        }
      ],
      "default": null,
      "title": "Topn"
    },
    "sortField": {
      "anyOf": [
        {
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "default": null,
      "title": "Sortfield"
    },
    "sortDirection": {
      "anyOf": [
        {
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "default": null,
      "title": "Sortdirection"
    },
    "dataShape": {
      "anyOf": [
        {
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "default": null,
      "title": "Datashape"
    },
    "displayOptions": {
      "items": {
        "additionalProperties": true,
        "type": "object"
      },
      "title": "Displayoptions",
      "type": "array"
    },
    "disabledOptions": {
      "items": {
        "additionalProperties": true,
        "type": "object"
      },
      "title": "Disabledoptions",
      "type": "array"
    },
    "markdownPreview": {
      "anyOf": [
        {
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "default": null,
      "title": "Markdownpreview"
    },
    "reason": {
      "anyOf": [
        {
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "default": null,
      "title": "Reason"
    },
    "presentationOperations": {
      "items": {
        "additionalProperties": true,
        "type": "object"
      },
      "title": "Presentationoperations",
      "type": "array"
    },
    "themeOperations": {
      "items": {
        "additionalProperties": true,
        "type": "object"
      },
      "title": "Themeoperations",
      "type": "array"
    }
  },
  "title": "EditOperation",
  "type": "object"
}
```

- **必填参数**：（无）
- **目标 / 引用要求**：target.regionId 或 target.matchedText 指明要改哪张卡片；组合修改用 operations 列表（旧格式每步共用顶层 target）
- **返回值**：EditResult：draftAfterEdit / changeSet / reply / markdownPreview / needsClarification；形成待确认候选，用户接受后才正式写入
- **适用节点**：WAITING_USER_CONFIRM、USER_EDITING_TEMPLATE
- **只读**：否
- **副作用**：在草稿副本上执行；通过后形成待确认候选，用户接受时才在正式事务里写入草稿。
- **主要错误**：
  - 目标不存在或不可唯一确定 → 返回 needsClarification + clarificationQuestion（草稿不变）
  - 候选基于旧版本草稿 → 拒绝（stale_edit_plan）
  - 依据的生效组合已变 → 拒绝（combination_conflict）
  - 数据源 / 指标不在当前授权范围 → 拒绝（incompatible_fields）
- **运行限制**：
  - 单步修改只针对一个区块；要一次改多处请用能力格式 operations
- **源码定位**：
  - registration：`app/schemas/edit_plan.py::EditIntent`
  - dispatch：`app/services/agent_service.py::AgentService._try_handle_template_edit`
  - validation：`app/schemas/edit_plan.py::EditPlan`
  - execution：`app/services/template_edit_engine.py::TemplateEditEngine._apply_rename_report`

### DELETE_SECTION（旧意图 / 报表与区块增删改名）

- **入口类型**：`legacy_intent` —— 旧格式编辑意图 —— 不是独立 Tool
- **分组**：旧意图 / 报表与区块增删改名
- **状态**：`available`
- **用途**：删掉一整张卡片（区块）。 （**旧格式入口，不是独立 Tool**：模型在 submit_edit_plan 的 intent / target / operation 里给出，由 template_edit_engine.TemplateEditEngine.apply 执行。）
- **参数**：与上文「RENAME_REPORT」的参数契约完全相同（完整内容见文件末尾机器可读元数据的 `entries` 字段）
- **必填参数**：（无）
- **目标 / 引用要求**：target.regionId 或 target.matchedText 指明要改哪张卡片；组合修改用 operations 列表（旧格式每步共用顶层 target）
- **返回值**：EditResult：draftAfterEdit / changeSet / reply / markdownPreview / needsClarification；形成待确认候选，用户接受后才正式写入
- **适用节点**：WAITING_USER_CONFIRM、USER_EDITING_TEMPLATE
- **只读**：否
- **副作用**：在草稿副本上执行；通过后形成待确认候选，用户接受时才在正式事务里写入草稿。
- **主要错误**：
  - 目标不存在或不可唯一确定 → 返回 needsClarification + clarificationQuestion（草稿不变）
  - 候选基于旧版本草稿 → 拒绝（stale_edit_plan）
  - 依据的生效组合已变 → 拒绝（combination_conflict）
  - 数据源 / 指标不在当前授权范围 → 拒绝（incompatible_fields）
- **运行限制**：
  - 单步修改只针对一个区块；要一次改多处请用能力格式 operations
- **源码定位**：
  - registration：`app/schemas/edit_plan.py::EditIntent`
  - dispatch：`app/services/agent_service.py::AgentService._try_handle_template_edit`
  - validation：`app/schemas/edit_plan.py::EditPlan`
  - execution：`app/services/template_edit_engine.py::TemplateEditEngine._apply_delete`

### DELETE_METRIC

- **入口类型**：`legacy_intent` —— 旧格式编辑意图 —— 不是独立 Tool
- **分组**：旧意图 / 指标增删替换
- **状态**：`available`
- **用途**：在已有卡片里删掉某一项指标（卡片本身保留）。 （**旧格式入口，不是独立 Tool**：模型在 submit_edit_plan 的 intent / target / operation 里给出，由 template_edit_engine.TemplateEditEngine.apply 执行。）
- **参数**：与上文「RENAME_REPORT」的参数契约完全相同（完整内容见文件末尾机器可读元数据的 `entries` 字段）
- **必填参数**：（无）
- **目标 / 引用要求**：target.regionId 或 target.matchedText 指明要改哪张卡片；组合修改用 operations 列表（旧格式每步共用顶层 target）
- **返回值**：EditResult：draftAfterEdit / changeSet / reply / markdownPreview / needsClarification；形成待确认候选，用户接受后才正式写入
- **适用节点**：WAITING_USER_CONFIRM、USER_EDITING_TEMPLATE
- **只读**：否
- **副作用**：在草稿副本上执行；通过后形成待确认候选，用户接受时才在正式事务里写入草稿。
- **主要错误**：
  - 目标不存在或不可唯一确定 → 返回 needsClarification + clarificationQuestion（草稿不变）
  - 候选基于旧版本草稿 → 拒绝（stale_edit_plan）
  - 依据的生效组合已变 → 拒绝（combination_conflict）
  - 数据源 / 指标不在当前授权范围 → 拒绝（incompatible_fields）
- **运行限制**：
  - 只删卡片内的指标项，卡片本身保留；目标不唯一要追问
  - 单步修改只针对一个区块；要一次改多处请用能力格式 operations
- **源码定位**：
  - registration：`app/schemas/edit_plan.py::EditIntent`
  - dispatch：`app/services/agent_service.py::AgentService._try_handle_template_edit`
  - validation：`app/schemas/edit_plan.py::EditPlan`
  - execution：`app/services/template_edit_engine.py::TemplateEditEngine._apply_delete_metric`

### RENAME_SECTION

- **入口类型**：`legacy_intent` —— 旧格式编辑意图 —— 不是独立 Tool
- **分组**：旧意图 / 报表与区块增删改名
- **状态**：`available`
- **用途**：改某张卡片的标题。 （**旧格式入口，不是独立 Tool**：模型在 submit_edit_plan 的 intent / target / operation 里给出，由 template_edit_engine.TemplateEditEngine.apply 执行。）
- **参数**：与上文「RENAME_REPORT」的参数契约完全相同（完整内容见文件末尾机器可读元数据的 `entries` 字段）
- **必填参数**：（无）
- **目标 / 引用要求**：target.regionId 或 target.matchedText 指明要改哪张卡片；组合修改用 operations 列表（旧格式每步共用顶层 target）
- **返回值**：EditResult：draftAfterEdit / changeSet / reply / markdownPreview / needsClarification；形成待确认候选，用户接受后才正式写入
- **适用节点**：WAITING_USER_CONFIRM、USER_EDITING_TEMPLATE
- **只读**：否
- **副作用**：在草稿副本上执行；通过后形成待确认候选，用户接受时才在正式事务里写入草稿。
- **主要错误**：
  - 目标不存在或不可唯一确定 → 返回 needsClarification + clarificationQuestion（草稿不变）
  - 候选基于旧版本草稿 → 拒绝（stale_edit_plan）
  - 依据的生效组合已变 → 拒绝（combination_conflict）
  - 数据源 / 指标不在当前授权范围 → 拒绝（incompatible_fields）
- **运行限制**：
  - 单步修改只针对一个区块；要一次改多处请用能力格式 operations
- **源码定位**：
  - registration：`app/schemas/edit_plan.py::EditIntent`
  - dispatch：`app/services/agent_service.py::AgentService._try_handle_template_edit`
  - validation：`app/schemas/edit_plan.py::EditPlan`
  - execution：`app/services/template_edit_engine.py::TemplateEditEngine._apply_rename`

### ADD_SECTION（旧意图 / 报表与区块增删改名）

- **入口类型**：`legacy_intent` —— 旧格式编辑意图 —— 不是独立 Tool
- **分组**：旧意图 / 报表与区块增删改名
- **状态**：`available`
- **用途**：新增一张卡片（旧格式入口；能力格式请用 ADD_SECTION 能力）。 （**旧格式入口，不是独立 Tool**：模型在 submit_edit_plan 的 intent / target / operation 里给出，由 template_edit_engine.TemplateEditEngine.apply 执行。）
- **参数**：与上文「RENAME_REPORT」的参数契约完全相同（完整内容见文件末尾机器可读元数据的 `entries` 字段）
- **必填参数**：（无）
- **目标 / 引用要求**：target.regionId 或 target.matchedText 指明要改哪张卡片；组合修改用 operations 列表（旧格式每步共用顶层 target）
- **返回值**：EditResult：draftAfterEdit / changeSet / reply / markdownPreview / needsClarification；形成待确认候选，用户接受后才正式写入
- **适用节点**：WAITING_USER_CONFIRM、USER_EDITING_TEMPLATE
- **只读**：否
- **副作用**：在草稿副本上执行；通过后形成待确认候选，用户接受时才在正式事务里写入草稿。
- **主要错误**：
  - 目标不存在或不可唯一确定 → 返回 needsClarification + clarificationQuestion（草稿不变）
  - 候选基于旧版本草稿 → 拒绝（stale_edit_plan）
  - 依据的生效组合已变 → 拒绝（combination_conflict）
  - 数据源 / 指标不在当前授权范围 → 拒绝（incompatible_fields）
- **运行限制**：
  - 单步修改只针对一个区块；要一次改多处请用能力格式 operations
- **源码定位**：
  - registration：`app/schemas/edit_plan.py::EditIntent`
  - dispatch：`app/services/agent_service.py::AgentService._try_handle_template_edit`
  - validation：`app/schemas/edit_plan.py::EditPlan`
  - execution：`app/services/template_edit_engine.py::TemplateEditEngine._apply_add`

### ADD_METRIC

- **入口类型**：`legacy_intent` —— 旧格式编辑意图 —— 不是独立 Tool
- **分组**：旧意图 / 指标增删替换
- **状态**：`available`
- **用途**：在已有卡片里**追加**指标（保留原有指标）。 （**旧格式入口，不是独立 Tool**：模型在 submit_edit_plan 的 intent / target / operation 里给出，由 template_edit_engine.TemplateEditEngine.apply 执行。）
- **参数**：与上文「RENAME_REPORT」的参数契约完全相同（完整内容见文件末尾机器可读元数据的 `entries` 字段）
- **必填参数**：（无）
- **目标 / 引用要求**：target.regionId 或 target.matchedText 指明要改哪张卡片；组合修改用 operations 列表（旧格式每步共用顶层 target）
- **返回值**：EditResult：draftAfterEdit / changeSet / reply / markdownPreview / needsClarification；形成待确认候选，用户接受后才正式写入
- **适用节点**：WAITING_USER_CONFIRM、USER_EDITING_TEMPLATE
- **只读**：否
- **副作用**：在草稿副本上执行；通过后形成待确认候选，用户接受时才在正式事务里写入草稿。
- **主要错误**：
  - 目标不存在或不可唯一确定 → 返回 needsClarification + clarificationQuestion（草稿不变）
  - 候选基于旧版本草稿 → 拒绝（stale_edit_plan）
  - 依据的生效组合已变 → 拒绝（combination_conflict）
  - 数据源 / 指标不在当前授权范围 → 拒绝（incompatible_fields）
- **运行限制**：
  - 追加保留原指标；「替换 / 只留 / 换成」才是 CHANGE_METRIC
  - 单步修改只针对一个区块；要一次改多处请用能力格式 operations
- **源码定位**：
  - registration：`app/schemas/edit_plan.py::EditIntent`
  - dispatch：`app/services/agent_service.py::AgentService._try_handle_template_edit`
  - validation：`app/schemas/edit_plan.py::EditPlan`
  - execution：`app/services/template_edit_engine.py::TemplateEditEngine._apply_add_metric`

### CHANGE_DATA_SOURCE

- **入口类型**：`legacy_intent` —— 旧格式编辑意图 —— 不是独立 Tool
- **分组**：旧意图 / 数据口径与时间
- **状态**：`available`
- **用途**：换一个数据源。 （**旧格式入口，不是独立 Tool**：模型在 submit_edit_plan 的 intent / target / operation 里给出，由 template_edit_engine.TemplateEditEngine.apply 执行。）
- **参数**：与上文「RENAME_REPORT」的参数契约完全相同（完整内容见文件末尾机器可读元数据的 `entries` 字段）
- **必填参数**：（无）
- **目标 / 引用要求**：target.regionId 或 target.matchedText 指明要改哪张卡片；组合修改用 operations 列表（旧格式每步共用顶层 target）
- **返回值**：EditResult：draftAfterEdit / changeSet / reply / markdownPreview / needsClarification；形成待确认候选，用户接受后才正式写入
- **适用节点**：WAITING_USER_CONFIRM、USER_EDITING_TEMPLATE
- **只读**：否
- **副作用**：在草稿副本上执行；通过后形成待确认候选，用户接受时才在正式事务里写入草稿。
- **主要错误**：
  - 目标不存在或不可唯一确定 → 返回 needsClarification + clarificationQuestion（草稿不变）
  - 候选基于旧版本草稿 → 拒绝（stale_edit_plan）
  - 依据的生效组合已变 → 拒绝（combination_conflict）
  - 数据源 / 指标不在当前授权范围 → 拒绝（incompatible_fields）
- **运行限制**：
  - 单步修改只针对一个区块；要一次改多处请用能力格式 operations
- **源码定位**：
  - registration：`app/schemas/edit_plan.py::EditIntent`
  - dispatch：`app/services/agent_service.py::AgentService._try_handle_template_edit`
  - validation：`app/schemas/edit_plan.py::EditPlan`
  - execution：`app/services/template_edit_engine.py::TemplateEditEngine._apply_change_source`

### CHANGE_VISUALIZATION

- **入口类型**：`legacy_intent` —— 旧格式编辑意图 —— 不是独立 Tool
- **分组**：旧意图 / 展示方式
- **状态**：`available`
- **用途**：改展示类型（componentType / chartKind）。 （**旧格式入口，不是独立 Tool**：模型在 submit_edit_plan 的 intent / target / operation 里给出，由 template_edit_engine.TemplateEditEngine.apply 执行。）
- **参数**：与上文「RENAME_REPORT」的参数契约完全相同（完整内容见文件末尾机器可读元数据的 `entries` 字段）
- **必填参数**：（无）
- **目标 / 引用要求**：target.regionId 或 target.matchedText 指明要改哪张卡片；组合修改用 operations 列表（旧格式每步共用顶层 target）
- **返回值**：EditResult：draftAfterEdit / changeSet / reply / markdownPreview / needsClarification；形成待确认候选，用户接受后才正式写入
- **适用节点**：WAITING_USER_CONFIRM、USER_EDITING_TEMPLATE
- **只读**：否
- **副作用**：在草稿副本上执行；通过后形成待确认候选，用户接受时才在正式事务里写入草稿。
- **主要错误**：
  - 目标不存在或不可唯一确定 → 返回 needsClarification + clarificationQuestion（草稿不变）
  - 候选基于旧版本草稿 → 拒绝（stale_edit_plan）
  - 依据的生效组合已变 → 拒绝（combination_conflict）
  - 数据源 / 指标不在当前授权范围 → 拒绝（incompatible_fields）
- **运行限制**：
  - 单步修改只针对一个区块；要一次改多处请用能力格式 operations
- **源码定位**：
  - registration：`app/schemas/edit_plan.py::EditIntent`
  - dispatch：`app/services/agent_service.py::AgentService._try_handle_template_edit`
  - validation：`app/schemas/edit_plan.py::EditPlan`
  - execution：`app/services/template_edit_engine.py::TemplateEditEngine._apply_change_visualization`

### SUGGEST_VISUALIZATION

- **入口类型**：`legacy_intent` —— 旧格式编辑意图 —— 不是独立 Tool
- **分组**：旧意图 / 展示方式
- **状态**：`available`
- **用途**：只给展示方式建议，**不改草稿**。 （**旧格式入口，不是独立 Tool**：模型在 submit_edit_plan 的 intent / target / operation 里给出，由 template_edit_engine.TemplateEditEngine.apply 执行。）
- **参数**：与上文「RENAME_REPORT」的参数契约完全相同（完整内容见文件末尾机器可读元数据的 `entries` 字段）
- **必填参数**：（无）
- **目标 / 引用要求**：target.regionId 或 target.matchedText 指明要改哪张卡片；组合修改用 operations 列表（旧格式每步共用顶层 target）
- **返回值**：EditResult：draftAfterEdit / changeSet / reply / markdownPreview / needsClarification；形成待确认候选，用户接受后才正式写入
- **适用节点**：WAITING_USER_CONFIRM、USER_EDITING_TEMPLATE
- **只读**：是
- **副作用**：在草稿副本上执行；通过后形成待确认候选，用户接受时才在正式事务里写入草稿。
- **主要错误**：
  - 目标不存在或不可唯一确定 → 返回 needsClarification + clarificationQuestion（草稿不变）
  - 候选基于旧版本草稿 → 拒绝（stale_edit_plan）
  - 依据的生效组合已变 → 拒绝（combination_conflict）
  - 数据源 / 指标不在当前授权范围 → 拒绝（incompatible_fields）
- **运行限制**：
  - 只回建议，不改草稿，不占修改澄清轮次
  - 单步修改只针对一个区块；要一次改多处请用能力格式 operations
- **源码定位**：
  - registration：`app/schemas/edit_plan.py::EditIntent`
  - dispatch：`app/services/agent_service.py::AgentService._try_handle_template_edit`
  - validation：`app/schemas/edit_plan.py::EditPlan`
  - execution：`app/services/template_edit_engine.py::TemplateEditEngine._markdown_display_options`

### CHANGE_COMPILATION_GRAIN

- **入口类型**：`legacy_intent` —— 旧格式编辑意图 —— 不是独立 Tool
- **分组**：旧意图 / 数据口径与时间
- **状态**：`available`
- **用途**：改统计粒度。 （**旧格式入口，不是独立 Tool**：模型在 submit_edit_plan 的 intent / target / operation 里给出，由 template_edit_engine.TemplateEditEngine.apply 执行。）
- **参数**：与上文「RENAME_REPORT」的参数契约完全相同（完整内容见文件末尾机器可读元数据的 `entries` 字段）
- **必填参数**：（无）
- **目标 / 引用要求**：target.regionId 或 target.matchedText 指明要改哪张卡片；组合修改用 operations 列表（旧格式每步共用顶层 target）
- **返回值**：EditResult：draftAfterEdit / changeSet / reply / markdownPreview / needsClarification；形成待确认候选，用户接受后才正式写入
- **适用节点**：WAITING_USER_CONFIRM、USER_EDITING_TEMPLATE
- **只读**：否
- **副作用**：在草稿副本上执行；通过后形成待确认候选，用户接受时才在正式事务里写入草稿。
- **主要错误**：
  - 目标不存在或不可唯一确定 → 返回 needsClarification + clarificationQuestion（草稿不变）
  - 候选基于旧版本草稿 → 拒绝（stale_edit_plan）
  - 依据的生效组合已变 → 拒绝（combination_conflict）
  - 数据源 / 指标不在当前授权范围 → 拒绝（incompatible_fields）
- **运行限制**：
  - 单步修改只针对一个区块；要一次改多处请用能力格式 operations
- **源码定位**：
  - registration：`app/schemas/edit_plan.py::EditIntent`
  - dispatch：`app/services/agent_service.py::AgentService._try_handle_template_edit`
  - validation：`app/schemas/edit_plan.py::EditPlan`
  - execution：`app/services/template_edit_engine.py::TemplateEditEngine._apply_change_grain`

### CHANGE_TIME_RANGE

- **入口类型**：`legacy_intent` —— 旧格式编辑意图 —— 不是独立 Tool
- **分组**：旧意图 / 数据口径与时间
- **状态**：`available`
- **用途**：改时间范围。 （**旧格式入口，不是独立 Tool**：模型在 submit_edit_plan 的 intent / target / operation 里给出，由 template_edit_engine.TemplateEditEngine.apply 执行。）
- **参数**：与上文「RENAME_REPORT」的参数契约完全相同（完整内容见文件末尾机器可读元数据的 `entries` 字段）
- **必填参数**：（无）
- **目标 / 引用要求**：target.regionId 或 target.matchedText 指明要改哪张卡片；组合修改用 operations 列表（旧格式每步共用顶层 target）
- **返回值**：EditResult：draftAfterEdit / changeSet / reply / markdownPreview / needsClarification；形成待确认候选，用户接受后才正式写入
- **适用节点**：WAITING_USER_CONFIRM、USER_EDITING_TEMPLATE
- **只读**：否
- **副作用**：在草稿副本上执行；通过后形成待确认候选，用户接受时才在正式事务里写入草稿。
- **主要错误**：
  - 目标不存在或不可唯一确定 → 返回 needsClarification + clarificationQuestion（草稿不变）
  - 候选基于旧版本草稿 → 拒绝（stale_edit_plan）
  - 依据的生效组合已变 → 拒绝（combination_conflict）
  - 数据源 / 指标不在当前授权范围 → 拒绝（incompatible_fields）
- **运行限制**：
  - 单步修改只针对一个区块；要一次改多处请用能力格式 operations
- **源码定位**：
  - registration：`app/schemas/edit_plan.py::EditIntent`
  - dispatch：`app/services/agent_service.py::AgentService._try_handle_template_edit`
  - validation：`app/schemas/edit_plan.py::EditPlan`
  - execution：`app/services/template_edit_engine.py::TemplateEditEngine._apply_change_time_range`

### CHANGE_METRIC

- **入口类型**：`legacy_intent` —— 旧格式编辑意图 —— 不是独立 Tool
- **分组**：旧意图 / 指标增删替换
- **状态**：`available`
- **用途**：替换某张卡片里的指标。 （**旧格式入口，不是独立 Tool**：模型在 submit_edit_plan 的 intent / target / operation 里给出，由 template_edit_engine.TemplateEditEngine.apply 执行。）
- **参数**：与上文「RENAME_REPORT」的参数契约完全相同（完整内容见文件末尾机器可读元数据的 `entries` 字段）
- **必填参数**：（无）
- **目标 / 引用要求**：target.regionId 或 target.matchedText 指明要改哪张卡片；组合修改用 operations 列表（旧格式每步共用顶层 target）
- **返回值**：EditResult：draftAfterEdit / changeSet / reply / markdownPreview / needsClarification；形成待确认候选，用户接受后才正式写入
- **适用节点**：WAITING_USER_CONFIRM、USER_EDITING_TEMPLATE
- **只读**：否
- **副作用**：在草稿副本上执行；通过后形成待确认候选，用户接受时才在正式事务里写入草稿。
- **主要错误**：
  - 目标不存在或不可唯一确定 → 返回 needsClarification + clarificationQuestion（草稿不变）
  - 候选基于旧版本草稿 → 拒绝（stale_edit_plan）
  - 依据的生效组合已变 → 拒绝（combination_conflict）
  - 数据源 / 指标不在当前授权范围 → 拒绝（incompatible_fields）
- **运行限制**：
  - 单步修改只针对一个区块；要一次改多处请用能力格式 operations
- **源码定位**：
  - registration：`app/schemas/edit_plan.py::EditIntent`
  - dispatch：`app/services/agent_service.py::AgentService._try_handle_template_edit`
  - validation：`app/schemas/edit_plan.py::EditPlan`
  - execution：`app/services/template_edit_engine.py::TemplateEditEngine._apply_change_metric`

### CHANGE_TOP_N

- **入口类型**：`legacy_intent` —— 旧格式编辑意图 —— 不是独立 Tool
- **分组**：旧意图 / 数据口径与时间
- **状态**：`available`
- **用途**：改 Top N。 （**旧格式入口，不是独立 Tool**：模型在 submit_edit_plan 的 intent / target / operation 里给出，由 template_edit_engine.TemplateEditEngine.apply 执行。）
- **参数**：与上文「RENAME_REPORT」的参数契约完全相同（完整内容见文件末尾机器可读元数据的 `entries` 字段）
- **必填参数**：（无）
- **目标 / 引用要求**：target.regionId 或 target.matchedText 指明要改哪张卡片；组合修改用 operations 列表（旧格式每步共用顶层 target）
- **返回值**：EditResult：draftAfterEdit / changeSet / reply / markdownPreview / needsClarification；形成待确认候选，用户接受后才正式写入
- **适用节点**：WAITING_USER_CONFIRM、USER_EDITING_TEMPLATE
- **只读**：否
- **副作用**：在草稿副本上执行；通过后形成待确认候选，用户接受时才在正式事务里写入草稿。
- **主要错误**：
  - 目标不存在或不可唯一确定 → 返回 needsClarification + clarificationQuestion（草稿不变）
  - 候选基于旧版本草稿 → 拒绝（stale_edit_plan）
  - 依据的生效组合已变 → 拒绝（combination_conflict）
  - 数据源 / 指标不在当前授权范围 → 拒绝（incompatible_fields）
- **运行限制**：
  - 单步修改只针对一个区块；要一次改多处请用能力格式 operations
- **源码定位**：
  - registration：`app/schemas/edit_plan.py::EditIntent`
  - dispatch：`app/services/agent_service.py::AgentService._try_handle_template_edit`
  - validation：`app/schemas/edit_plan.py::EditPlan`
  - execution：`app/services/template_edit_engine.py::TemplateEditEngine._apply_change_top_n`

### CHANGE_SORT

- **入口类型**：`legacy_intent` —— 旧格式编辑意图 —— 不是独立 Tool
- **分组**：旧意图 / 数据口径与时间
- **状态**：`available`
- **用途**：改排序字段与方向。 （**旧格式入口，不是独立 Tool**：模型在 submit_edit_plan 的 intent / target / operation 里给出，由 template_edit_engine.TemplateEditEngine.apply 执行。）
- **参数**：与上文「RENAME_REPORT」的参数契约完全相同（完整内容见文件末尾机器可读元数据的 `entries` 字段）
- **必填参数**：（无）
- **目标 / 引用要求**：target.regionId 或 target.matchedText 指明要改哪张卡片；组合修改用 operations 列表（旧格式每步共用顶层 target）
- **返回值**：EditResult：draftAfterEdit / changeSet / reply / markdownPreview / needsClarification；形成待确认候选，用户接受后才正式写入
- **适用节点**：WAITING_USER_CONFIRM、USER_EDITING_TEMPLATE
- **只读**：否
- **副作用**：在草稿副本上执行；通过后形成待确认候选，用户接受时才在正式事务里写入草稿。
- **主要错误**：
  - 目标不存在或不可唯一确定 → 返回 needsClarification + clarificationQuestion（草稿不变）
  - 候选基于旧版本草稿 → 拒绝（stale_edit_plan）
  - 依据的生效组合已变 → 拒绝（combination_conflict）
  - 数据源 / 指标不在当前授权范围 → 拒绝（incompatible_fields）
- **运行限制**：
  - 单步修改只针对一个区块；要一次改多处请用能力格式 operations
- **源码定位**：
  - registration：`app/schemas/edit_plan.py::EditIntent`
  - dispatch：`app/services/agent_service.py::AgentService._try_handle_template_edit`
  - validation：`app/schemas/edit_plan.py::EditPlan`
  - execution：`app/services/template_edit_engine.py::TemplateEditEngine._apply_change_sort`

### APPLY_PRESENTATION

- **入口类型**：`legacy_intent` —— 旧格式编辑意图 —— 不是独立 Tool
- **分组**：旧意图 / 展示与样式
- **状态**：`available`
- **用途**：一批受约束的展示操作（分组 / 排版 / 样式 / 格式 / 页面 / 元数据 / 主题）。 （**旧格式入口，不是独立 Tool**：模型在 submit_edit_plan 的 intent / target / operation 里给出，由 template_edit_engine.TemplateEditEngine.apply 执行。）
- **参数**：与上文「RENAME_REPORT」的参数契约完全相同（完整内容见文件末尾机器可读元数据的 `entries` 字段）
- **必填参数**：（无）
- **目标 / 引用要求**：target.regionId 或 target.matchedText 指明要改哪张卡片；组合修改用 operations 列表（旧格式每步共用顶层 target）
- **返回值**：EditResult：draftAfterEdit / changeSet / reply / markdownPreview / needsClarification；形成待确认候选，用户接受后才正式写入
- **适用节点**：WAITING_USER_CONFIRM、USER_EDITING_TEMPLATE
- **只读**：否
- **副作用**：在草稿副本上执行；通过后形成待确认候选，用户接受时才在正式事务里写入草稿。
- **主要错误**：
  - 目标不存在或不可唯一确定 → 返回 needsClarification + clarificationQuestion（草稿不变）
  - 候选基于旧版本草稿 → 拒绝（stale_edit_plan）
  - 依据的生效组合已变 → 拒绝（combination_conflict）
  - 数据源 / 指标不在当前授权范围 → 拒绝（incompatible_fields）
- **运行限制**：
  - 只改外观：分组 / 排版 / 样式 / 格式 / 页面 / 元数据 / 主题
  - 不改统计口径、时间范围与设备范围
  - 单步修改只针对一个区块；要一次改多处请用能力格式 operations
  - 旧格式入口：展示操作写在 operation.presentationOperations 里（每项形如 {"op": "<操作名>", "params": {...}}）；能力格式里它们写在 SET_PRESENTATION 的 params.operations 里 —— 两者不要混用，展示操作名不要直接放进 submit_edit_plan.operations 的顶层
- **源码定位**：
  - registration：`app/schemas/edit_plan.py::EditIntent`
  - dispatch：`app/services/agent_service.py::AgentService._try_handle_template_edit`
  - validation：`app/schemas/edit_plan.py::EditPlan`
  - execution：`app/services/template_edit_engine.py::TemplateEditEngine._apply_presentation`

### SAVE_THEME

- **入口类型**：`legacy_intent` —— 旧格式编辑意图 —— 不是独立 Tool
- **分组**：旧意图 / 共享主题
- **状态**：`available`
- **用途**：把当前外观存成 / 更新一个**共享主题**（改主题本身，不改报表定义）。 （**旧格式入口，不是独立 Tool**：模型在 submit_edit_plan 的 intent / target / operation 里给出，由 template_edit_engine.TemplateEditEngine.apply 执行。）
- **参数**：与上文「RENAME_REPORT」的参数契约完全相同（完整内容见文件末尾机器可读元数据的 `entries` 字段）
- **必填参数**：（无）
- **目标 / 引用要求**：target.regionId 或 target.matchedText 指明要改哪张卡片；组合修改用 operations 列表（旧格式每步共用顶层 target）
- **返回值**：EditResult：draftAfterEdit / changeSet / reply / markdownPreview / needsClarification；形成待确认候选，用户接受后才正式写入
- **适用节点**：WAITING_USER_CONFIRM、USER_EDITING_TEMPLATE
- **只读**：否
- **副作用**：在草稿副本上执行；通过后形成待确认候选，用户接受时才在正式事务里写入草稿。
- **主要错误**：
  - 目标不存在或不可唯一确定 → 返回 needsClarification + clarificationQuestion（草稿不变）
  - 候选基于旧版本草稿 → 拒绝（stale_edit_plan）
  - 依据的生效组合已变 → 拒绝（combination_conflict）
  - 数据源 / 指标不在当前授权范围 → 拒绝（incompatible_fields）
- **运行限制**：
  - 未确认不落库：confirm=false 时只算影响摘要并追问
  - 不改任何报表的定义（regions / layout / format 不动），定义版本也不 bump
  - 单步修改只针对一个区块；要一次改多处请用能力格式 operations
- **源码定位**：
  - registration：`app/schemas/edit_plan.py::EditIntent`
  - dispatch：`app/services/agent_service.py::AgentService._try_handle_template_edit`
  - validation：`app/schemas/edit_plan.py::EditPlan`
  - execution：`app/services/template_edit_engine.py::TemplateEditEngine._apply_save_theme`

### UNKNOWN

- **入口类型**：`legacy_intent` —— 旧格式编辑意图 —— 不是独立 Tool
- **分组**：旧意图 / 未识别
- **状态**：`available`
- **用途**：无法识别的意图：转成追问，不改草稿。 （**旧格式入口，不是独立 Tool**：模型在 submit_edit_plan 的 intent / target / operation 里给出，由 template_edit_engine.TemplateEditEngine.apply 执行。）
- **参数**：与上文「RENAME_REPORT」的参数契约完全相同（完整内容见文件末尾机器可读元数据的 `entries` 字段）
- **必填参数**：（无）
- **目标 / 引用要求**：target.regionId 或 target.matchedText 指明要改哪张卡片；组合修改用 operations 列表（旧格式每步共用顶层 target）
- **返回值**：EditResult：draftAfterEdit / changeSet / reply / markdownPreview / needsClarification；形成待确认候选，用户接受后才正式写入
- **适用节点**：WAITING_USER_CONFIRM、USER_EDITING_TEMPLATE
- **只读**：是
- **副作用**：在草稿副本上执行；通过后形成待确认候选，用户接受时才在正式事务里写入草稿。
- **主要错误**：
  - 目标不存在或不可唯一确定 → 返回 needsClarification + clarificationQuestion（草稿不变）
  - 候选基于旧版本草稿 → 拒绝（stale_edit_plan）
  - 依据的生效组合已变 → 拒绝（combination_conflict）
  - 数据源 / 指标不在当前授权范围 → 拒绝（incompatible_fields）
- **运行限制**：
  - 不改草稿；转成一次追问
  - 单步修改只针对一个区块；要一次改多处请用能力格式 operations
- **源码定位**：
  - registration：`app/schemas/edit_plan.py::EditIntent`
  - dispatch：`app/services/agent_service.py::AgentService._try_handle_template_edit`
  - validation：`app/schemas/edit_plan.py::EditPlan`
  - execution：`app/services/template_edit_engine.py::TemplateEditEngine.apply`

## 四、展示操作白名单（受约束的展示操作）

### CREATE_GROUP

- **入口类型**：`operation` —— 后端执行的操作 —— 不是独立 Tool
- **分组**：展示操作 / 受约束白名单
- **状态**：`available`
- **用途**：受约束的展示操作 CREATE_GROUP（**不是独立 Tool，也不是顶层能力**）：把若干张卡片放进一个**新的分组**（容器）里：给 groupId、memberRegionIds，可选 name / arrangement。 必须放在 SET_PRESENTATION 的 params.operations 里（形如 {"op": "CREATE_GROUP", "params": {...}}），不能直接作为 submit_edit_plan.operations 的顶层项。
- **参数**（JSON Schema，取自真实契约）：

```json
{
  "type": "object",
  "description": "展示操作 CREATE_GROUP 的参数对象。必填：groupId、memberRegionIds。可选：name、description、role、arrangement。多出来的键会被拒绝。",
  "properties": {
    "groupId": {
      "type": "string",
      "description": "groupId（取值见展示服务的校验）",
      "x-reference": "当前草稿里的分组 id（CREATE_GROUP 建出来的分组）"
    },
    "memberRegionIds": {
      "type": "array",
      "description": "memberRegionIds（取值见展示服务的校验）",
      "x-reference": "当前草稿里的区块 id 列表（至少一张卡片）",
      "items": {
        "type": "string"
      }
    },
    "name": {
      "type": "string",
      "description": "name（取值见展示服务的校验）"
    },
    "description": {
      "type": "string",
      "description": "description（取值见展示服务的校验）"
    },
    "role": {
      "type": "string",
      "description": "role（取值见展示服务的校验）"
    },
    "arrangement": {
      "type": "object",
      "description": "arrangement（对象，字段见下方）",
      "properties": {
        "mode": {
          "type": "string",
          "description": "排列模式",
          "enum": [
            "GRID",
            "FLOW"
          ],
          "default": "GRID"
        },
        "columns": {
          "type": "integer",
          "description": "列数",
          "minimum": 1,
          "maximum": 12,
          "default": 2
        },
        "direction": {
          "type": "string",
          "description": "排列方向",
          "enum": [
            "HORIZONTAL",
            "VERTICAL"
          ],
          "default": "HORIZONTAL"
        },
        "gap": {
          "type": "object",
          "description": "间距（长度类：value + unit）",
          "x-unit": "PX",
          "properties": {
            "value": {
              "type": "integer",
              "description": "长度数值",
              "minimum": 0,
              "maximum": 96
            },
            "unit": {
              "type": "string",
              "description": "长度单位（只能 PX 或 PERCENT）",
              "enum": [
                "PX",
                "PERCENT"
              ]
            }
          },
          "additionalProperties": false
        }
      },
      "additionalProperties": false
    }
  },
  "additionalProperties": false,
  "required": [
    "groupId",
    "memberRegionIds"
  ]
}
```

- **必填参数**：groupId、memberRegionIds
- **目标 / 引用要求**：params 里的 regionId / groupId / memberRegionIds 等引用字段必须来自当前草稿（get_current_draft 返回的区块 id 清单）；groupId：当前草稿里的分组 id（CREATE_GROUP 建出来的分组）；memberRegionIds：当前草稿里的区块 id 列表（至少一张卡片）
- **返回值**：整批展示操作的结果：PresentationResult（ok / changeSet / affectedRegionIds / issues）
- **适用节点**：WAITING_USER_CONFIRM、USER_EDITING_TEMPLATE
- **只读**：否
- **副作用**：只改草稿副本的外观部分；正式写入同样要等用户接受候选后的事务。
- **主要错误**：
  - op 不在白名单 → invalid_operation
  - 参数键不对 → invalid_operation（展示服务的参数校验）
  - 删除 / 移动导致引用断裂 → broken_reference（附引用清单）
- **运行限制**：
  - 只改外观：分组 / 排版 / 样式 / 格式 / 页面 / 元数据 / 主题
  - 不改统计口径、时间范围与设备范围
  - 一批操作是一个修改单位：整批成功才生效，任一步失败整批放弃
  - arrangement.mode 取值域：GRID、FLOW
  - arrangement.columns 取值范围：1 到 12
  - arrangement.direction 取值域：HORIZONTAL、VERTICAL
  - arrangement.gap.value 取值范围：0 到 96
  - arrangement.gap.unit 取值域：PX、PERCENT
  - 最小结构示例（只给格式）：{"op": "CREATE_GROUP", "params": {"groupId": "<groupId>", "memberRegionIds": ["<memberRegionIds>"]}}
- **源码定位**：
  - registration：`app/services/report_presentation_service.py::ALLOWED_OPERATIONS`
  - dispatch：`app/services/report_presentation_service.py::apply_presentation`
  - validation：`app/services/report_presentation_service.py::normalize_presentation_operations`
  - execution：`app/services/report_presentation_service.py::_op_create_group`

### SET_GROUP_ARRANGEMENT

- **入口类型**：`operation` —— 后端执行的操作 —— 不是独立 Tool
- **分组**：展示操作 / 受约束白名单
- **状态**：`available`
- **用途**：受约束的展示操作 SET_GROUP_ARRANGEMENT（**不是独立 Tool，也不是顶层能力**）：改一个**分组**里卡片的排列：给 groupId 与 arrangement（列数 / 方向 / 间距）。 必须放在 SET_PRESENTATION 的 params.operations 里（形如 {"op": "SET_GROUP_ARRANGEMENT", "params": {...}}），不能直接作为 submit_edit_plan.operations 的顶层项。
- **参数**（JSON Schema，取自真实契约）：

```json
{
  "type": "object",
  "description": "展示操作 SET_GROUP_ARRANGEMENT 的参数对象。必填：groupId、arrangement。多出来的键会被拒绝。",
  "properties": {
    "groupId": {
      "type": "string",
      "description": "groupId（取值见展示服务的校验）",
      "x-reference": "当前草稿里的分组 id（CREATE_GROUP 建出来的分组）"
    },
    "arrangement": {
      "type": "object",
      "description": "arrangement（对象，字段见下方）",
      "properties": {
        "mode": {
          "type": "string",
          "description": "排列模式",
          "enum": [
            "GRID",
            "FLOW"
          ],
          "default": "GRID"
        },
        "columns": {
          "type": "integer",
          "description": "列数",
          "minimum": 1,
          "maximum": 12,
          "default": 2
        },
        "direction": {
          "type": "string",
          "description": "排列方向",
          "enum": [
            "HORIZONTAL",
            "VERTICAL"
          ],
          "default": "HORIZONTAL"
        },
        "gap": {
          "type": "object",
          "description": "间距（长度类：value + unit）",
          "x-unit": "PX",
          "properties": {
            "value": {
              "type": "integer",
              "description": "长度数值",
              "minimum": 0,
              "maximum": 96
            },
            "unit": {
              "type": "string",
              "description": "长度单位（只能 PX 或 PERCENT）",
              "enum": [
                "PX",
                "PERCENT"
              ]
            }
          },
          "additionalProperties": false
        }
      },
      "additionalProperties": false
    }
  },
  "additionalProperties": false,
  "required": [
    "groupId",
    "arrangement"
  ]
}
```

- **必填参数**：groupId、arrangement
- **目标 / 引用要求**：params 里的 regionId / groupId / memberRegionIds 等引用字段必须来自当前草稿（get_current_draft 返回的区块 id 清单）；groupId：当前草稿里的分组 id（CREATE_GROUP 建出来的分组）
- **返回值**：整批展示操作的结果：PresentationResult（ok / changeSet / affectedRegionIds / issues）
- **适用节点**：WAITING_USER_CONFIRM、USER_EDITING_TEMPLATE
- **只读**：否
- **副作用**：只改草稿副本的外观部分；正式写入同样要等用户接受候选后的事务。
- **主要错误**：
  - op 不在白名单 → invalid_operation
  - 参数键不对 → invalid_operation（展示服务的参数校验）
  - 删除 / 移动导致引用断裂 → broken_reference（附引用清单）
- **运行限制**：
  - 只改外观：分组 / 排版 / 样式 / 格式 / 页面 / 元数据 / 主题
  - 不改统计口径、时间范围与设备范围
  - 一批操作是一个修改单位：整批成功才生效，任一步失败整批放弃
  - arrangement.mode 取值域：GRID、FLOW
  - arrangement.columns 取值范围：1 到 12
  - arrangement.direction 取值域：HORIZONTAL、VERTICAL
  - arrangement.gap.value 取值范围：0 到 96
  - arrangement.gap.unit 取值域：PX、PERCENT
  - 最小结构示例（只给格式）：{"op": "SET_GROUP_ARRANGEMENT", "params": {"groupId": "<groupId>", "arrangement": {}}}
- **源码定位**：
  - registration：`app/services/report_presentation_service.py::ALLOWED_OPERATIONS`
  - dispatch：`app/services/report_presentation_service.py::apply_presentation`
  - validation：`app/services/report_presentation_service.py::normalize_presentation_operations`
  - execution：`app/services/report_presentation_service.py::_op_set_group_arrangement`

### MOVE_REGION

- **入口类型**：`operation` —— 后端执行的操作 —— 不是独立 Tool
- **分组**：展示操作 / 受约束白名单
- **状态**：`available`
- **用途**：受约束的展示操作 MOVE_REGION（**不是独立 Tool，也不是顶层能力**）：**移动**一张卡片：给 regionId，可选 targetGroupId + index，或 afterRegionId / beforeRegionId。 必须放在 SET_PRESENTATION 的 params.operations 里（形如 {"op": "MOVE_REGION", "params": {...}}），不能直接作为 submit_edit_plan.operations 的顶层项。
- **参数**（JSON Schema，取自真实契约）：

```json
{
  "type": "object",
  "description": "展示操作 MOVE_REGION 的参数对象。必填：regionId。可选：targetGroupId、index、afterRegionId、beforeRegionId。多出来的键会被拒绝。",
  "properties": {
    "regionId": {
      "type": "string",
      "description": "regionId（取值见展示服务的校验）",
      "x-reference": "当前草稿里的区块 id（get_current_draft 返回的正文卡片 id 清单）"
    },
    "targetGroupId": {
      "type": "string",
      "description": "targetGroupId（取值见展示服务的校验）",
      "x-reference": "当前草稿里的分组 id"
    },
    "index": {
      "type": "string",
      "description": "index（取值见展示服务的校验）"
    },
    "afterRegionId": {
      "type": "string",
      "description": "afterRegionId（取值见展示服务的校验）",
      "x-reference": "当前草稿里的区块 id（相对位置锚点：放到这张卡后面）"
    },
    "beforeRegionId": {
      "type": "string",
      "description": "beforeRegionId（取值见展示服务的校验）",
      "x-reference": "当前草稿里的区块 id（相对位置锚点：放到这张卡前面）"
    }
  },
  "additionalProperties": false,
  "required": [
    "regionId"
  ]
}
```

- **必填参数**：regionId
- **目标 / 引用要求**：params 里的 regionId / groupId / memberRegionIds 等引用字段必须来自当前草稿（get_current_draft 返回的区块 id 清单）；regionId：当前草稿里的区块 id（get_current_draft 返回的正文卡片 id 清单）；targetGroupId：当前草稿里的分组 id；afterRegionId：当前草稿里的区块 id（相对位置锚点：放到这张卡后面）；beforeRegionId：当前草稿里的区块 id（相对位置锚点：放到这张卡前面）
- **返回值**：整批展示操作的结果：PresentationResult（ok / changeSet / affectedRegionIds / issues）
- **适用节点**：WAITING_USER_CONFIRM、USER_EDITING_TEMPLATE
- **只读**：否
- **副作用**：只改草稿副本的外观部分；正式写入同样要等用户接受候选后的事务。
- **主要错误**：
  - op 不在白名单 → invalid_operation
  - 参数键不对 → invalid_operation（展示服务的参数校验）
  - 删除 / 移动导致引用断裂 → broken_reference（附引用清单）
- **运行限制**：
  - 只改外观：分组 / 排版 / 样式 / 格式 / 页面 / 元数据 / 主题
  - 不改统计口径、时间范围与设备范围
  - 一批操作是一个修改单位：整批成功才生效，任一步失败整批放弃
  - 最小结构示例（只给格式）：{"op": "MOVE_REGION", "params": {"regionId": "<regionId>"}}
- **源码定位**：
  - registration：`app/services/report_presentation_service.py::ALLOWED_OPERATIONS`
  - dispatch：`app/services/report_presentation_service.py::apply_presentation`
  - validation：`app/services/report_presentation_service.py::normalize_presentation_operations`
  - execution：`app/services/report_presentation_service.py::_op_move_region`

### UNGROUP

- **入口类型**：`operation` —— 后端执行的操作 —— 不是独立 Tool
- **分组**：展示操作 / 受约束白名单
- **状态**：`available`
- **用途**：受约束的展示操作 UNGROUP（**不是独立 Tool，也不是顶层能力**）：**解散分组**：给 groupId，里面的卡片回到原来的层级。 必须放在 SET_PRESENTATION 的 params.operations 里（形如 {"op": "UNGROUP", "params": {...}}），不能直接作为 submit_edit_plan.operations 的顶层项。
- **参数**（JSON Schema，取自真实契约）：

```json
{
  "type": "object",
  "description": "展示操作 UNGROUP 的参数对象。必填：groupId。多出来的键会被拒绝。",
  "properties": {
    "groupId": {
      "type": "string",
      "description": "groupId（取值见展示服务的校验）",
      "x-reference": "当前草稿里的分组 id（CREATE_GROUP 建出来的分组）"
    }
  },
  "additionalProperties": false,
  "required": [
    "groupId"
  ]
}
```

- **必填参数**：groupId
- **目标 / 引用要求**：params 里的 regionId / groupId / memberRegionIds 等引用字段必须来自当前草稿（get_current_draft 返回的区块 id 清单）；groupId：当前草稿里的分组 id（CREATE_GROUP 建出来的分组）
- **返回值**：整批展示操作的结果：PresentationResult（ok / changeSet / affectedRegionIds / issues）
- **适用节点**：WAITING_USER_CONFIRM、USER_EDITING_TEMPLATE
- **只读**：否
- **副作用**：只改草稿副本的外观部分；正式写入同样要等用户接受候选后的事务。
- **主要错误**：
  - op 不在白名单 → invalid_operation
  - 参数键不对 → invalid_operation（展示服务的参数校验）
  - 删除 / 移动导致引用断裂 → broken_reference（附引用清单）
- **运行限制**：
  - 只改外观：分组 / 排版 / 样式 / 格式 / 页面 / 元数据 / 主题
  - 不改统计口径、时间范围与设备范围
  - 一批操作是一个修改单位：整批成功才生效，任一步失败整批放弃
  - 最小结构示例（只给格式）：{"op": "UNGROUP", "params": {"groupId": "<groupId>"}}
- **源码定位**：
  - registration：`app/services/report_presentation_service.py::ALLOWED_OPERATIONS`
  - dispatch：`app/services/report_presentation_service.py::apply_presentation`
  - validation：`app/services/report_presentation_service.py::normalize_presentation_operations`
  - execution：`app/services/report_presentation_service.py::_op_ungroup`

### DELETE_GROUP

- **入口类型**：`operation` —— 后端执行的操作 —— 不是独立 Tool
- **分组**：展示操作 / 受约束白名单
- **状态**：`available`
- **用途**：受约束的展示操作 DELETE_GROUP（**不是独立 Tool，也不是顶层能力**）：**删除分组**：给 groupId；组里还有卡片时默认整批失败（force=true 才强制）。 必须放在 SET_PRESENTATION 的 params.operations 里（形如 {"op": "DELETE_GROUP", "params": {...}}），不能直接作为 submit_edit_plan.operations 的顶层项。
- **参数**（JSON Schema，取自真实契约）：

```json
{
  "type": "object",
  "description": "展示操作 DELETE_GROUP 的参数对象。必填：groupId。可选：force。多出来的键会被拒绝。",
  "properties": {
    "groupId": {
      "type": "string",
      "description": "groupId（取值见展示服务的校验）",
      "x-reference": "当前草稿里的分组 id（CREATE_GROUP 建出来的分组）"
    },
    "force": {
      "type": "string",
      "description": "force（取值见展示服务的校验）"
    }
  },
  "additionalProperties": false,
  "required": [
    "groupId"
  ]
}
```

- **必填参数**：groupId
- **目标 / 引用要求**：params 里的 regionId / groupId / memberRegionIds 等引用字段必须来自当前草稿（get_current_draft 返回的区块 id 清单）；groupId：当前草稿里的分组 id（CREATE_GROUP 建出来的分组）
- **返回值**：整批展示操作的结果：PresentationResult（ok / changeSet / affectedRegionIds / issues）
- **适用节点**：WAITING_USER_CONFIRM、USER_EDITING_TEMPLATE
- **只读**：否
- **副作用**：只改草稿副本的外观部分；正式写入同样要等用户接受候选后的事务。
- **主要错误**：
  - op 不在白名单 → invalid_operation
  - 参数键不对 → invalid_operation（展示服务的参数校验）
  - 删除 / 移动导致引用断裂 → broken_reference（附引用清单）
- **运行限制**：
  - 只改外观：分组 / 排版 / 样式 / 格式 / 页面 / 元数据 / 主题
  - 不改统计口径、时间范围与设备范围
  - 一批操作是一个修改单位：整批成功才生效，任一步失败整批放弃
  - 最小结构示例（只给格式）：{"op": "DELETE_GROUP", "params": {"groupId": "<groupId>"}}
- **源码定位**：
  - registration：`app/services/report_presentation_service.py::ALLOWED_OPERATIONS`
  - dispatch：`app/services/report_presentation_service.py::apply_presentation`
  - validation：`app/services/report_presentation_service.py::normalize_presentation_operations`
  - execution：`app/services/report_presentation_service.py::_op_delete_group`

### COPY_REGION

- **入口类型**：`operation` —— 后端执行的操作 —— 不是独立 Tool
- **分组**：展示操作 / 受约束白名单
- **状态**：`available`
- **用途**：受约束的展示操作 COPY_REGION（**不是独立 Tool，也不是顶层能力**）：**复制**一张卡片：给 regionId 与 newRegionId（不能与现有 id 重复）。 必须放在 SET_PRESENTATION 的 params.operations 里（形如 {"op": "COPY_REGION", "params": {...}}），不能直接作为 submit_edit_plan.operations 的顶层项。
- **参数**（JSON Schema，取自真实契约）：

```json
{
  "type": "object",
  "description": "展示操作 COPY_REGION 的参数对象。必填：regionId、newRegionId。多出来的键会被拒绝。",
  "properties": {
    "regionId": {
      "type": "string",
      "description": "regionId（取值见展示服务的校验）",
      "x-reference": "当前草稿里的区块 id（get_current_draft 返回的正文卡片 id 清单）"
    },
    "newRegionId": {
      "type": "string",
      "description": "newRegionId（取值见展示服务的校验）",
      "x-reference": "新卡片使用的 id（不能与现有区块 id 重复）"
    }
  },
  "additionalProperties": false,
  "required": [
    "regionId",
    "newRegionId"
  ]
}
```

- **必填参数**：regionId、newRegionId
- **目标 / 引用要求**：params 里的 regionId / groupId / memberRegionIds 等引用字段必须来自当前草稿（get_current_draft 返回的区块 id 清单）；regionId：当前草稿里的区块 id（get_current_draft 返回的正文卡片 id 清单）；newRegionId：新卡片使用的 id（不能与现有区块 id 重复）
- **返回值**：整批展示操作的结果：PresentationResult（ok / changeSet / affectedRegionIds / issues）
- **适用节点**：WAITING_USER_CONFIRM、USER_EDITING_TEMPLATE
- **只读**：否
- **副作用**：只改草稿副本的外观部分；正式写入同样要等用户接受候选后的事务。
- **主要错误**：
  - op 不在白名单 → invalid_operation
  - 参数键不对 → invalid_operation（展示服务的参数校验）
  - 删除 / 移动导致引用断裂 → broken_reference（附引用清单）
- **运行限制**：
  - 只改外观：分组 / 排版 / 样式 / 格式 / 页面 / 元数据 / 主题
  - 不改统计口径、时间范围与设备范围
  - 一批操作是一个修改单位：整批成功才生效，任一步失败整批放弃
  - 最小结构示例（只给格式）：{"op": "COPY_REGION", "params": {"regionId": "<regionId>", "newRegionId": "<newRegionId>"}}
- **源码定位**：
  - registration：`app/services/report_presentation_service.py::ALLOWED_OPERATIONS`
  - dispatch：`app/services/report_presentation_service.py::apply_presentation`
  - validation：`app/services/report_presentation_service.py::normalize_presentation_operations`
  - execution：`app/services/report_presentation_service.py::_op_copy_region`

### DELETE_REGION

- **入口类型**：`operation` —— 后端执行的操作 —— 不是独立 Tool
- **分组**：展示操作 / 受约束白名单
- **状态**：`available`
- **用途**：受约束的展示操作 DELETE_REGION（**不是独立 Tool，也不是顶层能力**）：**删除**一张卡片：给 regionId；有引用方时默认整批失败（force=true 才强制）。 必须放在 SET_PRESENTATION 的 params.operations 里（形如 {"op": "DELETE_REGION", "params": {...}}），不能直接作为 submit_edit_plan.operations 的顶层项。
- **参数**（JSON Schema，取自真实契约）：

```json
{
  "type": "object",
  "description": "展示操作 DELETE_REGION 的参数对象。必填：regionId。可选：force。多出来的键会被拒绝。",
  "properties": {
    "regionId": {
      "type": "string",
      "description": "regionId（取值见展示服务的校验）",
      "x-reference": "当前草稿里的区块 id（get_current_draft 返回的正文卡片 id 清单）"
    },
    "force": {
      "type": "string",
      "description": "force（取值见展示服务的校验）"
    }
  },
  "additionalProperties": false,
  "required": [
    "regionId"
  ]
}
```

- **必填参数**：regionId
- **目标 / 引用要求**：params 里的 regionId / groupId / memberRegionIds 等引用字段必须来自当前草稿（get_current_draft 返回的区块 id 清单）；regionId：当前草稿里的区块 id（get_current_draft 返回的正文卡片 id 清单）
- **返回值**：整批展示操作的结果：PresentationResult（ok / changeSet / affectedRegionIds / issues）
- **适用节点**：WAITING_USER_CONFIRM、USER_EDITING_TEMPLATE
- **只读**：否
- **副作用**：只改草稿副本的外观部分；正式写入同样要等用户接受候选后的事务。
- **主要错误**：
  - op 不在白名单 → invalid_operation
  - 参数键不对 → invalid_operation（展示服务的参数校验）
  - 删除 / 移动导致引用断裂 → broken_reference（附引用清单）
- **运行限制**：
  - 只改外观：分组 / 排版 / 样式 / 格式 / 页面 / 元数据 / 主题
  - 不改统计口径、时间范围与设备范围
  - 一批操作是一个修改单位：整批成功才生效，任一步失败整批放弃
  - 最小结构示例（只给格式）：{"op": "DELETE_REGION", "params": {"regionId": "<regionId>"}}
- **源码定位**：
  - registration：`app/services/report_presentation_service.py::ALLOWED_OPERATIONS`
  - dispatch：`app/services/report_presentation_service.py::apply_presentation`
  - validation：`app/services/report_presentation_service.py::normalize_presentation_operations`
  - execution：`app/services/report_presentation_service.py::_op_delete_region`

### HIDE_REGION

- **入口类型**：`operation` —— 后端执行的操作 —— 不是独立 Tool
- **分组**：展示操作 / 受约束白名单
- **状态**：`available`
- **用途**：受约束的展示操作 HIDE_REGION（**不是独立 Tool，也不是顶层能力**）：**隐藏**一张卡片（不删）：给 regionId。 必须放在 SET_PRESENTATION 的 params.operations 里（形如 {"op": "HIDE_REGION", "params": {...}}），不能直接作为 submit_edit_plan.operations 的顶层项。
- **参数**（JSON Schema，取自真实契约）：

```json
{
  "type": "object",
  "description": "展示操作 HIDE_REGION 的参数对象。必填：regionId。多出来的键会被拒绝。",
  "properties": {
    "regionId": {
      "type": "string",
      "description": "regionId（取值见展示服务的校验）",
      "x-reference": "当前草稿里的区块 id（get_current_draft 返回的正文卡片 id 清单）"
    }
  },
  "additionalProperties": false,
  "required": [
    "regionId"
  ]
}
```

- **必填参数**：regionId
- **目标 / 引用要求**：params 里的 regionId / groupId / memberRegionIds 等引用字段必须来自当前草稿（get_current_draft 返回的区块 id 清单）；regionId：当前草稿里的区块 id（get_current_draft 返回的正文卡片 id 清单）
- **返回值**：整批展示操作的结果：PresentationResult（ok / changeSet / affectedRegionIds / issues）
- **适用节点**：WAITING_USER_CONFIRM、USER_EDITING_TEMPLATE
- **只读**：否
- **副作用**：只改草稿副本的外观部分；正式写入同样要等用户接受候选后的事务。
- **主要错误**：
  - op 不在白名单 → invalid_operation
  - 参数键不对 → invalid_operation（展示服务的参数校验）
  - 删除 / 移动导致引用断裂 → broken_reference（附引用清单）
- **运行限制**：
  - 只改外观：分组 / 排版 / 样式 / 格式 / 页面 / 元数据 / 主题
  - 不改统计口径、时间范围与设备范围
  - 一批操作是一个修改单位：整批成功才生效，任一步失败整批放弃
  - 最小结构示例（只给格式）：{"op": "HIDE_REGION", "params": {"regionId": "<regionId>"}}
- **源码定位**：
  - registration：`app/services/report_presentation_service.py::ALLOWED_OPERATIONS`
  - dispatch：`app/services/report_presentation_service.py::apply_presentation`
  - validation：`app/services/report_presentation_service.py::normalize_presentation_operations`
  - execution：`app/services/report_presentation_service.py::_op_hide_region`

### SHOW_REGION

- **入口类型**：`operation` —— 后端执行的操作 —— 不是独立 Tool
- **分组**：展示操作 / 受约束白名单
- **状态**：`available`
- **用途**：受约束的展示操作 SHOW_REGION（**不是独立 Tool，也不是顶层能力**）：**恢复显示**一张隐藏的卡片：给 regionId。 必须放在 SET_PRESENTATION 的 params.operations 里（形如 {"op": "SHOW_REGION", "params": {...}}），不能直接作为 submit_edit_plan.operations 的顶层项。
- **参数**（JSON Schema，取自真实契约）：

```json
{
  "type": "object",
  "description": "展示操作 SHOW_REGION 的参数对象。必填：regionId。多出来的键会被拒绝。",
  "properties": {
    "regionId": {
      "type": "string",
      "description": "regionId（取值见展示服务的校验）",
      "x-reference": "当前草稿里的区块 id（get_current_draft 返回的正文卡片 id 清单）"
    }
  },
  "additionalProperties": false,
  "required": [
    "regionId"
  ]
}
```

- **必填参数**：regionId
- **目标 / 引用要求**：params 里的 regionId / groupId / memberRegionIds 等引用字段必须来自当前草稿（get_current_draft 返回的区块 id 清单）；regionId：当前草稿里的区块 id（get_current_draft 返回的正文卡片 id 清单）
- **返回值**：整批展示操作的结果：PresentationResult（ok / changeSet / affectedRegionIds / issues）
- **适用节点**：WAITING_USER_CONFIRM、USER_EDITING_TEMPLATE
- **只读**：否
- **副作用**：只改草稿副本的外观部分；正式写入同样要等用户接受候选后的事务。
- **主要错误**：
  - op 不在白名单 → invalid_operation
  - 参数键不对 → invalid_operation（展示服务的参数校验）
  - 删除 / 移动导致引用断裂 → broken_reference（附引用清单）
- **运行限制**：
  - 只改外观：分组 / 排版 / 样式 / 格式 / 页面 / 元数据 / 主题
  - 不改统计口径、时间范围与设备范围
  - 一批操作是一个修改单位：整批成功才生效，任一步失败整批放弃
  - 最小结构示例（只给格式）：{"op": "SHOW_REGION", "params": {"regionId": "<regionId>"}}
- **源码定位**：
  - registration：`app/services/report_presentation_service.py::ALLOWED_OPERATIONS`
  - dispatch：`app/services/report_presentation_service.py::apply_presentation`
  - validation：`app/services/report_presentation_service.py::normalize_presentation_operations`
  - execution：`app/services/report_presentation_service.py::_op_show_region`

### SET_PLACEMENT

- **入口类型**：`operation` —— 后端执行的操作 —— 不是独立 Tool
- **分组**：展示操作 / 受约束白名单
- **状态**：`available`
- **用途**：受约束的展示操作 SET_PLACEMENT（**不是独立 Tool，也不是顶层能力**）：改**定位**：给 regionId 与 placement（GRID / FLOW / ABSOLUTE）。 必须放在 SET_PRESENTATION 的 params.operations 里（形如 {"op": "SET_PLACEMENT", "params": {...}}），不能直接作为 submit_edit_plan.operations 的顶层项。
- **参数**（JSON Schema，取自真实契约）：

```json
{
  "type": "object",
  "description": "展示操作 SET_PLACEMENT 的参数对象。必填：regionId、placement。多出来的键会被拒绝。",
  "properties": {
    "regionId": {
      "type": "string",
      "description": "regionId（取值见展示服务的校验）",
      "x-reference": "当前草稿里的区块 id（get_current_draft 返回的正文卡片 id 清单）"
    },
    "placement": {
      "type": "object",
      "description": "placement（对象，字段见下方）",
      "properties": {
        "type": {
          "type": "string",
          "description": "定位方式",
          "enum": [
            "GRID",
            "FLOW",
            "ABSOLUTE"
          ]
        },
        "column": {
          "type": "integer",
          "description": "网格定位的起始列",
          "minimum": 1,
          "maximum": 12
        },
        "columnSpan": {
          "type": "integer",
          "description": "网格定位的列跨度",
          "minimum": 1,
          "maximum": 12
        },
        "row": {
          "type": "integer",
          "description": "网格定位的行",
          "minimum": 1,
          "maximum": 999
        },
        "rowSpan": {
          "type": "integer",
          "description": "网格定位的行跨度",
          "minimum": 1,
          "maximum": 12
        },
        "align": {
          "type": "string",
          "description": "流式定位的对齐方式",
          "enum": [
            "START",
            "CENTER",
            "END",
            "STRETCH"
          ]
        },
        "width": {
          "type": "object",
          "description": "流式定位的宽度（长度类：value + unit）",
          "x-unit": "PX",
          "properties": {
            "value": {
              "type": "integer",
              "description": "长度数值"
            },
            "unit": {
              "type": "string",
              "description": "长度单位（只能 PX 或 PERCENT）",
              "enum": [
                "PX",
                "PERCENT"
              ]
            }
          },
          "additionalProperties": false
        },
        "minHeight": {
          "type": "object",
          "description": "最小高度（长度类：value + unit）",
          "x-unit": "PX",
          "properties": {
            "value": {
              "type": "integer",
              "description": "长度数值"
            },
            "unit": {
              "type": "string",
              "description": "长度单位（只能 PX 或 PERCENT）",
              "enum": [
                "PX",
                "PERCENT"
              ]
            }
          },
          "additionalProperties": false
        },
        "x": {
          "type": "integer",
          "description": "固定定位的横坐标",
          "minimum": 0,
          "maximum": 100000
        },
        "y": {
          "type": "integer",
          "description": "固定定位的纵坐标",
          "minimum": 0,
          "maximum": 100000
        },
        "height": {
          "type": "integer",
          "description": "固定定位的高度（整数像素）",
          "minimum": 1,
          "maximum": 100000
        },
        "zIndex": {
          "type": "integer",
          "description": "固定定位的层级",
          "minimum": 0,
          "maximum": 9999
        }
      },
      "additionalProperties": false
    }
  },
  "additionalProperties": false,
  "required": [
    "regionId",
    "placement"
  ]
}
```

- **必填参数**：regionId、placement
- **目标 / 引用要求**：params 里的 regionId / groupId / memberRegionIds 等引用字段必须来自当前草稿（get_current_draft 返回的区块 id 清单）；regionId：当前草稿里的区块 id（get_current_draft 返回的正文卡片 id 清单）
- **返回值**：整批展示操作的结果：PresentationResult（ok / changeSet / affectedRegionIds / issues）
- **适用节点**：WAITING_USER_CONFIRM、USER_EDITING_TEMPLATE
- **只读**：否
- **副作用**：只改草稿副本的外观部分；正式写入同样要等用户接受候选后的事务。
- **主要错误**：
  - op 不在白名单 → invalid_operation
  - 参数键不对 → invalid_operation（展示服务的参数校验）
  - 删除 / 移动导致引用断裂 → broken_reference（附引用清单）
- **运行限制**：
  - 只改外观：分组 / 排版 / 样式 / 格式 / 页面 / 元数据 / 主题
  - 不改统计口径、时间范围与设备范围
  - 一批操作是一个修改单位：整批成功才生效，任一步失败整批放弃
  - placement.type 取值域：GRID、FLOW、ABSOLUTE
  - placement.column 取值范围：1 到 12
  - placement.columnSpan 取值范围：1 到 12
  - placement.row 取值范围：1 到 999
  - placement.rowSpan 取值范围：1 到 12
  - placement.align 取值域：START、CENTER、END、STRETCH
  - placement.width.unit 取值域：PX、PERCENT
  - placement.minHeight.unit 取值域：PX、PERCENT
  - placement.x 取值范围：0 到 100000
  - placement.y 取值范围：0 到 100000
  - placement.height 取值范围：1 到 100000
  - placement.zIndex 取值范围：0 到 9999
  - 最小结构示例（只给格式）：{"op": "SET_PLACEMENT", "params": {"regionId": "<regionId>", "placement": {}}}
- **源码定位**：
  - registration：`app/services/report_presentation_service.py::ALLOWED_OPERATIONS`
  - dispatch：`app/services/report_presentation_service.py::apply_presentation`
  - validation：`app/services/report_presentation_service.py::normalize_presentation_operations`
  - execution：`app/services/report_presentation_service.py::_op_set_placement`

### SET_STYLE

- **入口类型**：`operation` —— 后端执行的操作 —— 不是独立 Tool
- **分组**：展示操作 / 受约束白名单
- **状态**：`available`
- **用途**：受约束的展示操作 SET_STYLE（**不是独立 Tool，也不是顶层能力**）：改**外观**：给 regionId 与 style（字体 / 字号 / 字重 / 颜色 / 对齐 / 行高 / 内外边距 / 边框 / 圆角 / 透明度）。 必须放在 SET_PRESENTATION 的 params.operations 里（形如 {"op": "SET_STYLE", "params": {...}}），不能直接作为 submit_edit_plan.operations 的顶层项。
- **参数**（JSON Schema，取自真实契约）：

```json
{
  "type": "object",
  "description": "展示操作 SET_STYLE 的参数对象。必填：regionId、style。多出来的键会被拒绝。",
  "properties": {
    "regionId": {
      "type": "string",
      "description": "regionId（取值见展示服务的校验）",
      "x-reference": "当前草稿里的区块 id（get_current_draft 返回的正文卡片 id 清单）"
    },
    "style": {
      "type": "object",
      "description": "style（对象，字段见下方）",
      "properties": {
        "fontFamily": {
          "type": "string",
          "description": "字体名称",
          "maxLength": 200
        },
        "fontSize": {
          "type": "object",
          "description": "字号（长度类：value + unit）",
          "x-unit": "PX",
          "properties": {
            "value": {
              "type": "integer",
              "description": "长度数值",
              "minimum": 1,
              "maximum": 200
            },
            "unit": {
              "type": "string",
              "description": "长度单位（只能 PX 或 PERCENT）",
              "enum": [
                "PX",
                "PERCENT"
              ]
            }
          },
          "additionalProperties": false
        },
        "fontWeight": {
          "type": "integer",
          "description": "字重（纯数字，不是长度）",
          "minimum": 100,
          "maximum": 900
        },
        "textColor": {
          "type": "string",
          "description": "文字颜色",
          "maxLength": 64
        },
        "backgroundColor": {
          "type": "string",
          "description": "背景颜色",
          "maxLength": 64
        },
        "textAlign": {
          "type": "string",
          "description": "水平对齐",
          "enum": [
            "LEFT",
            "CENTER",
            "RIGHT"
          ]
        },
        "verticalAlign": {
          "type": "string",
          "description": "垂直对齐",
          "enum": [
            "TOP",
            "MIDDLE",
            "BOTTOM"
          ]
        },
        "lineHeight": {
          "type": "object",
          "description": "行高（长度类：value + unit）",
          "x-unit": "PX",
          "properties": {
            "value": {
              "type": "integer",
              "description": "长度数值",
              "minimum": 0,
              "maximum": 400
            },
            "unit": {
              "type": "string",
              "description": "长度单位（只能 PX 或 PERCENT）",
              "enum": [
                "PX",
                "PERCENT"
              ]
            }
          },
          "additionalProperties": false
        },
        "padding": {
          "type": "object",
          "description": "内边距（长度类：value + unit）",
          "x-unit": "PX",
          "properties": {
            "value": {
              "type": "integer",
              "description": "长度数值"
            },
            "unit": {
              "type": "string",
              "description": "长度单位（只能 PX 或 PERCENT）",
              "enum": [
                "PX",
                "PERCENT"
              ]
            }
          },
          "additionalProperties": false
        },
        "margin": {
          "type": "object",
          "description": "外边距（长度类：value + unit）",
          "x-unit": "PX",
          "properties": {
            "value": {
              "type": "integer",
              "description": "长度数值",
              "minimum": -200,
              "maximum": 400
            },
            "unit": {
              "type": "string",
              "description": "长度单位（只能 PX 或 PERCENT）",
              "enum": [
                "PX",
                "PERCENT"
              ]
            }
          },
          "additionalProperties": false
        },
        "border": {
          "type": "object",
          "description": "边框",
          "properties": {
            "width": {
              "type": "integer",
              "description": "边框宽度（纯数字像素）",
              "minimum": 0,
              "maximum": 20
            },
            "style": {
              "type": "string",
              "description": "边框线型",
              "enum": [
                "SOLID",
                "DASHED",
                "DOTTED",
                "NONE"
              ]
            },
            "color": {
              "type": "string",
              "description": "边框颜色",
              "maxLength": 64
            }
          },
          "additionalProperties": false
        },
        "borderRadius": {
          "type": "integer",
          "description": "圆角（纯数字像素）",
          "minimum": 0,
          "maximum": 200
        },
        "opacity": {
          "type": "number",
          "description": "透明度（0 到 1）",
          "minimum": 0,
          "maximum": 1
        }
      },
      "additionalProperties": false
    }
  },
  "additionalProperties": false,
  "required": [
    "regionId",
    "style"
  ]
}
```

- **必填参数**：regionId、style
- **目标 / 引用要求**：params 里的 regionId / groupId / memberRegionIds 等引用字段必须来自当前草稿（get_current_draft 返回的区块 id 清单）；regionId：当前草稿里的区块 id（get_current_draft 返回的正文卡片 id 清单）
- **返回值**：整批展示操作的结果：PresentationResult（ok / changeSet / affectedRegionIds / issues）
- **适用节点**：WAITING_USER_CONFIRM、USER_EDITING_TEMPLATE
- **只读**：否
- **副作用**：只改草稿副本的外观部分；正式写入同样要等用户接受候选后的事务。
- **主要错误**：
  - op 不在白名单 → invalid_operation
  - 参数键不对 → invalid_operation（展示服务的参数校验）
  - 删除 / 移动导致引用断裂 → broken_reference（附引用清单）
- **运行限制**：
  - 只改外观：分组 / 排版 / 样式 / 格式 / 页面 / 元数据 / 主题
  - 不改统计口径、时间范围与设备范围
  - 一批操作是一个修改单位：整批成功才生效，任一步失败整批放弃
  - style.fontSize.value 取值范围：1 到 200
  - style.fontSize.unit 取值域：PX、PERCENT
  - style.fontWeight 取值范围：100 到 900
  - style.textAlign 取值域：LEFT、CENTER、RIGHT
  - style.verticalAlign 取值域：TOP、MIDDLE、BOTTOM
  - style.lineHeight.value 取值范围：0 到 400
  - style.lineHeight.unit 取值域：PX、PERCENT
  - style.padding.unit 取值域：PX、PERCENT
  - style.margin.value 取值范围：-200 到 400
  - style.margin.unit 取值域：PX、PERCENT
  - style.border.width 取值范围：0 到 20
  - style.border.style 取值域：SOLID、DASHED、DOTTED、NONE
  - style.borderRadius 取值范围：0 到 200
  - style.opacity 取值范围：0 到 1
  - 最小结构示例（只给格式）：{"op": "SET_STYLE", "params": {"regionId": "<regionId>", "style": {}}}
- **源码定位**：
  - registration：`app/services/report_presentation_service.py::ALLOWED_OPERATIONS`
  - dispatch：`app/services/report_presentation_service.py::apply_presentation`
  - validation：`app/services/report_presentation_service.py::normalize_presentation_operations`
  - execution：`app/services/report_presentation_service.py::_op_set_style`

### SET_FORMAT

- **入口类型**：`operation` —— 后端执行的操作 —— 不是独立 Tool
- **分组**：展示操作 / 受约束白名单
- **状态**：`available`
- **用途**：受约束的展示操作 SET_FORMAT（**不是独立 Tool，也不是顶层能力**）：改**数字显示格式**：给 regionId 与 format（kind / precision / unitLabel / thousandSeparator / dateTimePattern / nullText / roundingMode）。 必须放在 SET_PRESENTATION 的 params.operations 里（形如 {"op": "SET_FORMAT", "params": {...}}），不能直接作为 submit_edit_plan.operations 的顶层项。
- **参数**（JSON Schema，取自真实契约）：

```json
{
  "type": "object",
  "description": "展示操作 SET_FORMAT 的参数对象。必填：regionId、format。多出来的键会被拒绝。",
  "properties": {
    "regionId": {
      "type": "string",
      "description": "regionId（取值见展示服务的校验）",
      "x-reference": "当前草稿里的区块 id（get_current_draft 返回的正文卡片 id 清单）"
    },
    "format": {
      "type": "object",
      "description": "format（对象，字段见下方）",
      "properties": {
        "kind": {
          "type": "string",
          "description": "显示格式类型",
          "enum": [
            "NUMBER",
            "PERCENT",
            "CURRENCY",
            "DURATION",
            "DATE",
            "DATETIME",
            "TEXT"
          ]
        },
        "precision": {
          "type": "integer",
          "description": "小数位数",
          "minimum": 0,
          "maximum": 6
        },
        "unitLabel": {
          "type": "string",
          "description": "单位标签（只能保持当前单位或留空）",
          "maxLength": 64
        },
        "thousandSeparator": {
          "type": "boolean",
          "description": "是否加千分位"
        },
        "dateTimePattern": {
          "type": "string",
          "description": "日期时间格式",
          "maxLength": 64
        },
        "nullText": {
          "type": "string",
          "description": "空值显示文本",
          "maxLength": 64
        },
        "roundingMode": {
          "type": "string",
          "description": "舍入方式",
          "enum": [
            "HALF_UP",
            "HALF_DOWN",
            "FLOOR",
            "CEIL"
          ]
        }
      },
      "additionalProperties": false
    }
  },
  "additionalProperties": false,
  "required": [
    "regionId",
    "format"
  ]
}
```

- **必填参数**：regionId、format
- **目标 / 引用要求**：params 里的 regionId / groupId / memberRegionIds 等引用字段必须来自当前草稿（get_current_draft 返回的区块 id 清单）；regionId：当前草稿里的区块 id（get_current_draft 返回的正文卡片 id 清单）
- **返回值**：整批展示操作的结果：PresentationResult（ok / changeSet / affectedRegionIds / issues）
- **适用节点**：WAITING_USER_CONFIRM、USER_EDITING_TEMPLATE
- **只读**：否
- **副作用**：只改草稿副本的外观部分；正式写入同样要等用户接受候选后的事务。
- **主要错误**：
  - op 不在白名单 → invalid_operation
  - 参数键不对 → invalid_operation（展示服务的参数校验）
  - 删除 / 移动导致引用断裂 → broken_reference（附引用清单）
- **运行限制**：
  - 只改外观：分组 / 排版 / 样式 / 格式 / 页面 / 元数据 / 主题
  - 不改统计口径、时间范围与设备范围
  - 一批操作是一个修改单位：整批成功才生效，任一步失败整批放弃
  - format.kind 取值域：NUMBER、PERCENT、CURRENCY、DURATION、DATE、DATETIME、TEXT
  - format.precision 取值范围：0 到 6
  - format.roundingMode 取值域：HALF_UP、HALF_DOWN、FLOOR、CEIL
  - 最小结构示例（只给格式）：{"op": "SET_FORMAT", "params": {"regionId": "<regionId>", "format": {}}}
- **源码定位**：
  - registration：`app/services/report_presentation_service.py::ALLOWED_OPERATIONS`
  - dispatch：`app/services/report_presentation_service.py::apply_presentation`
  - validation：`app/services/report_presentation_service.py::normalize_presentation_operations`
  - execution：`app/services/report_presentation_service.py::_op_set_format`

### SET_PAGE

- **入口类型**：`operation` —— 后端执行的操作 —— 不是独立 Tool
- **分组**：展示操作 / 受约束白名单
- **状态**：`available`
- **用途**：受约束的展示操作 SET_PAGE（**不是独立 Tool，也不是顶层能力**）：改**整页设置**：给 webMaxWidth 与 / 或 arrangement。 必须放在 SET_PRESENTATION 的 params.operations 里（形如 {"op": "SET_PAGE", "params": {...}}），不能直接作为 submit_edit_plan.operations 的顶层项。
- **参数**（JSON Schema，取自真实契约）：

```json
{
  "type": "object",
  "description": "展示操作 SET_PAGE 的参数对象。没有必填键。可选：webMaxWidth、arrangement。多出来的键会被拒绝。",
  "properties": {
    "webMaxWidth": {
      "type": "object",
      "description": "webMaxWidth（对象，字段见下方）",
      "properties": {
        "value": {
          "type": "integer",
          "description": "长度数值",
          "minimum": 0,
          "maximum": 400
        },
        "unit": {
          "type": "string",
          "description": "长度单位（只能 PX 或 PERCENT）",
          "enum": [
            "PX",
            "PERCENT"
          ]
        }
      },
      "additionalProperties": false
    },
    "arrangement": {
      "type": "object",
      "description": "arrangement（对象，字段见下方）",
      "properties": {
        "mode": {
          "type": "string",
          "description": "排列模式",
          "enum": [
            "GRID",
            "FLOW"
          ],
          "default": "GRID"
        },
        "columns": {
          "type": "integer",
          "description": "列数",
          "minimum": 1,
          "maximum": 12,
          "default": 2
        },
        "direction": {
          "type": "string",
          "description": "排列方向",
          "enum": [
            "HORIZONTAL",
            "VERTICAL"
          ],
          "default": "HORIZONTAL"
        },
        "gap": {
          "type": "object",
          "description": "间距（长度类：value + unit）",
          "x-unit": "PX",
          "properties": {
            "value": {
              "type": "integer",
              "description": "长度数值",
              "minimum": 0,
              "maximum": 96
            },
            "unit": {
              "type": "string",
              "description": "长度单位（只能 PX 或 PERCENT）",
              "enum": [
                "PX",
                "PERCENT"
              ]
            }
          },
          "additionalProperties": false
        }
      },
      "additionalProperties": false
    }
  },
  "additionalProperties": false
}
```

- **必填参数**：（无）
- **目标 / 引用要求**：params 里的 regionId / groupId / memberRegionIds 等引用字段必须来自当前草稿（get_current_draft 返回的区块 id 清单）；
- **返回值**：整批展示操作的结果：PresentationResult（ok / changeSet / affectedRegionIds / issues）
- **适用节点**：WAITING_USER_CONFIRM、USER_EDITING_TEMPLATE
- **只读**：否
- **副作用**：只改草稿副本的外观部分；正式写入同样要等用户接受候选后的事务。
- **主要错误**：
  - op 不在白名单 → invalid_operation
  - 参数键不对 → invalid_operation（展示服务的参数校验）
  - 删除 / 移动导致引用断裂 → broken_reference（附引用清单）
- **运行限制**：
  - 只改外观：分组 / 排版 / 样式 / 格式 / 页面 / 元数据 / 主题
  - 不改统计口径、时间范围与设备范围
  - 一批操作是一个修改单位：整批成功才生效，任一步失败整批放弃
  - webMaxWidth.value 取值范围：0 到 400
  - webMaxWidth.unit 取值域：PX、PERCENT
  - arrangement.mode 取值域：GRID、FLOW
  - arrangement.columns 取值范围：1 到 12
  - arrangement.direction 取值域：HORIZONTAL、VERTICAL
  - arrangement.gap.value 取值范围：0 到 96
  - arrangement.gap.unit 取值域：PX、PERCENT
  - 最小结构示例（只给格式）：{"op": "SET_PAGE", "params": {}}
- **源码定位**：
  - registration：`app/services/report_presentation_service.py::ALLOWED_OPERATIONS`
  - dispatch：`app/services/report_presentation_service.py::apply_presentation`
  - validation：`app/services/report_presentation_service.py::normalize_presentation_operations`
  - execution：`app/services/report_presentation_service.py::_op_set_page`

### SET_METADATA

- **入口类型**：`operation` —— 后端执行的操作 —— 不是独立 Tool
- **分组**：展示操作 / 受约束白名单
- **状态**：`available`
- **用途**：受约束的展示操作 SET_METADATA（**不是独立 Tool，也不是顶层能力**）：改**报表元数据**：给 name / description / tags。 必须放在 SET_PRESENTATION 的 params.operations 里（形如 {"op": "SET_METADATA", "params": {...}}），不能直接作为 submit_edit_plan.operations 的顶层项。
- **参数**（JSON Schema，取自真实契约）：

```json
{
  "type": "object",
  "description": "展示操作 SET_METADATA 的参数对象。没有必填键。可选：name、description、tags。多出来的键会被拒绝。",
  "properties": {
    "name": {
      "type": "string",
      "description": "name（取值见展示服务的校验）"
    },
    "description": {
      "type": "string",
      "description": "description（取值见展示服务的校验）"
    },
    "tags": {
      "type": "string",
      "description": "tags（取值见展示服务的校验）"
    }
  },
  "additionalProperties": false
}
```

- **必填参数**：（无）
- **目标 / 引用要求**：params 里的 regionId / groupId / memberRegionIds 等引用字段必须来自当前草稿（get_current_draft 返回的区块 id 清单）；
- **返回值**：整批展示操作的结果：PresentationResult（ok / changeSet / affectedRegionIds / issues）
- **适用节点**：WAITING_USER_CONFIRM、USER_EDITING_TEMPLATE
- **只读**：否
- **副作用**：只改草稿副本的外观部分；正式写入同样要等用户接受候选后的事务。
- **主要错误**：
  - op 不在白名单 → invalid_operation
  - 参数键不对 → invalid_operation（展示服务的参数校验）
  - 删除 / 移动导致引用断裂 → broken_reference（附引用清单）
- **运行限制**：
  - 只改外观：分组 / 排版 / 样式 / 格式 / 页面 / 元数据 / 主题
  - 不改统计口径、时间范围与设备范围
  - 一批操作是一个修改单位：整批成功才生效，任一步失败整批放弃
  - 最小结构示例（只给格式）：{"op": "SET_METADATA", "params": {}}
- **源码定位**：
  - registration：`app/services/report_presentation_service.py::ALLOWED_OPERATIONS`
  - dispatch：`app/services/report_presentation_service.py::apply_presentation`
  - validation：`app/services/report_presentation_service.py::normalize_presentation_operations`
  - execution：`app/services/report_presentation_service.py::_op_set_metadata`

### SET_THEME

- **入口类型**：`operation` —— 后端执行的操作 —— 不是独立 Tool
- **分组**：展示操作 / 受约束白名单
- **状态**：`available`
- **用途**：受约束的展示操作 SET_THEME（**不是独立 Tool，也不是顶层能力**）：把一个**共享主题**应用到这份报表：给 themeId（取自 availableThemes）。 必须放在 SET_PRESENTATION 的 params.operations 里（形如 {"op": "SET_THEME", "params": {...}}），不能直接作为 submit_edit_plan.operations 的顶层项。
- **参数**（JSON Schema，取自真实契约）：

```json
{
  "type": "object",
  "description": "展示操作 SET_THEME 的参数对象。必填：themeId。多出来的键会被拒绝。",
  "properties": {
    "themeId": {
      "type": "string",
      "description": "themeId（取值见展示服务的校验）"
    }
  },
  "additionalProperties": false,
  "required": [
    "themeId"
  ]
}
```

- **必填参数**：themeId
- **目标 / 引用要求**：params 里的 regionId / groupId / memberRegionIds 等引用字段必须来自当前草稿（get_current_draft 返回的区块 id 清单）；
- **返回值**：整批展示操作的结果：PresentationResult（ok / changeSet / affectedRegionIds / issues）
- **适用节点**：WAITING_USER_CONFIRM、USER_EDITING_TEMPLATE
- **只读**：否
- **副作用**：只改草稿副本的外观部分；正式写入同样要等用户接受候选后的事务。
- **主要错误**：
  - op 不在白名单 → invalid_operation
  - 参数键不对 → invalid_operation（展示服务的参数校验）
  - 删除 / 移动导致引用断裂 → broken_reference（附引用清单）
- **运行限制**：
  - 只改外观：分组 / 排版 / 样式 / 格式 / 页面 / 元数据 / 主题
  - 不改统计口径、时间范围与设备范围
  - 一批操作是一个修改单位：整批成功才生效，任一步失败整批放弃
  - 最小结构示例（只给格式）：{"op": "SET_THEME", "params": {"themeId": "<themeId>"}}
- **源码定位**：
  - registration：`app/services/report_presentation_service.py::ALLOWED_OPERATIONS`
  - dispatch：`app/services/report_presentation_service.py::apply_presentation`
  - validation：`app/services/report_presentation_service.py::normalize_presentation_operations`
  - execution：`app/services/report_presentation_service.py::_op_set_theme`

## 五、规则快路（确定性快路径）

### RULE_TEMPLATE_EDIT

- **入口类型**：`operation` —— 后端执行的操作 —— 不是独立 Tool
- **分组**：规则快路（确定性快路径，不是独立 Tool）
- **状态**：`available`
- **用途**：规则快路（**不是独立 Tool**，模型调不到）：编辑态收到明确可判定的修改请求时，规则层直接解析出编辑计划并形成候选，不经模型。模型只处理规则认不出来的部分。
- **参数**（JSON Schema，取自真实契约）：

```json
{}
```

- **必填参数**：（无）
- **目标 / 引用要求**：目标由「前端可见正文卡片 ID 的顺序」与用户点名共同确定；认不准就追问。
- **返回值**：AgentResponse：待确认候选 + 「接受修改 / 撤回上一步 / 继续修改」菜单。
- **适用节点**：WAITING_USER_CONFIRM、USER_EDITING_TEMPLATE
- **只读**：否
- **副作用**：只形成待确认候选；草稿的正式改动由用户接受后的事务落库。
- **主要错误**：
  - 指代词没选中对象 / 序数越界 → 转成一次澄清
  - 时间窗说不清 → 给具体时间段选项让用户选
- **运行限制**：
  - 只在 WAITING_USER_CONFIRM / USER_EDITING_TEMPLATE 生效
  - 只在规则明确命中时接管，认不出来就交回模型支路
- **源码定位**：
  - registration：`app/services/agent_service.py::AgentService._try_handle_template_edit`
  - dispatch：`app/services/agent_service.py::AgentService._try_handle_template_edit`
  - validation：`app/services/template_edit_engine.py::TemplateEditEngine.parse`
  - execution：`app/services/template_edit_engine.py::TemplateEditEngine.apply`

### RULE_READONLY_QUERY_ANSWER

- **入口类型**：`operation` —— 后端执行的操作 —— 不是独立 Tool
- **分组**：规则快路（确定性快路径，不是独立 Tool）
- **状态**：`available`
- **用途**：规则快路（**不是独立 Tool**，模型调不到）：编辑态的只读能力查询（如「支持哪些粒度」）能用确定性证据回答时直接答。
- **参数**：（无入参）
- **必填参数**：（无）
- **目标 / 引用要求**：无（只回答当前草稿的能力范围）。
- **返回值**：AgentResponse：只追加聊天，不产生待确认计划，不推进业务状态。
- **适用节点**：WAITING_USER_CONFIRM、USER_EDITING_TEMPLATE
- **只读**：是
- **副作用**：只读：不改草稿、不产生候选、不占修改澄清轮次。
- **主要错误**：
  - 查不到证据时返回空，不编造能力
- **运行限制**：
  - 只回答只读问题；涉及修改的请求一律走修改支路
- **源码定位**：
  - registration：`app/services/agent_service.py::AgentService._try_answer_readonly_query`
  - dispatch：`app/services/agent_service.py::AgentService._try_answer_readonly_query`
  - validation：`app/services/template_edit_engine.py::TemplateEditEngine.parse`
  - execution：`app/services/agent_service.py::AgentService._try_answer_readonly_query`

### RULE_CORRECT_PLAN

- **入口类型**：`operation` —— 后端执行的操作 —— 不是独立 Tool
- **分组**：规则快路（确定性快路径，不是独立 Tool）
- **状态**：`available`
- **用途**：规则快路（**不是独立 Tool**，模型调不到）：用户对**当前待办**的纠正（「其他」自由文本）落到同一份计划上，不重开一轮普通对话。
- **参数**：（无入参）
- **必填参数**：（无）
- **目标 / 引用要求**：目标必须仍是待办计划指的那张卡片；换卡片是新任务，不走这里。
- **返回值**：修正后的 EditPlan（认不出来时返回 None → 同一张卡片里继续追问）。
- **适用节点**：WAITING_USER_CONFIRM、USER_EDITING_TEMPLATE
- **只读**：否
- **副作用**：只修正计划本身，不改草稿。
- **主要错误**：
  - 纠正后仍说不清要改成什么样 → 返回 None（同一张卡片里继续追问）
- **运行限制**：
  - 只处理对当前待办的纠正，不接受换目标的指令
- **源码定位**：
  - registration：`app/services/template_edit_engine.py::TemplateEditEngine.correct_plan`
  - dispatch：`app/services/agent_service.py::AgentService._try_handle_template_edit`
  - validation：`app/schemas/edit_plan.py::EditPlan`
  - execution：`app/services/template_edit_engine.py::TemplateEditEngine.correct_plan`

### RULE_EDIT_PLAN_PARSE

- **入口类型**：`operation` —— 后端执行的操作 —— 不是独立 Tool
- **分组**：规则快路（确定性快路径，不是独立 Tool）
- **状态**：`available`
- **用途**：规则快路（**不是独立 Tool**，模型调不到）：把一句自然语言修改要求解析成旧格式 EditPlan（intent / target / operation）。
- **参数**：（无入参）
- **必填参数**：（无）
- **目标 / 引用要求**：目标由可见正文卡片顺序、用户点名与卡片别名共同确定。
- **返回值**：EditPlan；认不出来时 intent=UNKNOWN 且 needsClarification=true。
- **适用节点**：WAITING_USER_CONFIRM、USER_EDITING_TEMPLATE
- **只读**：否
- **副作用**：只解析出计划，不改草稿。
- **主要错误**：
  - 认不出来 → intent=UNKNOWN + 追问
  - 目标不唯一 → 追问是哪一张
- **运行限制**：
  - 只产出旧格式计划；能力格式由模型组装 operations 后走试做
- **源码定位**：
  - registration：`app/services/template_edit_engine.py::TemplateEditEngine.parse`
  - dispatch：`app/services/template_edit_engine.py::TemplateEditEngine.parse`
  - validation：`app/schemas/edit_plan.py::EditPlan`
  - execution：`app/services/template_edit_engine.py::TemplateEditEngine.apply`

## 七、模拟分类维度目录

来源：`app/services/edit_capabilities/chart_data.py::SIMULATED_DIMENSIONS`。**数值是模拟的**，载荷带 `simulated` 标记，不接真实告警源。

- **告警等级**（`alertLevel`）：单位 条；口径 按告警等级统计告警数量；别名 告警等级、等级、告警级别、等级分布、按等级、告警等级分布
  - 分类与数值：严重=18、重要=27、一般=41、提示=12
- **告警区域**（`alertRegion`）：单位 条；口径 按告警区域统计告警数量；别名 告警区域、区域、区域分布、按区域、告警区域分布
  - 分类与数值：华北机房=26、华东机房=34、华南机房=22、西部机房=16
- **告警类型**（`alertType`）：单位 条；口径 按告警类型统计告警数量；别名 告警类型、类型、类型分布、按类型、告警类型分布
  - 分类与数值：温度越限=29、电压异常=21、通信中断=17、设备离线=31

<!-- 机器可读元数据（本文件的唯一机器可读来源；上面的正文是给人看的） -->
```json
{
  "version": "72d0cc0c0c8adcc7",
  "contractFingerprint": "72d0cc0c0c8adcc71374496f9491b273b3364e5529346c7a16b231a40dc33a4a",
  "generatedAt": "2026-11-09T03:46:37+00:00",
  "entries": [
    {
      "id": "search_catalog",
      "group": "工具 / 事实查询（只读）",
      "entryType": "agent_tool",
      "status": "available",
      "purpose": "查询当前项目授权范围内可用的数据目录（数据源）。参数 sourceRef 查具体数据源；参数 subjectRef 按业务对象过滤。返回的数据源包含 supportedGrains（支持的时间粒度）和 fields（可用字段），生成报表前必须先确认目标数据源存在且支持所需粒度。",
      "params": {
        "type": "object",
        "properties": {
          "sourceRef": {
            "type": "string",
            "description": "可选，数据源 ID"
          },
          "subjectRef": {
            "type": "string",
            "description": "可选，业务对象，如 machine_room"
          },
          "limit": {
            "type": "integer",
            "description": "可选，最多返回几条；超出会标 truncated=true 并给出 total"
          }
        }
      },
      "required": [],
      "targetSpec": "sourceRef / subjectRef 可空；不传时返回当前账号可见的全部目录。",
      "resultSpec": "{items: 目录项[], total: 总数, truncated: 是否被裁剪}",
      "states": [
        "ANALYZING",
        "NEEDS_CLARIFICATION",
        "DEFINITION_DRAFT_READY",
        "WAITING_USER_CONFIRM",
        "USER_EDITING_TEMPLATE",
        "VALIDATION_FAILED",
        "SAVE_DEFINITION_DRAFT",
        "VALIDATING",
        "PREVIEWING",
        "PREVIEW_READY",
        "RENDERING",
        "READY",
        "INTERRUPT_CONFIRMING",
        "BLOCKED_OR_UNSUPPORTED"
      ],
      "readOnly": true,
      "sideEffects": "只读：不改草稿、不写业务数据。",
      "errors": [
        "没有授权范围时直接报错，不返回空列表冒充「没有目录」",
        "目录服务不可用时抛错，由统一注册层转成 error 结果（不降级成空结果）"
      ],
      "limits": [
        "结果按 observation_max_items 裁剪，可用 limit 参数指定条数"
      ],
      "code": {
        "registration": "app/tools/registry.py::_TOOLS",
        "dispatch": "app/tools/search_catalog_tool.py::run",
        "validation": "app/tools/search_catalog_tool.py::PARAMETERS",
        "execution": "app/services/scope_service.py::filter_visible"
      }
    },
    {
      "id": "search_metric",
      "group": "工具 / 事实查询（只读）",
      "entryType": "agent_tool",
      "status": "available",
      "purpose": "查询当前项目授权范围内**已登记**的指标口径。参数 metricRef 查具体指标；参数 subjectRef 按业务对象过滤。返回的指标包含 unit（单位）和 fieldRef（底层字段）——这些是真实口径，查到的指标可以直接用。查不到**不代表**用户不能提这个指标：当前是模拟业务数据模式，用户口述、目录里没有的指标按「目录外模拟指标」处理即可（服务端给它补「仅模拟展示、业务口径待确认」标注），不需要等管理员先登记，也不要因此拒绝用户的请求。",
      "params": {
        "type": "object",
        "properties": {
          "metricRef": {
            "type": "string",
            "description": "可选，指标 ID"
          },
          "subjectRef": {
            "type": "string",
            "description": "可选，业务对象"
          },
          "limit": {
            "type": "integer",
            "description": "可选，最多返回几条；超出会标 truncated=true 并给出 total"
          }
        }
      },
      "required": [],
      "targetSpec": "metricRef / subjectRef 可空；不传时返回当前账号可见的全部指标。",
      "resultSpec": "{items: 指标项[], total: 总数, truncated: 是否被裁剪}",
      "states": [
        "ANALYZING",
        "NEEDS_CLARIFICATION",
        "DEFINITION_DRAFT_READY",
        "WAITING_USER_CONFIRM",
        "USER_EDITING_TEMPLATE",
        "VALIDATION_FAILED",
        "SAVE_DEFINITION_DRAFT",
        "VALIDATING",
        "PREVIEWING",
        "PREVIEW_READY",
        "RENDERING",
        "READY",
        "INTERRUPT_CONFIRMING",
        "BLOCKED_OR_UNSUPPORTED"
      ],
      "readOnly": true,
      "sideEffects": "只读：不改草稿、不写业务数据。",
      "errors": [
        "没有授权范围时直接报错，不返回空列表冒充「没有指标」",
        "指标目录不可用时抛错，由统一注册层转成 error 结果"
      ],
      "limits": [
        "结果按 observation_max_items 裁剪，可用 limit 参数指定条数"
      ],
      "code": {
        "registration": "app/tools/registry.py::_TOOLS",
        "dispatch": "app/tools/search_metric_tool.py::run",
        "validation": "app/tools/search_metric_tool.py::PARAMETERS",
        "execution": "app/services/scope_service.py::filter_visible"
      }
    },
    {
      "id": "check_capability",
      "group": "工具 / 事实查询（只读）",
      "entryType": "agent_tool",
      "status": "available",
      "purpose": "检查数据源是否支持某个时间粒度，返回 supported 和该数据源支持的粒度列表。若用户要求的粒度不支持，不要硬改，应改用支持的粒度或换数据源，并在回复中说明原因。",
      "params": {
        "type": "object",
        "properties": {
          "sourceRef": {
            "type": "string",
            "description": "数据源 ID"
          },
          "grainUnit": {
            "type": "string",
            "enum": [
              "MINUTE",
              "HOUR",
              "DAY",
              "WEEK",
              "MONTH"
            ],
            "description": "时间粒度"
          }
        },
        "required": [
          "sourceRef",
          "grainUnit"
        ]
      },
      "required": [
        "sourceRef",
        "grainUnit"
      ],
      "targetSpec": "sourceRef 必须是目录里真实存在的数据源 id；grainUnit 取粒度单位。",
      "resultSpec": "{sourceRef, grainUnit, supported: bool, supportedGrains: [...]}",
      "states": [
        "ANALYZING",
        "NEEDS_CLARIFICATION",
        "DEFINITION_DRAFT_READY",
        "WAITING_USER_CONFIRM",
        "USER_EDITING_TEMPLATE",
        "VALIDATION_FAILED",
        "SAVE_DEFINITION_DRAFT",
        "VALIDATING",
        "PREVIEWING",
        "PREVIEW_READY",
        "RENDERING",
        "READY",
        "INTERRUPT_CONFIRMING",
        "BLOCKED_OR_UNSUPPORTED"
      ],
      "readOnly": true,
      "sideEffects": "只读：不改草稿、不写业务数据。",
      "errors": [
        "数据源不存在或越权 → supported=false 且不返回任何能力细节（不泄漏越权信息）"
      ],
      "limits": [
        "一次只核对一个数据源的一个粒度",
        "grainUnit 取值域：MINUTE、HOUR、DAY、WEEK、MONTH"
      ],
      "code": {
        "registration": "app/tools/registry.py::_TOOLS",
        "dispatch": "app/tools/check_capability_tool.py::run",
        "validation": "app/tools/check_capability_tool.py::PARAMETERS",
        "execution": "app/services/catalog_mock_service.py::get_catalog"
      }
    },
    {
      "id": "get_current_draft",
      "group": "工具 / 事实查询（只读）",
      "entryType": "agent_tool",
      "status": "available",
      "purpose": "读取当前对话的报表草稿概览：报表名、版本、包含哪些 region（区块）及各自类型，以及这份草稿还差什么才能往下走。事实来自服务端会话记录：返回的 status 是事实状态，ABSENT 表示当前对话确实没有草稿，UNAVAILABLE 表示暂时无法确认（读不到），两者不能混为一谈。用户在说'把趋势图改成 7 天''删掉某个卡片'时，先调用它确认目标区块是否存在，再决定改哪个 region。",
      "params": {
        "type": "object",
        "properties": {}
      },
      "required": [],
      "targetSpec": "无参数。",
      "resultSpec": "当前草稿定义：regions（含正文卡片 id 清单）、可用共享主题 availableThemes、统计口径与时间范围等",
      "states": [
        "ANALYZING",
        "NEEDS_CLARIFICATION",
        "DEFINITION_DRAFT_READY",
        "WAITING_USER_CONFIRM",
        "USER_EDITING_TEMPLATE",
        "VALIDATION_FAILED",
        "SAVE_DEFINITION_DRAFT",
        "VALIDATING",
        "PREVIEWING",
        "PREVIEW_READY",
        "RENDERING",
        "READY",
        "INTERRUPT_CONFIRMING",
        "BLOCKED_OR_UNSUPPORTED"
      ],
      "readOnly": true,
      "sideEffects": "只读：不改草稿、不写业务数据。",
      "errors": [
        "当前没有草稿 → 返回 found=false 与原因，不编造草稿内容"
      ],
      "limits": [
        "只读当前会话的草稿，不跨会话"
      ],
      "code": {
        "registration": "app/tools/registry.py::_TOOLS",
        "dispatch": "app/tools/get_current_draft_tool.py::run",
        "validation": "app/tools/get_current_draft_tool.py::PARAMETERS",
        "execution": "app/services/session_fact_service.py::SessionFactService.load"
      }
    },
    {
      "id": "get_current_region",
      "group": "工具 / 事实查询（只读）",
      "entryType": "agent_tool",
      "status": "available",
      "purpose": "读取当前草稿中某个 region（区块）的完整内容，包含数据源、指标、时间范围、粒度和展示方式。用户要修改某个具体区块时，先用 get_current_draft 找到 regionId，再用本工具看细节。",
      "params": {
        "type": "object",
        "properties": {
          "regionId": {
            "type": "string",
            "description": "目标区块 ID，先从 get_current_draft 拿到"
          }
        },
        "required": [
          "regionId"
        ]
      },
      "required": [
        "regionId"
      ],
      "targetSpec": "regionId 必须来自 get_current_draft 返回的区块 id（不按标题猜）。",
      "resultSpec": "{found: bool, region: 区块详情}；找不到时附 availableRegionIds",
      "states": [
        "DEFINITION_DRAFT_READY",
        "WAITING_USER_CONFIRM",
        "USER_EDITING_TEMPLATE",
        "VALIDATION_FAILED"
      ],
      "readOnly": true,
      "sideEffects": "只读：不改草稿、不写业务数据。",
      "errors": [
        "没有草稿上下文 → found=false + 原因",
        "regionId 不存在 → found=false + 可用区块 id 清单"
      ],
      "limits": [
        "一次只读一个区块"
      ],
      "code": {
        "registration": "app/tools/registry.py::_TOOLS",
        "dispatch": "app/tools/get_current_region_tool.py::run",
        "validation": "app/tools/get_current_region_tool.py::PARAMETERS",
        "execution": "app/tools/context.py::current_tool_context"
      }
    },
    {
      "id": "get_current_session_status",
      "group": "工具 / 事实查询（只读）",
      "entryType": "agent_tool",
      "status": "available",
      "purpose": "读取当前对话服务端已记录的业务阶段和待确认事项：businessStage（已记录的阶段，不是实时任务状态）、pendingEditPlan（待确认修改计划）、pendingChoice（必须选择的菜单）、savedDefinition（已保存的定义）、preview（已生成的报表预览）、clarificationRounds（同一项修改已追问的轮数）。返回的 status 是事实状态：ABSENT 表示当前对话确实没有这些内容，UNAVAILABLE 表示暂时无法确认。不要把读不到说成没有，也不要据此编造实时进度、百分比或预计完成时间。",
      "params": {
        "type": "object",
        "properties": {}
      },
      "required": [],
      "targetSpec": "无参数。",
      "resultSpec": "{status, scope, sessionExists, businessStage, hasDraft, reportName, draftRevision, pendingSteps, pendingEditPlan, pendingChoice, savedDefinition, preview, clarificationRounds, source, reason}",
      "states": [
        "ANALYZING",
        "NEEDS_CLARIFICATION",
        "DEFINITION_DRAFT_READY",
        "WAITING_USER_CONFIRM",
        "USER_EDITING_TEMPLATE",
        "VALIDATION_FAILED",
        "SAVE_DEFINITION_DRAFT",
        "VALIDATING",
        "PREVIEWING",
        "PREVIEW_READY",
        "RENDERING",
        "READY",
        "INTERRUPT_CONFIRMING",
        "BLOCKED_OR_UNSUPPORTED"
      ],
      "readOnly": true,
      "sideEffects": "只读：不改草稿、不写业务数据。",
      "errors": [
        "会话记录不存在 → 返回 status 与 reason，不报错"
      ],
      "limits": [
        "只读当前会话"
      ],
      "code": {
        "registration": "app/tools/registry.py::_TOOLS",
        "dispatch": "app/tools/get_current_session_status_tool.py::run",
        "validation": "app/tools/get_current_session_status_tool.py::PARAMETERS",
        "execution": "app/services/session_fact_service.py::SessionFactService.load"
      }
    },
    {
      "id": "get_validation_result",
      "group": "工具 / 事实查询（只读）",
      "entryType": "agent_tool",
      "status": "available",
      "purpose": "读取当前草稿的校验结果，返回每条问题的 code、所在路径、原因和修改建议。校验没过时要修复草稿，先调用它拿到真实问题，再据此提出修改方案，不要凭空猜测哪里有问题。",
      "params": {
        "type": "object",
        "properties": {}
      },
      "required": [],
      "targetSpec": "无参数。",
      "resultSpec": "{available: bool, passed: bool, issues: 问题[], total, truncated}",
      "states": [
        "VALIDATION_FAILED"
      ],
      "readOnly": true,
      "sideEffects": "只读：不改草稿、不写业务数据。",
      "errors": [
        "当前没有校验上下文 → available=false + 原因",
        "还没有跑过校验 → available=false + 原因"
      ],
      "limits": [
        "只在 VALIDATION_FAILED 节点可用"
      ],
      "code": {
        "registration": "app/tools/registry.py::_TOOLS",
        "dispatch": "app/tools/get_validation_result_tool.py::run",
        "validation": "app/tools/get_validation_result_tool.py::PARAMETERS",
        "execution": "app/tools/context.py::current_tool_context"
      }
    },
    {
      "id": "get_recorded_progress",
      "group": "工具 / 事实查询（只读）",
      "entryType": "agent_tool",
      "status": "available",
      "purpose": "读取当前对话已记录的业务进度：已记录阶段、报表草稿（名称与版本）、待确认修改计划、已保存定义、报表预览，以及可确定的下一步。用户问「刚才那份做到哪了」「现在什么状态」「进度如何」时调用。结果只反映服务端已记录的阶段，不是实时任务监控：没有百分比，也没有预计完成时间。查进度不需要也不能调用保存、校验或预览。",
      "params": {
        "type": "object",
        "properties": {}
      },
      "required": [],
      "targetSpec": "无参数；会话 id 取自请求级上下文。",
      "resultSpec": "{status: FOUND/ABSENT/UNAVAILABLE/FORBIDDEN, ...}（四态事实状态）",
      "states": [
        "ANALYZING",
        "NEEDS_CLARIFICATION",
        "DEFINITION_DRAFT_READY",
        "WAITING_USER_CONFIRM",
        "USER_EDITING_TEMPLATE",
        "VALIDATION_FAILED",
        "SAVE_DEFINITION_DRAFT",
        "VALIDATING",
        "PREVIEWING",
        "PREVIEW_READY",
        "RENDERING",
        "READY",
        "INTERRUPT_CONFIRMING",
        "BLOCKED_OR_UNSUPPORTED"
      ],
      "readOnly": true,
      "sideEffects": "只读：不改草稿、不写业务数据。",
      "errors": [
        "上下文缺失 → UNAVAILABLE；越权 → FORBIDDEN，不返回细节"
      ],
      "limits": [
        "只读当前会话的已记录进度"
      ],
      "code": {
        "registration": "app/tools/registry.py::_TOOLS",
        "dispatch": "app/tools/get_recorded_progress_tool.py::run",
        "validation": "app/tools/get_recorded_progress_tool.py::PARAMETERS",
        "execution": "app/services/recorded_progress_service.py::describe_recorded_progress"
      }
    },
    {
      "id": "check_product_capability",
      "group": "工具 / 事实查询（只读）",
      "entryType": "agent_tool",
      "status": "available",
      "purpose": "核对一项报表能力在当前产品链路的真实边界：目录是否登记、生成/编辑/预览链路是否真的支持、预览数值来自模拟业务数据还是已接通生产取数。用户问「支持分钟趋势吗」「能耗成本或 PUE 能出吗」「这个源能做趋势和排名吗」时用它。只回答能力边界，不创建、不修改任何报表。返回 outcome 区分：OK（支持）、REGISTERED_NOT_SUPPORTED（目录登记但链路不支持）、NOT_REGISTERED（授权目录里没有）、NOT_AUTHORIZED（无权限）、MISSING_CONTEXT（上下文不足，已给产品范围）。",
      "params": {
        "type": "object",
        "properties": {
          "sourceRef": {
            "type": "string",
            "description": "可选，数据源 ID，如 catalog.machine_room.energy 或 eas_energy_detail"
          },
          "metricRef": {
            "type": "string",
            "description": "可选，指标 ID，如 metric.machine_room.total_energy"
          },
          "grainUnit": {
            "type": "string",
            "enum": [
              "MINUTE",
              "HOUR",
              "DAY",
              "WEEK",
              "MONTH"
            ],
            "description": "可选，时间粒度"
          },
          "component": {
            "type": "string",
            "enum": [
              "total",
              "ranking",
              "trend",
              "quality",
              "analysis"
            ],
            "description": "可选，报表区块"
          }
        }
      },
      "required": [],
      "targetSpec": "sourceRef / metricRef / grainUnit / component 均可空，按传入项核对。",
      "resultSpec": "{status: 四态事实状态, outcome: 领域结论, ...}",
      "states": [
        "ANALYZING",
        "NEEDS_CLARIFICATION",
        "DEFINITION_DRAFT_READY",
        "WAITING_USER_CONFIRM",
        "USER_EDITING_TEMPLATE",
        "VALIDATION_FAILED",
        "SAVE_DEFINITION_DRAFT",
        "VALIDATING",
        "PREVIEWING",
        "PREVIEW_READY",
        "RENDERING",
        "READY",
        "INTERRUPT_CONFIRMING",
        "BLOCKED_OR_UNSUPPORTED"
      ],
      "readOnly": true,
      "sideEffects": "只读：不改草稿、不写业务数据。",
      "errors": [
        "授权范围缺失 → MISSING_CONTEXT（事实查到，限制在别处披露）",
        "未登记 → NOT_REGISTERED（折算为 ABSENT）；越权 → NOT_AUTHORIZED（折算为 FORBIDDEN）"
      ],
      "limits": [
        "只核对能力边界，不执行修改",
        "grainUnit 取值域：MINUTE、HOUR、DAY、WEEK、MONTH",
        "component 取值域：total、ranking、trend、quality、analysis"
      ],
      "code": {
        "registration": "app/tools/registry.py::_TOOLS",
        "dispatch": "app/tools/check_product_capability_tool.py::run",
        "validation": "app/tools/check_product_capability_tool.py::PARAMETERS",
        "execution": "app/services/product_capability_service.py::describe_capability"
      }
    },
    {
      "id": "list_simulated_dimensions",
      "group": "工具 / 事实查询（只读）",
      "entryType": "agent_tool",
      "status": "available",
      "purpose": "列出当前可用的**模拟分类维度**（如告警等级、告警区域、告警类型），每个维度有哪些分类、各是多少模拟数值。新增分类统计图表（饼图 / 柱状图）时先用它确认维度名，再把 dimension 填进新增图表的参数里。同时列出当前可绑定的**模拟单值字段**（指标卡 / 指标组 / 文本占位符用）与**注册图标**（指标图标用）—— 先查清单再填 fieldRef / iconRef，不要凭名字编字段或图标。这些数值都是**模拟**的，不接真实告警源。",
      "params": {
        "type": "object",
        "properties": {}
      },
      "required": [],
      "targetSpec": "无参数。",
      "resultSpec": "{status, dimensions: 维度[], payloadContract: 载荷契约, note}",
      "states": [
        "WAITING_USER_CONFIRM",
        "USER_EDITING_TEMPLATE"
      ],
      "readOnly": true,
      "sideEffects": "只读：不改草稿、不写业务数据。",
      "errors": [
        "维度目录不可用时抛错，由统一注册层转成 error 结果"
      ],
      "limits": [
        "维度只有三个（alertLevel / alertRegion / alertType）",
        "数值均为**模拟**数据，载荷带 simulated 标记，不接真实告警源"
      ],
      "code": {
        "registration": "app/tools/registry.py::_TOOLS",
        "dispatch": "app/tools/list_simulated_dimensions_tool.py::run",
        "validation": "app/tools/list_simulated_dimensions_tool.py::PARAMETERS",
        "execution": "app/services/edit_capabilities/chart_data.py::dimension_catalog"
      }
    },
    {
      "id": "search_edit_experience",
      "group": "工具 / 事实查询（只读）",
      "entryType": "agent_tool",
      "status": "available",
      "purpose": "检索已批准且未禁用的编辑经验（方法 + 适用条件）。",
      "params": {
        "type": "object",
        "properties": {
          "query": {
            "type": "string",
            "description": "用户本次修改要求的关键词"
          },
          "maxResults": {
            "type": "integer",
            "description": "最多返回几条，默认 3，上限 3"
          }
        },
        "required": [
          "query"
        ]
      },
      "required": [
        "query"
      ],
      "targetSpec": "见参数契约。",
      "resultSpec": "事实查询结果；具体字段见该工具的实现。",
      "states": [
        "USER_EDITING_TEMPLATE",
        "WAITING_USER_CONFIRM"
      ],
      "readOnly": true,
      "sideEffects": "只读：不改草稿、不写业务数据。",
      "errors": [
        "没有授权范围时直接报错，不返回空结果冒充「查不到」",
        "工具异常会被统一注册层截断成 error 结果返回"
      ],
      "limits": [
        "结果条数受 observation_max_items 约束"
      ],
      "code": {
        "registration": "app/tools/registry.py::_TOOLS",
        "dispatch": "app/tools/search_edit_experience_tool.py::run",
        "validation": "app/tools/search_edit_experience_tool.py::PARAMETERS",
        "execution": "app/services/edit_experience_service.py::search_experiences"
      }
    },
    {
      "id": "build_definition_draft",
      "group": "工具 / 确定性组装",
      "entryType": "agent_tool",
      "status": "available",
      "purpose": "根据报表计划组装完整的 ReportPolicyDraft 和 ReportDefinitionDraft。Agent 必须通过本工具生成草稿，禁止自行拼接协议结构。用户明确指定的数据源、粒度、时间窗、过滤、TopN、派生列必须通过参数传进来，不要依赖默认值；条件查不到或不支持时本工具会报错，此时应改条件或明确告知用户不支持。",
      "params": {
        "type": "object",
        "properties": {
          "reportName": {
            "type": "string",
            "description": "报表名称"
          },
          "roomId": {
            "type": "string",
            "description": "机房 ID，如 room_a"
          },
          "includeTrend": {
            "type": "boolean",
            "description": "是否包含小时趋势图",
            "default": true
          },
          "sourceRef": {
            "type": "string",
            "description": "数据源 ID，如 catalog.machine_room.energy 或 eas_energy_detail。用户点名了数据源就必须原样传入。"
          },
          "grainUnit": {
            "type": "string",
            "enum": [
              "MINUTE",
              "HOUR",
              "DAY",
              "WEEK",
              "MONTH"
            ],
            "description": "时间粒度。用户说“分钟/小时/每天”时必须传。"
          },
          "timeWindow": {
            "type": "object",
            "description": "相对时间窗，形如 {\"unit\":\"DAY\",\"count\":7,\"offset\":-7} 表示过去 7 个完整自然日；offset 省略时按 -count 推导。",
            "properties": {
              "unit": {
                "type": "string",
                "enum": [
                  "MINUTE",
                  "HOUR",
                  "DAY",
                  "WEEK",
                  "MONTH"
                ]
              },
              "count": {
                "type": "integer",
                "minimum": 1
              },
              "offset": {
                "type": "integer"
              },
              "timezone": {
                "type": "string"
              }
            }
          },
          "metricRefs": {
            "type": "array",
            "items": {
              "type": "string"
            },
            "description": "指标引用列表，如 metric.machine_room.total_energy"
          },
          "componentRequests": {
            "type": "array",
            "items": {
              "type": "string"
            },
            "description": "要生成的区块：total / ranking / trend / quality / analysis。用户明确说“只要某几块”时按白名单传；可写成 \"trend:DAY\" 指定粒度。"
          },
          "excludedRegions": {
            "type": "array",
            "items": {
              "type": "string"
            },
            "description": "用户明确不要的区块，例如 ranking / trend / analysis"
          },
          "filters": {
            "type": "array",
            "description": "过滤条件，形如 [{\"fieldRef\":\"deviceName\",\"op\":\"IN\",\"values\":[\"CRAC-01 空调\"]}]",
            "items": {
              "type": "object",
              "properties": {
                "fieldRef": {
                  "type": "string"
                },
                "op": {
                  "type": "string",
                  "enum": [
                    "IN",
                    "NOT_IN",
                    "EQ",
                    "NE",
                    "GT",
                    "LT"
                  ]
                },
                "values": {
                  "type": "array",
                  "items": {
                    "type": "string"
                  }
                }
              },
              "required": [
                "fieldRef"
              ]
            }
          },
          "limit": {
            "type": "integer",
            "description": "排名显示行数（如 Top3 传 3）"
          },
          "topN": {
            "type": "integer",
            "description": "排名显示行数（与 limit 等价）"
          },
          "derivedColumns": {
            "type": "array",
            "description": "派生列。kind 取 PREVIOUS_PERIOD（前一日值）/ GROWTH_RATE（增长率）/ SHARE_OF_TOTAL（占机房总耗电比例）",
            "items": {
              "type": "object",
              "properties": {
                "id": {
                  "type": "string"
                },
                "label": {
                  "type": "string"
                },
                "fieldRef": {
                  "type": "string"
                },
                "kind": {
                  "type": "string",
                  "enum": [
                    "PREVIOUS_PERIOD",
                    "GROWTH_RATE",
                    "SHARE_OF_TOTAL"
                  ]
                },
                "sourceFieldRef": {
                  "type": "string"
                },
                "format": {
                  "type": "object"
                }
              },
              "required": [
                "kind"
              ]
            }
          },
          "styleHints": {
            "type": "array",
            "description": "条件样式，形如 [{\"targetRegionId\":\"region_device_ranking\",\"fieldRef\":\"growthRatePercent\",\"comparison\":\"GT\",\"threshold\":20,\"style\":\"RED\"}]",
            "items": {
              "type": "object",
              "properties": {
                "targetRegionId": {
                  "type": "string"
                },
                "fieldRef": {
                  "type": "string"
                },
                "comparison": {
                  "type": "string",
                  "enum": [
                    "GT",
                    "GTE",
                    "LT",
                    "LTE",
                    "EQ"
                  ]
                },
                "threshold": {
                  "type": "number"
                },
                "style": {
                  "type": "string"
                }
              },
              "required": [
                "fieldRef"
              ]
            }
          },
          "capabilityEvidence": {
            "type": "object",
            "description": "模型查证到的能力证据（如 check_capability 的返回），原样留档"
          }
        },
        "required": [
          "reportName",
          "roomId"
        ]
      },
      "required": [
        "reportName",
        "roomId"
      ],
      "targetSpec": "按草稿计划的载荷组装。",
      "resultSpec": "组装出的草稿定义与 ChangeSet（服务端现场，不改正式草稿）。",
      "states": [
        "ANALYZING",
        "NEEDS_CLARIFICATION"
      ],
      "readOnly": true,
      "sideEffects": "在服务端现场组装草稿，不直接改正式草稿。",
      "errors": [
        "组装失败（口径不兼容等）→ 抛错，由统一注册层转成 error 结果"
      ],
      "limits": [
        "只在 ANALYZING / NEEDS_CLARIFICATION 节点可用",
        "grainUnit 取值域：MINUTE、HOUR、DAY、WEEK、MONTH"
      ],
      "code": {
        "registration": "app/tools/registry.py::_TOOLS",
        "dispatch": "app/tools/build_definition_draft_tool.py::run",
        "validation": "app/tools/build_definition_draft_tool.py::PARAMETERS",
        "execution": "app/services/report_draft_service.py::build_policy_draft"
      }
    },
    {
      "id": "submit_draft_plan",
      "group": "工具 / 终结提交",
      "entryType": "agent_tool",
      "status": "available",
      "purpose": "提交本轮生成的报表计划，这是本轮的最终结论，调用后不要再调用其他工具。reportName 是报表名，roomId 是机房 ID，includeTrend 表示是否包含趋势图。确认数据源、指标、粒度都查得到之后再提交。用户明确说过的条件必须一并提交，不要只报名字：sourceRef（数据源）、grainUnit（粒度）、timeWindow（时间窗）、metricRefs（指标）、componentRequests（要哪些区块）、excludedRegions（不要哪些区块）、filters（过滤）、topN（排名行数）、derivedColumns（派生列）、styleHints（条件样式）。",
      "params": {
        "$defs": {
          "DerivedColumnSpec": {
            "description": "一个派生列（上一周期值 / 增长率 / 占比等）。",
            "properties": {
              "id": {
                "default": "",
                "title": "Id",
                "type": "string"
              },
              "label": {
                "default": "",
                "title": "Label",
                "type": "string"
              },
              "fieldRef": {
                "default": "",
                "title": "Fieldref",
                "type": "string"
              },
              "kind": {
                "default": "",
                "title": "Kind",
                "type": "string"
              },
              "sourceFieldRef": {
                "default": "energyKwh",
                "title": "Sourcefieldref",
                "type": "string"
              },
              "format": {
                "additionalProperties": true,
                "title": "Format",
                "type": "object"
              }
            },
            "title": "DerivedColumnSpec",
            "type": "object"
          },
          "FilterSpec": {
            "description": "一条过滤条件。",
            "properties": {
              "fieldRef": {
                "default": "",
                "title": "Fieldref",
                "type": "string"
              },
              "op": {
                "default": "IN",
                "title": "Op",
                "type": "string"
              },
              "values": {
                "items": {
                  "type": "string"
                },
                "title": "Values",
                "type": "array"
              }
            },
            "title": "FilterSpec",
            "type": "object"
          },
          "StyleHintSpec": {
            "description": "条件样式：例如\"日增长率严格超过 20% 的行标红\"。",
            "properties": {
              "targetRegionId": {
                "default": "",
                "title": "Targetregionid",
                "type": "string"
              },
              "rule": {
                "default": "HIGHLIGHT_ABOVE_THRESHOLD",
                "title": "Rule",
                "type": "string"
              },
              "fieldRef": {
                "default": "",
                "title": "Fieldref",
                "type": "string"
              },
              "comparison": {
                "default": "GT",
                "title": "Comparison",
                "type": "string"
              },
              "threshold": {
                "default": 0.0,
                "title": "Threshold",
                "type": "number"
              },
              "style": {
                "default": "RED",
                "title": "Style",
                "type": "string"
              }
            },
            "title": "StyleHintSpec",
            "type": "object"
          }
        },
        "description": "节点 1：生成报表的计划。\n\n旧字段（reportName / roomId / includeTrend）保持不变；\n新字段全部可选，用于把路由层已经解析出来的结构化条件\n（数据源 / 粒度 / 时间窗 / 指标 / 过滤 / TopN / 派生列 / 排除区域）\n完整地带进确定性组装层，不再被默认值覆盖。",
        "properties": {
          "reportName": {
            "title": "Reportname",
            "type": "string"
          },
          "roomId": {
            "title": "Roomid",
            "type": "string"
          },
          "includeTrend": {
            "default": true,
            "title": "Includetrend",
            "type": "boolean"
          },
          "reply": {
            "default": "",
            "title": "Reply",
            "type": "string"
          },
          "sourceRef": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Sourceref"
          },
          "grainUnit": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Grainunit"
          },
          "timeWindow": {
            "anyOf": [
              {
                "additionalProperties": true,
                "type": "object"
              },
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Timewindow"
          },
          "metricRefs": {
            "items": {
              "type": "string"
            },
            "title": "Metricrefs",
            "type": "array"
          },
          "componentRequests": {
            "items": {
              "type": "string"
            },
            "title": "Componentrequests",
            "type": "array"
          },
          "excludedRegions": {
            "items": {
              "type": "string"
            },
            "title": "Excludedregions",
            "type": "array"
          },
          "filters": {
            "items": {
              "$ref": "#/$defs/FilterSpec"
            },
            "title": "Filters",
            "type": "array"
          },
          "limit": {
            "anyOf": [
              {
                "type": "integer"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Limit"
          },
          "topN": {
            "anyOf": [
              {
                "type": "integer"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Topn"
          },
          "derivedColumns": {
            "items": {
              "$ref": "#/$defs/DerivedColumnSpec"
            },
            "title": "Derivedcolumns",
            "type": "array"
          },
          "styleHints": {
            "items": {
              "$ref": "#/$defs/StyleHintSpec"
            },
            "title": "Stylehints",
            "type": "array"
          },
          "capabilityEvidence": {
            "additionalProperties": true,
            "title": "Capabilityevidence",
            "type": "object"
          }
        },
        "required": [
          "reportName",
          "roomId"
        ],
        "title": "DraftPlanPayload",
        "type": "object"
      },
      "required": [
        "reportName",
        "roomId"
      ],
      "targetSpec": "无（面向用户的生成计划）。",
      "resultSpec": "{accepted: true, tool: 'submit_draft_plan'}",
      "states": [
        "ANALYZING",
        "NEEDS_CLARIFICATION"
      ],
      "readOnly": false,
      "sideEffects": "写本轮**提交槽**（会话内），本轮结论由业务 Agent 取走；草稿的正式改动由用户接受候选后的事务落库。",
      "errors": [
        "载荷不合法 → {accepted: false, error: '载荷不合法：...'}"
      ],
      "limits": [
        "终结工具：一轮只认第一次提交"
      ],
      "code": {
        "registration": "app/tools/registry.py::_TOOLS",
        "dispatch": "app/tools/submit_draft_plan_tool.py::run",
        "validation": "app/tools/schemas.py::DraftPlanPayload",
        "execution": "app/tools/submission.py::submit_payload"
      }
    },
    {
      "id": "submit_clarification",
      "group": "工具 / 终结提交",
      "entryType": "agent_tool",
      "status": "available",
      "purpose": "提交需要向用户追问的信息，这是本轮的最终结论，调用后不要再调用其他工具。missingInfo 列出缺哪些信息（expects 指明是文本、单选、多选还是日期范围），nextQuestion 是要问用户的具体问题，能自动回填的放进 autoFillable。",
      "params": {
        "$defs": {
          "ClarificationField": {
            "description": "需要补的一个信息项。",
            "properties": {
              "field": {
                "title": "Field",
                "type": "string"
              },
              "label": {
                "default": "",
                "title": "Label",
                "type": "string"
              },
              "expects": {
                "default": "TEXT",
                "title": "Expects",
                "type": "string"
              },
              "options": {
                "items": {
                  "$ref": "#/$defs/QuestionOption"
                },
                "title": "Options",
                "type": "array"
              },
              "required": {
                "default": true,
                "title": "Required",
                "type": "boolean"
              }
            },
            "required": [
              "field"
            ],
            "title": "ClarificationField",
            "type": "object"
          },
          "QuestionOption": {
            "properties": {
              "value": {
                "title": "Value",
                "type": "string"
              },
              "label": {
                "title": "Label",
                "type": "string"
              }
            },
            "required": [
              "value",
              "label"
            ],
            "title": "QuestionOption",
            "type": "object"
          }
        },
        "description": "节点 2：缺信息，需要追问。",
        "properties": {
          "missingInfo": {
            "items": {
              "$ref": "#/$defs/ClarificationField"
            },
            "title": "Missinginfo",
            "type": "array"
          },
          "nextQuestion": {
            "default": "",
            "title": "Nextquestion",
            "type": "string"
          },
          "autoFillable": {
            "anyOf": [
              {
                "additionalProperties": true,
                "type": "object"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Autofillable"
          },
          "reply": {
            "default": "",
            "title": "Reply",
            "type": "string"
          }
        },
        "title": "ClarificationPayload",
        "type": "object"
      },
      "required": [],
      "targetSpec": "无。",
      "resultSpec": "{accepted: true, tool: 'submit_clarification'}",
      "states": [
        "ANALYZING",
        "NEEDS_CLARIFICATION",
        "WAITING_USER_CONFIRM"
      ],
      "readOnly": false,
      "sideEffects": "写本轮**提交槽**（会话内），本轮结论由业务 Agent 取走；草稿的正式改动由用户接受候选后的事务落库。",
      "errors": [
        "载荷不合法 → {accepted: false, error: '载荷不合法：...'}"
      ],
      "limits": [
        "终结工具：一轮只认第一次提交"
      ],
      "code": {
        "registration": "app/tools/registry.py::_TOOLS",
        "dispatch": "app/tools/submit_clarification_tool.py::run",
        "validation": "app/tools/schemas.py::ClarificationPayload",
        "execution": "app/tools/submission.py::submit_payload"
      }
    },
    {
      "id": "submit_blocked",
      "group": "工具 / 终结提交",
      "entryType": "agent_tool",
      "status": "available",
      "purpose": "提交无法处理的结论（能力不支持 / 权限越界 / 诉求超出范围），这是本轮的最终结论，调用后不要再调用其他工具。message 说清原因，alternatives 给出用户可选的替代方案。",
      "params": {
        "description": "节点 3：能力不支持 / 权限越界 / 无法处理。",
        "properties": {
          "layer": {
            "default": "CAPABILITY",
            "title": "Layer",
            "type": "string"
          },
          "code": {
            "default": "",
            "title": "Code",
            "type": "string"
          },
          "message": {
            "default": "",
            "title": "Message",
            "type": "string"
          },
          "alternatives": {
            "items": {
              "type": "string"
            },
            "title": "Alternatives",
            "type": "array"
          },
          "reply": {
            "default": "",
            "title": "Reply",
            "type": "string"
          }
        },
        "title": "BlockedPayload",
        "type": "object"
      },
      "required": [],
      "targetSpec": "无。",
      "resultSpec": "{accepted: true, tool: 'submit_blocked'}",
      "states": [
        "ANALYZING",
        "WAITING_USER_CONFIRM"
      ],
      "readOnly": false,
      "sideEffects": "写本轮**提交槽**（会话内），本轮结论由业务 Agent 取走；草稿的正式改动由用户接受候选后的事务落库。",
      "errors": [
        "载荷不合法 → {accepted: false, error: '载荷不合法：...'}"
      ],
      "limits": [
        "终结工具：代表本轮判定为不支持 / 被拦截"
      ],
      "code": {
        "registration": "app/tools/registry.py::_TOOLS",
        "dispatch": "app/tools/submit_blocked_tool.py::run",
        "validation": "app/tools/schemas.py::BlockedPayload",
        "execution": "app/tools/submission.py::submit_payload"
      }
    },
    {
      "id": "submit_change_plan",
      "group": "工具 / 终结提交",
      "entryType": "agent_tool",
      "status": "available",
      "purpose": "提交对当前草稿的修改方案，这是本轮的最终结论，调用后不要再调用其他工具。每处修改给出 regionId（改哪个区块）、path（改哪个属性）、after（改成什么）和 summary（为什么这么改）。",
      "params": {
        "$defs": {
          "ChangeProposal": {
            "description": "一处修改提议。",
            "properties": {
              "regionId": {
                "title": "Regionid",
                "type": "string"
              },
              "path": {
                "title": "Path",
                "type": "string"
              },
              "after": {
                "default": null,
                "title": "After"
              },
              "summary": {
                "default": "",
                "title": "Summary",
                "type": "string"
              }
            },
            "required": [
              "regionId",
              "path"
            ],
            "title": "ChangeProposal",
            "type": "object"
          }
        },
        "description": "节点 4：对已有草稿的修改方案。",
        "properties": {
          "baseDefinitionRevision": {
            "default": 0,
            "title": "Basedefinitionrevision",
            "type": "integer"
          },
          "changes": {
            "items": {
              "$ref": "#/$defs/ChangeProposal"
            },
            "title": "Changes",
            "type": "array"
          },
          "reply": {
            "default": "",
            "title": "Reply",
            "type": "string"
          }
        },
        "title": "ChangePlanPayload",
        "type": "object"
      },
      "required": [],
      "targetSpec": "修改方案的目标与操作。",
      "resultSpec": "{accepted: true, tool: 'submit_change_plan'}",
      "states": [
        "ANALYZING",
        "DEFINITION_DRAFT_READY",
        "WAITING_USER_CONFIRM"
      ],
      "readOnly": false,
      "sideEffects": "写本轮**提交槽**（会话内），本轮结论由业务 Agent 取走；草稿的正式改动由用户接受候选后的事务落库。",
      "errors": [
        "载荷不合法 → {accepted: false, error: '载荷不合法：...'}"
      ],
      "limits": [
        "终结工具：一轮只认第一次提交"
      ],
      "code": {
        "registration": "app/tools/registry.py::_TOOLS",
        "dispatch": "app/tools/submit_change_plan_tool.py::run",
        "validation": "app/tools/schemas.py::ChangePlanPayload",
        "execution": "app/tools/submission.py::submit_payload"
      }
    },
    {
      "id": "submit_edit_answer",
      "group": "工具 / 终结提交",
      "entryType": "agent_tool",
      "status": "available",
      "purpose": "查证当前草稿、授权数据源和能力后，只回答用户的问题。facts 填已核实事实，limitations 填限制，suggestions 只能提出建议；不得提交目标修改、操作或声称草稿已更改。调用后不要再调用工具。",
      "params": {
        "additionalProperties": false,
        "description": "编辑态查询的只回答结论，不承载任何修改操作。",
        "properties": {
          "reply": {
            "minLength": 1,
            "title": "Reply",
            "type": "string"
          },
          "facts": {
            "items": {
              "type": "string"
            },
            "title": "Facts",
            "type": "array"
          },
          "limitations": {
            "items": {
              "type": "string"
            },
            "title": "Limitations",
            "type": "array"
          },
          "suggestions": {
            "items": {
              "type": "string"
            },
            "title": "Suggestions",
            "type": "array"
          }
        },
        "required": [
          "reply"
        ],
        "title": "EditAnswerPayload",
        "type": "object"
      },
      "required": [
        "reply"
      ],
      "targetSpec": "无（只回答，不承载修改操作）。",
      "resultSpec": "{accepted: true, tool: 'submit_edit_answer'}",
      "states": [
        "WAITING_USER_CONFIRM",
        "USER_EDITING_TEMPLATE"
      ],
      "readOnly": false,
      "sideEffects": "写本轮**提交槽**（会话内），本轮结论由业务 Agent 取走；草稿的正式改动由用户接受候选后的事务落库。",
      "errors": [
        "载荷不合法 → {accepted: false, error: '载荷不合法：...'}"
      ],
      "limits": [
        "终结工具：只回答，不创建 EditPlan"
      ],
      "code": {
        "registration": "app/tools/registry.py::_TOOLS",
        "dispatch": "app/tools/submit_edit_answer_tool.py::run",
        "validation": "app/tools/schemas.py::EditAnswerPayload",
        "execution": "app/tools/submission.py::submit_payload"
      }
    },
    {
      "id": "submit_edit_plan",
      "group": "工具 / 终结提交",
      "entryType": "agent_tool",
      "status": "available",
      "purpose": "提交对当前草稿的编辑计划，这是本轮的最终结论。两种提交方式：（1）**能力格式**（推荐）：operations 里每一项形如 {\"op\": \"<能力名>\", \"target\": {\"regionId\" 或 \"ref\"}, \"params\": {...}},服务端会先在草稿副本上**整批试做**；新增区块时在 params.ref 里起一个引用名，后续步骤用 target.ref 指它；试做失败会返回**具体错误**（哪一步、哪个目标、哪条约束、为什么，外加字段路径、收到的类型、要求的类型、该填在哪、最小结构示例），请按反馈改好 operations 后**再次调用本工具**，不要换别的出口；试做通过才形成待确认候选。（2）单步格式：target 指明改哪个区块（regionId），operation 指明怎么改。**能力名、参数、支持范围、分类维度与源码位置见本轮下发的能力总目录**（编辑说明末尾），不要凭记忆猜能力名或参数。needsClarification 为 true 时说明还缺参数并给出 clarificationQuestion。用户表达含糊（比如'改好看点'）时，把候选解释放进 options 让用户选，不要自己替用户决定。包装能力（子操作要放在它的 params 里，不要放在顶层）：**包装能力 SET_PRESENTATION**：它的 params.operations 是**非空列表**，{\"op\": \"<操作名>\", \"params\": {...}}；允许的 op：CREATE_GROUP、SET_GROUP_ARRANGEMENT、MOVE_REGION、UNGROUP、DELETE_GROUP、COPY_REGION、DELETE_REGION、HIDE_REGION、SHOW_REGION、SET_PLACEMENT、SET_STYLE、SET_FORMAT、SET_PAGE、SET_METADATA、SET_THEME。这些 op **不能**直接作为 operations 的顶层项 —— 顶层项是 SET_PRESENTATION 自己，它的子操作写在 params.operations 里。",
      "params": {
        "$defs": {
          "EditIntent": {
            "enum": [
              "RENAME_REPORT",
              "DELETE_SECTION",
              "DELETE_METRIC",
              "RENAME_SECTION",
              "ADD_SECTION",
              "ADD_METRIC",
              "CHANGE_DATA_SOURCE",
              "CHANGE_VISUALIZATION",
              "SUGGEST_VISUALIZATION",
              "CHANGE_COMPILATION_GRAIN",
              "CHANGE_TIME_RANGE",
              "CHANGE_METRIC",
              "CHANGE_TOP_N",
              "CHANGE_SORT",
              "APPLY_PRESENTATION",
              "SAVE_THEME",
              "UNKNOWN"
            ],
            "title": "EditIntent",
            "type": "string"
          },
          "EditMetricAddition": {
            "description": "要**追加**到某个已有区块里的一个指标。\n\n「追加」与「替换」是两种不同的意图：追加**保留**该区块原有指标，\n把新指标加进去；替换才把原来的换掉。\n\n每个待追加指标至少能表达：用户原话意图、显示名、单位、展示模式。",
            "properties": {
              "intent": {
                "default": "",
                "title": "Intent",
                "type": "string"
              },
              "label": {
                "default": "",
                "title": "Label",
                "type": "string"
              },
              "unit": {
                "default": "",
                "title": "Unit",
                "type": "string"
              },
              "displayMode": {
                "default": "simulated",
                "title": "Displaymode",
                "type": "string"
              },
              "metricRef": {
                "anyOf": [
                  {
                    "type": "string"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Metricref"
              },
              "metricCandidate": {
                "anyOf": [
                  {
                    "additionalProperties": true,
                    "type": "object"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Metriccandidate"
              },
              "note": {
                "default": "",
                "title": "Note",
                "type": "string"
              }
            },
            "title": "EditMetricAddition",
            "type": "object"
          },
          "EditMetricDeletion": {
            "description": "要**删除**的一个指标 —— 卡片内的**一个指标实例**。\n\n「删掉某个指标」与「删掉整张卡片」是两件事（契约表 §4.3）：目标必须\n指向卡片内**真实存在**的那一项指标。\n\n身份用**稳定标识**记（``metricKey`` / ``simulatedId`` / ``fieldRef``），\n**不记数组下标** —— 删掉中间一项之后下标会漂移；``label`` 只是给人看\n的显示名，**不作为定位依据**（同名指标要澄清，不能删第一条匹配的）。",
            "properties": {
              "metricKey": {
                "default": "",
                "title": "Metrickey",
                "type": "string"
              },
              "simulatedId": {
                "anyOf": [
                  {
                    "type": "string"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Simulatedid"
              },
              "fieldRef": {
                "anyOf": [
                  {
                    "type": "string"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Fieldref"
              },
              "label": {
                "default": "",
                "title": "Label",
                "type": "string"
              },
              "isPrimary": {
                "default": false,
                "title": "Isprimary",
                "type": "boolean"
              },
              "intent": {
                "default": "",
                "title": "Intent",
                "type": "string"
              }
            },
            "title": "EditMetricDeletion",
            "type": "object"
          },
          "EditOperation": {
            "description": "具体改成什么。字段按 intent 不同用到的子集不同；这里统一松结构承载。",
            "properties": {
              "newTitle": {
                "anyOf": [
                  {
                    "type": "string"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Newtitle"
              },
              "componentType": {
                "anyOf": [
                  {
                    "type": "string"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Componenttype"
              },
              "chartKind": {
                "anyOf": [
                  {
                    "type": "string"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Chartkind"
              },
              "sectionTitle": {
                "anyOf": [
                  {
                    "type": "string"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Sectiontitle"
              },
              "insertAfterRegionId": {
                "anyOf": [
                  {
                    "type": "string"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Insertafterregionid"
              },
              "sourceRef": {
                "anyOf": [
                  {
                    "type": "string"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Sourceref"
              },
              "sourceVersion": {
                "anyOf": [
                  {
                    "type": "string"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Sourceversion"
              },
              "fieldMappingRequired": {
                "anyOf": [
                  {
                    "type": "boolean"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Fieldmappingrequired"
              },
              "metricRef": {
                "anyOf": [
                  {
                    "type": "string"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Metricref"
              },
              "metricVersion": {
                "anyOf": [
                  {
                    "type": "string"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Metricversion"
              },
              "metricIntent": {
                "anyOf": [
                  {
                    "type": "string"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Metricintent"
              },
              "metricCandidate": {
                "anyOf": [
                  {
                    "additionalProperties": true,
                    "type": "object"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Metriccandidate"
              },
              "baseDefinitionRevision": {
                "anyOf": [
                  {
                    "type": "integer"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Basedefinitionrevision"
              },
              "addMetrics": {
                "items": {
                  "$ref": "#/$defs/EditMetricAddition"
                },
                "title": "Addmetrics",
                "type": "array"
              },
              "deleteMetrics": {
                "items": {
                  "$ref": "#/$defs/EditMetricDeletion"
                },
                "title": "Deletemetrics",
                "type": "array"
              },
              "componentSelection": {
                "anyOf": [
                  {
                    "additionalProperties": true,
                    "type": "object"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Componentselection"
              },
              "timeWindow": {
                "anyOf": [
                  {
                    "additionalProperties": true,
                    "type": "object"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Timewindow"
              },
              "timeWindowLabel": {
                "anyOf": [
                  {
                    "type": "string"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Timewindowlabel"
              },
              "grain": {
                "anyOf": [
                  {
                    "additionalProperties": true,
                    "type": "object"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Grain"
              },
              "supported": {
                "anyOf": [
                  {
                    "type": "boolean"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Supported"
              },
              "supportedGrains": {
                "items": {
                  "type": "string"
                },
                "title": "Supportedgrains",
                "type": "array"
              },
              "topN": {
                "anyOf": [
                  {
                    "type": "integer"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Topn"
              },
              "sortField": {
                "anyOf": [
                  {
                    "type": "string"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Sortfield"
              },
              "sortDirection": {
                "anyOf": [
                  {
                    "type": "string"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Sortdirection"
              },
              "dataShape": {
                "anyOf": [
                  {
                    "type": "string"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Datashape"
              },
              "displayOptions": {
                "items": {
                  "additionalProperties": true,
                  "type": "object"
                },
                "title": "Displayoptions",
                "type": "array"
              },
              "disabledOptions": {
                "items": {
                  "additionalProperties": true,
                  "type": "object"
                },
                "title": "Disabledoptions",
                "type": "array"
              },
              "markdownPreview": {
                "anyOf": [
                  {
                    "type": "string"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Markdownpreview"
              },
              "reason": {
                "anyOf": [
                  {
                    "type": "string"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Reason"
              },
              "presentationOperations": {
                "items": {
                  "additionalProperties": true,
                  "type": "object"
                },
                "title": "Presentationoperations",
                "type": "array"
              },
              "themeOperations": {
                "items": {
                  "additionalProperties": true,
                  "type": "object"
                },
                "title": "Themeoperations",
                "type": "array"
              }
            },
            "title": "EditOperation",
            "type": "object"
          },
          "EditOperationStep": {
            "description": "组合修改中的一步：一次原子修改里的一个确定性动作。\n\n单项编辑不需要它；只有「换源 + 改粒度」这类组合计划才用到。\n\n两种形态：\n\n- **旧形态**（``intent`` + ``operation``）：既有编辑引擎的组合步骤，\n  所有步骤共用**同一个**顶层 target。\n- **新形态**（``op`` + ``target`` + ``params``）：能力目录的操作项，\n  **每一步有自己的目标**（既有区块或本次新建的稳定引用）。",
            "properties": {
              "intent": {
                "$ref": "#/$defs/EditIntent",
                "default": "UNKNOWN"
              },
              "operation": {
                "$ref": "#/$defs/EditOperation"
              },
              "summary": {
                "default": "",
                "title": "Summary",
                "type": "string"
              },
              "op": {
                "default": "",
                "title": "Op",
                "type": "string"
              },
              "target": {
                "$ref": "#/$defs/EditOperationTarget"
              },
              "params": {
                "additionalProperties": true,
                "title": "Params",
                "type": "object"
              }
            },
            "title": "EditOperationStep",
            "type": "object"
          },
          "EditOperationTarget": {
            "description": "一步操作的**目标**。\n\n两种写法，指向的都是**这一次修改现场**里的区块：\n\n- ``regionId``：权威草稿里**已有**的区块；\n- ``ref``：本次修改里**新建**的区块的稳定引用名（ADD_SECTION 时给出，\n  后续步骤用它指这一块）。\n\n不按标题猜目标：解析不出来就是错。",
            "properties": {
              "regionId": {
                "default": "",
                "title": "Regionid",
                "type": "string"
              },
              "ref": {
                "default": "",
                "title": "Ref",
                "type": "string"
              }
            },
            "title": "EditOperationTarget",
            "type": "object"
          },
          "EditTarget": {
            "description": "用户想改哪个 section。",
            "properties": {
              "regionId": {
                "default": "",
                "title": "Regionid",
                "type": "string"
              },
              "matchedText": {
                "default": "",
                "title": "Matchedtext",
                "type": "string"
              },
              "confidence": {
                "default": 0.0,
                "title": "Confidence",
                "type": "number"
              }
            },
            "title": "EditTarget",
            "type": "object"
          },
          "QuestionOption": {
            "properties": {
              "value": {
                "title": "Value",
                "type": "string"
              },
              "label": {
                "title": "Label",
                "type": "string"
              }
            },
            "required": [
              "value",
              "label"
            ],
            "title": "QuestionOption",
            "type": "object"
          }
        },
        "description": "节点 5.5：模板编辑计划。\n\n继承现有 ``EditPlan``，只多一个 ``options``：用户表达含糊时\n（例如\"改好看点\"）把候选解释列出来交给用户选，而不是擅自改。",
        "properties": {
          "isTemplateEdit": {
            "default": true,
            "title": "Istemplateedit",
            "type": "boolean"
          },
          "intent": {
            "$ref": "#/$defs/EditIntent",
            "default": "UNKNOWN"
          },
          "confidence": {
            "default": 0.0,
            "title": "Confidence",
            "type": "number"
          },
          "target": {
            "$ref": "#/$defs/EditTarget"
          },
          "operation": {
            "$ref": "#/$defs/EditOperation"
          },
          "needsClarification": {
            "default": false,
            "title": "Needsclarification",
            "type": "boolean"
          },
          "clarificationQuestion": {
            "default": "",
            "title": "Clarificationquestion",
            "type": "string"
          },
          "operations": {
            "items": {
              "$ref": "#/$defs/EditOperationStep"
            },
            "title": "Operations",
            "type": "array",
            "description": "能力格式的操作清单：每一项形如 {\"op\": \"<能力名>\", \"target\": {\"regionId\" 或 \"ref\"}, \"params\": {...}}。**包装能力**（子操作要放在它 params 里的列表字段中）不能把子操作直接写成本列表的顶层项 —— 见本轮能力总目录里每项能力的「提交位置」。"
          },
          "baseCombination": {
            "anyOf": [
              {
                "additionalProperties": true,
                "type": "object"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Basecombination"
          },
          "editFacts": {
            "anyOf": [
              {
                "additionalProperties": true,
                "type": "object"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Editfacts"
          },
          "originalRequest": {
            "default": "",
            "title": "Originalrequest",
            "type": "string"
          },
          "summary": {
            "default": "",
            "title": "Summary",
            "type": "string"
          },
          "options": {
            "items": {
              "$ref": "#/$defs/QuestionOption"
            },
            "title": "Options",
            "type": "array"
          },
          "reply": {
            "default": "",
            "title": "Reply",
            "type": "string"
          }
        },
        "title": "EditPlanPayload",
        "type": "object"
      },
      "required": [],
      "targetSpec": "target.regionId（或 target.ref 指向本次新增区块）；能力格式里每一步的 target 各自独立",
      "resultSpec": "{accepted: true, tool: 'submit_edit_plan', checked, summary, resolvedOperations}；能力格式试做失败 → {accepted: false, error, errors, hint}",
      "states": [
        "WAITING_USER_CONFIRM",
        "USER_EDITING_TEMPLATE"
      ],
      "readOnly": false,
      "sideEffects": "写本轮**提交槽**（会话内），本轮结论由业务 Agent 取走；草稿的正式改动由用户接受候选后的事务落库。",
      "errors": [
        "载荷不合法 → {accepted: false, error: '载荷不合法：...'}",
        "能力格式试做失败 → 返回**具体**错误（哪一步、哪个目标、哪条约束、为什么），草稿不变，按反馈改正 operations 后再次调用",
        "服务端读不到当前草稿 → {accepted: false, error}，草稿不变"
      ],
      "limits": [
        "终结工具：一轮只认第一次提交",
        "能力格式整批试做：任一步失败则整批不生效"
      ],
      "code": {
        "registration": "app/tools/registry.py::_TOOLS",
        "dispatch": "app/tools/submit_edit_plan_tool.py::run",
        "validation": "app/tools/schemas.py::EditPlanPayload",
        "execution": "app/services/edit_trial_service.py::run_trial"
      }
    },
    {
      "id": "submit_fix_plan",
      "group": "工具 / 终结提交",
      "entryType": "agent_tool",
      "status": "available",
      "purpose": "提交校验问题的修复方案，这是本轮的最终结论，调用后不要再调用其他工具。先用 get_validation_result 拿到真实问题，再针对每条问题给出修复动作，不要凭空猜测哪里有问题。",
      "params": {
        "$defs": {
          "FixProposal": {
            "description": "针对一条校验问题的修复动作。",
            "properties": {
              "issueCode": {
                "title": "Issuecode",
                "type": "string"
              },
              "regionId": {
                "anyOf": [
                  {
                    "type": "string"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Regionid"
              },
              "action": {
                "default": "",
                "title": "Action",
                "type": "string"
              },
              "summary": {
                "default": "",
                "title": "Summary",
                "type": "string"
              }
            },
            "required": [
              "issueCode"
            ],
            "title": "FixProposal",
            "type": "object"
          }
        },
        "description": "节点 8：校验失败后的修复方案。",
        "properties": {
          "fixes": {
            "items": {
              "$ref": "#/$defs/FixProposal"
            },
            "title": "Fixes",
            "type": "array"
          },
          "reply": {
            "default": "",
            "title": "Reply",
            "type": "string"
          }
        },
        "title": "FixPlanPayload",
        "type": "object"
      },
      "required": [],
      "targetSpec": "针对校验问题清单的修复动作。",
      "resultSpec": "{accepted: true, tool: 'submit_fix_plan'}",
      "states": [
        "VALIDATION_FAILED"
      ],
      "readOnly": false,
      "sideEffects": "写本轮**提交槽**（会话内），本轮结论由业务 Agent 取走；草稿的正式改动由用户接受候选后的事务落库。",
      "errors": [
        "载荷不合法 → {accepted: false, error: '载荷不合法：...'}"
      ],
      "limits": [
        "终结工具：只在 VALIDATION_FAILED 节点可用"
      ],
      "code": {
        "registration": "app/tools/registry.py::_TOOLS",
        "dispatch": "app/tools/submit_fix_plan_tool.py::run",
        "validation": "app/tools/schemas.py::FixPlanPayload",
        "execution": "app/tools/submission.py::submit_payload"
      }
    },
    {
      "id": "submit_query_answer",
      "group": "工具 / 终结提交",
      "entryType": "agent_tool",
      "status": "available",
      "purpose": "提交对用户问题的只读回答：facts 填已核实的事实，limitations 填限制与无法确认的原因，suggestions 只给建议。回答必须区分报表草稿、待确认修改计划和报表预览，不能把未确认的内容说成已经出数，也不能编造实时进度、百分比或预计完成时间。提交后本轮结束，不会生成、修改、保存、校验或预览任何报表。问题指向的对象不明确时，把 needsClarification 设为 true 并在clarificationQuestion 里问一个具体问题，不要替用户选择操作。",
      "params": {
        "additionalProperties": false,
        "description": "只读查询的答案提交（工单 #13 / #14 / #15 / #16 共用）。\n\n与 ``EditAnswerPayload`` 的区别：它适用于**所有**业务节点，而不只是编辑态；\n它还带一个「需要澄清」的分支，用来在只读提问上下文里做针对性澄清，\n而不是把用户推去补生成条件。",
        "properties": {
          "reply": {
            "minLength": 1,
            "title": "Reply",
            "type": "string"
          },
          "facts": {
            "items": {
              "type": "string"
            },
            "title": "Facts",
            "type": "array"
          },
          "limitations": {
            "items": {
              "type": "string"
            },
            "title": "Limitations",
            "type": "array"
          },
          "suggestions": {
            "items": {
              "type": "string"
            },
            "title": "Suggestions",
            "type": "array"
          },
          "needsClarification": {
            "default": false,
            "title": "Needsclarification",
            "type": "boolean"
          },
          "clarificationQuestion": {
            "default": "",
            "title": "Clarificationquestion",
            "type": "string"
          }
        },
        "required": [
          "reply"
        ],
        "title": "QueryAnswerPayload",
        "type": "object"
      },
      "required": [
        "reply"
      ],
      "targetSpec": "无。",
      "resultSpec": "{accepted: true, tool: 'submit_query_answer'}",
      "states": [
        "ANALYZING",
        "NEEDS_CLARIFICATION",
        "DEFINITION_DRAFT_READY",
        "WAITING_USER_CONFIRM",
        "USER_EDITING_TEMPLATE",
        "VALIDATION_FAILED",
        "SAVE_DEFINITION_DRAFT",
        "VALIDATING",
        "PREVIEWING",
        "PREVIEW_READY",
        "RENDERING",
        "READY",
        "INTERRUPT_CONFIRMING",
        "BLOCKED_OR_UNSUPPORTED"
      ],
      "readOnly": false,
      "sideEffects": "写本轮**提交槽**（会话内），本轮结论由业务 Agent 取走；草稿的正式改动由用户接受候选后的事务落库。",
      "errors": [
        "载荷不合法 → {accepted: false, error: '载荷不合法：...'}"
      ],
      "limits": [
        "终结工具：只在只读查询模式下注册（IS_QUERY_ONLY）",
        "只回答，不推进业务状态（结论状态 SAME）"
      ],
      "code": {
        "registration": "app/tools/registry.py::_TOOLS",
        "dispatch": "app/tools/submit_query_answer_tool.py::run",
        "validation": "app/tools/schemas.py::QueryAnswerPayload",
        "execution": "app/tools/submission.py::submit_payload"
      }
    },
    {
      "id": "ADD_SECTION",
      "group": "清单能力 / 新增与删除区块",
      "entryType": "operation",
      "status": "available",
      "purpose": "在草稿里新增一个区块（图表或文本）。图表必须给 chartKind（LINE / BAR / PIE / AREA）；分类统计图给 dimension（模拟分类维度）或 categories（分类与数值）。要用 ref 给这个新区块起一个稳定引用名，后续步骤用它指这块；replaceRegionId 让新区块插到那一块原来的位置。 （**操作清单里的能力，不是独立 Tool**：模型在 submit_edit_plan 的 operations 里给出 op + target + params，由后端在草稿副本上执行。）**提交位置**：submit_edit_plan 的 operations 里直接给一项 {\"op\": \"<能力名>\", \"target\": {...}, \"params\": {...}}。**包装要求**：{\"op\": \"ADD_SECTION\", \"params\": {\"title\": \"<title>\"}}。",
      "params": {
        "type": "object",
        "description": "ADD_SECTION 的参数对象。",
        "properties": {
          "title": {
            "type": "string",
            "minLength": 1
          },
          "componentType": {
            "type": "string",
            "description": "组件类型（TEXT / METRIC_CARD / TABLE / CHART）",
            "enum": [
              "ANALYSIS",
              "CHART",
              "CONTAINER",
              "DATA_QUALITY",
              "METRIC_CARD",
              "METRIC_GROUP",
              "TABLE",
              "TEXT"
            ],
            "default": "CHART"
          },
          "chartKind": {
            "type": "string",
            "description": "图表类型（componentType=CHART 时必填）",
            "enum": [
              "AREA",
              "BAR",
              "DONUT",
              "LINE",
              "PIE",
              "STACKED_BAR"
            ],
            "default": ""
          },
          "role": {
            "type": "string",
            "default": "DETAIL"
          },
          "dimension": {
            "type": "string",
            "description": "模拟分类维度（key 或业务别名）",
            "enum": [
              "alertLevel",
              "alertRegion",
              "alertType"
            ],
            "default": ""
          },
          "categories": {
            "type": "array",
            "items": {
              "type": "object",
              "properties": {
                "label": {
                  "type": "string",
                  "minLength": 1
                },
                "value": {
                  "type": "number",
                  "default": 0
                }
              },
              "additionalProperties": false
            }
          },
          "unit": {
            "type": "string",
            "default": ""
          },
          "replaceRegionId": {
            "type": "string",
            "default": ""
          },
          "afterRegionId": {
            "type": "string",
            "default": ""
          },
          "beforeRegionId": {
            "type": "string",
            "default": ""
          },
          "ref": {
            "type": "string",
            "default": ""
          }
        },
        "required": [
          "title"
        ]
      },
      "required": [
        "title"
      ],
      "targetSpec": "每一步各自带 target：target.regionId 指向草稿里已有区块，target.ref 指向本次新增区块（ADD_SECTION 的 params.ref 起名）。",
      "resultSpec": "CapabilityOutcome：ok / message / detail（含 regionId、title、chartKind）；试做失败时返回具体错误（哪一步、哪个目标、哪条约束、为什么，外加字段路径、收到的类型、要求的类型、该填在哪、最小示例）",
      "states": [
        "WAITING_USER_CONFIRM",
        "USER_EDITING_TEMPLATE"
      ],
      "readOnly": false,
      "sideEffects": "只改**服务端草稿的隔离副本**；试做通过并由用户接受后，才在正式事务里写入草稿（版本 +1）。",
      "errors": [
        "能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=unsupported_component_type",
        "能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=unknown_dimension",
        "能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=missing_statistic",
        "能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=duplicate_ref",
        "能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=missing_chart_kind",
        "能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=unsupported_chart_kind",
        "能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=unknown_anchor",
        "参数不合法 → reason=invalid_params，不执行",
        "目标引用不认识 → reason=unknown_ref / missing_target / unknown_region"
      ],
      "limits": [
        "不需要目标（自己新建区块 / 整批作用于草稿）",
        "新增的区块可用 params.ref 起稳定引用名，后续步骤用 target.ref 指它",
        "title 至少 1 个字符",
        "componentType 取值域：ANALYSIS、CHART、CONTAINER、DATA_QUALITY、METRIC_CARD、METRIC_GROUP、TABLE、TEXT",
        "chartKind 取值域：AREA、BAR、DONUT、LINE、PIE、STACKED_BAR",
        "dimension 取值域：alertLevel、alertRegion、alertType",
        "提交位置：submit_edit_plan 的 operations 里直接给一项 {\"op\": \"<能力名>\", \"target\": {...}, \"params\": {...}}",
        "包装要求：{\"op\": \"ADD_SECTION\", \"params\": {\"title\": \"<title>\"}}",
        "最小结构示例（只给格式）：{\"op\": \"ADD_SECTION\", \"params\": {\"title\": \"<title>\"}}"
      ],
      "code": {
        "registration": "app/services/edit_capabilities/base.py::register",
        "dispatch": "app/tools/submit_edit_plan_tool.py::run",
        "validation": "app/services/edit_capabilities/base.py::validate_params",
        "execution": "app/services/edit_capabilities/blocks.py::_add_section"
      }
    },
    {
      "id": "DELETE_SECTION",
      "group": "清单能力 / 新增与删除区块",
      "entryType": "operation",
      "status": "available",
      "purpose": "删除草稿里的一个区块。目标用 target.regionId 或 target.ref 指定。 （**操作清单里的能力，不是独立 Tool**：模型在 submit_edit_plan 的 operations 里给出 op + target + params，由后端在草稿副本上执行。）**提交位置**：submit_edit_plan 的 operations 里直接给一项 {\"op\": \"<能力名>\", \"target\": {...}, \"params\": {...}}。**包装要求**：{\"op\": \"DELETE_SECTION\", \"target\": {\"regionId\": \"<regionId>\"}, \"params\": {}}。",
      "params": {
        "type": "object",
        "description": "DELETE_SECTION 的参数对象。",
        "properties": {}
      },
      "required": [],
      "targetSpec": "每一步各自带 target：target.regionId 指向草稿里已有区块，target.ref 指向本次新增区块（ADD_SECTION 的 params.ref 起名）。",
      "resultSpec": "CapabilityOutcome：ok / message / detail（含 regionId、title、chartKind）；试做失败时返回具体错误（哪一步、哪个目标、哪条约束、为什么，外加字段路径、收到的类型、要求的类型、该填在哪、最小示例）",
      "states": [
        "WAITING_USER_CONFIRM",
        "USER_EDITING_TEMPLATE"
      ],
      "readOnly": false,
      "sideEffects": "只改**服务端草稿的隔离副本**；试做通过并由用户接受后，才在正式事务里写入草稿（版本 +1）。",
      "errors": [
        "能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=unknown_region",
        "参数不合法 → reason=invalid_params，不执行",
        "目标引用不认识 → reason=unknown_ref / missing_target / unknown_region"
      ],
      "limits": [
        "每一步都要有明确目标：target.regionId（草稿里已有区块）或 target.ref（本次新增区块的引用名）",
        "提交位置：submit_edit_plan 的 operations 里直接给一项 {\"op\": \"<能力名>\", \"target\": {...}, \"params\": {...}}",
        "包装要求：{\"op\": \"DELETE_SECTION\", \"target\": {\"regionId\": \"<regionId>\"}, \"params\": {}}",
        "最小结构示例（只给格式）：{\"op\": \"DELETE_SECTION\", \"target\": {\"regionId\": \"<regionId>\"}, \"params\": {}}"
      ],
      "code": {
        "registration": "app/services/edit_capabilities/base.py::register",
        "dispatch": "app/tools/submit_edit_plan_tool.py::run",
        "validation": "app/services/edit_capabilities/base.py::validate_params",
        "execution": "app/services/edit_capabilities/blocks.py::_delete_section"
      }
    },
    {
      "id": "SET_DISPLAY",
      "group": "清单能力 / 设置统计与展示",
      "entryType": "operation",
      "status": "available",
      "purpose": "改一个区块的展示类型。componentType 取 TEXT / METRIC_CARD / TABLE / CHART；componentType=CHART 时必须给 chartKind（LINE / BAR / PIE / AREA）。 （**操作清单里的能力，不是独立 Tool**：模型在 submit_edit_plan 的 operations 里给出 op + target + params，由后端在草稿副本上执行。）**提交位置**：submit_edit_plan 的 operations 里直接给一项 {\"op\": \"<能力名>\", \"target\": {...}, \"params\": {...}}。**包装要求**：{\"op\": \"SET_DISPLAY\", \"target\": {\"regionId\": \"<regionId>\"}, \"params\": {}}。",
      "params": {
        "type": "object",
        "description": "SET_DISPLAY 的参数对象。",
        "properties": {
          "componentType": {
            "type": "string",
            "description": "组件类型（TEXT / METRIC_CARD / TABLE / CHART）",
            "enum": [
              "ANALYSIS",
              "CHART",
              "CONTAINER",
              "DATA_QUALITY",
              "METRIC_CARD",
              "METRIC_GROUP",
              "TABLE",
              "TEXT"
            ],
            "default": ""
          },
          "chartKind": {
            "type": "string",
            "description": "图表类型（componentType=CHART 时必填）",
            "enum": [
              "AREA",
              "BAR",
              "DONUT",
              "LINE",
              "PIE",
              "STACKED_BAR"
            ],
            "default": ""
          }
        }
      },
      "required": [],
      "targetSpec": "每一步各自带 target：target.regionId 指向草稿里已有区块，target.ref 指向本次新增区块（ADD_SECTION 的 params.ref 起名）。",
      "resultSpec": "CapabilityOutcome：ok / message / detail（含 regionId、title、chartKind）；试做失败时返回具体错误（哪一步、哪个目标、哪条约束、为什么，外加字段路径、收到的类型、要求的类型、该填在哪、最小示例）",
      "states": [
        "WAITING_USER_CONFIRM",
        "USER_EDITING_TEMPLATE"
      ],
      "readOnly": false,
      "sideEffects": "只改**服务端草稿的隔离副本**；试做通过并由用户接受后，才在正式事务里写入草稿（版本 +1）。",
      "errors": [
        "能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=unknown_region",
        "能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=missing_component_type",
        "能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=unsupported_component_type",
        "能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=missing_chart_kind",
        "能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=unsupported_chart_kind",
        "参数不合法 → reason=invalid_params，不执行",
        "目标引用不认识 → reason=unknown_ref / missing_target / unknown_region"
      ],
      "limits": [
        "每一步都要有明确目标：target.regionId（草稿里已有区块）或 target.ref（本次新增区块的引用名）",
        "componentType 取值域：ANALYSIS、CHART、CONTAINER、DATA_QUALITY、METRIC_CARD、METRIC_GROUP、TABLE、TEXT",
        "chartKind 取值域：AREA、BAR、DONUT、LINE、PIE、STACKED_BAR",
        "提交位置：submit_edit_plan 的 operations 里直接给一项 {\"op\": \"<能力名>\", \"target\": {...}, \"params\": {...}}",
        "包装要求：{\"op\": \"SET_DISPLAY\", \"target\": {\"regionId\": \"<regionId>\"}, \"params\": {}}",
        "最小结构示例（只给格式）：{\"op\": \"SET_DISPLAY\", \"target\": {\"regionId\": \"<regionId>\"}, \"params\": {}}"
      ],
      "code": {
        "registration": "app/services/edit_capabilities/base.py::register",
        "dispatch": "app/tools/submit_edit_plan_tool.py::run",
        "validation": "app/services/edit_capabilities/base.py::validate_params",
        "execution": "app/services/edit_capabilities/blocks.py::_set_display"
      }
    },
    {
      "id": "SET_CATEGORY_DATA",
      "group": "清单能力 / 设置统计与展示",
      "entryType": "operation",
      "status": "available",
      "purpose": "给一个图表区块设置分类统计口径：dimension（模拟分类维度，如告警等级 / 告警区域）或 categories（分类与数值）。数值是模拟的，会在图上标注。 （**操作清单里的能力，不是独立 Tool**：模型在 submit_edit_plan 的 operations 里给出 op + target + params，由后端在草稿副本上执行。）**提交位置**：submit_edit_plan 的 operations 里直接给一项 {\"op\": \"<能力名>\", \"target\": {...}, \"params\": {...}}。**包装要求**：{\"op\": \"SET_CATEGORY_DATA\", \"target\": {\"regionId\": \"<regionId>\"}, \"params\": {}}。",
      "params": {
        "type": "object",
        "description": "SET_CATEGORY_DATA 的参数对象。",
        "properties": {
          "dimension": {
            "type": "string",
            "description": "模拟分类维度（key 或业务别名）",
            "enum": [
              "alertLevel",
              "alertRegion",
              "alertType"
            ],
            "default": ""
          },
          "categories": {
            "type": "array",
            "items": {
              "type": "object",
              "properties": {
                "label": {
                  "type": "string",
                  "minLength": 1
                },
                "value": {
                  "type": "number",
                  "default": 0
                }
              },
              "additionalProperties": false
            }
          },
          "unit": {
            "type": "string",
            "default": ""
          }
        }
      },
      "required": [],
      "targetSpec": "每一步各自带 target：target.regionId 指向草稿里已有区块，target.ref 指向本次新增区块（ADD_SECTION 的 params.ref 起名）。",
      "resultSpec": "CapabilityOutcome：ok / message / detail（含 regionId、title、chartKind）；试做失败时返回具体错误（哪一步、哪个目标、哪条约束、为什么，外加字段路径、收到的类型、要求的类型、该填在哪、最小示例）",
      "states": [
        "WAITING_USER_CONFIRM",
        "USER_EDITING_TEMPLATE"
      ],
      "readOnly": false,
      "sideEffects": "只改**服务端草稿的隔离副本**；试做通过并由用户接受后，才在正式事务里写入草稿（版本 +1）。",
      "errors": [
        "能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=unknown_region",
        "能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=unknown_dimension",
        "能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=missing_statistic",
        "能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=duplicate_category",
        "参数不合法 → reason=invalid_params，不执行",
        "目标引用不认识 → reason=unknown_ref / missing_target / unknown_region"
      ],
      "limits": [
        "每一步都要有明确目标：target.regionId（草稿里已有区块）或 target.ref（本次新增区块的引用名）",
        "dimension 取值域：alertLevel、alertRegion、alertType",
        "提交位置：submit_edit_plan 的 operations 里直接给一项 {\"op\": \"<能力名>\", \"target\": {...}, \"params\": {...}}",
        "包装要求：{\"op\": \"SET_CATEGORY_DATA\", \"target\": {\"regionId\": \"<regionId>\"}, \"params\": {}}",
        "最小结构示例（只给格式）：{\"op\": \"SET_CATEGORY_DATA\", \"target\": {\"regionId\": \"<regionId>\"}, \"params\": {}}"
      ],
      "code": {
        "registration": "app/services/edit_capabilities/base.py::register",
        "dispatch": "app/tools/submit_edit_plan_tool.py::run",
        "validation": "app/services/edit_capabilities/base.py::validate_params",
        "execution": "app/services/edit_capabilities/blocks.py::_set_category_data"
      }
    },
    {
      "id": "SET_PRESENTATION",
      "group": "清单能力 / 设置样式",
      "entryType": "operation",
      "status": "available",
      "purpose": "一批受约束的展示操作（分组 / 排版 / 样式 / 格式 / 页面）。**这是一个包装能力**：展示操作名不是顶层能力，只能放在它的 params.operations 里，每一项形如 {\"op\": \"<展示操作名>\", \"params\": {...}}；operations 是**非空列表**。允许的 op、每个 op 的必填 / 可选键与取值域见能力总目录的「展示操作白名单」一节（由展示服务的白名单自动生成）。**区域级格式（SET_FORMAT）写的是整块卡片的格式**：指标卡 / 指标组里某个指标项的数字显示格式请用 SET_METRIC_ITEM（指标级）—— 区域级格式会连带改掉这张卡上别的指标的单位。 （**操作清单里的能力，不是独立 Tool**：模型在 submit_edit_plan 的 operations 里给出 op + target + params，由后端在草稿副本上执行。）**提交位置**：submit_edit_plan 的 operations 里 SET_PRESENTATION 这一项的 params.operations 里（每一项形如 {\"op\": \"<展示操作名>\", \"params\": {...}}）。**包装要求**：{\"op\": \"SET_PRESENTATION\", \"params\": {\"operations\": [{\"op\": \"CREATE_GROUP\", \"params\": {\"groupId\": \"<groupId>\", \"memberRegionIds\": [\"<memberRegionIds>\"]}}]}}。",
      "params": {
        "type": "object",
        "description": "SET_PRESENTATION 的参数对象。",
        "properties": {
          "operations": {
            "type": "array",
            "description": "外观修改包里的操作名单：非空列表，每一项形如 {\"op\": \"<展示操作名>\", \"params\": {...}}",
            "minItems": 1,
            "items": {
              "type": "object",
              "properties": {
                "op": {
                  "type": "string",
                  "description": "展示操作名（包装能力里允许的那些）",
                  "enum": [
                    "CREATE_GROUP",
                    "SET_GROUP_ARRANGEMENT",
                    "MOVE_REGION",
                    "UNGROUP",
                    "DELETE_GROUP",
                    "COPY_REGION",
                    "DELETE_REGION",
                    "HIDE_REGION",
                    "SHOW_REGION",
                    "SET_PLACEMENT",
                    "SET_STYLE",
                    "SET_FORMAT",
                    "SET_PAGE",
                    "SET_METADATA",
                    "SET_THEME"
                  ]
                },
                "params": {
                  "type": "object",
                  "description": "该展示操作的参数对象",
                  "additionalProperties": false
                }
              },
              "additionalProperties": false
            }
          }
        }
      },
      "required": [],
      "targetSpec": "每一步各自带 target：target.regionId 指向草稿里已有区块，target.ref 指向本次新增区块（ADD_SECTION 的 params.ref 起名）。",
      "resultSpec": "CapabilityOutcome：ok / message / detail（含 regionId、title、chartKind）；试做失败时返回具体错误（哪一步、哪个目标、哪条约束、为什么，外加字段路径、收到的类型、要求的类型、该填在哪、最小示例）",
      "states": [
        "WAITING_USER_CONFIRM",
        "USER_EDITING_TEMPLATE"
      ],
      "readOnly": false,
      "sideEffects": "只改**服务端草稿的隔离副本**；试做通过并由用户接受后，才在正式事务里写入草稿（版本 +1）。",
      "errors": [
        "能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=missing_operations",
        "能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=invalid_presentation_ops",
        "能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=presentation_failed",
        "参数不合法 → reason=invalid_params，不执行",
        "目标引用不认识 → reason=unknown_ref / missing_target / unknown_region"
      ],
      "limits": [
        "不需要目标（自己新建区块 / 整批作用于草稿）",
        "operations 至少 1 项",
        "提交位置：submit_edit_plan 的 operations 里 SET_PRESENTATION 这一项的 params.operations 里（每一项形如 {\"op\": \"<展示操作名>\", \"params\": {...}}）",
        "包装要求：{\"op\": \"SET_PRESENTATION\", \"params\": {\"operations\": [{\"op\": \"CREATE_GROUP\", \"params\": {\"groupId\": \"<groupId>\", \"memberRegionIds\": [\"<memberRegionIds>\"]}}]}}",
        "最小结构示例（只给格式）：{\"op\": \"SET_PRESENTATION\", \"params\": {\"operations\": [{\"op\": \"CREATE_GROUP\", \"params\": {\"groupId\": \"<groupId>\", \"memberRegionIds\": [\"<memberRegionIds>\"]}}]}}"
      ],
      "code": {
        "registration": "app/services/edit_capabilities/base.py::register",
        "dispatch": "app/tools/submit_edit_plan_tool.py::run",
        "validation": "app/services/edit_capabilities/base.py::validate_params",
        "execution": "app/services/edit_capabilities/blocks.py::_set_presentation"
      }
    },
    {
      "id": "SET_DATA_BINDING",
      "group": "清单能力 / 设置统计与展示",
      "entryType": "operation",
      "status": "available",
      "purpose": "设置这个区域**展示**用的绑定：outputRef（换绑已登记的输出）、fieldAliases（展示字段名 → 实际结果字段名）、presentationFilters（只过滤展示记录）、sort / topN（展示排序与前 N 项）。这些**只改展示**：源输出、全量总计和其他区域的输入不变；未登记的引用会被拒绝，不会自动新增跨区域数据依赖。 （**操作清单里的能力，不是独立 Tool**：模型在 submit_edit_plan 的 operations 里给出 op + target + params，由后端在草稿副本上执行。）**提交位置**：submit_edit_plan 的 operations 里直接给一项 {\"op\": \"<能力名>\", \"target\": {...}, \"params\": {...}}。**包装要求**：{\"op\": \"SET_DATA_BINDING\", \"target\": {\"regionId\": \"<regionId>\"}, \"params\": {}}。",
      "params": {
        "type": "object",
        "description": "SET_DATA_BINDING 的参数对象。",
        "properties": {
          "outputRef": {
            "type": "string",
            "description": "改绑的输出引用（output:<name>）",
            "default": ""
          },
          "fieldAliases": {
            "type": "object",
            "description": "展示字段名 → 实际结果字段名，例如 {\"时间点\":\"timestamp\"}。右侧必须是已声明字段；也兼容可唯一识别的反向映射。",
            "additionalProperties": false
          },
          "clearFieldAliases": {
            "type": "boolean",
            "default": false
          },
          "presentationFilters": {
            "type": "array",
            "description": "只过滤展示记录。条件形如 {\"type\":\"CONDITION\",\"left\":{\"type\":\"FIELD\",\"fieldRef\":\"energyKwh\"},\"operator\":\"GT\",\"operands\":[{\"type\":\"LITERAL\",\"value\":400}]}；组用 type=GROUP、logic=AND/OR/NOT、conditions=[条件]。也接收 fieldRef/operator/value 简写，统一转成上述结构。",
            "items": {
              "type": "object",
              "properties": {
                "type": {
                  "type": "string",
                  "enum": [
                    "CONDITION",
                    "GROUP"
                  ]
                },
                "left": {
                  "type": "object",
                  "properties": {
                    "type": {
                      "type": "string",
                      "enum": [
                        "FIELD"
                      ]
                    },
                    "fieldRef": {
                      "type": "string",
                      "description": "已声明的实际字段名"
                    }
                  },
                  "additionalProperties": false
                },
                "operator": {
                  "type": "string",
                  "enum": [
                    "EQ",
                    "NE",
                    "GT",
                    "GE",
                    "LT",
                    "LE",
                    "IN",
                    "NOT_IN",
                    "BETWEEN",
                    "CONTAINS",
                    "STARTS_WITH",
                    "IS_NULL",
                    "IS_NOT_NULL"
                  ]
                },
                "operands": {
                  "type": "array",
                  "items": {
                    "type": "object",
                    "properties": {
                      "type": {
                        "type": "string",
                        "enum": [
                          "LITERAL"
                        ]
                      },
                      "value": {
                        "type": "string",
                        "description": "字面量值；数字阈值用数字"
                      }
                    },
                    "additionalProperties": false
                  }
                },
                "logic": {
                  "type": "string",
                  "enum": [
                    "AND",
                    "OR",
                    "NOT"
                  ]
                },
                "conditions": {
                  "type": "array",
                  "items": {
                    "type": "object"
                  }
                }
              },
              "additionalProperties": false
            }
          },
          "clearPresentationFilters": {
            "type": "boolean",
            "default": false
          },
          "sort": {
            "type": "array",
            "description": "展示排序字段（fieldRef / direction / nulls）",
            "items": {
              "type": "object"
            }
          },
          "clearSort": {
            "type": "boolean",
            "default": false
          },
          "topN": {
            "type": "integer",
            "description": "展示前 N 项；只是展示裁剪，不限制取数"
          },
          "clearTopN": {
            "type": "boolean",
            "default": false
          }
        }
      },
      "required": [],
      "targetSpec": "每一步各自带 target：target.regionId 指向草稿里已有区块，target.ref 指向本次新增区块（ADD_SECTION 的 params.ref 起名）。",
      "resultSpec": "CapabilityOutcome：ok / message / detail（含 regionId、title、chartKind）；试做失败时返回具体错误（哪一步、哪个目标、哪条约束、为什么，外加字段路径、收到的类型、要求的类型、该填在哪、最小示例）",
      "states": [
        "WAITING_USER_CONFIRM",
        "USER_EDITING_TEMPLATE"
      ],
      "readOnly": false,
      "sideEffects": "只改**服务端草稿的隔离副本**；试做通过并由用户接受后，才在正式事务里写入草稿（版本 +1）。",
      "errors": [
        "能力失败抛 CapabilityError（带 step / op / target / constraint / reason）",
        "参数不合法 → reason=invalid_params，不执行",
        "目标引用不认识 → reason=unknown_ref / missing_target / unknown_region"
      ],
      "limits": [
        "每一步都要有明确目标：target.regionId（草稿里已有区块）或 target.ref（本次新增区块的引用名）",
        "提交位置：submit_edit_plan 的 operations 里直接给一项 {\"op\": \"<能力名>\", \"target\": {...}, \"params\": {...}}",
        "包装要求：{\"op\": \"SET_DATA_BINDING\", \"target\": {\"regionId\": \"<regionId>\"}, \"params\": {}}",
        "最小结构示例（只给格式）：{\"op\": \"SET_DATA_BINDING\", \"target\": {\"regionId\": \"<regionId>\"}, \"params\": {}}"
      ],
      "code": {
        "registration": "app/services/edit_capabilities/base.py::register",
        "dispatch": "app/tools/submit_edit_plan_tool.py::run",
        "validation": "app/services/edit_capabilities/base.py::validate_params",
        "execution": "app/services/edit_capabilities/binding_component.py::_set_data_binding"
      }
    },
    {
      "id": "SET_CHART_KIND",
      "group": "清单能力 / 设置统计与展示",
      "entryType": "operation",
      "status": "available",
      "purpose": "改一个图表区块的图表类型：LINE / AREA / BAR / STACKED_BAR / PIE / DONUT。**只改图型**，分类、系列与数值一律不动；类型逐字保留（STACKED_BAR 不折成 BAR，DONUT 不折成 PIE）。饼图 / 环形图复用当前分类绑定，或当前输出已声明的分类字段与数值字段；已有分组聚合输出、SUM/COUNT 的离散时间桶也可直接切换，保持原标签和逐点数值。无需先调用 SET_CATEGORY_DATA；不得为了切换图型另填分类或数值。未聚合的连续时间序列与多系列不能直接转换。 （**操作清单里的能力，不是独立 Tool**：模型在 submit_edit_plan 的 operations 里给出 op + target + params，由后端在草稿副本上执行。）**提交位置**：submit_edit_plan 的 operations 里直接给一项 {\"op\": \"<能力名>\", \"target\": {...}, \"params\": {...}}。**包装要求**：{\"op\": \"SET_CHART_KIND\", \"target\": {\"regionId\": \"<regionId>\"}, \"params\": {\"chartKind\": \"AREA\"}}。",
      "params": {
        "type": "object",
        "description": "SET_CHART_KIND 的参数对象。",
        "properties": {
          "chartKind": {
            "type": "string",
            "description": "图表类型（LINE / AREA / BAR / STACKED_BAR / PIE / DONUT）",
            "enum": [
              "AREA",
              "BAR",
              "DONUT",
              "LINE",
              "PIE",
              "STACKED_BAR"
            ],
            "minLength": 1
          }
        },
        "required": [
          "chartKind"
        ]
      },
      "required": [
        "chartKind"
      ],
      "targetSpec": "每一步各自带 target：target.regionId 指向草稿里已有区块，target.ref 指向本次新增区块（ADD_SECTION 的 params.ref 起名）。",
      "resultSpec": "CapabilityOutcome：ok / message / detail（含 regionId、title、chartKind）；试做失败时返回具体错误（哪一步、哪个目标、哪条约束、为什么，外加字段路径、收到的类型、要求的类型、该填在哪、最小示例）",
      "states": [
        "WAITING_USER_CONFIRM",
        "USER_EDITING_TEMPLATE"
      ],
      "readOnly": false,
      "sideEffects": "只改**服务端草稿的隔离副本**；试做通过并由用户接受后，才在正式事务里写入草稿（版本 +1）。",
      "errors": [
        "能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=unknown_region",
        "能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=unsupported_chart_kind",
        "能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=incompatible_data_shape",
        "参数不合法 → reason=invalid_params，不执行",
        "目标引用不认识 → reason=unknown_ref / missing_target / unknown_region"
      ],
      "limits": [
        "每一步都要有明确目标：target.regionId（草稿里已有区块）或 target.ref（本次新增区块的引用名）",
        "chartKind 取值域：AREA、BAR、DONUT、LINE、PIE、STACKED_BAR",
        "chartKind 至少 1 个字符",
        "提交位置：submit_edit_plan 的 operations 里直接给一项 {\"op\": \"<能力名>\", \"target\": {...}, \"params\": {...}}",
        "包装要求：{\"op\": \"SET_CHART_KIND\", \"target\": {\"regionId\": \"<regionId>\"}, \"params\": {\"chartKind\": \"AREA\"}}",
        "最小结构示例（只给格式）：{\"op\": \"SET_CHART_KIND\", \"target\": {\"regionId\": \"<regionId>\"}, \"params\": {\"chartKind\": \"AREA\"}}"
      ],
      "code": {
        "registration": "app/services/edit_capabilities/base.py::register",
        "dispatch": "app/tools/submit_edit_plan_tool.py::run",
        "validation": "app/services/edit_capabilities/base.py::validate_params",
        "execution": "app/services/edit_capabilities/chart_component.py::_set_chart_kind"
      }
    },
    {
      "id": "SET_CHART_ENCODINGS",
      "group": "清单能力 / 设置统计与展示",
      "entryType": "operation",
      "status": "available",
      "purpose": "设置图表的视觉通道绑定（给的是**完整清单**）：每一项形如 {\"channel\": \"X\", \"fieldRef\": \"<字段名>\", \"format\": {...}}；channel 取 X / Y / SERIES / COLOR / LABEL，每个通道最多一项。字段必须已登记或属于当前模拟字段目录；饼图 / 环形图没有横纵轴，只接受 COLOR 与 LABEL。 （**操作清单里的能力，不是独立 Tool**：模型在 submit_edit_plan 的 operations 里给出 op + target + params，由后端在草稿副本上执行。）**提交位置**：submit_edit_plan 的 operations 里直接给一项 {\"op\": \"<能力名>\", \"target\": {...}, \"params\": {...}}。**包装要求**：{\"op\": \"SET_CHART_ENCODINGS\", \"target\": {\"regionId\": \"<regionId>\"}, \"params\": {}}。",
      "params": {
        "type": "object",
        "description": "SET_CHART_ENCODINGS 的参数对象。",
        "properties": {
          "encodings": {
            "type": "array",
            "description": "完整的通道绑定清单：每一项形如 {\"channel\": \"X\", \"fieldRef\": \"<字段名>\", \"format\": {...}}；channel 取 X / Y / SERIES / COLOR / LABEL，每个通道最多一项。",
            "items": {
              "type": "object",
              "properties": {
                "channel": {
                  "type": "string",
                  "description": "视觉通道（X / Y / SERIES / COLOR / LABEL）",
                  "enum": [
                    "X",
                    "Y",
                    "SERIES",
                    "COLOR",
                    "LABEL"
                  ]
                },
                "fieldRef": {
                  "type": "string",
                  "description": "数据字段名（必须已登记或属于模拟字段目录）"
                },
                "format": {
                  "type": "object",
                  "description": "该通道的显示格式（可选）",
                  "additionalProperties": false
                }
              },
              "additionalProperties": false
            }
          }
        }
      },
      "required": [],
      "targetSpec": "每一步各自带 target：target.regionId 指向草稿里已有区块，target.ref 指向本次新增区块（ADD_SECTION 的 params.ref 起名）。",
      "resultSpec": "CapabilityOutcome：ok / message / detail（含 regionId、title、chartKind）；试做失败时返回具体错误（哪一步、哪个目标、哪条约束、为什么，外加字段路径、收到的类型、要求的类型、该填在哪、最小示例）",
      "states": [
        "WAITING_USER_CONFIRM",
        "USER_EDITING_TEMPLATE"
      ],
      "readOnly": false,
      "sideEffects": "只改**服务端草稿的隔离副本**；试做通过并由用户接受后，才在正式事务里写入草稿（版本 +1）。",
      "errors": [
        "能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=unknown_region",
        "能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=invalid_encoding",
        "能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=missing_channel",
        "能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=unsupported_channel",
        "能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=duplicate_channel",
        "能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=channel_not_applicable",
        "参数不合法 → reason=invalid_params，不执行",
        "目标引用不认识 → reason=unknown_ref / missing_target / unknown_region"
      ],
      "limits": [
        "每一步都要有明确目标：target.regionId（草稿里已有区块）或 target.ref（本次新增区块的引用名）",
        "提交位置：submit_edit_plan 的 operations 里直接给一项 {\"op\": \"<能力名>\", \"target\": {...}, \"params\": {...}}",
        "包装要求：{\"op\": \"SET_CHART_ENCODINGS\", \"target\": {\"regionId\": \"<regionId>\"}, \"params\": {}}",
        "最小结构示例（只给格式）：{\"op\": \"SET_CHART_ENCODINGS\", \"target\": {\"regionId\": \"<regionId>\"}, \"params\": {}}"
      ],
      "code": {
        "registration": "app/services/edit_capabilities/base.py::register",
        "dispatch": "app/tools/submit_edit_plan_tool.py::run",
        "validation": "app/services/edit_capabilities/base.py::validate_params",
        "execution": "app/services/edit_capabilities/chart_component.py::_set_chart_encodings"
      }
    },
    {
      "id": "SET_CHART_AXES",
      "group": "清单能力 / 设置统计与展示",
      "entryType": "operation",
      "status": "available",
      "purpose": "设置图表坐标轴：axes 的键是 'x' 或 'y'，值形如 {\"title\": \"...\", \"minimum\": 0, \"maximum\": 100, \"format\": {...}, \"scale\": \"LINEAR\"}；给哪个轴就改哪个轴，没给的轴保持原样（clearAxes=true 先清空全部）。scale 取 LINEAR / LOG / TIME / CATEGORY；下限不能高于上限。饼图 / 环形图没有坐标轴，设不了。 （**操作清单里的能力，不是独立 Tool**：模型在 submit_edit_plan 的 operations 里给出 op + target + params，由后端在草稿副本上执行。）**提交位置**：submit_edit_plan 的 operations 里直接给一项 {\"op\": \"<能力名>\", \"target\": {...}, \"params\": {...}}。**包装要求**：{\"op\": \"SET_CHART_AXES\", \"target\": {\"regionId\": \"<regionId>\"}, \"params\": {}}。",
      "params": {
        "type": "object",
        "description": "SET_CHART_AXES 的参数对象。",
        "properties": {
          "axes": {
            "type": "object",
            "description": "要设置的坐标轴：键是 'x' 或 'y'，值形如 {\"title\": \"...\", \"minimum\": 0, \"maximum\": 100, \"format\": {...}, \"scale\": \"LINEAR\"}；给哪个轴就改哪个轴，没给的轴保持原样。",
            "additionalProperties": false
          },
          "clearAxes": {
            "type": "boolean",
            "description": "是否先清空全部坐标轴（true = 清空后再应用 axes 里的内容）",
            "default": false
          }
        }
      },
      "required": [],
      "targetSpec": "每一步各自带 target：target.regionId 指向草稿里已有区块，target.ref 指向本次新增区块（ADD_SECTION 的 params.ref 起名）。",
      "resultSpec": "CapabilityOutcome：ok / message / detail（含 regionId、title、chartKind）；试做失败时返回具体错误（哪一步、哪个目标、哪条约束、为什么，外加字段路径、收到的类型、要求的类型、该填在哪、最小示例）",
      "states": [
        "WAITING_USER_CONFIRM",
        "USER_EDITING_TEMPLATE"
      ],
      "readOnly": false,
      "sideEffects": "只改**服务端草稿的隔离副本**；试做通过并由用户接受后，才在正式事务里写入草稿（版本 +1）。",
      "errors": [
        "能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=unknown_region",
        "能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=axes_not_supported",
        "能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=unknown_axis",
        "参数不合法 → reason=invalid_params，不执行",
        "目标引用不认识 → reason=unknown_ref / missing_target / unknown_region"
      ],
      "limits": [
        "每一步都要有明确目标：target.regionId（草稿里已有区块）或 target.ref（本次新增区块的引用名）",
        "提交位置：submit_edit_plan 的 operations 里直接给一项 {\"op\": \"<能力名>\", \"target\": {...}, \"params\": {...}}",
        "包装要求：{\"op\": \"SET_CHART_AXES\", \"target\": {\"regionId\": \"<regionId>\"}, \"params\": {}}",
        "最小结构示例（只给格式）：{\"op\": \"SET_CHART_AXES\", \"target\": {\"regionId\": \"<regionId>\"}, \"params\": {}}"
      ],
      "code": {
        "registration": "app/services/edit_capabilities/base.py::register",
        "dispatch": "app/tools/submit_edit_plan_tool.py::run",
        "validation": "app/services/edit_capabilities/base.py::validate_params",
        "execution": "app/services/edit_capabilities/chart_component.py::_set_chart_axes"
      }
    },
    {
      "id": "SET_CHART_LEGEND",
      "group": "清单能力 / 设置统计与展示",
      "entryType": "operation",
      "status": "available",
      "purpose": "设置图例开关与稳定系列配色：legend 是布尔值（不填 = 不改，协议里图例只有开关，没有位置与方向）；seriesColors 是「稳定系列键 → 颜色」的映射（颜色写十六进制、rgb()/hsl()、颜色关键字或主题 token 名，不接受外链）；系列键必须是这个图表真实存在的系列（clearSeriesColors=true 先清空）。 （**操作清单里的能力，不是独立 Tool**：模型在 submit_edit_plan 的 operations 里给出 op + target + params，由后端在草稿副本上执行。）**提交位置**：submit_edit_plan 的 operations 里直接给一项 {\"op\": \"<能力名>\", \"target\": {...}, \"params\": {...}}。**包装要求**：{\"op\": \"SET_CHART_LEGEND\", \"target\": {\"regionId\": \"<regionId>\"}, \"params\": {}}。",
      "params": {
        "type": "object",
        "description": "SET_CHART_LEGEND 的参数对象。",
        "properties": {
          "legend": {
            "type": "boolean",
            "description": "是否显示图例（不填 = 不改；协议里图例只有开关，没有位置 / 方向）"
          },
          "seriesColors": {
            "type": "object",
            "description": "稳定系列键 → 颜色（十六进制 / rgb() / 颜色关键字或主题 token 名）",
            "additionalProperties": false
          },
          "clearSeriesColors": {
            "type": "boolean",
            "description": "是否先清空已有配色（true = 清空后再应用 seriesColors）",
            "default": false
          }
        }
      },
      "required": [],
      "targetSpec": "每一步各自带 target：target.regionId 指向草稿里已有区块，target.ref 指向本次新增区块（ADD_SECTION 的 params.ref 起名）。",
      "resultSpec": "CapabilityOutcome：ok / message / detail（含 regionId、title、chartKind）；试做失败时返回具体错误（哪一步、哪个目标、哪条约束、为什么，外加字段路径、收到的类型、要求的类型、该填在哪、最小示例）",
      "states": [
        "WAITING_USER_CONFIRM",
        "USER_EDITING_TEMPLATE"
      ],
      "readOnly": false,
      "sideEffects": "只改**服务端草稿的隔离副本**；试做通过并由用户接受后，才在正式事务里写入草稿（版本 +1）。",
      "errors": [
        "能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=unknown_region",
        "能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=unknown_series",
        "能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=invalid_series_color",
        "参数不合法 → reason=invalid_params，不执行",
        "目标引用不认识 → reason=unknown_ref / missing_target / unknown_region"
      ],
      "limits": [
        "每一步都要有明确目标：target.regionId（草稿里已有区块）或 target.ref（本次新增区块的引用名）",
        "提交位置：submit_edit_plan 的 operations 里直接给一项 {\"op\": \"<能力名>\", \"target\": {...}, \"params\": {...}}",
        "包装要求：{\"op\": \"SET_CHART_LEGEND\", \"target\": {\"regionId\": \"<regionId>\"}, \"params\": {}}",
        "最小结构示例（只给格式）：{\"op\": \"SET_CHART_LEGEND\", \"target\": {\"regionId\": \"<regionId>\"}, \"params\": {}}"
      ],
      "code": {
        "registration": "app/services/edit_capabilities/base.py::register",
        "dispatch": "app/tools/submit_edit_plan_tool.py::run",
        "validation": "app/services/edit_capabilities/base.py::validate_params",
        "execution": "app/services/edit_capabilities/chart_component.py::_set_chart_legend"
      }
    },
    {
      "id": "SET_CHART_TOOLTIP",
      "group": "清单能力 / 设置统计与展示",
      "entryType": "operation",
      "status": "available",
      "purpose": "设置提示框里**额外**显示的字段：tooltipFields 给的是完整清单（字段必须已登记或属于当前模拟字段目录；clearTooltipFields=true 清空）。基础提示（分类名与数值）永远保留，不会被关掉。 （**操作清单里的能力，不是独立 Tool**：模型在 submit_edit_plan 的 operations 里给出 op + target + params，由后端在草稿副本上执行。）**提交位置**：submit_edit_plan 的 operations 里直接给一项 {\"op\": \"<能力名>\", \"target\": {...}, \"params\": {...}}。**包装要求**：{\"op\": \"SET_CHART_TOOLTIP\", \"target\": {\"regionId\": \"<regionId>\"}, \"params\": {}}。",
      "params": {
        "type": "object",
        "description": "SET_CHART_TOOLTIP 的参数对象。",
        "properties": {
          "tooltipFields": {
            "type": "array",
            "description": "提示里要额外显示的字段名（非空时按此完整清单替换）",
            "items": {
              "type": "string"
            }
          },
          "clearTooltipFields": {
            "type": "boolean",
            "description": "是否清空提示字段（true = 清空；基础提示不受影响）",
            "default": false
          }
        }
      },
      "required": [],
      "targetSpec": "每一步各自带 target：target.regionId 指向草稿里已有区块，target.ref 指向本次新增区块（ADD_SECTION 的 params.ref 起名）。",
      "resultSpec": "CapabilityOutcome：ok / message / detail（含 regionId、title、chartKind）；试做失败时返回具体错误（哪一步、哪个目标、哪条约束、为什么，外加字段路径、收到的类型、要求的类型、该填在哪、最小示例）",
      "states": [
        "WAITING_USER_CONFIRM",
        "USER_EDITING_TEMPLATE"
      ],
      "readOnly": false,
      "sideEffects": "只改**服务端草稿的隔离副本**；试做通过并由用户接受后，才在正式事务里写入草稿（版本 +1）。",
      "errors": [
        "能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=unknown_region",
        "参数不合法 → reason=invalid_params，不执行",
        "目标引用不认识 → reason=unknown_ref / missing_target / unknown_region"
      ],
      "limits": [
        "每一步都要有明确目标：target.regionId（草稿里已有区块）或 target.ref（本次新增区块的引用名）",
        "提交位置：submit_edit_plan 的 operations 里直接给一项 {\"op\": \"<能力名>\", \"target\": {...}, \"params\": {...}}",
        "包装要求：{\"op\": \"SET_CHART_TOOLTIP\", \"target\": {\"regionId\": \"<regionId>\"}, \"params\": {}}",
        "最小结构示例（只给格式）：{\"op\": \"SET_CHART_TOOLTIP\", \"target\": {\"regionId\": \"<regionId>\"}, \"params\": {}}"
      ],
      "code": {
        "registration": "app/services/edit_capabilities/base.py::register",
        "dispatch": "app/tools/submit_edit_plan_tool.py::run",
        "validation": "app/services/edit_capabilities/base.py::validate_params",
        "execution": "app/services/edit_capabilities/chart_component.py::_set_chart_tooltip"
      }
    },
    {
      "id": "SET_CHART_REFERENCE_LINES",
      "group": "清单能力 / 设置统计与展示",
      "entryType": "operation",
      "status": "available",
      "purpose": "设置参考线：referenceLines 是「名称 → 表达式」，形如 {\"type\": \"LITERAL\", \"value\": 100}（本批只支持常量数值表达式，数值按图表数值轴的单位理解）；参考线挂在有单一数值轴的图型上，饼图 / 环形图没有轴，设不了；名称不能重复（clearReferenceLines=true 先清空）。 （**操作清单里的能力，不是独立 Tool**：模型在 submit_edit_plan 的 operations 里给出 op + target + params，由后端在草稿副本上执行。）**提交位置**：submit_edit_plan 的 operations 里直接给一项 {\"op\": \"<能力名>\", \"target\": {...}, \"params\": {...}}。**包装要求**：{\"op\": \"SET_CHART_REFERENCE_LINES\", \"target\": {\"regionId\": \"<regionId>\"}, \"params\": {}}。",
      "params": {
        "type": "object",
        "description": "SET_CHART_REFERENCE_LINES 的参数对象。",
        "properties": {
          "referenceLines": {
            "type": "object",
            "description": "参考线：名称 → 表达式，形如 {\"type\": \"LITERAL\", \"value\": 100}；数值按图表数值轴的单位理解。",
            "additionalProperties": false
          },
          "clearReferenceLines": {
            "type": "boolean",
            "description": "是否先清空已有参考线（true = 清空后再应用 referenceLines）",
            "default": false
          }
        }
      },
      "required": [],
      "targetSpec": "每一步各自带 target：target.regionId 指向草稿里已有区块，target.ref 指向本次新增区块（ADD_SECTION 的 params.ref 起名）。",
      "resultSpec": "CapabilityOutcome：ok / message / detail（含 regionId、title、chartKind）；试做失败时返回具体错误（哪一步、哪个目标、哪条约束、为什么，外加字段路径、收到的类型、要求的类型、该填在哪、最小示例）",
      "states": [
        "WAITING_USER_CONFIRM",
        "USER_EDITING_TEMPLATE"
      ],
      "readOnly": false,
      "sideEffects": "只改**服务端草稿的隔离副本**；试做通过并由用户接受后，才在正式事务里写入草稿（版本 +1）。",
      "errors": [
        "能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=unknown_region",
        "能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=reference_axis_ambiguous",
        "能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=invalid_reference_line",
        "能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=duplicate_reference_line",
        "参数不合法 → reason=invalid_params，不执行",
        "目标引用不认识 → reason=unknown_ref / missing_target / unknown_region"
      ],
      "limits": [
        "每一步都要有明确目标：target.regionId（草稿里已有区块）或 target.ref（本次新增区块的引用名）",
        "提交位置：submit_edit_plan 的 operations 里直接给一项 {\"op\": \"<能力名>\", \"target\": {...}, \"params\": {...}}",
        "包装要求：{\"op\": \"SET_CHART_REFERENCE_LINES\", \"target\": {\"regionId\": \"<regionId>\"}, \"params\": {}}",
        "最小结构示例（只给格式）：{\"op\": \"SET_CHART_REFERENCE_LINES\", \"target\": {\"regionId\": \"<regionId>\"}, \"params\": {}}"
      ],
      "code": {
        "registration": "app/services/edit_capabilities/base.py::register",
        "dispatch": "app/tools/submit_edit_plan_tool.py::run",
        "validation": "app/services/edit_capabilities/base.py::validate_params",
        "execution": "app/services/edit_capabilities/chart_component.py::_set_chart_reference_lines"
      }
    },
    {
      "id": "SET_TABLE_COLUMNS",
      "group": "清单能力 / 设置统计与展示",
      "entryType": "operation",
      "status": "available",
      "purpose": "设置表格的**列清单**（完整清单：给了就是全部，顺序即显示顺序）。每一项形如 {\"id\": \"col_1\", \"label\": \"机房名称\", \"fieldRef\": \"roomName\", \"format\": {\"kind\": \"TEXT\"}, \"width\": {\"value\": 120, \"unit\": \"PX\"}, \"style\": {\"textAlign\": \"LEFT\"}, \"headerPath\": [\"能耗\", \"总量\"]}；给了 id 就是改这一列（id 必须已存在），不给 id 就是新增一列。标题（label）与取值字段（fieldRef）**互相独立**；清单里没有的列**不再显示**，但源字段、输出与指标一个都不删。**只改其中几列时，清单里其余列的相对顺序要照抄当前表格的顺序**（先按 get_current_region 给的顺序列出，再改你要改的那几项）—— **不要按 id 重排**：清单顺序就是显示顺序，按 id 排会把用户刚放好的列挪走。 （**操作清单里的能力，不是独立 Tool**：模型在 submit_edit_plan 的 operations 里给出 op + target + params，由后端在草稿副本上执行。）**提交位置**：submit_edit_plan 的 operations 里直接给一项 {\"op\": \"<能力名>\", \"target\": {...}, \"params\": {...}}。**包装要求**：{\"op\": \"SET_TABLE_COLUMNS\", \"target\": {\"regionId\": \"<regionId>\"}, \"params\": {}}。",
      "params": {
        "type": "object",
        "description": "SET_TABLE_COLUMNS 的参数对象。",
        "properties": {
          "columns": {
            "type": "array",
            "description": "完整的列清单（顺序即显示顺序）：每一项形如 {\"id\": \"col_1\", \"label\": \"机房名称\", \"fieldRef\": \"roomName\", \"format\": {\"kind\": \"TEXT\"}, \"width\": {\"value\": 120, \"unit\": \"PX\"}, \"style\": {\"textAlign\": \"LEFT\"}, \"headerPath\": [\"能耗\", \"总量\"]}；给了 id 就是改这一列（id 必须已存在），不给 id 就是新增一列。清单里没有的列**不再显示**（源字段、输出与指标一个都不删）。",
            "items": {
              "type": "object",
              "properties": {
                "id": {
                  "type": "string",
                  "description": "列的稳定 id（改已有列时给，新增列不给）",
                  "default": ""
                },
                "label": {
                  "type": "string",
                  "description": "列标题（与取值字段互相独立）",
                  "default": ""
                },
                "fieldRef": {
                  "type": "string",
                  "description": "取值字段名（必须已登记）",
                  "default": ""
                },
                "format": {
                  "type": "object",
                  "description": "这一列的显示格式（可选；不给则沿用原格式）",
                  "additionalProperties": false
                },
                "width": {
                  "type": "object",
                  "description": "列宽 {\"value\": 120, \"unit\": \"PX\"}（可选）",
                  "additionalProperties": false
                },
                "style": {
                  "type": "object",
                  "description": "这一列的样式（可选；只认受控的外观键）",
                  "additionalProperties": false
                },
                "headerPath": {
                  "type": "array",
                  "description": "多层表头路径（可选；每一层一段表头文字；[] 表示清空）",
                  "items": {
                    "type": "object"
                  }
                }
              },
              "additionalProperties": false
            }
          }
        }
      },
      "required": [],
      "targetSpec": "每一步各自带 target：target.regionId 指向草稿里已有区块，target.ref 指向本次新增区块（ADD_SECTION 的 params.ref 起名）。",
      "resultSpec": "CapabilityOutcome：ok / message / detail（含 regionId、title、chartKind）；试做失败时返回具体错误（哪一步、哪个目标、哪条约束、为什么，外加字段路径、收到的类型、要求的类型、该填在哪、最小示例）",
      "states": [
        "WAITING_USER_CONFIRM",
        "USER_EDITING_TEMPLATE"
      ],
      "readOnly": false,
      "sideEffects": "只改**服务端草稿的隔离副本**；试做通过并由用户接受后，才在正式事务里写入草稿（版本 +1）。",
      "errors": [
        "能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=unknown_region",
        "能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=no_changes",
        "能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=empty_column_list",
        "能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=unknown_column_id",
        "能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=missing_column_label",
        "能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=missing_column_field_ref",
        "参数不合法 → reason=invalid_params，不执行",
        "目标引用不认识 → reason=unknown_ref / missing_target / unknown_region"
      ],
      "limits": [
        "每一步都要有明确目标：target.regionId（草稿里已有区块）或 target.ref（本次新增区块的引用名）",
        "提交位置：submit_edit_plan 的 operations 里直接给一项 {\"op\": \"<能力名>\", \"target\": {...}, \"params\": {...}}",
        "包装要求：{\"op\": \"SET_TABLE_COLUMNS\", \"target\": {\"regionId\": \"<regionId>\"}, \"params\": {}}",
        "最小结构示例（只给格式）：{\"op\": \"SET_TABLE_COLUMNS\", \"target\": {\"regionId\": \"<regionId>\"}, \"params\": {}}"
      ],
      "code": {
        "registration": "app/services/edit_capabilities/base.py::register",
        "dispatch": "app/tools/submit_edit_plan_tool.py::run",
        "validation": "app/services/edit_capabilities/base.py::validate_params",
        "execution": "app/services/edit_capabilities/table_component.py::_set_table_columns"
      }
    },
    {
      "id": "SET_TABLE_DISPLAY",
      "group": "清单能力 / 设置统计与展示",
      "entryType": "operation",
      "status": "available",
      "purpose": "设置表格的**展示方式**：groupRowsBy（按字段分组显示行 —— 只是排版，**不产生小计或聚合**；清空用 clearGroupRowsBy）、rowNumber（是否显示序号）、pageSize（网页展示每页行数 —— 只是展示分页，**不限制取数与导出**；清空用 clearPageSize）、frozenColumnCount（冻结前几列；清空用 clearFrozenColumnCount）。没给的项保持原样。 （**操作清单里的能力，不是独立 Tool**：模型在 submit_edit_plan 的 operations 里给出 op + target + params，由后端在草稿副本上执行。）**提交位置**：submit_edit_plan 的 operations 里直接给一项 {\"op\": \"<能力名>\", \"target\": {...}, \"params\": {...}}。**包装要求**：{\"op\": \"SET_TABLE_DISPLAY\", \"target\": {\"regionId\": \"<regionId>\"}, \"params\": {}}。",
      "params": {
        "type": "object",
        "description": "SET_TABLE_DISPLAY 的参数对象。",
        "properties": {
          "groupRowsBy": {
            "type": "array",
            "description": "按哪些字段分组显示行（只是展示分组，**不产生小计或聚合**）；清空用 clearGroupRowsBy",
            "items": {
              "type": "string"
            }
          },
          "clearGroupRowsBy": {
            "type": "boolean",
            "description": "是否清空行分组字段",
            "default": false
          },
          "rowNumber": {
            "type": "boolean",
            "description": "是否显示序号列（不填 = 不改）"
          },
          "pageSize": {
            "type": "integer",
            "description": "网页展示每页行数（只是**展示**分页，不限制取数与导出）"
          },
          "clearPageSize": {
            "type": "boolean",
            "description": "是否清掉每页行数（恢复成不分页）",
            "default": false
          },
          "frozenColumnCount": {
            "type": "integer",
            "description": "冻结前几列（不填 = 不改）"
          },
          "clearFrozenColumnCount": {
            "type": "boolean",
            "description": "是否清掉冻结列设置",
            "default": false
          }
        }
      },
      "required": [],
      "targetSpec": "每一步各自带 target：target.regionId 指向草稿里已有区块，target.ref 指向本次新增区块（ADD_SECTION 的 params.ref 起名）。",
      "resultSpec": "CapabilityOutcome：ok / message / detail（含 regionId、title、chartKind）；试做失败时返回具体错误（哪一步、哪个目标、哪条约束、为什么，外加字段路径、收到的类型、要求的类型、该填在哪、最小示例）",
      "states": [
        "WAITING_USER_CONFIRM",
        "USER_EDITING_TEMPLATE"
      ],
      "readOnly": false,
      "sideEffects": "只改**服务端草稿的隔离副本**；试做通过并由用户接受后，才在正式事务里写入草稿（版本 +1）。",
      "errors": [
        "能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=unknown_region",
        "能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=no_changes",
        "能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=invalid_page_size",
        "能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=invalid_frozen_column_count",
        "参数不合法 → reason=invalid_params，不执行",
        "目标引用不认识 → reason=unknown_ref / missing_target / unknown_region"
      ],
      "limits": [
        "每一步都要有明确目标：target.regionId（草稿里已有区块）或 target.ref（本次新增区块的引用名）",
        "提交位置：submit_edit_plan 的 operations 里直接给一项 {\"op\": \"<能力名>\", \"target\": {...}, \"params\": {...}}",
        "包装要求：{\"op\": \"SET_TABLE_DISPLAY\", \"target\": {\"regionId\": \"<regionId>\"}, \"params\": {}}",
        "最小结构示例（只给格式）：{\"op\": \"SET_TABLE_DISPLAY\", \"target\": {\"regionId\": \"<regionId>\"}, \"params\": {}}"
      ],
      "code": {
        "registration": "app/services/edit_capabilities/base.py::register",
        "dispatch": "app/tools/submit_edit_plan_tool.py::run",
        "validation": "app/services/edit_capabilities/base.py::validate_params",
        "execution": "app/services/edit_capabilities/table_component.py::_set_table_display"
      }
    },
    {
      "id": "SET_TABLE_SUMMARIES",
      "group": "清单能力 / 设置统计与展示",
      "entryType": "operation",
      "status": "available",
      "purpose": "设置表格的**汇总引用**（完整清单：给了就是全部；清空用 clearSummaries）。每一项形如 {\"label\": \"告警总数\", \"outputRef\": \"output:alarms\", \"fieldRef\": \"totalAlarmCount\", \"basis\": \"FULL_RESULT\"}；outputRef 必须是这个区域**已登记**的计算输出（不会凭空生成汇总），basis 只能是 FULL_RESULT（全量口径）或 VISIBLE_ROWS（可见行口径）。 （**操作清单里的能力，不是独立 Tool**：模型在 submit_edit_plan 的 operations 里给出 op + target + params，由后端在草稿副本上执行。）**提交位置**：submit_edit_plan 的 operations 里直接给一项 {\"op\": \"<能力名>\", \"target\": {...}, \"params\": {...}}。**包装要求**：{\"op\": \"SET_TABLE_SUMMARIES\", \"target\": {\"regionId\": \"<regionId>\"}, \"params\": {}}。",
      "params": {
        "type": "object",
        "description": "SET_TABLE_SUMMARIES 的参数对象。",
        "properties": {
          "summaries": {
            "type": "array",
            "description": "完整的汇总清单：每一项形如 {\"label\": \"告警总数\", \"outputRef\": \"output:alarms\", \"fieldRef\": \"totalAlarmCount\", \"basis\": \"FULL_RESULT\"}；outputRef 必须是这个区域**已登记**的计算输出，basis 只能是 FULL_RESULT（全量口径）或 VISIBLE_ROWS（可见行口径）。",
            "items": {
              "type": "object",
              "properties": {
                "label": {
                  "type": "string",
                  "description": "汇总标签",
                  "minLength": 1
                },
                "outputRef": {
                  "type": "string",
                  "description": "已登记的计算输出（output:<name>）",
                  "default": ""
                },
                "fieldRef": {
                  "type": "string",
                  "description": "汇总字段名",
                  "default": ""
                },
                "basis": {
                  "type": "string",
                  "description": "汇总口径（FULL_RESULT 全量口径 / VISIBLE_ROWS 可见行口径）",
                  "default": ""
                }
              },
              "additionalProperties": false
            }
          },
          "clearSummaries": {
            "type": "boolean",
            "description": "是否清空全部汇总引用",
            "default": false
          }
        }
      },
      "required": [],
      "targetSpec": "每一步各自带 target：target.regionId 指向草稿里已有区块，target.ref 指向本次新增区块（ADD_SECTION 的 params.ref 起名）。",
      "resultSpec": "CapabilityOutcome：ok / message / detail（含 regionId、title、chartKind）；试做失败时返回具体错误（哪一步、哪个目标、哪条约束、为什么，外加字段路径、收到的类型、要求的类型、该填在哪、最小示例）",
      "states": [
        "WAITING_USER_CONFIRM",
        "USER_EDITING_TEMPLATE"
      ],
      "readOnly": false,
      "sideEffects": "只改**服务端草稿的隔离副本**；试做通过并由用户接受后，才在正式事务里写入草稿（版本 +1）。",
      "errors": [
        "能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=unknown_region",
        "能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=no_changes",
        "能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=unknown_output_ref",
        "参数不合法 → reason=invalid_params，不执行",
        "目标引用不认识 → reason=unknown_ref / missing_target / unknown_region"
      ],
      "limits": [
        "每一步都要有明确目标：target.regionId（草稿里已有区块）或 target.ref（本次新增区块的引用名）",
        "提交位置：submit_edit_plan 的 operations 里直接给一项 {\"op\": \"<能力名>\", \"target\": {...}, \"params\": {...}}",
        "包装要求：{\"op\": \"SET_TABLE_SUMMARIES\", \"target\": {\"regionId\": \"<regionId>\"}, \"params\": {}}",
        "最小结构示例（只给格式）：{\"op\": \"SET_TABLE_SUMMARIES\", \"target\": {\"regionId\": \"<regionId>\"}, \"params\": {}}"
      ],
      "code": {
        "registration": "app/services/edit_capabilities/base.py::register",
        "dispatch": "app/tools/submit_edit_plan_tool.py::run",
        "validation": "app/services/edit_capabilities/base.py::validate_params",
        "execution": "app/services/edit_capabilities/table_component.py::_set_table_summaries"
      }
    },
    {
      "id": "SET_TEXT_CONTENT",
      "group": "清单能力 / 设置统计与展示",
      "entryType": "operation",
      "status": "available",
      "purpose": "设置文本区块的正文：template（正文文本）、format（PLAIN / MARKDOWN）、headingLevel（目录层级，不是字号）、bindings（占位符 → 表达式，形如 {\"type\": \"FIELD\", \"fieldRef\": \"<字段名>\"}）。**卡片标题与正文互不相干** —— 这里只改正文。模板里没有绑定的占位符不会报错，但会列在结果里；{{roomName}} 与 {{logicalDate}} 仍按固定替换生效。 （**操作清单里的能力，不是独立 Tool**：模型在 submit_edit_plan 的 operations 里给出 op + target + params，由后端在草稿副本上执行。）**提交位置**：submit_edit_plan 的 operations 里直接给一项 {\"op\": \"<能力名>\", \"target\": {...}, \"params\": {...}}。**包装要求**：{\"op\": \"SET_TEXT_CONTENT\", \"target\": {\"regionId\": \"<regionId>\"}, \"params\": {}}。",
      "params": {
        "type": "object",
        "description": "SET_TEXT_CONTENT 的参数对象。",
        "properties": {
          "template": {
            "type": "string",
            "description": "正文文本（空 = 不改）；里面的 {{占位符}} 按 bindings 取值",
            "default": ""
          },
          "clearTemplate": {
            "type": "boolean",
            "description": "是否清空正文（true = 清空后再应用 template）",
            "default": false
          },
          "format": {
            "type": "string",
            "description": "文本格式：PLAIN（纯文本，保留换行）或 MARKDOWN（受控 Markdown）",
            "default": ""
          },
          "headingLevel": {
            "type": "integer",
            "description": "目录层级（0 起的整数；不填 = 不改）—— 不是字号"
          },
          "clearHeadingLevel": {
            "type": "boolean",
            "description": "是否清空目录层级",
            "default": false
          },
          "bindings": {
            "type": "object",
            "description": "占位符 → 表达式：键是占位符名，值形如 {\"type\": \"FIELD\", \"fieldRef\": \"<字段名>\"}；给的项按名字合并进去，没给的保持原样。",
            "additionalProperties": false
          },
          "clearBindings": {
            "type": "boolean",
            "description": "是否先清空全部占位符绑定（true = 清空后再应用 bindings）",
            "default": false
          }
        }
      },
      "required": [],
      "targetSpec": "每一步各自带 target：target.regionId 指向草稿里已有区块，target.ref 指向本次新增区块（ADD_SECTION 的 params.ref 起名）。",
      "resultSpec": "CapabilityOutcome：ok / message / detail（含 regionId、title、chartKind）；试做失败时返回具体错误（哪一步、哪个目标、哪条约束、为什么，外加字段路径、收到的类型、要求的类型、该填在哪、最小示例）",
      "states": [
        "WAITING_USER_CONFIRM",
        "USER_EDITING_TEMPLATE"
      ],
      "readOnly": false,
      "sideEffects": "只改**服务端草稿的隔离副本**；试做通过并由用户接受后，才在正式事务里写入草稿（版本 +1）。",
      "errors": [
        "能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=unsupported_text_format",
        "能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=invalid_heading_level",
        "参数不合法 → reason=invalid_params，不执行",
        "目标引用不认识 → reason=unknown_ref / missing_target / unknown_region"
      ],
      "limits": [
        "每一步都要有明确目标：target.regionId（草稿里已有区块）或 target.ref（本次新增区块的引用名）",
        "提交位置：submit_edit_plan 的 operations 里直接给一项 {\"op\": \"<能力名>\", \"target\": {...}, \"params\": {...}}",
        "包装要求：{\"op\": \"SET_TEXT_CONTENT\", \"target\": {\"regionId\": \"<regionId>\"}, \"params\": {}}",
        "最小结构示例（只给格式）：{\"op\": \"SET_TEXT_CONTENT\", \"target\": {\"regionId\": \"<regionId>\"}, \"params\": {}}"
      ],
      "code": {
        "registration": "app/services/edit_capabilities/base.py::register",
        "dispatch": "app/tools/submit_edit_plan_tool.py::run",
        "validation": "app/services/edit_capabilities/base.py::validate_params",
        "execution": "app/services/edit_capabilities/text_metric.py::_set_text_content"
      }
    },
    {
      "id": "SET_METRIC_ITEM",
      "group": "清单能力 / 设置统计与展示",
      "entryType": "operation",
      "status": "available",
      "purpose": "设置单指标卡：label（指标名称）、fieldRef（数值字段）、outputRef（覆盖区域默认输出，必须是本区域已登记的 output:<name>）、format（指标级显示格式）、comparisonFieldRef（对比字段）、iconRef（注册图标，只认注册清单里的键）。**改名称不碰数值、字段绑定、单位与格式。**对比字段与指标字段的单位必须一致，单位标签不许悄悄改。**指标卡 / 指标组的数字显示格式（精度、千分位、单位标签、空值文本）一律用本能力**：它写在指标项自己的 format 上；区域级的 SET_FORMAT 是整块卡片的格式，不是指标项的格式 —— 用它改指标卡会连带改掉这张卡上别的指标的单位。 （**操作清单里的能力，不是独立 Tool**：模型在 submit_edit_plan 的 operations 里给出 op + target + params，由后端在草稿副本上执行。）**提交位置**：submit_edit_plan 的 operations 里直接给一项 {\"op\": \"<能力名>\", \"target\": {...}, \"params\": {...}}。**包装要求**：{\"op\": \"SET_METRIC_ITEM\", \"target\": {\"regionId\": \"<regionId>\"}, \"params\": {}}。",
      "params": {
        "type": "object",
        "description": "SET_METRIC_ITEM 的参数对象。",
        "properties": {
          "label": {
            "type": "string",
            "description": "指标名称（空 = 不改）；改名字不碰数值、字段、单位与格式",
            "default": ""
          },
          "fieldRef": {
            "type": "string",
            "description": "数值字段名（必须已登记或属于当前模拟字段目录；空 = 不改）",
            "default": ""
          },
          "outputRef": {
            "type": "string",
            "description": "覆盖区域默认输出的引用（output:<name>，name 必须是本区域已登记的计算输出）",
            "default": ""
          },
          "clearOutputRef": {
            "type": "boolean",
            "description": "是否清空输出覆盖（回到区域默认输出）",
            "default": false
          },
          "format": {
            "type": "object",
            "description": "指标的显示格式（空 = 不改）：kind / precision / unitLabel / thousandSeparator / dateTimePattern / nullText / roundingMode",
            "additionalProperties": false
          },
          "clearFormat": {
            "type": "boolean",
            "description": "是否清空指标格式",
            "default": false
          },
          "comparisonFieldRef": {
            "type": "string",
            "description": "对比字段名（必须已登记；单位要与指标字段一致）",
            "default": ""
          },
          "clearComparisonFieldRef": {
            "type": "boolean",
            "description": "是否清空对比字段",
            "default": false
          },
          "iconRef": {
            "type": "string",
            "description": "注册图标引用（只认注册清单里的键；不接受图片 URL）",
            "default": ""
          },
          "clearIconRef": {
            "type": "boolean",
            "description": "是否清空图标",
            "default": false
          }
        }
      },
      "required": [],
      "targetSpec": "每一步各自带 target：target.regionId 指向草稿里已有区块，target.ref 指向本次新增区块（ADD_SECTION 的 params.ref 起名）。",
      "resultSpec": "CapabilityOutcome：ok / message / detail（含 regionId、title、chartKind）；试做失败时返回具体错误（哪一步、哪个目标、哪条约束、为什么，外加字段路径、收到的类型、要求的类型、该填在哪、最小示例）",
      "states": [
        "WAITING_USER_CONFIRM",
        "USER_EDITING_TEMPLATE"
      ],
      "readOnly": false,
      "sideEffects": "只改**服务端草稿的隔离副本**；试做通过并由用户接受后，才在正式事务里写入草稿（版本 +1）。",
      "errors": [
        "能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=incompatible_unit",
        "参数不合法 → reason=invalid_params，不执行",
        "目标引用不认识 → reason=unknown_ref / missing_target / unknown_region"
      ],
      "limits": [
        "每一步都要有明确目标：target.regionId（草稿里已有区块）或 target.ref（本次新增区块的引用名）",
        "提交位置：submit_edit_plan 的 operations 里直接给一项 {\"op\": \"<能力名>\", \"target\": {...}, \"params\": {...}}",
        "包装要求：{\"op\": \"SET_METRIC_ITEM\", \"target\": {\"regionId\": \"<regionId>\"}, \"params\": {}}",
        "最小结构示例（只给格式）：{\"op\": \"SET_METRIC_ITEM\", \"target\": {\"regionId\": \"<regionId>\"}, \"params\": {}}"
      ],
      "code": {
        "registration": "app/services/edit_capabilities/base.py::register",
        "dispatch": "app/tools/submit_edit_plan_tool.py::run",
        "validation": "app/services/edit_capabilities/base.py::validate_params",
        "execution": "app/services/edit_capabilities/text_metric.py::_set_metric_item"
      }
    },
    {
      "id": "SET_METRIC_GROUP",
      "group": "清单能力 / 设置统计与展示",
      "entryType": "operation",
      "status": "available",
      "purpose": "设置多指标组：metrics 是该组的**完整**指标项清单（每项形如 {'id': 'm_1', 'label': '总耗电量', 'fieldRef': 'totalEnergyKwh', 'format': {'kind': 'NUMBER'}}）；带 id 的项按 id 就地更新，不带 id 的是新项，清单里没有的项从展示清单里移除（**不删任何源数据 / 输出 / 列 / 区块**）。**不传 metrics = 只改排版，组里已有的指标项原样保留** —— 只改列数 / 间距这类要求不要顺手把指标项清成 0 项。arrangement 设组内排版（mode / columns / direction / gap）。create=true 可把指标卡（或正文为空的文本区块）改成多指标组。 （**操作清单里的能力，不是独立 Tool**：模型在 submit_edit_plan 的 operations 里给出 op + target + params，由后端在草稿副本上执行。）**提交位置**：submit_edit_plan 的 operations 里直接给一项 {\"op\": \"<能力名>\", \"target\": {...}, \"params\": {...}}。**包装要求**：{\"op\": \"SET_METRIC_GROUP\", \"target\": {\"regionId\": \"<regionId>\"}, \"params\": {}}。",
      "params": {
        "type": "object",
        "description": "SET_METRIC_GROUP 的参数对象。",
        "properties": {
          "metrics": {
            "type": "array",
            "description": "该指标组的**完整**指标项清单：每一项形如 {\"id\": \"m_1\", \"label\": \"总耗电量\", \"fieldRef\": \"totalEnergyKwh\", \"format\": {\"kind\": \"NUMBER\", \"precision\": 1}}；带 id 的项按 id 就地更新（id 必须已存在），不带 id 的是新项；清单里没有的项从展示清单里移除（**不删任何源数据 / 输出 / 列 / 区块**）。**不传这一项 = 只改组内排版，组里已有的指标项原样保留**（缺省不等于「清空」——清空会把用户没提的指标项悄悄抹掉）。",
            "items": {
              "type": "object"
            }
          },
          "arrangement": {
            "type": "object",
            "description": "组内排版：{'mode': 'GRID'|'FLOW'|'ABSOLUTE', 'columns': 1..12, 'direction': 'VERTICAL'|'HORIZONTAL', 'gap': {'value': 0..96, 'unit': 'PX'|'PERCENT'}}",
            "additionalProperties": false
          },
          "create": {
            "type": "boolean",
            "description": "是否把目标区块改成多指标组（指标卡 → 指标组；正文为空的文本区块 → 指标组）",
            "default": false
          }
        }
      },
      "required": [],
      "targetSpec": "每一步各自带 target：target.regionId 指向草稿里已有区块，target.ref 指向本次新增区块（ADD_SECTION 的 params.ref 起名）。",
      "resultSpec": "CapabilityOutcome：ok / message / detail（含 regionId、title、chartKind）；试做失败时返回具体错误（哪一步、哪个目标、哪条约束、为什么，外加字段路径、收到的类型、要求的类型、该填在哪、最小示例）",
      "states": [
        "WAITING_USER_CONFIRM",
        "USER_EDITING_TEMPLATE"
      ],
      "readOnly": false,
      "sideEffects": "只改**服务端草稿的隔离副本**；试做通过并由用户接受后，才在正式事务里写入草稿（版本 +1）。",
      "errors": [
        "能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=incompatible_component_type",
        "能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=invalid_metric_item",
        "能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=unknown_metric_id",
        "能力失败抛 CapabilityError（step / op / target / constraint / reason），reason=incompatible_unit",
        "参数不合法 → reason=invalid_params，不执行",
        "目标引用不认识 → reason=unknown_ref / missing_target / unknown_region"
      ],
      "limits": [
        "每一步都要有明确目标：target.regionId（草稿里已有区块）或 target.ref（本次新增区块的引用名）",
        "提交位置：submit_edit_plan 的 operations 里直接给一项 {\"op\": \"<能力名>\", \"target\": {...}, \"params\": {...}}",
        "包装要求：{\"op\": \"SET_METRIC_GROUP\", \"target\": {\"regionId\": \"<regionId>\"}, \"params\": {}}",
        "最小结构示例（只给格式）：{\"op\": \"SET_METRIC_GROUP\", \"target\": {\"regionId\": \"<regionId>\"}, \"params\": {}}"
      ],
      "code": {
        "registration": "app/services/edit_capabilities/base.py::register",
        "dispatch": "app/tools/submit_edit_plan_tool.py::run",
        "validation": "app/services/edit_capabilities/base.py::validate_params",
        "execution": "app/services/edit_capabilities/text_metric.py::_set_metric_group"
      }
    },
    {
      "id": "RENAME_REPORT",
      "group": "旧意图 / 报表与区块增删改名",
      "entryType": "legacy_intent",
      "status": "available",
      "purpose": "改报表名称（不动任何区块）。 （**旧格式入口，不是独立 Tool**：模型在 submit_edit_plan 的 intent / target / operation 里给出，由 template_edit_engine.TemplateEditEngine.apply 执行。）",
      "params": {
        "$defs": {
          "EditMetricAddition": {
            "description": "要**追加**到某个已有区块里的一个指标。\n\n「追加」与「替换」是两种不同的意图：追加**保留**该区块原有指标，\n把新指标加进去；替换才把原来的换掉。\n\n每个待追加指标至少能表达：用户原话意图、显示名、单位、展示模式。",
            "properties": {
              "intent": {
                "default": "",
                "title": "Intent",
                "type": "string"
              },
              "label": {
                "default": "",
                "title": "Label",
                "type": "string"
              },
              "unit": {
                "default": "",
                "title": "Unit",
                "type": "string"
              },
              "displayMode": {
                "default": "simulated",
                "title": "Displaymode",
                "type": "string"
              },
              "metricRef": {
                "anyOf": [
                  {
                    "type": "string"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Metricref"
              },
              "metricCandidate": {
                "anyOf": [
                  {
                    "additionalProperties": true,
                    "type": "object"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Metriccandidate"
              },
              "note": {
                "default": "",
                "title": "Note",
                "type": "string"
              }
            },
            "title": "EditMetricAddition",
            "type": "object"
          },
          "EditMetricDeletion": {
            "description": "要**删除**的一个指标 —— 卡片内的**一个指标实例**。\n\n「删掉某个指标」与「删掉整张卡片」是两件事（契约表 §4.3）：目标必须\n指向卡片内**真实存在**的那一项指标。\n\n身份用**稳定标识**记（``metricKey`` / ``simulatedId`` / ``fieldRef``），\n**不记数组下标** —— 删掉中间一项之后下标会漂移；``label`` 只是给人看\n的显示名，**不作为定位依据**（同名指标要澄清，不能删第一条匹配的）。",
            "properties": {
              "metricKey": {
                "default": "",
                "title": "Metrickey",
                "type": "string"
              },
              "simulatedId": {
                "anyOf": [
                  {
                    "type": "string"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Simulatedid"
              },
              "fieldRef": {
                "anyOf": [
                  {
                    "type": "string"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Fieldref"
              },
              "label": {
                "default": "",
                "title": "Label",
                "type": "string"
              },
              "isPrimary": {
                "default": false,
                "title": "Isprimary",
                "type": "boolean"
              },
              "intent": {
                "default": "",
                "title": "Intent",
                "type": "string"
              }
            },
            "title": "EditMetricDeletion",
            "type": "object"
          }
        },
        "description": "具体改成什么。字段按 intent 不同用到的子集不同；这里统一松结构承载。",
        "properties": {
          "newTitle": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Newtitle"
          },
          "componentType": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Componenttype"
          },
          "chartKind": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Chartkind"
          },
          "sectionTitle": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Sectiontitle"
          },
          "insertAfterRegionId": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Insertafterregionid"
          },
          "sourceRef": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Sourceref"
          },
          "sourceVersion": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Sourceversion"
          },
          "fieldMappingRequired": {
            "anyOf": [
              {
                "type": "boolean"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Fieldmappingrequired"
          },
          "metricRef": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Metricref"
          },
          "metricVersion": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Metricversion"
          },
          "metricIntent": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Metricintent"
          },
          "metricCandidate": {
            "anyOf": [
              {
                "additionalProperties": true,
                "type": "object"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Metriccandidate"
          },
          "baseDefinitionRevision": {
            "anyOf": [
              {
                "type": "integer"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Basedefinitionrevision"
          },
          "addMetrics": {
            "items": {
              "$ref": "#/$defs/EditMetricAddition"
            },
            "title": "Addmetrics",
            "type": "array"
          },
          "deleteMetrics": {
            "items": {
              "$ref": "#/$defs/EditMetricDeletion"
            },
            "title": "Deletemetrics",
            "type": "array"
          },
          "componentSelection": {
            "anyOf": [
              {
                "additionalProperties": true,
                "type": "object"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Componentselection"
          },
          "timeWindow": {
            "anyOf": [
              {
                "additionalProperties": true,
                "type": "object"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Timewindow"
          },
          "timeWindowLabel": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Timewindowlabel"
          },
          "grain": {
            "anyOf": [
              {
                "additionalProperties": true,
                "type": "object"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Grain"
          },
          "supported": {
            "anyOf": [
              {
                "type": "boolean"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Supported"
          },
          "supportedGrains": {
            "items": {
              "type": "string"
            },
            "title": "Supportedgrains",
            "type": "array"
          },
          "topN": {
            "anyOf": [
              {
                "type": "integer"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Topn"
          },
          "sortField": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Sortfield"
          },
          "sortDirection": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Sortdirection"
          },
          "dataShape": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Datashape"
          },
          "displayOptions": {
            "items": {
              "additionalProperties": true,
              "type": "object"
            },
            "title": "Displayoptions",
            "type": "array"
          },
          "disabledOptions": {
            "items": {
              "additionalProperties": true,
              "type": "object"
            },
            "title": "Disabledoptions",
            "type": "array"
          },
          "markdownPreview": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Markdownpreview"
          },
          "reason": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Reason"
          },
          "presentationOperations": {
            "items": {
              "additionalProperties": true,
              "type": "object"
            },
            "title": "Presentationoperations",
            "type": "array"
          },
          "themeOperations": {
            "items": {
              "additionalProperties": true,
              "type": "object"
            },
            "title": "Themeoperations",
            "type": "array"
          }
        },
        "title": "EditOperation",
        "type": "object"
      },
      "required": [],
      "targetSpec": "target.regionId 或 target.matchedText 指明要改哪张卡片；组合修改用 operations 列表（旧格式每步共用顶层 target）",
      "resultSpec": "EditResult：draftAfterEdit / changeSet / reply / markdownPreview / needsClarification；形成待确认候选，用户接受后才正式写入",
      "states": [
        "WAITING_USER_CONFIRM",
        "USER_EDITING_TEMPLATE"
      ],
      "readOnly": false,
      "sideEffects": "在草稿副本上执行；通过后形成待确认候选，用户接受时才在正式事务里写入草稿。",
      "errors": [
        "目标不存在或不可唯一确定 → 返回 needsClarification + clarificationQuestion（草稿不变）",
        "候选基于旧版本草稿 → 拒绝（stale_edit_plan）",
        "依据的生效组合已变 → 拒绝（combination_conflict）",
        "数据源 / 指标不在当前授权范围 → 拒绝（incompatible_fields）"
      ],
      "limits": [
        "单步修改只针对一个区块；要一次改多处请用能力格式 operations"
      ],
      "code": {
        "registration": "app/schemas/edit_plan.py::EditIntent",
        "dispatch": "app/services/agent_service.py::AgentService._try_handle_template_edit",
        "validation": "app/schemas/edit_plan.py::EditPlan",
        "execution": "app/services/template_edit_engine.py::TemplateEditEngine._apply_rename_report"
      }
    },
    {
      "id": "DELETE_SECTION",
      "group": "旧意图 / 报表与区块增删改名",
      "entryType": "legacy_intent",
      "status": "available",
      "purpose": "删掉一整张卡片（区块）。 （**旧格式入口，不是独立 Tool**：模型在 submit_edit_plan 的 intent / target / operation 里给出，由 template_edit_engine.TemplateEditEngine.apply 执行。）",
      "params": {
        "$defs": {
          "EditMetricAddition": {
            "description": "要**追加**到某个已有区块里的一个指标。\n\n「追加」与「替换」是两种不同的意图：追加**保留**该区块原有指标，\n把新指标加进去；替换才把原来的换掉。\n\n每个待追加指标至少能表达：用户原话意图、显示名、单位、展示模式。",
            "properties": {
              "intent": {
                "default": "",
                "title": "Intent",
                "type": "string"
              },
              "label": {
                "default": "",
                "title": "Label",
                "type": "string"
              },
              "unit": {
                "default": "",
                "title": "Unit",
                "type": "string"
              },
              "displayMode": {
                "default": "simulated",
                "title": "Displaymode",
                "type": "string"
              },
              "metricRef": {
                "anyOf": [
                  {
                    "type": "string"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Metricref"
              },
              "metricCandidate": {
                "anyOf": [
                  {
                    "additionalProperties": true,
                    "type": "object"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Metriccandidate"
              },
              "note": {
                "default": "",
                "title": "Note",
                "type": "string"
              }
            },
            "title": "EditMetricAddition",
            "type": "object"
          },
          "EditMetricDeletion": {
            "description": "要**删除**的一个指标 —— 卡片内的**一个指标实例**。\n\n「删掉某个指标」与「删掉整张卡片」是两件事（契约表 §4.3）：目标必须\n指向卡片内**真实存在**的那一项指标。\n\n身份用**稳定标识**记（``metricKey`` / ``simulatedId`` / ``fieldRef``），\n**不记数组下标** —— 删掉中间一项之后下标会漂移；``label`` 只是给人看\n的显示名，**不作为定位依据**（同名指标要澄清，不能删第一条匹配的）。",
            "properties": {
              "metricKey": {
                "default": "",
                "title": "Metrickey",
                "type": "string"
              },
              "simulatedId": {
                "anyOf": [
                  {
                    "type": "string"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Simulatedid"
              },
              "fieldRef": {
                "anyOf": [
                  {
                    "type": "string"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Fieldref"
              },
              "label": {
                "default": "",
                "title": "Label",
                "type": "string"
              },
              "isPrimary": {
                "default": false,
                "title": "Isprimary",
                "type": "boolean"
              },
              "intent": {
                "default": "",
                "title": "Intent",
                "type": "string"
              }
            },
            "title": "EditMetricDeletion",
            "type": "object"
          }
        },
        "description": "具体改成什么。字段按 intent 不同用到的子集不同；这里统一松结构承载。",
        "properties": {
          "newTitle": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Newtitle"
          },
          "componentType": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Componenttype"
          },
          "chartKind": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Chartkind"
          },
          "sectionTitle": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Sectiontitle"
          },
          "insertAfterRegionId": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Insertafterregionid"
          },
          "sourceRef": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Sourceref"
          },
          "sourceVersion": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Sourceversion"
          },
          "fieldMappingRequired": {
            "anyOf": [
              {
                "type": "boolean"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Fieldmappingrequired"
          },
          "metricRef": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Metricref"
          },
          "metricVersion": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Metricversion"
          },
          "metricIntent": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Metricintent"
          },
          "metricCandidate": {
            "anyOf": [
              {
                "additionalProperties": true,
                "type": "object"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Metriccandidate"
          },
          "baseDefinitionRevision": {
            "anyOf": [
              {
                "type": "integer"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Basedefinitionrevision"
          },
          "addMetrics": {
            "items": {
              "$ref": "#/$defs/EditMetricAddition"
            },
            "title": "Addmetrics",
            "type": "array"
          },
          "deleteMetrics": {
            "items": {
              "$ref": "#/$defs/EditMetricDeletion"
            },
            "title": "Deletemetrics",
            "type": "array"
          },
          "componentSelection": {
            "anyOf": [
              {
                "additionalProperties": true,
                "type": "object"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Componentselection"
          },
          "timeWindow": {
            "anyOf": [
              {
                "additionalProperties": true,
                "type": "object"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Timewindow"
          },
          "timeWindowLabel": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Timewindowlabel"
          },
          "grain": {
            "anyOf": [
              {
                "additionalProperties": true,
                "type": "object"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Grain"
          },
          "supported": {
            "anyOf": [
              {
                "type": "boolean"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Supported"
          },
          "supportedGrains": {
            "items": {
              "type": "string"
            },
            "title": "Supportedgrains",
            "type": "array"
          },
          "topN": {
            "anyOf": [
              {
                "type": "integer"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Topn"
          },
          "sortField": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Sortfield"
          },
          "sortDirection": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Sortdirection"
          },
          "dataShape": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Datashape"
          },
          "displayOptions": {
            "items": {
              "additionalProperties": true,
              "type": "object"
            },
            "title": "Displayoptions",
            "type": "array"
          },
          "disabledOptions": {
            "items": {
              "additionalProperties": true,
              "type": "object"
            },
            "title": "Disabledoptions",
            "type": "array"
          },
          "markdownPreview": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Markdownpreview"
          },
          "reason": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Reason"
          },
          "presentationOperations": {
            "items": {
              "additionalProperties": true,
              "type": "object"
            },
            "title": "Presentationoperations",
            "type": "array"
          },
          "themeOperations": {
            "items": {
              "additionalProperties": true,
              "type": "object"
            },
            "title": "Themeoperations",
            "type": "array"
          }
        },
        "title": "EditOperation",
        "type": "object"
      },
      "required": [],
      "targetSpec": "target.regionId 或 target.matchedText 指明要改哪张卡片；组合修改用 operations 列表（旧格式每步共用顶层 target）",
      "resultSpec": "EditResult：draftAfterEdit / changeSet / reply / markdownPreview / needsClarification；形成待确认候选，用户接受后才正式写入",
      "states": [
        "WAITING_USER_CONFIRM",
        "USER_EDITING_TEMPLATE"
      ],
      "readOnly": false,
      "sideEffects": "在草稿副本上执行；通过后形成待确认候选，用户接受时才在正式事务里写入草稿。",
      "errors": [
        "目标不存在或不可唯一确定 → 返回 needsClarification + clarificationQuestion（草稿不变）",
        "候选基于旧版本草稿 → 拒绝（stale_edit_plan）",
        "依据的生效组合已变 → 拒绝（combination_conflict）",
        "数据源 / 指标不在当前授权范围 → 拒绝（incompatible_fields）"
      ],
      "limits": [
        "单步修改只针对一个区块；要一次改多处请用能力格式 operations"
      ],
      "code": {
        "registration": "app/schemas/edit_plan.py::EditIntent",
        "dispatch": "app/services/agent_service.py::AgentService._try_handle_template_edit",
        "validation": "app/schemas/edit_plan.py::EditPlan",
        "execution": "app/services/template_edit_engine.py::TemplateEditEngine._apply_delete"
      }
    },
    {
      "id": "DELETE_METRIC",
      "group": "旧意图 / 指标增删替换",
      "entryType": "legacy_intent",
      "status": "available",
      "purpose": "在已有卡片里删掉某一项指标（卡片本身保留）。 （**旧格式入口，不是独立 Tool**：模型在 submit_edit_plan 的 intent / target / operation 里给出，由 template_edit_engine.TemplateEditEngine.apply 执行。）",
      "params": {
        "$defs": {
          "EditMetricAddition": {
            "description": "要**追加**到某个已有区块里的一个指标。\n\n「追加」与「替换」是两种不同的意图：追加**保留**该区块原有指标，\n把新指标加进去；替换才把原来的换掉。\n\n每个待追加指标至少能表达：用户原话意图、显示名、单位、展示模式。",
            "properties": {
              "intent": {
                "default": "",
                "title": "Intent",
                "type": "string"
              },
              "label": {
                "default": "",
                "title": "Label",
                "type": "string"
              },
              "unit": {
                "default": "",
                "title": "Unit",
                "type": "string"
              },
              "displayMode": {
                "default": "simulated",
                "title": "Displaymode",
                "type": "string"
              },
              "metricRef": {
                "anyOf": [
                  {
                    "type": "string"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Metricref"
              },
              "metricCandidate": {
                "anyOf": [
                  {
                    "additionalProperties": true,
                    "type": "object"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Metriccandidate"
              },
              "note": {
                "default": "",
                "title": "Note",
                "type": "string"
              }
            },
            "title": "EditMetricAddition",
            "type": "object"
          },
          "EditMetricDeletion": {
            "description": "要**删除**的一个指标 —— 卡片内的**一个指标实例**。\n\n「删掉某个指标」与「删掉整张卡片」是两件事（契约表 §4.3）：目标必须\n指向卡片内**真实存在**的那一项指标。\n\n身份用**稳定标识**记（``metricKey`` / ``simulatedId`` / ``fieldRef``），\n**不记数组下标** —— 删掉中间一项之后下标会漂移；``label`` 只是给人看\n的显示名，**不作为定位依据**（同名指标要澄清，不能删第一条匹配的）。",
            "properties": {
              "metricKey": {
                "default": "",
                "title": "Metrickey",
                "type": "string"
              },
              "simulatedId": {
                "anyOf": [
                  {
                    "type": "string"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Simulatedid"
              },
              "fieldRef": {
                "anyOf": [
                  {
                    "type": "string"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Fieldref"
              },
              "label": {
                "default": "",
                "title": "Label",
                "type": "string"
              },
              "isPrimary": {
                "default": false,
                "title": "Isprimary",
                "type": "boolean"
              },
              "intent": {
                "default": "",
                "title": "Intent",
                "type": "string"
              }
            },
            "title": "EditMetricDeletion",
            "type": "object"
          }
        },
        "description": "具体改成什么。字段按 intent 不同用到的子集不同；这里统一松结构承载。",
        "properties": {
          "newTitle": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Newtitle"
          },
          "componentType": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Componenttype"
          },
          "chartKind": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Chartkind"
          },
          "sectionTitle": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Sectiontitle"
          },
          "insertAfterRegionId": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Insertafterregionid"
          },
          "sourceRef": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Sourceref"
          },
          "sourceVersion": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Sourceversion"
          },
          "fieldMappingRequired": {
            "anyOf": [
              {
                "type": "boolean"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Fieldmappingrequired"
          },
          "metricRef": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Metricref"
          },
          "metricVersion": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Metricversion"
          },
          "metricIntent": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Metricintent"
          },
          "metricCandidate": {
            "anyOf": [
              {
                "additionalProperties": true,
                "type": "object"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Metriccandidate"
          },
          "baseDefinitionRevision": {
            "anyOf": [
              {
                "type": "integer"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Basedefinitionrevision"
          },
          "addMetrics": {
            "items": {
              "$ref": "#/$defs/EditMetricAddition"
            },
            "title": "Addmetrics",
            "type": "array"
          },
          "deleteMetrics": {
            "items": {
              "$ref": "#/$defs/EditMetricDeletion"
            },
            "title": "Deletemetrics",
            "type": "array"
          },
          "componentSelection": {
            "anyOf": [
              {
                "additionalProperties": true,
                "type": "object"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Componentselection"
          },
          "timeWindow": {
            "anyOf": [
              {
                "additionalProperties": true,
                "type": "object"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Timewindow"
          },
          "timeWindowLabel": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Timewindowlabel"
          },
          "grain": {
            "anyOf": [
              {
                "additionalProperties": true,
                "type": "object"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Grain"
          },
          "supported": {
            "anyOf": [
              {
                "type": "boolean"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Supported"
          },
          "supportedGrains": {
            "items": {
              "type": "string"
            },
            "title": "Supportedgrains",
            "type": "array"
          },
          "topN": {
            "anyOf": [
              {
                "type": "integer"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Topn"
          },
          "sortField": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Sortfield"
          },
          "sortDirection": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Sortdirection"
          },
          "dataShape": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Datashape"
          },
          "displayOptions": {
            "items": {
              "additionalProperties": true,
              "type": "object"
            },
            "title": "Displayoptions",
            "type": "array"
          },
          "disabledOptions": {
            "items": {
              "additionalProperties": true,
              "type": "object"
            },
            "title": "Disabledoptions",
            "type": "array"
          },
          "markdownPreview": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Markdownpreview"
          },
          "reason": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Reason"
          },
          "presentationOperations": {
            "items": {
              "additionalProperties": true,
              "type": "object"
            },
            "title": "Presentationoperations",
            "type": "array"
          },
          "themeOperations": {
            "items": {
              "additionalProperties": true,
              "type": "object"
            },
            "title": "Themeoperations",
            "type": "array"
          }
        },
        "title": "EditOperation",
        "type": "object"
      },
      "required": [],
      "targetSpec": "target.regionId 或 target.matchedText 指明要改哪张卡片；组合修改用 operations 列表（旧格式每步共用顶层 target）",
      "resultSpec": "EditResult：draftAfterEdit / changeSet / reply / markdownPreview / needsClarification；形成待确认候选，用户接受后才正式写入",
      "states": [
        "WAITING_USER_CONFIRM",
        "USER_EDITING_TEMPLATE"
      ],
      "readOnly": false,
      "sideEffects": "在草稿副本上执行；通过后形成待确认候选，用户接受时才在正式事务里写入草稿。",
      "errors": [
        "目标不存在或不可唯一确定 → 返回 needsClarification + clarificationQuestion（草稿不变）",
        "候选基于旧版本草稿 → 拒绝（stale_edit_plan）",
        "依据的生效组合已变 → 拒绝（combination_conflict）",
        "数据源 / 指标不在当前授权范围 → 拒绝（incompatible_fields）"
      ],
      "limits": [
        "只删卡片内的指标项，卡片本身保留；目标不唯一要追问",
        "单步修改只针对一个区块；要一次改多处请用能力格式 operations"
      ],
      "code": {
        "registration": "app/schemas/edit_plan.py::EditIntent",
        "dispatch": "app/services/agent_service.py::AgentService._try_handle_template_edit",
        "validation": "app/schemas/edit_plan.py::EditPlan",
        "execution": "app/services/template_edit_engine.py::TemplateEditEngine._apply_delete_metric"
      }
    },
    {
      "id": "RENAME_SECTION",
      "group": "旧意图 / 报表与区块增删改名",
      "entryType": "legacy_intent",
      "status": "available",
      "purpose": "改某张卡片的标题。 （**旧格式入口，不是独立 Tool**：模型在 submit_edit_plan 的 intent / target / operation 里给出，由 template_edit_engine.TemplateEditEngine.apply 执行。）",
      "params": {
        "$defs": {
          "EditMetricAddition": {
            "description": "要**追加**到某个已有区块里的一个指标。\n\n「追加」与「替换」是两种不同的意图：追加**保留**该区块原有指标，\n把新指标加进去；替换才把原来的换掉。\n\n每个待追加指标至少能表达：用户原话意图、显示名、单位、展示模式。",
            "properties": {
              "intent": {
                "default": "",
                "title": "Intent",
                "type": "string"
              },
              "label": {
                "default": "",
                "title": "Label",
                "type": "string"
              },
              "unit": {
                "default": "",
                "title": "Unit",
                "type": "string"
              },
              "displayMode": {
                "default": "simulated",
                "title": "Displaymode",
                "type": "string"
              },
              "metricRef": {
                "anyOf": [
                  {
                    "type": "string"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Metricref"
              },
              "metricCandidate": {
                "anyOf": [
                  {
                    "additionalProperties": true,
                    "type": "object"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Metriccandidate"
              },
              "note": {
                "default": "",
                "title": "Note",
                "type": "string"
              }
            },
            "title": "EditMetricAddition",
            "type": "object"
          },
          "EditMetricDeletion": {
            "description": "要**删除**的一个指标 —— 卡片内的**一个指标实例**。\n\n「删掉某个指标」与「删掉整张卡片」是两件事（契约表 §4.3）：目标必须\n指向卡片内**真实存在**的那一项指标。\n\n身份用**稳定标识**记（``metricKey`` / ``simulatedId`` / ``fieldRef``），\n**不记数组下标** —— 删掉中间一项之后下标会漂移；``label`` 只是给人看\n的显示名，**不作为定位依据**（同名指标要澄清，不能删第一条匹配的）。",
            "properties": {
              "metricKey": {
                "default": "",
                "title": "Metrickey",
                "type": "string"
              },
              "simulatedId": {
                "anyOf": [
                  {
                    "type": "string"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Simulatedid"
              },
              "fieldRef": {
                "anyOf": [
                  {
                    "type": "string"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Fieldref"
              },
              "label": {
                "default": "",
                "title": "Label",
                "type": "string"
              },
              "isPrimary": {
                "default": false,
                "title": "Isprimary",
                "type": "boolean"
              },
              "intent": {
                "default": "",
                "title": "Intent",
                "type": "string"
              }
            },
            "title": "EditMetricDeletion",
            "type": "object"
          }
        },
        "description": "具体改成什么。字段按 intent 不同用到的子集不同；这里统一松结构承载。",
        "properties": {
          "newTitle": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Newtitle"
          },
          "componentType": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Componenttype"
          },
          "chartKind": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Chartkind"
          },
          "sectionTitle": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Sectiontitle"
          },
          "insertAfterRegionId": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Insertafterregionid"
          },
          "sourceRef": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Sourceref"
          },
          "sourceVersion": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Sourceversion"
          },
          "fieldMappingRequired": {
            "anyOf": [
              {
                "type": "boolean"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Fieldmappingrequired"
          },
          "metricRef": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Metricref"
          },
          "metricVersion": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Metricversion"
          },
          "metricIntent": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Metricintent"
          },
          "metricCandidate": {
            "anyOf": [
              {
                "additionalProperties": true,
                "type": "object"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Metriccandidate"
          },
          "baseDefinitionRevision": {
            "anyOf": [
              {
                "type": "integer"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Basedefinitionrevision"
          },
          "addMetrics": {
            "items": {
              "$ref": "#/$defs/EditMetricAddition"
            },
            "title": "Addmetrics",
            "type": "array"
          },
          "deleteMetrics": {
            "items": {
              "$ref": "#/$defs/EditMetricDeletion"
            },
            "title": "Deletemetrics",
            "type": "array"
          },
          "componentSelection": {
            "anyOf": [
              {
                "additionalProperties": true,
                "type": "object"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Componentselection"
          },
          "timeWindow": {
            "anyOf": [
              {
                "additionalProperties": true,
                "type": "object"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Timewindow"
          },
          "timeWindowLabel": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Timewindowlabel"
          },
          "grain": {
            "anyOf": [
              {
                "additionalProperties": true,
                "type": "object"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Grain"
          },
          "supported": {
            "anyOf": [
              {
                "type": "boolean"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Supported"
          },
          "supportedGrains": {
            "items": {
              "type": "string"
            },
            "title": "Supportedgrains",
            "type": "array"
          },
          "topN": {
            "anyOf": [
              {
                "type": "integer"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Topn"
          },
          "sortField": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Sortfield"
          },
          "sortDirection": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Sortdirection"
          },
          "dataShape": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Datashape"
          },
          "displayOptions": {
            "items": {
              "additionalProperties": true,
              "type": "object"
            },
            "title": "Displayoptions",
            "type": "array"
          },
          "disabledOptions": {
            "items": {
              "additionalProperties": true,
              "type": "object"
            },
            "title": "Disabledoptions",
            "type": "array"
          },
          "markdownPreview": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Markdownpreview"
          },
          "reason": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Reason"
          },
          "presentationOperations": {
            "items": {
              "additionalProperties": true,
              "type": "object"
            },
            "title": "Presentationoperations",
            "type": "array"
          },
          "themeOperations": {
            "items": {
              "additionalProperties": true,
              "type": "object"
            },
            "title": "Themeoperations",
            "type": "array"
          }
        },
        "title": "EditOperation",
        "type": "object"
      },
      "required": [],
      "targetSpec": "target.regionId 或 target.matchedText 指明要改哪张卡片；组合修改用 operations 列表（旧格式每步共用顶层 target）",
      "resultSpec": "EditResult：draftAfterEdit / changeSet / reply / markdownPreview / needsClarification；形成待确认候选，用户接受后才正式写入",
      "states": [
        "WAITING_USER_CONFIRM",
        "USER_EDITING_TEMPLATE"
      ],
      "readOnly": false,
      "sideEffects": "在草稿副本上执行；通过后形成待确认候选，用户接受时才在正式事务里写入草稿。",
      "errors": [
        "目标不存在或不可唯一确定 → 返回 needsClarification + clarificationQuestion（草稿不变）",
        "候选基于旧版本草稿 → 拒绝（stale_edit_plan）",
        "依据的生效组合已变 → 拒绝（combination_conflict）",
        "数据源 / 指标不在当前授权范围 → 拒绝（incompatible_fields）"
      ],
      "limits": [
        "单步修改只针对一个区块；要一次改多处请用能力格式 operations"
      ],
      "code": {
        "registration": "app/schemas/edit_plan.py::EditIntent",
        "dispatch": "app/services/agent_service.py::AgentService._try_handle_template_edit",
        "validation": "app/schemas/edit_plan.py::EditPlan",
        "execution": "app/services/template_edit_engine.py::TemplateEditEngine._apply_rename"
      }
    },
    {
      "id": "ADD_SECTION",
      "group": "旧意图 / 报表与区块增删改名",
      "entryType": "legacy_intent",
      "status": "available",
      "purpose": "新增一张卡片（旧格式入口；能力格式请用 ADD_SECTION 能力）。 （**旧格式入口，不是独立 Tool**：模型在 submit_edit_plan 的 intent / target / operation 里给出，由 template_edit_engine.TemplateEditEngine.apply 执行。）",
      "params": {
        "$defs": {
          "EditMetricAddition": {
            "description": "要**追加**到某个已有区块里的一个指标。\n\n「追加」与「替换」是两种不同的意图：追加**保留**该区块原有指标，\n把新指标加进去；替换才把原来的换掉。\n\n每个待追加指标至少能表达：用户原话意图、显示名、单位、展示模式。",
            "properties": {
              "intent": {
                "default": "",
                "title": "Intent",
                "type": "string"
              },
              "label": {
                "default": "",
                "title": "Label",
                "type": "string"
              },
              "unit": {
                "default": "",
                "title": "Unit",
                "type": "string"
              },
              "displayMode": {
                "default": "simulated",
                "title": "Displaymode",
                "type": "string"
              },
              "metricRef": {
                "anyOf": [
                  {
                    "type": "string"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Metricref"
              },
              "metricCandidate": {
                "anyOf": [
                  {
                    "additionalProperties": true,
                    "type": "object"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Metriccandidate"
              },
              "note": {
                "default": "",
                "title": "Note",
                "type": "string"
              }
            },
            "title": "EditMetricAddition",
            "type": "object"
          },
          "EditMetricDeletion": {
            "description": "要**删除**的一个指标 —— 卡片内的**一个指标实例**。\n\n「删掉某个指标」与「删掉整张卡片」是两件事（契约表 §4.3）：目标必须\n指向卡片内**真实存在**的那一项指标。\n\n身份用**稳定标识**记（``metricKey`` / ``simulatedId`` / ``fieldRef``），\n**不记数组下标** —— 删掉中间一项之后下标会漂移；``label`` 只是给人看\n的显示名，**不作为定位依据**（同名指标要澄清，不能删第一条匹配的）。",
            "properties": {
              "metricKey": {
                "default": "",
                "title": "Metrickey",
                "type": "string"
              },
              "simulatedId": {
                "anyOf": [
                  {
                    "type": "string"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Simulatedid"
              },
              "fieldRef": {
                "anyOf": [
                  {
                    "type": "string"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Fieldref"
              },
              "label": {
                "default": "",
                "title": "Label",
                "type": "string"
              },
              "isPrimary": {
                "default": false,
                "title": "Isprimary",
                "type": "boolean"
              },
              "intent": {
                "default": "",
                "title": "Intent",
                "type": "string"
              }
            },
            "title": "EditMetricDeletion",
            "type": "object"
          }
        },
        "description": "具体改成什么。字段按 intent 不同用到的子集不同；这里统一松结构承载。",
        "properties": {
          "newTitle": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Newtitle"
          },
          "componentType": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Componenttype"
          },
          "chartKind": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Chartkind"
          },
          "sectionTitle": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Sectiontitle"
          },
          "insertAfterRegionId": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Insertafterregionid"
          },
          "sourceRef": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Sourceref"
          },
          "sourceVersion": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Sourceversion"
          },
          "fieldMappingRequired": {
            "anyOf": [
              {
                "type": "boolean"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Fieldmappingrequired"
          },
          "metricRef": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Metricref"
          },
          "metricVersion": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Metricversion"
          },
          "metricIntent": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Metricintent"
          },
          "metricCandidate": {
            "anyOf": [
              {
                "additionalProperties": true,
                "type": "object"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Metriccandidate"
          },
          "baseDefinitionRevision": {
            "anyOf": [
              {
                "type": "integer"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Basedefinitionrevision"
          },
          "addMetrics": {
            "items": {
              "$ref": "#/$defs/EditMetricAddition"
            },
            "title": "Addmetrics",
            "type": "array"
          },
          "deleteMetrics": {
            "items": {
              "$ref": "#/$defs/EditMetricDeletion"
            },
            "title": "Deletemetrics",
            "type": "array"
          },
          "componentSelection": {
            "anyOf": [
              {
                "additionalProperties": true,
                "type": "object"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Componentselection"
          },
          "timeWindow": {
            "anyOf": [
              {
                "additionalProperties": true,
                "type": "object"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Timewindow"
          },
          "timeWindowLabel": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Timewindowlabel"
          },
          "grain": {
            "anyOf": [
              {
                "additionalProperties": true,
                "type": "object"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Grain"
          },
          "supported": {
            "anyOf": [
              {
                "type": "boolean"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Supported"
          },
          "supportedGrains": {
            "items": {
              "type": "string"
            },
            "title": "Supportedgrains",
            "type": "array"
          },
          "topN": {
            "anyOf": [
              {
                "type": "integer"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Topn"
          },
          "sortField": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Sortfield"
          },
          "sortDirection": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Sortdirection"
          },
          "dataShape": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Datashape"
          },
          "displayOptions": {
            "items": {
              "additionalProperties": true,
              "type": "object"
            },
            "title": "Displayoptions",
            "type": "array"
          },
          "disabledOptions": {
            "items": {
              "additionalProperties": true,
              "type": "object"
            },
            "title": "Disabledoptions",
            "type": "array"
          },
          "markdownPreview": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Markdownpreview"
          },
          "reason": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Reason"
          },
          "presentationOperations": {
            "items": {
              "additionalProperties": true,
              "type": "object"
            },
            "title": "Presentationoperations",
            "type": "array"
          },
          "themeOperations": {
            "items": {
              "additionalProperties": true,
              "type": "object"
            },
            "title": "Themeoperations",
            "type": "array"
          }
        },
        "title": "EditOperation",
        "type": "object"
      },
      "required": [],
      "targetSpec": "target.regionId 或 target.matchedText 指明要改哪张卡片；组合修改用 operations 列表（旧格式每步共用顶层 target）",
      "resultSpec": "EditResult：draftAfterEdit / changeSet / reply / markdownPreview / needsClarification；形成待确认候选，用户接受后才正式写入",
      "states": [
        "WAITING_USER_CONFIRM",
        "USER_EDITING_TEMPLATE"
      ],
      "readOnly": false,
      "sideEffects": "在草稿副本上执行；通过后形成待确认候选，用户接受时才在正式事务里写入草稿。",
      "errors": [
        "目标不存在或不可唯一确定 → 返回 needsClarification + clarificationQuestion（草稿不变）",
        "候选基于旧版本草稿 → 拒绝（stale_edit_plan）",
        "依据的生效组合已变 → 拒绝（combination_conflict）",
        "数据源 / 指标不在当前授权范围 → 拒绝（incompatible_fields）"
      ],
      "limits": [
        "单步修改只针对一个区块；要一次改多处请用能力格式 operations"
      ],
      "code": {
        "registration": "app/schemas/edit_plan.py::EditIntent",
        "dispatch": "app/services/agent_service.py::AgentService._try_handle_template_edit",
        "validation": "app/schemas/edit_plan.py::EditPlan",
        "execution": "app/services/template_edit_engine.py::TemplateEditEngine._apply_add"
      }
    },
    {
      "id": "ADD_METRIC",
      "group": "旧意图 / 指标增删替换",
      "entryType": "legacy_intent",
      "status": "available",
      "purpose": "在已有卡片里**追加**指标（保留原有指标）。 （**旧格式入口，不是独立 Tool**：模型在 submit_edit_plan 的 intent / target / operation 里给出，由 template_edit_engine.TemplateEditEngine.apply 执行。）",
      "params": {
        "$defs": {
          "EditMetricAddition": {
            "description": "要**追加**到某个已有区块里的一个指标。\n\n「追加」与「替换」是两种不同的意图：追加**保留**该区块原有指标，\n把新指标加进去；替换才把原来的换掉。\n\n每个待追加指标至少能表达：用户原话意图、显示名、单位、展示模式。",
            "properties": {
              "intent": {
                "default": "",
                "title": "Intent",
                "type": "string"
              },
              "label": {
                "default": "",
                "title": "Label",
                "type": "string"
              },
              "unit": {
                "default": "",
                "title": "Unit",
                "type": "string"
              },
              "displayMode": {
                "default": "simulated",
                "title": "Displaymode",
                "type": "string"
              },
              "metricRef": {
                "anyOf": [
                  {
                    "type": "string"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Metricref"
              },
              "metricCandidate": {
                "anyOf": [
                  {
                    "additionalProperties": true,
                    "type": "object"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Metriccandidate"
              },
              "note": {
                "default": "",
                "title": "Note",
                "type": "string"
              }
            },
            "title": "EditMetricAddition",
            "type": "object"
          },
          "EditMetricDeletion": {
            "description": "要**删除**的一个指标 —— 卡片内的**一个指标实例**。\n\n「删掉某个指标」与「删掉整张卡片」是两件事（契约表 §4.3）：目标必须\n指向卡片内**真实存在**的那一项指标。\n\n身份用**稳定标识**记（``metricKey`` / ``simulatedId`` / ``fieldRef``），\n**不记数组下标** —— 删掉中间一项之后下标会漂移；``label`` 只是给人看\n的显示名，**不作为定位依据**（同名指标要澄清，不能删第一条匹配的）。",
            "properties": {
              "metricKey": {
                "default": "",
                "title": "Metrickey",
                "type": "string"
              },
              "simulatedId": {
                "anyOf": [
                  {
                    "type": "string"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Simulatedid"
              },
              "fieldRef": {
                "anyOf": [
                  {
                    "type": "string"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Fieldref"
              },
              "label": {
                "default": "",
                "title": "Label",
                "type": "string"
              },
              "isPrimary": {
                "default": false,
                "title": "Isprimary",
                "type": "boolean"
              },
              "intent": {
                "default": "",
                "title": "Intent",
                "type": "string"
              }
            },
            "title": "EditMetricDeletion",
            "type": "object"
          }
        },
        "description": "具体改成什么。字段按 intent 不同用到的子集不同；这里统一松结构承载。",
        "properties": {
          "newTitle": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Newtitle"
          },
          "componentType": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Componenttype"
          },
          "chartKind": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Chartkind"
          },
          "sectionTitle": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Sectiontitle"
          },
          "insertAfterRegionId": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Insertafterregionid"
          },
          "sourceRef": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Sourceref"
          },
          "sourceVersion": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Sourceversion"
          },
          "fieldMappingRequired": {
            "anyOf": [
              {
                "type": "boolean"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Fieldmappingrequired"
          },
          "metricRef": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Metricref"
          },
          "metricVersion": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Metricversion"
          },
          "metricIntent": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Metricintent"
          },
          "metricCandidate": {
            "anyOf": [
              {
                "additionalProperties": true,
                "type": "object"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Metriccandidate"
          },
          "baseDefinitionRevision": {
            "anyOf": [
              {
                "type": "integer"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Basedefinitionrevision"
          },
          "addMetrics": {
            "items": {
              "$ref": "#/$defs/EditMetricAddition"
            },
            "title": "Addmetrics",
            "type": "array"
          },
          "deleteMetrics": {
            "items": {
              "$ref": "#/$defs/EditMetricDeletion"
            },
            "title": "Deletemetrics",
            "type": "array"
          },
          "componentSelection": {
            "anyOf": [
              {
                "additionalProperties": true,
                "type": "object"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Componentselection"
          },
          "timeWindow": {
            "anyOf": [
              {
                "additionalProperties": true,
                "type": "object"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Timewindow"
          },
          "timeWindowLabel": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Timewindowlabel"
          },
          "grain": {
            "anyOf": [
              {
                "additionalProperties": true,
                "type": "object"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Grain"
          },
          "supported": {
            "anyOf": [
              {
                "type": "boolean"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Supported"
          },
          "supportedGrains": {
            "items": {
              "type": "string"
            },
            "title": "Supportedgrains",
            "type": "array"
          },
          "topN": {
            "anyOf": [
              {
                "type": "integer"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Topn"
          },
          "sortField": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Sortfield"
          },
          "sortDirection": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Sortdirection"
          },
          "dataShape": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Datashape"
          },
          "displayOptions": {
            "items": {
              "additionalProperties": true,
              "type": "object"
            },
            "title": "Displayoptions",
            "type": "array"
          },
          "disabledOptions": {
            "items": {
              "additionalProperties": true,
              "type": "object"
            },
            "title": "Disabledoptions",
            "type": "array"
          },
          "markdownPreview": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Markdownpreview"
          },
          "reason": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Reason"
          },
          "presentationOperations": {
            "items": {
              "additionalProperties": true,
              "type": "object"
            },
            "title": "Presentationoperations",
            "type": "array"
          },
          "themeOperations": {
            "items": {
              "additionalProperties": true,
              "type": "object"
            },
            "title": "Themeoperations",
            "type": "array"
          }
        },
        "title": "EditOperation",
        "type": "object"
      },
      "required": [],
      "targetSpec": "target.regionId 或 target.matchedText 指明要改哪张卡片；组合修改用 operations 列表（旧格式每步共用顶层 target）",
      "resultSpec": "EditResult：draftAfterEdit / changeSet / reply / markdownPreview / needsClarification；形成待确认候选，用户接受后才正式写入",
      "states": [
        "WAITING_USER_CONFIRM",
        "USER_EDITING_TEMPLATE"
      ],
      "readOnly": false,
      "sideEffects": "在草稿副本上执行；通过后形成待确认候选，用户接受时才在正式事务里写入草稿。",
      "errors": [
        "目标不存在或不可唯一确定 → 返回 needsClarification + clarificationQuestion（草稿不变）",
        "候选基于旧版本草稿 → 拒绝（stale_edit_plan）",
        "依据的生效组合已变 → 拒绝（combination_conflict）",
        "数据源 / 指标不在当前授权范围 → 拒绝（incompatible_fields）"
      ],
      "limits": [
        "追加保留原指标；「替换 / 只留 / 换成」才是 CHANGE_METRIC",
        "单步修改只针对一个区块；要一次改多处请用能力格式 operations"
      ],
      "code": {
        "registration": "app/schemas/edit_plan.py::EditIntent",
        "dispatch": "app/services/agent_service.py::AgentService._try_handle_template_edit",
        "validation": "app/schemas/edit_plan.py::EditPlan",
        "execution": "app/services/template_edit_engine.py::TemplateEditEngine._apply_add_metric"
      }
    },
    {
      "id": "CHANGE_DATA_SOURCE",
      "group": "旧意图 / 数据口径与时间",
      "entryType": "legacy_intent",
      "status": "available",
      "purpose": "换一个数据源。 （**旧格式入口，不是独立 Tool**：模型在 submit_edit_plan 的 intent / target / operation 里给出，由 template_edit_engine.TemplateEditEngine.apply 执行。）",
      "params": {
        "$defs": {
          "EditMetricAddition": {
            "description": "要**追加**到某个已有区块里的一个指标。\n\n「追加」与「替换」是两种不同的意图：追加**保留**该区块原有指标，\n把新指标加进去；替换才把原来的换掉。\n\n每个待追加指标至少能表达：用户原话意图、显示名、单位、展示模式。",
            "properties": {
              "intent": {
                "default": "",
                "title": "Intent",
                "type": "string"
              },
              "label": {
                "default": "",
                "title": "Label",
                "type": "string"
              },
              "unit": {
                "default": "",
                "title": "Unit",
                "type": "string"
              },
              "displayMode": {
                "default": "simulated",
                "title": "Displaymode",
                "type": "string"
              },
              "metricRef": {
                "anyOf": [
                  {
                    "type": "string"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Metricref"
              },
              "metricCandidate": {
                "anyOf": [
                  {
                    "additionalProperties": true,
                    "type": "object"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Metriccandidate"
              },
              "note": {
                "default": "",
                "title": "Note",
                "type": "string"
              }
            },
            "title": "EditMetricAddition",
            "type": "object"
          },
          "EditMetricDeletion": {
            "description": "要**删除**的一个指标 —— 卡片内的**一个指标实例**。\n\n「删掉某个指标」与「删掉整张卡片」是两件事（契约表 §4.3）：目标必须\n指向卡片内**真实存在**的那一项指标。\n\n身份用**稳定标识**记（``metricKey`` / ``simulatedId`` / ``fieldRef``），\n**不记数组下标** —— 删掉中间一项之后下标会漂移；``label`` 只是给人看\n的显示名，**不作为定位依据**（同名指标要澄清，不能删第一条匹配的）。",
            "properties": {
              "metricKey": {
                "default": "",
                "title": "Metrickey",
                "type": "string"
              },
              "simulatedId": {
                "anyOf": [
                  {
                    "type": "string"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Simulatedid"
              },
              "fieldRef": {
                "anyOf": [
                  {
                    "type": "string"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Fieldref"
              },
              "label": {
                "default": "",
                "title": "Label",
                "type": "string"
              },
              "isPrimary": {
                "default": false,
                "title": "Isprimary",
                "type": "boolean"
              },
              "intent": {
                "default": "",
                "title": "Intent",
                "type": "string"
              }
            },
            "title": "EditMetricDeletion",
            "type": "object"
          }
        },
        "description": "具体改成什么。字段按 intent 不同用到的子集不同；这里统一松结构承载。",
        "properties": {
          "newTitle": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Newtitle"
          },
          "componentType": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Componenttype"
          },
          "chartKind": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Chartkind"
          },
          "sectionTitle": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Sectiontitle"
          },
          "insertAfterRegionId": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Insertafterregionid"
          },
          "sourceRef": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Sourceref"
          },
          "sourceVersion": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Sourceversion"
          },
          "fieldMappingRequired": {
            "anyOf": [
              {
                "type": "boolean"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Fieldmappingrequired"
          },
          "metricRef": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Metricref"
          },
          "metricVersion": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Metricversion"
          },
          "metricIntent": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Metricintent"
          },
          "metricCandidate": {
            "anyOf": [
              {
                "additionalProperties": true,
                "type": "object"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Metriccandidate"
          },
          "baseDefinitionRevision": {
            "anyOf": [
              {
                "type": "integer"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Basedefinitionrevision"
          },
          "addMetrics": {
            "items": {
              "$ref": "#/$defs/EditMetricAddition"
            },
            "title": "Addmetrics",
            "type": "array"
          },
          "deleteMetrics": {
            "items": {
              "$ref": "#/$defs/EditMetricDeletion"
            },
            "title": "Deletemetrics",
            "type": "array"
          },
          "componentSelection": {
            "anyOf": [
              {
                "additionalProperties": true,
                "type": "object"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Componentselection"
          },
          "timeWindow": {
            "anyOf": [
              {
                "additionalProperties": true,
                "type": "object"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Timewindow"
          },
          "timeWindowLabel": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Timewindowlabel"
          },
          "grain": {
            "anyOf": [
              {
                "additionalProperties": true,
                "type": "object"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Grain"
          },
          "supported": {
            "anyOf": [
              {
                "type": "boolean"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Supported"
          },
          "supportedGrains": {
            "items": {
              "type": "string"
            },
            "title": "Supportedgrains",
            "type": "array"
          },
          "topN": {
            "anyOf": [
              {
                "type": "integer"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Topn"
          },
          "sortField": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Sortfield"
          },
          "sortDirection": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Sortdirection"
          },
          "dataShape": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Datashape"
          },
          "displayOptions": {
            "items": {
              "additionalProperties": true,
              "type": "object"
            },
            "title": "Displayoptions",
            "type": "array"
          },
          "disabledOptions": {
            "items": {
              "additionalProperties": true,
              "type": "object"
            },
            "title": "Disabledoptions",
            "type": "array"
          },
          "markdownPreview": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Markdownpreview"
          },
          "reason": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Reason"
          },
          "presentationOperations": {
            "items": {
              "additionalProperties": true,
              "type": "object"
            },
            "title": "Presentationoperations",
            "type": "array"
          },
          "themeOperations": {
            "items": {
              "additionalProperties": true,
              "type": "object"
            },
            "title": "Themeoperations",
            "type": "array"
          }
        },
        "title": "EditOperation",
        "type": "object"
      },
      "required": [],
      "targetSpec": "target.regionId 或 target.matchedText 指明要改哪张卡片；组合修改用 operations 列表（旧格式每步共用顶层 target）",
      "resultSpec": "EditResult：draftAfterEdit / changeSet / reply / markdownPreview / needsClarification；形成待确认候选，用户接受后才正式写入",
      "states": [
        "WAITING_USER_CONFIRM",
        "USER_EDITING_TEMPLATE"
      ],
      "readOnly": false,
      "sideEffects": "在草稿副本上执行；通过后形成待确认候选，用户接受时才在正式事务里写入草稿。",
      "errors": [
        "目标不存在或不可唯一确定 → 返回 needsClarification + clarificationQuestion（草稿不变）",
        "候选基于旧版本草稿 → 拒绝（stale_edit_plan）",
        "依据的生效组合已变 → 拒绝（combination_conflict）",
        "数据源 / 指标不在当前授权范围 → 拒绝（incompatible_fields）"
      ],
      "limits": [
        "单步修改只针对一个区块；要一次改多处请用能力格式 operations"
      ],
      "code": {
        "registration": "app/schemas/edit_plan.py::EditIntent",
        "dispatch": "app/services/agent_service.py::AgentService._try_handle_template_edit",
        "validation": "app/schemas/edit_plan.py::EditPlan",
        "execution": "app/services/template_edit_engine.py::TemplateEditEngine._apply_change_source"
      }
    },
    {
      "id": "CHANGE_VISUALIZATION",
      "group": "旧意图 / 展示方式",
      "entryType": "legacy_intent",
      "status": "available",
      "purpose": "改展示类型（componentType / chartKind）。 （**旧格式入口，不是独立 Tool**：模型在 submit_edit_plan 的 intent / target / operation 里给出，由 template_edit_engine.TemplateEditEngine.apply 执行。）",
      "params": {
        "$defs": {
          "EditMetricAddition": {
            "description": "要**追加**到某个已有区块里的一个指标。\n\n「追加」与「替换」是两种不同的意图：追加**保留**该区块原有指标，\n把新指标加进去；替换才把原来的换掉。\n\n每个待追加指标至少能表达：用户原话意图、显示名、单位、展示模式。",
            "properties": {
              "intent": {
                "default": "",
                "title": "Intent",
                "type": "string"
              },
              "label": {
                "default": "",
                "title": "Label",
                "type": "string"
              },
              "unit": {
                "default": "",
                "title": "Unit",
                "type": "string"
              },
              "displayMode": {
                "default": "simulated",
                "title": "Displaymode",
                "type": "string"
              },
              "metricRef": {
                "anyOf": [
                  {
                    "type": "string"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Metricref"
              },
              "metricCandidate": {
                "anyOf": [
                  {
                    "additionalProperties": true,
                    "type": "object"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Metriccandidate"
              },
              "note": {
                "default": "",
                "title": "Note",
                "type": "string"
              }
            },
            "title": "EditMetricAddition",
            "type": "object"
          },
          "EditMetricDeletion": {
            "description": "要**删除**的一个指标 —— 卡片内的**一个指标实例**。\n\n「删掉某个指标」与「删掉整张卡片」是两件事（契约表 §4.3）：目标必须\n指向卡片内**真实存在**的那一项指标。\n\n身份用**稳定标识**记（``metricKey`` / ``simulatedId`` / ``fieldRef``），\n**不记数组下标** —— 删掉中间一项之后下标会漂移；``label`` 只是给人看\n的显示名，**不作为定位依据**（同名指标要澄清，不能删第一条匹配的）。",
            "properties": {
              "metricKey": {
                "default": "",
                "title": "Metrickey",
                "type": "string"
              },
              "simulatedId": {
                "anyOf": [
                  {
                    "type": "string"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Simulatedid"
              },
              "fieldRef": {
                "anyOf": [
                  {
                    "type": "string"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Fieldref"
              },
              "label": {
                "default": "",
                "title": "Label",
                "type": "string"
              },
              "isPrimary": {
                "default": false,
                "title": "Isprimary",
                "type": "boolean"
              },
              "intent": {
                "default": "",
                "title": "Intent",
                "type": "string"
              }
            },
            "title": "EditMetricDeletion",
            "type": "object"
          }
        },
        "description": "具体改成什么。字段按 intent 不同用到的子集不同；这里统一松结构承载。",
        "properties": {
          "newTitle": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Newtitle"
          },
          "componentType": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Componenttype"
          },
          "chartKind": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Chartkind"
          },
          "sectionTitle": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Sectiontitle"
          },
          "insertAfterRegionId": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Insertafterregionid"
          },
          "sourceRef": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Sourceref"
          },
          "sourceVersion": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Sourceversion"
          },
          "fieldMappingRequired": {
            "anyOf": [
              {
                "type": "boolean"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Fieldmappingrequired"
          },
          "metricRef": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Metricref"
          },
          "metricVersion": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Metricversion"
          },
          "metricIntent": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Metricintent"
          },
          "metricCandidate": {
            "anyOf": [
              {
                "additionalProperties": true,
                "type": "object"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Metriccandidate"
          },
          "baseDefinitionRevision": {
            "anyOf": [
              {
                "type": "integer"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Basedefinitionrevision"
          },
          "addMetrics": {
            "items": {
              "$ref": "#/$defs/EditMetricAddition"
            },
            "title": "Addmetrics",
            "type": "array"
          },
          "deleteMetrics": {
            "items": {
              "$ref": "#/$defs/EditMetricDeletion"
            },
            "title": "Deletemetrics",
            "type": "array"
          },
          "componentSelection": {
            "anyOf": [
              {
                "additionalProperties": true,
                "type": "object"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Componentselection"
          },
          "timeWindow": {
            "anyOf": [
              {
                "additionalProperties": true,
                "type": "object"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Timewindow"
          },
          "timeWindowLabel": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Timewindowlabel"
          },
          "grain": {
            "anyOf": [
              {
                "additionalProperties": true,
                "type": "object"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Grain"
          },
          "supported": {
            "anyOf": [
              {
                "type": "boolean"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Supported"
          },
          "supportedGrains": {
            "items": {
              "type": "string"
            },
            "title": "Supportedgrains",
            "type": "array"
          },
          "topN": {
            "anyOf": [
              {
                "type": "integer"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Topn"
          },
          "sortField": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Sortfield"
          },
          "sortDirection": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Sortdirection"
          },
          "dataShape": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Datashape"
          },
          "displayOptions": {
            "items": {
              "additionalProperties": true,
              "type": "object"
            },
            "title": "Displayoptions",
            "type": "array"
          },
          "disabledOptions": {
            "items": {
              "additionalProperties": true,
              "type": "object"
            },
            "title": "Disabledoptions",
            "type": "array"
          },
          "markdownPreview": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Markdownpreview"
          },
          "reason": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Reason"
          },
          "presentationOperations": {
            "items": {
              "additionalProperties": true,
              "type": "object"
            },
            "title": "Presentationoperations",
            "type": "array"
          },
          "themeOperations": {
            "items": {
              "additionalProperties": true,
              "type": "object"
            },
            "title": "Themeoperations",
            "type": "array"
          }
        },
        "title": "EditOperation",
        "type": "object"
      },
      "required": [],
      "targetSpec": "target.regionId 或 target.matchedText 指明要改哪张卡片；组合修改用 operations 列表（旧格式每步共用顶层 target）",
      "resultSpec": "EditResult：draftAfterEdit / changeSet / reply / markdownPreview / needsClarification；形成待确认候选，用户接受后才正式写入",
      "states": [
        "WAITING_USER_CONFIRM",
        "USER_EDITING_TEMPLATE"
      ],
      "readOnly": false,
      "sideEffects": "在草稿副本上执行；通过后形成待确认候选，用户接受时才在正式事务里写入草稿。",
      "errors": [
        "目标不存在或不可唯一确定 → 返回 needsClarification + clarificationQuestion（草稿不变）",
        "候选基于旧版本草稿 → 拒绝（stale_edit_plan）",
        "依据的生效组合已变 → 拒绝（combination_conflict）",
        "数据源 / 指标不在当前授权范围 → 拒绝（incompatible_fields）"
      ],
      "limits": [
        "单步修改只针对一个区块；要一次改多处请用能力格式 operations"
      ],
      "code": {
        "registration": "app/schemas/edit_plan.py::EditIntent",
        "dispatch": "app/services/agent_service.py::AgentService._try_handle_template_edit",
        "validation": "app/schemas/edit_plan.py::EditPlan",
        "execution": "app/services/template_edit_engine.py::TemplateEditEngine._apply_change_visualization"
      }
    },
    {
      "id": "SUGGEST_VISUALIZATION",
      "group": "旧意图 / 展示方式",
      "entryType": "legacy_intent",
      "status": "available",
      "purpose": "只给展示方式建议，**不改草稿**。 （**旧格式入口，不是独立 Tool**：模型在 submit_edit_plan 的 intent / target / operation 里给出，由 template_edit_engine.TemplateEditEngine.apply 执行。）",
      "params": {
        "$defs": {
          "EditMetricAddition": {
            "description": "要**追加**到某个已有区块里的一个指标。\n\n「追加」与「替换」是两种不同的意图：追加**保留**该区块原有指标，\n把新指标加进去；替换才把原来的换掉。\n\n每个待追加指标至少能表达：用户原话意图、显示名、单位、展示模式。",
            "properties": {
              "intent": {
                "default": "",
                "title": "Intent",
                "type": "string"
              },
              "label": {
                "default": "",
                "title": "Label",
                "type": "string"
              },
              "unit": {
                "default": "",
                "title": "Unit",
                "type": "string"
              },
              "displayMode": {
                "default": "simulated",
                "title": "Displaymode",
                "type": "string"
              },
              "metricRef": {
                "anyOf": [
                  {
                    "type": "string"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Metricref"
              },
              "metricCandidate": {
                "anyOf": [
                  {
                    "additionalProperties": true,
                    "type": "object"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Metriccandidate"
              },
              "note": {
                "default": "",
                "title": "Note",
                "type": "string"
              }
            },
            "title": "EditMetricAddition",
            "type": "object"
          },
          "EditMetricDeletion": {
            "description": "要**删除**的一个指标 —— 卡片内的**一个指标实例**。\n\n「删掉某个指标」与「删掉整张卡片」是两件事（契约表 §4.3）：目标必须\n指向卡片内**真实存在**的那一项指标。\n\n身份用**稳定标识**记（``metricKey`` / ``simulatedId`` / ``fieldRef``），\n**不记数组下标** —— 删掉中间一项之后下标会漂移；``label`` 只是给人看\n的显示名，**不作为定位依据**（同名指标要澄清，不能删第一条匹配的）。",
            "properties": {
              "metricKey": {
                "default": "",
                "title": "Metrickey",
                "type": "string"
              },
              "simulatedId": {
                "anyOf": [
                  {
                    "type": "string"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Simulatedid"
              },
              "fieldRef": {
                "anyOf": [
                  {
                    "type": "string"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Fieldref"
              },
              "label": {
                "default": "",
                "title": "Label",
                "type": "string"
              },
              "isPrimary": {
                "default": false,
                "title": "Isprimary",
                "type": "boolean"
              },
              "intent": {
                "default": "",
                "title": "Intent",
                "type": "string"
              }
            },
            "title": "EditMetricDeletion",
            "type": "object"
          }
        },
        "description": "具体改成什么。字段按 intent 不同用到的子集不同；这里统一松结构承载。",
        "properties": {
          "newTitle": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Newtitle"
          },
          "componentType": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Componenttype"
          },
          "chartKind": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Chartkind"
          },
          "sectionTitle": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Sectiontitle"
          },
          "insertAfterRegionId": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Insertafterregionid"
          },
          "sourceRef": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Sourceref"
          },
          "sourceVersion": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Sourceversion"
          },
          "fieldMappingRequired": {
            "anyOf": [
              {
                "type": "boolean"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Fieldmappingrequired"
          },
          "metricRef": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Metricref"
          },
          "metricVersion": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Metricversion"
          },
          "metricIntent": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Metricintent"
          },
          "metricCandidate": {
            "anyOf": [
              {
                "additionalProperties": true,
                "type": "object"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Metriccandidate"
          },
          "baseDefinitionRevision": {
            "anyOf": [
              {
                "type": "integer"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Basedefinitionrevision"
          },
          "addMetrics": {
            "items": {
              "$ref": "#/$defs/EditMetricAddition"
            },
            "title": "Addmetrics",
            "type": "array"
          },
          "deleteMetrics": {
            "items": {
              "$ref": "#/$defs/EditMetricDeletion"
            },
            "title": "Deletemetrics",
            "type": "array"
          },
          "componentSelection": {
            "anyOf": [
              {
                "additionalProperties": true,
                "type": "object"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Componentselection"
          },
          "timeWindow": {
            "anyOf": [
              {
                "additionalProperties": true,
                "type": "object"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Timewindow"
          },
          "timeWindowLabel": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Timewindowlabel"
          },
          "grain": {
            "anyOf": [
              {
                "additionalProperties": true,
                "type": "object"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Grain"
          },
          "supported": {
            "anyOf": [
              {
                "type": "boolean"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Supported"
          },
          "supportedGrains": {
            "items": {
              "type": "string"
            },
            "title": "Supportedgrains",
            "type": "array"
          },
          "topN": {
            "anyOf": [
              {
                "type": "integer"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Topn"
          },
          "sortField": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Sortfield"
          },
          "sortDirection": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Sortdirection"
          },
          "dataShape": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Datashape"
          },
          "displayOptions": {
            "items": {
              "additionalProperties": true,
              "type": "object"
            },
            "title": "Displayoptions",
            "type": "array"
          },
          "disabledOptions": {
            "items": {
              "additionalProperties": true,
              "type": "object"
            },
            "title": "Disabledoptions",
            "type": "array"
          },
          "markdownPreview": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Markdownpreview"
          },
          "reason": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Reason"
          },
          "presentationOperations": {
            "items": {
              "additionalProperties": true,
              "type": "object"
            },
            "title": "Presentationoperations",
            "type": "array"
          },
          "themeOperations": {
            "items": {
              "additionalProperties": true,
              "type": "object"
            },
            "title": "Themeoperations",
            "type": "array"
          }
        },
        "title": "EditOperation",
        "type": "object"
      },
      "required": [],
      "targetSpec": "target.regionId 或 target.matchedText 指明要改哪张卡片；组合修改用 operations 列表（旧格式每步共用顶层 target）",
      "resultSpec": "EditResult：draftAfterEdit / changeSet / reply / markdownPreview / needsClarification；形成待确认候选，用户接受后才正式写入",
      "states": [
        "WAITING_USER_CONFIRM",
        "USER_EDITING_TEMPLATE"
      ],
      "readOnly": true,
      "sideEffects": "在草稿副本上执行；通过后形成待确认候选，用户接受时才在正式事务里写入草稿。",
      "errors": [
        "目标不存在或不可唯一确定 → 返回 needsClarification + clarificationQuestion（草稿不变）",
        "候选基于旧版本草稿 → 拒绝（stale_edit_plan）",
        "依据的生效组合已变 → 拒绝（combination_conflict）",
        "数据源 / 指标不在当前授权范围 → 拒绝（incompatible_fields）"
      ],
      "limits": [
        "只回建议，不改草稿，不占修改澄清轮次",
        "单步修改只针对一个区块；要一次改多处请用能力格式 operations"
      ],
      "code": {
        "registration": "app/schemas/edit_plan.py::EditIntent",
        "dispatch": "app/services/agent_service.py::AgentService._try_handle_template_edit",
        "validation": "app/schemas/edit_plan.py::EditPlan",
        "execution": "app/services/template_edit_engine.py::TemplateEditEngine._markdown_display_options"
      }
    },
    {
      "id": "CHANGE_COMPILATION_GRAIN",
      "group": "旧意图 / 数据口径与时间",
      "entryType": "legacy_intent",
      "status": "available",
      "purpose": "改统计粒度。 （**旧格式入口，不是独立 Tool**：模型在 submit_edit_plan 的 intent / target / operation 里给出，由 template_edit_engine.TemplateEditEngine.apply 执行。）",
      "params": {
        "$defs": {
          "EditMetricAddition": {
            "description": "要**追加**到某个已有区块里的一个指标。\n\n「追加」与「替换」是两种不同的意图：追加**保留**该区块原有指标，\n把新指标加进去；替换才把原来的换掉。\n\n每个待追加指标至少能表达：用户原话意图、显示名、单位、展示模式。",
            "properties": {
              "intent": {
                "default": "",
                "title": "Intent",
                "type": "string"
              },
              "label": {
                "default": "",
                "title": "Label",
                "type": "string"
              },
              "unit": {
                "default": "",
                "title": "Unit",
                "type": "string"
              },
              "displayMode": {
                "default": "simulated",
                "title": "Displaymode",
                "type": "string"
              },
              "metricRef": {
                "anyOf": [
                  {
                    "type": "string"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Metricref"
              },
              "metricCandidate": {
                "anyOf": [
                  {
                    "additionalProperties": true,
                    "type": "object"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Metriccandidate"
              },
              "note": {
                "default": "",
                "title": "Note",
                "type": "string"
              }
            },
            "title": "EditMetricAddition",
            "type": "object"
          },
          "EditMetricDeletion": {
            "description": "要**删除**的一个指标 —— 卡片内的**一个指标实例**。\n\n「删掉某个指标」与「删掉整张卡片」是两件事（契约表 §4.3）：目标必须\n指向卡片内**真实存在**的那一项指标。\n\n身份用**稳定标识**记（``metricKey`` / ``simulatedId`` / ``fieldRef``），\n**不记数组下标** —— 删掉中间一项之后下标会漂移；``label`` 只是给人看\n的显示名，**不作为定位依据**（同名指标要澄清，不能删第一条匹配的）。",
            "properties": {
              "metricKey": {
                "default": "",
                "title": "Metrickey",
                "type": "string"
              },
              "simulatedId": {
                "anyOf": [
                  {
                    "type": "string"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Simulatedid"
              },
              "fieldRef": {
                "anyOf": [
                  {
                    "type": "string"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Fieldref"
              },
              "label": {
                "default": "",
                "title": "Label",
                "type": "string"
              },
              "isPrimary": {
                "default": false,
                "title": "Isprimary",
                "type": "boolean"
              },
              "intent": {
                "default": "",
                "title": "Intent",
                "type": "string"
              }
            },
            "title": "EditMetricDeletion",
            "type": "object"
          }
        },
        "description": "具体改成什么。字段按 intent 不同用到的子集不同；这里统一松结构承载。",
        "properties": {
          "newTitle": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Newtitle"
          },
          "componentType": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Componenttype"
          },
          "chartKind": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Chartkind"
          },
          "sectionTitle": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Sectiontitle"
          },
          "insertAfterRegionId": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Insertafterregionid"
          },
          "sourceRef": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Sourceref"
          },
          "sourceVersion": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Sourceversion"
          },
          "fieldMappingRequired": {
            "anyOf": [
              {
                "type": "boolean"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Fieldmappingrequired"
          },
          "metricRef": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Metricref"
          },
          "metricVersion": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Metricversion"
          },
          "metricIntent": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Metricintent"
          },
          "metricCandidate": {
            "anyOf": [
              {
                "additionalProperties": true,
                "type": "object"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Metriccandidate"
          },
          "baseDefinitionRevision": {
            "anyOf": [
              {
                "type": "integer"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Basedefinitionrevision"
          },
          "addMetrics": {
            "items": {
              "$ref": "#/$defs/EditMetricAddition"
            },
            "title": "Addmetrics",
            "type": "array"
          },
          "deleteMetrics": {
            "items": {
              "$ref": "#/$defs/EditMetricDeletion"
            },
            "title": "Deletemetrics",
            "type": "array"
          },
          "componentSelection": {
            "anyOf": [
              {
                "additionalProperties": true,
                "type": "object"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Componentselection"
          },
          "timeWindow": {
            "anyOf": [
              {
                "additionalProperties": true,
                "type": "object"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Timewindow"
          },
          "timeWindowLabel": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Timewindowlabel"
          },
          "grain": {
            "anyOf": [
              {
                "additionalProperties": true,
                "type": "object"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Grain"
          },
          "supported": {
            "anyOf": [
              {
                "type": "boolean"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Supported"
          },
          "supportedGrains": {
            "items": {
              "type": "string"
            },
            "title": "Supportedgrains",
            "type": "array"
          },
          "topN": {
            "anyOf": [
              {
                "type": "integer"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Topn"
          },
          "sortField": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Sortfield"
          },
          "sortDirection": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Sortdirection"
          },
          "dataShape": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Datashape"
          },
          "displayOptions": {
            "items": {
              "additionalProperties": true,
              "type": "object"
            },
            "title": "Displayoptions",
            "type": "array"
          },
          "disabledOptions": {
            "items": {
              "additionalProperties": true,
              "type": "object"
            },
            "title": "Disabledoptions",
            "type": "array"
          },
          "markdownPreview": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Markdownpreview"
          },
          "reason": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Reason"
          },
          "presentationOperations": {
            "items": {
              "additionalProperties": true,
              "type": "object"
            },
            "title": "Presentationoperations",
            "type": "array"
          },
          "themeOperations": {
            "items": {
              "additionalProperties": true,
              "type": "object"
            },
            "title": "Themeoperations",
            "type": "array"
          }
        },
        "title": "EditOperation",
        "type": "object"
      },
      "required": [],
      "targetSpec": "target.regionId 或 target.matchedText 指明要改哪张卡片；组合修改用 operations 列表（旧格式每步共用顶层 target）",
      "resultSpec": "EditResult：draftAfterEdit / changeSet / reply / markdownPreview / needsClarification；形成待确认候选，用户接受后才正式写入",
      "states": [
        "WAITING_USER_CONFIRM",
        "USER_EDITING_TEMPLATE"
      ],
      "readOnly": false,
      "sideEffects": "在草稿副本上执行；通过后形成待确认候选，用户接受时才在正式事务里写入草稿。",
      "errors": [
        "目标不存在或不可唯一确定 → 返回 needsClarification + clarificationQuestion（草稿不变）",
        "候选基于旧版本草稿 → 拒绝（stale_edit_plan）",
        "依据的生效组合已变 → 拒绝（combination_conflict）",
        "数据源 / 指标不在当前授权范围 → 拒绝（incompatible_fields）"
      ],
      "limits": [
        "单步修改只针对一个区块；要一次改多处请用能力格式 operations"
      ],
      "code": {
        "registration": "app/schemas/edit_plan.py::EditIntent",
        "dispatch": "app/services/agent_service.py::AgentService._try_handle_template_edit",
        "validation": "app/schemas/edit_plan.py::EditPlan",
        "execution": "app/services/template_edit_engine.py::TemplateEditEngine._apply_change_grain"
      }
    },
    {
      "id": "CHANGE_TIME_RANGE",
      "group": "旧意图 / 数据口径与时间",
      "entryType": "legacy_intent",
      "status": "available",
      "purpose": "改时间范围。 （**旧格式入口，不是独立 Tool**：模型在 submit_edit_plan 的 intent / target / operation 里给出，由 template_edit_engine.TemplateEditEngine.apply 执行。）",
      "params": {
        "$defs": {
          "EditMetricAddition": {
            "description": "要**追加**到某个已有区块里的一个指标。\n\n「追加」与「替换」是两种不同的意图：追加**保留**该区块原有指标，\n把新指标加进去；替换才把原来的换掉。\n\n每个待追加指标至少能表达：用户原话意图、显示名、单位、展示模式。",
            "properties": {
              "intent": {
                "default": "",
                "title": "Intent",
                "type": "string"
              },
              "label": {
                "default": "",
                "title": "Label",
                "type": "string"
              },
              "unit": {
                "default": "",
                "title": "Unit",
                "type": "string"
              },
              "displayMode": {
                "default": "simulated",
                "title": "Displaymode",
                "type": "string"
              },
              "metricRef": {
                "anyOf": [
                  {
                    "type": "string"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Metricref"
              },
              "metricCandidate": {
                "anyOf": [
                  {
                    "additionalProperties": true,
                    "type": "object"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Metriccandidate"
              },
              "note": {
                "default": "",
                "title": "Note",
                "type": "string"
              }
            },
            "title": "EditMetricAddition",
            "type": "object"
          },
          "EditMetricDeletion": {
            "description": "要**删除**的一个指标 —— 卡片内的**一个指标实例**。\n\n「删掉某个指标」与「删掉整张卡片」是两件事（契约表 §4.3）：目标必须\n指向卡片内**真实存在**的那一项指标。\n\n身份用**稳定标识**记（``metricKey`` / ``simulatedId`` / ``fieldRef``），\n**不记数组下标** —— 删掉中间一项之后下标会漂移；``label`` 只是给人看\n的显示名，**不作为定位依据**（同名指标要澄清，不能删第一条匹配的）。",
            "properties": {
              "metricKey": {
                "default": "",
                "title": "Metrickey",
                "type": "string"
              },
              "simulatedId": {
                "anyOf": [
                  {
                    "type": "string"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Simulatedid"
              },
              "fieldRef": {
                "anyOf": [
                  {
                    "type": "string"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Fieldref"
              },
              "label": {
                "default": "",
                "title": "Label",
                "type": "string"
              },
              "isPrimary": {
                "default": false,
                "title": "Isprimary",
                "type": "boolean"
              },
              "intent": {
                "default": "",
                "title": "Intent",
                "type": "string"
              }
            },
            "title": "EditMetricDeletion",
            "type": "object"
          }
        },
        "description": "具体改成什么。字段按 intent 不同用到的子集不同；这里统一松结构承载。",
        "properties": {
          "newTitle": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Newtitle"
          },
          "componentType": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Componenttype"
          },
          "chartKind": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Chartkind"
          },
          "sectionTitle": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Sectiontitle"
          },
          "insertAfterRegionId": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Insertafterregionid"
          },
          "sourceRef": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Sourceref"
          },
          "sourceVersion": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Sourceversion"
          },
          "fieldMappingRequired": {
            "anyOf": [
              {
                "type": "boolean"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Fieldmappingrequired"
          },
          "metricRef": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Metricref"
          },
          "metricVersion": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Metricversion"
          },
          "metricIntent": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Metricintent"
          },
          "metricCandidate": {
            "anyOf": [
              {
                "additionalProperties": true,
                "type": "object"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Metriccandidate"
          },
          "baseDefinitionRevision": {
            "anyOf": [
              {
                "type": "integer"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Basedefinitionrevision"
          },
          "addMetrics": {
            "items": {
              "$ref": "#/$defs/EditMetricAddition"
            },
            "title": "Addmetrics",
            "type": "array"
          },
          "deleteMetrics": {
            "items": {
              "$ref": "#/$defs/EditMetricDeletion"
            },
            "title": "Deletemetrics",
            "type": "array"
          },
          "componentSelection": {
            "anyOf": [
              {
                "additionalProperties": true,
                "type": "object"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Componentselection"
          },
          "timeWindow": {
            "anyOf": [
              {
                "additionalProperties": true,
                "type": "object"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Timewindow"
          },
          "timeWindowLabel": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Timewindowlabel"
          },
          "grain": {
            "anyOf": [
              {
                "additionalProperties": true,
                "type": "object"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Grain"
          },
          "supported": {
            "anyOf": [
              {
                "type": "boolean"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Supported"
          },
          "supportedGrains": {
            "items": {
              "type": "string"
            },
            "title": "Supportedgrains",
            "type": "array"
          },
          "topN": {
            "anyOf": [
              {
                "type": "integer"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Topn"
          },
          "sortField": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Sortfield"
          },
          "sortDirection": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Sortdirection"
          },
          "dataShape": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Datashape"
          },
          "displayOptions": {
            "items": {
              "additionalProperties": true,
              "type": "object"
            },
            "title": "Displayoptions",
            "type": "array"
          },
          "disabledOptions": {
            "items": {
              "additionalProperties": true,
              "type": "object"
            },
            "title": "Disabledoptions",
            "type": "array"
          },
          "markdownPreview": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Markdownpreview"
          },
          "reason": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Reason"
          },
          "presentationOperations": {
            "items": {
              "additionalProperties": true,
              "type": "object"
            },
            "title": "Presentationoperations",
            "type": "array"
          },
          "themeOperations": {
            "items": {
              "additionalProperties": true,
              "type": "object"
            },
            "title": "Themeoperations",
            "type": "array"
          }
        },
        "title": "EditOperation",
        "type": "object"
      },
      "required": [],
      "targetSpec": "target.regionId 或 target.matchedText 指明要改哪张卡片；组合修改用 operations 列表（旧格式每步共用顶层 target）",
      "resultSpec": "EditResult：draftAfterEdit / changeSet / reply / markdownPreview / needsClarification；形成待确认候选，用户接受后才正式写入",
      "states": [
        "WAITING_USER_CONFIRM",
        "USER_EDITING_TEMPLATE"
      ],
      "readOnly": false,
      "sideEffects": "在草稿副本上执行；通过后形成待确认候选，用户接受时才在正式事务里写入草稿。",
      "errors": [
        "目标不存在或不可唯一确定 → 返回 needsClarification + clarificationQuestion（草稿不变）",
        "候选基于旧版本草稿 → 拒绝（stale_edit_plan）",
        "依据的生效组合已变 → 拒绝（combination_conflict）",
        "数据源 / 指标不在当前授权范围 → 拒绝（incompatible_fields）"
      ],
      "limits": [
        "单步修改只针对一个区块；要一次改多处请用能力格式 operations"
      ],
      "code": {
        "registration": "app/schemas/edit_plan.py::EditIntent",
        "dispatch": "app/services/agent_service.py::AgentService._try_handle_template_edit",
        "validation": "app/schemas/edit_plan.py::EditPlan",
        "execution": "app/services/template_edit_engine.py::TemplateEditEngine._apply_change_time_range"
      }
    },
    {
      "id": "CHANGE_METRIC",
      "group": "旧意图 / 指标增删替换",
      "entryType": "legacy_intent",
      "status": "available",
      "purpose": "替换某张卡片里的指标。 （**旧格式入口，不是独立 Tool**：模型在 submit_edit_plan 的 intent / target / operation 里给出，由 template_edit_engine.TemplateEditEngine.apply 执行。）",
      "params": {
        "$defs": {
          "EditMetricAddition": {
            "description": "要**追加**到某个已有区块里的一个指标。\n\n「追加」与「替换」是两种不同的意图：追加**保留**该区块原有指标，\n把新指标加进去；替换才把原来的换掉。\n\n每个待追加指标至少能表达：用户原话意图、显示名、单位、展示模式。",
            "properties": {
              "intent": {
                "default": "",
                "title": "Intent",
                "type": "string"
              },
              "label": {
                "default": "",
                "title": "Label",
                "type": "string"
              },
              "unit": {
                "default": "",
                "title": "Unit",
                "type": "string"
              },
              "displayMode": {
                "default": "simulated",
                "title": "Displaymode",
                "type": "string"
              },
              "metricRef": {
                "anyOf": [
                  {
                    "type": "string"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Metricref"
              },
              "metricCandidate": {
                "anyOf": [
                  {
                    "additionalProperties": true,
                    "type": "object"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Metriccandidate"
              },
              "note": {
                "default": "",
                "title": "Note",
                "type": "string"
              }
            },
            "title": "EditMetricAddition",
            "type": "object"
          },
          "EditMetricDeletion": {
            "description": "要**删除**的一个指标 —— 卡片内的**一个指标实例**。\n\n「删掉某个指标」与「删掉整张卡片」是两件事（契约表 §4.3）：目标必须\n指向卡片内**真实存在**的那一项指标。\n\n身份用**稳定标识**记（``metricKey`` / ``simulatedId`` / ``fieldRef``），\n**不记数组下标** —— 删掉中间一项之后下标会漂移；``label`` 只是给人看\n的显示名，**不作为定位依据**（同名指标要澄清，不能删第一条匹配的）。",
            "properties": {
              "metricKey": {
                "default": "",
                "title": "Metrickey",
                "type": "string"
              },
              "simulatedId": {
                "anyOf": [
                  {
                    "type": "string"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Simulatedid"
              },
              "fieldRef": {
                "anyOf": [
                  {
                    "type": "string"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Fieldref"
              },
              "label": {
                "default": "",
                "title": "Label",
                "type": "string"
              },
              "isPrimary": {
                "default": false,
                "title": "Isprimary",
                "type": "boolean"
              },
              "intent": {
                "default": "",
                "title": "Intent",
                "type": "string"
              }
            },
            "title": "EditMetricDeletion",
            "type": "object"
          }
        },
        "description": "具体改成什么。字段按 intent 不同用到的子集不同；这里统一松结构承载。",
        "properties": {
          "newTitle": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Newtitle"
          },
          "componentType": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Componenttype"
          },
          "chartKind": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Chartkind"
          },
          "sectionTitle": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Sectiontitle"
          },
          "insertAfterRegionId": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Insertafterregionid"
          },
          "sourceRef": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Sourceref"
          },
          "sourceVersion": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Sourceversion"
          },
          "fieldMappingRequired": {
            "anyOf": [
              {
                "type": "boolean"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Fieldmappingrequired"
          },
          "metricRef": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Metricref"
          },
          "metricVersion": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Metricversion"
          },
          "metricIntent": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Metricintent"
          },
          "metricCandidate": {
            "anyOf": [
              {
                "additionalProperties": true,
                "type": "object"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Metriccandidate"
          },
          "baseDefinitionRevision": {
            "anyOf": [
              {
                "type": "integer"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Basedefinitionrevision"
          },
          "addMetrics": {
            "items": {
              "$ref": "#/$defs/EditMetricAddition"
            },
            "title": "Addmetrics",
            "type": "array"
          },
          "deleteMetrics": {
            "items": {
              "$ref": "#/$defs/EditMetricDeletion"
            },
            "title": "Deletemetrics",
            "type": "array"
          },
          "componentSelection": {
            "anyOf": [
              {
                "additionalProperties": true,
                "type": "object"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Componentselection"
          },
          "timeWindow": {
            "anyOf": [
              {
                "additionalProperties": true,
                "type": "object"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Timewindow"
          },
          "timeWindowLabel": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Timewindowlabel"
          },
          "grain": {
            "anyOf": [
              {
                "additionalProperties": true,
                "type": "object"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Grain"
          },
          "supported": {
            "anyOf": [
              {
                "type": "boolean"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Supported"
          },
          "supportedGrains": {
            "items": {
              "type": "string"
            },
            "title": "Supportedgrains",
            "type": "array"
          },
          "topN": {
            "anyOf": [
              {
                "type": "integer"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Topn"
          },
          "sortField": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Sortfield"
          },
          "sortDirection": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Sortdirection"
          },
          "dataShape": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Datashape"
          },
          "displayOptions": {
            "items": {
              "additionalProperties": true,
              "type": "object"
            },
            "title": "Displayoptions",
            "type": "array"
          },
          "disabledOptions": {
            "items": {
              "additionalProperties": true,
              "type": "object"
            },
            "title": "Disabledoptions",
            "type": "array"
          },
          "markdownPreview": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Markdownpreview"
          },
          "reason": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Reason"
          },
          "presentationOperations": {
            "items": {
              "additionalProperties": true,
              "type": "object"
            },
            "title": "Presentationoperations",
            "type": "array"
          },
          "themeOperations": {
            "items": {
              "additionalProperties": true,
              "type": "object"
            },
            "title": "Themeoperations",
            "type": "array"
          }
        },
        "title": "EditOperation",
        "type": "object"
      },
      "required": [],
      "targetSpec": "target.regionId 或 target.matchedText 指明要改哪张卡片；组合修改用 operations 列表（旧格式每步共用顶层 target）",
      "resultSpec": "EditResult：draftAfterEdit / changeSet / reply / markdownPreview / needsClarification；形成待确认候选，用户接受后才正式写入",
      "states": [
        "WAITING_USER_CONFIRM",
        "USER_EDITING_TEMPLATE"
      ],
      "readOnly": false,
      "sideEffects": "在草稿副本上执行；通过后形成待确认候选，用户接受时才在正式事务里写入草稿。",
      "errors": [
        "目标不存在或不可唯一确定 → 返回 needsClarification + clarificationQuestion（草稿不变）",
        "候选基于旧版本草稿 → 拒绝（stale_edit_plan）",
        "依据的生效组合已变 → 拒绝（combination_conflict）",
        "数据源 / 指标不在当前授权范围 → 拒绝（incompatible_fields）"
      ],
      "limits": [
        "单步修改只针对一个区块；要一次改多处请用能力格式 operations"
      ],
      "code": {
        "registration": "app/schemas/edit_plan.py::EditIntent",
        "dispatch": "app/services/agent_service.py::AgentService._try_handle_template_edit",
        "validation": "app/schemas/edit_plan.py::EditPlan",
        "execution": "app/services/template_edit_engine.py::TemplateEditEngine._apply_change_metric"
      }
    },
    {
      "id": "CHANGE_TOP_N",
      "group": "旧意图 / 数据口径与时间",
      "entryType": "legacy_intent",
      "status": "available",
      "purpose": "改 Top N。 （**旧格式入口，不是独立 Tool**：模型在 submit_edit_plan 的 intent / target / operation 里给出，由 template_edit_engine.TemplateEditEngine.apply 执行。）",
      "params": {
        "$defs": {
          "EditMetricAddition": {
            "description": "要**追加**到某个已有区块里的一个指标。\n\n「追加」与「替换」是两种不同的意图：追加**保留**该区块原有指标，\n把新指标加进去；替换才把原来的换掉。\n\n每个待追加指标至少能表达：用户原话意图、显示名、单位、展示模式。",
            "properties": {
              "intent": {
                "default": "",
                "title": "Intent",
                "type": "string"
              },
              "label": {
                "default": "",
                "title": "Label",
                "type": "string"
              },
              "unit": {
                "default": "",
                "title": "Unit",
                "type": "string"
              },
              "displayMode": {
                "default": "simulated",
                "title": "Displaymode",
                "type": "string"
              },
              "metricRef": {
                "anyOf": [
                  {
                    "type": "string"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Metricref"
              },
              "metricCandidate": {
                "anyOf": [
                  {
                    "additionalProperties": true,
                    "type": "object"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Metriccandidate"
              },
              "note": {
                "default": "",
                "title": "Note",
                "type": "string"
              }
            },
            "title": "EditMetricAddition",
            "type": "object"
          },
          "EditMetricDeletion": {
            "description": "要**删除**的一个指标 —— 卡片内的**一个指标实例**。\n\n「删掉某个指标」与「删掉整张卡片」是两件事（契约表 §4.3）：目标必须\n指向卡片内**真实存在**的那一项指标。\n\n身份用**稳定标识**记（``metricKey`` / ``simulatedId`` / ``fieldRef``），\n**不记数组下标** —— 删掉中间一项之后下标会漂移；``label`` 只是给人看\n的显示名，**不作为定位依据**（同名指标要澄清，不能删第一条匹配的）。",
            "properties": {
              "metricKey": {
                "default": "",
                "title": "Metrickey",
                "type": "string"
              },
              "simulatedId": {
                "anyOf": [
                  {
                    "type": "string"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Simulatedid"
              },
              "fieldRef": {
                "anyOf": [
                  {
                    "type": "string"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Fieldref"
              },
              "label": {
                "default": "",
                "title": "Label",
                "type": "string"
              },
              "isPrimary": {
                "default": false,
                "title": "Isprimary",
                "type": "boolean"
              },
              "intent": {
                "default": "",
                "title": "Intent",
                "type": "string"
              }
            },
            "title": "EditMetricDeletion",
            "type": "object"
          }
        },
        "description": "具体改成什么。字段按 intent 不同用到的子集不同；这里统一松结构承载。",
        "properties": {
          "newTitle": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Newtitle"
          },
          "componentType": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Componenttype"
          },
          "chartKind": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Chartkind"
          },
          "sectionTitle": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Sectiontitle"
          },
          "insertAfterRegionId": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Insertafterregionid"
          },
          "sourceRef": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Sourceref"
          },
          "sourceVersion": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Sourceversion"
          },
          "fieldMappingRequired": {
            "anyOf": [
              {
                "type": "boolean"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Fieldmappingrequired"
          },
          "metricRef": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Metricref"
          },
          "metricVersion": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Metricversion"
          },
          "metricIntent": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Metricintent"
          },
          "metricCandidate": {
            "anyOf": [
              {
                "additionalProperties": true,
                "type": "object"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Metriccandidate"
          },
          "baseDefinitionRevision": {
            "anyOf": [
              {
                "type": "integer"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Basedefinitionrevision"
          },
          "addMetrics": {
            "items": {
              "$ref": "#/$defs/EditMetricAddition"
            },
            "title": "Addmetrics",
            "type": "array"
          },
          "deleteMetrics": {
            "items": {
              "$ref": "#/$defs/EditMetricDeletion"
            },
            "title": "Deletemetrics",
            "type": "array"
          },
          "componentSelection": {
            "anyOf": [
              {
                "additionalProperties": true,
                "type": "object"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Componentselection"
          },
          "timeWindow": {
            "anyOf": [
              {
                "additionalProperties": true,
                "type": "object"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Timewindow"
          },
          "timeWindowLabel": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Timewindowlabel"
          },
          "grain": {
            "anyOf": [
              {
                "additionalProperties": true,
                "type": "object"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Grain"
          },
          "supported": {
            "anyOf": [
              {
                "type": "boolean"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Supported"
          },
          "supportedGrains": {
            "items": {
              "type": "string"
            },
            "title": "Supportedgrains",
            "type": "array"
          },
          "topN": {
            "anyOf": [
              {
                "type": "integer"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Topn"
          },
          "sortField": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Sortfield"
          },
          "sortDirection": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Sortdirection"
          },
          "dataShape": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Datashape"
          },
          "displayOptions": {
            "items": {
              "additionalProperties": true,
              "type": "object"
            },
            "title": "Displayoptions",
            "type": "array"
          },
          "disabledOptions": {
            "items": {
              "additionalProperties": true,
              "type": "object"
            },
            "title": "Disabledoptions",
            "type": "array"
          },
          "markdownPreview": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Markdownpreview"
          },
          "reason": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Reason"
          },
          "presentationOperations": {
            "items": {
              "additionalProperties": true,
              "type": "object"
            },
            "title": "Presentationoperations",
            "type": "array"
          },
          "themeOperations": {
            "items": {
              "additionalProperties": true,
              "type": "object"
            },
            "title": "Themeoperations",
            "type": "array"
          }
        },
        "title": "EditOperation",
        "type": "object"
      },
      "required": [],
      "targetSpec": "target.regionId 或 target.matchedText 指明要改哪张卡片；组合修改用 operations 列表（旧格式每步共用顶层 target）",
      "resultSpec": "EditResult：draftAfterEdit / changeSet / reply / markdownPreview / needsClarification；形成待确认候选，用户接受后才正式写入",
      "states": [
        "WAITING_USER_CONFIRM",
        "USER_EDITING_TEMPLATE"
      ],
      "readOnly": false,
      "sideEffects": "在草稿副本上执行；通过后形成待确认候选，用户接受时才在正式事务里写入草稿。",
      "errors": [
        "目标不存在或不可唯一确定 → 返回 needsClarification + clarificationQuestion（草稿不变）",
        "候选基于旧版本草稿 → 拒绝（stale_edit_plan）",
        "依据的生效组合已变 → 拒绝（combination_conflict）",
        "数据源 / 指标不在当前授权范围 → 拒绝（incompatible_fields）"
      ],
      "limits": [
        "单步修改只针对一个区块；要一次改多处请用能力格式 operations"
      ],
      "code": {
        "registration": "app/schemas/edit_plan.py::EditIntent",
        "dispatch": "app/services/agent_service.py::AgentService._try_handle_template_edit",
        "validation": "app/schemas/edit_plan.py::EditPlan",
        "execution": "app/services/template_edit_engine.py::TemplateEditEngine._apply_change_top_n"
      }
    },
    {
      "id": "CHANGE_SORT",
      "group": "旧意图 / 数据口径与时间",
      "entryType": "legacy_intent",
      "status": "available",
      "purpose": "改排序字段与方向。 （**旧格式入口，不是独立 Tool**：模型在 submit_edit_plan 的 intent / target / operation 里给出，由 template_edit_engine.TemplateEditEngine.apply 执行。）",
      "params": {
        "$defs": {
          "EditMetricAddition": {
            "description": "要**追加**到某个已有区块里的一个指标。\n\n「追加」与「替换」是两种不同的意图：追加**保留**该区块原有指标，\n把新指标加进去；替换才把原来的换掉。\n\n每个待追加指标至少能表达：用户原话意图、显示名、单位、展示模式。",
            "properties": {
              "intent": {
                "default": "",
                "title": "Intent",
                "type": "string"
              },
              "label": {
                "default": "",
                "title": "Label",
                "type": "string"
              },
              "unit": {
                "default": "",
                "title": "Unit",
                "type": "string"
              },
              "displayMode": {
                "default": "simulated",
                "title": "Displaymode",
                "type": "string"
              },
              "metricRef": {
                "anyOf": [
                  {
                    "type": "string"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Metricref"
              },
              "metricCandidate": {
                "anyOf": [
                  {
                    "additionalProperties": true,
                    "type": "object"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Metriccandidate"
              },
              "note": {
                "default": "",
                "title": "Note",
                "type": "string"
              }
            },
            "title": "EditMetricAddition",
            "type": "object"
          },
          "EditMetricDeletion": {
            "description": "要**删除**的一个指标 —— 卡片内的**一个指标实例**。\n\n「删掉某个指标」与「删掉整张卡片」是两件事（契约表 §4.3）：目标必须\n指向卡片内**真实存在**的那一项指标。\n\n身份用**稳定标识**记（``metricKey`` / ``simulatedId`` / ``fieldRef``），\n**不记数组下标** —— 删掉中间一项之后下标会漂移；``label`` 只是给人看\n的显示名，**不作为定位依据**（同名指标要澄清，不能删第一条匹配的）。",
            "properties": {
              "metricKey": {
                "default": "",
                "title": "Metrickey",
                "type": "string"
              },
              "simulatedId": {
                "anyOf": [
                  {
                    "type": "string"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Simulatedid"
              },
              "fieldRef": {
                "anyOf": [
                  {
                    "type": "string"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Fieldref"
              },
              "label": {
                "default": "",
                "title": "Label",
                "type": "string"
              },
              "isPrimary": {
                "default": false,
                "title": "Isprimary",
                "type": "boolean"
              },
              "intent": {
                "default": "",
                "title": "Intent",
                "type": "string"
              }
            },
            "title": "EditMetricDeletion",
            "type": "object"
          }
        },
        "description": "具体改成什么。字段按 intent 不同用到的子集不同；这里统一松结构承载。",
        "properties": {
          "newTitle": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Newtitle"
          },
          "componentType": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Componenttype"
          },
          "chartKind": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Chartkind"
          },
          "sectionTitle": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Sectiontitle"
          },
          "insertAfterRegionId": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Insertafterregionid"
          },
          "sourceRef": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Sourceref"
          },
          "sourceVersion": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Sourceversion"
          },
          "fieldMappingRequired": {
            "anyOf": [
              {
                "type": "boolean"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Fieldmappingrequired"
          },
          "metricRef": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Metricref"
          },
          "metricVersion": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Metricversion"
          },
          "metricIntent": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Metricintent"
          },
          "metricCandidate": {
            "anyOf": [
              {
                "additionalProperties": true,
                "type": "object"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Metriccandidate"
          },
          "baseDefinitionRevision": {
            "anyOf": [
              {
                "type": "integer"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Basedefinitionrevision"
          },
          "addMetrics": {
            "items": {
              "$ref": "#/$defs/EditMetricAddition"
            },
            "title": "Addmetrics",
            "type": "array"
          },
          "deleteMetrics": {
            "items": {
              "$ref": "#/$defs/EditMetricDeletion"
            },
            "title": "Deletemetrics",
            "type": "array"
          },
          "componentSelection": {
            "anyOf": [
              {
                "additionalProperties": true,
                "type": "object"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Componentselection"
          },
          "timeWindow": {
            "anyOf": [
              {
                "additionalProperties": true,
                "type": "object"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Timewindow"
          },
          "timeWindowLabel": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Timewindowlabel"
          },
          "grain": {
            "anyOf": [
              {
                "additionalProperties": true,
                "type": "object"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Grain"
          },
          "supported": {
            "anyOf": [
              {
                "type": "boolean"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Supported"
          },
          "supportedGrains": {
            "items": {
              "type": "string"
            },
            "title": "Supportedgrains",
            "type": "array"
          },
          "topN": {
            "anyOf": [
              {
                "type": "integer"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Topn"
          },
          "sortField": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Sortfield"
          },
          "sortDirection": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Sortdirection"
          },
          "dataShape": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Datashape"
          },
          "displayOptions": {
            "items": {
              "additionalProperties": true,
              "type": "object"
            },
            "title": "Displayoptions",
            "type": "array"
          },
          "disabledOptions": {
            "items": {
              "additionalProperties": true,
              "type": "object"
            },
            "title": "Disabledoptions",
            "type": "array"
          },
          "markdownPreview": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Markdownpreview"
          },
          "reason": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Reason"
          },
          "presentationOperations": {
            "items": {
              "additionalProperties": true,
              "type": "object"
            },
            "title": "Presentationoperations",
            "type": "array"
          },
          "themeOperations": {
            "items": {
              "additionalProperties": true,
              "type": "object"
            },
            "title": "Themeoperations",
            "type": "array"
          }
        },
        "title": "EditOperation",
        "type": "object"
      },
      "required": [],
      "targetSpec": "target.regionId 或 target.matchedText 指明要改哪张卡片；组合修改用 operations 列表（旧格式每步共用顶层 target）",
      "resultSpec": "EditResult：draftAfterEdit / changeSet / reply / markdownPreview / needsClarification；形成待确认候选，用户接受后才正式写入",
      "states": [
        "WAITING_USER_CONFIRM",
        "USER_EDITING_TEMPLATE"
      ],
      "readOnly": false,
      "sideEffects": "在草稿副本上执行；通过后形成待确认候选，用户接受时才在正式事务里写入草稿。",
      "errors": [
        "目标不存在或不可唯一确定 → 返回 needsClarification + clarificationQuestion（草稿不变）",
        "候选基于旧版本草稿 → 拒绝（stale_edit_plan）",
        "依据的生效组合已变 → 拒绝（combination_conflict）",
        "数据源 / 指标不在当前授权范围 → 拒绝（incompatible_fields）"
      ],
      "limits": [
        "单步修改只针对一个区块；要一次改多处请用能力格式 operations"
      ],
      "code": {
        "registration": "app/schemas/edit_plan.py::EditIntent",
        "dispatch": "app/services/agent_service.py::AgentService._try_handle_template_edit",
        "validation": "app/schemas/edit_plan.py::EditPlan",
        "execution": "app/services/template_edit_engine.py::TemplateEditEngine._apply_change_sort"
      }
    },
    {
      "id": "APPLY_PRESENTATION",
      "group": "旧意图 / 展示与样式",
      "entryType": "legacy_intent",
      "status": "available",
      "purpose": "一批受约束的展示操作（分组 / 排版 / 样式 / 格式 / 页面 / 元数据 / 主题）。 （**旧格式入口，不是独立 Tool**：模型在 submit_edit_plan 的 intent / target / operation 里给出，由 template_edit_engine.TemplateEditEngine.apply 执行。）",
      "params": {
        "$defs": {
          "EditMetricAddition": {
            "description": "要**追加**到某个已有区块里的一个指标。\n\n「追加」与「替换」是两种不同的意图：追加**保留**该区块原有指标，\n把新指标加进去；替换才把原来的换掉。\n\n每个待追加指标至少能表达：用户原话意图、显示名、单位、展示模式。",
            "properties": {
              "intent": {
                "default": "",
                "title": "Intent",
                "type": "string"
              },
              "label": {
                "default": "",
                "title": "Label",
                "type": "string"
              },
              "unit": {
                "default": "",
                "title": "Unit",
                "type": "string"
              },
              "displayMode": {
                "default": "simulated",
                "title": "Displaymode",
                "type": "string"
              },
              "metricRef": {
                "anyOf": [
                  {
                    "type": "string"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Metricref"
              },
              "metricCandidate": {
                "anyOf": [
                  {
                    "additionalProperties": true,
                    "type": "object"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Metriccandidate"
              },
              "note": {
                "default": "",
                "title": "Note",
                "type": "string"
              }
            },
            "title": "EditMetricAddition",
            "type": "object"
          },
          "EditMetricDeletion": {
            "description": "要**删除**的一个指标 —— 卡片内的**一个指标实例**。\n\n「删掉某个指标」与「删掉整张卡片」是两件事（契约表 §4.3）：目标必须\n指向卡片内**真实存在**的那一项指标。\n\n身份用**稳定标识**记（``metricKey`` / ``simulatedId`` / ``fieldRef``），\n**不记数组下标** —— 删掉中间一项之后下标会漂移；``label`` 只是给人看\n的显示名，**不作为定位依据**（同名指标要澄清，不能删第一条匹配的）。",
            "properties": {
              "metricKey": {
                "default": "",
                "title": "Metrickey",
                "type": "string"
              },
              "simulatedId": {
                "anyOf": [
                  {
                    "type": "string"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Simulatedid"
              },
              "fieldRef": {
                "anyOf": [
                  {
                    "type": "string"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Fieldref"
              },
              "label": {
                "default": "",
                "title": "Label",
                "type": "string"
              },
              "isPrimary": {
                "default": false,
                "title": "Isprimary",
                "type": "boolean"
              },
              "intent": {
                "default": "",
                "title": "Intent",
                "type": "string"
              }
            },
            "title": "EditMetricDeletion",
            "type": "object"
          }
        },
        "description": "具体改成什么。字段按 intent 不同用到的子集不同；这里统一松结构承载。",
        "properties": {
          "newTitle": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Newtitle"
          },
          "componentType": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Componenttype"
          },
          "chartKind": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Chartkind"
          },
          "sectionTitle": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Sectiontitle"
          },
          "insertAfterRegionId": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Insertafterregionid"
          },
          "sourceRef": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Sourceref"
          },
          "sourceVersion": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Sourceversion"
          },
          "fieldMappingRequired": {
            "anyOf": [
              {
                "type": "boolean"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Fieldmappingrequired"
          },
          "metricRef": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Metricref"
          },
          "metricVersion": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Metricversion"
          },
          "metricIntent": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Metricintent"
          },
          "metricCandidate": {
            "anyOf": [
              {
                "additionalProperties": true,
                "type": "object"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Metriccandidate"
          },
          "baseDefinitionRevision": {
            "anyOf": [
              {
                "type": "integer"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Basedefinitionrevision"
          },
          "addMetrics": {
            "items": {
              "$ref": "#/$defs/EditMetricAddition"
            },
            "title": "Addmetrics",
            "type": "array"
          },
          "deleteMetrics": {
            "items": {
              "$ref": "#/$defs/EditMetricDeletion"
            },
            "title": "Deletemetrics",
            "type": "array"
          },
          "componentSelection": {
            "anyOf": [
              {
                "additionalProperties": true,
                "type": "object"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Componentselection"
          },
          "timeWindow": {
            "anyOf": [
              {
                "additionalProperties": true,
                "type": "object"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Timewindow"
          },
          "timeWindowLabel": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Timewindowlabel"
          },
          "grain": {
            "anyOf": [
              {
                "additionalProperties": true,
                "type": "object"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Grain"
          },
          "supported": {
            "anyOf": [
              {
                "type": "boolean"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Supported"
          },
          "supportedGrains": {
            "items": {
              "type": "string"
            },
            "title": "Supportedgrains",
            "type": "array"
          },
          "topN": {
            "anyOf": [
              {
                "type": "integer"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Topn"
          },
          "sortField": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Sortfield"
          },
          "sortDirection": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Sortdirection"
          },
          "dataShape": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Datashape"
          },
          "displayOptions": {
            "items": {
              "additionalProperties": true,
              "type": "object"
            },
            "title": "Displayoptions",
            "type": "array"
          },
          "disabledOptions": {
            "items": {
              "additionalProperties": true,
              "type": "object"
            },
            "title": "Disabledoptions",
            "type": "array"
          },
          "markdownPreview": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Markdownpreview"
          },
          "reason": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Reason"
          },
          "presentationOperations": {
            "items": {
              "additionalProperties": true,
              "type": "object"
            },
            "title": "Presentationoperations",
            "type": "array"
          },
          "themeOperations": {
            "items": {
              "additionalProperties": true,
              "type": "object"
            },
            "title": "Themeoperations",
            "type": "array"
          }
        },
        "title": "EditOperation",
        "type": "object"
      },
      "required": [],
      "targetSpec": "target.regionId 或 target.matchedText 指明要改哪张卡片；组合修改用 operations 列表（旧格式每步共用顶层 target）",
      "resultSpec": "EditResult：draftAfterEdit / changeSet / reply / markdownPreview / needsClarification；形成待确认候选，用户接受后才正式写入",
      "states": [
        "WAITING_USER_CONFIRM",
        "USER_EDITING_TEMPLATE"
      ],
      "readOnly": false,
      "sideEffects": "在草稿副本上执行；通过后形成待确认候选，用户接受时才在正式事务里写入草稿。",
      "errors": [
        "目标不存在或不可唯一确定 → 返回 needsClarification + clarificationQuestion（草稿不变）",
        "候选基于旧版本草稿 → 拒绝（stale_edit_plan）",
        "依据的生效组合已变 → 拒绝（combination_conflict）",
        "数据源 / 指标不在当前授权范围 → 拒绝（incompatible_fields）"
      ],
      "limits": [
        "只改外观：分组 / 排版 / 样式 / 格式 / 页面 / 元数据 / 主题",
        "不改统计口径、时间范围与设备范围",
        "单步修改只针对一个区块；要一次改多处请用能力格式 operations",
        "旧格式入口：展示操作写在 operation.presentationOperations 里（每项形如 {\"op\": \"<操作名>\", \"params\": {...}}）；能力格式里它们写在 SET_PRESENTATION 的 params.operations 里 —— 两者不要混用，展示操作名不要直接放进 submit_edit_plan.operations 的顶层"
      ],
      "code": {
        "registration": "app/schemas/edit_plan.py::EditIntent",
        "dispatch": "app/services/agent_service.py::AgentService._try_handle_template_edit",
        "validation": "app/schemas/edit_plan.py::EditPlan",
        "execution": "app/services/template_edit_engine.py::TemplateEditEngine._apply_presentation"
      }
    },
    {
      "id": "SAVE_THEME",
      "group": "旧意图 / 共享主题",
      "entryType": "legacy_intent",
      "status": "available",
      "purpose": "把当前外观存成 / 更新一个**共享主题**（改主题本身，不改报表定义）。 （**旧格式入口，不是独立 Tool**：模型在 submit_edit_plan 的 intent / target / operation 里给出，由 template_edit_engine.TemplateEditEngine.apply 执行。）",
      "params": {
        "$defs": {
          "EditMetricAddition": {
            "description": "要**追加**到某个已有区块里的一个指标。\n\n「追加」与「替换」是两种不同的意图：追加**保留**该区块原有指标，\n把新指标加进去；替换才把原来的换掉。\n\n每个待追加指标至少能表达：用户原话意图、显示名、单位、展示模式。",
            "properties": {
              "intent": {
                "default": "",
                "title": "Intent",
                "type": "string"
              },
              "label": {
                "default": "",
                "title": "Label",
                "type": "string"
              },
              "unit": {
                "default": "",
                "title": "Unit",
                "type": "string"
              },
              "displayMode": {
                "default": "simulated",
                "title": "Displaymode",
                "type": "string"
              },
              "metricRef": {
                "anyOf": [
                  {
                    "type": "string"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Metricref"
              },
              "metricCandidate": {
                "anyOf": [
                  {
                    "additionalProperties": true,
                    "type": "object"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Metriccandidate"
              },
              "note": {
                "default": "",
                "title": "Note",
                "type": "string"
              }
            },
            "title": "EditMetricAddition",
            "type": "object"
          },
          "EditMetricDeletion": {
            "description": "要**删除**的一个指标 —— 卡片内的**一个指标实例**。\n\n「删掉某个指标」与「删掉整张卡片」是两件事（契约表 §4.3）：目标必须\n指向卡片内**真实存在**的那一项指标。\n\n身份用**稳定标识**记（``metricKey`` / ``simulatedId`` / ``fieldRef``），\n**不记数组下标** —— 删掉中间一项之后下标会漂移；``label`` 只是给人看\n的显示名，**不作为定位依据**（同名指标要澄清，不能删第一条匹配的）。",
            "properties": {
              "metricKey": {
                "default": "",
                "title": "Metrickey",
                "type": "string"
              },
              "simulatedId": {
                "anyOf": [
                  {
                    "type": "string"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Simulatedid"
              },
              "fieldRef": {
                "anyOf": [
                  {
                    "type": "string"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Fieldref"
              },
              "label": {
                "default": "",
                "title": "Label",
                "type": "string"
              },
              "isPrimary": {
                "default": false,
                "title": "Isprimary",
                "type": "boolean"
              },
              "intent": {
                "default": "",
                "title": "Intent",
                "type": "string"
              }
            },
            "title": "EditMetricDeletion",
            "type": "object"
          }
        },
        "description": "具体改成什么。字段按 intent 不同用到的子集不同；这里统一松结构承载。",
        "properties": {
          "newTitle": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Newtitle"
          },
          "componentType": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Componenttype"
          },
          "chartKind": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Chartkind"
          },
          "sectionTitle": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Sectiontitle"
          },
          "insertAfterRegionId": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Insertafterregionid"
          },
          "sourceRef": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Sourceref"
          },
          "sourceVersion": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Sourceversion"
          },
          "fieldMappingRequired": {
            "anyOf": [
              {
                "type": "boolean"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Fieldmappingrequired"
          },
          "metricRef": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Metricref"
          },
          "metricVersion": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Metricversion"
          },
          "metricIntent": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Metricintent"
          },
          "metricCandidate": {
            "anyOf": [
              {
                "additionalProperties": true,
                "type": "object"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Metriccandidate"
          },
          "baseDefinitionRevision": {
            "anyOf": [
              {
                "type": "integer"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Basedefinitionrevision"
          },
          "addMetrics": {
            "items": {
              "$ref": "#/$defs/EditMetricAddition"
            },
            "title": "Addmetrics",
            "type": "array"
          },
          "deleteMetrics": {
            "items": {
              "$ref": "#/$defs/EditMetricDeletion"
            },
            "title": "Deletemetrics",
            "type": "array"
          },
          "componentSelection": {
            "anyOf": [
              {
                "additionalProperties": true,
                "type": "object"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Componentselection"
          },
          "timeWindow": {
            "anyOf": [
              {
                "additionalProperties": true,
                "type": "object"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Timewindow"
          },
          "timeWindowLabel": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Timewindowlabel"
          },
          "grain": {
            "anyOf": [
              {
                "additionalProperties": true,
                "type": "object"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Grain"
          },
          "supported": {
            "anyOf": [
              {
                "type": "boolean"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Supported"
          },
          "supportedGrains": {
            "items": {
              "type": "string"
            },
            "title": "Supportedgrains",
            "type": "array"
          },
          "topN": {
            "anyOf": [
              {
                "type": "integer"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Topn"
          },
          "sortField": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Sortfield"
          },
          "sortDirection": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Sortdirection"
          },
          "dataShape": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Datashape"
          },
          "displayOptions": {
            "items": {
              "additionalProperties": true,
              "type": "object"
            },
            "title": "Displayoptions",
            "type": "array"
          },
          "disabledOptions": {
            "items": {
              "additionalProperties": true,
              "type": "object"
            },
            "title": "Disabledoptions",
            "type": "array"
          },
          "markdownPreview": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Markdownpreview"
          },
          "reason": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Reason"
          },
          "presentationOperations": {
            "items": {
              "additionalProperties": true,
              "type": "object"
            },
            "title": "Presentationoperations",
            "type": "array"
          },
          "themeOperations": {
            "items": {
              "additionalProperties": true,
              "type": "object"
            },
            "title": "Themeoperations",
            "type": "array"
          }
        },
        "title": "EditOperation",
        "type": "object"
      },
      "required": [],
      "targetSpec": "target.regionId 或 target.matchedText 指明要改哪张卡片；组合修改用 operations 列表（旧格式每步共用顶层 target）",
      "resultSpec": "EditResult：draftAfterEdit / changeSet / reply / markdownPreview / needsClarification；形成待确认候选，用户接受后才正式写入",
      "states": [
        "WAITING_USER_CONFIRM",
        "USER_EDITING_TEMPLATE"
      ],
      "readOnly": false,
      "sideEffects": "在草稿副本上执行；通过后形成待确认候选，用户接受时才在正式事务里写入草稿。",
      "errors": [
        "目标不存在或不可唯一确定 → 返回 needsClarification + clarificationQuestion（草稿不变）",
        "候选基于旧版本草稿 → 拒绝（stale_edit_plan）",
        "依据的生效组合已变 → 拒绝（combination_conflict）",
        "数据源 / 指标不在当前授权范围 → 拒绝（incompatible_fields）"
      ],
      "limits": [
        "未确认不落库：confirm=false 时只算影响摘要并追问",
        "不改任何报表的定义（regions / layout / format 不动），定义版本也不 bump",
        "单步修改只针对一个区块；要一次改多处请用能力格式 operations"
      ],
      "code": {
        "registration": "app/schemas/edit_plan.py::EditIntent",
        "dispatch": "app/services/agent_service.py::AgentService._try_handle_template_edit",
        "validation": "app/schemas/edit_plan.py::EditPlan",
        "execution": "app/services/template_edit_engine.py::TemplateEditEngine._apply_save_theme"
      }
    },
    {
      "id": "UNKNOWN",
      "group": "旧意图 / 未识别",
      "entryType": "legacy_intent",
      "status": "available",
      "purpose": "无法识别的意图：转成追问，不改草稿。 （**旧格式入口，不是独立 Tool**：模型在 submit_edit_plan 的 intent / target / operation 里给出，由 template_edit_engine.TemplateEditEngine.apply 执行。）",
      "params": {
        "$defs": {
          "EditMetricAddition": {
            "description": "要**追加**到某个已有区块里的一个指标。\n\n「追加」与「替换」是两种不同的意图：追加**保留**该区块原有指标，\n把新指标加进去；替换才把原来的换掉。\n\n每个待追加指标至少能表达：用户原话意图、显示名、单位、展示模式。",
            "properties": {
              "intent": {
                "default": "",
                "title": "Intent",
                "type": "string"
              },
              "label": {
                "default": "",
                "title": "Label",
                "type": "string"
              },
              "unit": {
                "default": "",
                "title": "Unit",
                "type": "string"
              },
              "displayMode": {
                "default": "simulated",
                "title": "Displaymode",
                "type": "string"
              },
              "metricRef": {
                "anyOf": [
                  {
                    "type": "string"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Metricref"
              },
              "metricCandidate": {
                "anyOf": [
                  {
                    "additionalProperties": true,
                    "type": "object"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Metriccandidate"
              },
              "note": {
                "default": "",
                "title": "Note",
                "type": "string"
              }
            },
            "title": "EditMetricAddition",
            "type": "object"
          },
          "EditMetricDeletion": {
            "description": "要**删除**的一个指标 —— 卡片内的**一个指标实例**。\n\n「删掉某个指标」与「删掉整张卡片」是两件事（契约表 §4.3）：目标必须\n指向卡片内**真实存在**的那一项指标。\n\n身份用**稳定标识**记（``metricKey`` / ``simulatedId`` / ``fieldRef``），\n**不记数组下标** —— 删掉中间一项之后下标会漂移；``label`` 只是给人看\n的显示名，**不作为定位依据**（同名指标要澄清，不能删第一条匹配的）。",
            "properties": {
              "metricKey": {
                "default": "",
                "title": "Metrickey",
                "type": "string"
              },
              "simulatedId": {
                "anyOf": [
                  {
                    "type": "string"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Simulatedid"
              },
              "fieldRef": {
                "anyOf": [
                  {
                    "type": "string"
                  },
                  {
                    "type": "null"
                  }
                ],
                "default": null,
                "title": "Fieldref"
              },
              "label": {
                "default": "",
                "title": "Label",
                "type": "string"
              },
              "isPrimary": {
                "default": false,
                "title": "Isprimary",
                "type": "boolean"
              },
              "intent": {
                "default": "",
                "title": "Intent",
                "type": "string"
              }
            },
            "title": "EditMetricDeletion",
            "type": "object"
          }
        },
        "description": "具体改成什么。字段按 intent 不同用到的子集不同；这里统一松结构承载。",
        "properties": {
          "newTitle": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Newtitle"
          },
          "componentType": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Componenttype"
          },
          "chartKind": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Chartkind"
          },
          "sectionTitle": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Sectiontitle"
          },
          "insertAfterRegionId": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Insertafterregionid"
          },
          "sourceRef": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Sourceref"
          },
          "sourceVersion": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Sourceversion"
          },
          "fieldMappingRequired": {
            "anyOf": [
              {
                "type": "boolean"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Fieldmappingrequired"
          },
          "metricRef": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Metricref"
          },
          "metricVersion": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Metricversion"
          },
          "metricIntent": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Metricintent"
          },
          "metricCandidate": {
            "anyOf": [
              {
                "additionalProperties": true,
                "type": "object"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Metriccandidate"
          },
          "baseDefinitionRevision": {
            "anyOf": [
              {
                "type": "integer"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Basedefinitionrevision"
          },
          "addMetrics": {
            "items": {
              "$ref": "#/$defs/EditMetricAddition"
            },
            "title": "Addmetrics",
            "type": "array"
          },
          "deleteMetrics": {
            "items": {
              "$ref": "#/$defs/EditMetricDeletion"
            },
            "title": "Deletemetrics",
            "type": "array"
          },
          "componentSelection": {
            "anyOf": [
              {
                "additionalProperties": true,
                "type": "object"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Componentselection"
          },
          "timeWindow": {
            "anyOf": [
              {
                "additionalProperties": true,
                "type": "object"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Timewindow"
          },
          "timeWindowLabel": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Timewindowlabel"
          },
          "grain": {
            "anyOf": [
              {
                "additionalProperties": true,
                "type": "object"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Grain"
          },
          "supported": {
            "anyOf": [
              {
                "type": "boolean"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Supported"
          },
          "supportedGrains": {
            "items": {
              "type": "string"
            },
            "title": "Supportedgrains",
            "type": "array"
          },
          "topN": {
            "anyOf": [
              {
                "type": "integer"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Topn"
          },
          "sortField": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Sortfield"
          },
          "sortDirection": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Sortdirection"
          },
          "dataShape": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Datashape"
          },
          "displayOptions": {
            "items": {
              "additionalProperties": true,
              "type": "object"
            },
            "title": "Displayoptions",
            "type": "array"
          },
          "disabledOptions": {
            "items": {
              "additionalProperties": true,
              "type": "object"
            },
            "title": "Disabledoptions",
            "type": "array"
          },
          "markdownPreview": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Markdownpreview"
          },
          "reason": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "null"
              }
            ],
            "default": null,
            "title": "Reason"
          },
          "presentationOperations": {
            "items": {
              "additionalProperties": true,
              "type": "object"
            },
            "title": "Presentationoperations",
            "type": "array"
          },
          "themeOperations": {
            "items": {
              "additionalProperties": true,
              "type": "object"
            },
            "title": "Themeoperations",
            "type": "array"
          }
        },
        "title": "EditOperation",
        "type": "object"
      },
      "required": [],
      "targetSpec": "target.regionId 或 target.matchedText 指明要改哪张卡片；组合修改用 operations 列表（旧格式每步共用顶层 target）",
      "resultSpec": "EditResult：draftAfterEdit / changeSet / reply / markdownPreview / needsClarification；形成待确认候选，用户接受后才正式写入",
      "states": [
        "WAITING_USER_CONFIRM",
        "USER_EDITING_TEMPLATE"
      ],
      "readOnly": true,
      "sideEffects": "在草稿副本上执行；通过后形成待确认候选，用户接受时才在正式事务里写入草稿。",
      "errors": [
        "目标不存在或不可唯一确定 → 返回 needsClarification + clarificationQuestion（草稿不变）",
        "候选基于旧版本草稿 → 拒绝（stale_edit_plan）",
        "依据的生效组合已变 → 拒绝（combination_conflict）",
        "数据源 / 指标不在当前授权范围 → 拒绝（incompatible_fields）"
      ],
      "limits": [
        "不改草稿；转成一次追问",
        "单步修改只针对一个区块；要一次改多处请用能力格式 operations"
      ],
      "code": {
        "registration": "app/schemas/edit_plan.py::EditIntent",
        "dispatch": "app/services/agent_service.py::AgentService._try_handle_template_edit",
        "validation": "app/schemas/edit_plan.py::EditPlan",
        "execution": "app/services/template_edit_engine.py::TemplateEditEngine.apply"
      }
    },
    {
      "id": "CREATE_GROUP",
      "group": "展示操作 / 受约束白名单",
      "entryType": "operation",
      "status": "available",
      "purpose": "受约束的展示操作 CREATE_GROUP（**不是独立 Tool，也不是顶层能力**）：把若干张卡片放进一个**新的分组**（容器）里：给 groupId、memberRegionIds，可选 name / arrangement。 必须放在 SET_PRESENTATION 的 params.operations 里（形如 {\"op\": \"CREATE_GROUP\", \"params\": {...}}），不能直接作为 submit_edit_plan.operations 的顶层项。",
      "params": {
        "type": "object",
        "description": "展示操作 CREATE_GROUP 的参数对象。必填：groupId、memberRegionIds。可选：name、description、role、arrangement。多出来的键会被拒绝。",
        "properties": {
          "groupId": {
            "type": "string",
            "description": "groupId（取值见展示服务的校验）",
            "x-reference": "当前草稿里的分组 id（CREATE_GROUP 建出来的分组）"
          },
          "memberRegionIds": {
            "type": "array",
            "description": "memberRegionIds（取值见展示服务的校验）",
            "x-reference": "当前草稿里的区块 id 列表（至少一张卡片）",
            "items": {
              "type": "string"
            }
          },
          "name": {
            "type": "string",
            "description": "name（取值见展示服务的校验）"
          },
          "description": {
            "type": "string",
            "description": "description（取值见展示服务的校验）"
          },
          "role": {
            "type": "string",
            "description": "role（取值见展示服务的校验）"
          },
          "arrangement": {
            "type": "object",
            "description": "arrangement（对象，字段见下方）",
            "properties": {
              "mode": {
                "type": "string",
                "description": "排列模式",
                "enum": [
                  "GRID",
                  "FLOW"
                ],
                "default": "GRID"
              },
              "columns": {
                "type": "integer",
                "description": "列数",
                "minimum": 1,
                "maximum": 12,
                "default": 2
              },
              "direction": {
                "type": "string",
                "description": "排列方向",
                "enum": [
                  "HORIZONTAL",
                  "VERTICAL"
                ],
                "default": "HORIZONTAL"
              },
              "gap": {
                "type": "object",
                "description": "间距（长度类：value + unit）",
                "x-unit": "PX",
                "properties": {
                  "value": {
                    "type": "integer",
                    "description": "长度数值",
                    "minimum": 0,
                    "maximum": 96
                  },
                  "unit": {
                    "type": "string",
                    "description": "长度单位（只能 PX 或 PERCENT）",
                    "enum": [
                      "PX",
                      "PERCENT"
                    ]
                  }
                },
                "additionalProperties": false
              }
            },
            "additionalProperties": false
          }
        },
        "additionalProperties": false,
        "required": [
          "groupId",
          "memberRegionIds"
        ]
      },
      "required": [
        "groupId",
        "memberRegionIds"
      ],
      "targetSpec": "params 里的 regionId / groupId / memberRegionIds 等引用字段必须来自当前草稿（get_current_draft 返回的区块 id 清单）；groupId：当前草稿里的分组 id（CREATE_GROUP 建出来的分组）；memberRegionIds：当前草稿里的区块 id 列表（至少一张卡片）",
      "resultSpec": "整批展示操作的结果：PresentationResult（ok / changeSet / affectedRegionIds / issues）",
      "states": [
        "WAITING_USER_CONFIRM",
        "USER_EDITING_TEMPLATE"
      ],
      "readOnly": false,
      "sideEffects": "只改草稿副本的外观部分；正式写入同样要等用户接受候选后的事务。",
      "errors": [
        "op 不在白名单 → invalid_operation",
        "参数键不对 → invalid_operation（展示服务的参数校验）",
        "删除 / 移动导致引用断裂 → broken_reference（附引用清单）"
      ],
      "limits": [
        "只改外观：分组 / 排版 / 样式 / 格式 / 页面 / 元数据 / 主题",
        "不改统计口径、时间范围与设备范围",
        "一批操作是一个修改单位：整批成功才生效，任一步失败整批放弃",
        "arrangement.mode 取值域：GRID、FLOW",
        "arrangement.columns 取值范围：1 到 12",
        "arrangement.direction 取值域：HORIZONTAL、VERTICAL",
        "arrangement.gap.value 取值范围：0 到 96",
        "arrangement.gap.unit 取值域：PX、PERCENT",
        "最小结构示例（只给格式）：{\"op\": \"CREATE_GROUP\", \"params\": {\"groupId\": \"<groupId>\", \"memberRegionIds\": [\"<memberRegionIds>\"]}}"
      ],
      "code": {
        "registration": "app/services/report_presentation_service.py::ALLOWED_OPERATIONS",
        "dispatch": "app/services/report_presentation_service.py::apply_presentation",
        "validation": "app/services/report_presentation_service.py::normalize_presentation_operations",
        "execution": "app/services/report_presentation_service.py::_op_create_group"
      }
    },
    {
      "id": "SET_GROUP_ARRANGEMENT",
      "group": "展示操作 / 受约束白名单",
      "entryType": "operation",
      "status": "available",
      "purpose": "受约束的展示操作 SET_GROUP_ARRANGEMENT（**不是独立 Tool，也不是顶层能力**）：改一个**分组**里卡片的排列：给 groupId 与 arrangement（列数 / 方向 / 间距）。 必须放在 SET_PRESENTATION 的 params.operations 里（形如 {\"op\": \"SET_GROUP_ARRANGEMENT\", \"params\": {...}}），不能直接作为 submit_edit_plan.operations 的顶层项。",
      "params": {
        "type": "object",
        "description": "展示操作 SET_GROUP_ARRANGEMENT 的参数对象。必填：groupId、arrangement。多出来的键会被拒绝。",
        "properties": {
          "groupId": {
            "type": "string",
            "description": "groupId（取值见展示服务的校验）",
            "x-reference": "当前草稿里的分组 id（CREATE_GROUP 建出来的分组）"
          },
          "arrangement": {
            "type": "object",
            "description": "arrangement（对象，字段见下方）",
            "properties": {
              "mode": {
                "type": "string",
                "description": "排列模式",
                "enum": [
                  "GRID",
                  "FLOW"
                ],
                "default": "GRID"
              },
              "columns": {
                "type": "integer",
                "description": "列数",
                "minimum": 1,
                "maximum": 12,
                "default": 2
              },
              "direction": {
                "type": "string",
                "description": "排列方向",
                "enum": [
                  "HORIZONTAL",
                  "VERTICAL"
                ],
                "default": "HORIZONTAL"
              },
              "gap": {
                "type": "object",
                "description": "间距（长度类：value + unit）",
                "x-unit": "PX",
                "properties": {
                  "value": {
                    "type": "integer",
                    "description": "长度数值",
                    "minimum": 0,
                    "maximum": 96
                  },
                  "unit": {
                    "type": "string",
                    "description": "长度单位（只能 PX 或 PERCENT）",
                    "enum": [
                      "PX",
                      "PERCENT"
                    ]
                  }
                },
                "additionalProperties": false
              }
            },
            "additionalProperties": false
          }
        },
        "additionalProperties": false,
        "required": [
          "groupId",
          "arrangement"
        ]
      },
      "required": [
        "groupId",
        "arrangement"
      ],
      "targetSpec": "params 里的 regionId / groupId / memberRegionIds 等引用字段必须来自当前草稿（get_current_draft 返回的区块 id 清单）；groupId：当前草稿里的分组 id（CREATE_GROUP 建出来的分组）",
      "resultSpec": "整批展示操作的结果：PresentationResult（ok / changeSet / affectedRegionIds / issues）",
      "states": [
        "WAITING_USER_CONFIRM",
        "USER_EDITING_TEMPLATE"
      ],
      "readOnly": false,
      "sideEffects": "只改草稿副本的外观部分；正式写入同样要等用户接受候选后的事务。",
      "errors": [
        "op 不在白名单 → invalid_operation",
        "参数键不对 → invalid_operation（展示服务的参数校验）",
        "删除 / 移动导致引用断裂 → broken_reference（附引用清单）"
      ],
      "limits": [
        "只改外观：分组 / 排版 / 样式 / 格式 / 页面 / 元数据 / 主题",
        "不改统计口径、时间范围与设备范围",
        "一批操作是一个修改单位：整批成功才生效，任一步失败整批放弃",
        "arrangement.mode 取值域：GRID、FLOW",
        "arrangement.columns 取值范围：1 到 12",
        "arrangement.direction 取值域：HORIZONTAL、VERTICAL",
        "arrangement.gap.value 取值范围：0 到 96",
        "arrangement.gap.unit 取值域：PX、PERCENT",
        "最小结构示例（只给格式）：{\"op\": \"SET_GROUP_ARRANGEMENT\", \"params\": {\"groupId\": \"<groupId>\", \"arrangement\": {}}}"
      ],
      "code": {
        "registration": "app/services/report_presentation_service.py::ALLOWED_OPERATIONS",
        "dispatch": "app/services/report_presentation_service.py::apply_presentation",
        "validation": "app/services/report_presentation_service.py::normalize_presentation_operations",
        "execution": "app/services/report_presentation_service.py::_op_set_group_arrangement"
      }
    },
    {
      "id": "MOVE_REGION",
      "group": "展示操作 / 受约束白名单",
      "entryType": "operation",
      "status": "available",
      "purpose": "受约束的展示操作 MOVE_REGION（**不是独立 Tool，也不是顶层能力**）：**移动**一张卡片：给 regionId，可选 targetGroupId + index，或 afterRegionId / beforeRegionId。 必须放在 SET_PRESENTATION 的 params.operations 里（形如 {\"op\": \"MOVE_REGION\", \"params\": {...}}），不能直接作为 submit_edit_plan.operations 的顶层项。",
      "params": {
        "type": "object",
        "description": "展示操作 MOVE_REGION 的参数对象。必填：regionId。可选：targetGroupId、index、afterRegionId、beforeRegionId。多出来的键会被拒绝。",
        "properties": {
          "regionId": {
            "type": "string",
            "description": "regionId（取值见展示服务的校验）",
            "x-reference": "当前草稿里的区块 id（get_current_draft 返回的正文卡片 id 清单）"
          },
          "targetGroupId": {
            "type": "string",
            "description": "targetGroupId（取值见展示服务的校验）",
            "x-reference": "当前草稿里的分组 id"
          },
          "index": {
            "type": "string",
            "description": "index（取值见展示服务的校验）"
          },
          "afterRegionId": {
            "type": "string",
            "description": "afterRegionId（取值见展示服务的校验）",
            "x-reference": "当前草稿里的区块 id（相对位置锚点：放到这张卡后面）"
          },
          "beforeRegionId": {
            "type": "string",
            "description": "beforeRegionId（取值见展示服务的校验）",
            "x-reference": "当前草稿里的区块 id（相对位置锚点：放到这张卡前面）"
          }
        },
        "additionalProperties": false,
        "required": [
          "regionId"
        ]
      },
      "required": [
        "regionId"
      ],
      "targetSpec": "params 里的 regionId / groupId / memberRegionIds 等引用字段必须来自当前草稿（get_current_draft 返回的区块 id 清单）；regionId：当前草稿里的区块 id（get_current_draft 返回的正文卡片 id 清单）；targetGroupId：当前草稿里的分组 id；afterRegionId：当前草稿里的区块 id（相对位置锚点：放到这张卡后面）；beforeRegionId：当前草稿里的区块 id（相对位置锚点：放到这张卡前面）",
      "resultSpec": "整批展示操作的结果：PresentationResult（ok / changeSet / affectedRegionIds / issues）",
      "states": [
        "WAITING_USER_CONFIRM",
        "USER_EDITING_TEMPLATE"
      ],
      "readOnly": false,
      "sideEffects": "只改草稿副本的外观部分；正式写入同样要等用户接受候选后的事务。",
      "errors": [
        "op 不在白名单 → invalid_operation",
        "参数键不对 → invalid_operation（展示服务的参数校验）",
        "删除 / 移动导致引用断裂 → broken_reference（附引用清单）"
      ],
      "limits": [
        "只改外观：分组 / 排版 / 样式 / 格式 / 页面 / 元数据 / 主题",
        "不改统计口径、时间范围与设备范围",
        "一批操作是一个修改单位：整批成功才生效，任一步失败整批放弃",
        "最小结构示例（只给格式）：{\"op\": \"MOVE_REGION\", \"params\": {\"regionId\": \"<regionId>\"}}"
      ],
      "code": {
        "registration": "app/services/report_presentation_service.py::ALLOWED_OPERATIONS",
        "dispatch": "app/services/report_presentation_service.py::apply_presentation",
        "validation": "app/services/report_presentation_service.py::normalize_presentation_operations",
        "execution": "app/services/report_presentation_service.py::_op_move_region"
      }
    },
    {
      "id": "UNGROUP",
      "group": "展示操作 / 受约束白名单",
      "entryType": "operation",
      "status": "available",
      "purpose": "受约束的展示操作 UNGROUP（**不是独立 Tool，也不是顶层能力**）：**解散分组**：给 groupId，里面的卡片回到原来的层级。 必须放在 SET_PRESENTATION 的 params.operations 里（形如 {\"op\": \"UNGROUP\", \"params\": {...}}），不能直接作为 submit_edit_plan.operations 的顶层项。",
      "params": {
        "type": "object",
        "description": "展示操作 UNGROUP 的参数对象。必填：groupId。多出来的键会被拒绝。",
        "properties": {
          "groupId": {
            "type": "string",
            "description": "groupId（取值见展示服务的校验）",
            "x-reference": "当前草稿里的分组 id（CREATE_GROUP 建出来的分组）"
          }
        },
        "additionalProperties": false,
        "required": [
          "groupId"
        ]
      },
      "required": [
        "groupId"
      ],
      "targetSpec": "params 里的 regionId / groupId / memberRegionIds 等引用字段必须来自当前草稿（get_current_draft 返回的区块 id 清单）；groupId：当前草稿里的分组 id（CREATE_GROUP 建出来的分组）",
      "resultSpec": "整批展示操作的结果：PresentationResult（ok / changeSet / affectedRegionIds / issues）",
      "states": [
        "WAITING_USER_CONFIRM",
        "USER_EDITING_TEMPLATE"
      ],
      "readOnly": false,
      "sideEffects": "只改草稿副本的外观部分；正式写入同样要等用户接受候选后的事务。",
      "errors": [
        "op 不在白名单 → invalid_operation",
        "参数键不对 → invalid_operation（展示服务的参数校验）",
        "删除 / 移动导致引用断裂 → broken_reference（附引用清单）"
      ],
      "limits": [
        "只改外观：分组 / 排版 / 样式 / 格式 / 页面 / 元数据 / 主题",
        "不改统计口径、时间范围与设备范围",
        "一批操作是一个修改单位：整批成功才生效，任一步失败整批放弃",
        "最小结构示例（只给格式）：{\"op\": \"UNGROUP\", \"params\": {\"groupId\": \"<groupId>\"}}"
      ],
      "code": {
        "registration": "app/services/report_presentation_service.py::ALLOWED_OPERATIONS",
        "dispatch": "app/services/report_presentation_service.py::apply_presentation",
        "validation": "app/services/report_presentation_service.py::normalize_presentation_operations",
        "execution": "app/services/report_presentation_service.py::_op_ungroup"
      }
    },
    {
      "id": "DELETE_GROUP",
      "group": "展示操作 / 受约束白名单",
      "entryType": "operation",
      "status": "available",
      "purpose": "受约束的展示操作 DELETE_GROUP（**不是独立 Tool，也不是顶层能力**）：**删除分组**：给 groupId；组里还有卡片时默认整批失败（force=true 才强制）。 必须放在 SET_PRESENTATION 的 params.operations 里（形如 {\"op\": \"DELETE_GROUP\", \"params\": {...}}），不能直接作为 submit_edit_plan.operations 的顶层项。",
      "params": {
        "type": "object",
        "description": "展示操作 DELETE_GROUP 的参数对象。必填：groupId。可选：force。多出来的键会被拒绝。",
        "properties": {
          "groupId": {
            "type": "string",
            "description": "groupId（取值见展示服务的校验）",
            "x-reference": "当前草稿里的分组 id（CREATE_GROUP 建出来的分组）"
          },
          "force": {
            "type": "string",
            "description": "force（取值见展示服务的校验）"
          }
        },
        "additionalProperties": false,
        "required": [
          "groupId"
        ]
      },
      "required": [
        "groupId"
      ],
      "targetSpec": "params 里的 regionId / groupId / memberRegionIds 等引用字段必须来自当前草稿（get_current_draft 返回的区块 id 清单）；groupId：当前草稿里的分组 id（CREATE_GROUP 建出来的分组）",
      "resultSpec": "整批展示操作的结果：PresentationResult（ok / changeSet / affectedRegionIds / issues）",
      "states": [
        "WAITING_USER_CONFIRM",
        "USER_EDITING_TEMPLATE"
      ],
      "readOnly": false,
      "sideEffects": "只改草稿副本的外观部分；正式写入同样要等用户接受候选后的事务。",
      "errors": [
        "op 不在白名单 → invalid_operation",
        "参数键不对 → invalid_operation（展示服务的参数校验）",
        "删除 / 移动导致引用断裂 → broken_reference（附引用清单）"
      ],
      "limits": [
        "只改外观：分组 / 排版 / 样式 / 格式 / 页面 / 元数据 / 主题",
        "不改统计口径、时间范围与设备范围",
        "一批操作是一个修改单位：整批成功才生效，任一步失败整批放弃",
        "最小结构示例（只给格式）：{\"op\": \"DELETE_GROUP\", \"params\": {\"groupId\": \"<groupId>\"}}"
      ],
      "code": {
        "registration": "app/services/report_presentation_service.py::ALLOWED_OPERATIONS",
        "dispatch": "app/services/report_presentation_service.py::apply_presentation",
        "validation": "app/services/report_presentation_service.py::normalize_presentation_operations",
        "execution": "app/services/report_presentation_service.py::_op_delete_group"
      }
    },
    {
      "id": "COPY_REGION",
      "group": "展示操作 / 受约束白名单",
      "entryType": "operation",
      "status": "available",
      "purpose": "受约束的展示操作 COPY_REGION（**不是独立 Tool，也不是顶层能力**）：**复制**一张卡片：给 regionId 与 newRegionId（不能与现有 id 重复）。 必须放在 SET_PRESENTATION 的 params.operations 里（形如 {\"op\": \"COPY_REGION\", \"params\": {...}}），不能直接作为 submit_edit_plan.operations 的顶层项。",
      "params": {
        "type": "object",
        "description": "展示操作 COPY_REGION 的参数对象。必填：regionId、newRegionId。多出来的键会被拒绝。",
        "properties": {
          "regionId": {
            "type": "string",
            "description": "regionId（取值见展示服务的校验）",
            "x-reference": "当前草稿里的区块 id（get_current_draft 返回的正文卡片 id 清单）"
          },
          "newRegionId": {
            "type": "string",
            "description": "newRegionId（取值见展示服务的校验）",
            "x-reference": "新卡片使用的 id（不能与现有区块 id 重复）"
          }
        },
        "additionalProperties": false,
        "required": [
          "regionId",
          "newRegionId"
        ]
      },
      "required": [
        "regionId",
        "newRegionId"
      ],
      "targetSpec": "params 里的 regionId / groupId / memberRegionIds 等引用字段必须来自当前草稿（get_current_draft 返回的区块 id 清单）；regionId：当前草稿里的区块 id（get_current_draft 返回的正文卡片 id 清单）；newRegionId：新卡片使用的 id（不能与现有区块 id 重复）",
      "resultSpec": "整批展示操作的结果：PresentationResult（ok / changeSet / affectedRegionIds / issues）",
      "states": [
        "WAITING_USER_CONFIRM",
        "USER_EDITING_TEMPLATE"
      ],
      "readOnly": false,
      "sideEffects": "只改草稿副本的外观部分；正式写入同样要等用户接受候选后的事务。",
      "errors": [
        "op 不在白名单 → invalid_operation",
        "参数键不对 → invalid_operation（展示服务的参数校验）",
        "删除 / 移动导致引用断裂 → broken_reference（附引用清单）"
      ],
      "limits": [
        "只改外观：分组 / 排版 / 样式 / 格式 / 页面 / 元数据 / 主题",
        "不改统计口径、时间范围与设备范围",
        "一批操作是一个修改单位：整批成功才生效，任一步失败整批放弃",
        "最小结构示例（只给格式）：{\"op\": \"COPY_REGION\", \"params\": {\"regionId\": \"<regionId>\", \"newRegionId\": \"<newRegionId>\"}}"
      ],
      "code": {
        "registration": "app/services/report_presentation_service.py::ALLOWED_OPERATIONS",
        "dispatch": "app/services/report_presentation_service.py::apply_presentation",
        "validation": "app/services/report_presentation_service.py::normalize_presentation_operations",
        "execution": "app/services/report_presentation_service.py::_op_copy_region"
      }
    },
    {
      "id": "DELETE_REGION",
      "group": "展示操作 / 受约束白名单",
      "entryType": "operation",
      "status": "available",
      "purpose": "受约束的展示操作 DELETE_REGION（**不是独立 Tool，也不是顶层能力**）：**删除**一张卡片：给 regionId；有引用方时默认整批失败（force=true 才强制）。 必须放在 SET_PRESENTATION 的 params.operations 里（形如 {\"op\": \"DELETE_REGION\", \"params\": {...}}），不能直接作为 submit_edit_plan.operations 的顶层项。",
      "params": {
        "type": "object",
        "description": "展示操作 DELETE_REGION 的参数对象。必填：regionId。可选：force。多出来的键会被拒绝。",
        "properties": {
          "regionId": {
            "type": "string",
            "description": "regionId（取值见展示服务的校验）",
            "x-reference": "当前草稿里的区块 id（get_current_draft 返回的正文卡片 id 清单）"
          },
          "force": {
            "type": "string",
            "description": "force（取值见展示服务的校验）"
          }
        },
        "additionalProperties": false,
        "required": [
          "regionId"
        ]
      },
      "required": [
        "regionId"
      ],
      "targetSpec": "params 里的 regionId / groupId / memberRegionIds 等引用字段必须来自当前草稿（get_current_draft 返回的区块 id 清单）；regionId：当前草稿里的区块 id（get_current_draft 返回的正文卡片 id 清单）",
      "resultSpec": "整批展示操作的结果：PresentationResult（ok / changeSet / affectedRegionIds / issues）",
      "states": [
        "WAITING_USER_CONFIRM",
        "USER_EDITING_TEMPLATE"
      ],
      "readOnly": false,
      "sideEffects": "只改草稿副本的外观部分；正式写入同样要等用户接受候选后的事务。",
      "errors": [
        "op 不在白名单 → invalid_operation",
        "参数键不对 → invalid_operation（展示服务的参数校验）",
        "删除 / 移动导致引用断裂 → broken_reference（附引用清单）"
      ],
      "limits": [
        "只改外观：分组 / 排版 / 样式 / 格式 / 页面 / 元数据 / 主题",
        "不改统计口径、时间范围与设备范围",
        "一批操作是一个修改单位：整批成功才生效，任一步失败整批放弃",
        "最小结构示例（只给格式）：{\"op\": \"DELETE_REGION\", \"params\": {\"regionId\": \"<regionId>\"}}"
      ],
      "code": {
        "registration": "app/services/report_presentation_service.py::ALLOWED_OPERATIONS",
        "dispatch": "app/services/report_presentation_service.py::apply_presentation",
        "validation": "app/services/report_presentation_service.py::normalize_presentation_operations",
        "execution": "app/services/report_presentation_service.py::_op_delete_region"
      }
    },
    {
      "id": "HIDE_REGION",
      "group": "展示操作 / 受约束白名单",
      "entryType": "operation",
      "status": "available",
      "purpose": "受约束的展示操作 HIDE_REGION（**不是独立 Tool，也不是顶层能力**）：**隐藏**一张卡片（不删）：给 regionId。 必须放在 SET_PRESENTATION 的 params.operations 里（形如 {\"op\": \"HIDE_REGION\", \"params\": {...}}），不能直接作为 submit_edit_plan.operations 的顶层项。",
      "params": {
        "type": "object",
        "description": "展示操作 HIDE_REGION 的参数对象。必填：regionId。多出来的键会被拒绝。",
        "properties": {
          "regionId": {
            "type": "string",
            "description": "regionId（取值见展示服务的校验）",
            "x-reference": "当前草稿里的区块 id（get_current_draft 返回的正文卡片 id 清单）"
          }
        },
        "additionalProperties": false,
        "required": [
          "regionId"
        ]
      },
      "required": [
        "regionId"
      ],
      "targetSpec": "params 里的 regionId / groupId / memberRegionIds 等引用字段必须来自当前草稿（get_current_draft 返回的区块 id 清单）；regionId：当前草稿里的区块 id（get_current_draft 返回的正文卡片 id 清单）",
      "resultSpec": "整批展示操作的结果：PresentationResult（ok / changeSet / affectedRegionIds / issues）",
      "states": [
        "WAITING_USER_CONFIRM",
        "USER_EDITING_TEMPLATE"
      ],
      "readOnly": false,
      "sideEffects": "只改草稿副本的外观部分；正式写入同样要等用户接受候选后的事务。",
      "errors": [
        "op 不在白名单 → invalid_operation",
        "参数键不对 → invalid_operation（展示服务的参数校验）",
        "删除 / 移动导致引用断裂 → broken_reference（附引用清单）"
      ],
      "limits": [
        "只改外观：分组 / 排版 / 样式 / 格式 / 页面 / 元数据 / 主题",
        "不改统计口径、时间范围与设备范围",
        "一批操作是一个修改单位：整批成功才生效，任一步失败整批放弃",
        "最小结构示例（只给格式）：{\"op\": \"HIDE_REGION\", \"params\": {\"regionId\": \"<regionId>\"}}"
      ],
      "code": {
        "registration": "app/services/report_presentation_service.py::ALLOWED_OPERATIONS",
        "dispatch": "app/services/report_presentation_service.py::apply_presentation",
        "validation": "app/services/report_presentation_service.py::normalize_presentation_operations",
        "execution": "app/services/report_presentation_service.py::_op_hide_region"
      }
    },
    {
      "id": "SHOW_REGION",
      "group": "展示操作 / 受约束白名单",
      "entryType": "operation",
      "status": "available",
      "purpose": "受约束的展示操作 SHOW_REGION（**不是独立 Tool，也不是顶层能力**）：**恢复显示**一张隐藏的卡片：给 regionId。 必须放在 SET_PRESENTATION 的 params.operations 里（形如 {\"op\": \"SHOW_REGION\", \"params\": {...}}），不能直接作为 submit_edit_plan.operations 的顶层项。",
      "params": {
        "type": "object",
        "description": "展示操作 SHOW_REGION 的参数对象。必填：regionId。多出来的键会被拒绝。",
        "properties": {
          "regionId": {
            "type": "string",
            "description": "regionId（取值见展示服务的校验）",
            "x-reference": "当前草稿里的区块 id（get_current_draft 返回的正文卡片 id 清单）"
          }
        },
        "additionalProperties": false,
        "required": [
          "regionId"
        ]
      },
      "required": [
        "regionId"
      ],
      "targetSpec": "params 里的 regionId / groupId / memberRegionIds 等引用字段必须来自当前草稿（get_current_draft 返回的区块 id 清单）；regionId：当前草稿里的区块 id（get_current_draft 返回的正文卡片 id 清单）",
      "resultSpec": "整批展示操作的结果：PresentationResult（ok / changeSet / affectedRegionIds / issues）",
      "states": [
        "WAITING_USER_CONFIRM",
        "USER_EDITING_TEMPLATE"
      ],
      "readOnly": false,
      "sideEffects": "只改草稿副本的外观部分；正式写入同样要等用户接受候选后的事务。",
      "errors": [
        "op 不在白名单 → invalid_operation",
        "参数键不对 → invalid_operation（展示服务的参数校验）",
        "删除 / 移动导致引用断裂 → broken_reference（附引用清单）"
      ],
      "limits": [
        "只改外观：分组 / 排版 / 样式 / 格式 / 页面 / 元数据 / 主题",
        "不改统计口径、时间范围与设备范围",
        "一批操作是一个修改单位：整批成功才生效，任一步失败整批放弃",
        "最小结构示例（只给格式）：{\"op\": \"SHOW_REGION\", \"params\": {\"regionId\": \"<regionId>\"}}"
      ],
      "code": {
        "registration": "app/services/report_presentation_service.py::ALLOWED_OPERATIONS",
        "dispatch": "app/services/report_presentation_service.py::apply_presentation",
        "validation": "app/services/report_presentation_service.py::normalize_presentation_operations",
        "execution": "app/services/report_presentation_service.py::_op_show_region"
      }
    },
    {
      "id": "SET_PLACEMENT",
      "group": "展示操作 / 受约束白名单",
      "entryType": "operation",
      "status": "available",
      "purpose": "受约束的展示操作 SET_PLACEMENT（**不是独立 Tool，也不是顶层能力**）：改**定位**：给 regionId 与 placement（GRID / FLOW / ABSOLUTE）。 必须放在 SET_PRESENTATION 的 params.operations 里（形如 {\"op\": \"SET_PLACEMENT\", \"params\": {...}}），不能直接作为 submit_edit_plan.operations 的顶层项。",
      "params": {
        "type": "object",
        "description": "展示操作 SET_PLACEMENT 的参数对象。必填：regionId、placement。多出来的键会被拒绝。",
        "properties": {
          "regionId": {
            "type": "string",
            "description": "regionId（取值见展示服务的校验）",
            "x-reference": "当前草稿里的区块 id（get_current_draft 返回的正文卡片 id 清单）"
          },
          "placement": {
            "type": "object",
            "description": "placement（对象，字段见下方）",
            "properties": {
              "type": {
                "type": "string",
                "description": "定位方式",
                "enum": [
                  "GRID",
                  "FLOW",
                  "ABSOLUTE"
                ]
              },
              "column": {
                "type": "integer",
                "description": "网格定位的起始列",
                "minimum": 1,
                "maximum": 12
              },
              "columnSpan": {
                "type": "integer",
                "description": "网格定位的列跨度",
                "minimum": 1,
                "maximum": 12
              },
              "row": {
                "type": "integer",
                "description": "网格定位的行",
                "minimum": 1,
                "maximum": 999
              },
              "rowSpan": {
                "type": "integer",
                "description": "网格定位的行跨度",
                "minimum": 1,
                "maximum": 12
              },
              "align": {
                "type": "string",
                "description": "流式定位的对齐方式",
                "enum": [
                  "START",
                  "CENTER",
                  "END",
                  "STRETCH"
                ]
              },
              "width": {
                "type": "object",
                "description": "流式定位的宽度（长度类：value + unit）",
                "x-unit": "PX",
                "properties": {
                  "value": {
                    "type": "integer",
                    "description": "长度数值"
                  },
                  "unit": {
                    "type": "string",
                    "description": "长度单位（只能 PX 或 PERCENT）",
                    "enum": [
                      "PX",
                      "PERCENT"
                    ]
                  }
                },
                "additionalProperties": false
              },
              "minHeight": {
                "type": "object",
                "description": "最小高度（长度类：value + unit）",
                "x-unit": "PX",
                "properties": {
                  "value": {
                    "type": "integer",
                    "description": "长度数值"
                  },
                  "unit": {
                    "type": "string",
                    "description": "长度单位（只能 PX 或 PERCENT）",
                    "enum": [
                      "PX",
                      "PERCENT"
                    ]
                  }
                },
                "additionalProperties": false
              },
              "x": {
                "type": "integer",
                "description": "固定定位的横坐标",
                "minimum": 0,
                "maximum": 100000
              },
              "y": {
                "type": "integer",
                "description": "固定定位的纵坐标",
                "minimum": 0,
                "maximum": 100000
              },
              "height": {
                "type": "integer",
                "description": "固定定位的高度（整数像素）",
                "minimum": 1,
                "maximum": 100000
              },
              "zIndex": {
                "type": "integer",
                "description": "固定定位的层级",
                "minimum": 0,
                "maximum": 9999
              }
            },
            "additionalProperties": false
          }
        },
        "additionalProperties": false,
        "required": [
          "regionId",
          "placement"
        ]
      },
      "required": [
        "regionId",
        "placement"
      ],
      "targetSpec": "params 里的 regionId / groupId / memberRegionIds 等引用字段必须来自当前草稿（get_current_draft 返回的区块 id 清单）；regionId：当前草稿里的区块 id（get_current_draft 返回的正文卡片 id 清单）",
      "resultSpec": "整批展示操作的结果：PresentationResult（ok / changeSet / affectedRegionIds / issues）",
      "states": [
        "WAITING_USER_CONFIRM",
        "USER_EDITING_TEMPLATE"
      ],
      "readOnly": false,
      "sideEffects": "只改草稿副本的外观部分；正式写入同样要等用户接受候选后的事务。",
      "errors": [
        "op 不在白名单 → invalid_operation",
        "参数键不对 → invalid_operation（展示服务的参数校验）",
        "删除 / 移动导致引用断裂 → broken_reference（附引用清单）"
      ],
      "limits": [
        "只改外观：分组 / 排版 / 样式 / 格式 / 页面 / 元数据 / 主题",
        "不改统计口径、时间范围与设备范围",
        "一批操作是一个修改单位：整批成功才生效，任一步失败整批放弃",
        "placement.type 取值域：GRID、FLOW、ABSOLUTE",
        "placement.column 取值范围：1 到 12",
        "placement.columnSpan 取值范围：1 到 12",
        "placement.row 取值范围：1 到 999",
        "placement.rowSpan 取值范围：1 到 12",
        "placement.align 取值域：START、CENTER、END、STRETCH",
        "placement.width.unit 取值域：PX、PERCENT",
        "placement.minHeight.unit 取值域：PX、PERCENT",
        "placement.x 取值范围：0 到 100000",
        "placement.y 取值范围：0 到 100000",
        "placement.height 取值范围：1 到 100000",
        "placement.zIndex 取值范围：0 到 9999",
        "最小结构示例（只给格式）：{\"op\": \"SET_PLACEMENT\", \"params\": {\"regionId\": \"<regionId>\", \"placement\": {}}}"
      ],
      "code": {
        "registration": "app/services/report_presentation_service.py::ALLOWED_OPERATIONS",
        "dispatch": "app/services/report_presentation_service.py::apply_presentation",
        "validation": "app/services/report_presentation_service.py::normalize_presentation_operations",
        "execution": "app/services/report_presentation_service.py::_op_set_placement"
      }
    },
    {
      "id": "SET_STYLE",
      "group": "展示操作 / 受约束白名单",
      "entryType": "operation",
      "status": "available",
      "purpose": "受约束的展示操作 SET_STYLE（**不是独立 Tool，也不是顶层能力**）：改**外观**：给 regionId 与 style（字体 / 字号 / 字重 / 颜色 / 对齐 / 行高 / 内外边距 / 边框 / 圆角 / 透明度）。 必须放在 SET_PRESENTATION 的 params.operations 里（形如 {\"op\": \"SET_STYLE\", \"params\": {...}}），不能直接作为 submit_edit_plan.operations 的顶层项。",
      "params": {
        "type": "object",
        "description": "展示操作 SET_STYLE 的参数对象。必填：regionId、style。多出来的键会被拒绝。",
        "properties": {
          "regionId": {
            "type": "string",
            "description": "regionId（取值见展示服务的校验）",
            "x-reference": "当前草稿里的区块 id（get_current_draft 返回的正文卡片 id 清单）"
          },
          "style": {
            "type": "object",
            "description": "style（对象，字段见下方）",
            "properties": {
              "fontFamily": {
                "type": "string",
                "description": "字体名称",
                "maxLength": 200
              },
              "fontSize": {
                "type": "object",
                "description": "字号（长度类：value + unit）",
                "x-unit": "PX",
                "properties": {
                  "value": {
                    "type": "integer",
                    "description": "长度数值",
                    "minimum": 1,
                    "maximum": 200
                  },
                  "unit": {
                    "type": "string",
                    "description": "长度单位（只能 PX 或 PERCENT）",
                    "enum": [
                      "PX",
                      "PERCENT"
                    ]
                  }
                },
                "additionalProperties": false
              },
              "fontWeight": {
                "type": "integer",
                "description": "字重（纯数字，不是长度）",
                "minimum": 100,
                "maximum": 900
              },
              "textColor": {
                "type": "string",
                "description": "文字颜色",
                "maxLength": 64
              },
              "backgroundColor": {
                "type": "string",
                "description": "背景颜色",
                "maxLength": 64
              },
              "textAlign": {
                "type": "string",
                "description": "水平对齐",
                "enum": [
                  "LEFT",
                  "CENTER",
                  "RIGHT"
                ]
              },
              "verticalAlign": {
                "type": "string",
                "description": "垂直对齐",
                "enum": [
                  "TOP",
                  "MIDDLE",
                  "BOTTOM"
                ]
              },
              "lineHeight": {
                "type": "object",
                "description": "行高（长度类：value + unit）",
                "x-unit": "PX",
                "properties": {
                  "value": {
                    "type": "integer",
                    "description": "长度数值",
                    "minimum": 0,
                    "maximum": 400
                  },
                  "unit": {
                    "type": "string",
                    "description": "长度单位（只能 PX 或 PERCENT）",
                    "enum": [
                      "PX",
                      "PERCENT"
                    ]
                  }
                },
                "additionalProperties": false
              },
              "padding": {
                "type": "object",
                "description": "内边距（长度类：value + unit）",
                "x-unit": "PX",
                "properties": {
                  "value": {
                    "type": "integer",
                    "description": "长度数值"
                  },
                  "unit": {
                    "type": "string",
                    "description": "长度单位（只能 PX 或 PERCENT）",
                    "enum": [
                      "PX",
                      "PERCENT"
                    ]
                  }
                },
                "additionalProperties": false
              },
              "margin": {
                "type": "object",
                "description": "外边距（长度类：value + unit）",
                "x-unit": "PX",
                "properties": {
                  "value": {
                    "type": "integer",
                    "description": "长度数值",
                    "minimum": -200,
                    "maximum": 400
                  },
                  "unit": {
                    "type": "string",
                    "description": "长度单位（只能 PX 或 PERCENT）",
                    "enum": [
                      "PX",
                      "PERCENT"
                    ]
                  }
                },
                "additionalProperties": false
              },
              "border": {
                "type": "object",
                "description": "边框",
                "properties": {
                  "width": {
                    "type": "integer",
                    "description": "边框宽度（纯数字像素）",
                    "minimum": 0,
                    "maximum": 20
                  },
                  "style": {
                    "type": "string",
                    "description": "边框线型",
                    "enum": [
                      "SOLID",
                      "DASHED",
                      "DOTTED",
                      "NONE"
                    ]
                  },
                  "color": {
                    "type": "string",
                    "description": "边框颜色",
                    "maxLength": 64
                  }
                },
                "additionalProperties": false
              },
              "borderRadius": {
                "type": "integer",
                "description": "圆角（纯数字像素）",
                "minimum": 0,
                "maximum": 200
              },
              "opacity": {
                "type": "number",
                "description": "透明度（0 到 1）",
                "minimum": 0,
                "maximum": 1
              }
            },
            "additionalProperties": false
          }
        },
        "additionalProperties": false,
        "required": [
          "regionId",
          "style"
        ]
      },
      "required": [
        "regionId",
        "style"
      ],
      "targetSpec": "params 里的 regionId / groupId / memberRegionIds 等引用字段必须来自当前草稿（get_current_draft 返回的区块 id 清单）；regionId：当前草稿里的区块 id（get_current_draft 返回的正文卡片 id 清单）",
      "resultSpec": "整批展示操作的结果：PresentationResult（ok / changeSet / affectedRegionIds / issues）",
      "states": [
        "WAITING_USER_CONFIRM",
        "USER_EDITING_TEMPLATE"
      ],
      "readOnly": false,
      "sideEffects": "只改草稿副本的外观部分；正式写入同样要等用户接受候选后的事务。",
      "errors": [
        "op 不在白名单 → invalid_operation",
        "参数键不对 → invalid_operation（展示服务的参数校验）",
        "删除 / 移动导致引用断裂 → broken_reference（附引用清单）"
      ],
      "limits": [
        "只改外观：分组 / 排版 / 样式 / 格式 / 页面 / 元数据 / 主题",
        "不改统计口径、时间范围与设备范围",
        "一批操作是一个修改单位：整批成功才生效，任一步失败整批放弃",
        "style.fontSize.value 取值范围：1 到 200",
        "style.fontSize.unit 取值域：PX、PERCENT",
        "style.fontWeight 取值范围：100 到 900",
        "style.textAlign 取值域：LEFT、CENTER、RIGHT",
        "style.verticalAlign 取值域：TOP、MIDDLE、BOTTOM",
        "style.lineHeight.value 取值范围：0 到 400",
        "style.lineHeight.unit 取值域：PX、PERCENT",
        "style.padding.unit 取值域：PX、PERCENT",
        "style.margin.value 取值范围：-200 到 400",
        "style.margin.unit 取值域：PX、PERCENT",
        "style.border.width 取值范围：0 到 20",
        "style.border.style 取值域：SOLID、DASHED、DOTTED、NONE",
        "style.borderRadius 取值范围：0 到 200",
        "style.opacity 取值范围：0 到 1",
        "最小结构示例（只给格式）：{\"op\": \"SET_STYLE\", \"params\": {\"regionId\": \"<regionId>\", \"style\": {}}}"
      ],
      "code": {
        "registration": "app/services/report_presentation_service.py::ALLOWED_OPERATIONS",
        "dispatch": "app/services/report_presentation_service.py::apply_presentation",
        "validation": "app/services/report_presentation_service.py::normalize_presentation_operations",
        "execution": "app/services/report_presentation_service.py::_op_set_style"
      }
    },
    {
      "id": "SET_FORMAT",
      "group": "展示操作 / 受约束白名单",
      "entryType": "operation",
      "status": "available",
      "purpose": "受约束的展示操作 SET_FORMAT（**不是独立 Tool，也不是顶层能力**）：改**数字显示格式**：给 regionId 与 format（kind / precision / unitLabel / thousandSeparator / dateTimePattern / nullText / roundingMode）。 必须放在 SET_PRESENTATION 的 params.operations 里（形如 {\"op\": \"SET_FORMAT\", \"params\": {...}}），不能直接作为 submit_edit_plan.operations 的顶层项。",
      "params": {
        "type": "object",
        "description": "展示操作 SET_FORMAT 的参数对象。必填：regionId、format。多出来的键会被拒绝。",
        "properties": {
          "regionId": {
            "type": "string",
            "description": "regionId（取值见展示服务的校验）",
            "x-reference": "当前草稿里的区块 id（get_current_draft 返回的正文卡片 id 清单）"
          },
          "format": {
            "type": "object",
            "description": "format（对象，字段见下方）",
            "properties": {
              "kind": {
                "type": "string",
                "description": "显示格式类型",
                "enum": [
                  "NUMBER",
                  "PERCENT",
                  "CURRENCY",
                  "DURATION",
                  "DATE",
                  "DATETIME",
                  "TEXT"
                ]
              },
              "precision": {
                "type": "integer",
                "description": "小数位数",
                "minimum": 0,
                "maximum": 6
              },
              "unitLabel": {
                "type": "string",
                "description": "单位标签（只能保持当前单位或留空）",
                "maxLength": 64
              },
              "thousandSeparator": {
                "type": "boolean",
                "description": "是否加千分位"
              },
              "dateTimePattern": {
                "type": "string",
                "description": "日期时间格式",
                "maxLength": 64
              },
              "nullText": {
                "type": "string",
                "description": "空值显示文本",
                "maxLength": 64
              },
              "roundingMode": {
                "type": "string",
                "description": "舍入方式",
                "enum": [
                  "HALF_UP",
                  "HALF_DOWN",
                  "FLOOR",
                  "CEIL"
                ]
              }
            },
            "additionalProperties": false
          }
        },
        "additionalProperties": false,
        "required": [
          "regionId",
          "format"
        ]
      },
      "required": [
        "regionId",
        "format"
      ],
      "targetSpec": "params 里的 regionId / groupId / memberRegionIds 等引用字段必须来自当前草稿（get_current_draft 返回的区块 id 清单）；regionId：当前草稿里的区块 id（get_current_draft 返回的正文卡片 id 清单）",
      "resultSpec": "整批展示操作的结果：PresentationResult（ok / changeSet / affectedRegionIds / issues）",
      "states": [
        "WAITING_USER_CONFIRM",
        "USER_EDITING_TEMPLATE"
      ],
      "readOnly": false,
      "sideEffects": "只改草稿副本的外观部分；正式写入同样要等用户接受候选后的事务。",
      "errors": [
        "op 不在白名单 → invalid_operation",
        "参数键不对 → invalid_operation（展示服务的参数校验）",
        "删除 / 移动导致引用断裂 → broken_reference（附引用清单）"
      ],
      "limits": [
        "只改外观：分组 / 排版 / 样式 / 格式 / 页面 / 元数据 / 主题",
        "不改统计口径、时间范围与设备范围",
        "一批操作是一个修改单位：整批成功才生效，任一步失败整批放弃",
        "format.kind 取值域：NUMBER、PERCENT、CURRENCY、DURATION、DATE、DATETIME、TEXT",
        "format.precision 取值范围：0 到 6",
        "format.roundingMode 取值域：HALF_UP、HALF_DOWN、FLOOR、CEIL",
        "最小结构示例（只给格式）：{\"op\": \"SET_FORMAT\", \"params\": {\"regionId\": \"<regionId>\", \"format\": {}}}"
      ],
      "code": {
        "registration": "app/services/report_presentation_service.py::ALLOWED_OPERATIONS",
        "dispatch": "app/services/report_presentation_service.py::apply_presentation",
        "validation": "app/services/report_presentation_service.py::normalize_presentation_operations",
        "execution": "app/services/report_presentation_service.py::_op_set_format"
      }
    },
    {
      "id": "SET_PAGE",
      "group": "展示操作 / 受约束白名单",
      "entryType": "operation",
      "status": "available",
      "purpose": "受约束的展示操作 SET_PAGE（**不是独立 Tool，也不是顶层能力**）：改**整页设置**：给 webMaxWidth 与 / 或 arrangement。 必须放在 SET_PRESENTATION 的 params.operations 里（形如 {\"op\": \"SET_PAGE\", \"params\": {...}}），不能直接作为 submit_edit_plan.operations 的顶层项。",
      "params": {
        "type": "object",
        "description": "展示操作 SET_PAGE 的参数对象。没有必填键。可选：webMaxWidth、arrangement。多出来的键会被拒绝。",
        "properties": {
          "webMaxWidth": {
            "type": "object",
            "description": "webMaxWidth（对象，字段见下方）",
            "properties": {
              "value": {
                "type": "integer",
                "description": "长度数值",
                "minimum": 0,
                "maximum": 400
              },
              "unit": {
                "type": "string",
                "description": "长度单位（只能 PX 或 PERCENT）",
                "enum": [
                  "PX",
                  "PERCENT"
                ]
              }
            },
            "additionalProperties": false
          },
          "arrangement": {
            "type": "object",
            "description": "arrangement（对象，字段见下方）",
            "properties": {
              "mode": {
                "type": "string",
                "description": "排列模式",
                "enum": [
                  "GRID",
                  "FLOW"
                ],
                "default": "GRID"
              },
              "columns": {
                "type": "integer",
                "description": "列数",
                "minimum": 1,
                "maximum": 12,
                "default": 2
              },
              "direction": {
                "type": "string",
                "description": "排列方向",
                "enum": [
                  "HORIZONTAL",
                  "VERTICAL"
                ],
                "default": "HORIZONTAL"
              },
              "gap": {
                "type": "object",
                "description": "间距（长度类：value + unit）",
                "x-unit": "PX",
                "properties": {
                  "value": {
                    "type": "integer",
                    "description": "长度数值",
                    "minimum": 0,
                    "maximum": 96
                  },
                  "unit": {
                    "type": "string",
                    "description": "长度单位（只能 PX 或 PERCENT）",
                    "enum": [
                      "PX",
                      "PERCENT"
                    ]
                  }
                },
                "additionalProperties": false
              }
            },
            "additionalProperties": false
          }
        },
        "additionalProperties": false
      },
      "required": [],
      "targetSpec": "params 里的 regionId / groupId / memberRegionIds 等引用字段必须来自当前草稿（get_current_draft 返回的区块 id 清单）；",
      "resultSpec": "整批展示操作的结果：PresentationResult（ok / changeSet / affectedRegionIds / issues）",
      "states": [
        "WAITING_USER_CONFIRM",
        "USER_EDITING_TEMPLATE"
      ],
      "readOnly": false,
      "sideEffects": "只改草稿副本的外观部分；正式写入同样要等用户接受候选后的事务。",
      "errors": [
        "op 不在白名单 → invalid_operation",
        "参数键不对 → invalid_operation（展示服务的参数校验）",
        "删除 / 移动导致引用断裂 → broken_reference（附引用清单）"
      ],
      "limits": [
        "只改外观：分组 / 排版 / 样式 / 格式 / 页面 / 元数据 / 主题",
        "不改统计口径、时间范围与设备范围",
        "一批操作是一个修改单位：整批成功才生效，任一步失败整批放弃",
        "webMaxWidth.value 取值范围：0 到 400",
        "webMaxWidth.unit 取值域：PX、PERCENT",
        "arrangement.mode 取值域：GRID、FLOW",
        "arrangement.columns 取值范围：1 到 12",
        "arrangement.direction 取值域：HORIZONTAL、VERTICAL",
        "arrangement.gap.value 取值范围：0 到 96",
        "arrangement.gap.unit 取值域：PX、PERCENT",
        "最小结构示例（只给格式）：{\"op\": \"SET_PAGE\", \"params\": {}}"
      ],
      "code": {
        "registration": "app/services/report_presentation_service.py::ALLOWED_OPERATIONS",
        "dispatch": "app/services/report_presentation_service.py::apply_presentation",
        "validation": "app/services/report_presentation_service.py::normalize_presentation_operations",
        "execution": "app/services/report_presentation_service.py::_op_set_page"
      }
    },
    {
      "id": "SET_METADATA",
      "group": "展示操作 / 受约束白名单",
      "entryType": "operation",
      "status": "available",
      "purpose": "受约束的展示操作 SET_METADATA（**不是独立 Tool，也不是顶层能力**）：改**报表元数据**：给 name / description / tags。 必须放在 SET_PRESENTATION 的 params.operations 里（形如 {\"op\": \"SET_METADATA\", \"params\": {...}}），不能直接作为 submit_edit_plan.operations 的顶层项。",
      "params": {
        "type": "object",
        "description": "展示操作 SET_METADATA 的参数对象。没有必填键。可选：name、description、tags。多出来的键会被拒绝。",
        "properties": {
          "name": {
            "type": "string",
            "description": "name（取值见展示服务的校验）"
          },
          "description": {
            "type": "string",
            "description": "description（取值见展示服务的校验）"
          },
          "tags": {
            "type": "string",
            "description": "tags（取值见展示服务的校验）"
          }
        },
        "additionalProperties": false
      },
      "required": [],
      "targetSpec": "params 里的 regionId / groupId / memberRegionIds 等引用字段必须来自当前草稿（get_current_draft 返回的区块 id 清单）；",
      "resultSpec": "整批展示操作的结果：PresentationResult（ok / changeSet / affectedRegionIds / issues）",
      "states": [
        "WAITING_USER_CONFIRM",
        "USER_EDITING_TEMPLATE"
      ],
      "readOnly": false,
      "sideEffects": "只改草稿副本的外观部分；正式写入同样要等用户接受候选后的事务。",
      "errors": [
        "op 不在白名单 → invalid_operation",
        "参数键不对 → invalid_operation（展示服务的参数校验）",
        "删除 / 移动导致引用断裂 → broken_reference（附引用清单）"
      ],
      "limits": [
        "只改外观：分组 / 排版 / 样式 / 格式 / 页面 / 元数据 / 主题",
        "不改统计口径、时间范围与设备范围",
        "一批操作是一个修改单位：整批成功才生效，任一步失败整批放弃",
        "最小结构示例（只给格式）：{\"op\": \"SET_METADATA\", \"params\": {}}"
      ],
      "code": {
        "registration": "app/services/report_presentation_service.py::ALLOWED_OPERATIONS",
        "dispatch": "app/services/report_presentation_service.py::apply_presentation",
        "validation": "app/services/report_presentation_service.py::normalize_presentation_operations",
        "execution": "app/services/report_presentation_service.py::_op_set_metadata"
      }
    },
    {
      "id": "SET_THEME",
      "group": "展示操作 / 受约束白名单",
      "entryType": "operation",
      "status": "available",
      "purpose": "受约束的展示操作 SET_THEME（**不是独立 Tool，也不是顶层能力**）：把一个**共享主题**应用到这份报表：给 themeId（取自 availableThemes）。 必须放在 SET_PRESENTATION 的 params.operations 里（形如 {\"op\": \"SET_THEME\", \"params\": {...}}），不能直接作为 submit_edit_plan.operations 的顶层项。",
      "params": {
        "type": "object",
        "description": "展示操作 SET_THEME 的参数对象。必填：themeId。多出来的键会被拒绝。",
        "properties": {
          "themeId": {
            "type": "string",
            "description": "themeId（取值见展示服务的校验）"
          }
        },
        "additionalProperties": false,
        "required": [
          "themeId"
        ]
      },
      "required": [
        "themeId"
      ],
      "targetSpec": "params 里的 regionId / groupId / memberRegionIds 等引用字段必须来自当前草稿（get_current_draft 返回的区块 id 清单）；",
      "resultSpec": "整批展示操作的结果：PresentationResult（ok / changeSet / affectedRegionIds / issues）",
      "states": [
        "WAITING_USER_CONFIRM",
        "USER_EDITING_TEMPLATE"
      ],
      "readOnly": false,
      "sideEffects": "只改草稿副本的外观部分；正式写入同样要等用户接受候选后的事务。",
      "errors": [
        "op 不在白名单 → invalid_operation",
        "参数键不对 → invalid_operation（展示服务的参数校验）",
        "删除 / 移动导致引用断裂 → broken_reference（附引用清单）"
      ],
      "limits": [
        "只改外观：分组 / 排版 / 样式 / 格式 / 页面 / 元数据 / 主题",
        "不改统计口径、时间范围与设备范围",
        "一批操作是一个修改单位：整批成功才生效，任一步失败整批放弃",
        "最小结构示例（只给格式）：{\"op\": \"SET_THEME\", \"params\": {\"themeId\": \"<themeId>\"}}"
      ],
      "code": {
        "registration": "app/services/report_presentation_service.py::ALLOWED_OPERATIONS",
        "dispatch": "app/services/report_presentation_service.py::apply_presentation",
        "validation": "app/services/report_presentation_service.py::normalize_presentation_operations",
        "execution": "app/services/report_presentation_service.py::_op_set_theme"
      }
    },
    {
      "id": "RULE_TEMPLATE_EDIT",
      "group": "规则快路（确定性快路径，不是独立 Tool）",
      "entryType": "operation",
      "status": "available",
      "purpose": "规则快路（**不是独立 Tool**，模型调不到）：编辑态收到明确可判定的修改请求时，规则层直接解析出编辑计划并形成候选，不经模型。模型只处理规则认不出来的部分。",
      "params": {},
      "required": [],
      "targetSpec": "目标由「前端可见正文卡片 ID 的顺序」与用户点名共同确定；认不准就追问。",
      "resultSpec": "AgentResponse：待确认候选 + 「接受修改 / 撤回上一步 / 继续修改」菜单。",
      "states": [
        "WAITING_USER_CONFIRM",
        "USER_EDITING_TEMPLATE"
      ],
      "readOnly": false,
      "sideEffects": "只形成待确认候选；草稿的正式改动由用户接受后的事务落库。",
      "errors": [
        "指代词没选中对象 / 序数越界 → 转成一次澄清",
        "时间窗说不清 → 给具体时间段选项让用户选"
      ],
      "limits": [
        "只在 WAITING_USER_CONFIRM / USER_EDITING_TEMPLATE 生效",
        "只在规则明确命中时接管，认不出来就交回模型支路"
      ],
      "code": {
        "registration": "app/services/agent_service.py::AgentService._try_handle_template_edit",
        "dispatch": "app/services/agent_service.py::AgentService._try_handle_template_edit",
        "validation": "app/services/template_edit_engine.py::TemplateEditEngine.parse",
        "execution": "app/services/template_edit_engine.py::TemplateEditEngine.apply"
      }
    },
    {
      "id": "RULE_READONLY_QUERY_ANSWER",
      "group": "规则快路（确定性快路径，不是独立 Tool）",
      "entryType": "operation",
      "status": "available",
      "purpose": "规则快路（**不是独立 Tool**，模型调不到）：编辑态的只读能力查询（如「支持哪些粒度」）能用确定性证据回答时直接答。",
      "params": {},
      "required": [],
      "targetSpec": "无（只回答当前草稿的能力范围）。",
      "resultSpec": "AgentResponse：只追加聊天，不产生待确认计划，不推进业务状态。",
      "states": [
        "WAITING_USER_CONFIRM",
        "USER_EDITING_TEMPLATE"
      ],
      "readOnly": true,
      "sideEffects": "只读：不改草稿、不产生候选、不占修改澄清轮次。",
      "errors": [
        "查不到证据时返回空，不编造能力"
      ],
      "limits": [
        "只回答只读问题；涉及修改的请求一律走修改支路"
      ],
      "code": {
        "registration": "app/services/agent_service.py::AgentService._try_answer_readonly_query",
        "dispatch": "app/services/agent_service.py::AgentService._try_answer_readonly_query",
        "validation": "app/services/template_edit_engine.py::TemplateEditEngine.parse",
        "execution": "app/services/agent_service.py::AgentService._try_answer_readonly_query"
      }
    },
    {
      "id": "RULE_CORRECT_PLAN",
      "group": "规则快路（确定性快路径，不是独立 Tool）",
      "entryType": "operation",
      "status": "available",
      "purpose": "规则快路（**不是独立 Tool**，模型调不到）：用户对**当前待办**的纠正（「其他」自由文本）落到同一份计划上，不重开一轮普通对话。",
      "params": {},
      "required": [],
      "targetSpec": "目标必须仍是待办计划指的那张卡片；换卡片是新任务，不走这里。",
      "resultSpec": "修正后的 EditPlan（认不出来时返回 None → 同一张卡片里继续追问）。",
      "states": [
        "WAITING_USER_CONFIRM",
        "USER_EDITING_TEMPLATE"
      ],
      "readOnly": false,
      "sideEffects": "只修正计划本身，不改草稿。",
      "errors": [
        "纠正后仍说不清要改成什么样 → 返回 None（同一张卡片里继续追问）"
      ],
      "limits": [
        "只处理对当前待办的纠正，不接受换目标的指令"
      ],
      "code": {
        "registration": "app/services/template_edit_engine.py::TemplateEditEngine.correct_plan",
        "dispatch": "app/services/agent_service.py::AgentService._try_handle_template_edit",
        "validation": "app/schemas/edit_plan.py::EditPlan",
        "execution": "app/services/template_edit_engine.py::TemplateEditEngine.correct_plan"
      }
    },
    {
      "id": "RULE_EDIT_PLAN_PARSE",
      "group": "规则快路（确定性快路径，不是独立 Tool）",
      "entryType": "operation",
      "status": "available",
      "purpose": "规则快路（**不是独立 Tool**，模型调不到）：把一句自然语言修改要求解析成旧格式 EditPlan（intent / target / operation）。",
      "params": {},
      "required": [],
      "targetSpec": "目标由可见正文卡片顺序、用户点名与卡片别名共同确定。",
      "resultSpec": "EditPlan；认不出来时 intent=UNKNOWN 且 needsClarification=true。",
      "states": [
        "WAITING_USER_CONFIRM",
        "USER_EDITING_TEMPLATE"
      ],
      "readOnly": false,
      "sideEffects": "只解析出计划，不改草稿。",
      "errors": [
        "认不出来 → intent=UNKNOWN + 追问",
        "目标不唯一 → 追问是哪一张"
      ],
      "limits": [
        "只产出旧格式计划；能力格式由模型组装 operations 后走试做"
      ],
      "code": {
        "registration": "app/services/template_edit_engine.py::TemplateEditEngine.parse",
        "dispatch": "app/services/template_edit_engine.py::TemplateEditEngine.parse",
        "validation": "app/schemas/edit_plan.py::EditPlan",
        "execution": "app/services/template_edit_engine.py::TemplateEditEngine.apply"
      }
    }
  ],
  "simulatedDimensions": [
    {
      "dimension": "alertLevel",
      "title": "告警等级",
      "aliases": [
        "告警等级",
        "等级",
        "告警级别",
        "等级分布",
        "按等级",
        "告警等级分布"
      ],
      "unit": "条",
      "statistic": "按告警等级统计告警数量",
      "categories": [
        {
          "label": "严重",
          "value": 18
        },
        {
          "label": "重要",
          "value": 27
        },
        {
          "label": "一般",
          "value": 41
        },
        {
          "label": "提示",
          "value": 12
        }
      ]
    },
    {
      "dimension": "alertRegion",
      "title": "告警区域",
      "aliases": [
        "告警区域",
        "区域",
        "区域分布",
        "按区域",
        "告警区域分布"
      ],
      "unit": "条",
      "statistic": "按告警区域统计告警数量",
      "categories": [
        {
          "label": "华北机房",
          "value": 26
        },
        {
          "label": "华东机房",
          "value": 34
        },
        {
          "label": "华南机房",
          "value": 22
        },
        {
          "label": "西部机房",
          "value": 16
        }
      ]
    },
    {
      "dimension": "alertType",
      "title": "告警类型",
      "aliases": [
        "告警类型",
        "类型",
        "类型分布",
        "按类型",
        "告警类型分布"
      ],
      "unit": "条",
      "statistic": "按告警类型统计告警数量",
      "categories": [
        {
          "label": "温度越限",
          "value": 29
        },
        {
          "label": "电压异常",
          "value": 21
        },
        {
          "label": "通信中断",
          "value": 17
        },
        {
          "label": "设备离线",
          "value": 31
        }
      ]
    }
  ],
  "counts": {
    "agent_tool": 20,
    "operation": 37,
    "legacy_intent": 17
  },
  "contract": {
    "operations": [
      {
        "op": "ADD_SECTION",
        "title": "新增区块",
        "submission": "top_level",
        "packageOp": "",
        "packageField": "",
        "envelope": {
          "op": "ADD_SECTION",
          "params": {
            "title": "<title>"
          }
        },
        "required": [
          "title"
        ],
        "optional": [
          "componentType",
          "chartKind",
          "role",
          "dimension",
          "categories",
          "unit",
          "replaceRegionId",
          "afterRegionId",
          "beforeRegionId",
          "ref"
        ],
        "fields": [
          {
            "name": "title",
            "type": "string",
            "required": true,
            "minLength": 1
          },
          {
            "name": "componentType",
            "type": "string",
            "required": false,
            "description": "组件类型（TEXT / METRIC_CARD / TABLE / CHART）",
            "enum": [
              "ANALYSIS",
              "CHART",
              "CONTAINER",
              "DATA_QUALITY",
              "METRIC_CARD",
              "METRIC_GROUP",
              "TABLE",
              "TEXT"
            ]
          },
          {
            "name": "chartKind",
            "type": "string",
            "required": false,
            "description": "图表类型（componentType=CHART 时必填）",
            "enum": [
              "AREA",
              "BAR",
              "DONUT",
              "LINE",
              "PIE",
              "STACKED_BAR"
            ]
          },
          {
            "name": "role",
            "type": "string",
            "required": false
          },
          {
            "name": "dimension",
            "type": "string",
            "required": false,
            "description": "模拟分类维度（key 或业务别名）",
            "enum": [
              "alertLevel",
              "alertRegion",
              "alertType"
            ]
          },
          {
            "name": "categories",
            "type": "array",
            "required": false,
            "items": [
              {
                "name": "label",
                "type": "string",
                "required": true,
                "minLength": 1
              },
              {
                "name": "value",
                "type": "number",
                "required": false
              }
            ]
          },
          {
            "name": "unit",
            "type": "string",
            "required": false
          },
          {
            "name": "replaceRegionId",
            "type": "string",
            "required": false
          },
          {
            "name": "afterRegionId",
            "type": "string",
            "required": false
          },
          {
            "name": "beforeRegionId",
            "type": "string",
            "required": false
          },
          {
            "name": "ref",
            "type": "string",
            "required": false
          }
        ],
        "references": [],
        "limits": [
          "title 至少 1 个字符",
          "componentType 取值域：ANALYSIS、CHART、CONTAINER、DATA_QUALITY、METRIC_CARD、METRIC_GROUP、TABLE、TEXT",
          "chartKind 取值域：AREA、BAR、DONUT、LINE、PIE、STACKED_BAR",
          "dimension 取值域：alertLevel、alertRegion、alertType",
          "不需要目标（自己新建区块 / 整批作用于草稿）",
          "新增的区块可用 params.ref 起稳定引用名，后续步骤用 target.ref 指它"
        ],
        "available": true,
        "source": "app/services/edit_capabilities/base.py::register"
      },
      {
        "op": "DELETE_SECTION",
        "title": "删除区块",
        "submission": "top_level",
        "packageOp": "",
        "packageField": "",
        "envelope": {
          "op": "DELETE_SECTION",
          "target": {
            "regionId": "<regionId>"
          },
          "params": {}
        },
        "required": [],
        "optional": [],
        "fields": [],
        "references": [],
        "limits": [
          "每一步都要有明确目标：target.regionId（草稿里已有区块）或 target.ref（本次新增区块的引用名）"
        ],
        "available": true,
        "source": "app/services/edit_capabilities/base.py::register"
      },
      {
        "op": "SET_DISPLAY",
        "title": "设置展示类型",
        "submission": "top_level",
        "packageOp": "",
        "packageField": "",
        "envelope": {
          "op": "SET_DISPLAY",
          "target": {
            "regionId": "<regionId>"
          },
          "params": {}
        },
        "required": [],
        "optional": [
          "componentType",
          "chartKind"
        ],
        "fields": [
          {
            "name": "componentType",
            "type": "string",
            "required": false,
            "description": "组件类型（TEXT / METRIC_CARD / TABLE / CHART）",
            "enum": [
              "ANALYSIS",
              "CHART",
              "CONTAINER",
              "DATA_QUALITY",
              "METRIC_CARD",
              "METRIC_GROUP",
              "TABLE",
              "TEXT"
            ]
          },
          {
            "name": "chartKind",
            "type": "string",
            "required": false,
            "description": "图表类型（componentType=CHART 时必填）",
            "enum": [
              "AREA",
              "BAR",
              "DONUT",
              "LINE",
              "PIE",
              "STACKED_BAR"
            ]
          }
        ],
        "references": [],
        "limits": [
          "componentType 取值域：ANALYSIS、CHART、CONTAINER、DATA_QUALITY、METRIC_CARD、METRIC_GROUP、TABLE、TEXT",
          "chartKind 取值域：AREA、BAR、DONUT、LINE、PIE、STACKED_BAR",
          "每一步都要有明确目标：target.regionId（草稿里已有区块）或 target.ref（本次新增区块的引用名）"
        ],
        "available": true,
        "source": "app/services/edit_capabilities/base.py::register"
      },
      {
        "op": "SET_CATEGORY_DATA",
        "title": "设置分类统计口径",
        "submission": "top_level",
        "packageOp": "",
        "packageField": "",
        "envelope": {
          "op": "SET_CATEGORY_DATA",
          "target": {
            "regionId": "<regionId>"
          },
          "params": {}
        },
        "required": [],
        "optional": [
          "dimension",
          "categories",
          "unit"
        ],
        "fields": [
          {
            "name": "dimension",
            "type": "string",
            "required": false,
            "description": "模拟分类维度（key 或业务别名）",
            "enum": [
              "alertLevel",
              "alertRegion",
              "alertType"
            ]
          },
          {
            "name": "categories",
            "type": "array",
            "required": false,
            "items": [
              {
                "name": "label",
                "type": "string",
                "required": true,
                "minLength": 1
              },
              {
                "name": "value",
                "type": "number",
                "required": false
              }
            ]
          },
          {
            "name": "unit",
            "type": "string",
            "required": false
          }
        ],
        "references": [],
        "limits": [
          "dimension 取值域：alertLevel、alertRegion、alertType",
          "每一步都要有明确目标：target.regionId（草稿里已有区块）或 target.ref（本次新增区块的引用名）"
        ],
        "available": true,
        "source": "app/services/edit_capabilities/base.py::register"
      },
      {
        "op": "SET_PRESENTATION",
        "title": "设置样式与布局",
        "submission": "top_level",
        "packageOp": "",
        "packageField": "",
        "envelope": {
          "op": "SET_PRESENTATION",
          "params": {
            "operations": [
              {
                "op": "CREATE_GROUP",
                "params": {
                  "groupId": "<groupId>",
                  "memberRegionIds": [
                    "<memberRegionIds>"
                  ]
                }
              }
            ]
          }
        },
        "required": [],
        "optional": [
          "operations"
        ],
        "fields": [
          {
            "name": "operations",
            "type": "array",
            "required": false,
            "description": "外观修改包里的操作名单：非空列表，每一项形如 {\"op\": \"<展示操作名>\", \"params\": {...}}",
            "minItems": 1,
            "items": [
              {
                "name": "op",
                "type": "string",
                "required": true,
                "description": "展示操作名（包装能力里允许的那些）",
                "enum": [
                  "CREATE_GROUP",
                  "SET_GROUP_ARRANGEMENT",
                  "MOVE_REGION",
                  "UNGROUP",
                  "DELETE_GROUP",
                  "COPY_REGION",
                  "DELETE_REGION",
                  "HIDE_REGION",
                  "SHOW_REGION",
                  "SET_PLACEMENT",
                  "SET_STYLE",
                  "SET_FORMAT",
                  "SET_PAGE",
                  "SET_METADATA",
                  "SET_THEME"
                ]
              },
              {
                "name": "params",
                "type": "object",
                "required": false,
                "description": "该展示操作的参数对象",
                "freeForm": true
              }
            ]
          }
        ],
        "references": [],
        "limits": [
          "operations 至少 1 项",
          "不需要目标（自己新建区块 / 整批作用于草稿）"
        ],
        "available": true,
        "source": "app/services/edit_capabilities/base.py::register"
      },
      {
        "op": "SET_DATA_BINDING",
        "title": "设置展示绑定",
        "submission": "top_level",
        "packageOp": "",
        "packageField": "",
        "envelope": {
          "op": "SET_DATA_BINDING",
          "target": {
            "regionId": "<regionId>"
          },
          "params": {}
        },
        "required": [],
        "optional": [
          "outputRef",
          "fieldAliases",
          "clearFieldAliases",
          "presentationFilters",
          "clearPresentationFilters",
          "sort",
          "clearSort",
          "topN",
          "clearTopN"
        ],
        "fields": [
          {
            "name": "outputRef",
            "type": "string",
            "required": false,
            "description": "改绑的输出引用（output:<name>）"
          },
          {
            "name": "fieldAliases",
            "type": "object",
            "required": false,
            "description": "展示字段名 → 实际结果字段名，例如 {\"时间点\":\"timestamp\"}。右侧必须是已声明字段；也兼容可唯一识别的反向映射。",
            "freeForm": true
          },
          {
            "name": "clearFieldAliases",
            "type": "boolean",
            "required": false
          },
          {
            "name": "presentationFilters",
            "type": "array",
            "required": false,
            "description": "只过滤展示记录。条件形如 {\"type\":\"CONDITION\",\"left\":{\"type\":\"FIELD\",\"fieldRef\":\"energyKwh\"},\"operator\":\"GT\",\"operands\":[{\"type\":\"LITERAL\",\"value\":400}]}；组用 type=GROUP、logic=AND/OR/NOT、conditions=[条件]。也接收 fieldRef/operator/value 简写，统一转成上述结构。",
            "items": [
              {
                "name": "type",
                "type": "string",
                "required": false,
                "enum": [
                  "CONDITION",
                  "GROUP"
                ]
              },
              {
                "name": "left",
                "type": "object",
                "required": false,
                "properties": [
                  {
                    "name": "type",
                    "type": "string",
                    "required": true,
                    "enum": [
                      "FIELD"
                    ]
                  },
                  {
                    "name": "fieldRef",
                    "type": "string",
                    "required": true,
                    "description": "已声明的实际字段名"
                  }
                ]
              },
              {
                "name": "operator",
                "type": "string",
                "required": false,
                "enum": [
                  "EQ",
                  "NE",
                  "GT",
                  "GE",
                  "LT",
                  "LE",
                  "IN",
                  "NOT_IN",
                  "BETWEEN",
                  "CONTAINS",
                  "STARTS_WITH",
                  "IS_NULL",
                  "IS_NOT_NULL"
                ]
              },
              {
                "name": "operands",
                "type": "array",
                "required": false,
                "items": [
                  {
                    "name": "type",
                    "type": "string",
                    "required": true,
                    "enum": [
                      "LITERAL"
                    ]
                  },
                  {
                    "name": "value",
                    "type": "string",
                    "required": true,
                    "description": "字面量值；数字阈值用数字"
                  }
                ]
              },
              {
                "name": "logic",
                "type": "string",
                "required": false,
                "enum": [
                  "AND",
                  "OR",
                  "NOT"
                ]
              },
              {
                "name": "conditions",
                "type": "array",
                "required": false,
                "itemsType": "object"
              }
            ]
          },
          {
            "name": "clearPresentationFilters",
            "type": "boolean",
            "required": false
          },
          {
            "name": "sort",
            "type": "array",
            "required": false,
            "description": "展示排序字段（fieldRef / direction / nulls）",
            "itemsType": "object"
          },
          {
            "name": "clearSort",
            "type": "boolean",
            "required": false
          },
          {
            "name": "topN",
            "type": "integer",
            "required": false,
            "description": "展示前 N 项；只是展示裁剪，不限制取数"
          },
          {
            "name": "clearTopN",
            "type": "boolean",
            "required": false
          }
        ],
        "references": [],
        "limits": [
          "每一步都要有明确目标：target.regionId（草稿里已有区块）或 target.ref（本次新增区块的引用名）"
        ],
        "available": true,
        "source": "app/services/edit_capabilities/base.py::register"
      },
      {
        "op": "SET_CHART_KIND",
        "title": "设置图表类型",
        "submission": "top_level",
        "packageOp": "",
        "packageField": "",
        "envelope": {
          "op": "SET_CHART_KIND",
          "target": {
            "regionId": "<regionId>"
          },
          "params": {
            "chartKind": "AREA"
          }
        },
        "required": [
          "chartKind"
        ],
        "optional": [],
        "fields": [
          {
            "name": "chartKind",
            "type": "string",
            "required": true,
            "description": "图表类型（LINE / AREA / BAR / STACKED_BAR / PIE / DONUT）",
            "enum": [
              "AREA",
              "BAR",
              "DONUT",
              "LINE",
              "PIE",
              "STACKED_BAR"
            ],
            "minLength": 1
          }
        ],
        "references": [],
        "limits": [
          "chartKind 取值域：AREA、BAR、DONUT、LINE、PIE、STACKED_BAR",
          "chartKind 至少 1 个字符",
          "每一步都要有明确目标：target.regionId（草稿里已有区块）或 target.ref（本次新增区块的引用名）"
        ],
        "available": true,
        "source": "app/services/edit_capabilities/base.py::register"
      },
      {
        "op": "SET_CHART_ENCODINGS",
        "title": "设置图表通道绑定",
        "submission": "top_level",
        "packageOp": "",
        "packageField": "",
        "envelope": {
          "op": "SET_CHART_ENCODINGS",
          "target": {
            "regionId": "<regionId>"
          },
          "params": {}
        },
        "required": [],
        "optional": [
          "encodings"
        ],
        "fields": [
          {
            "name": "encodings",
            "type": "array",
            "required": false,
            "description": "完整的通道绑定清单：每一项形如 {\"channel\": \"X\", \"fieldRef\": \"<字段名>\", \"format\": {...}}；channel 取 X / Y / SERIES / COLOR / LABEL，每个通道最多一项。",
            "items": [
              {
                "name": "channel",
                "type": "string",
                "required": false,
                "description": "视觉通道（X / Y / SERIES / COLOR / LABEL）",
                "enum": [
                  "X",
                  "Y",
                  "SERIES",
                  "COLOR",
                  "LABEL"
                ]
              },
              {
                "name": "fieldRef",
                "type": "string",
                "required": false,
                "description": "数据字段名（必须已登记或属于模拟字段目录）"
              },
              {
                "name": "format",
                "type": "object",
                "required": false,
                "description": "该通道的显示格式（可选）",
                "freeForm": true
              }
            ]
          }
        ],
        "references": [],
        "limits": [
          "每一步都要有明确目标：target.regionId（草稿里已有区块）或 target.ref（本次新增区块的引用名）"
        ],
        "available": true,
        "source": "app/services/edit_capabilities/base.py::register"
      },
      {
        "op": "SET_CHART_AXES",
        "title": "设置图表坐标轴",
        "submission": "top_level",
        "packageOp": "",
        "packageField": "",
        "envelope": {
          "op": "SET_CHART_AXES",
          "target": {
            "regionId": "<regionId>"
          },
          "params": {}
        },
        "required": [],
        "optional": [
          "axes",
          "clearAxes"
        ],
        "fields": [
          {
            "name": "axes",
            "type": "object",
            "required": false,
            "description": "要设置的坐标轴：键是 'x' 或 'y'，值形如 {\"title\": \"...\", \"minimum\": 0, \"maximum\": 100, \"format\": {...}, \"scale\": \"LINEAR\"}；给哪个轴就改哪个轴，没给的轴保持原样。",
            "freeForm": true
          },
          {
            "name": "clearAxes",
            "type": "boolean",
            "required": false,
            "description": "是否先清空全部坐标轴（true = 清空后再应用 axes 里的内容）"
          }
        ],
        "references": [],
        "limits": [
          "每一步都要有明确目标：target.regionId（草稿里已有区块）或 target.ref（本次新增区块的引用名）"
        ],
        "available": true,
        "source": "app/services/edit_capabilities/base.py::register"
      },
      {
        "op": "SET_CHART_LEGEND",
        "title": "设置图例与系列配色",
        "submission": "top_level",
        "packageOp": "",
        "packageField": "",
        "envelope": {
          "op": "SET_CHART_LEGEND",
          "target": {
            "regionId": "<regionId>"
          },
          "params": {}
        },
        "required": [],
        "optional": [
          "legend",
          "seriesColors",
          "clearSeriesColors"
        ],
        "fields": [
          {
            "name": "legend",
            "type": "boolean",
            "required": false,
            "description": "是否显示图例（不填 = 不改；协议里图例只有开关，没有位置 / 方向）"
          },
          {
            "name": "seriesColors",
            "type": "object",
            "required": false,
            "description": "稳定系列键 → 颜色（十六进制 / rgb() / 颜色关键字或主题 token 名）",
            "freeForm": true
          },
          {
            "name": "clearSeriesColors",
            "type": "boolean",
            "required": false,
            "description": "是否先清空已有配色（true = 清空后再应用 seriesColors）"
          }
        ],
        "references": [],
        "limits": [
          "每一步都要有明确目标：target.regionId（草稿里已有区块）或 target.ref（本次新增区块的引用名）"
        ],
        "available": true,
        "source": "app/services/edit_capabilities/base.py::register"
      },
      {
        "op": "SET_CHART_TOOLTIP",
        "title": "设置图表提示字段",
        "submission": "top_level",
        "packageOp": "",
        "packageField": "",
        "envelope": {
          "op": "SET_CHART_TOOLTIP",
          "target": {
            "regionId": "<regionId>"
          },
          "params": {}
        },
        "required": [],
        "optional": [
          "tooltipFields",
          "clearTooltipFields"
        ],
        "fields": [
          {
            "name": "tooltipFields",
            "type": "array",
            "required": false,
            "description": "提示里要额外显示的字段名（非空时按此完整清单替换）",
            "itemsType": "string"
          },
          {
            "name": "clearTooltipFields",
            "type": "boolean",
            "required": false,
            "description": "是否清空提示字段（true = 清空；基础提示不受影响）"
          }
        ],
        "references": [],
        "limits": [
          "每一步都要有明确目标：target.regionId（草稿里已有区块）或 target.ref（本次新增区块的引用名）"
        ],
        "available": true,
        "source": "app/services/edit_capabilities/base.py::register"
      },
      {
        "op": "SET_CHART_REFERENCE_LINES",
        "title": "设置图表参考线",
        "submission": "top_level",
        "packageOp": "",
        "packageField": "",
        "envelope": {
          "op": "SET_CHART_REFERENCE_LINES",
          "target": {
            "regionId": "<regionId>"
          },
          "params": {}
        },
        "required": [],
        "optional": [
          "referenceLines",
          "clearReferenceLines"
        ],
        "fields": [
          {
            "name": "referenceLines",
            "type": "object",
            "required": false,
            "description": "参考线：名称 → 表达式，形如 {\"type\": \"LITERAL\", \"value\": 100}；数值按图表数值轴的单位理解。",
            "freeForm": true
          },
          {
            "name": "clearReferenceLines",
            "type": "boolean",
            "required": false,
            "description": "是否先清空已有参考线（true = 清空后再应用 referenceLines）"
          }
        ],
        "references": [],
        "limits": [
          "每一步都要有明确目标：target.regionId（草稿里已有区块）或 target.ref（本次新增区块的引用名）"
        ],
        "available": true,
        "source": "app/services/edit_capabilities/base.py::register"
      },
      {
        "op": "SET_TABLE_COLUMNS",
        "title": "设置表格列",
        "submission": "top_level",
        "packageOp": "",
        "packageField": "",
        "envelope": {
          "op": "SET_TABLE_COLUMNS",
          "target": {
            "regionId": "<regionId>"
          },
          "params": {}
        },
        "required": [],
        "optional": [
          "columns"
        ],
        "fields": [
          {
            "name": "columns",
            "type": "array",
            "required": false,
            "description": "完整的列清单（顺序即显示顺序）：每一项形如 {\"id\": \"col_1\", \"label\": \"机房名称\", \"fieldRef\": \"roomName\", \"format\": {\"kind\": \"TEXT\"}, \"width\": {\"value\": 120, \"unit\": \"PX\"}, \"style\": {\"textAlign\": \"LEFT\"}, \"headerPath\": [\"能耗\", \"总量\"]}；给了 id 就是改这一列（id 必须已存在），不给 id 就是新增一列。清单里没有的列**不再显示**（源字段、输出与指标一个都不删）。",
            "items": [
              {
                "name": "id",
                "type": "string",
                "required": false,
                "description": "列的稳定 id（改已有列时给，新增列不给）"
              },
              {
                "name": "label",
                "type": "string",
                "required": false,
                "description": "列标题（与取值字段互相独立）"
              },
              {
                "name": "fieldRef",
                "type": "string",
                "required": false,
                "description": "取值字段名（必须已登记）"
              },
              {
                "name": "format",
                "type": "object",
                "required": false,
                "description": "这一列的显示格式（可选；不给则沿用原格式）",
                "freeForm": true
              },
              {
                "name": "width",
                "type": "object",
                "required": false,
                "description": "列宽 {\"value\": 120, \"unit\": \"PX\"}（可选）",
                "freeForm": true
              },
              {
                "name": "style",
                "type": "object",
                "required": false,
                "description": "这一列的样式（可选；只认受控的外观键）",
                "freeForm": true
              },
              {
                "name": "headerPath",
                "type": "array",
                "required": false,
                "description": "多层表头路径（可选；每一层一段表头文字；[] 表示清空）"
              }
            ]
          }
        ],
        "references": [],
        "limits": [
          "每一步都要有明确目标：target.regionId（草稿里已有区块）或 target.ref（本次新增区块的引用名）"
        ],
        "available": true,
        "source": "app/services/edit_capabilities/base.py::register"
      },
      {
        "op": "SET_TABLE_DISPLAY",
        "title": "设置表格展示方式",
        "submission": "top_level",
        "packageOp": "",
        "packageField": "",
        "envelope": {
          "op": "SET_TABLE_DISPLAY",
          "target": {
            "regionId": "<regionId>"
          },
          "params": {}
        },
        "required": [],
        "optional": [
          "groupRowsBy",
          "clearGroupRowsBy",
          "rowNumber",
          "pageSize",
          "clearPageSize",
          "frozenColumnCount",
          "clearFrozenColumnCount"
        ],
        "fields": [
          {
            "name": "groupRowsBy",
            "type": "array",
            "required": false,
            "description": "按哪些字段分组显示行（只是展示分组，**不产生小计或聚合**）；清空用 clearGroupRowsBy",
            "itemsType": "string"
          },
          {
            "name": "clearGroupRowsBy",
            "type": "boolean",
            "required": false,
            "description": "是否清空行分组字段"
          },
          {
            "name": "rowNumber",
            "type": "boolean",
            "required": false,
            "description": "是否显示序号列（不填 = 不改）"
          },
          {
            "name": "pageSize",
            "type": "integer",
            "required": false,
            "description": "网页展示每页行数（只是**展示**分页，不限制取数与导出）"
          },
          {
            "name": "clearPageSize",
            "type": "boolean",
            "required": false,
            "description": "是否清掉每页行数（恢复成不分页）"
          },
          {
            "name": "frozenColumnCount",
            "type": "integer",
            "required": false,
            "description": "冻结前几列（不填 = 不改）"
          },
          {
            "name": "clearFrozenColumnCount",
            "type": "boolean",
            "required": false,
            "description": "是否清掉冻结列设置"
          }
        ],
        "references": [],
        "limits": [
          "每一步都要有明确目标：target.regionId（草稿里已有区块）或 target.ref（本次新增区块的引用名）"
        ],
        "available": true,
        "source": "app/services/edit_capabilities/base.py::register"
      },
      {
        "op": "SET_TABLE_SUMMARIES",
        "title": "设置表格汇总",
        "submission": "top_level",
        "packageOp": "",
        "packageField": "",
        "envelope": {
          "op": "SET_TABLE_SUMMARIES",
          "target": {
            "regionId": "<regionId>"
          },
          "params": {}
        },
        "required": [],
        "optional": [
          "summaries",
          "clearSummaries"
        ],
        "fields": [
          {
            "name": "summaries",
            "type": "array",
            "required": false,
            "description": "完整的汇总清单：每一项形如 {\"label\": \"告警总数\", \"outputRef\": \"output:alarms\", \"fieldRef\": \"totalAlarmCount\", \"basis\": \"FULL_RESULT\"}；outputRef 必须是这个区域**已登记**的计算输出，basis 只能是 FULL_RESULT（全量口径）或 VISIBLE_ROWS（可见行口径）。",
            "items": [
              {
                "name": "label",
                "type": "string",
                "required": true,
                "description": "汇总标签",
                "minLength": 1
              },
              {
                "name": "outputRef",
                "type": "string",
                "required": false,
                "description": "已登记的计算输出（output:<name>）"
              },
              {
                "name": "fieldRef",
                "type": "string",
                "required": false,
                "description": "汇总字段名"
              },
              {
                "name": "basis",
                "type": "string",
                "required": false,
                "description": "汇总口径（FULL_RESULT 全量口径 / VISIBLE_ROWS 可见行口径）"
              }
            ]
          },
          {
            "name": "clearSummaries",
            "type": "boolean",
            "required": false,
            "description": "是否清空全部汇总引用"
          }
        ],
        "references": [],
        "limits": [
          "每一步都要有明确目标：target.regionId（草稿里已有区块）或 target.ref（本次新增区块的引用名）"
        ],
        "available": true,
        "source": "app/services/edit_capabilities/base.py::register"
      },
      {
        "op": "SET_TEXT_CONTENT",
        "title": "设置文本正文",
        "submission": "top_level",
        "packageOp": "",
        "packageField": "",
        "envelope": {
          "op": "SET_TEXT_CONTENT",
          "target": {
            "regionId": "<regionId>"
          },
          "params": {}
        },
        "required": [],
        "optional": [
          "template",
          "clearTemplate",
          "format",
          "headingLevel",
          "clearHeadingLevel",
          "bindings",
          "clearBindings"
        ],
        "fields": [
          {
            "name": "template",
            "type": "string",
            "required": false,
            "description": "正文文本（空 = 不改）；里面的 {{占位符}} 按 bindings 取值"
          },
          {
            "name": "clearTemplate",
            "type": "boolean",
            "required": false,
            "description": "是否清空正文（true = 清空后再应用 template）"
          },
          {
            "name": "format",
            "type": "string",
            "required": false,
            "description": "文本格式：PLAIN（纯文本，保留换行）或 MARKDOWN（受控 Markdown）"
          },
          {
            "name": "headingLevel",
            "type": "integer",
            "required": false,
            "description": "目录层级（0 起的整数；不填 = 不改）—— 不是字号"
          },
          {
            "name": "clearHeadingLevel",
            "type": "boolean",
            "required": false,
            "description": "是否清空目录层级"
          },
          {
            "name": "bindings",
            "type": "object",
            "required": false,
            "description": "占位符 → 表达式：键是占位符名，值形如 {\"type\": \"FIELD\", \"fieldRef\": \"<字段名>\"}；给的项按名字合并进去，没给的保持原样。",
            "freeForm": true
          },
          {
            "name": "clearBindings",
            "type": "boolean",
            "required": false,
            "description": "是否先清空全部占位符绑定（true = 清空后再应用 bindings）"
          }
        ],
        "references": [],
        "limits": [
          "每一步都要有明确目标：target.regionId（草稿里已有区块）或 target.ref（本次新增区块的引用名）"
        ],
        "available": true,
        "source": "app/services/edit_capabilities/base.py::register"
      },
      {
        "op": "SET_METRIC_ITEM",
        "title": "设置指标卡",
        "submission": "top_level",
        "packageOp": "",
        "packageField": "",
        "envelope": {
          "op": "SET_METRIC_ITEM",
          "target": {
            "regionId": "<regionId>"
          },
          "params": {}
        },
        "required": [],
        "optional": [
          "label",
          "fieldRef",
          "outputRef",
          "clearOutputRef",
          "format",
          "clearFormat",
          "comparisonFieldRef",
          "clearComparisonFieldRef",
          "iconRef",
          "clearIconRef"
        ],
        "fields": [
          {
            "name": "label",
            "type": "string",
            "required": false,
            "description": "指标名称（空 = 不改）；改名字不碰数值、字段、单位与格式"
          },
          {
            "name": "fieldRef",
            "type": "string",
            "required": false,
            "description": "数值字段名（必须已登记或属于当前模拟字段目录；空 = 不改）"
          },
          {
            "name": "outputRef",
            "type": "string",
            "required": false,
            "description": "覆盖区域默认输出的引用（output:<name>，name 必须是本区域已登记的计算输出）"
          },
          {
            "name": "clearOutputRef",
            "type": "boolean",
            "required": false,
            "description": "是否清空输出覆盖（回到区域默认输出）"
          },
          {
            "name": "format",
            "type": "object",
            "required": false,
            "description": "指标的显示格式（空 = 不改）：kind / precision / unitLabel / thousandSeparator / dateTimePattern / nullText / roundingMode",
            "freeForm": true
          },
          {
            "name": "clearFormat",
            "type": "boolean",
            "required": false,
            "description": "是否清空指标格式"
          },
          {
            "name": "comparisonFieldRef",
            "type": "string",
            "required": false,
            "description": "对比字段名（必须已登记；单位要与指标字段一致）"
          },
          {
            "name": "clearComparisonFieldRef",
            "type": "boolean",
            "required": false,
            "description": "是否清空对比字段"
          },
          {
            "name": "iconRef",
            "type": "string",
            "required": false,
            "description": "注册图标引用（只认注册清单里的键；不接受图片 URL）"
          },
          {
            "name": "clearIconRef",
            "type": "boolean",
            "required": false,
            "description": "是否清空图标"
          }
        ],
        "references": [],
        "limits": [
          "每一步都要有明确目标：target.regionId（草稿里已有区块）或 target.ref（本次新增区块的引用名）"
        ],
        "available": true,
        "source": "app/services/edit_capabilities/base.py::register"
      },
      {
        "op": "SET_METRIC_GROUP",
        "title": "设置多指标组",
        "submission": "top_level",
        "packageOp": "",
        "packageField": "",
        "envelope": {
          "op": "SET_METRIC_GROUP",
          "target": {
            "regionId": "<regionId>"
          },
          "params": {}
        },
        "required": [],
        "optional": [
          "metrics",
          "arrangement",
          "create"
        ],
        "fields": [
          {
            "name": "metrics",
            "type": "array",
            "required": false,
            "description": "该指标组的**完整**指标项清单：每一项形如 {\"id\": \"m_1\", \"label\": \"总耗电量\", \"fieldRef\": \"totalEnergyKwh\", \"format\": {\"kind\": \"NUMBER\", \"precision\": 1}}；带 id 的项按 id 就地更新（id 必须已存在），不带 id 的是新项；清单里没有的项从展示清单里移除（**不删任何源数据 / 输出 / 列 / 区块**）。**不传这一项 = 只改组内排版，组里已有的指标项原样保留**（缺省不等于「清空」——清空会把用户没提的指标项悄悄抹掉）。"
          },
          {
            "name": "arrangement",
            "type": "object",
            "required": false,
            "description": "组内排版：{'mode': 'GRID'|'FLOW'|'ABSOLUTE', 'columns': 1..12, 'direction': 'VERTICAL'|'HORIZONTAL', 'gap': {'value': 0..96, 'unit': 'PX'|'PERCENT'}}",
            "freeForm": true
          },
          {
            "name": "create",
            "type": "boolean",
            "required": false,
            "description": "是否把目标区块改成多指标组（指标卡 → 指标组；正文为空的文本区块 → 指标组）"
          }
        ],
        "references": [],
        "limits": [
          "每一步都要有明确目标：target.regionId（草稿里已有区块）或 target.ref（本次新增区块的引用名）"
        ],
        "available": true,
        "source": "app/services/edit_capabilities/base.py::register"
      }
    ],
    "packages": [
      {
        "op": "SET_PRESENTATION",
        "title": "设置样式与布局",
        "payloadField": "operations",
        "payloadType": "array",
        "payloadMinItems": 1,
        "members": [
          "CREATE_GROUP",
          "SET_GROUP_ARRANGEMENT",
          "MOVE_REGION",
          "UNGROUP",
          "DELETE_GROUP",
          "COPY_REGION",
          "DELETE_REGION",
          "HIDE_REGION",
          "SHOW_REGION",
          "SET_PLACEMENT",
          "SET_STYLE",
          "SET_FORMAT",
          "SET_PAGE",
          "SET_METADATA",
          "SET_THEME"
        ],
        "envelope": {
          "op": "SET_PRESENTATION",
          "params": {
            "operations": [
              {
                "op": "CREATE_GROUP",
                "params": {
                  "groupId": "<groupId>",
                  "memberRegionIds": [
                    "<memberRegionIds>"
                  ]
                }
              }
            ]
          }
        },
        "available": true,
        "source": "app/services/edit_capabilities/base.py::register"
      }
    ],
    "internal": [
      {
        "op": "CREATE_GROUP",
        "title": "CREATE_GROUP",
        "submission": "package_internal",
        "packageOp": "",
        "packageField": "",
        "envelope": {
          "op": "CREATE_GROUP",
          "params": {
            "groupId": "<groupId>",
            "memberRegionIds": [
              "<memberRegionIds>"
            ]
          }
        },
        "required": [
          "groupId",
          "memberRegionIds"
        ],
        "optional": [
          "name",
          "description",
          "role",
          "arrangement"
        ],
        "fields": [
          {
            "name": "groupId",
            "type": "string",
            "required": true,
            "description": "groupId（取值见展示服务的校验）",
            "reference": "当前草稿里的分组 id（CREATE_GROUP 建出来的分组）"
          },
          {
            "name": "memberRegionIds",
            "type": "array",
            "required": true,
            "description": "memberRegionIds（取值见展示服务的校验）",
            "reference": "当前草稿里的区块 id 列表（至少一张卡片）",
            "itemsType": "string"
          },
          {
            "name": "name",
            "type": "string",
            "required": false,
            "description": "name（取值见展示服务的校验）"
          },
          {
            "name": "description",
            "type": "string",
            "required": false,
            "description": "description（取值见展示服务的校验）"
          },
          {
            "name": "role",
            "type": "string",
            "required": false,
            "description": "role（取值见展示服务的校验）"
          },
          {
            "name": "arrangement",
            "type": "object",
            "required": false,
            "description": "arrangement（对象，字段见下方）",
            "properties": [
              {
                "name": "mode",
                "type": "string",
                "required": false,
                "description": "排列模式",
                "enum": [
                  "GRID",
                  "FLOW"
                ]
              },
              {
                "name": "columns",
                "type": "integer",
                "required": false,
                "description": "列数",
                "minimum": 1,
                "maximum": 12
              },
              {
                "name": "direction",
                "type": "string",
                "required": false,
                "description": "排列方向",
                "enum": [
                  "HORIZONTAL",
                  "VERTICAL"
                ]
              },
              {
                "name": "gap",
                "type": "object",
                "required": false,
                "description": "间距（长度类：value + unit）",
                "unit": "PX",
                "properties": [
                  {
                    "name": "value",
                    "type": "integer",
                    "required": false,
                    "description": "长度数值",
                    "minimum": 0,
                    "maximum": 96
                  },
                  {
                    "name": "unit",
                    "type": "string",
                    "required": false,
                    "description": "长度单位（只能 PX 或 PERCENT）",
                    "enum": [
                      "PX",
                      "PERCENT"
                    ]
                  }
                ]
              }
            ]
          }
        ],
        "references": [
          "groupId：当前草稿里的分组 id（CREATE_GROUP 建出来的分组）",
          "memberRegionIds：当前草稿里的区块 id 列表（至少一张卡片）"
        ],
        "limits": [
          "arrangement.mode 取值域：GRID、FLOW",
          "arrangement.columns 取值范围：1 到 12",
          "arrangement.direction 取值域：HORIZONTAL、VERTICAL",
          "arrangement.gap.value 取值范围：0 到 96",
          "arrangement.gap.unit 取值域：PX、PERCENT"
        ],
        "available": true,
        "source": "app/services/report_presentation_service.py::_op_create_group"
      },
      {
        "op": "SET_GROUP_ARRANGEMENT",
        "title": "SET_GROUP_ARRANGEMENT",
        "submission": "package_internal",
        "packageOp": "",
        "packageField": "",
        "envelope": {
          "op": "SET_GROUP_ARRANGEMENT",
          "params": {
            "groupId": "<groupId>",
            "arrangement": {}
          }
        },
        "required": [
          "groupId",
          "arrangement"
        ],
        "optional": [],
        "fields": [
          {
            "name": "groupId",
            "type": "string",
            "required": true,
            "description": "groupId（取值见展示服务的校验）",
            "reference": "当前草稿里的分组 id（CREATE_GROUP 建出来的分组）"
          },
          {
            "name": "arrangement",
            "type": "object",
            "required": true,
            "description": "arrangement（对象，字段见下方）",
            "properties": [
              {
                "name": "mode",
                "type": "string",
                "required": false,
                "description": "排列模式",
                "enum": [
                  "GRID",
                  "FLOW"
                ]
              },
              {
                "name": "columns",
                "type": "integer",
                "required": false,
                "description": "列数",
                "minimum": 1,
                "maximum": 12
              },
              {
                "name": "direction",
                "type": "string",
                "required": false,
                "description": "排列方向",
                "enum": [
                  "HORIZONTAL",
                  "VERTICAL"
                ]
              },
              {
                "name": "gap",
                "type": "object",
                "required": false,
                "description": "间距（长度类：value + unit）",
                "unit": "PX",
                "properties": [
                  {
                    "name": "value",
                    "type": "integer",
                    "required": false,
                    "description": "长度数值",
                    "minimum": 0,
                    "maximum": 96
                  },
                  {
                    "name": "unit",
                    "type": "string",
                    "required": false,
                    "description": "长度单位（只能 PX 或 PERCENT）",
                    "enum": [
                      "PX",
                      "PERCENT"
                    ]
                  }
                ]
              }
            ]
          }
        ],
        "references": [
          "groupId：当前草稿里的分组 id（CREATE_GROUP 建出来的分组）"
        ],
        "limits": [
          "arrangement.mode 取值域：GRID、FLOW",
          "arrangement.columns 取值范围：1 到 12",
          "arrangement.direction 取值域：HORIZONTAL、VERTICAL",
          "arrangement.gap.value 取值范围：0 到 96",
          "arrangement.gap.unit 取值域：PX、PERCENT"
        ],
        "available": true,
        "source": "app/services/report_presentation_service.py::_op_set_group_arrangement"
      },
      {
        "op": "MOVE_REGION",
        "title": "MOVE_REGION",
        "submission": "package_internal",
        "packageOp": "",
        "packageField": "",
        "envelope": {
          "op": "MOVE_REGION",
          "params": {
            "regionId": "<regionId>"
          }
        },
        "required": [
          "regionId"
        ],
        "optional": [
          "targetGroupId",
          "index",
          "afterRegionId",
          "beforeRegionId"
        ],
        "fields": [
          {
            "name": "regionId",
            "type": "string",
            "required": true,
            "description": "regionId（取值见展示服务的校验）",
            "reference": "当前草稿里的区块 id（get_current_draft 返回的正文卡片 id 清单）"
          },
          {
            "name": "targetGroupId",
            "type": "string",
            "required": false,
            "description": "targetGroupId（取值见展示服务的校验）",
            "reference": "当前草稿里的分组 id"
          },
          {
            "name": "index",
            "type": "string",
            "required": false,
            "description": "index（取值见展示服务的校验）"
          },
          {
            "name": "afterRegionId",
            "type": "string",
            "required": false,
            "description": "afterRegionId（取值见展示服务的校验）",
            "reference": "当前草稿里的区块 id（相对位置锚点：放到这张卡后面）"
          },
          {
            "name": "beforeRegionId",
            "type": "string",
            "required": false,
            "description": "beforeRegionId（取值见展示服务的校验）",
            "reference": "当前草稿里的区块 id（相对位置锚点：放到这张卡前面）"
          }
        ],
        "references": [
          "regionId：当前草稿里的区块 id（get_current_draft 返回的正文卡片 id 清单）",
          "targetGroupId：当前草稿里的分组 id",
          "afterRegionId：当前草稿里的区块 id（相对位置锚点：放到这张卡后面）",
          "beforeRegionId：当前草稿里的区块 id（相对位置锚点：放到这张卡前面）"
        ],
        "limits": [],
        "available": true,
        "source": "app/services/report_presentation_service.py::_op_move_region"
      },
      {
        "op": "UNGROUP",
        "title": "UNGROUP",
        "submission": "package_internal",
        "packageOp": "",
        "packageField": "",
        "envelope": {
          "op": "UNGROUP",
          "params": {
            "groupId": "<groupId>"
          }
        },
        "required": [
          "groupId"
        ],
        "optional": [],
        "fields": [
          {
            "name": "groupId",
            "type": "string",
            "required": true,
            "description": "groupId（取值见展示服务的校验）",
            "reference": "当前草稿里的分组 id（CREATE_GROUP 建出来的分组）"
          }
        ],
        "references": [
          "groupId：当前草稿里的分组 id（CREATE_GROUP 建出来的分组）"
        ],
        "limits": [],
        "available": true,
        "source": "app/services/report_presentation_service.py::_op_ungroup"
      },
      {
        "op": "DELETE_GROUP",
        "title": "DELETE_GROUP",
        "submission": "package_internal",
        "packageOp": "",
        "packageField": "",
        "envelope": {
          "op": "DELETE_GROUP",
          "params": {
            "groupId": "<groupId>"
          }
        },
        "required": [
          "groupId"
        ],
        "optional": [
          "force"
        ],
        "fields": [
          {
            "name": "groupId",
            "type": "string",
            "required": true,
            "description": "groupId（取值见展示服务的校验）",
            "reference": "当前草稿里的分组 id（CREATE_GROUP 建出来的分组）"
          },
          {
            "name": "force",
            "type": "string",
            "required": false,
            "description": "force（取值见展示服务的校验）"
          }
        ],
        "references": [
          "groupId：当前草稿里的分组 id（CREATE_GROUP 建出来的分组）"
        ],
        "limits": [],
        "available": true,
        "source": "app/services/report_presentation_service.py::_op_delete_group"
      },
      {
        "op": "COPY_REGION",
        "title": "COPY_REGION",
        "submission": "package_internal",
        "packageOp": "",
        "packageField": "",
        "envelope": {
          "op": "COPY_REGION",
          "params": {
            "regionId": "<regionId>",
            "newRegionId": "<newRegionId>"
          }
        },
        "required": [
          "regionId",
          "newRegionId"
        ],
        "optional": [],
        "fields": [
          {
            "name": "regionId",
            "type": "string",
            "required": true,
            "description": "regionId（取值见展示服务的校验）",
            "reference": "当前草稿里的区块 id（get_current_draft 返回的正文卡片 id 清单）"
          },
          {
            "name": "newRegionId",
            "type": "string",
            "required": true,
            "description": "newRegionId（取值见展示服务的校验）",
            "reference": "新卡片使用的 id（不能与现有区块 id 重复）"
          }
        ],
        "references": [
          "regionId：当前草稿里的区块 id（get_current_draft 返回的正文卡片 id 清单）",
          "newRegionId：新卡片使用的 id（不能与现有区块 id 重复）"
        ],
        "limits": [],
        "available": true,
        "source": "app/services/report_presentation_service.py::_op_copy_region"
      },
      {
        "op": "DELETE_REGION",
        "title": "DELETE_REGION",
        "submission": "package_internal",
        "packageOp": "",
        "packageField": "",
        "envelope": {
          "op": "DELETE_REGION",
          "params": {
            "regionId": "<regionId>"
          }
        },
        "required": [
          "regionId"
        ],
        "optional": [
          "force"
        ],
        "fields": [
          {
            "name": "regionId",
            "type": "string",
            "required": true,
            "description": "regionId（取值见展示服务的校验）",
            "reference": "当前草稿里的区块 id（get_current_draft 返回的正文卡片 id 清单）"
          },
          {
            "name": "force",
            "type": "string",
            "required": false,
            "description": "force（取值见展示服务的校验）"
          }
        ],
        "references": [
          "regionId：当前草稿里的区块 id（get_current_draft 返回的正文卡片 id 清单）"
        ],
        "limits": [],
        "available": true,
        "source": "app/services/report_presentation_service.py::_op_delete_region"
      },
      {
        "op": "HIDE_REGION",
        "title": "HIDE_REGION",
        "submission": "package_internal",
        "packageOp": "",
        "packageField": "",
        "envelope": {
          "op": "HIDE_REGION",
          "params": {
            "regionId": "<regionId>"
          }
        },
        "required": [
          "regionId"
        ],
        "optional": [],
        "fields": [
          {
            "name": "regionId",
            "type": "string",
            "required": true,
            "description": "regionId（取值见展示服务的校验）",
            "reference": "当前草稿里的区块 id（get_current_draft 返回的正文卡片 id 清单）"
          }
        ],
        "references": [
          "regionId：当前草稿里的区块 id（get_current_draft 返回的正文卡片 id 清单）"
        ],
        "limits": [],
        "available": true,
        "source": "app/services/report_presentation_service.py::_op_hide_region"
      },
      {
        "op": "SHOW_REGION",
        "title": "SHOW_REGION",
        "submission": "package_internal",
        "packageOp": "",
        "packageField": "",
        "envelope": {
          "op": "SHOW_REGION",
          "params": {
            "regionId": "<regionId>"
          }
        },
        "required": [
          "regionId"
        ],
        "optional": [],
        "fields": [
          {
            "name": "regionId",
            "type": "string",
            "required": true,
            "description": "regionId（取值见展示服务的校验）",
            "reference": "当前草稿里的区块 id（get_current_draft 返回的正文卡片 id 清单）"
          }
        ],
        "references": [
          "regionId：当前草稿里的区块 id（get_current_draft 返回的正文卡片 id 清单）"
        ],
        "limits": [],
        "available": true,
        "source": "app/services/report_presentation_service.py::_op_show_region"
      },
      {
        "op": "SET_PLACEMENT",
        "title": "SET_PLACEMENT",
        "submission": "package_internal",
        "packageOp": "",
        "packageField": "",
        "envelope": {
          "op": "SET_PLACEMENT",
          "params": {
            "regionId": "<regionId>",
            "placement": {}
          }
        },
        "required": [
          "regionId",
          "placement"
        ],
        "optional": [],
        "fields": [
          {
            "name": "regionId",
            "type": "string",
            "required": true,
            "description": "regionId（取值见展示服务的校验）",
            "reference": "当前草稿里的区块 id（get_current_draft 返回的正文卡片 id 清单）"
          },
          {
            "name": "placement",
            "type": "object",
            "required": true,
            "description": "placement（对象，字段见下方）",
            "properties": [
              {
                "name": "type",
                "type": "string",
                "required": false,
                "description": "定位方式",
                "enum": [
                  "GRID",
                  "FLOW",
                  "ABSOLUTE"
                ]
              },
              {
                "name": "column",
                "type": "integer",
                "required": false,
                "description": "网格定位的起始列",
                "minimum": 1,
                "maximum": 12
              },
              {
                "name": "columnSpan",
                "type": "integer",
                "required": false,
                "description": "网格定位的列跨度",
                "minimum": 1,
                "maximum": 12
              },
              {
                "name": "row",
                "type": "integer",
                "required": false,
                "description": "网格定位的行",
                "minimum": 1,
                "maximum": 999
              },
              {
                "name": "rowSpan",
                "type": "integer",
                "required": false,
                "description": "网格定位的行跨度",
                "minimum": 1,
                "maximum": 12
              },
              {
                "name": "align",
                "type": "string",
                "required": false,
                "description": "流式定位的对齐方式",
                "enum": [
                  "START",
                  "CENTER",
                  "END",
                  "STRETCH"
                ]
              },
              {
                "name": "width",
                "type": "object",
                "required": false,
                "description": "流式定位的宽度（长度类：value + unit）",
                "unit": "PX",
                "properties": [
                  {
                    "name": "value",
                    "type": "integer",
                    "required": false,
                    "description": "长度数值"
                  },
                  {
                    "name": "unit",
                    "type": "string",
                    "required": false,
                    "description": "长度单位（只能 PX 或 PERCENT）",
                    "enum": [
                      "PX",
                      "PERCENT"
                    ]
                  }
                ]
              },
              {
                "name": "minHeight",
                "type": "object",
                "required": false,
                "description": "最小高度（长度类：value + unit）",
                "unit": "PX",
                "properties": [
                  {
                    "name": "value",
                    "type": "integer",
                    "required": false,
                    "description": "长度数值"
                  },
                  {
                    "name": "unit",
                    "type": "string",
                    "required": false,
                    "description": "长度单位（只能 PX 或 PERCENT）",
                    "enum": [
                      "PX",
                      "PERCENT"
                    ]
                  }
                ]
              },
              {
                "name": "x",
                "type": "integer",
                "required": false,
                "description": "固定定位的横坐标",
                "minimum": 0,
                "maximum": 100000
              },
              {
                "name": "y",
                "type": "integer",
                "required": false,
                "description": "固定定位的纵坐标",
                "minimum": 0,
                "maximum": 100000
              },
              {
                "name": "height",
                "type": "integer",
                "required": false,
                "description": "固定定位的高度（整数像素）",
                "minimum": 1,
                "maximum": 100000
              },
              {
                "name": "zIndex",
                "type": "integer",
                "required": false,
                "description": "固定定位的层级",
                "minimum": 0,
                "maximum": 9999
              }
            ]
          }
        ],
        "references": [
          "regionId：当前草稿里的区块 id（get_current_draft 返回的正文卡片 id 清单）"
        ],
        "limits": [
          "placement.type 取值域：GRID、FLOW、ABSOLUTE",
          "placement.column 取值范围：1 到 12",
          "placement.columnSpan 取值范围：1 到 12",
          "placement.row 取值范围：1 到 999",
          "placement.rowSpan 取值范围：1 到 12",
          "placement.align 取值域：START、CENTER、END、STRETCH",
          "placement.width.unit 取值域：PX、PERCENT",
          "placement.minHeight.unit 取值域：PX、PERCENT",
          "placement.x 取值范围：0 到 100000",
          "placement.y 取值范围：0 到 100000",
          "placement.height 取值范围：1 到 100000",
          "placement.zIndex 取值范围：0 到 9999"
        ],
        "available": true,
        "source": "app/services/report_presentation_service.py::_op_set_placement"
      },
      {
        "op": "SET_STYLE",
        "title": "SET_STYLE",
        "submission": "package_internal",
        "packageOp": "",
        "packageField": "",
        "envelope": {
          "op": "SET_STYLE",
          "params": {
            "regionId": "<regionId>",
            "style": {}
          }
        },
        "required": [
          "regionId",
          "style"
        ],
        "optional": [],
        "fields": [
          {
            "name": "regionId",
            "type": "string",
            "required": true,
            "description": "regionId（取值见展示服务的校验）",
            "reference": "当前草稿里的区块 id（get_current_draft 返回的正文卡片 id 清单）"
          },
          {
            "name": "style",
            "type": "object",
            "required": true,
            "description": "style（对象，字段见下方）",
            "properties": [
              {
                "name": "fontFamily",
                "type": "string",
                "required": false,
                "description": "字体名称",
                "maxLength": 200
              },
              {
                "name": "fontSize",
                "type": "object",
                "required": false,
                "description": "字号（长度类：value + unit）",
                "unit": "PX",
                "properties": [
                  {
                    "name": "value",
                    "type": "integer",
                    "required": false,
                    "description": "长度数值",
                    "minimum": 1,
                    "maximum": 200
                  },
                  {
                    "name": "unit",
                    "type": "string",
                    "required": false,
                    "description": "长度单位（只能 PX 或 PERCENT）",
                    "enum": [
                      "PX",
                      "PERCENT"
                    ]
                  }
                ]
              },
              {
                "name": "fontWeight",
                "type": "integer",
                "required": false,
                "description": "字重（纯数字，不是长度）",
                "minimum": 100,
                "maximum": 900
              },
              {
                "name": "textColor",
                "type": "string",
                "required": false,
                "description": "文字颜色",
                "maxLength": 64
              },
              {
                "name": "backgroundColor",
                "type": "string",
                "required": false,
                "description": "背景颜色",
                "maxLength": 64
              },
              {
                "name": "textAlign",
                "type": "string",
                "required": false,
                "description": "水平对齐",
                "enum": [
                  "LEFT",
                  "CENTER",
                  "RIGHT"
                ]
              },
              {
                "name": "verticalAlign",
                "type": "string",
                "required": false,
                "description": "垂直对齐",
                "enum": [
                  "TOP",
                  "MIDDLE",
                  "BOTTOM"
                ]
              },
              {
                "name": "lineHeight",
                "type": "object",
                "required": false,
                "description": "行高（长度类：value + unit）",
                "unit": "PX",
                "properties": [
                  {
                    "name": "value",
                    "type": "integer",
                    "required": false,
                    "description": "长度数值",
                    "minimum": 0,
                    "maximum": 400
                  },
                  {
                    "name": "unit",
                    "type": "string",
                    "required": false,
                    "description": "长度单位（只能 PX 或 PERCENT）",
                    "enum": [
                      "PX",
                      "PERCENT"
                    ]
                  }
                ]
              },
              {
                "name": "padding",
                "type": "object",
                "required": false,
                "description": "内边距（长度类：value + unit）",
                "unit": "PX",
                "properties": [
                  {
                    "name": "value",
                    "type": "integer",
                    "required": false,
                    "description": "长度数值"
                  },
                  {
                    "name": "unit",
                    "type": "string",
                    "required": false,
                    "description": "长度单位（只能 PX 或 PERCENT）",
                    "enum": [
                      "PX",
                      "PERCENT"
                    ]
                  }
                ]
              },
              {
                "name": "margin",
                "type": "object",
                "required": false,
                "description": "外边距（长度类：value + unit）",
                "unit": "PX",
                "properties": [
                  {
                    "name": "value",
                    "type": "integer",
                    "required": false,
                    "description": "长度数值",
                    "minimum": -200,
                    "maximum": 400
                  },
                  {
                    "name": "unit",
                    "type": "string",
                    "required": false,
                    "description": "长度单位（只能 PX 或 PERCENT）",
                    "enum": [
                      "PX",
                      "PERCENT"
                    ]
                  }
                ]
              },
              {
                "name": "border",
                "type": "object",
                "required": false,
                "description": "边框",
                "properties": [
                  {
                    "name": "width",
                    "type": "integer",
                    "required": false,
                    "description": "边框宽度（纯数字像素）",
                    "minimum": 0,
                    "maximum": 20
                  },
                  {
                    "name": "style",
                    "type": "string",
                    "required": false,
                    "description": "边框线型",
                    "enum": [
                      "SOLID",
                      "DASHED",
                      "DOTTED",
                      "NONE"
                    ]
                  },
                  {
                    "name": "color",
                    "type": "string",
                    "required": false,
                    "description": "边框颜色",
                    "maxLength": 64
                  }
                ]
              },
              {
                "name": "borderRadius",
                "type": "integer",
                "required": false,
                "description": "圆角（纯数字像素）",
                "minimum": 0,
                "maximum": 200
              },
              {
                "name": "opacity",
                "type": "number",
                "required": false,
                "description": "透明度（0 到 1）",
                "minimum": 0,
                "maximum": 1
              }
            ]
          }
        ],
        "references": [
          "regionId：当前草稿里的区块 id（get_current_draft 返回的正文卡片 id 清单）"
        ],
        "limits": [
          "style.fontSize.value 取值范围：1 到 200",
          "style.fontSize.unit 取值域：PX、PERCENT",
          "style.fontWeight 取值范围：100 到 900",
          "style.textAlign 取值域：LEFT、CENTER、RIGHT",
          "style.verticalAlign 取值域：TOP、MIDDLE、BOTTOM",
          "style.lineHeight.value 取值范围：0 到 400",
          "style.lineHeight.unit 取值域：PX、PERCENT",
          "style.padding.unit 取值域：PX、PERCENT",
          "style.margin.value 取值范围：-200 到 400",
          "style.margin.unit 取值域：PX、PERCENT",
          "style.border.width 取值范围：0 到 20",
          "style.border.style 取值域：SOLID、DASHED、DOTTED、NONE",
          "style.borderRadius 取值范围：0 到 200",
          "style.opacity 取值范围：0 到 1"
        ],
        "available": true,
        "source": "app/services/report_presentation_service.py::_op_set_style"
      },
      {
        "op": "SET_FORMAT",
        "title": "SET_FORMAT",
        "submission": "package_internal",
        "packageOp": "",
        "packageField": "",
        "envelope": {
          "op": "SET_FORMAT",
          "params": {
            "regionId": "<regionId>",
            "format": {}
          }
        },
        "required": [
          "regionId",
          "format"
        ],
        "optional": [],
        "fields": [
          {
            "name": "regionId",
            "type": "string",
            "required": true,
            "description": "regionId（取值见展示服务的校验）",
            "reference": "当前草稿里的区块 id（get_current_draft 返回的正文卡片 id 清单）"
          },
          {
            "name": "format",
            "type": "object",
            "required": true,
            "description": "format（对象，字段见下方）",
            "properties": [
              {
                "name": "kind",
                "type": "string",
                "required": false,
                "description": "显示格式类型",
                "enum": [
                  "NUMBER",
                  "PERCENT",
                  "CURRENCY",
                  "DURATION",
                  "DATE",
                  "DATETIME",
                  "TEXT"
                ]
              },
              {
                "name": "precision",
                "type": "integer",
                "required": false,
                "description": "小数位数",
                "minimum": 0,
                "maximum": 6
              },
              {
                "name": "unitLabel",
                "type": "string",
                "required": false,
                "description": "单位标签（只能保持当前单位或留空）",
                "maxLength": 64
              },
              {
                "name": "thousandSeparator",
                "type": "boolean",
                "required": false,
                "description": "是否加千分位"
              },
              {
                "name": "dateTimePattern",
                "type": "string",
                "required": false,
                "description": "日期时间格式",
                "maxLength": 64
              },
              {
                "name": "nullText",
                "type": "string",
                "required": false,
                "description": "空值显示文本",
                "maxLength": 64
              },
              {
                "name": "roundingMode",
                "type": "string",
                "required": false,
                "description": "舍入方式",
                "enum": [
                  "HALF_UP",
                  "HALF_DOWN",
                  "FLOOR",
                  "CEIL"
                ]
              }
            ]
          }
        ],
        "references": [
          "regionId：当前草稿里的区块 id（get_current_draft 返回的正文卡片 id 清单）"
        ],
        "limits": [
          "format.kind 取值域：NUMBER、PERCENT、CURRENCY、DURATION、DATE、DATETIME、TEXT",
          "format.precision 取值范围：0 到 6",
          "format.roundingMode 取值域：HALF_UP、HALF_DOWN、FLOOR、CEIL"
        ],
        "available": true,
        "source": "app/services/report_presentation_service.py::_op_set_format"
      },
      {
        "op": "SET_PAGE",
        "title": "SET_PAGE",
        "submission": "package_internal",
        "packageOp": "",
        "packageField": "",
        "envelope": {
          "op": "SET_PAGE",
          "params": {}
        },
        "required": [],
        "optional": [
          "webMaxWidth",
          "arrangement"
        ],
        "fields": [
          {
            "name": "webMaxWidth",
            "type": "object",
            "required": false,
            "description": "webMaxWidth（对象，字段见下方）",
            "properties": [
              {
                "name": "value",
                "type": "integer",
                "required": false,
                "description": "长度数值",
                "minimum": 0,
                "maximum": 400
              },
              {
                "name": "unit",
                "type": "string",
                "required": false,
                "description": "长度单位（只能 PX 或 PERCENT）",
                "enum": [
                  "PX",
                  "PERCENT"
                ]
              }
            ]
          },
          {
            "name": "arrangement",
            "type": "object",
            "required": false,
            "description": "arrangement（对象，字段见下方）",
            "properties": [
              {
                "name": "mode",
                "type": "string",
                "required": false,
                "description": "排列模式",
                "enum": [
                  "GRID",
                  "FLOW"
                ]
              },
              {
                "name": "columns",
                "type": "integer",
                "required": false,
                "description": "列数",
                "minimum": 1,
                "maximum": 12
              },
              {
                "name": "direction",
                "type": "string",
                "required": false,
                "description": "排列方向",
                "enum": [
                  "HORIZONTAL",
                  "VERTICAL"
                ]
              },
              {
                "name": "gap",
                "type": "object",
                "required": false,
                "description": "间距（长度类：value + unit）",
                "unit": "PX",
                "properties": [
                  {
                    "name": "value",
                    "type": "integer",
                    "required": false,
                    "description": "长度数值",
                    "minimum": 0,
                    "maximum": 96
                  },
                  {
                    "name": "unit",
                    "type": "string",
                    "required": false,
                    "description": "长度单位（只能 PX 或 PERCENT）",
                    "enum": [
                      "PX",
                      "PERCENT"
                    ]
                  }
                ]
              }
            ]
          }
        ],
        "references": [],
        "limits": [
          "webMaxWidth.value 取值范围：0 到 400",
          "webMaxWidth.unit 取值域：PX、PERCENT",
          "arrangement.mode 取值域：GRID、FLOW",
          "arrangement.columns 取值范围：1 到 12",
          "arrangement.direction 取值域：HORIZONTAL、VERTICAL",
          "arrangement.gap.value 取值范围：0 到 96",
          "arrangement.gap.unit 取值域：PX、PERCENT"
        ],
        "available": true,
        "source": "app/services/report_presentation_service.py::_op_set_page"
      },
      {
        "op": "SET_METADATA",
        "title": "SET_METADATA",
        "submission": "package_internal",
        "packageOp": "",
        "packageField": "",
        "envelope": {
          "op": "SET_METADATA",
          "params": {}
        },
        "required": [],
        "optional": [
          "name",
          "description",
          "tags"
        ],
        "fields": [
          {
            "name": "name",
            "type": "string",
            "required": false,
            "description": "name（取值见展示服务的校验）"
          },
          {
            "name": "description",
            "type": "string",
            "required": false,
            "description": "description（取值见展示服务的校验）"
          },
          {
            "name": "tags",
            "type": "string",
            "required": false,
            "description": "tags（取值见展示服务的校验）"
          }
        ],
        "references": [],
        "limits": [],
        "available": true,
        "source": "app/services/report_presentation_service.py::_op_set_metadata"
      },
      {
        "op": "SET_THEME",
        "title": "SET_THEME",
        "submission": "package_internal",
        "packageOp": "",
        "packageField": "",
        "envelope": {
          "op": "SET_THEME",
          "params": {
            "themeId": "<themeId>"
          }
        },
        "required": [
          "themeId"
        ],
        "optional": [],
        "fields": [
          {
            "name": "themeId",
            "type": "string",
            "required": true,
            "description": "themeId（取值见展示服务的校验）"
          }
        ],
        "references": [],
        "limits": [],
        "available": true,
        "source": "app/services/report_presentation_service.py::_op_set_theme"
      }
    ]
  }
}
```
