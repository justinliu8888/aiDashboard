/**
 * 极简 DOM 替身（测试用）
 *
 * Node 内置 test runner 没有 DOM。本文件只实现「网格页面模块」用到的浏览器平台能力：
 * 元素树、属性 / classList / style.setProperty、事件（冒泡）、querySelector 家族、
 * getBoundingClientRect、ResizeObserver。
 *
 * 这是对**浏览器平台（系统边界）**的替身，不是对本模块内部实现的 mock：
 * 测试只通过 mountReportGrid 的公开接口观察行为。
 */

const NODE_ELEMENT = 1;
const NODE_TEXT = 3;

/* --------------------------------------------------------------- 选择器 */

function parseCompound(part) {
  const spec = { tag: null, classes: [], id: null, attrs: [] };
  let rest = part.trim();
  const tagMatch = /^[a-zA-Z][\w-]*/.exec(rest);
  if (tagMatch) {
    spec.tag = tagMatch[0].toUpperCase();
    rest = rest.slice(tagMatch[0].length);
  }
  const tokens = rest.match(/[.#][\w-]+|\[[^\]]+\]/g) || [];
  for (const token of tokens) {
    if (token.startsWith('.')) spec.classes.push(token.slice(1));
    else if (token.startsWith('#')) spec.id = token.slice(1);
    else if (token.startsWith('[')) {
      const m = /^\[([\w-]+)(?:[~|^$*]?=)?["']?([^"'\]]*)["']?\]$/.exec(token);
      if (m) spec.attrs.push({ name: m[1], value: m[2] });
    }
  }
  return spec;
}

function parseSelector(selector) {
  return String(selector)
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean)
    .map(parseCompound);
}

function matchesSpec(el, spec) {
  if (el.nodeType !== NODE_ELEMENT) return false;
  if (spec.tag && el.tagName !== spec.tag) return false;
  if (spec.id && el.getAttribute('id') !== spec.id) return false;
  for (const cls of spec.classes) {
    if (!el.classList.contains(cls)) return false;
  }
  for (const attr of spec.attrs) {
    const value = el.getAttribute(attr.name);
    if (value === null) return false;
    if (attr.value !== undefined && attr.value !== '' && value !== attr.value) return false;
  }
  return true;
}

/* -------------------------------------------------------------- 事件目标 */

class FakeEventTarget {
  constructor() {
    this._listeners = new Map();
  }

  addEventListener(type, fn) {
    if (typeof fn !== 'function') return;
    if (!this._listeners.has(type)) this._listeners.set(type, []);
    const list = this._listeners.get(type);
    if (!list.includes(fn)) list.push(fn);
  }

  removeEventListener(type, fn) {
    const list = this._listeners.get(type);
    if (!list) return;
    const i = list.indexOf(fn);
    if (i >= 0) list.splice(i, 1);
  }

  /** 冒泡派发：target 默认取派发元素，逐级向上冒泡。 */
  dispatchEvent(event) {
    if (!event.target) event.target = this;
    let node = this;
    while (node) {
      event.currentTarget = node;
      const list = (node._listeners && node._listeners.get(event.type)) || [];
      for (const fn of list.slice()) {
        fn.call(node, event);
        if (event._immediateStopped) return !event.defaultPrevented;
      }
      if (event._stopped) break;
      node = node.parentNode || null;
    }
    return !event.defaultPrevented;
  }
}

/* ------------------------------------------------------------------ 样式 */

class FakeStyle {
  constructor() {
    this._props = new Map();
  }

  setProperty(name, value) {
    this._props.set(String(name), String(value));
  }

  getPropertyValue(name) {
    const v = this._props.get(String(name));
    return v === undefined ? '' : v;
  }

  removeProperty(name) {
    this._props.delete(String(name));
  }

  get cssText() {
    return [...this._props].map(([k, v]) => `${k}: ${v};`).join(' ');
  }
}

class FakeClassList {
  constructor(el) {
    this.el = el;
  }

  get _set() {
    return new Set(String(this.el.className || '').split(/\s+/).filter(Boolean));
  }

  _write(set) {
    this.el.className = [...set].join(' ');
  }

  add(...names) {
    const set = this._set;
    for (const n of names) set.add(n);
    this._write(set);
  }

  remove(...names) {
    const set = this._set;
    for (const n of names) set.delete(n);
    this._write(set);
  }

  contains(name) {
    return this._set.has(name);
  }

  toggle(name, force) {
    const set = this._set;
    if (force === undefined) {
      if (set.has(name)) set.delete(name);
      else set.add(name);
    } else if (force) set.add(name);
    else set.delete(name);
    this._write(set);
    return set.has(name);
  }
}

/* ------------------------------------------------------------------ 节点 */

class FakeText {
  constructor(text, ownerDocument) {
    this.nodeType = NODE_TEXT;
    this.ownerDocument = ownerDocument;
    this.parentNode = null;
    this.data = String(text);
  }

  get textContent() {
    return this.data;
  }

  set textContent(value) {
    this.data = String(value);
  }
}

class FakeElement extends FakeEventTarget {
  constructor(tagName, ownerDocument) {
    super();
    this.tagName = String(tagName).toUpperCase();
    this.nodeType = NODE_ELEMENT;
    this.ownerDocument = ownerDocument;
    this.parentNode = null;
    this.childNodes = [];
    this.className = '';
    this.style = new FakeStyle();
    this._attrs = new Map();
    this._html = null;
    this._captures = new Set();
    this._rect = { x: 0, y: 0, left: 0, top: 0, width: 0, height: 0, right: 0, bottom: 0 };
  }

  get classList() {
    return new FakeClassList(this);
  }

  get dataset() {
    const el = this;
    const attrOf = (prop) => 'data-' + String(prop).replace(/[A-Z]/g, (m) => '-' + m.toLowerCase());
    return new Proxy(
      {},
      {
        get(_t, prop) {
          if (typeof prop !== 'string') return undefined;
          const v = el.getAttribute(attrOf(prop));
          return v === null ? undefined : v;
        },
        set(_t, prop, value) {
          el.setAttribute(attrOf(prop), value);
          return true;
        },
        has(_t, prop) {
          return el.hasAttribute(attrOf(prop));
        },
      },
    );
  }

  get children() {
    return this.childNodes.filter((n) => n.nodeType === NODE_ELEMENT);
  }

  get parentElement() {
    return this.parentNode && this.parentNode.nodeType === NODE_ELEMENT ? this.parentNode : null;
  }

  get firstChild() {
    return this.childNodes[0] || null;
  }

  /* 属性 */
  setAttribute(name, value) {
    this._attrs.set(String(name), String(value));
  }

  getAttribute(name) {
    const v = this._attrs.get(String(name));
    return v === undefined ? null : v;
  }

  hasAttribute(name) {
    return this._attrs.has(String(name));
  }

  removeAttribute(name) {
    this._attrs.delete(String(name));
  }

  /* 树 */
  appendChild(child) {
    if (!child) throw new TypeError('appendChild 需要一个节点');
    if (child.parentNode) child.parentNode.removeChild(child);
    this.childNodes.push(child);
    child.parentNode = this;
    return child;
  }

  insertBefore(child, ref) {
    if (!ref) return this.appendChild(child);
    const i = this.childNodes.indexOf(ref);
    if (i < 0) return this.appendChild(child);
    if (child.parentNode) child.parentNode.removeChild(child);
    this.childNodes.splice(i, 0, child);
    child.parentNode = this;
    return child;
  }

  removeChild(child) {
    const i = this.childNodes.indexOf(child);
    if (i >= 0) this.childNodes.splice(i, 1);
    child.parentNode = null;
    return child;
  }

  replaceChildren(...nodes) {
    for (const child of this.childNodes.slice()) this.removeChild(child);
    for (const node of nodes) this.appendChild(node);
  }

  remove() {
    if (this.parentNode) this.parentNode.removeChild(this);
  }

  /* 文本 */
  get textContent() {
    return this.childNodes.map((n) => n.textContent || '').join('');
  }

  set textContent(value) {
    this.replaceChildren();
    if (value !== '' && value != null) {
      this.appendChild(new FakeText(value, this.ownerDocument));
    }
  }

  get innerHTML() {
    return this._html === null ? '' : this._html;
  }

  set innerHTML(html) {
    this._html = String(html);
    this.replaceChildren();
    const text = this._html
      .replace(/<script[\s\S]*?<\/script>/gi, '')
      .replace(/<style[\s\S]*?<\/style>/gi, '')
      .replace(/<[^>]*>/g, '')
      .replace(/&nbsp;/g, ' ')
      .trim();
    if (text) this.appendChild(new FakeText(text, this.ownerDocument));
  }

  /* 选择器 */
  matches(selector) {
    return parseSelector(selector).some((spec) => matchesSpec(this, spec));
  }

  closest(selector) {
    let node = this;
    while (node && node.nodeType === NODE_ELEMENT) {
      if (node.matches(selector)) return node;
      node = node.parentNode;
    }
    return null;
  }

  querySelectorAll(selector) {
    const specs = parseSelector(selector);
    const out = [];
    const walk = (node) => {
      for (const child of node.childNodes) {
        if (child.nodeType !== NODE_ELEMENT) continue;
        if (specs.some((spec) => matchesSpec(child, spec))) out.push(child);
        walk(child);
      }
    };
    walk(this);
    return out;
  }

  querySelector(selector) {
    const all = this.querySelectorAll(selector);
    return all.length ? all[0] : null;
  }

  /* 几何 */
  getBoundingClientRect() {
    const r = this._rect;
    return { ...r, right: r.left + r.width, bottom: r.top + r.height };
  }

  setPointerCapture(pointerId) {
    this._captures.add(pointerId);
  }

  releasePointerCapture(pointerId) {
    this._captures.delete(pointerId);
  }

  focus() {}
  blur() {}
  scrollIntoView() {}
}

/* ------------------------------------------------------------ 观察器 */

class FakeResizeObserver {
  constructor(callback) {
    this.callback = callback;
    this.observed = new Set();
    FakeResizeObserver.instances.push(this);
  }

  observe(el) {
    this.observed.add(el);
  }

  unobserve(el) {
    this.observed.delete(el);
  }

  disconnect() {
    this.observed.clear();
    const i = FakeResizeObserver.instances.indexOf(this);
    if (i >= 0) FakeResizeObserver.instances.splice(i, 1);
  }
}
FakeResizeObserver.instances = [];

/* ---------------------------------------------------------------- 文档 */

class FakeDocument extends FakeEventTarget {
  constructor() {
    super();
    this.nodeType = 9;
    this.defaultView = {
      ResizeObserver: FakeResizeObserver,
      innerWidth: 1440,
      innerHeight: 900,
      addEventListener() {},
      removeEventListener() {},
    };
    this.head = new FakeElement('head', this);
    this.body = new FakeElement('body', this);
  }

  createElement(tagName) {
    return new FakeElement(tagName, this);
  }

  createTextNode(text) {
    return new FakeText(text, this);
  }

  querySelector(selector) {
    if (selector === 'head') return this.head;
    if (selector === 'body') return this.body;
    return this.body.querySelector(selector);
  }

  querySelectorAll(selector) {
    return this.body.querySelectorAll(selector);
  }
}

/* ---------------------------------------------------------------- 工厂 */

/** 造一个「页面」：{ document, root, triggerResize() } */
export function createDom() {
  const document = new FakeDocument();
  const root = document.createElement('div');
  document.body.appendChild(root);
  return {
    document,
    root,
    /** 触发 ResizeObserver 回调（模拟窗口变窄） */
    triggerResize() {
      for (const obs of FakeResizeObserver.instances.slice()) {
        obs.callback([{ target: [...obs.observed][0] }], obs);
      }
    },
    setGridWidth(width) {
      const grid = root.querySelector('.rg-grid');
      if (!grid) return;
      grid._rect = { x: 0, y: 0, left: 0, top: 0, width, height: 600, right: width, bottom: 600 };
    },
  };
}

/** 造一个事件对象 */
export function makeEvent(type, props = {}) {
  return {
    type,
    bubbles: true,
    cancelable: true,
    defaultPrevented: false,
    target: null,
    currentTarget: null,
    _stopped: false,
    _immediateStopped: false,
    preventDefault() {
      this.defaultPrevented = true;
    },
    stopPropagation() {
      this._stopped = true;
    },
    stopImmediatePropagation() {
      this._stopped = true;
      this._immediateStopped = true;
    },
    ...props,
  };
}

/** 造一个 pointer 事件 */
export function makePointerEvent(type, { x = 0, y = 0, pointerId = 1, button = 0, buttons = 1 } = {}) {
  return makeEvent(type, { pointerId, clientX: x, clientY: y, button, buttons });
}

export { FakeElement, FakeDocument, FakeResizeObserver };
