import assert from "node:assert/strict";
import { mkdir, writeFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import {
  CDP, launchChrome, closeChrome, collectEvents, evaluate, realClick, captureScreenshot,
} from "../../../../.trae/skills/_shared/cdp.mjs";

const base = process.env.CHECK_URL || "http://127.0.0.1:8960";
const out = fileURLToPath(new URL("../../.selfcheck/template-grid-hotfix/drag/", import.meta.url));
await mkdir(out, { recursive: true });
const list = await (await fetch(base + "/api/sessions?q=" + encodeURIComponent("四个板块"))).json();
let detail;
for (const entry of list.items) {
  const candidate = await (await fetch(base + "/api/sessions/" + entry.sessionId)).json();
  if (candidate.snapshot?.templateDesign?.regions.filter((region) => region.dataStatus === "bound").length === 3) {
    detail = candidate;
    break;
  }
}
assert.ok(detail, "Run template-grid-workbench.flow.json first to create the smoke-test session");
const original = detail.snapshot.templateDesign;
const chrome = await launchChrome();
const cdp = await new CDP().connect(chrome.wsUrl);
let sessionId;
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
async function wait(expression) {
  for (let attempt = 0; attempt < 50; attempt++) {
    if (await evaluate(cdp, sessionId, expression)) return;
    await sleep(200);
  }
  throw new Error("Timed out: " + expression);
}
try {
  const { targetId } = await cdp.send("Target.createTarget", { url: "about:blank" });
  ({ sessionId } = await cdp.send("Target.attachToTarget", { targetId, flatten: true }));
  for (const domain of ["Page", "Runtime", "Network", "Log"]) await cdp.send(domain + ".enable", {}, sessionId);
  await cdp.send("Emulation.setDeviceMetricsOverride", { width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false }, sessionId);
  const events = collectEvents(cdp, sessionId);
  await cdp.send("Page.navigate", { url: base + "/playground/pages/workbench.html" }, sessionId);
  const sessionSelector = `[data-session-id="${detail.sessionId}"]`;
  await wait(`document.querySelector(${JSON.stringify(sessionSelector)}) !== null`);
  await realClick(cdp, sessionId, sessionSelector);
  await wait("document.querySelector('.template-grid .rg-region') !== null");
  await sleep(300);
  const regionId = original.regions[0].id;
  const handle = `.template-grid [data-region-id="${regionId}"] [data-rg-handle="e"]`;
  const start = await evaluate(cdp, sessionId, `(() => {
    const node = document.querySelector(${JSON.stringify(handle)});
    node.scrollIntoView({ block: 'center' });
    const box = node.getBoundingClientRect();
    const root = document.querySelector('.template-grid');
    const grid = root.querySelector('.rg-grid');
    const gap = parseFloat(getComputedStyle(grid).columnGap);
    return { x: box.x + box.width / 2, y: box.y + box.height / 2,
      pitch: (grid.getBoundingClientRect().width + gap) / 12 };
  })()`);
  assert.ok(start.pitch > 0);
  const mouse = (type, x, buttons) => cdp.send("Input.dispatchMouseEvent", {
    type, x, y: start.y, button: "left", buttons, clickCount: 1,
  }, sessionId);
  await mouse("mousePressed", start.x, 1);
  for (let step = 1; step <= 6; step++) {
    await mouse("mouseMoved", start.x - start.pitch * step / 6, 1);
    await sleep(40);
  }
  await mouse("mouseReleased", start.x - start.pitch, 0);
  await wait("document.querySelector('.template-grid-status').textContent.includes('布局已保存')");
  const saved = await (await fetch(base + "/api/sessions/" + detail.sessionId)).json();
  const template = saved.snapshot.templateDesign;
  assert.equal(template.revision, original.revision + 1);
  assert.deepEqual(template.regions, original.regions);
  const placement = template.layout.items.find((item) => item.regionId === regionId).placement;
  const oldSpan = original.layout.items.find((item) => item.regionId === regionId)?.placement?.columnSpan || 6;
  assert.equal(placement.columnSpan, oldSpan - 1);
  await cdp.send("Page.reload", {}, sessionId);
  await wait("document.querySelector('.template-grid .rg-region') !== null");
  const restoredSpan = await evaluate(cdp, sessionId,
    `document.querySelector('.template-grid [data-region-id="${regionId}"]').style.getPropertyValue('--rg-column-span')`);
  assert.equal(Number(restoredSpan), placement.columnSpan);
  assert.equal(events.errors.length, 0, JSON.stringify(events.errors));
  await writeFile(out + "saved-and-restored.png", Buffer.from(await captureScreenshot(cdp, sessionId), "base64"));
  console.log("PASS: real mouse resize -> saved revision -> refreshed layout; region data unchanged; 0 browser errors");
} catch (error) {
  if (sessionId) await writeFile(out + "failure.png", Buffer.from(await captureScreenshot(cdp, sessionId), "base64"));
  throw error;
} finally {
  cdp.close();
  await closeChrome(chrome);
}
