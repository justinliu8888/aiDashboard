# 展示规则库（第一版）

**规则库版本：1.0.0**
规则表代码：`backend/app/services/report_rule_library.py`
契约来源：GitHub #55《开发规格 - 报表智能生成与调整》第二章「规则库与选型」第 9–11 条
工单：#60「统一开发 05：第一版规则库与命中依据持久化」

---

## 1. 这份规则库是什么

「看总量给指标卡、看变化给趋势图、比较对象给柱状图」这套判断，在本工单之前
是几张扁平表，优先级靠元组的排列顺序隐含表达，没有稳定 ID、没有禁用条件，
也没有说明「为什么是这个图」。本工单把它落成一份**随代码维护**的结构化规则表。

它**不是**：

- 不是规则数据库（没有表、没有迁移、没有后台管理页）；
- 不是自动学习系统（规则由人写在代码里，改规则 = 改代码 + 改版本号 + 改本文档）；
- 不是新的图表库（规则只能建议**当前可渲染**的组件类型）。

它**是**：一份带稳定 ID、适用条件、建议展示、禁用条件、优先级与解释模板的规则表，
外加一套「命中 → 依据 → 理由」的接线方式。

## 2. 每条规则长什么样

```python
DisplayRule(
    rule_id="DR-MAP-COMPARISON",   # 稳定 ID：命中依据靠它回溯
    family="mapping",              # 规则族
    priority=200,                  # 显式优先级：数字大的先命中
    applies_when=(...),            # 适用条件：任一成立即适用（空 = 恒适用）
    suggested_display="bar_chart", # 建议展示
    disabled_when=(...),           # 禁用条件：任一成立即不适用（空 = 不禁用）
    explain_template="按展示目的「{intent_label}」选{component_label}。",
    reason_template="按展示目的「{intent_label}」",
    match_values=("comparison",),  # 用来匹配的关键词
    payload=DisplayIntent.COMPARISON,
)
```

**条件原语**（`RuleCondition(kind, value)`）：

| 原语 | 成立条件 |
| --- | --- |
| `keyword` | 文本里出现这个短语 |
| `directive_keyword` | 这个短语前面不远处有指令词（改用/换成/用…）→ 用户**明确指定** |
| `negated_keyword` | 这个短语被否定（不要/不用/别用）→ 这条规则**禁用** |
| `display_intent_is` | 板块的展示目的等于这个值 |
| `model_type_is` / `model_type_is_not` | 模型的候选组件类型等于 / 不等于这个值 |
| `type_not_renderable` | 规则建议的组件类型当前**不可渲染** |
| `model_declared_columns` | 模型已经声明了栏目（系统不再补） |
| `grain_declared` | 方案已经声明了粒度（用户说过就不推定） |
| `column_hints_present` | 用户在需求里点名了栏目 |

**否定写法**：明确指定这一族的禁用条件认这四种否定 —— `不要`、`不用`、`别用`、
`别要`。它们**不是**明确指定（规则被禁用），但同句里另一次出现的
「用柱状图」不受影响。

**指令词的例外**：`费用`、`适用`、`重要`、`运用`这类词里的「用／要」不是
指令词 —— 那是词的一部分，不是用户在下指令。这条由指令词的正则负责，
与否定写法是两回事。

## 3. 规则族与选型三档

| 规则族 | 作用 | 优先级 |
| --- | --- | --- |
| `intent` | 展示目的关键词 → 展示目的（**模型的语义候选**） | 500 / 400 / 300 / 200 / 100 |
| `mapping` | 展示目的 → 建议的组件类型 | 200 |
| `explicit` | 用户明确指定的组件类型（类型词 + 指令词，且没被否定） | 300 |
| `column` | 表格栏目的系统补齐依据（预设 → 用户点名的栏目） | 400 / 300 / 200 / 100 / 50 |
| `grain` | 时间粒度关键词 → 粒度 | 500 / 400 / 300 / 200 / 100 / 0 |
| `fallback` | 没有更具体依据时的兜底（模型原选 / 纯说明板块） | 100 |

**选型的三档是显式的**：用户明确指定 **300** > 按展示目的映射 **200** > 模型原选 **100**。
同一档里再按规则自己的优先级排；同级按规则 ID 排，结果是确定的。

### 3.1 「模型给候选，服务端定稿」

模型的 `displayIntent` 只是**语义候选** —— 它不决定用哪种图。服务端按规则前提
定稿：先看用户有没有明确指定，再看展示目的，最后才沿用模型原选。每一层都还要
满足该规则自己的前提条件：**被禁用的规则不参与定稿**。

理由不是事后编的：它由**命中的那条规则**用自己的解释模板渲染出来。

### 3.2 明确指定这一族按出现位置取

一条指令里出现多个类型词时，取**最先出现**的那个（「先说的那个算数」）。
同一位置命中多个写法时取**更具体的**那个 —— 「折线图」比「折线」具体，
与改造前「长的先匹配」的口径一致：命中依据指的那条规则，就是用户
实际写下的那个写法。

沿用上一版已指定的类型时（例如这一版没改那块），依据指向该类型的
**正式叫法**那条规则（指标卡 → `DR-EXPLICIT-METRIC-CARD`），不因为
同类型还有别的写法就指到更冷僻的一条上。

### 3.3 不凑三件套、不授权业务计算

规则库只决定「用什么展示」，不决定「算什么东西」：

- 板块数量、指标意图、需求块都不由规则增删 —— 要一块总量表就只给一块，
  不凑「指标卡 + 趋势 + 排名」；
- 选了折线图不等于授权求和、平均或任何派生计算；
- 规则建议的类型只从**当前可渲染**的组件类型里出（`RENDERABLE_COMPONENT_TYPES`），
  不扩图表库。

## 4. 命中依据与规则版本怎么持久化

方案（`ReportPlan`）多两个字段，随方案一起落库：

```json
{
  "ruleVersion": "1.0.0",
  "selectionEvidence": [
    {
      "sectionId": "section_1",
      "ruleId": "DR-MAP-COMPARISON",
      "componentType": "bar_chart",
      "detail": "展示目的=comparison"
    }
  ]
}
```

- `ruleVersion`：定稿这份方案时用的规则库版本。
- `selectionEvidence`：每个板块是被**哪条规则**、按**什么前提**定稿的。

`detail` 记的是**真实命中的前提**，一路能追溯回去：

| 场景 | `ruleId` | `detail` |
| --- | --- | --- |
| 用户明确指定 | `DR-EXPLICIT-BAR-CHART` | `指令「柱状图」` |
| 请求里读出展示目的 | `DR-MAP-COMPARISON` | `关键词「对比」→展示目的=comparison` |
| 模型给的语义候选 | `DR-MAP-COMPARISON` | `模型给的语义候选→展示目的=comparison` |
| 没有更具体的依据 | `DR-FALLBACK-MODEL` | `模型候选=metric_card` |

由此得到三条保证：

1. **恢复时不无声改型** —— 恢复走的是存下来的那份方案，不重新跑一遍选型。
   规则库后来怎么改，都不会把用户已经确认过的方案改成另一种图。
2. **规则版本变化不追溯改写历史方案已给出的理由** —— 历史方案记着自己那版
   规则与当时给出的理由；规则升版只影响之后新出的版本。
3. **老数据不迁移** —— 没有这两个字段的旧方案照样读得懂（`ruleVersion` 为
   `null`，`selectionEvidence` 为空）。

## 5. 规则表全表

| 规则 ID | 族 | 优先级 | 适用条件 | 建议展示 | 禁用条件 | 解释模板 |
| --- | --- | --- | --- | --- | --- | --- |
| `DR-INTENT-COMPOSITION` | intent | 500 | `占比`、`构成`、`比例`、`分布`、`组成` | `composition` | `model_type_is`(text) | `按展示目的「{intent_label}」判断这块要看什么。` |
| `DR-INTENT-COMPARISON` | intent | 400 | `对比`、`比较`、`排名`、`排行`、`排序`、`高低` | `comparison` | `model_type_is`(text) | `按展示目的「{intent_label}」判断这块要看什么。` |
| `DR-INTENT-TREND` | intent | 300 | `趋势`、`走势`、`变化`、`随时间`、`逐时`、`逐日` | `trend` | `model_type_is`(text) | `按展示目的「{intent_label}」判断这块要看什么。` |
| `DR-INTENT-RECORDS` | intent | 200 | `列表`、`明细`、`清单`、`记录`、`台账`、`逐条` | `records` | `model_type_is`(text) | `按展示目的「{intent_label}」判断这块要看什么。` |
| `DR-INTENT-TOTAL` | intent | 100 | `总量`、`总数`、`合计`、`汇总`、`总览` | `total` | `model_type_is`(text) | `按展示目的「{intent_label}」判断这块要看什么。` |
| `DR-MAP-TOTAL` | mapping | 200 | `total` | `metric_card` | `type_not_renderable` | `按展示目的「{intent_label}」选{component_label}。` |
| `DR-MAP-TREND` | mapping | 200 | `trend` | `line_chart` | `type_not_renderable` | `按展示目的「{intent_label}」选{component_label}。` |
| `DR-MAP-COMPARISON` | mapping | 200 | `comparison` | `bar_chart` | `type_not_renderable` | `按展示目的「{intent_label}」选{component_label}。` |
| `DR-MAP-COMPOSITION` | mapping | 200 | `composition` | `pie_chart` | `type_not_renderable` | `按展示目的「{intent_label}」选{component_label}。` |
| `DR-MAP-RECORDS` | mapping | 200 | `records` | `table` | `type_not_renderable` | `按展示目的「{intent_label}」选{component_label}。` |
| `DR-EXPLICIT-METRIC-CARD` | explicit | 300 | `指标卡` | `metric_card` | `negated_keyword`(指标卡) | `你明确指定用{component_label}。` |
| `DR-EXPLICIT-KPI` | explicit | 300 | `kpi` | `metric_card` | `negated_keyword`(kpi) | `你明确指定用{component_label}。` |
| `DR-EXPLICIT-LINE-CHART` | explicit | 300 | `折线图` | `line_chart` | `negated_keyword`(折线图) | `你明确指定用{component_label}。` |
| `DR-EXPLICIT-LINE-CURVE` | explicit | 300 | `曲线图` | `line_chart` | `negated_keyword`(曲线图) | `你明确指定用{component_label}。` |
| `DR-EXPLICIT-TREND-CHART` | explicit | 300 | `趋势图` | `line_chart` | `negated_keyword`(趋势图) | `你明确指定用{component_label}。` |
| `DR-EXPLICIT-LINE` | explicit | 300 | `折线` | `line_chart` | `negated_keyword`(折线) | `你明确指定用{component_label}。` |
| `DR-EXPLICIT-BAR-CHART` | explicit | 300 | `柱状图` | `bar_chart` | `negated_keyword`(柱状图) | `你明确指定用{component_label}。` |
| `DR-EXPLICIT-BAR` | explicit | 300 | `条形图` | `bar_chart` | `negated_keyword`(条形图) | `你明确指定用{component_label}。` |
| `DR-EXPLICIT-BAR-SHORT` | explicit | 300 | `柱图` | `bar_chart` | `negated_keyword`(柱图) | `你明确指定用{component_label}。` |
| `DR-EXPLICIT-DONUT` | explicit | 300 | `环形图` | `pie_chart` | `negated_keyword`(环形图) | `你明确指定用{component_label}。` |
| `DR-EXPLICIT-PIE` | explicit | 300 | `饼图` | `pie_chart` | `negated_keyword`(饼图) | `你明确指定用{component_label}。` |
| `DR-EXPLICIT-RATIO-CHART` | explicit | 300 | `占比图` | `pie_chart` | `negated_keyword`(占比图) | `你明确指定用{component_label}。` |
| `DR-EXPLICIT-TABLE` | explicit | 300 | `表格` | `table` | `negated_keyword`(表格) | `你明确指定用{component_label}。` |
| `DR-COLUMN-ALARM` | column | 400 | `告警`、`alarm` | `设备名称、时间、告警级别` | `model_declared_columns` | `系统为「{section_title}」补齐了栏目：{column_labels}。` |
| `DR-COLUMN-CABINET` | column | 300 | `机柜`、`cabinet` | `机柜名称、状态、负载率` | `model_declared_columns` | `系统为「{section_title}」补齐了栏目：{column_labels}。` |
| `DR-COLUMN-ENERGY` | column | 200 | `能耗`、`耗电`、`用电`、`energy` | `设备名称、耗电量` | `model_declared_columns` | `系统为「{section_title}」补齐了栏目：{column_labels}。` |
| `DR-COLUMN-DEVICE` | column | 100 | `设备`、`device` | `设备名称、状态` | `model_declared_columns` | `系统为「{section_title}」补齐了栏目：{column_labels}。` |
| `DR-COLUMN-USER-HINT` | column | 50 | — | `` | `model_declared_columns` | `系统为「{section_title}」补齐了栏目：{column_labels}。` |
| `DR-GRAIN-MINUTE` | grain | 500 | `分钟`、`分粒` | `MINUTE` | `grain_declared` | `时间粒度按 {suggested_display} 处理。` |
| `DR-GRAIN-HOUR` | grain | 400 | `小时`、`逐时`、`时点` | `HOUR` | `grain_declared` | `时间粒度按 {suggested_display} 处理。` |
| `DR-GRAIN-DAY` | grain | 300 | `按天`、`每天`、`天粒`、`日粒`、`逐日` | `DAY` | `grain_declared` | `时间粒度按 {suggested_display} 处理。` |
| `DR-GRAIN-WEEK` | grain | 200 | `按周`、`每周`、`周粒` | `WEEK` | `grain_declared` | `时间粒度按 {suggested_display} 处理。` |
| `DR-GRAIN-MONTH` | grain | 100 | `按月`、`每月`、`月粒` | `MONTH` | `grain_declared` | `时间粒度按 {suggested_display} 处理。` |
| `DR-GRAIN-DEFAULT` | grain | 0 | — | `DAY` | `grain_declared` | `未指定时间粒度，按 {suggested_display} 处理。` |
| `DR-FALLBACK-MODEL` | fallback | 100 | — | `模型候选的类型` | `model_type_is`(text) | `沿用模型给出的{component_label}。` |
| `DR-FALLBACK-TEXT` | fallback | 100 | — | `text` | `model_type_is_not`(text) | `纯说明板块保持文字。` |

### 5.1 栏目预设的由来

- `DR-COLUMN-ALARM`：需求稿 4.1 —— 「告警列表」建议设备名称、时间、告警级别。
- `DR-COLUMN-CABINET`：ADR 0004 §3 —— 「机柜运行状态」对应成套机柜、状态、负载率。
- 补出来的列必须**能被渲染出合理内容**（规格 US47）。「机房」曾经在能耗与设备
  两类预设里，但造数器没有机房列的值类型，它会落进兜底的「示例值 69」——
  占位观感的列不算合理的列，所以拿掉。
- 数值列自带单位（规格《契约缺口补充》A2）：载荷的
  `payload.columns[].format.unitLabel` 要有单位，表头才读得出量纲。
  名称、状态、时间这类离散列**不带单位**。

## 6. 改规则时怎么做

1. 改 `backend/app/services/report_rule_library.py` 里的规则表；
2. 规则**语义**变了就改 `RULE_LIBRARY_VERSION`（只改措辞、只加注释不用改）；
3. 同步更新本文档的「规则表全表」与「版本历史」；
4. 跑 `backend/tests/test_ticket60_rule_library.py`（文档与规则表的一致性也在里面查）。

历史方案不会被改写：它们记着自己那版规则版本与当时给出的理由。

## 7. 版本历史

| 版本 | 日期 | 变更 |
| --- | --- | --- |
| 1.0.0 | 2026-10-30 | 第一版。把展示目的关键词表、展示目的到组件类型的映射、组件类型关键词表、栏目预设表、粒度关键词表纳入规则体系；优先级从元组顺序改为显式字段；命中依据与规则版本随方案持久化。 |
