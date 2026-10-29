// 前后端接缝探针（#54）：把**前端源码里的原函数**抽出来，跑一份**真实后端产出**的载荷。
//
// 运行（stdin 收 JSON，stdout 出 JSON）：
//   echo '{"definition": ..., "payload": ...}' | node frontend/tests/seam_probe.mjs
//
// 输入：
//   {
//     "definition":      <定义>            —— 前端 session.definition
//     "payload":         <报示意载荷>      —— 响应顶层 reportPreviewPayload
//     "hasNext":         <bool>            —— 是否有「下一轮」要模拟
//     "nextPayload":     <载荷 或 null>    —— 下一轮响应带的载荷（同意轮就是 null）
//     "definitionAfter": <定义 或 null>    —— 下一轮之后的 session.definition
//   }
//
// 输出：
//   {
//     "stale": bool, "accepted": bool, "kept": bool,
//     "nextStale": bool|null, "nextAccepted": bool|null, "retainedSame": bool,
//     "previewContext": {...}, "regionCount": int,
//     "values": { 区域 id → payload }, "nextValues": { ... },
//     "unitTotals": { 单位 → 合计 }, "regions": [ 逐区域读数 ]
//   }
//
// 为什么是「抽源码」而不是抄一份：抄的那份会跟着前端一起漂移，验的就不是
// 真正在跑的代码了。这里按函数名从 frontend/pages/workbench.js 的**原文**里
// 抠出函数体（大括号配平），抠不出来就直接失败 —— 抽不出来就是接缝断了，
// 不能静默跳过。
//
// 探针只读不改：不写文件、不碰后端、不改工作台脚本。
import { readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";
import vm from "node:vm";

const WORKBENCH_JS = readFileSync(new URL("../pages/workbench.js", import.meta.url), "utf8");

// ---------------------------------------------------------------- 抽取工具

/** 从源码里原样抽出一个函数定义（按大括号配平）。 */
export function extractFunction(name) {
  const marker = `function ${name}(`;
  const start = WORKBENCH_JS.indexOf(marker);
  if (start < 0) throw new Error(`源码里找不到函数 ${name}`);
  let depth = 0;
  let opened = false;
  for (let i = start; i < WORKBENCH_JS.length; i += 1) {
    const ch = WORKBENCH_JS[i];
    if (ch === "{") {
      depth += 1;
      opened = true;
    } else if (ch === "}") {
      depth -= 1;
      if (opened && depth === 0) return WORKBENCH_JS.slice(start, i + 1);
    }
  }
  throw new Error(`函数 ${name} 的大括号没有配平`);
}

/** 被测的纯逻辑函数（全部来自工作台主脚本的原文）。 */
export const PROBE_FUNCTIONS = [
  "readReportPreviewPayload",
  "normalizeVersionNumber",
  "isStaleReportPayload",
  "applyReportPreviewPayload",
  "findDefinitionRegion",
  "readPayloadUnit",
  "readCapabilityNotes",
  "buildTotalsByUnit",
  "readPositiveInt",
  "sumColumnValues",
  "readTableTruncation",
  "formatMetricValue",
  "readPayloadWindowLabel",
  "isTitleRegion",
];

/** 把选中的片段放进沙箱跑一遍，返回它们组成的 API。 */
export function loadApi() {
  const snippets = PROBE_FUNCTIONS.map(extractFunction).join("\n\n");
  const sandbox = {
    console: { warn() {}, error() {}, log() {} },
    session: null,
    window: {},
    document: {},
  };
  vm.createContext(sandbox);
  vm.runInContext(
    snippets + "\n;globalThis.__api = {" + PROBE_FUNCTIONS.join(", ") + "};",
    sandbox
  );
  return sandbox;
}

/** 前端真实的工作台会话状态（与 workbench.js 里的 session 同形）。 */
function makeSession(definition) {
  return {
    state: "ANALYZING",
    definition: definition ?? null,
    previewPayload: null,
    reportPreviewPayload: null,
  };
}

/** 摊平一个载荷的逐区域数值，便于跨阶段逐字段比对。 */
function collectValues(payload) {
  const values = {};
  for (const region of payload?.regions || []) {
    values[region.regionId] = region.payload;
  }
  return values;
}

/** 逐区域读数：前端能读出哪些东西。 */
function readRegions(api, definition, payload) {
  const bodyRegions = (payload?.regions || []).filter((r) => !api.isTitleRegion(r));
  const totalsByUnit = api.buildTotalsByUnit(bodyRegions);
  const totals = {};
  for (const [unit, total] of totalsByUnit) totals[unit] = total;

  return {
    unitTotals: totals,
    regions: bodyRegions.map((region) => {
      const p = region.payload || {};
      const defRegion = api.findDefinitionRegion(definition, region.regionId);
      const unit = api.readPayloadUnit(region, p, defRegion);
      const capabilityNotes = api.readCapabilityNotes(region, p, defRegion);
      const columns = Array.isArray(p.columns) ? p.columns : [];
      const rows = Array.isArray(p.rows) ? p.rows : [];
      const metrics = Array.isArray(p.metrics) ? p.metrics : [];
      return {
        regionId: region.regionId,
        name: region.name,
        role: region.role,
        componentType: region.componentType,
        unit,
        capabilityNotes,
        // 指标卡：数值 / 标签 / 精度 / 逐条指标
        label: p.label ?? null,
        value: p.value ?? null,
        precision: p.precision ?? null,
        formattedValue:
          p.value === undefined ? null : api.formatMetricValue(p.value, p.precision),
        metrics: metrics.map((m) => ({
          metricKey: m.metricKey ?? null,
          label: m.label ?? null,
          unit: m.unit ?? null,
          value: m.value ?? null,
          capabilityPending: m.capabilityPending === true,
          pendingReason: m.pendingReason ?? null,
        })),
        // 表格：列与行
        columns: columns.map((c) => ({
          id: c.id ?? null,
          label: c.label ?? null,
          fieldRef: c.fieldRef ?? null,
          unitLabel: c.format?.unitLabel ?? null,
          kind: c.format?.kind ?? null,
        })),
        rowCount: rows.length,
        rows,
        fieldRefsResolveInRows: columns.every((c) =>
          rows.every((row) => row && Object.prototype.hasOwnProperty.call(row, c.fieldRef))
        ),
        truncation: api.readTableTruncation(p, defRegion, totalsByUnit),
        // 图表：xLabels / yValues
        chartKind: p.chartKind ?? null,
        xLabels: Array.isArray(p.xLabels) ? p.xLabels : [],
        yValues: Array.isArray(p.yValues) ? p.yValues : [],
        windowLabel: api.readPayloadWindowLabel(payload),
      };
    }),
  };
}

/**
 * 跑一遍前端真实会做的事：读载荷 → 判过期 → 收下（→ 可选：下一轮）。
 */
export function probe(input) {
  const api = loadApi();
  const session = makeSession(input.definition);
  api.session = session;

  // 第 1 轮：确认方案那轮响应顶层的载荷。
  const read = api.readReportPreviewPayload({ reportPreviewPayload: input.payload });
  const stale = read ? api.isStaleReportPayload(read) : null;
  const accepted = read ? api.applyReportPreviewPayload(read) : false;
  const kept = session.reportPreviewPayload === read;

  const result = {
    stale,
    accepted,
    kept,
    nextStale: null,
    nextAccepted: null,
    retainedSame: null,
    previewContext: read?.previewContext ?? null,
    regionCount: (read?.regions || []).length,
    values: collectValues(read),
    nextValues: null,
    ...readRegions(api, input.definition, read),
  };

  // 第 2 轮：下一轮响应（同意轮不带载荷 → null；恢复会话轮带重新生成的那份）。
  if (input.hasNext) {
    if (input.definitionAfter !== undefined && input.definitionAfter !== null) {
      session.definition = input.definitionAfter;
    }
    const nextRead = api.readReportPreviewPayload({
      reportPreviewPayload: input.nextPayload ?? null,
    });
    result.nextStale = nextRead ? api.isStaleReportPayload(nextRead) : null;
    result.nextAccepted = nextRead ? api.applyReportPreviewPayload(nextRead) : false;
    // 前端手上那份是不是还是**第一份**（没被换掉、没被丢掉）。
    result.retainedSame = session.reportPreviewPayload === read;
    result.nextValues = collectValues(session.reportPreviewPayload);
    result.nextRegionCount = (session.reportPreviewPayload?.regions || []).length;
  }

  return result;
}

// ---------------------------------------------------------------- CLI

function readStdin() {
  return new Promise((resolve, reject) => {
    let text = "";
    process.stdin.setEncoding("utf8");
    process.stdin.on("data", (chunk) => { text += chunk; });
    process.stdin.on("end", () => resolve(text));
    process.stdin.on("error", reject);
  });
}

async function runCli() {
  const text = await readStdin();
  let input;
  try {
    input = JSON.parse(text || "{}");
  } catch (err) {
    process.stderr.write(`输入不是合法 JSON：${err.message}\n`);
    process.exit(2);
  }
  process.stdout.write(JSON.stringify(probe(input)));
}

// 作为脚本被直接执行时走 CLI；被 import 时只导出函数。
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  await runCli();
}
