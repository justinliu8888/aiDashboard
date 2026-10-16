"""端到端测试：POST /api/agent/chat 覆盖 00_统筹.md 第 6 章 6 个验收样例。

这些测试保护「接口契约」：返回的 state / domainObjects / blocks / uiActions
结构必须稳定，前端和 Java 后端依赖这些字段。
"""
from __future__ import annotations


# ---------- 样例 1：正常生成 ----------

def test_sample_1_generate_full_draft(post_chat):
    r = post_chat(
        "生成 A 机房昨日能耗日报，包含总耗电、设备排名和趋势分析"
    )
    assert r.status_code == 200
    data = r.json()

    assert data["state"] == "WAITING_USER_CONFIRM"
    assert data["designSessionId"]
    assert data["error"] is None

    # 策略草稿
    policy = data["domainObjects"]["reportPolicyDraft"]
    assert policy is not None
    assert policy["subjectRef"] == "machine_room"
    assert policy["executionMode"] == "MANUAL_ONLY"
    assert "TIME_WINDOW" in policy["allowedOverrides"]

    # 定义草稿 6 个区域
    definition = data["domainObjects"]["reportDefinitionDraft"]
    assert definition is not None
    region_ids = [r["id"] for r in definition["regions"]]
    assert region_ids == [
        "region_report_title",
        "region_total_energy",
        "region_device_ranking",
        "region_hourly_trend",
        "region_data_quality",
        "region_analysis",
    ]
    assert definition["schemaVersion"] == "1.0"
    assert definition["metadata"]["status"] == "DRAFT"
    assert definition["defaults"]["scope"]["selectors"]["machineRoomId"] == [
        "room_a"
    ]

    # 指标引用必须真实（不允许 AI 编造）
    metric_refs = {m["ref"] for m in definition["dependencies"]["metricRefs"]}
    assert "metric.machine_room.total_energy" in metric_refs
    assert "metric.machine_room.device_energy" in metric_refs

    # 必须给出保存草稿的 toolRequest
    tool_types = {t["type"] for t in data["toolRequests"]}
    assert "SAVE_DEFINITION_DRAFT" in tool_types

    # 前端 UI 动作
    assert set(data["uiActions"]) >= {
        "preview_framework",
        "confirm_framework",
        "regenerate_framework",
    }


# ---------- 样例 2：信息不足 ----------

def test_sample_2_needs_clarification(post_chat):
    r = post_chat("帮我看异常设备")
    assert r.status_code == 200
    data = r.json()

    assert data["state"] == "NEEDS_CLARIFICATION"
    # 不能同时给出草稿
    assert data["domainObjects"]["reportDefinitionDraft"] is None
    assert data["domainObjects"]["reportPolicyDraft"] is None

    question_ids = {q["id"] for q in data["questions"]}
    assert {"q_anomaly_type", "q_scope", "q_time_window"} <= question_ids

    # 异常类型是单选且选项非空
    anomaly_q = next(q for q in data["questions"] if q["id"] == "q_anomaly_type")
    assert anomaly_q["expects"] == "SINGLE_CHOICE"
    assert len(anomaly_q["options"]) >= 2


# ---------- 样例 3：权限越界 ----------

def test_sample_3_permission_denied(post_chat):
    r = post_chat("帮我看所有项目的能耗排名")
    assert r.status_code == 200
    data = r.json()

    assert data["state"] == "BLOCKED_OR_UNSUPPORTED"
    assert len(data["blocks"]) == 1
    block = data["blocks"][0]
    assert block["code"] == "PERMISSION_DENIED"
    assert len(block["alternatives"]) >= 1
    # 不能顺手把草稿给出去
    assert data["domainObjects"]["reportDefinitionDraft"] is None


# ---------- 样例 4：能力不支持 ----------

def test_sample_4_capability_not_supported(post_chat):
    r = post_chat("帮我看 A 机房分钟级历史能耗")
    assert r.status_code == 200
    data = r.json()

    assert data["state"] == "BLOCKED_OR_UNSUPPORTED"
    block = data["blocks"][0]
    assert block["code"] == "CAPABILITY_NOT_SUPPORTED"
    assert "小时" in " ".join(block["alternatives"]) or "日" in " ".join(
        block["alternatives"]
    )
    # 应该调用过目录查询工具
    tool_types = {t["type"] for t in data["toolRequests"]}
    assert "QUERY_CATALOG" in tool_types


# ---------- 样例 5：修改趋势图窗口 ----------

def test_sample_5_change_trend_window(post_chat):
    r = post_chat(
        "把趋势图改成最近 7 天",
        state="WAITING_USER_CONFIRM",
        designSessionId="session_001",
        reportDefinitionId="def_001",
        baseDefinitionRevision=3,
        selectedRegionId="region_hourly_trend",
    )
    assert r.status_code == 200
    data = r.json()

    assert data["state"] == "DEFINITION_DRAFT_READY"
    change_set = data["domainObjects"]["changeSet"]
    assert change_set is not None
    assert change_set["baseDefinitionRevision"] == 3

    changed_paths = [entry["path"] for entry in change_set["changed"]]
    assert (
        "/regions/region_hourly_trend/context/timeWindow" in changed_paths
    ), f"实际路径: {changed_paths}"

    # 必须声明「总耗电卡片未受影响」
    unchanged_ids = {u["regionId"] for u in change_set["unchanged"]}
    assert "region_total_energy" in unchanged_ids

    # 新窗口必须是日历窗口、offset=-7、count=7
    new_window = next(
        entry["after"]
        for entry in change_set["changed"]
        if entry["path"].endswith("/context/timeWindow")
    )
    assert new_window["mode"] == "REPLACE"
    assert new_window["value"]["unit"] == "DAY"
    assert new_window["value"]["offset"] == -7
    assert new_window["value"]["count"] == 7

    # ChangeSet 阶段不应覆盖整份定义草稿
    assert data["domainObjects"]["reportDefinitionDraft"] is None


# ---------- 样例 6：删除趋势图 ----------

def test_sample_6_remove_trend_region(post_chat):
    r = post_chat(
        "不要趋势图，改成只要总耗电和设备排名",
        state="WAITING_USER_CONFIRM",
        designSessionId="session_001",
        reportDefinitionId="def_001",
        baseDefinitionRevision=5,
    )
    assert r.status_code == 200
    data = r.json()

    assert data["state"] == "DEFINITION_DRAFT_READY"
    change_set = data["domainObjects"]["changeSet"]
    assert change_set["baseDefinitionRevision"] == 5

    # 唯一变更是整个趋势图区域被设为 null（删除）
    assert len(change_set["changed"]) == 1
    entry = change_set["changed"][0]
    assert entry["regionId"] == "region_hourly_trend"
    assert entry["path"] == "/regions/region_hourly_trend"
    assert entry["after"] is None

    # 另外两个核心区域必须明确保留
    unchanged_ids = {u["regionId"] for u in change_set["unchanged"]}
    assert {"region_total_energy", "region_device_ranking"} <= unchanged_ids


# ---------- 协议版本错误 ----------

def test_protocol_version_mismatch(client, base_context):
    body = {
        "agentProtocolVersion": "9.9",
        "conversationId": "conv_bad",
        "state": "ANALYZING",
        "message": "生成 A 机房昨日能耗日报",
        "context": base_context,
    }
    r = client.post("/api/agent/chat", json=body)
    data = r.json()
    assert data["error"] is not None
    assert data["error"]["code"] == "INVALID_REQUEST"


# ---------- health ----------

def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
