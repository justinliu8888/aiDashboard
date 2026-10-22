/**
 * 智能报表 AI 后端的 eval 套件。
 *
 * 打的是 FastAPI 后端的 POST /api/agent/chat（真实 HTTP 调用，不是 mock）。
 * 用例来自 backend/00_统筹.md 第 6 章的 6 个验收样例。
 *
 * 跑之前先起后端：
 *   cd ../backend && uvicorn app.main:app --port 8000
 *
 * 然后：
 *   npm run eval:run   跑一次
 *   npm run eval:dev   watch 模式，改了自动重跑
 */
import { createScorer, evalite } from "evalite";

const API_BASE = process.env.EVAL_API_BASE ?? "http://127.0.0.1:8000";
const CHAT_URL = `${API_BASE}/api/agent/chat`;

/** 协议里允许的 state，取自 backend/app/schemas/agent.py 的 AgentState。 */
const PROTOCOL_STATES = [
  "ANALYZING",
  "NEEDS_CLARIFICATION",
  "BLOCKED_OR_UNSUPPORTED",
  "INTERRUPT_CONFIRMING",
  "DEFINITION_DRAFT_READY",
  "WAITING_USER_CONFIRM",
  "USER_EDITING_TEMPLATE",
  "SAVE_DEFINITION_DRAFT",
  "VALIDATING",
  "VALIDATION_FAILED",
  "PREVIEWING",
  "PREVIEW_READY",
  "RENDERING",
  "READY",
] as const;

type AgentRequest = {
  agentProtocolVersion?: string;
  conversationId: string;
  state: string;
  message: string;
  context: Record<string, unknown>;
  designSessionId?: string;
  reportDefinitionId?: string;
  baseDefinitionRevision?: number;
  selectedRegionId?: string;
};

type AgentResponse = {
  agentProtocolVersion?: string;
  conversationId?: string;
  state?: string;
  reply?: string;
  domainObjects?: {
    reportPolicyDraft?: unknown;
    reportDefinitionDraft?: unknown;
    changeSet?: { changed?: { path?: string; regionId?: string; after?: unknown }[] };
  };
  questions?: { id?: string }[];
  blocks?: { code?: string; alternatives?: string[] }[];
  toolRequests?: { type?: string }[];
  uiActions?: string[];
  error?: { code?: string; message?: string };
};

/** 一个用例：请求 + 期望。 */
type Case = {
  /** 用例名，显示在 Evalite UI 里。 */
  name: string;
  request: AgentRequest;
  /** 期望的状态机落点。 */
  expectState: string;
  /** 期望出现的拦截码。 */
  expectBlock?: string;
  /** 期望出现在 changeSet 里的变更路径。 */
  expectChangePath?: string;
  /** 期望被删除的卡片 regionId。 */
  expectDeletedRegion?: string;
};

const CONTEXT = {
  projectId: "p1",
  subjectRef: "machine_room",
  subjectId: "room_a",
};

const CASES: Case[] = [
  {
    name: "样例1 正常生成",
    request: {
      conversationId: "eval_1",
      state: "ANALYZING",
      message: "生成 A 机房昨日能耗日报，包含总耗电、设备排名和趋势分析",
      context: CONTEXT,
    },
    expectState: "WAITING_USER_CONFIRM",
  },
  {
    name: "样例2 信息不足",
    request: {
      conversationId: "eval_2",
      state: "ANALYZING",
      message: "帮我看异常设备",
      context: CONTEXT,
    },
    expectState: "NEEDS_CLARIFICATION",
  },
  {
    name: "样例3 权限越界",
    request: {
      conversationId: "eval_3",
      state: "ANALYZING",
      message: "帮我看所有项目的能耗排名",
      context: CONTEXT,
    },
    expectState: "BLOCKED_OR_UNSUPPORTED",
    expectBlock: "PERMISSION_DENIED",
  },
  {
    name: "样例4 分钟级不支持",
    request: {
      conversationId: "eval_4",
      state: "ANALYZING",
      message: "帮我看 A 机房分钟级历史能耗",
      context: CONTEXT,
    },
    expectState: "BLOCKED_OR_UNSUPPORTED",
    expectBlock: "CAPABILITY_NOT_SUPPORTED",
  },
  {
    name: "样例5 改 7 天趋势",
    request: {
      conversationId: "eval_5",
      state: "WAITING_USER_CONFIRM",
      message: "把趋势图改成最近 7 天",
      context: CONTEXT,
      designSessionId: "s1",
      reportDefinitionId: "d1",
      baseDefinitionRevision: 3,
      selectedRegionId: "region_hourly_trend",
    },
    expectState: "DEFINITION_DRAFT_READY",
    expectChangePath: "/regions/region_hourly_trend/context/timeWindow",
  },
  {
    name: "样例6 删趋势图",
    request: {
      conversationId: "eval_6",
      state: "WAITING_USER_CONFIRM",
      message: "不要趋势图，改成只要总耗电和设备排名",
      context: CONTEXT,
      designSessionId: "s1",
      reportDefinitionId: "d1",
      baseDefinitionRevision: 5,
    },
    expectState: "DEFINITION_DRAFT_READY",
    expectDeletedRegion: "region_hourly_trend",
  },
];

async function chat(c: Case): Promise<AgentResponse> {
  const res = await fetch(CHAT_URL, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ agentProtocolVersion: "1.0", ...c.request }),
  });
  if (!res.ok) {
    throw new Error(`POST ${CHAT_URL} 返回 ${res.status} ${res.statusText}`);
  }
  return (await res.json()) as AgentResponse;
}

/** 状态落点正确：这是接口契约的底线。 */
const stateMatches = createScorer<Case, AgentResponse>({
  name: "状态落点正确",
  description: "响应的 state 等于用例期望的状态机落点。",
  scorer: ({ input, output }) => ({
    score: output.state === input.expectState ? 1 : 0,
    metadata: { expected: input.expectState, actual: output.state ?? null },
  }),
});

/** 拦截场景要给出对应的拦截码，并给出可执行的替代方案。 */
const blockCodeMatches = createScorer<Case, AgentResponse>({
  name: "拦截码正确",
  description: "需要拦截时，blocks 里必须有约定的 code，且给出 alternatives。",
  scorer: ({ input, output }) => {
    if (!input.expectBlock) return 1;
    const codes = (output.blocks ?? []).map((b) => b.code);
    const hit = codes.includes(input.expectBlock);
    const withAlternatives = hit
      ? (output.blocks ?? []).some(
          (b) => b.code === input.expectBlock && b.alternatives && b.alternatives.length > 0,
        )
      : false;
    return {
      score: hit && withAlternatives ? 1 : 0,
      metadata: { expected: input.expectBlock, actual: codes, hasAlternatives: withAlternatives },
    };
  },
});

/** 修改场景要真的产出 changeSet，且改到正确的目标。 */
const changeSetMatches = createScorer<Case, AgentResponse>({
  name: "变更集命中目标",
  description: "需要修改时，changeSet 里改到的路径或删除的卡片符合预期。",
  scorer: ({ input, output }) => {
    const changed = output.domainObjects?.changeSet?.changed ?? [];
    if (input.expectChangePath) {
      const paths = changed.map((c) => c.path);
      return {
        score: paths.includes(input.expectChangePath) ? 1 : 0,
        metadata: { expected: input.expectChangePath, actual: paths },
      };
    }
    if (input.expectDeletedRegion) {
      const deleted = changed.filter((c) => c.after === undefined).map((c) => c.regionId);
      return {
        score: deleted.includes(input.expectDeletedRegion) ? 1 : 0,
        metadata: { expected: input.expectDeletedRegion, actual: deleted },
      };
    }
    return 1;
  },
});

/** 协议形状：前端和 Java 后端依赖这些字段，缺一个就崩。 */
const protocolShapeValid = createScorer<Case, AgentResponse>({
  name: "协议字段完整",
  description: "响应包含前端依赖的全部字段，且 state 在协议枚举内。",
  scorer: ({ output }) => {
    const problems: string[] = [];
    if (typeof output.reply !== "string" || output.reply.length === 0) {
      problems.push("reply 缺失或为空");
    }
    if (!output.state || !PROTOCOL_STATES.includes(output.state as never)) {
      problems.push(`state 非法: ${output.state}`);
    }
    if (!Array.isArray(output.uiActions)) problems.push("uiActions 不是数组");
    if (!Array.isArray(output.questions)) problems.push("questions 不是数组");
    if (!Array.isArray(output.blocks)) problems.push("blocks 不是数组");
    if (!Array.isArray(output.toolRequests)) problems.push("toolRequests 不是数组");
    if (!output.domainObjects) problems.push("domainObjects 缺失");
    return { score: problems.length === 0 ? 1 : 0, metadata: { problems } };
  },
});

await evalite("智能报表 AI 后端 · 接口契约", {
  data: CASES.map((c) => ({ input: c })),
  task: chat,
  scorers: [stateMatches, blockCodeMatches, changeSetMatches, protocolShapeValid],
});
