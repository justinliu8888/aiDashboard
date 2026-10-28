// G 独立网格模块 · 布局层测试公用夹具。
//
// 运行：node --test frontend/modules/report-grid/tests/layout/*.test.mjs
// （目录参数在 Node 24 上不匹配默认测试文件规则，要显式给 glob；
//   跑单个文件 node --test frontend/modules/report-grid/tests/layout/intent.test.mjs 也可以）
//
// 本文件只是夹具，不含测试用例，因此不命名为 *.test.mjs，测试运行器不会把它当测试文件收集。
//
// 只用 Node 内置能力，不依赖任何第三方包，也不改动共用依赖配置。
import assert from 'node:assert/strict';

/** 构造一个 GRID placement。 */
export function grid(column, row, columnSpan = 1, rowSpan = 1) {
  return { type: 'GRID', column, row, columnSpan, rowSpan };
}

/** 构造一个区域 item。 */
export function item(regionId, placement = grid(1, 1), extra = {}) {
  return { regionId, placement, ...extra };
}

/** 从 resolveGridLayout 的成功结果里取某个区域的 placement。 */
export function placementOf(result, regionId) {
  const found = result.placements.find((p) => p.regionId === regionId);
  assert.ok(found, `结果里没有 ${regionId}`);
  return found.placement;
}
