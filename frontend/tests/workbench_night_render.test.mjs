// 夜间统一修复 · 包 B（前端）：渲染真实生效。
//
// 运行：node --test frontend/tests/workbench_night_render.test.mjs
// 不依赖任何第三方包（Node 内置 test runner + node:vm / node:module），不改共用依赖配置。
//
// ── 被测的缝（seam）─────────────────────────────────────────────
// 1. 数字格式（BUG-010）：workbench.js 里按**服务端权威格式**渲染的那几段
//    （regionAuthoritativeFormat / formatDisplayValueByFormat /
//    authoritativeDisplayValue）与指标卡的正文渲染 —— 用 node:vm 原样抽出来跑。
// 2. 卡片样式（BUG-001）与分组排列（BUG-006）：workbench_presentation.js
//    （require 进来，它自己就是模块）+ 一个够用的假 DOM。
//
// ── 契约（契约 0002 §3.5 / §5）────────────────────────────────
//   - 格式的**唯一出处**是服务端写进定义里的 region.format；
//     5553.7 + precision=2 + thousandSeparator + unitLabel="kWh"
//     → 显示成 5,553.70 kWh（底层数值不变）。
//   - fontSize / textAlign / borderRadius 要**真的落到 DOM 上**。
//   - 分组排列：DOM 上真实呈现**两列**与**实际间距**（不是单卡 placement 冒充）。
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import vm from "node:vm";

const require = createRequire(import.meta.url);
const WORKBENCH_JS = readFileSync(new URL("../pages/workbench.js", import.meta.url), "utf8");
const WORKBENCH_CSS = readFileSync(new URL("../pages/workbench_presentation.css", import.meta.url), "utf8");

// ---------------------------------------------------------------- 抽取工具

function extractFunction(name) {
  const marker = `function ${name}(`;
  const start = WORKBENCH_JS.indexOf(marker);
  assert.ok(start >= 0, `源码里找不到函数 ${name}`);
  // 先收参数表（参数表里可能有默认值 {}），再配平函数体。
  let parens = 0;
  let i = start + marker.length - 1;
  for (; i < WORKBENCH_JS.length; i += 1) {
    const ch = WORKBENCH_JS[i];
    if (ch === "(") parens += 1;
    else if (ch === ")") {
      parens -= 1;
      if (parens === 0) break;
    }
  }
  assert.ok(i < WORKBENCH_JS.length, `函数 ${name} 的参数表没有收口`);
  let braces = 0;
  let opened = false;
  for (let j = i + 1; j < WORKBENCH_JS.length; j += 1) {
    const ch = WORKBENCH_JS[j];
    if (ch === "{") {
      braces += 1;
      opened = true;
    } else if (ch === "}") {
      braces -= 1;
      if (opened && braces === 0) return WORKBENCH_JS.slice(start, j + 1);
    }
  }
  throw new Error(`函数 ${name} 的大括号没有配平`);
}

function extractConst(name) {
  const pattern = new RegExp(`^const ${name} = .*;$`, "m");
  const match = WORKBENCH_JS.match(pattern);
  assert.ok(match, `源码里找不到 const ${name}`);
  return match[0];
}

/** 抽一条多行的顶层 const（数组 / 对象字面量）。 */
function extractConstBlock(name) {
  const marker = `const ${name} =`;
  const start = WORKBENCH_JS.indexOf(marker);
  assert.ok(start >= 0, `源码里找不到 const ${name}`);
  let depth = 0;
  let opened = false;
  for (let i = start; i < WORKBENCH_JS.length; i += 1) {
    const ch = WORKBENCH_JS[i];
    if (ch === "{" || ch === "[") {
      depth += 1;
      opened = true;
    } else if (ch === "}" || ch === "]") {
      depth -= 1;
      if (opened && depth === 0) return WORKBENCH_JS.slice(start, i + 1);
    } else if (ch === ";" && opened && depth === 0) {
      return WORKBENCH_JS.slice(start, i + 1);
    }
  }
  throw new Error(`const ${name} 的括号没有配平`);
}

function loadApi(names, { extraStatements = [], globals = {} } = {}) {
  const snippets = [...extraStatements, ...names.map((name) => extractFunction(name))].join("\n\n");
  const sandbox = { console: { warn() {}, error() {}, log() {} }, ...globals };
  vm.createContext(sandbox);
  vm.runInContext(snippets + "\n;globalThis.__api = {" + names.join(", ") + "};", sandbox);
  return sandbox;
}

// ---------------------------------------------------------------- 契约样例

/** 服务端写进定义里的权威格式（SET_FORMAT 写下的那一份）。 */
const AUTHORITATIVE_FORMAT = {
  kind: "NUMBER",
  precision: 2,
  thousandSeparator: true,
  unitLabel: "kWh",
};

const METRIC_CARD_DEFINITION_REGION = {
  id: "region_1",
  name: "昨日总耗电",
  role: "SUMMARY",
  component: { type: "METRIC_CARD" },
  format: AUTHORITATIVE_FORMAT,
};

const METRIC_CARD_PAYLOAD = {
  label: "昨日总耗电",
  metricKey: "total_power",
  value: 5553.7,
  unit: "kWh",
  precision: 1,
};

// ==========================================================================
// BUG-010：数字格式按权威格式渲染
// ==========================================================================

function loadFormatApi() {
  return loadApi(
    [
      "regionAuthoritativeFormat", "formatAuthoritativeNumber", "formatAuthoritativeDate",
      "formatDisplayValueByFormat", "authoritativeDisplayValue", "hasAuthoritativeFormat",
    ],
    {
      extraStatements: [
        extractConstBlock("AUTHORITATIVE_FORMAT_KEYS"),
        extractConstBlock("AUTHORITATIVE_NUMERIC_FORMAT_KINDS"),
        extractConstBlock("AUTHORITATIVE_DATE_FORMAT_KINDS"),
        extractConstBlock("AUTHORITATIVE_ROUNDING_MODES"),
      ],
    }
  ).__api;
}

test("权威格式从定义里的 region.format 读，只保留白名单的键", () => {
  const api = loadFormatApi();

  const fmt = api.regionAuthoritativeFormat(
    { ...METRIC_CARD_DEFINITION_REGION, format: { ...AUTHORITATIVE_FORMAT, madeUpKey: "x" } },
    null
  );

  assert.deepEqual(Object.keys(fmt).sort(), ["kind", "precision", "thousandSeparator", "unitLabel"]);
  assert.equal(fmt.precision, 2);
  assert.equal(fmt.thousandSeparator, true);
  assert.equal(fmt.unitLabel, "kWh");
});

test("5553.7 + 两位小数 + 千分位 + kWh → 5,553.70 kWh", () => {
  const api = loadFormatApi();

  const text = api.formatDisplayValueByFormat(5553.7, AUTHORITATIVE_FORMAT, "");

  assert.equal(text, "5,553.70 kWh");
});

test("权威格式渲染：底层数值不动（只改显示）", () => {
  const api = loadFormatApi();

  const value = 5553.7;
  api.formatDisplayValueByFormat(value, AUTHORITATIVE_FORMAT, "kWh");

  assert.equal(value, 5553.7, "底层数值一个字节都不动");
});

test("千分位与精度分开看：只给精度不给千分位，就不加逗号", () => {
  const api = loadFormatApi();

  assert.equal(api.formatDisplayValueByFormat(5553.7, { precision: 2 }, ""), "5553.70");
  assert.equal(api.formatDisplayValueByFormat(5553.7, { thousandSeparator: true }, ""), "5,553.7");
  assert.equal(api.formatDisplayValueByFormat(-5553.7, { precision: 2, thousandSeparator: true }, ""), "-5,553.70");
});

test("单位标签可以清空（= 不带单位），没声明才用载荷里的单位", () => {
  const api = loadFormatApi();

  assert.equal(api.formatDisplayValueByFormat(5553.7, { precision: 2, unitLabel: "" }, "kWh"), "5553.70");
  assert.equal(api.formatDisplayValueByFormat(5553.7, { precision: 2 }, "kWh"), "5553.70 kWh");
});

test("服务端已经算好的显示串优先用服务端的（两边不各拼一套）", () => {
  const api = loadFormatApi();

  const text = api.authoritativeDisplayValue(
    5553.7, METRIC_CARD_DEFINITION_REGION, null, "kWh", "服务端算好的 5,553.70 kWh"
  );

  assert.equal(text, "服务端算好的 5,553.70 kWh");
});

test("服务端没给显示串：按同一份权威格式本地渲染（同一口径，结果一致）", () => {
  const api = loadFormatApi();

  const text = api.authoritativeDisplayValue(
    5553.7, METRIC_CARD_DEFINITION_REGION, null, "kWh", null
  );

  assert.equal(text, "5,553.70 kWh");
});

test("没有权威格式：不改显示（返回 null，调用方退回原来的写法）", () => {
  const api = loadFormatApi();

  const text = api.authoritativeDisplayValue(5553.7, { id: "r" }, null, "kWh", null);

  assert.equal(text, null);
});

// ==========================================================================
// 指标卡正文：5,553.70 kWh 真的画在 DOM 上
// ==========================================================================

test("指标卡正文：按权威格式渲染出 5,553.70 kWh", () => {
  const api = loadApi(
    [
      "renderRegionPayloadBody", "formatMetricValue", "regionAuthoritativeFormat",
      "formatAuthoritativeNumber", "formatAuthoritativeDate", "formatDisplayValueByFormat",
      "authoritativeDisplayValue", "hasAuthoritativeFormat",
    ],
    {
      extraStatements: [
        extractConstBlock("AUTHORITATIVE_FORMAT_KEYS"),
        extractConstBlock("AUTHORITATIVE_NUMERIC_FORMAT_KINDS"),
        extractConstBlock("AUTHORITATIVE_DATE_FORMAT_KINDS"),
        extractConstBlock("AUTHORITATIVE_ROUNDING_MODES"),
      ],
      globals: {
        esc: (value) => String(value ?? ""),
        // 图表分支用不到，但函数体里引用了它（编译期不查，运行到才查）。
        renderChartPayload: () => {},
        readTableTruncation: () => null,
        hasEcharts: () => false,
        window: {},
        document: { createElement: () => ({ style: {}, classList: { add() {}, remove() {} }, appendChild() {} }) },
      },
    }
  ).__api;

  const container = { innerHTML: "", querySelectorAll: () => [], querySelector: () => null };
  const region = {
    regionId: "region_1",
    name: "昨日总耗电",
    componentType: "METRIC_CARD",
    payload: METRIC_CARD_PAYLOAD,
  };

  api.renderRegionPayloadBody(container, region, region.payload, {
    unit: "kWh",
    defRegion: METRIC_CARD_DEFINITION_REGION,
    totalsByUnit: new Map(),
  });

  assert.ok(
    container.innerHTML.includes("5,553.70 kWh"),
    `指标卡正文应当是 5,553.70 kWh，实际是：${container.innerHTML}`
  );
  assert.ok(!container.innerHTML.includes(">5553.7<"), "不能还画着旧写法 5553.7");
});

// ==========================================================================
// 展示排版模块（BUG-001 卡片样式 / BUG-006 分组两列）
// ==========================================================================

/** 够用的假元素：属性、行内样式（只存写进去的）、子节点。 */
class FakeElement {
  constructor(tagName, ownerDocument) {
    this.tagName = String(tagName).toUpperCase();
    this.ownerDocument = ownerDocument;
    this.childNodes = [];
    this.parentNode = null;
    this.attributes = new Map();
    this.style = {
      props: {},
      setProperty(name, value) { this.props[name] = String(value); },
    };
    this._text = "";
  }

  get children() {
    return this.childNodes.filter((node) => node instanceof FakeElement);
  }

  get textContent() {
    let out = this._text;
    for (const child of this.childNodes) out += child.textContent;
    return out;
  }

  set textContent(value) {
    this._text = value === null || value === undefined ? "" : String(value);
    this.childNodes = [];
  }

  set innerHTML(value) { this.childNodes = []; }
  get innerHTML() { return ""; }

  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  getAttribute(name) { return this.attributes.has(name) ? this.attributes.get(name) : null; }
  removeAttribute(name) { this.attributes.delete(name); }

  appendChild(child) {
    child.parentNode = this;
    this.childNodes.push(child);
    return child;
  }

  querySelectorAll(selector) {
    const out = [];
    const walk = (node) => {
      for (const child of node.childNodes) {
        if (child instanceof FakeElement) {
          out.push(child);
          walk(child);
        }
      }
    };
    walk(this);
    return out;
  }

  addEventListener() {}
  removeEventListener() {}
  dispatchEvent() { return true; }
  getBoundingClientRect() { return { width: 0, height: 0 }; }
}

function makeFakeDocument() {
  const doc = {
    createElement(tagName) { return new FakeElement(tagName, doc); },
    defaultView: { addEventListener() {}, removeEventListener() {} },
  };
  doc.body = new FakeElement("body", doc);
  return doc;
}

function byPresentation(root, kind) {
  return root.querySelectorAll("*").filter((el) => el.getAttribute("data-presentation") === kind);
}

test("BUG-006 分组两列：DOM 上真实呈现两列与实际间距（不是单卡 placement 冒充）", () => {
  const P = require("../pages/workbench_presentation.js");
  const doc = makeFakeDocument();
  const root = doc.createElement("div");
  doc.body.appendChild(root);

  const definition = {
    page: { arrangement: { mode: "GRID", columns: 12, gap: { value: 12, unit: "PX" } } },
    regions: [
      {
        id: "g1",
        name: "能耗概览",
        component: {
          type: "CONTAINER",
          arrangement: { mode: "GRID", columns: 2, direction: "HORIZONTAL", gap: { value: 20, unit: "PX" } },
        },
        children: [
          { id: "a1", name: "甲", component: { type: "METRIC_CARD" } },
          { id: "a2", name: "乙", component: { type: "METRIC_CARD" } },
        ],
      },
    ],
  };

  P.render(root, { definition, previewPayload: { regions: [] } }, {
    renderLeaf: (leafRegion) => {
      const card = doc.createElement("div");
      card.setAttribute("class", "bg-white rounded-xl border border-slate-200 p-5");
      return card;
    },
  });

  const groups = byPresentation(root, "group");
  assert.equal(groups.length, 1, "组应当渲染成一个 <section>");
  const group = groups[0];

  assert.equal(group.getAttribute("data-arrangement-mode"), "GRID");
  assert.equal(group.getAttribute("data-arrangement-columns"), "2");
  assert.equal(group.getAttribute("data-gap"), "20");
  assert.equal(group.style.display, "grid", "组要真的按网格排");
  assert.equal(group.style.gridTemplateColumns, "repeat(2, minmax(0, 1fr))", "两列");
  assert.equal(group.style.gap, "20px", "实际间距 20 像素");

  // 组内子卡在组里（能定位到组，也能定位到子卡）。
  const leaves = byPresentation(root, "leaf");
  assert.equal(leaves.length, 2);
  assert.deepEqual(leaves.map((leaf) => leaf.getAttribute("data-region-id")), ["a1", "a2"]);
  for (const leaf of leaves) {
    assert.equal(leaf.parentNode, group, "子卡要挂在组里");
  }
});

test("BUG-001 卡片样式：fontSize / textAlign / borderRadius 真的落到 DOM 上", () => {
  const P = require("../pages/workbench_presentation.js");
  const doc = makeFakeDocument();
  const root = doc.createElement("div");
  doc.body.appendChild(root);

  const definition = {
    page: { arrangement: { mode: "GRID", columns: 12, gap: { value: 12, unit: "PX" } } },
    regions: [
      {
        id: "r1",
        name: "昨日能耗总览",
        component: { type: "METRIC_CARD" },
        style: {
          fontSize: { value: 28, unit: "PX" },
          textAlign: "RIGHT",
          borderRadius: 4,
        },
      },
    ],
  };

  let card = null;
  P.render(root, { definition, previewPayload: { regions: [] } }, {
    renderLeaf: () => {
      card = doc.createElement("div");
      card.setAttribute("class", "bg-white rounded-xl border border-slate-200 p-5");
      return card;
    },
  });

  assert.ok(card, "卡片渲染出来了");
  assert.equal(card.getAttribute("data-wp-font-size"), "1", "字号覆盖要落到卡片上");
  assert.equal(card.getAttribute("data-wp-text-align"), "1", "对齐覆盖要落到卡片上");
  assert.equal(card.getAttribute("data-wp-border-radius"), "1", "圆角覆盖要落到卡片上");
  assert.equal(card.style.props["--wp-font-size"], "28px");
  assert.equal(card.style.props["--wp-text-align"], "right");
  assert.equal(card.style.props["--wp-border-radius"], "4px");
  assert.ok(String(card.getAttribute("class")).includes("wp-card"), "卡片要带上排版模块的标记类");
});

test("窄卡：文字不竖排、按钮不越界（样式表里要有那几条规则）", () => {
  assert.ok(WORKBENCH_CSS.includes("writing-mode: horizontal-tb"), "要明确横排，不让中文被竖着排");
  assert.ok(
    /\.wp-card \[data-region-metric\] > div \{[^}]*white-space: nowrap/s.test(WORKBENCH_CSS),
    "数值区要 nowrap（一个字一行看着像竖排）"
  );
  assert.ok(/\.wp-card button,\s*\.wp-card select \{[^}]*max-width: 100%/s.test(WORKBENCH_CSS), "按钮不越界");
});

test("样式覆盖要真的作用到卡片内容上（CSS 变量 + 选择器都在）", () => {
  assert.ok(WORKBENCH_CSS.includes("font-size: var(--wp-font-size)"), "字号变量要被用上");
  assert.ok(WORKBENCH_CSS.includes("text-align: var(--wp-text-align)"), "对齐变量要被用上");
  assert.ok(WORKBENCH_CSS.includes("border-radius: var(--wp-border-radius)"), "圆角变量要被用上");
});
