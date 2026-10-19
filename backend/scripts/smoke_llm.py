"""真大脑冒烟测试：直接调 AgentScopeReportAgent，验证样例 1 能否跑通。

使用：
    cd backend
    python3 scripts/smoke_llm.py

依赖：backend/.env 里配好 VOLC_API_KEY 和 VOLC_MODEL。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agents.agentscope_report_agent import AgentScopeReportAgent  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.schemas.agent import (  # noqa: E402
    AgentRequest,
    AgentState,
    AGENT_PROTOCOL_VERSION,
)
from app.schemas.agent import AgentRequestContext  # noqa: E402


CASES = [
    (
        "样例 1：生成能耗日报",
        "帮我生成 A 机房昨日的能耗日报，我想看总耗电、设备排名和小时趋势。",
        AgentState.WAITING_USER_CONFIRM,
    ),
    (
        "样例 3：所有项目（预期 FALLBACK → FakeAgent → BLOCKED）",
        "帮我看一下所有项目的能耗排名",
        AgentState.BLOCKED_OR_UNSUPPORTED,
    ),
    (
        "样例 4：分钟级（预期 FALLBACK → FakeAgent → BLOCKED）",
        "我想看 A 机房分钟级能耗",
        AgentState.BLOCKED_OR_UNSUPPORTED,
    ),
]


def main() -> int:
    settings = get_settings()
    print(f"==> AGENT_IMPL = {settings.agent_impl}")
    print(f"==> VOLC_MODEL = {settings.volc_model}")
    print(f"==> VOLC_BASE_URL = {settings.volc_base_url}")
    print(f"==> api_key = {'<set>' if settings.volc_api_key else '<empty>'}\n")

    agent = AgentScopeReportAgent()

    failed: list[str] = []
    for name, msg, expected_state in CASES:
        print(f"--- {name} ---")
        print(f"user: {msg}")
        req = AgentRequest(
            agentProtocolVersion=AGENT_PROTOCOL_VERSION,
            conversationId="smoke_" + name[:4],
            state=AgentState.ANALYZING,
            message=msg,
            context=AgentRequestContext(
                projectId="project_001",
                subjectRef="machine_room",
                subjectId="room_a",
            ),
        )
        t0 = time.time()
        try:
            resp = agent.handle(req)
        except Exception as e:  # noqa: BLE001
            print(f"  [EXCEPTION] {e}")
            failed.append(name)
            continue
        dt = (time.time() - t0) * 1000
        ok = resp.state == expected_state
        tag = "PASS" if ok else "FAIL"
        print(
            f"  [{tag}] state={resp.state.value} "
            f"expected={expected_state.value} dt={dt:.0f}ms"
        )
        print(f"  reply: {resp.reply[:120]}")
        if resp.domainObjects and resp.domainObjects.reportDefinitionDraft:
            n = len(resp.domainObjects.reportDefinitionDraft.regions)
            print(f"  regions: {n} 个")
        if resp.blocks:
            print(f"  blocks: {[b.code for b in resp.blocks]}")
        if not ok:
            failed.append(name)
        print()

    if failed:
        print(f"FAILED: {len(failed)} / {len(CASES)}")
        for n in failed:
            print(f"  - {n}")
        return 1
    print(f"ALL PASS ({len(CASES)}/{len(CASES)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
