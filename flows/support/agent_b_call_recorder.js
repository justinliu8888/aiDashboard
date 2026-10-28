// Agent B 专属：交互流用的请求记录器。
//
// 只在 flows/agent-b-readonly-query.flow.json 里通过 <script> 注入，
// 不参与 workbench.html 的正常运行，也不进前端目录。
//
// 用途：把页面发出的每一次 fetch 记下来，用来断言
//   - 只读问答这一轮**没有**发出 __advance__（自动推进次数不增加）
//   - 问完之后，原来的确认操作仍然能发出 __advance__（原操作保持可用）
(function () {
  if (window.__callRecorder) return;
  const calls = [];
  const realFetch = window.fetch.bind(window);
  window.fetch = function (url, options) {
    const href = String(url);
    calls.push({
      url: href,
      method: String((options && options.method) || "GET").toUpperCase(),
      body: options && options.body ? options.body : null,
    });
    return realFetch(url, options);
  };
  window.__callRecorder = { calls: calls };
  window.__callRecorderReady = true;
})();
