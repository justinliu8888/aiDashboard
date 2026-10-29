"""前后端接缝验证（#54）：真实后端载荷 → 前端原函数校对 → 三段数字恒定。

这条测试守的是**接缝**，不是某一侧：

- 后端走**真实编排**产出 ``reportPreviewPayload``（真 SQLite、真服务、
  只替换外部模型调用 —— 真实模型要联网，联网就不是可复验的）；
- 前端按 ``frontend/pages/workbench.js`` **源码里的原函数**校对这份载荷
  （用 node:vm 从原文抽出来跑，不是抄一份 —— 抄的那份会跟着前端一起漂移）；
- 三段（确认方案 / 点同意 / 重新读取会话）**逐字段**相等。

为什么非要「真的」：前端曾经拿 ``definition.metadata.revision``（**定义的编辑
计数**）去比 ``previewContext.definitionVersion``（**方案 revision**），两条不同
的轴 —— 同一份载荷被判成过期直接丢掉，草稿阶段的数字整个消失。改完之后，
「后端真实产出」与「前端真实读取」这两个接缝**从来没有在一起验过**：后端单测
用自己的断言，前端单测用手写样例，中间那一段没人管。

本文件**不改产品代码**：``backend/app/``、``frontend/pages/`` 一律不动。
"""
from __future__ import annotations

import copy
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from app.services.report_design_service import report_design_service
from app.services.session_service import session_service
from tests.test_report_template_generation import (
    RecordingModel,
    _candidate,
    _post,
    install_model,
)


BACKEND_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = BACKEND_DIR.parent

#: 前端探针：从 ``frontend/pages/workbench.js`` 的**原文**里抽函数跑。
PROBE_PATH = REPO_ROOT / "frontend" / "tests" / "seam_probe.mjs"

#: 前端侧用的固定夹具（由本文件 ``--emit-fixture`` 从真实链路生成）。
FIXTURE_PATH = REPO_ROOT / "frontend" / "tests" / "seam_fixture.json"

SESSION_ID = "sess_tpl_1"

NODE = shutil.which("node")

requires_node = pytest.mark.skipif(
    NODE is None, reason="本机没有 node：前端原函数跑不起来，接缝没法验"
)


# --------------------------------------------------------------------------
# 外部模型调用替身（只替换这一个东西，其余全真）
# --------------------------------------------------------------------------


def _seam_candidate() -> dict[str, Any]:
    """一份覆盖四种前端渲染路径的方案候选。

    - 指标卡：无量纲的 PUE（口径待核实 → 能力待接通）
    - 指标卡：有单位的「总电费」（单位必须是「元」，不能被写死成 kWh）
    - 图表：折线（带 xLabels / yValues）
    - 表格：带列与行
    """
    return _candidate(
        topic="A 机房能耗日报",
        metrics=["总耗电"],
        subject="A 机房",
        time_range="最近 7 天",
        sections=[
            {
                "id": "section_pue",
                "title": "PUE 总览",
                "metricIntents": ["PUE"],
                "componentType": "metric_card",
            },
            {
                "id": "section_cost",
                "title": "总电费",
                "metricIntents": ["电费"],
                "componentType": "metric_card",
            },
            {
                "id": "section_trend",
                "title": "总耗电趋势",
                "metricIntents": ["总耗电"],
                "componentType": "line_chart",
            },
            {
                "id": "section_devices",
                "title": "设备耗电明细",
                "metricIntents": ["设备耗电"],
                "componentType": "table",
            },
        ],
    )


# --------------------------------------------------------------------------
# 真实链路：发消息 → 方案 → 确认方案 → 点同意 → 重新读取会话
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_design_service():
    """每个用例一份干净的设计服务依赖（模型由用例自己装）。"""
    report_design_service.reset_for_tests()
    yield
    report_design_service._model_call = None
    report_design_service.reset_for_tests()


def _run_real_chain(client, monkeypatch) -> dict[str, Any]:
    """走真实编排跑完整条链路，把三段的产物原样收回来。

    真的部分：路由、编排、方案服务、模板转换、造数器、落库、会话读取、
    会话读取时的临时附加。假的只有一个：外部模型调用。
    """
    install_model(monkeypatch, RecordingModel(_seam_candidate()))

    # ---- 第 1 段的前半：用户发消息 → 出方案 ------------------------------
    plan_response = _post(
        client, "生成 A 机房最近 7 天能耗报表", message_id="seam_request"
    ).json()
    plan = plan_response["domainObjects"]["reportPlan"]

    # ---- 第 1 段：确认方案 → 同一轮响应就带示意载荷 ----------------------
    confirm = _post(
        client,
        "确认",
        message_id="seam_confirm",
        context={
            "designAction": "CONFIRM_PLAN",
            "planId": plan["id"],
            "basePlanRevision": plan["revision"],
        },
    ).json()

    # ---- 第 2 段：点「同意」 --------------------------------------------
    approve = _post(client, "同意", message_id="seam_approve").json()

    # ---- 第 3 段：重新读取会话（后端按定义 + 上下文重新生成） -----------
    detail = client.get(f"/api/sessions/{SESSION_ID}").json()
    snapshot = detail.get("snapshot") or {}

    return {
        "planResponse": plan_response,
        "plan": plan,
        "confirm": confirm,
        "approve": approve,
        "detail": detail,
        "snapshot": snapshot,
        # 第 1 段：确认方案那轮响应顶层的载荷
        "stageConfirm": confirm.get("reportPreviewPayload"),
        # 第 2 段：点同意那轮响应（带不带载荷都如实记下来）
        "stageApprove": approve.get("reportPreviewPayload"),
        # 第 3 段：会话读取时临时附加的那一份
        "stageRestore": snapshot.get("reportPreviewPayload"),
        "draftDefinition": (confirm.get("domainObjects") or {}).get(
            "reportDefinitionDraft"
        ),
        "savedDefinition": snapshot.get("definition"),
    }


@pytest.fixture()
def seam_chain(client, monkeypatch) -> dict[str, Any]:
    return _run_real_chain(client, monkeypatch)


# --------------------------------------------------------------------------
# 前端探针：把真实载荷交给前端源码里的原函数
# --------------------------------------------------------------------------


def _probe(
    definition: Any,
    payload: Any,
    *,
    next_payload: Any = None,
    definition_after: Any = None,
    has_next: bool = False,
) -> dict[str, Any]:
    """跑一遍前端源码里的原函数，返回它的判定结果。

    探针做的是**前端真实做的事**：
    ``readReportPreviewPayload`` → ``isStaleReportPayload`` →
    ``applyReportPreviewPayload``；``next_payload`` 模拟**下一轮**响应的载荷
    （可以是 null —— 同意轮就不带载荷）。
    """
    request = {
        "definition": definition,
        "payload": payload,
        "hasNext": has_next,
        "nextPayload": next_payload,
        "definitionAfter": definition_after,
    }
    proc = subprocess.run(
        [NODE, str(PROBE_PATH)],
        input=json.dumps(request, ensure_ascii=False),
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=180,
    )
    assert proc.returncode == 0, f"前端探针跑失败：\n{proc.stderr}"
    return json.loads(proc.stdout)


# --------------------------------------------------------------------------
# ① 后端真实载荷 → 前端真实校对函数
# --------------------------------------------------------------------------


def test_the_chain_runs_on_an_isolated_temporary_database(client, monkeypatch, tmp_path):
    """这条链路跑在**独立的临时 SQLite** 上，绝不碰开发库。

    库文件落在 pytest 的 ``tmp_path`` 里；会话读取那一步读的也是这份库。
    """
    repo = session_service.repository
    db_path = Path(str(repo.conn.execute("PRAGMA database_list").fetchone()[2]))
    assert db_path.is_relative_to(tmp_path), f"用的不是临时库：{db_path}"
    assert "data" not in db_path.parts[-2:], f"库看起来像开发库：{db_path}"

    chain = _run_real_chain(client, monkeypatch)
    # 会话确实落在那份临时库里了。
    assert chain["snapshot"]["previewContext"]
    assert repo.conn.execute(
        "SELECT COUNT(*) FROM report_design_sessions WHERE session_id = ?",
        (SESSION_ID,),
    ).fetchone()[0] >= 1


def test_preview_context_is_the_same_identity_as_the_definition_binding(seam_chain):
    """载荷的 ``previewContext`` 与定义的 ``templateBinding`` 是**同一套值**。

    这是一切的根：前端拿定义里登记的方案身份（方案 id + 方案 revision）
    去比载荷里的 ``previewContext``。两边不是同一套值 → 载荷会被丢掉。
    """
    context = seam_chain["stageConfirm"]["previewContext"]
    binding = seam_chain["draftDefinition"]["metadata"]["templateBinding"]
    plan = seam_chain["plan"]

    # 身份：方案 id + 方案 revision。
    assert context["definitionId"] == binding["definitionId"]
    assert context["definitionVersion"] == binding["definitionVersion"]
    # templateBinding 登的确实是**这一版方案**。
    assert binding["planId"] == plan["id"]
    assert binding["planRevision"] == plan["revision"]
    assert binding["definitionVersion"] == plan["revision"]
    # 版本号是整数形态（前端只认整数形态的版本号）。
    assert isinstance(context["definitionVersion"], int)

    # 两条轴确实**不是**一回事：metadata.revision 是定义的编辑计数。
    # 草稿刚生成时它是 0，而方案 revision 是 1 —— 拿它们互比就是接缝断裂。
    assert seam_chain["draftDefinition"]["metadata"]["revision"] != (
        context["definitionVersion"]
    )


def test_frontend_accepts_the_real_payload_from_the_confirm_turn(seam_chain):
    """前端**原函数**收下真实载荷：不被判过期，apply 返回 true。

    这是 #53 修的那一刀的验收点：修之前这份载荷实测 ``stale=true``，
    草稿阶段的数字整个消失。
    """
    verdict = _probe(seam_chain["draftDefinition"], seam_chain["stageConfirm"])

    assert verdict["stale"] is False, "前端把后端真实产出的载荷判成了过期版本"
    assert verdict["accepted"] is True, "前端没有收下这份载荷"
    assert verdict["kept"] is True, "载荷没有被写进 session.reportPreviewPayload"
    # 收下的就是**这一份**，没有换对象。
    assert verdict["previewContext"] == seam_chain["stageConfirm"]["previewContext"]
    assert verdict["regionCount"] == len(seam_chain["stageConfirm"]["regions"])


def test_frontend_accepts_the_real_payload_from_the_session_restore(seam_chain):
    """会话恢复那一份载荷也收得下（用的是**已保存**的定义当基准）。"""
    verdict = _probe(
        seam_chain["savedDefinition"],
        seam_chain["stageRestore"],
    )

    assert verdict["stale"] is False
    assert verdict["accepted"] is True
    assert verdict["kept"] is True


def test_the_same_payload_would_be_dropped_without_the_template_binding(seam_chain):
    """回归护栏：拿掉 ``templateBinding``，这份真实载荷**确实**会被丢掉。

    这条断言守的是「#53 那刀是必要的」：前端的历史回退口径是
    ``metadata.id`` / ``metadata.revision``，而真实载荷的版本号是**方案
    revision**（1），草稿定义的编辑计数是 0 —— 两条轴互比必然对不上，
    于是同一份载荷被判成过期直接丢掉。
    """
    legacy = copy.deepcopy(seam_chain["draftDefinition"])
    legacy["metadata"].pop("templateBinding", None)

    verdict = _probe(legacy, seam_chain["stageConfirm"])

    assert verdict["stale"] is True, "去掉 templateBinding 后竟然没被判过期"
    assert verdict["accepted"] is False
    assert verdict["kept"] is False


def test_frontend_uses_the_plan_axis_not_the_definition_edit_counter(seam_chain):
    """两条轴在真实数据上确实是分开的：1 ≠ 0。

    载荷的版本号（方案 revision）与定义的编辑计数不相等 —— 前端必须读前者。
    """
    context = seam_chain["stageConfirm"]["previewContext"]
    draft_revision = seam_chain["draftDefinition"]["metadata"]["revision"]

    assert context["definitionVersion"] == 1
    assert draft_revision == 0
    assert context["definitionVersion"] != draft_revision


# --------------------------------------------------------------------------
# 字段名全链路对得上
# --------------------------------------------------------------------------


def test_field_names_line_up_across_the_whole_chain(seam_chain):
    """从「发消息」到「带数报表」，每一环的字段名都对得上。

    接缝最容易断在**名字**上：后端换个字段名，前端就读不到（读到 undefined
    就当没有），界面上就是一片空。这条把每一环用到的字段名逐个钉住。
    """
    plan = seam_chain["plan"]
    confirm = seam_chain["confirm"]
    payload = seam_chain["stageConfirm"]
    definition = seam_chain["draftDefinition"]

    # 方案环节
    assert plan["id"] and plan["revision"]
    assert confirm["domainObjects"]["reportPlan"]["id"] == plan["id"]

    # 定义环节：登记方案身份的那套字段
    binding = definition["metadata"]["templateBinding"]
    assert set(binding) >= {
        "templateId",
        "templateRevision",
        "planId",
        "planRevision",
        "definitionId",
        "definitionVersion",
    }
    assert definition["regions"] and all(r["id"] for r in definition["regions"])

    # 载荷环节：顶层与预览上下文
    assert set(payload) >= {"previewContext", "regions"}
    assert set(payload["previewContext"]) == {
        "definitionId",
        "definitionVersion",
        "timezone",
        "anchorTime",
        "windowStart",
        "windowEnd",
        "grainUnit",
        "generatorVersion",
    }

    # 区域环节：字段名与前端读的一致
    definition_region_ids = {r["id"] for r in definition["regions"]}
    for region in payload["regions"]:
        assert set(region) >= {"regionId", "name", "role", "componentType", "payload"}
        assert region["regionId"] in definition_region_ids, (
            "载荷里的 regionId 在定义里找不到 —— 前端 findDefinitionRegion 会返回 null"
        )
        assert region["role"] != "REPORT_TITLE"

    # 会话恢复环节：重新附加的那一份，字段名与第 1 段完全同构
    restore = seam_chain["stageRestore"]
    assert set(restore) == set(payload)
    assert set(restore["previewContext"]) == set(payload["previewContext"])
    assert [r["regionId"] for r in restore["regions"]] == [
        r["regionId"] for r in payload["regions"]
    ]


# --------------------------------------------------------------------------
# ② 三段数字恒定
# --------------------------------------------------------------------------


def _numeric_fields(payload: dict[str, Any]) -> dict[str, Any]:
    """把载荷摊平成「区域 → 字段路径 → 值」，便于逐字段比对。"""
    flat: dict[str, Any] = {}
    for region in payload["regions"]:
        prefix = f"{region['regionId']}.payload"
        flat[f"{prefix}.__type__"] = region["componentType"]
        for key, value in region["payload"].items():
            flat[f"{prefix}.{key}"] = value
    return flat


def test_three_stages_carry_identical_numbers(seam_chain):
    """三段示意数值**逐字段**相等。

    1. 确认方案那轮响应里的 ``reportPreviewPayload``；
    2. 点「同意」之后前端手上那份（同意轮**不带**载荷 → 前端保留第 1 段）；
    3. 重新读取会话时按定义 + 冻结上下文重新生成的那一份。

    规格要求三处**完全相同**（后端确定性种子保证）。
    """
    stage1 = seam_chain["stageConfirm"]
    stage3 = seam_chain["stageRestore"]
    assert stage1 is not None and stage3 is not None

    # 第 1 段 vs 第 3 段：逐字段相等。
    flat1 = _numeric_fields(stage1)
    flat3 = _numeric_fields(stage3)
    assert set(flat1) == set(flat3)
    differing = {
        key: (flat1[key], flat3[key])
        for key in flat1
        if flat1[key] != flat3[key]
    }
    assert not differing, f"第 1 段与第 3 段的数值不一致：{differing}"
    # 上下文也一模一样（同一个冻结上下文 → 同一个种子）。
    assert stage1["previewContext"] == stage3["previewContext"]

    # 第 2 段：点「同意」那轮**不带**载荷 —— 前端保留第 1 段那份。
    # 这是产品行为（模拟载荷只活在内存里），前端必须原样保留。
    assert seam_chain["stageApprove"] is None, "同意轮竟然带了载荷，口径变了"

    verdict = _probe(
        seam_chain["draftDefinition"],
        stage1,
        next_payload=None,
        definition_after=seam_chain["draftDefinition"],
        has_next=True,
    )
    assert verdict["nextAccepted"] is False, "没有载荷的一轮不该收下任何东西"
    assert verdict["retainedSame"] is True, "同意轮之后前端手上那份载荷被动了"


def test_three_stages_are_identical_through_the_frontend_functions(seam_chain):
    """三段恒定这件事，从**前端原函数**的角度也成立。

    前端收下第 1 段 → 同意轮保留 → 恢复会话再收下第 3 段；
    三段在前端手上始终是同一份数字。
    """
    stage1 = seam_chain["stageConfirm"]
    stage3 = seam_chain["stageRestore"]

    verdict = _probe(
        seam_chain["draftDefinition"],
        stage1,
        next_payload=stage3,
        definition_after=seam_chain["savedDefinition"],
        has_next=True,
    )
    assert verdict["accepted"] is True
    assert verdict["nextStale"] is False
    assert verdict["nextAccepted"] is True
    # 恢复会话那一轮收下的就是重新生成的那一份，数字与第 1 段一致。
    assert verdict["nextRegionCount"] == verdict["regionCount"]
    assert verdict["nextValues"] == verdict["values"]


# --------------------------------------------------------------------------
# ③ 渲染侧能吃下这份真实载荷
# --------------------------------------------------------------------------


def test_frontend_can_read_every_region_of_the_real_payload(seam_chain):
    """真实载荷的每一块，前端都读得出该读的东西。

    - 指标卡：数值、单位、逐条指标
    - 表格：列、行，列的 fieldRef 对得上行键
    - 图表：xLabels / yValues
    - 能力待接通：capabilityPending / pendingReason
    """
    verdict = _probe(seam_chain["draftDefinition"], seam_chain["stageConfirm"])
    payload = seam_chain["stageConfirm"]
    by_id = {r["regionId"]: r for r in payload["regions"]}
    regions = {r["regionId"]: r for r in verdict["regions"]}

    # --- 指标卡：数值与单位 ---
    metric_cards = [r for r in verdict["regions"] if r["componentType"] == "METRIC_CARD"]
    assert metric_cards, "真实载荷里没有指标卡"
    for card in metric_cards:
        raw = by_id[card["regionId"]]["payload"]
        assert isinstance(card["value"], (int, float))
        assert card["value"] == raw["value"]
        assert card["formattedValue"]
        # 单位**来自载荷**（不是前端写死的 kWh）。
        assert card["unit"] == str(raw.get("unit") or "")
        assert card["metrics"], "指标卡的逐条指标没读出来"

    # --- 表格：列与行 ---
    tables = [r for r in verdict["regions"] if r["componentType"] == "TABLE"]
    assert tables, "真实载荷里没有表格"
    for table in tables:
        assert table["columns"], "表格的列没读出来"
        assert table["rowCount"] > 0, "表格的行没读出来"
        assert table["fieldRefsResolveInRows"], "表格列的 fieldRef 对不上行键"
        assert all(c["label"] for c in table["columns"])

    # --- 图表：xLabels / yValues ---
    charts = [r for r in verdict["regions"] if r["componentType"] == "CHART"]
    assert charts, "真实载荷里没有图表"
    for chart in charts:
        assert chart["xLabels"], "图表的 xLabels 没读出来"
        assert chart["yValues"], "图表的 yValues 没读出来"
        assert len(chart["xLabels"]) == len(chart["yValues"])

    # --- 能力待接通：标记与原因 ---
    pending = [r for r in verdict["regions"] if r["capabilityNotes"]]
    assert pending, "真实载荷里没有「能力待接通」的指标"
    for region in pending:
        raw = by_id[region["regionId"]]["payload"]
        marked = [m for m in raw.get("metrics") or [] if m.get("capabilityPending") is True]
        assert marked, "能力待接通的标记没落在载荷的 metrics 上"
        assert all(m.get("pendingReason") for m in marked), "能力待接通没有原因"
        # 前端读出来的是「哪个指标 + 为什么」。
        for metric in marked:
            assert metric["label"] in " ".join(region["capabilityNotes"])
            assert metric["pendingReason"] in " ".join(region["capabilityNotes"])


def test_units_come_from_the_payload_not_from_hardcoded_text(seam_chain):
    """单位是**载荷带来的**，不同区域单位不同 —— 说明前端没有写死 kWh。"""
    verdict = _probe(seam_chain["draftDefinition"], seam_chain["stageConfirm"])
    payload = seam_chain["stageConfirm"]
    by_id = {r["regionId"]: r for r in payload["regions"]}

    units = {}
    for region in verdict["regions"]:
        if region["componentType"] in ("METRIC_CARD", "CHART"):
            units[region["regionId"]] = region["unit"]
            assert region["unit"] == str(by_id[region["regionId"]]["payload"].get("unit") or "")

    # 至少三种单位形态：无量纲（空）、货币（元）、能量（kWh）。
    assert "" in units.values(), "没有无量纲指标卡，单位不可能是写死的"
    assert "元" in units.values(), "没有带货币单位的指标卡"
    assert "kWh" in units.values(), "没有带能量单位的图表"
    # 同一个载荷里单位不止一种 —— 写死的话不可能同时对上。
    assert len(set(units.values())) >= 3


# --------------------------------------------------------------------------
# 前端侧固定夹具：结构不许漂
# --------------------------------------------------------------------------


def test_frontend_fixture_is_not_drifted(seam_chain):
    """前端用的固定夹具（``frontend/tests/seam_fixture.json``）结构不许漂。

    数值会随锚点时间变（每次确认冻结一个新的锚点），所以这里只钉**结构**：
    区域 id、组件类型、单位、预览上下文的字段集。
    """
    if not FIXTURE_PATH.exists():
        pytest.skip(f"夹具还没生成：{FIXTURE_PATH}")

    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    live = seam_chain["stageConfirm"]
    fixed = fixture["payload"]

    assert set(fixed["previewContext"]) == set(live["previewContext"])
    assert [r["regionId"] for r in fixed["regions"]] == [
        r["regionId"] for r in live["regions"]
    ]
    assert [r["componentType"] for r in fixed["regions"]] == [
        r["componentType"] for r in live["regions"]
    ]
    assert [
        str(r["payload"].get("unit") or "") for r in fixed["regions"]
    ] == [str(r["payload"].get("unit") or "") for r in live["regions"]]

    # 夹具里的定义与载荷也必须是同一套身份。
    binding = fixture["definition"]["metadata"]["templateBinding"]
    assert fixed["previewContext"]["definitionId"] == binding["definitionId"]
    assert fixed["previewContext"]["definitionVersion"] == binding["definitionVersion"]


# --------------------------------------------------------------------------
# 夹具生成（给前端 harness / 固定夹具用）
# --------------------------------------------------------------------------


def _emit_fixture(path: Path) -> dict[str, Any]:
    """跑一遍真实链路，把「载荷 + 定义 + 三段」写成一份夹具。"""
    from fastapi.testclient import TestClient

    from app.main import app
    from app.storage.sqlite_store import SqliteSessionRepository

    import tempfile

    tmpdir = tempfile.TemporaryDirectory(prefix="seam-fixture-")
    try:
        old_repo = session_service._repo
        try:
            session_service._repo = SqliteSessionRepository(Path(tmpdir.name) / "sessions.db")
            chain = _run_real_chain(TestClient(app), _NullMonkeypatch())
        finally:
            session_service._repo = old_repo
    finally:
        tmpdir.cleanup()

    bundle = {
        "generatedBy": "backend/tests/test_seam_frontend_backend.py",
        "note": "真实后端链路产出的载荷与定义；数值随锚点时间变，结构稳定。",
        "sessionId": SESSION_ID,
        "payload": chain["stageConfirm"],
        "definition": chain["draftDefinition"],
        "savedDefinition": chain["savedDefinition"],
        "plan": chain["plan"],
        "stageApproveCarriesPayload": chain["stageApprove"] is not None,
        "stageRestorePayload": chain["stageRestore"],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(bundle, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return bundle


class _NullMonkeypatch:
    """``install_model`` 只要一个能收下 setattr 的对象。

    生成夹具时**不开 pytest**，所以这里给一个什么都不做的替身：模型由
    ``install_model`` 直接装到服务上，不需要 monkeypatch 的回滚。
    """

    def setattr(self, target, name, value=None, raising=True):  # noqa: D401
        setattr(target, name, value)


def main(argv: list[str]) -> int:
    """``python3 -m tests.test_seam_frontend_backend --emit-fixture <路径>``。"""
    import argparse

    parser = argparse.ArgumentParser(description="生成前后端接缝夹具")
    parser.add_argument("--emit-fixture", help="夹具写到哪个文件")
    args = parser.parse_args(argv)

    if not args.emit_fixture:
        parser.print_help()
        return 2

    bundle = _emit_fixture(Path(args.emit_fixture))
    print(f"已写入 {args.emit_fixture}")
    print(
        f"  区域：{[r['regionId'] for r in bundle['payload']['regions']]}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
