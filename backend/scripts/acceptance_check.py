"""端到端验收脚本：真实话术 → 路由槽位 → 定义草稿 → 校验 → 预览。

用真实的 ``routing_rules.explicit_slots()``（路由层实际用的那套规则）
抽槽位，再喂给 ``build_definition_draft``，检查用户要的���件有没有
真的落进报表定义、预览数据有没有跟着变。

    python scripts/acceptance_check.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import routing_rules  # noqa: E402
from app.services.preview_service import preview_service  # noqa: E402
from app.services.report_draft_service import (  # noqa: E402
    REGION_IDS,
    DefinitionBuildError,
    build_definition_draft,
)
from app.services.validation_service import validate_definition  # noqa: E402


CASES: list[tuple[str, str, list[str], dict[str, Any]]] = [
    (
        "M-08 过去7个完整自然日 + 每天趋势",
        "生成 A 机房过去7个完整自然日的总耗电和每天趋势，不要只看昨日。",
        ["timeWindow=7天", "grain=DAY", "总数>1天"],
        {},
    ),
    (
        "M-13 eas_energy_detail + MINUTE",
        "使用 eas_energy_detail 数据源，生成 A 机房昨日分钟耗电趋势。",
        ["sourceRef=eas_energy_detail", "grain=MINUTE"],
        {},
    ),
    (
        "M-03 只要一个总耗电指标卡",
        "生成 A 机房昨日总耗电报表，只放一个总耗电指标卡，不要排名表、趋势图和分析段。",
        ["无ranking", "无trend", "无analysis"],
        {},
    ),
    (
        "M-09 只统计 CRAC-01/CRAC-02",
        "生成 A 机房昨日仅空调设备的耗电报表，只统计 CRAC-01 和 CRAC-02；不要统计机架、PDU和UPS。",
        ["有设备过滤", "预览2行"],
        {},
    ),
    (
        "M-10 Top3 + 占机房总耗电比例",
        "生成 A 机房昨日能耗日报。总耗电按全部10台设备，排名表只显示耗电最高的前3台；显示这3台占机房总耗电的比例。",
        ["topN=3", "有占比列", "预览3行"],
        {},
    ),
    (
        "M-11 前一日耗电 / 日增长率 / 标红",
        "生成 A 机房昨日各设备耗电表，增加前一日耗电、日增长率，并将增长严格超过20%的设备行标红。",
        ["有前一日列", "有增长率列", "有标红样式"],
        {},
    ),
    (
        "M-14 所有项目全部机房 → 阻断",
        "生成所有项目全部机房的能耗排名，不限 A 机房。",
        ["阻断"],
        {"roomId": "room_all"},
    ),
    (
        "M-15 设备心情值 → 阻断",
        '生成 A 机房昨日"设备心情值"的趋势。没有登记指标就说明缺失，不要用耗电量或随机数代替。',
        ["阻断"],
        {"metricRefs": ["metric.machine_room.device_mood"]},
    ),
]


def _api_block_check(message: str) -> bool:
    """接口层：越权范围 / 未知指标必须给明确的 blocks。"""
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    response = client.post(
        "/api/agent/chat",
        json={
            "conversationId": f"conv_acceptance_{abs(hash(message)) % 100000}",
            "state": "ANALYZING",
            "message": message,
            "context": {
                "projectId": "project_001",
                "subjectRef": "machine_room",
                "subjectId": "room_a",
            },
        },
    ).json()
    state = response.get("state")
    blocks = response.get("blocks") or []
    print(f"    接口层返回：state={state} blocks={[b.get('code') for b in blocks]}")
    return _check(
        "接口层明确阻断",
        state == "BLOCKED_OR_UNSUPPORTED" and bool(blocks),
        str(state),
    )


def _builder_block_check(message: str, **plan_overrides: Any) -> bool:
    """组装层兜底：计划里带着越权对象 / 未登记指标时必须阻断。"""
    plan: dict[str, Any] = {
        "reportName": "A 机房能耗日报",
        "roomId": "room_a",
        "includeTrend": True,
        "sourceRef": None,
        "grainUnit": None,
        "timeWindow": None,
        "metricRefs": None,
        "componentRequests": None,
        "excludedRegions": None,
        "filters": None,
        "topN": None,
        "derivedColumns": None,
        "styleHints": None,
    }
    plan.update(plan_overrides)
    try:
        build_definition_draft(
            plan["reportName"],
            plan["roomId"],
            include_trend=plan.get("includeTrend", True),
            sourceRef=plan.get("sourceRef"),
            grainUnit=plan.get("grainUnit"),
            timeWindow=plan.get("timeWindow"),
            metricRefs=plan.get("metricRefs"),
            componentRequests=plan.get("componentRequests"),
            excludedRegions=plan.get("excludedRegions"),
            filters=plan.get("filters"),
            topN=plan.get("topN"),
            derivedColumns=plan.get("derivedColumns"),
            styleHints=plan.get("styleHints"),
        )
    except DefinitionBuildError as exc:
        print(f"    组装层阻断：[{exc.code}] {exc.message}")
        return _check("组装层阻断", True, exc.code)
    print("    ❌ 组装层没有阻断")
    return False


def _region(definition: Any, key: str) -> dict[str, Any] | None:
    for region in definition.regions:
        if region["id"] == REGION_IDS[key]:
            return region
    return None


def _check(label: str, condition: bool, detail: str = "") -> bool:
    mark = "✅" if condition else "❌"
    print(f"    {mark} {label}{(' → ' + detail) if detail else ''}")
    return condition


def run_case(
    title: str,
    message: str,
    expectations: list[str],
    block_overrides: dict[str, Any] | None = None,
) -> bool:
    print(f"\n=== {title} ===")
    print(f"    用户输入：{message}")

    slots = routing_rules.explicit_slots(message)
    print(f"    路由槽位：{json.dumps(slots, ensure_ascii=False)}")

    if "阻断" in expectations:
        ok = _api_block_check(message)
        ok &= _builder_block_check(message, **(block_overrides or {}))
        return ok

    plan: dict[str, Any] = {
        "reportName": "A 机房能耗日报",
        "roomId": slots.get("subjectId", "room_a"),
        "includeTrend": True,
        "sourceRef": slots.get("sourceRef"),
        "grainUnit": slots.get("grainUnit"),
        "timeWindow": slots.get("timeRange"),
        "metricRefs": slots.get("metricRefs"),
        "componentRequests": slots.get("componentRequests"),
        "excludedRegions": slots.get("excludedRegions"),
        "filters": slots.get("filters"),
        "topN": slots.get("topN"),
        "derivedColumns": slots.get("derivedColumns"),
        "styleHints": slots.get("styleHints"),
    }
    plan = {k: v for k, v in plan.items() if v not in (None, [], {})}

    try:
        definition = build_definition_draft(
            plan["reportName"],
            plan["roomId"],
            include_trend=plan.get("includeTrend", True),
            sourceRef=plan.get("sourceRef"),
            grainUnit=plan.get("grainUnit"),
            timeWindow=plan.get("timeWindow"),
            metricRefs=plan.get("metricRefs"),
            componentRequests=plan.get("componentRequests"),
            excludedRegions=plan.get("excludedRegions"),
            filters=plan.get("filters"),
            topN=plan.get("topN"),
            derivedColumns=plan.get("derivedColumns"),
            styleHints=plan.get("styleHints"),
        )
    except DefinitionBuildError as exc:
        blocked = "阻断" in expectations
        print(f"    阻断：[{exc.code}] {exc.message}")
        return _check("按预期阻断", blocked, exc.code)

    if "阻断" in expectations:
        print("    ❌ 期望阻断，但真的生成了草稿")
        return False
    ok = True
    window = definition.defaults.get("timeWindow") or {}
    grain = (definition.defaults.get("grain") or {}).get("unit")

    if "timeWindow=7天" in expectations:
        ok &= _check(
            "时间窗是 7 个完整自然日",
            window.get("count") == 7 and window.get("offset") == -7,
            f"count={window.get('count')} offset={window.get('offset')}",
        )
    if "grain=DAY" in expectations:
        ok &= _check("默认粒度是 DAY", grain == "DAY", str(grain))
    if "grain=MINUTE" in expectations:
        ok &= _check("默认粒度是 MINUTE", grain == "MINUTE", str(grain))
    if "sourceRef=eas_energy_detail" in expectations:
        ok &= _check(
            "数据源是 eas_energy_detail",
            definition.defaults.get("source", {}).get("sourceRef") == "eas_energy_detail",
            str(definition.defaults.get("source")),
        )
    if "总数>1天" in expectations:
        total = _region(definition, "total")
        region_window = (total.get("context") or {}).get("timeWindow", {}).get("value", {})
        ok &= _check(
            "总耗电卡片也用 7 天窗口",
            region_window.get("count") == 7,
            f"count={region_window.get('count')}",
        )
    if "无ranking" in expectations:
        ok &= _check("没有排名区", _region(definition, "ranking") is None)
    if "无trend" in expectations:
        ok &= _check("没有趋势区", _region(definition, "trend") is None)
    if "无analysis" in expectations:
        ok &= _check("没有分析区", _region(definition, "analysis") is None)

    ranking = _region(definition, "ranking")
    if "有设备过滤" in expectations and ranking is not None:
        nodes = (ranking.get("data") or {}).get("pipeline", {}).get("nodes", [])
        filter_nodes = [n for n in nodes if n.get("type") == "FILTER"]
        ok &= _check("排名区有 FILTER 节点", bool(filter_nodes))
    if "topN=3" in expectations and ranking is not None:
        ok &= _check(
            "topN=3",
            (ranking.get("binding") or {}).get("topN") == 3,
            str((ranking.get("binding") or {}).get("topN")),
        )
    if "有占比列" in expectations and ranking is not None:
        refs = {c.get("fieldRef") for c in ranking["component"]["columns"]}
        ok &= _check("有占比列", "energySharePercent" in refs, str(sorted(refs)))
    if "有前一日列" in expectations and ranking is not None:
        refs = {c.get("fieldRef") for c in ranking["component"]["columns"]}
        ok &= _check("有前一日耗电列", "prevPeriodEnergyKwh" in refs, str(sorted(refs)))
    if "有增长率列" in expectations and ranking is not None:
        refs = {c.get("fieldRef") for c in ranking["component"]["columns"]}
        ok &= _check("有日增长率列", "growthRatePercent" in refs, str(sorted(refs)))
    if "有标红样式" in expectations and ranking is not None:
        hints = ranking["component"].get("styleHints") or []
        ok &= _check(
            "有条件样式",
            any(h.get("threshold") == 20 for h in hints),
            str(hints),
        )

    # ---- 校验 ----
    validation = validate_definition(definition.model_dump(mode="json"))
    ok &= _check(
        "定义通过校验",
        validation.passed,
        "" if validation.passed else str([i.message for i in validation.issues]),
    )

    # ---- 预览 ----
    _preview, record = preview_service.run_preview(
        definitionId="def_acceptance", definitionDraft=definition.model_dump(mode="json")
    )
    for region in record.regions:
        if region["regionId"] == REGION_IDS["ranking"]:
            rows = region["payload"].get("rows") or []
            if "预览2行" in expectations:
                ok &= _check("预览只有 2 行", len(rows) == 2, f"{len(rows)} 行")
                ok &= _check(
                    "预览行是 CRAC",
                    all("CRAC" in str(r.get("deviceName")) for r in rows),
                    str([r.get("deviceName") for r in rows]),
                )
            if "预览3行" in expectations:
                ok &= _check("预览只有 3 行", len(rows) == 3, f"{len(rows)} 行")
                ok &= _check(
                    "3 行都有占比",
                    all(r.get("energySharePercent") is not None for r in rows),
                )
            if "有标红样式" in expectations:
                highlighted = [
                    r.get("deviceName")
                    for r in rows
                    if r.get("styleHints")
                ]
                print(f"    ℹ️ 标红行：{highlighted}")

    return ok


def main() -> int:
    results: list[tuple[str, bool]] = []
    for title, message, expectations, overrides in CASES:
        results.append((title, run_case(title, message, expectations, overrides)))
    print("\n" + "=" * 60)
    failed = [title for title, ok in results if not ok]
    for title, ok in results:
        print(f"{'✅' if ok else '❌'} {title}")
    print("=" * 60)
    if failed:
        print(f"未通过：{len(failed)} / {len(results)}")
        return 1
    print(f"全部通过：{len(results)} / {len(results)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
