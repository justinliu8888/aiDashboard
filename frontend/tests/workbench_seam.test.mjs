// 前后端接缝验证（#54）：**真实后端产出**的载荷 → 前端源码里的原函数。
//
// 运行：node --test frontend/tests/workbench_seam.test.mjs
// 不依赖任何第三方包（Node 内置 test runner + node:vm）。
//
// ── 被测的缝（seam）─────────────────────────────────────────────
// 这里验的是**前端读到真实载荷之后会怎么做**，不是「前端读得好不好」：
//
//   1. 前端**收得下**吗 —— isStaleReportPayload 不判过期，
//      applyReportPreviewPayload 返回 true（这是 #53 修的那一刀）；
//   2. 前端**读得出**吗 —— 指标卡的数值与单位、表格的列与行、
//      图表的 xLabels / yValues、能力待接通的标记与原因；
//   3. 单位**来自载荷**吗 —— 不是前端写死的 kWh。
//
// 载荷不是手写样例：它是后端真实链路（真编排 + 真服务 + 真 SQLite，
// 只替换外部模型调用）产出的，由
// `backend/tests/test_seam_frontend_backend.py --emit-fixture` 生成。
//
// 前端函数从 `frontend/pages/workbench.js` 的**原文**里抽出来跑
// （node:vm + 大括号配平），不是抄一份 —— 抄的那份会跟着前端一起漂移。
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import { extractFunction, probe } from "./seam_probe.mjs";

const FIXTURE = JSON.parse(
  readFileSync(new URL("./seam_fixture.json", import.meta.url), "utf8")
);

// 真实链路产出的三段：
//   - payload：确认方案那轮响应顶层的载荷（第 1 段）
//   - stageRestorePayload：重新读取会话时按定义 + 上下文重新生成的（第 3 段）
//   - 第 2 段（点同意）响应**不带**载荷 → 前端保留第 1 段那份。
const PAYLOAD = FIXTURE.payload;
const RESTORE = FIXTURE.stageRestorePayload;
const DEFINITION = FIXTURE.definition;
const SAVED_DEFINITION = FIXTURE.savedDefinition;

test("前端收得下真实载荷：不被判过期，apply 返回 true", () => {
  const verdict = probe({ definition: DEFINITION, payload: PAYLOAD });

  assert.equal(verdict.stale, false, "前端把后端真实产出的载荷判成了过期版本");
  assert.equal(verdict.accepted, true, "前端没有收下这份载荷");
  assert.equal(verdict.kept, true, "载荷没有被写进 session.reportPreviewPayload");
  assert.equal(verdict.regionCount, PAYLOAD.regions.length);
});

test("载荷的 previewContext 与定义的 templateBinding 是同一套值", () => {
  const binding = DEFINITION.metadata.templateBinding;
  const context = PAYLOAD.previewContext;

  assert.equal(context.definitionId, binding.definitionId);
  assert.equal(context.definitionVersion, binding.definitionVersion);
  assert.equal(typeof context.definitionVersion, "number");
  // 两条轴不是一回事：metadata.revision 是定义的编辑计数。
  assert.notEqual(DEFINITION.metadata.revision, context.definitionVersion);
});

test("会话恢复那一份也收得下（基准是已保存的定义）", () => {
  const verdict = probe({ definition: SAVED_DEFINITION, payload: RESTORE });

  assert.equal(verdict.stale, false);
  assert.equal(verdict.accepted, true);
  assert.equal(verdict.kept, true);
});

test("拿掉 templateBinding，同一份载荷会被判过期丢掉（#53 那刀是必要的）", () => {
  const legacy = JSON.parse(JSON.stringify(DEFINITION));
  delete legacy.metadata.templateBinding;

  const verdict = probe({ definition: legacy, payload: PAYLOAD });

  assert.equal(verdict.stale, true, "去掉 templateBinding 后竟然没被判过期");
  assert.equal(verdict.accepted, false);
  assert.equal(verdict.kept, false);
});

test("三段数字恒定：确认 → 同意 → 重新读取会话", () => {
  // 第 1 段收下。
  const first = probe({
    definition: DEFINITION,
    payload: PAYLOAD,
    hasNext: true,
    nextPayload: null, // 同意轮不带载荷
    definitionAfter: DEFINITION,
  });
  assert.equal(first.accepted, true);
  assert.equal(first.nextAccepted, false, "没有载荷的一轮不该收下任何东西");
  assert.equal(first.retainedSame, true, "同意轮之后前端手上那份载荷被动了");

  // 第 3 段（重新读取会话）重新生成的那份也收得下，且数字与第 1 段一致。
  const second = probe({
    definition: DEFINITION,
    payload: PAYLOAD,
    hasNext: true,
    nextPayload: RESTORE,
    definitionAfter: SAVED_DEFINITION,
  });
  assert.equal(second.nextStale, false);
  assert.equal(second.nextAccepted, true);
  assert.equal(second.nextRegionCount, second.regionCount);
  assert.deepEqual(second.nextValues, second.values, "三段的数字不一致");
});

test("真实载荷的每一块，前端都读得出该读的东西", () => {
  const verdict = probe({ definition: DEFINITION, payload: PAYLOAD });
  const byId = new Map(PAYLOAD.regions.map((r) => [r.regionId, r]));

  // 指标卡：数值与单位
  const cards = verdict.regions.filter((r) => r.componentType === "METRIC_CARD");
  assert.ok(cards.length, "真实载荷里没有指标卡");
  for (const card of cards) {
    const raw = byId.get(card.regionId).payload;
    assert.equal(typeof card.value, "number");
    assert.equal(card.value, raw.value);
    assert.ok(card.formattedValue, "指标卡的数值没格式化出来");
    assert.equal(card.unit, String(raw.unit ?? ""));
    assert.ok(card.metrics.length, "指标卡的逐条指标没读出来");
  }

  // 表格：列与行，列的 fieldRef 对得上行键
  const tables = verdict.regions.filter((r) => r.componentType === "TABLE");
  assert.ok(tables.length, "真实载荷里没有表格");
  for (const table of tables) {
    assert.ok(table.columns.length, "表格的列没读出来");
    assert.ok(table.rowCount > 0, "表格的行没读出来");
    assert.ok(table.fieldRefsResolveInRows, "表格列的 fieldRef 对不上行键");
    assert.ok(table.columns.every((c) => c.label));
  }

  // 图表：xLabels / yValues
  const charts = verdict.regions.filter((r) => r.componentType === "CHART");
  assert.ok(charts.length, "真实载荷里没有图表");
  for (const chart of charts) {
    assert.ok(chart.xLabels.length, "图表的 xLabels 没读出来");
    assert.ok(chart.yValues.length, "图表的 yValues 没读出来");
    assert.equal(chart.xLabels.length, chart.yValues.length);
  }

  // 能力待接通：标记与原因
  const pending = verdict.regions.filter((r) => r.capabilityNotes.length);
  assert.ok(pending.length, "真实载荷里没有「能力待接通」的指标");
  for (const region of pending) {
    const raw = byId.get(region.regionId).payload;
    const marked = (raw.metrics || []).filter((m) => m.capabilityPending === true);
    assert.ok(marked.length, "能力待接通的标记没落在载荷的 metrics 上");
    assert.ok(marked.every((m) => m.pendingReason), "能力待接通没有原因");
    for (const metric of marked) {
      assert.ok(region.capabilityNotes.join(" ").includes(metric.label));
      assert.ok(region.capabilityNotes.join(" ").includes(metric.pendingReason));
    }
  }
});

test("单位来自载荷，不是前端写死的 kWh", () => {
  const verdict = probe({ definition: DEFINITION, payload: PAYLOAD });
  const byId = new Map(PAYLOAD.regions.map((r) => [r.regionId, r]));

  const units = [];
  for (const region of verdict.regions) {
    if (region.componentType !== "METRIC_CARD" && region.componentType !== "CHART") continue;
    const raw = byId.get(region.regionId).payload;
    assert.equal(region.unit, String(raw.unit ?? ""), "单位不是从载荷读出来的");
    units.push(region.unit);
  }

  // 同一份载荷里至少三种单位形态：无量纲 / 货币 / 能量。
  assert.ok(units.includes(""), "没有无量纲指标卡，单位不可能是写死的");
  assert.ok(units.includes("元"), "没有带货币单位的指标卡");
  assert.ok(units.includes("kWh"), "没有带能量单位的图表");
  assert.ok(new Set(units).size >= 3, "单位不止一种，写死的话不可能同时对上");

  // 静态口径：读单位的那个函数里不许出现写死的单位字面量。
  const source = extractFunction("readPayloadUnit");
  assert.ok(!source.includes("kWh"), "readPayloadUnit 里写死了 kWh");
  assert.ok(!source.includes("耗电"), "readPayloadUnit 里写死了「耗电」");
});
