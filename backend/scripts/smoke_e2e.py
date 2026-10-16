#!/usr/bin/env python3
"""端到端冒烟：依次打 FakeAgent 6 个样例，打印关键字段。"""
from __future__ import annotations

import json
import urllib.request

API = "http://127.0.0.1:8000/api/agent/chat"

SAMPLES = [
    {
        "name": "样例1 正常生成",
        "body": {
            "agentProtocolVersion": "1.0",
            "conversationId": "smoke_1",
            "state": "ANALYZING",
            "message": "生成 A 机房昨日能耗日报，包含总耗电、设备排名和趋势分析",
            "context": {"projectId": "p1", "subjectRef": "machine_room", "subjectId": "room_a"},
        },
        "expect_state": "WAITING_USER_CONFIRM",
    },
    {
        "name": "样例2 信息不足",
        "body": {
            "agentProtocolVersion": "1.0",
            "conversationId": "smoke_2",
            "state": "ANALYZING",
            "message": "帮我看异常设备",
            "context": {"projectId": "p1", "subjectRef": "machine_room", "subjectId": "room_a"},
        },
        "expect_state": "NEEDS_CLARIFICATION",
    },
    {
        "name": "样例3 权限越界",
        "body": {
            "agentProtocolVersion": "1.0",
            "conversationId": "smoke_3",
            "state": "ANALYZING",
            "message": "帮我看所有项目的能耗排名",
            "context": {"projectId": "p1", "subjectRef": "machine_room", "subjectId": "room_a"},
        },
        "expect_state": "BLOCKED_OR_UNSUPPORTED",
        "expect_block": "PERMISSION_DENIED",
    },
    {
        "name": "样例4 分钟级",
        "body": {
            "agentProtocolVersion": "1.0",
            "conversationId": "smoke_4",
            "state": "ANALYZING",
            "message": "帮我看 A 机房分钟级历史能耗",
            "context": {"projectId": "p1", "subjectRef": "machine_room", "subjectId": "room_a"},
        },
        "expect_state": "BLOCKED_OR_UNSUPPORTED",
        "expect_block": "CAPABILITY_NOT_SUPPORTED",
    },
    {
        "name": "样例5 改 7 天",
        "body": {
            "agentProtocolVersion": "1.0",
            "conversationId": "smoke_5",
            "state": "WAITING_USER_CONFIRM",
            "message": "把趋势图改成最近 7 天",
            "context": {"projectId": "p1", "subjectRef": "machine_room", "subjectId": "room_a"},
            "designSessionId": "s1",
            "reportDefinitionId": "d1",
            "baseDefinitionRevision": 3,
            "selectedRegionId": "region_hourly_trend",
        },
        "expect_state": "DEFINITION_DRAFT_READY",
        "expect_path": "/regions/region_hourly_trend/context/timeWindow",
    },
    {
        "name": "样例6 删趋势图",
        "body": {
            "agentProtocolVersion": "1.0",
            "conversationId": "smoke_6",
            "state": "WAITING_USER_CONFIRM",
            "message": "不要趋势图，改成只要总耗电和设备排名",
            "context": {"projectId": "p1", "subjectRef": "machine_room", "subjectId": "room_a"},
            "designSessionId": "s1",
            "reportDefinitionId": "d1",
            "baseDefinitionRevision": 5,
        },
        "expect_state": "DEFINITION_DRAFT_READY",
        "expect_delete_region": "region_hourly_trend",
    },
]


def post(body: dict) -> dict:
    req = urllib.request.Request(
        API,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    passed, failed = 0, 0
    for s in SAMPLES:
        data = post(s["body"])
        ok = data["state"] == s["expect_state"]
        detail = f"state={data['state']}"

        if "expect_block" in s:
            codes = [b["code"] for b in data.get("blocks", [])]
            ok = ok and s["expect_block"] in codes
            detail += f" blocks={codes}"

        if "expect_path" in s:
            paths = [c["path"] for c in (data["domainObjects"].get("changeSet") or {}).get("changed", [])]
            ok = ok and s["expect_path"] in paths
            detail += f" paths={paths}"

        if "expect_delete_region" in s:
            cs = data["domainObjects"].get("changeSet") or {}
            deleted = [c["regionId"] for c in cs.get("changed", []) if c.get("after") is None]
            ok = ok and s["expect_delete_region"] in deleted
            detail += f" deleted={deleted}"

        mark = "✅" if ok else "❌"
        print(f"{mark} {s['name']:14s} {detail}")
        if ok:
            passed += 1
        else:
            failed += 1

    print()
    print(f"Total: {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
