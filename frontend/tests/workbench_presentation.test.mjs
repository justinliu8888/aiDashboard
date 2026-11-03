// P3：报表展示排版模块（workbench_presentation.js）的行为测试。
//
// 运行：node --test frontend/tests/workbench_presentation.test.mjs
// 不依赖任何第三方包（Node 内置 test runner + 手写 DOM 桩）。
//
// ── 被测的缝（seam）─────────────────────────────────────────────
// 冻结契约 0002 §5 约定的公开缝：
//   WorkbenchPresentation.render(root, view, {renderLeaf}) -> 根元素 | null
//   WorkbenchPresentation.clear(root)                       -> undefined
//   WorkbenchPresentation.buildTree(definition)              -> 节点数组
//   WorkbenchPresentation.resolveArrangement(region)        -> arrangement | null
//   WorkbenchPresentation.mergeStyle(styleChain, theme)     -> 合并后的 style
//   WorkbenchPresentation.regionPath(definition, regionId)  -> id 路径数组
//
// ── DOM 桩刻意做得很「笨」──────────────────────────────────────
//   - 没有 innerHTML 解析能力：写入 innerHTML 只会**清空**子节点（并被记录）
//   - style 是普通对象：只存被写进去的属性，方便逐条断言
//   - 没有 ResizeObserver 以外的浏览器能力
// 这样「不写任意 CSS 文本」「只写受控属性」才有意义，而不是自说自话。
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const P = require("../pages/workbench_presentation.js");

// ---------------------------------------------------------------- DOM 桩

class FakeElement {
  constructor(tagName, ownerDocument) {
    this.tagName = String(tagName).toUpperCase();
    this.ownerDocument = ownerDocument;
    this.childNodes = [];
    this.parentNode = null;
    this.attributes = new Map();
    this.listeners = new Map();
    // 行内样式：真实浏览器里是 CSSStyleDeclaration，桩里只保留写进去的属性。
    this.style = {};
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
    for (const child of this.childNodes) child.parentNode = null;
    this.childNodes = [];
  }

  // 模块一旦写 innerHTML 就被记下来（真实浏览器会解析并可能执行脚本）
  get innerHTML() {
    return "";
  }

  set innerHTML(value) {
    this.ownerDocument.innerHTMLWrites.push({ node: this, value });
    for (const child of this.childNodes) child.parentNode = null;
    this.childNodes = [];
    this._text = "";
  }

  setAttribute(name, value) {
    this.attributes.set(String(name), String(value));
  }

  getAttribute(name) {
    return this.attributes.has(String(name)) ? this.attributes.get(String(name)) : null;
  }

  hasAttribute(name) {
    return this.attributes.has(String(name));
  }

  removeAttribute(name) {
    this.attributes.delete(String(name));
  }

  get classList() {
    const self = this;
    const read = () => String(self.getAttribute("class") || "").split(/\s+/).filter(Boolean);
    const write = (list) => self.setAttribute("class", list.join(" "));
    return {
      add(...names) {
        const list = read();
        for (const name of names) if (!list.includes(name)) list.push(name);
        write(list);
      },
      remove(...names) {
        write(read().filter((item) => !names.includes(item)));
      },
      contains(name) {
        return read().includes(name);
      },
    };
  }

  get dataset() {
    const self = this;
    const toAttr = (key) => "data-" + String(key).replace(/[A-Z]/g, (ch) => "-" + ch.toLowerCase());
    return new Proxy(
      {},
      {
        get(_target, key) {
          if (typeof key !== "string") return undefined;
          return self.getAttribute(toAttr(key));
        },
        set(_target, key, value) {
          self.setAttribute(toAttr(key), value);
          return true;
        },
        has(_target, key) {
          if (typeof key !== "string") return false;
          return self.hasAttribute(toAttr(key));
        },
      }
    );
  }

  appendChild(child) {
    if (child.parentNode) child.parentNode.removeChild(child);
    child.parentNode = this;
    this.childNodes.push(child);
    return child;
  }

  removeChild(child) {
    const index = this.childNodes.indexOf(child);
    if (index >= 0) this.childNodes.splice(index, 1);
    child.parentNode = null;
    return child;
  }

  remove() {
    if (this.parentNode) this.parentNode.removeChild(this);
  }

  addEventListener(type, handler) {
    const key = String(type);
    if (!this.listeners.has(key)) this.listeners.set(key, []);
    this.listeners.get(key).push(handler);
  }

  removeEventListener(type, handler) {
    const key = String(type);
    const list = this.listeners.get(key);
    if (!list) return;
    const index = list.indexOf(handler);
    if (index >= 0) list.splice(index, 1);
  }

  dispatchEvent(event) {
    const list = this.listeners.get(String(event.type)) || [];
    for (const handler of list.slice()) handler.call(this, event);
    return true;
  }
}

class FakeResizeObserver {
  constructor(callback) {
    this.callback = callback;
    this.observed = [];
    this.disconnected = false;
    FakeResizeObserver.instances.push(this);
  }
  observe(element) {
    this.observed.push(element);
  }
  unobserve(element) {
    const index = this.observed.indexOf(element);
    if (index >= 0) this.observed.splice(index, 1);
  }
  disconnect() {
    this.disconnected = true;
    this.observed = [];
  }
  trigger() {
    if (this.disconnected) return;
    if (typeof this.callback === "function") this.callback([], this);
  }
}
FakeResizeObserver.instances = [];

function makeFakeDocument() {
  const doc = {
    innerHTMLWrites: [],
    createElement(tagName) {
      return new FakeElement(tagName, doc);
    },
    createTextNode(text) {
      return { nodeType: 3, textContent: String(text) };
    },
    defaultView: {
      ResizeObserver: FakeResizeObserver,
      addEventListener() {},
      removeEventListener() {},
    },
  };
  doc.root = doc.createElement("body");
  return doc;
}

// ---------------------------------------------------------------- 取数工具

function walk(node, out = []) {
  for (const child of node.childNodes) {
    if (child instanceof FakeElement) out.push(child);
    walk(child, out);
  }
  return out;
}

function byPresentation(root, value) {
  return walk(root).filter((node) => node.getAttribute("data-presentation") === value);
}

function leafIds(root) {
  return byPresentation(root, "leaf").map((node) => node.getAttribute("data-region-id"));
}

function allRegionIds(root) {
  return walk(root)
    .filter((node) => node.hasAttribute("data-region-id"))
    .map((node) => node.getAttribute("data-region-id"));
}

function contains(root, node) {
  let current = node;
  while (current) {
    if (current === root) return true;
    current = current.parentNode;
  }
  return false;
}

/** 错误提示条（data-presentation-error="1"）。 */
function errorBoxes(root) {
  return walk(root).filter((node) => node.getAttribute("data-presentation-error") === "1");
}

/** 渲染结果的指纹：结构 + 属性 + 行内样式（用来比「渲染结果有没有变」）。 */
function signatureOf(root) {
  return walk(root).map((node) => ({
    tag: node.tagName,
    presentation: node.getAttribute("data-presentation"),
    regionId: node.getAttribute("data-region-id"),
    hidden: node.getAttribute("data-hidden"),
    regionError: node.getAttribute("data-region-error"),
    cls: node.getAttribute("class"),
    style: Object.keys(node.style)
      .sort()
      .map((key) => key + "=" + node.style[key]),
  }));
}

/** 渲染一次并返回指纹（每次用全新的 DOM 桩，互不干扰）。 */
function renderSignature(view) {
  const doc = makeFakeDocument();
  const root = doc.createElement("div");
  doc.root.appendChild(root);
  P.render(root, view, { renderLeaf: makeRenderLeaf(doc) });
  return signatureOf(root);
}

// ---------------------------------------------------------------- 契约样例

/** 旧平面定义（没有 children）：改造前就是这种。 */
const FLAT_DEFINITION = {
  page: { arrangement: { mode: "GRID", columns: 12, gap: { value: 12, unit: "PX" } } },
  regions: [
    { id: "r1", name: "甲", role: "SUMMARY", component: { type: "METRIC_CARD" } },
    { id: "r2", name: "乙", role: "TREND", component: { type: "CHART" } },
    { id: "r3", name: "丙", role: "DETAIL", component: { type: "TABLE" } },
  ],
};

function makeRenderLeaf(doc, calls) {
  return (leafRegion, payloadRegion, ctx) => {
    if (calls) calls.push({ leafRegion, payloadRegion, ctx });
    const element = doc.createElement("div");
    element.setAttribute("data-test-leaf", leafRegion && leafRegion.id ? leafRegion.id : "");
    return element;
  };
}

// ---------------------------------------------------------------- 用例

test("test_flat_definition_renders_leaves_in_order", () => {
  const doc = makeFakeDocument();
  const root = doc.createElement("div");
  doc.root.appendChild(root);

  P.render(root, { definition: FLAT_DEFINITION, previewPayload: { regions: [] } }, {
    renderLeaf: makeRenderLeaf(doc),
  });

  assert.deepEqual(leafIds(root), ["r1", "r2", "r3"]);
  // 根容器按 page.arrangement 排版
  const rootEl = byPresentation(root, "root")[0];
  assert.ok(rootEl, "根容器应当存在");
  assert.equal(rootEl.getAttribute("data-arrangement-mode"), "GRID");
  assert.equal(rootEl.getAttribute("data-arrangement-columns"), "12");
  assert.equal(rootEl.getAttribute("data-gap"), "12");
});

test("test_container_renders_group_with_two_columns", () => {
  const doc = makeFakeDocument();
  const root = doc.createElement("div");
  doc.root.appendChild(root);

  const definition = {
    page: { arrangement: { mode: "GRID", columns: 12, gap: { value: 12, unit: "PX" } } },
    regions: [
      {
        id: "g1",
        name: "对比组",
        component: {
          type: "CONTAINER",
          arrangement: { mode: "GRID", columns: 2, direction: "HORIZONTAL", gap: { value: 16, unit: "PX" } },
        },
        children: [
          { id: "a1", name: "甲", component: { type: "METRIC_CARD" } },
          { id: "a2", name: "乙", component: { type: "METRIC_CARD" } },
        ],
      },
    ],
  };

  P.render(root, { definition, previewPayload: { regions: [] } }, { renderLeaf: makeRenderLeaf(doc) });

  const groups = byPresentation(root, "group");
  assert.equal(groups.length, 1);
  const group = groups[0];
  assert.equal(group.tagName, "SECTION");
  assert.equal(group.getAttribute("data-region-id"), "g1");
  assert.equal(group.getAttribute("data-arrangement-mode"), "GRID");
  assert.equal(group.getAttribute("data-arrangement-columns"), "2");
  assert.equal(group.getAttribute("data-gap"), "16");
  assert.equal(group.style.display, "grid");
  assert.equal(group.style.gridTemplateColumns, "repeat(2, minmax(0, 1fr))");
  assert.equal(group.style.gap, "16px");

  // 子叶子在 group 内
  const leaves = byPresentation(root, "leaf");
  assert.equal(leaves.length, 2);
  for (const leaf of leaves) assert.ok(contains(group, leaf), "子叶子必须在 group 内");
  assert.deepEqual(
    leaves.map((leaf) => leaf.getAttribute("data-region-id")),
    ["a1", "a2"],
  );

  // 容器自己不出卡片：group 才是分组元素
  assert.equal(P.buildTree(definition)[0].isContainer, true);
});

test("test_hidden_region_absent_by_default_and_shown_with_flag", () => {
  const doc = makeFakeDocument();
  const root = doc.createElement("div");
  doc.root.appendChild(root);

  const definition = {
    regions: [
      { id: "v1", name: "可见甲", component: { type: "METRIC_CARD" } },
      { id: "h1", name: "隐藏乙", hidden: true, component: { type: "METRIC_CARD" } },
      { id: "v2", name: "可见丙", component: { type: "METRIC_CARD" } },
    ],
  };
  const view = { definition, previewPayload: { regions: [] } };

  P.render(root, view, { renderLeaf: makeRenderLeaf(doc) });
  assert.deepEqual(leafIds(root), ["v1", "v2"]);
  assert.equal(
    allRegionIds(root).includes("h1"),
    false,
    "隐藏卡片默认不进 DOM（其它内容补位，不留空）",
  );

  P.render(root, { ...view, showHidden: true }, { renderLeaf: makeRenderLeaf(doc) });
  const hiddenLeaves = byPresentation(root, "hidden-leaf");
  assert.equal(hiddenLeaves.length, 1);
  assert.equal(hiddenLeaves[0].getAttribute("data-region-id"), "h1");
  assert.equal(hiddenLeaves[0].getAttribute("data-hidden"), "true");
  assert.equal(hiddenLeaves[0].style.opacity, "0.45");
  // 按原阅读顺序插回
  assert.deepEqual(allRegionIds(root), ["v1", "h1", "v2"]);
});

test("test_style_merge_order_theme_then_ancestor_then_region", () => {
  const merged = P.mergeStyle(
    [
      { fontSize: { value: 16, unit: "PX" } }, // 祖先
      { fontSize: { value: 20, unit: "PX" } }, // 当前区域
    ],
    { defaultStyle: { fontSize: { value: 12, unit: "PX" }, backgroundColor: "#ffffff" } },
  );

  assert.deepEqual(merged.fontSize, { value: 20, unit: "PX" }, "区域样式盖过祖先与主题默认");
  assert.equal(merged.backgroundColor, "#ffffff", "主题默认继续生效");

  // 只有祖先时，祖先盖过主题默认
  const ancestorOnly = P.mergeStyle([{ fontSize: { value: 16, unit: "PX" } }], {
    defaultStyle: { fontSize: { value: 12, unit: "PX" } },
  });
  assert.deepEqual(ancestorOnly.fontSize, { value: 16, unit: "PX" });

  // 没有主题时链内后者覆盖前者
  const noTheme = P.mergeStyle([{ fontWeight: 400 }, { fontWeight: 700 }], null);
  assert.equal(noTheme.fontWeight, 700);
});

test("test_unsafe_style_values_are_dropped", () => {
  const merged = P.mergeStyle(
    [
      { backgroundColor: "url(javascript:alert(1))" },
      { fontFamily: "url(javascript:alert(1))" },
      { textColor: "expression(alert(1))" },
      { borderRadius: "<script>" },
      { color: "#123456" }, // 未知键：不在白名单里
      { madeUpKey: "x" },
    ],
    { defaultStyle: { backgroundColor: "#eeeeee" } },
  );

  assert.equal("fontFamily" in merged, false);
  assert.equal("textColor" in merged, false);
  assert.equal("borderRadius" in merged, false);
  assert.equal("color" in merged, false, "白名单外的键不进最终 style");
  assert.equal("madeUpKey" in merged, false);
  // 非法覆盖被丢弃：下层（主题默认）的值继续生效
  assert.equal(merged.backgroundColor, "#eeeeee");

  // 最终 style 只映射成受控的 inline 属性，不拼 cssText
  const css = P.styleToCss({ fontSize: { value: 14, unit: "PX" }, textAlign: "CENTER" });
  assert.deepEqual(css, { fontSize: "14px", textAlign: "center" });
});

test("test_render_leaf_receives_payload_matched_by_region_id", () => {
  const doc = makeFakeDocument();
  const root = doc.createElement("div");
  doc.root.appendChild(root);

  const calls = [];
  const definition = {
    regions: [
      { id: "r1", name: "甲", component: { type: "METRIC_CARD" } },
      { id: "r2", name: "乙", component: { type: "METRIC_CARD" } },
    ],
  };
  // 载荷顺序与定义顺序**不一致**：只能靠 regionId 关联。
  const previewPayload = {
    regions: [
      { regionId: "r2", componentType: "METRIC_CARD", payload: { value: 22, unit: "kW" } },
      { regionId: "r1", componentType: "METRIC_CARD", payload: { value: 11, unit: "kWh" } },
    ],
  };

  P.render(root, { definition, previewPayload }, { renderLeaf: makeRenderLeaf(doc, calls) });

  assert.equal(calls.length, 2);
  assert.equal(calls[0].leafRegion.id, "r1");
  assert.ok(calls[0].payloadRegion, "r1 应当匹配到自己的载荷");
  assert.equal(calls[0].payloadRegion.regionId, "r1");
  assert.equal(calls[0].payloadRegion.payload.value, 11);
  assert.equal(calls[1].payloadRegion.regionId, "r2");
  assert.equal(calls[1].payloadRegion.payload.value, 22);

  // ctx 里带上总量表、定义与载荷
  assert.ok(calls[0].ctx.totalsByUnit instanceof Map);
  assert.equal(calls[0].ctx.totalsByUnit.get("kWh"), 11);
  assert.equal(calls[0].ctx.totalsByUnit.get("kW"), 22);
  assert.equal(calls[0].ctx.definition, definition);
  assert.equal(calls[0].ctx.payload, previewPayload);
  assert.equal(calls[0].ctx.hidden, false);

  // 载荷里没有的叶子：照样渲染卡片外壳（不静默丢卡）
  const calls2 = [];
  P.render(root, { definition, previewPayload: { regions: [] } }, {
    renderLeaf: makeRenderLeaf(doc, calls2),
  });
  assert.equal(calls2.length, 2);
  assert.equal(calls2[0].payloadRegion, null);
  assert.deepEqual(leafIds(root), ["r1", "r2"]);
});

test("test_clear_releases_observers_and_empties_root", () => {
  const doc = makeFakeDocument();
  const root = doc.createElement("div");
  doc.root.appendChild(root);

  const before = FakeResizeObserver.instances.length;
  P.render(root, { definition: FLAT_DEFINITION, previewPayload: { regions: [] } }, {
    renderLeaf: makeRenderLeaf(doc),
  });

  const created = FakeResizeObserver.instances.slice(before);
  assert.ok(created.length > 0, "应当注册了 ResizeObserver");
  const state = root.__workbenchPresentationState;
  assert.ok(state, "渲染状态应当挂在 root 上");
  assert.equal(state.observers.length, created.length);
  assert.equal(root.childNodes.length, 1, "渲染后 root 里应有本模块的根元素");

  P.clear(root);

  assert.ok(created.every((observer) => observer.disconnected), "observer 应当全部断开");
  assert.equal(state.disposed, true);
  assert.equal(state.observers.length, 0);
  assert.equal(root.childNodes.length, 0, "clear 之后 root 应当为空");

  // 再次 render：旧的 observer 先被释放，不会越攒越多
  const beforeSecond = FakeResizeObserver.instances.length;
  P.render(root, { definition: FLAT_DEFINITION, previewPayload: { regions: [] } }, {
    renderLeaf: makeRenderLeaf(doc),
  });
  assert.ok(created.every((observer) => observer.disconnected));
  assert.ok(FakeResizeObserver.instances.length > beforeSecond);
  assert.ok(FakeResizeObserver.instances.slice(beforeSecond).every((o) => !o.disconnected));
});

// ---------------------------------------------------------------- 补充用例

test("纯函数：buildTree / resolveArrangement / regionPath", () => {
  const definition = {
    regions: [
      { id: "g1", component: { type: "CONTAINER", arrangement: { mode: "FLOW", direction: "VERTICAL", gap: { value: 8, unit: "PX" } } }, children: [{ id: "c1", component: { type: "METRIC_CARD" } }] },
      { id: "r1", component: { type: "METRIC_CARD" } },
    ],
  };

  const tree = P.buildTree(definition);
  assert.equal(tree.length, 2);
  assert.equal(tree[0].isContainer, true);
  assert.equal(tree[0].children.length, 1);
  assert.equal(tree[0].children[0].id, "c1");
  assert.equal(tree[1].isContainer, false);
  assert.equal(tree[1].arrangement, null, "叶子没有 arrangement");

  const arrangement = P.resolveArrangement(tree[0].region);
  assert.equal(arrangement.mode, "FLOW");
  assert.equal(arrangement.direction, "VERTICAL");
  assert.equal(arrangement.gap, 8);
  assert.equal(P.resolveArrangement(tree[1].region), null);

  assert.deepEqual(P.regionPath(definition, "c1"), ["g1", "c1"]);
  assert.deepEqual(P.regionPath(definition, "r1"), ["r1"]);
  assert.deepEqual(P.regionPath(definition, "nope"), []);
});

test("隐藏容器：默认不渲染，showHidden 时带 data-hidden", () => {
  const doc = makeFakeDocument();
  const root = doc.createElement("div");
  doc.root.appendChild(root);

  const definition = {
    regions: [
      {
        id: "g1",
        hidden: true,
        component: { type: "CONTAINER", arrangement: { mode: "GRID", columns: 2, gap: { value: 16, unit: "PX" } } },
        children: [{ id: "c1", component: { type: "METRIC_CARD" } }],
      },
    ],
  };

  P.render(root, { definition, previewPayload: { regions: [] } }, { renderLeaf: makeRenderLeaf(doc) });
  assert.equal(byPresentation(root, "group").length, 0);

  P.render(root, { definition, previewPayload: { regions: [] }, showHidden: true }, {
    renderLeaf: makeRenderLeaf(doc),
  });
  const group = byPresentation(root, "group")[0];
  assert.ok(group);
  assert.equal(group.getAttribute("data-hidden"), "true");
  assert.equal(group.style.opacity, "0.45");
  assert.deepEqual(leafIds(root), ["c1"]);
});

// ---------------------------------------------------------------- 主题消费

test("test_theme_tokens_apply_background_border_and_font_size", () => {
  const doc = makeFakeDocument();
  const root = doc.createElement("div");
  doc.root.appendChild(root);

  const definition = {
    regions: [{ id: "r1", name: "甲", component: { type: "METRIC_CARD" } }],
  };
  const resolvedTheme = {
    themeId: "theme_test",
    themeRevision: 3,
    name: "测试主题",
    tokens: {
      colorBackground: "#FFFFFF",
      colorSurface: "#EEF2FF",
      colorText: "#1F2329",
      colorBorder: "#334155",
      fontFamily: "PingFang SC, sans-serif",
      fontSizeBase: 18,
      lineHeightBase: 1.6,
      radiusBase: 8,
    },
    defaultStyle: {},
  };

  P.render(root, { definition, previewPayload: { regions: [] }, resolvedTheme }, {
    renderLeaf: makeRenderLeaf(doc),
  });

  const leaf = byPresentation(root, "leaf")[0];
  assert.ok(leaf, "主题区域应当照常渲染");
  // 背景 / 边框 / 字号：token 落到样式上
  assert.equal(leaf.style.backgroundColor, "#EEF2FF", "colorSurface 落到区域背景");
  assert.equal(leaf.style.border, "1px solid #334155", "colorBorder 落到边框");
  assert.equal(leaf.style.fontSize, "18px", "fontSizeBase 落到字号");
  // 其它能落到白名单键上的 token
  assert.equal(leaf.style.color, "#1F2329", "colorText 落到文字颜色");
  assert.equal(leaf.style.fontFamily, "PingFang SC, sans-serif");
  assert.equal(leaf.style.borderRadius, "8px");
  assert.equal(leaf.style.lineHeight, "1.6");
  // 页面底色落在根容器
  const rootEl = byPresentation(root, "root")[0];
  assert.equal(rootEl.style.backgroundColor, "#FFFFFF");
});

test("test_theme_does_not_override_region_local_style", () => {
  const doc = makeFakeDocument();
  const root = doc.createElement("div");
  doc.root.appendChild(root);

  const definition = {
    regions: [
      {
        id: "g1",
        component: { type: "CONTAINER", arrangement: { mode: "GRID", columns: 2, gap: { value: 16, unit: "PX" } } },
        style: { backgroundColor: "#222222", fontSize: { value: 18, unit: "PX" } },
        children: [
          { id: "c1", component: { type: "METRIC_CARD" }, style: { backgroundColor: "#333333" } },
          { id: "c2", component: { type: "METRIC_CARD" } },
        ],
      },
    ],
  };
  const resolvedTheme = {
    themeId: "theme_test",
    themeRevision: 1,
    name: "测试主题",
    tokens: { colorSurface: "#EEF2FF", fontSizeBase: 16 },
    defaultStyle: { backgroundColor: "#FFFFFF", fontSize: { value: 12, unit: "PX" } },
  };

  P.render(root, { definition, previewPayload: { regions: [] }, resolvedTheme }, {
    renderLeaf: makeRenderLeaf(doc),
  });

  const group = byPresentation(root, "group")[0];
  assert.equal(group.style.backgroundColor, "#222222", "区域自身样式盖过主题");
  assert.equal(group.style.fontSize, "18px");

  const leaves = byPresentation(root, "leaf");
  const c1 = leaves.find((leaf) => leaf.getAttribute("data-region-id") === "c1");
  const c2 = leaves.find((leaf) => leaf.getAttribute("data-region-id") === "c2");
  assert.equal(c1.style.backgroundColor, "#333333", "末端优先：自身样式盖过祖先与主题");
  assert.equal(c2.style.backgroundColor, "#222222", "没有局部样式时继承祖先的");
  assert.equal(c2.style.fontSize, "18px", "祖先样式盖过主题默认");
});

test("test_theme_only_changes_appearance_not_layout", () => {
  const doc = makeFakeDocument();
  const root = doc.createElement("div");
  doc.root.appendChild(root);

  const definition = {
    page: { arrangement: { mode: "GRID", columns: 12, gap: { value: 12, unit: "PX" } } },
    regions: [
      {
        id: "g1",
        component: { type: "CONTAINER", arrangement: { mode: "GRID", columns: 2, direction: "HORIZONTAL", gap: { value: 16, unit: "PX" } } },
        children: [
          { id: "c1", component: { type: "METRIC_CARD" }, layout: { placement: { type: "GRID", column: 1, columnSpan: 2, row: 3 } } },
        ],
      },
    ],
  };
  const resolvedTheme = {
    themeId: "theme_test",
    themeRevision: 1,
    name: "测试主题",
    tokens: { colorSurface: "#EEF2FF", gapBase: 40, spacingUnit: 24 },
    defaultStyle: { padding: { value: 20, unit: "PX" } },
  };

  P.render(root, { definition, previewPayload: { regions: [] }, resolvedTheme }, {
    renderLeaf: makeRenderLeaf(doc),
  });

  const rootEl = byPresentation(root, "root")[0];
  const group = byPresentation(root, "group")[0];
  const leaf = byPresentation(root, "leaf")[0];

  // 排版相关：主题不许动
  assert.equal(rootEl.getAttribute("data-arrangement-columns"), "12");
  assert.equal(rootEl.getAttribute("data-gap"), "12");
  assert.equal(rootEl.style.gap, "12px");
  assert.equal(group.getAttribute("data-arrangement-columns"), "2");
  assert.equal(group.getAttribute("data-gap"), "16");
  assert.equal(group.style.gap, "16px", "gapBase 不许改间距");
  assert.equal(group.style.gridTemplateColumns, "repeat(2, minmax(0, 1fr))");
  // 位置相关：主题不许动
  assert.equal(leaf.getAttribute("data-placement-type"), "GRID");
  assert.equal(leaf.getAttribute("data-grid-column"), "1");
  assert.equal(leaf.getAttribute("data-grid-row"), "3");
  assert.equal(leaf.style.gridColumn, "1 / span 2");
});

test("test_missing_theme_keeps_render_unchanged", () => {
  const definition = {
    regions: [{ id: "r1", name: "甲", component: { type: "METRIC_CARD" } }],
  };
  const payload = { regions: [] };

  const baseline = renderSignature({ definition, previewPayload: payload });
  assert.ok(baseline.length > 0, "基准渲染不为空");

  // 没有主题 / 空主题 / 只有 themeId：三者必须**完全一样**
  const nullTheme = renderSignature({ definition, previewPayload: payload, resolvedTheme: null });
  const emptyTheme = renderSignature({ definition, previewPayload: payload, resolvedTheme: {} });
  const idOnlyTheme = renderSignature({ definition, previewPayload: payload, resolvedTheme: { themeId: "theme_x" } });
  assert.deepEqual(nullTheme, baseline, "resolvedTheme 为 null 时渲染结果不变");
  assert.deepEqual(emptyTheme, baseline, "resolvedTheme 为空对象时渲染结果不变");
  assert.deepEqual(idOnlyTheme, baseline, "resolvedTheme 只有 themeId 时渲染结果不变");

  // 没有主题时不写任何外观样式（老草稿没主题也照常渲染）
  const doc = makeFakeDocument();
  const root = doc.createElement("div");
  doc.root.appendChild(root);
  P.render(root, { definition, previewPayload: payload }, { renderLeaf: makeRenderLeaf(doc) });
  const leaf = byPresentation(root, "leaf")[0];
  assert.deepEqual(Object.keys(leaf.style), [], "没有主题时不写任何行内样式");
});

test("test_invalid_theme_values_are_skipped", () => {
  const style = P.themeStyleFromTokens({
    colorSurface: "red;position:fixed",
    colorText: "url(javascript:alert(1))",
    colorBorder: "#GGG",
    fontSizeBase: "abc",
    fontFamily: "<script>",
    radiusBase: 8,
    unknownToken: "#ffffff",
  });

  assert.equal(style.backgroundColor, undefined, "非法颜色跳过");
  assert.equal(style.textColor, undefined, "非法颜色跳过");
  assert.equal(style.border, undefined, "非法边框颜色跳过");
  assert.equal(style.fontSize, undefined, "非法字号跳过");
  assert.equal(style.fontFamily, undefined, "非法字体族跳过");
  assert.equal(style.borderRadius, 8, "合法 token 照常生效");
  assert.equal("unknownToken" in style, false, "白名单外的 token 不进样式");

  // 坏值不会进 DOM
  const doc = makeFakeDocument();
  const root = doc.createElement("div");
  doc.root.appendChild(root);
  const definition = { regions: [{ id: "r1", component: { type: "METRIC_CARD" } }] };
  P.render(
    root,
    {
      definition,
      previewPayload: { regions: [] },
      resolvedTheme: {
        themeId: "theme_bad",
        themeRevision: 1,
        name: "坏主题",
        tokens: { colorSurface: "red;position:fixed", fontSizeBase: "abc" },
        defaultStyle: { backgroundColor: "#ffffff" },
      },
    },
    { renderLeaf: makeRenderLeaf(doc) },
  );
  const leaf = byPresentation(root, "leaf")[0];
  assert.equal(leaf.style.backgroundColor, "#ffffff", "defaultStyle 里的合法值照常生效");
  assert.equal(leaf.style.fontSize, undefined, "非法字号不会落到 DOM");
});

// ---------------------------------------------------------------- 错误态

test("test_region_errors_render_visible_error_state", () => {
  const doc = makeFakeDocument();
  const root = doc.createElement("div");
  doc.root.appendChild(root);

  const definition = {
    regions: [
      {
        id: "bad1",
        name: "有问题的卡",
        component: { type: "METRIC_CARD" },
        errors: [
          { code: "E_LAYOUT_OVERLAP", message: "与上方卡片重叠" },
          { code: "E_NO_DATA" },
        ],
      },
    ],
  };

  const calls = [];
  P.render(root, { definition, previewPayload: { regions: [] } }, {
    renderLeaf: makeRenderLeaf(doc, calls),
  });

  // 区域仍要渲染、仍可交互
  const leaf = byPresentation(root, "leaf")[0];
  assert.ok(leaf, "有错误的区域照常渲染");
  assert.equal(calls.length, 1, "有错误的区域照常走叶子渲染");
  assert.equal(leaf.getAttribute("data-region-id"), "bad1");

  // DOM 标记
  assert.equal(leaf.getAttribute("data-region-error"), "1");
  assert.equal(leaf.getAttribute("data-region-error-code"), "E_LAYOUT_OVERLAP");
  assert.equal(leaf.getAttribute("data-region-error-codes"), "E_LAYOUT_OVERLAP,E_NO_DATA");
  // 红色边框
  assert.equal(leaf.style.border, "1px solid #dc2626");

  // 提示文案：多个错误都列出来，缺 message 时用 code 兜底
  const boxes = errorBoxes(root);
  assert.equal(boxes.length, 1, "错误提示条应当只有一条");
  assert.ok(contains(leaf, boxes[0]), "提示条属于出错区域");
  const items = boxes[0].childNodes.filter((node) => node instanceof FakeElement);
  assert.equal(items.length, 2, "多个错误都要列出来");
  assert.equal(items[0].textContent, "与上方卡片重叠");
  assert.equal(items[0].getAttribute("data-region-error-code"), "E_LAYOUT_OVERLAP");
  assert.equal(items[1].textContent, "E_NO_DATA", "没有 message 时用 code 兜底");
});

test("test_container_errors_render_error_state", () => {
  const doc = makeFakeDocument();
  const root = doc.createElement("div");
  doc.root.appendChild(root);

  const definition = {
    regions: [
      {
        id: "g1",
        component: { type: "CONTAINER", arrangement: { mode: "GRID", columns: 2, gap: { value: 16, unit: "PX" } } },
        errors: [{ code: "E_GROUP_OVERLAP", message: "组内布局冲突", refRegionId: "c1" }],
        children: [{ id: "c1", component: { type: "METRIC_CARD" } }],
      },
    ],
  };

  P.render(root, { definition, previewPayload: { regions: [] } }, {
    renderLeaf: makeRenderLeaf(doc),
  });

  const group = byPresentation(root, "group")[0];
  assert.ok(group, "出错的容器照常渲染");
  assert.equal(group.getAttribute("data-region-error"), "1");
  assert.equal(group.getAttribute("data-region-error-code"), "E_GROUP_OVERLAP");
  assert.equal(group.style.border, "1px solid #dc2626");
  // 子区域照常渲染
  assert.deepEqual(leafIds(root), ["c1"]);

  const boxes = errorBoxes(root);
  assert.equal(boxes.length, 1);
  const items = boxes[0].childNodes.filter((node) => node instanceof FakeElement);
  assert.equal(items[0].textContent, "组内布局冲突");
  assert.equal(items[0].getAttribute("data-region-error-ref-region-id"), "c1");
});

test("test_no_errors_leaves_no_error_mark", () => {
  const doc = makeFakeDocument();
  const root = doc.createElement("div");
  doc.root.appendChild(root);

  P.render(root, { definition: FLAT_DEFINITION, previewPayload: { regions: [] } }, {
    renderLeaf: makeRenderLeaf(doc),
  });

  for (const leaf of byPresentation(root, "leaf")) {
    assert.equal(leaf.getAttribute("data-region-error"), null, "没有错误就没有错误标记");
    assert.equal(leaf.getAttribute("data-region-error-code"), null);
  }
  assert.equal(errorBoxes(root).length, 0, "没有错误就没有提示条");
});

// ---------------------------------------------------------------- 隐藏与补位

test("test_show_hidden_toggles_presence_and_marks_hidden", () => {
  const definition = {
    regions: [
      { id: "v1", name: "可见甲", component: { type: "METRIC_CARD" } },
      { id: "h1", name: "隐藏乙", hidden: true, component: { type: "METRIC_CARD" } },
      { id: "v2", name: "可见丙", component: { type: "METRIC_CARD" } },
    ],
  };

  // 关闭：隐藏内容**完全不进 DOM**（不占空间，其它内容补位）
  const docOff = makeFakeDocument();
  const rootOff = docOff.createElement("div");
  docOff.root.appendChild(rootOff);
  P.render(rootOff, { definition, previewPayload: { regions: [] } }, {
    renderLeaf: makeRenderLeaf(docOff),
  });
  assert.deepEqual(leafIds(rootOff), ["v1", "v2"]);
  assert.equal(allRegionIds(rootOff).includes("h1"), false, "关闭时隐藏区域不在 DOM 里");
  assert.equal(errorBoxes(rootOff).length, 0);

  // 开启：占位（半透明）且仍标 hidden 态
  const docOn = makeFakeDocument();
  const rootOn = docOn.createElement("div");
  docOn.root.appendChild(rootOn);
  P.render(rootOn, { definition, previewPayload: { regions: [] }, showHidden: true }, {
    renderLeaf: makeRenderLeaf(docOn),
  });
  const hiddenLeaf = byPresentation(rootOn, "hidden-leaf")[0];
  assert.ok(hiddenLeaf, "开启时隐藏区域在 DOM 里");
  assert.equal(hiddenLeaf.getAttribute("data-region-id"), "h1");
  assert.equal(hiddenLeaf.getAttribute("data-hidden"), "true", "开启时标 hidden 态");
  assert.equal(hiddenLeaf.style.opacity, "0.45", "开启时半透明");
  // 占位：仍在原来的阅读顺序里
  assert.deepEqual(allRegionIds(rootOn), ["v1", "h1", "v2"]);
});

// ---------------------------------------------------------------- R05 组自身定位 / 隐藏补位 / 窄屏减列

/**
 * 两卡组：形状照后端 CREATE_GROUP + _auto_placements 的样子。
 *
 *   - 组占满根栅格（column 1 / columnSpan 12）
 *   - 组内两列，间距 20px（不是旧流程里的 16px）
 *   - 两张子卡的定位是后端自动推导出来的（列 1 / 列 2，第 1 行）
 */
function twoCardGroupDefinition() {
  return {
    page: { arrangement: { mode: "GRID", columns: 12, gap: { value: 12, unit: "PX" } } },
    regions: [
      {
        id: "g1",
        name: "对比组",
        layout: { placement: { type: "GRID", column: 1, columnSpan: 12 } },
        component: {
          type: "CONTAINER",
          arrangement: { mode: "GRID", columns: 2, direction: "HORIZONTAL", gap: { value: 20, unit: "PX" } },
        },
        children: [
          {
            id: "a1",
            name: "甲",
            component: { type: "METRIC_CARD" },
            layout: { placement: { type: "GRID", column: 1, columnSpan: 1, row: 1, rowSpan: 1 } },
          },
          {
            id: "a2",
            name: "乙",
            component: { type: "METRIC_CARD" },
            layout: { placement: { type: "GRID", column: 2, columnSpan: 1, row: 1, rowSpan: 1 } },
          },
        ],
      },
    ],
  };
}

/** 渲染一次并返回 {root, doc}（每次用全新的 DOM 桩）。 */
function renderOnce(view, options = {}) {
  const doc = makeFakeDocument();
  const root = doc.createElement("div");
  doc.root.appendChild(root);
  P.render(root, view, { renderLeaf: makeRenderLeaf(doc), ...options });
  return { doc, root };
}

test("R05 组应用自身 placement：定位属性与排版都落在组元素上", () => {
  const { root } = renderOnce({ definition: twoCardGroupDefinition(), previewPayload: { regions: [] } });

  const group = byPresentation(root, "group")[0];
  assert.ok(group, "组应当渲染出来");

  // 组自己的定位：来自 definition.regions[].layout.placement
  assert.equal(group.getAttribute("data-placement-type"), "GRID");
  assert.equal(group.getAttribute("data-grid-column"), "1");
  assert.equal(group.getAttribute("data-grid-column-span"), "12");
  assert.equal(group.style.gridColumn, "1 / span 12", "组占定义要求的宽度（12 列）");

  // 组自己的排列：来自 component.arrangement
  assert.equal(group.getAttribute("data-arrangement-mode"), "GRID");
  assert.equal(group.getAttribute("data-arrangement-columns"), "2");
  assert.equal(group.getAttribute("data-gap"), "20");
  assert.equal(group.style.display, "grid");
  assert.equal(group.style.gridTemplateColumns, "repeat(2, minmax(0, 1fr))");
  assert.equal(group.style.gap, "20px");

  // 两张卡在同一行、同一组里
  const leaves = byPresentation(root, "leaf");
  assert.deepEqual(leaves.map((leaf) => leaf.getAttribute("data-region-id")), ["a1", "a2"]);
  assert.deepEqual(leaves.map((leaf) => leaf.getAttribute("data-grid-row")), ["1", "1"], "两卡同一行");
  for (const leaf of leaves) assert.ok(contains(group, leaf), "子卡在组内");
});

test("R05 组的 FLOW / ABSOLUTE 定位同样落到组元素上", () => {
  const definition = {
    page: { arrangement: { mode: "GRID", columns: 12, gap: { value: 12, unit: "PX" } } },
    regions: [
      {
        id: "gf",
        layout: { placement: { type: "FLOW", align: "CENTER", width: { value: 50, unit: "PERCENT" } } },
        component: { type: "CONTAINER", arrangement: { mode: "FLOW", direction: "VERTICAL", gap: { value: 8, unit: "PX" } } },
        children: [{ id: "f1", component: { type: "METRIC_CARD" } }],
      },
      {
        id: "ga",
        layout: { placement: { type: "ABSOLUTE", x: 10, y: 20, width: 300, height: 120, zIndex: 5 } },
        component: { type: "CONTAINER", arrangement: { mode: "GRID", columns: 2, gap: { value: 16, unit: "PX" } } },
        children: [{ id: "a1", component: { type: "METRIC_CARD" } }],
      },
    ],
  };
  const { root } = renderOnce({ definition, previewPayload: { regions: [] } });

  const groups = byPresentation(root, "group");
  assert.equal(groups.length, 2);
  const flow = groups.find((group) => group.getAttribute("data-region-id") === "gf");
  const abs = groups.find((group) => group.getAttribute("data-region-id") === "ga");

  assert.equal(flow.getAttribute("data-placement-type"), "FLOW");
  assert.equal(flow.getAttribute("data-placement-align"), "CENTER");
  assert.equal(flow.style.alignSelf, "center");
  assert.equal(flow.style.width, "50%");
  assert.equal(flow.getAttribute("data-arrangement-mode"), "FLOW");
  assert.equal(flow.getAttribute("data-arrangement-direction"), "VERTICAL");

  assert.equal(abs.getAttribute("data-placement-type"), "ABSOLUTE");
  assert.equal(abs.style.position, "absolute");
  assert.equal(abs.style.left, "10px");
  assert.equal(abs.style.top, "20px");
  assert.equal(abs.style.width, "300px");
  assert.equal(abs.style.height, "120px");
  assert.equal(abs.style.zIndex, "5");
});

test("R05 隐藏一张卡后另一张前移、没有空位（且不写定义）", () => {
  const definition = twoCardGroupDefinition();
  definition.regions[0].children[0].hidden = true;
  const before = JSON.stringify(definition);

  const { root } = renderOnce({ definition, previewPayload: { regions: [] } });

  assert.deepEqual(leafIds(root), ["a2"], "隐藏的卡不进 DOM");
  const a2 = byPresentation(root, "leaf")[0];
  assert.ok(a2);
  assert.equal(a2.getAttribute("data-grid-column"), "1", "乙前移到第 1 列（原来在第 2 列）");
  assert.equal(a2.getAttribute("data-grid-row"), "1");
  assert.equal(a2.style.gridColumn, "1 / span 1");

  // 没有空位：组仍然是两列，但渲染出来的卡只占第 1 列
  const group = byPresentation(root, "group")[0];
  assert.equal(group.getAttribute("data-arrangement-columns"), "2");
  assert.deepEqual(
    byPresentation(root, "leaf").map((leaf) => leaf.getAttribute("data-grid-column")),
    ["1"],
    "留下的卡占第 1 列，第 2 列没有留下空位",
  );

  // 补位靠布局重排，不靠写定义
  assert.equal(JSON.stringify(definition), before, "隐藏不改报表定义");
});

test("R05 隐藏中间的卡后，后面的卡也前移（不留空位）", () => {
  const definition = twoCardGroupDefinition();
  definition.regions[0].children.push({
    id: "a3",
    name: "丙",
    component: { type: "METRIC_CARD" },
    layout: { placement: { type: "GRID", column: 1, columnSpan: 1, row: 2, rowSpan: 1 } },
  });
  definition.regions[0].children[1].hidden = true; // 隐藏「乙」

  const { root } = renderOnce({ definition, previewPayload: { regions: [] } });

  assert.deepEqual(leafIds(root), ["a1", "a3"], "隐藏的卡不进 DOM，其余卡按原顺序");
  const leaves = byPresentation(root, "leaf");
  assert.equal(leaves[0].getAttribute("data-grid-column"), "1");
  assert.equal(leaves[0].getAttribute("data-grid-row"), "1");
  assert.equal(leaves[1].getAttribute("data-grid-column"), "2", "丙从第 1 行第 1 列前移到第 1 行第 2 列");
  assert.equal(leaves[1].getAttribute("data-grid-row"), "1");
  assert.deepEqual(
    leaves.map((leaf) => leaf.getAttribute("data-grid-row")),
    ["1", "1"],
    "两张卡同一行，没有第 2 行的空位",
  );
});

test("R05 打开「显示隐藏内容」按原序插回半透明卡，仍是隐藏态", () => {
  const definition = twoCardGroupDefinition();
  definition.regions[0].children[0].hidden = true;

  const { root } = renderOnce({
    definition,
    previewPayload: { regions: [] },
    showHidden: true,
  });

  const leaves = byPresentation(root, "leaf");
  const hiddenLeaves = byPresentation(root, "hidden-leaf");
  assert.deepEqual(leaves.map((leaf) => leaf.getAttribute("data-region-id")), ["a2"]);
  assert.deepEqual(hiddenLeaves.map((leaf) => leaf.getAttribute("data-region-id")), ["a1"]);

  // 按**原顺序**插回：隐藏卡在「乙」前面
  assert.deepEqual(allRegionIds(root), ["g1", "a1", "a2"], "隐藏卡按原顺序插回");

  // 仍是隐藏态：标 hidden + 半透明
  assert.equal(hiddenLeaves[0].getAttribute("data-hidden"), "true");
  assert.equal(hiddenLeaves[0].style.opacity, "0.45");
  assert.ok(hiddenLeaves[0].getAttribute("class").includes("wp-leaf-hidden"));

  // 临时让位：两张卡回到各自原来的位置
  assert.equal(hiddenLeaves[0].getAttribute("data-grid-column"), "1");
  assert.equal(leaves[0].getAttribute("data-grid-column"), "2");
});

test("R05 窄屏减列，恢复宽屏恢复两列", () => {
  const definition = twoCardGroupDefinition();

  // 窄屏：容器 400px < 480px → 组收成一列
  const narrow = renderOnce(
    { definition, previewPayload: { regions: [] } },
    { containerWidth: 400 },
  );
  const narrowGroup = byPresentation(narrow.root, "group")[0];
  assert.equal(narrowGroup.style.gridTemplateColumns, "repeat(1, minmax(0, 1fr))", "窄屏收成一列");
  assert.equal(narrowGroup.getAttribute("data-arrangement-columns"), "2", "声明列数不变（契约 §5.2）");
  assert.equal(
    narrowGroup.getAttribute("data-arrangement-columns-effective"),
    "1",
    "窄屏生效列数单独记一个属性",
  );

  const narrowRoot = byPresentation(narrow.root, "root")[0];
  assert.equal(narrowRoot.getAttribute("data-narrow-active"), "1");
  assert.equal(narrowRoot.getAttribute("data-narrow-breakpoint"), "480");
  assert.equal(narrowRoot.getAttribute("data-narrow-columns"), "1");
  assert.equal(narrowRoot.getAttribute("data-narrow-source"), "default");

  // 子项也按窄屏列数重排：两卡各占一行
  const narrowLeaves = byPresentation(narrow.root, "leaf");
  assert.deepEqual(narrowLeaves.map((leaf) => leaf.getAttribute("data-grid-column")), ["1", "1"]);
  assert.deepEqual(narrowLeaves.map((leaf) => leaf.getAttribute("data-grid-row")), ["1", "2"]);

  // 恢复宽屏：容器 1200px → 回到两列
  const wide = renderOnce(
    { definition, previewPayload: { regions: [] } },
    { containerWidth: 1200 },
  );
  const wideGroup = byPresentation(wide.root, "group")[0];
  assert.equal(wideGroup.style.gridTemplateColumns, "repeat(2, minmax(0, 1fr))", "恢复两列");
  assert.equal(wideGroup.getAttribute("data-arrangement-columns"), "2");
  assert.equal(
    wideGroup.getAttribute("data-arrangement-columns-effective"),
    null,
    "恢复宽屏后不再有生效列数覆盖",
  );

  const wideRoot = byPresentation(wide.root, "root")[0];
  assert.equal(wideRoot.getAttribute("data-narrow-active"), null);
  assert.equal(wideRoot.getAttribute("data-narrow-breakpoint"), null);

  const wideLeaves = byPresentation(wide.root, "leaf");
  assert.deepEqual(wideLeaves.map((leaf) => leaf.getAttribute("data-grid-column")), ["1", "2"]);
  assert.deepEqual(wideLeaves.map((leaf) => leaf.getAttribute("data-grid-row")), ["1", "1"]);
});

test("R05 显式窄屏配置优先于默认断点", () => {
  const configured = twoCardGroupDefinition();
  configured.page.breakpoints = [{ maxWidth: 900, columns: 1 }];

  // 配置生效：容器 800px 在配置断点（900px）以内 → 减列
  const viaDefinition = renderOnce(
    { definition: configured, previewPayload: { regions: [] } },
    { containerWidth: 800 },
  );
  const group = byPresentation(viaDefinition.root, "group")[0];
  assert.equal(group.style.gridTemplateColumns, "repeat(1, minmax(0, 1fr))");
  const rootEl = byPresentation(viaDefinition.root, "root")[0];
  assert.equal(rootEl.getAttribute("data-narrow-source"), "definition");
  assert.equal(rootEl.getAttribute("data-narrow-breakpoint"), "900");

  // 同一宽度、没有配置时：默认断点 480px 不覆盖 800px → 不减列
  const viaDefault = renderOnce(
    { definition: twoCardGroupDefinition(), previewPayload: { regions: [] } },
    { containerWidth: 800 },
  );
  const defaultGroup = byPresentation(viaDefault.root, "group")[0];
  assert.equal(defaultGroup.style.gridTemplateColumns, "repeat(2, minmax(0, 1fr))");
  assert.equal(
    byPresentation(viaDefault.root, "root")[0].getAttribute("data-narrow-active"),
    null,
  );

  // options.breakpoints（接线注入）优先于定义里的配置
  const viaOptions = renderOnce(
    { definition: configured, previewPayload: { regions: [] } },
    { containerWidth: 800, breakpoints: [{ maxWidth: 700, columns: 2 }] },
  );
  const optionsGroup = byPresentation(viaOptions.root, "group")[0];
  assert.equal(optionsGroup.style.gridTemplateColumns, "repeat(2, minmax(0, 1fr))");
  assert.equal(
    byPresentation(viaOptions.root, "root")[0].getAttribute("data-narrow-source"),
    "options",
  );
});

test("R05 非法定位值被忽略：不写定位属性，也不丢内容", () => {
  const definition = {
    page: { arrangement: { mode: "GRID", columns: 12, gap: { value: 12, unit: "PX" } } },
    regions: [
      {
        id: "g1",
        layout: { placement: { type: "WEIRD", column: 3 } },
        component: { type: "CONTAINER", arrangement: { mode: "GRID", columns: 2, gap: { value: 16, unit: "PX" } } },
        children: [{ id: "a1", component: { type: "METRIC_CARD" } }],
      },
      {
        id: "g2",
        layout: { placement: { type: "GRID", column: 99 } },
        component: { type: "CONTAINER", arrangement: { mode: "GRID", columns: 2, gap: { value: 16, unit: "PX" } } },
        children: [{ id: "b1", component: { type: "METRIC_CARD" } }],
      },
    ],
  };

  const { root } = renderOnce({ definition, previewPayload: { regions: [] } });
  const groups = byPresentation(root, "group");
  assert.equal(groups.length, 2, "组照常渲染，不丢内容");

  const weird = groups.find((group) => group.getAttribute("data-region-id") === "g1");
  assert.equal(weird.getAttribute("data-placement-type"), null, "非法定位类型不落到 DOM");
  assert.equal(weird.style.gridColumn, undefined);

  const outOfRange = groups.find((group) => group.getAttribute("data-region-id") === "g2");
  assert.equal(outOfRange.getAttribute("data-placement-type"), "GRID");
  assert.equal(outOfRange.getAttribute("data-grid-column"), "1", "越界列号回落到整行");
  assert.equal(outOfRange.style.gridColumn, "1 / -1");
});

test("card presentation reaches real leaf with sanitized precedence and preserves controls/data", () => {
  const doc = makeFakeDocument();
  const root = doc.createElement("div");
  const definition = twoCardGroupDefinition();
  definition.regions[0].style = { fontSize: { value: 24, unit: "PX" }, textAlign: "RIGHT", borderRadius: 18 };
  const child = definition.regions[0].children[0];
  child.styleRefs = [{ fontSize: { value: 26, unit: "PX" } }];
  child.style = { fontSize: { value: 28, unit: "PX" }, textAlign: "CENTER", borderRadius: 0 };
  child.component.style = { fontSize: { value: 30, unit: "PX" } };
  child.component.styleHints = [{ style: { textAlign: "url(javascript:bad)", borderRadius: "<bad>" } }];
  const previewPayload = { regions: [{ regionId: "a1", componentType: "METRIC_CARD", payload: { value: 1234.5, displayValue: "1,234.50 kWh" } }] };
  const before = JSON.stringify({ definition, previewPayload });
  const cards = [];
  const controls = [];
  let clicks = 0;
  P.render(root, { definition, previewPayload, resolvedTheme: { defaultStyle: { fontSize: { value: 12, unit: "PX" } } } }, {
    renderLeaf(region, payload) {
      if (region.id === "a1") assert.equal(payload.payload.displayValue, "1,234.50 kWh");
      const card = doc.createElement("div");
      card.setAttribute("class", "bg-white rounded-xl");
      const button = doc.createElement("button");
      button.setAttribute("class", "region-tools text-[11px] rounded");
      button.disabled = true;
      button.addEventListener("click", () => clicks++);
      card.appendChild(button);
      cards.push(card);
      controls.push(button);
      return card;
    },
  });
  assert.equal(cards[0].style["--wp-font-size"], "30px");
  assert.equal(cards[0].style["--wp-text-align"], "center");
  assert.equal(cards[0].style["--wp-border-radius"], "0px");
  assert.equal(cards[1].style["--wp-font-size"], "24px");
  assert.equal(cards[1].style["--wp-text-align"], "right");
  assert.equal(cards[1].style["--wp-border-radius"], "18px");
  assert.equal(controls[0].getAttribute("class"), "region-tools text-[11px] rounded");
  assert.equal(controls[0].disabled, true);
  assert.deepEqual(controls[0].style, {});
  controls[0].dispatchEvent({ type: "click" });
  assert.equal(clicks, 1, "existing listeners are not replaced");
  assert.equal(JSON.stringify({ definition, previewPayload }), before);
});

test("unstyled cards retain typography and rounding utilities without override markers", () => {
  const doc = makeFakeDocument();
  const root = doc.createElement("div");
  let card;
  P.render(root, { definition: FLAT_DEFINITION }, {
    renderLeaf() {
      card = doc.createElement("div");
      card.setAttribute("class", "text-xl rounded-xl");
      return card;
    },
  });
  assert.equal(card.getAttribute("class"), "text-xl rounded-xl wp-card");
  assert.equal(card.getAttribute("data-wp-font-size"), null);
  assert.equal(card.getAttribute("data-wp-text-align"), null);
  assert.equal(card.getAttribute("data-wp-border-radius"), null);
  assert.deepEqual(card.style, {});
});

test("vertical FLOW does not reserve wrapping columns; horizontal FLOW still wraps", () => {
  for (const direction of ["VERTICAL", "HORIZONTAL"]) {
    const { root } = renderOnce({ definition: { regions: [{
      id: "flow", component: { type: "CONTAINER", arrangement: { mode: "FLOW", direction } },
      children: [{ id: "child", component: { type: "TEXT" } }],
    }] } });
    assert.equal(byPresentation(root, "group")[0].style.flexWrap, direction === "VERTICAL" ? "nowrap" : "wrap");
  }
});
