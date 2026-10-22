// Agent E 专属：交互冒烟用的后端 mock。
// 只在 flows/pending-edit-plan-confirm.flow.json 里通过 <script> 注入，
// 不参与 workbench.html 的正常运行，也不进前端目录。
//
// 冻结的响应样例（方案 8.1 的契约）：
//   请求 context.visibleRegionIds = 客户可见正文卡片 ID 的有序列表
//   响应 domainObjects.pendingEditPlan = { planId, draftRevision, targetRegionId, operations, summary }
//   用户选择 = 消息正文「接受修改 / 继续调整 / 取消」
(function () {
  const DRAFT_REVISION_1 = 1;
  const DRAFT_REVISION_2 = 2;

  // 与后端 report_draft_service 的区域顺序一致：1 个隐藏标题区 + 5 个正文卡片
  function mkDraft(revision) {
    return {
      schemaVersion: "1.0",
      metadata: {
        id: "def_mock",
        name: "A 机房昨日能耗日报",
        revision: revision,
        status: "DRAFT",
      },
      datasets: {
        ds_room_energy: { id: "ds_room_energy", name: "机房能耗日汇总" },
        ds_room_energy_hourly: { id: "ds_room_energy_hourly", name: "机房能耗小时明细" },
      },
      regions: [
        {
          id: "region_report_title",
          name: "A 机房 昨日能耗日报",
          role: "REPORT_TITLE",
          component: { type: "TEXT", content: { template: "A 机房 昨日能耗日报" } },
        },
        {
          id: "region_total_energy",
          name: "昨日总耗电",
          role: "SUMMARY",
          component: {
            type: "METRIC_CARD",
            metric: { label: "昨日总耗电", fieldRef: "energyKwh", format: { kind: "NUMBER", precision: 1, unitLabel: "kWh" } },
          },
        },
        {
          id: "region_device_ranking",
          name: "设备耗电排名",
          role: "DETAIL",
          component: { type: "TABLE", columns: [{ label: "设备" }, { label: "耗电", format: { unitLabel: "kWh" } }] },
        },
        {
          id: "region_hourly_trend",
          name: "小时趋势",
          role: "TREND",
          data: { inputs: { main: { type: "DATASET", datasetRef: "ds_room_energy", periodRef: "CURRENT" } } },
          context: { timeWindow: { value: { unit: "DAY", offset: -1, count: 1 } } },
          component: { type: "CHART", chartKind: "LINE" },
        },
        {
          id: "region_data_quality",
          name: "数据质量",
          role: "QUALITY",
          component: { type: "DATA_QUALITY", fields: ["energyKwh", "deviceName"] },
        },
        {
          id: "region_analysis",
          name: "分析说明",
          role: "ANALYSIS",
          component: { type: "ANALYSIS", goal: "说明能耗结构", mode: "TEMPLATE", promptTemplateRef: "pt_analysis" },
        },
      ],
    };
  }

  // 目标 = 第 3 个可见正文卡片（region_hourly_trend），标题区不计入
  function mkPlan(planId, draftRevision) {
    return {
      planId: planId,
      draftRevision: draftRevision,
      targetRegionId: "region_hourly_trend",
      summary: "把「小时趋势」这张卡片从日粒度改成小时粒度，数据源换成机房能耗小时明细。",
      operations: [
        {
          summary: "小时趋势：日粒度改为小时粒度，数据源换成机房能耗小时明细",
          source: "ds_room_energy_hourly",
          grain: "HOUR",
          timeWindow: { value: { unit: "HOUR", offset: -24, count: 24 } },
        },
      ],
      unverified: "小时粒度下明细表是否已采集未验证。",
    };
  }

  const PLAN_QUESTION = {
    id: "pending_edit_plan_confirm",
    prompt: "确认这次修改：",
    expects: "SINGLE_CHOICE",
    selectionOnly: true,
    options: [
      { value: "接受修改", label: "接受修改" },
      { value: "继续调整", label: "继续调整" },
      { value: "取消", label: "取消" },
    ],
  };

  const responses = [
    // ① 第一次提修改要求：后端下发候选计划，草稿还没动
    {
      state: "USER_EDITING_TEMPLATE",
      reply: "我准备这样改，确认之后才会动草稿。",
      domainObjects: {
        reportDefinitionDraft: mkDraft(DRAFT_REVISION_1),
        pendingEditPlan: mkPlan("plan_001", DRAFT_REVISION_1),
      },
      questions: [PLAN_QUESTION],
    },
    // ② 用户选了「接受修改」：后端确定性应用后返回新草稿
    {
      state: "USER_EDITING_TEMPLATE",
      reply: "已按你的确认修改草稿。",
      domainObjects: { reportDefinitionDraft: mkDraft(DRAFT_REVISION_2) },
    },
    // ③ 再提一次修改要求：草稿已是 rev2，给出对应的新计划
    {
      state: "USER_EDITING_TEMPLATE",
      reply: "这次按当前草稿重算了一版。",
      domainObjects: {
        reportDefinitionDraft: mkDraft(DRAFT_REVISION_2),
        pendingEditPlan: mkPlan("plan_002", DRAFT_REVISION_2),
      },
      questions: [PLAN_QUESTION],
    },
    // ④ 用户选了「取消」：后端清掉待确认计划
    {
      state: "USER_EDITING_TEMPLATE",
      reply: "好的，这次不改，草稿保持原样。",
      domainObjects: { reportDefinitionDraft: mkDraft(DRAFT_REVISION_2) },
    },
    // ⑤ 再来一条：后端下发基于旧版本的计划（过期，不该能直接接受）
    {
      state: "USER_EDITING_TEMPLATE",
      reply: "这是按上一版草稿算出来的方案。",
      domainObjects: {
        reportDefinitionDraft: mkDraft(DRAFT_REVISION_2),
        pendingEditPlan: mkPlan("plan_003", DRAFT_REVISION_1),
      },
    },
  ];

  const calls = [];
  let index = 0;
  const realFetch = window.fetch.bind(window);

  window.fetch = async function (url, options) {
    const href = String(url);
    const method = String((options && options.method) || "GET").toUpperCase();
    calls.push({
      url: href,
      method: method,
      body: options && options.body ? JSON.parse(options.body) : null,
    });

    if (href.indexOf("/api/sessions") >= 0) {
      if (method === "POST") {
        return new Response(JSON.stringify({ sessionId: "sess_mock", state: "ANALYZING" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      return new Response(JSON.stringify({ items: [] }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }

    if (href.indexOf("/api/agent/chat/stream") >= 0) {
      const payload = responses[index] || { state: "USER_EDITING_TEMPLATE", reply: "（mock 没有更多响应了）" };
      index += 1;
      const sse = "event: final\ndata: " + JSON.stringify(payload) + "\n\n";
      return new Response(sse, {
        status: 200,
        headers: { "Content-Type": "text/event-stream" },
      });
    }

    return realFetch(url, options);
  };

  window.__mock = { calls: calls, responses: responses };
  window.__mockReady = true;
})();
