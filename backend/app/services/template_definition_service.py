"""Deterministic adapter from the stored template to the legacy definition."""
from copy import deepcopy
from typing import Any

from app.data.machine_room_energy import METRIC_DEVICE_ENERGY, METRIC_TOTAL_ENERGY
from app.schemas.report_definition import ReportDefinitionDraft
from app.schemas.template_design import TemplateDesign
from app.services.report_draft_service import build_definition_draft


def build_template_definition(
    template: dict[str, Any], plan: dict[str, Any], subject_id: str,
) -> ReportDefinitionDraft:
    current = TemplateDesign.model_validate(template)
    window = (plan.get("requirements") or {}).get("timeRange")
    seed = build_definition_draft(current.title, subject_id, timeRange=window,
                                  componentRequests=["total"])
    regions = []
    placements = {item["regionId"]: item for item in current.layout.get("items", [])}
    supported_refs = {METRIC_TOTAL_ENERGY["metricRef"], METRIC_DEVICE_ENERGY["metricRef"]}
    for index, region in enumerate(current.regions):
        kind = region.componentType
        if region.dataStatus == "bound":
            if len(region.metricRefs) != 1 or region.metricRefs[0] not in supported_refs:
                raise ValueError("Template metric has no safe legacy execution binding")
            device = region.metricRefs[0] == METRIC_DEVICE_ENERGY["metricRef"]
            key = "total" if kind == "metric_card" else "ranking" if device else "trend"
            if kind not in {"metric_card", "table", "line_chart", "bar_chart", "pie_chart"}:
                raise ValueError("Unsupported template component")
            built = build_definition_draft(current.title, subject_id, timeRange=window,
                                           componentRequests=[key], metricRefs=region.metricRefs)
            converted = deepcopy(built.regions[1])
            if device:
                # Template device regions display the final day, not a multi-day sum.
                resolved_window = converted["context"]["timeWindow"]["value"]
                if resolved_window.get("type") == "CALENDAR" and resolved_window.get("unit") == "DAY":
                    resolved_window["offset"] += resolved_window["count"] - 1
                    resolved_window["count"] = 1
                if kind == "metric_card":
                    for node in converted["data"]["pipeline"]["nodes"]:
                        if node.get("type") == "METRIC":
                            node["metricRef"] = METRIC_DEVICE_ENERGY["metricRef"]
                            node["metricVersion"] = METRIC_DEVICE_ENERGY["metricVersion"]
            if kind == "table" and not device:
                converted["component"] = {
                    "type": "TABLE", "columns": [
                        {"id": "timestamp", "label": "日期", "fieldRef": "timestamp"},
                        {"id": "energyKwh", "label": "耗电", "fieldRef": "energyKwh"},
                    ],
                }
            elif kind.endswith("_chart"):
                converted["component"] = {
                    "type": "CHART", "chartKind": kind.split("_")[0].upper(),
                    "encodings": [
                        {"channel": "X", "fieldRef": "deviceName" if device else "timestamp"},
                        {"channel": "Y", "fieldRef": "energyKwh"},
                    ],
                }
            elif kind == "metric_card":
                converted["component"]["metric"]["label"] = region.title
        elif region.dataStatus in {"placeholder", "text"}:
            text = region.placeholderReason if region.dataStatus == "placeholder" else (
                (region.data or {}).get("template") or (region.data or {}).get("rendered") or region.title
            )
            converted = {
                "role": "DESCRIPTION", "component": {
                    "type": "TEXT", "content": {"format": "PLAIN", "template": text or "数据待连接"},
                },
            }
        else:
            raise ValueError("Unknown template data status")
        item = placements.get(region.id, {})
        placement = item.get("placement") or {
            "type": "GRID", "column": int(item.get("column", index % 2)) * 6 + 1,
            "row": int(item.get("row", index // 2)) + 1, "columnSpan": 6, "rowSpan": 1,
        }
        converted.update({
            "id": region.id, "name": region.title,
            "layout": {"placement": deepcopy(placement)},
            "templateBinding": {
                "sectionId": region.sectionId, "dataStatus": region.dataStatus,
                "placeholderReason": region.placeholderReason,
                "dataSourceLabel": region.dataSourceLabel,
                "metricRefs": list(region.metricRefs),
            },
        })
        regions.append(converted)
    seed.regions = regions
    seed.intentBindings = []
    seed.metadata["templateBinding"] = {
        "templateId": current.id, "templateRevision": current.revision,
        "planId": current.planId, "planRevision": current.planRevision,
    }
    seed.metadata["pendingNotes"] = list(current.pendingNotes)
    seed.dependencies["metricRefs"] = [
        item for item in seed.dependencies.get("metricRefs", [])
        if item["ref"] in {ref for r in current.regions if r.dataStatus == "bound" for ref in r.metricRefs}
    ]
    return seed
