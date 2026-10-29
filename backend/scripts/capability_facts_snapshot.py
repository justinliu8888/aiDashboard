"""打印「问实际产品能力」的事实快照，作为 #16 的可复核证据。

用法（backend 目录下）::

    python3 scripts/capability_facts_snapshot.py            # 人类可读
    python3 scripts/capability_facts_snapshot.py --json     # 机读

只读脚本：不写文件、不改会话、不碰业务状态。
每个场景打印 outcome 与关键事实，用来核对「目录登记 / 链路支持 /
模拟数据」三层语义没有被说反。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services import product_capability_service as capability  # noqa: E402


SCOPE = {
    "project_id": "project_001",
    "subject_ref": "machine_room",
    "subject_id": "room_a",
}

UNAUTHORIZED_SCOPE = {
    "project_id": "project_999",
    "subject_ref": "machine_room",
    "subject_id": "room_a",
}

SCENARIOS: list[tuple[str, dict[str, Any]]] = [
    (
        "默认机房能耗源 + 分钟粒度（应不支持）",
        {"source_ref": "catalog.machine_room.energy", "grain_unit": "MINUTE"},
    ),
    (
        "EAS 能耗明细 + 分钟粒度（支持，但预览是模拟数据）",
        {"source_ref": "eas_energy_detail", "grain_unit": "MINUTE"},
    ),
    (
        "能耗成本（目录登记，生成模板不支持）",
        {"metric_ref": "metric.machine_room.energy_cost"},
    ),
    (
        "PUE（目录登记，生成模板不支持）",
        {"metric_ref": "metric.machine_room.pue"},
    ),
    (
        "日汇总源（缺设备维度，设备排名出不来）",
        {"source_ref": "catalog.machine_room.energy_daily_summary"},
    ),
    (
        "未知数据源（授权目录里确实没有）",
        {"source_ref": "catalog.unknown.source"},
    ),
    (
        "无权限的数据源（边界，不是「不存在」）",
        {"source_ref": "eas_energy_detail"},
    ),
    (
        "无上下文：只问分钟趋势（给产品范围 + 澄清）",
        {"grain_unit": "MINUTE"},
    ),
]


def _describe(scope: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    return capability.describe_capability(**scope, **params)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()

    snapshot: list[dict[str, Any]] = []
    for title, params in SCENARIOS:
        scope = UNAUTHORIZED_SCOPE if "无权限" in title else SCOPE
        result = _describe(scope, params)
        snapshot.append({"scenario": title, "result": result})

    if args.json:
        print(json.dumps(snapshot, ensure_ascii=False, indent=2))
        return 0

    for item in snapshot:
        result = item["result"]
        print("=" * 72)
        print(item["scenario"])
        print(f"  outcome            : {result['outcome']}")
        catalog = result["catalog"]
        print(f"  目录登记            : {catalog['registered']}")
        if catalog["registered"]:
            print(f"  支持粒度            : {catalog['supportedGrains']}")
            print(f"  缺设备维度          : {not catalog['hasDeviceDimension']}")
            print(f"  时间字段            : {catalog['timeField']}")
        else:
            print("  支持粒度            : （未查询具体数据源）")
            print("  缺设备维度          : （未查询具体数据源）")
            print("  时间字段            : （未查询具体数据源）")
        metric = result["metric"]
        print(f"  指标                : {metric.get('label')} / 登记={metric['registered']}")
        execution = result["execution"]
        for link in ("generation", "edit", "preview"):
            flag = execution[link]
            print(f"  {link:<19}: {flag['supported']} {flag['reason'] or ''}")
        grain = execution["grain"]
        print(f"  粒度 {str(grain['requested']):<13}: {grain['supported']}")
        components = execution["components"]
        if components:
            print(f"  区块支持            : {components}")
        source = result["dataSource"]
        print(f"  取数模式            : {source['mode']}（生产接通={source['productionConnected']}）")
        print(f"  替代方案            : {result['alternatives']}")
        print(f"  澄清                : {result['clarification']}")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
