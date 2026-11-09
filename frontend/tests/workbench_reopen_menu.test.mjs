import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const source = readFileSync(new URL("../pages/workbench.js", import.meta.url), "utf8");
const start = source.indexOf("function pendingMenuCardShown(");
const end = source.indexOf("\n}\n", start) + 2;
assert.ok(start >= 0 && end > start);

function shown(question, entries) {
  const context = vm.createContext({
    document: { querySelectorAll: () => entries.map(({ id, step }) => ({
      getAttribute: () => id,
      closest: () => step ? { getAttribute: () => step } : null,
    })) },
  });
  vm.runInContext(source.slice(start, end), context);
  return context.pendingMenuCardShown(question);
}

test("重开时同名旧菜单不能代替缺失的当前步骤菜单", () => {
  assert.equal(shown({ id: "template_edit_confirm", stepId: "current" }, [
    { id: "template_edit_confirm", step: "old" },
  ]), false);
});

test("同一步菜单已回放时不重复补画", () => {
  assert.equal(shown({ id: "template_edit_confirm", stepId: "current" }, [
    { id: "template_edit_confirm", step: "current" },
  ]), true);
});

test("有步骤身份时无身份的历史菜单也不能冒充当前入口", () => {
  assert.equal(shown({ id: "template_edit_confirm", stepId: "current" }, [
    { id: "template_edit_confirm" },
  ]), false);
});

test("兼容没有步骤身份的旧会话", () => {
  assert.equal(shown({ id: "template_edit_confirm" }, [
    { id: "template_edit_confirm" },
  ]), true);
});
