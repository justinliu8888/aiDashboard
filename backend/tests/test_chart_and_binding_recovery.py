"""图型只改展示；展示别名/过滤契约及撤销后再做的局部回归。"""
from copy import deepcopy

import pytest

from app.schemas.report_definition import ReportDefinitionDraft
from app.services.edit_trial_service import run_trial
from app.services.simulated_data_service import generate_simulated_data
from app.services.draft_workspace import draft_workspace

CONTEXT = {"windowStart": "2026-11-08T00:00:00+08:00", "windowEnd": "2026-11-09T00:00:00+08:00", "timezone": "Asia/Shanghai", "grainUnit": "HOUR"}


def definition(chart=True):
    return ReportDefinitionDraft.model_validate({
        "metadata": {"name": "设备能耗日报", "revision": 1},
        "regions": [{
            "id": "r", "name": "设备耗电排名" if chart else "设备耗电明细", "role": "DETAIL",
            "data": {"inputs": {"main": {"type": "DATASET", "datasetRef": "ds_room_energy"}},
                     "pipeline": {"nodes": [{"id": "group", "type": "GROUP_AGGREGATE", "inputs": ["input:main"], "groupBy": ["deviceName"], "measures": [{"asField": "energyKwh", "aggregation": "SUM", "fieldRef": "energyKwh"}]}],
                                  "outputs": {"main": {"nodeRef": "node:group", "expectedFields": {"deviceName": {"dataType": "STRING"}, "timestamp": {"dataType": "DATETIME"}, "energyKwh": {"dataType": "DECIMAL", "unit": "kWh"}}}}}},
            "binding": {"outputRef": "output:main"},
            "component": {"type": "CHART", "chartKind": "BAR", "encodings": [{"channel": "X", "fieldRef": "deviceName"}, {"channel": "Y", "fieldRef": "energyKwh"}]} if chart else {
                "type": "TABLE", "columns": [{"id": "t", "fieldRef": "timestamp", "label": "时间"}, {"id": "e", "fieldRef": "energyKwh", "label": "昨日耗电量"}]},
            "templateBinding": {"previewSpec": {"kind": "energy", "unit": "kWh", "metrics": [{"metricKey": "metric.machine_room.device_energy", "label": "设备耗电量", "kind": "energy", "unit": "kWh"}]}},
        }],
    })


def trial(d, op, params):
    return run_trial(definition=d, operations=[{"op": op, "target": {"regionId": "r"}, "params": params}])


def payload(d):
    return generate_simulated_data(d.model_dump(mode="json"), CONTEXT)["r"]


@pytest.mark.parametrize("kind", ["PIE", "DONUT"])
def test_switch_kind_preserves_every_point_and_definition(kind):
    d = definition()
    before = payload(d)
    result = trial(d, "SET_CHART_KIND", {"chartKind": kind})
    assert result.ok, result.errors
    expected = d.model_dump(mode="json")
    expected["regions"][0]["component"]["chartKind"] = kind
    # 执行器本身只改图型，不重绑数据、不写入分类样本。
    actual = result.definition.model_dump(mode="json")
    actual["metadata"] = expected["metadata"]  # 版本升级属于试做框架。
    assert actual == expected
    after = payload(result.definition)
    assert after["chartKind"] == kind
    for key in ("xLabels", "yValues", "points", "unit"):
        assert after[key] == before[key], key


def test_undo_then_repeat_switch_uses_same_data(client):
    d = definition()
    before = payload(d)
    draft_workspace.create_draft("d_switch", session_id="s_switch", name="设备能耗日报", definition=d.model_dump(mode="json"))
    first = trial(d, "SET_CHART_KIND", {"chartKind": "DONUT"})
    assert first.ok, first.errors
    draft_workspace.commit_edit("d_switch", definition=first.definition.model_dump(mode="json"), snapshot={})
    restored = draft_workspace.undo("d_switch")
    again = trial(ReportDefinitionDraft.model_validate(restored.definition), "SET_CHART_KIND", {"chartKind": "DONUT"})
    assert again.ok, again.errors
    assert payload(again.definition)["yValues"] == before["yValues"]
    assert payload(again.definition)["xLabels"] == before["xLabels"]


def test_time_series_and_multiple_series_still_rejected():
    d = definition()
    r = d.regions[0]
    r["component"]["encodings"][0]["fieldRef"] = "timestamp"
    r["data"]["pipeline"]["nodes"][0]["groupBy"] = ["timestamp"]
    assert not trial(d, "SET_CHART_KIND", {"chartKind": "DONUT"}).ok
    d = definition()
    d.regions[0]["component"]["encodings"].append({"channel": "SERIES", "fieldRef": "deviceName"})
    assert not trial(d, "SET_CHART_KIND", {"chartKind": "DONUT"}).ok


@pytest.mark.parametrize("shorthand", [False, True])
def test_alias_filter_sort_topn_changes_display_only(shorthand):
    d = definition(chart=False)
    before = payload(d)
    condition = {"fieldRef": "energyKwh", "operator": "GT", "value": 400} if shorthand else {
        "type": "CONDITION", "left": {"type": "FIELD", "fieldRef": "energyKwh"}, "operator": "GT", "operands": [{"type": "LITERAL", "value": 400}]}
    aliases = {"timestamp": "时间点", "energyKwh": "昨日耗电量（kWh）"} if shorthand else {"时间点": "timestamp", "昨日耗电量（kWh）": "energyKwh"}
    result = trial(d, "SET_DATA_BINDING", {"fieldAliases": aliases, "presentationFilters": [condition], "sort": [{"fieldRef": "energyKwh", "direction": "DESC"}], "topN": 5})
    assert result.ok, result.errors
    after = payload(result.definition)
    assert after["rows"] == before["rows"]
    expected = sorted([r for r in before["rows"] if r["energyKwh"] > 400], key=lambda r:r["energyKwh"], reverse=True)[:5]
    assert [r["energyKwh"] for r in after["visibleRows"]] == [r["energyKwh"] for r in expected]
    assert {c["fieldRef"]: c["label"] for c in after["columns"]} == {"timestamp": "时间点", "energyKwh": "昨日耗电量（kWh）"}
    assert result.definition.regions[0]["data"] == d.regions[0]["data"]


def test_invalid_filter_keeps_original_binding():
    d = definition(False)
    before = deepcopy(d.model_dump(mode="json"))
    result = trial(d, "SET_DATA_BINDING", {"fieldAliases": {"时间点": "timestamp"}, "presentationFilters": [{"fieldRef": "missing", "operator": "GT", "value": 400}]})
    assert not result.ok
    assert d.model_dump(mode="json") == before


def test_existing_time_bucket_sums_switch_without_replacing_labels_or_values():
    d = definition()
    r = d.regions[0]
    r["role"] = "TREND"
    r["component"]["encodings"][0]["fieldRef"] = "timestamp"
    r["data"]["pipeline"]["nodes"] = [{"id": "group", "type": "RESAMPLE", "inputs": ["input:main"], "timeFieldRef": "timestamp", "grain": {"unit": "HOUR"}, "measures": [{"asField": "energyKwh", "fieldRef": "energyKwh", "aggregation": "SUM"}]}]
    before = payload(d)
    result = trial(d, "SET_CHART_KIND", {"chartKind": "DONUT"})
    assert result.ok, result.errors
    after = payload(result.definition)
    for key in ("xLabels", "yValues", "points"):
        assert after[key] == before[key]
    r["data"]["pipeline"]["nodes"][0]["measures"][0]["aggregation"] = "AVG"
    assert not trial(d, "SET_CHART_KIND", {"chartKind": "DONUT"}).ok


def test_undo_then_repeat_display_change_preserves_full_rows(client):
    d = definition(False)
    params = {"fieldAliases": {"timestamp": "时间点"},
              "presentationFilters": [{"fieldRef": "energyKwh", "operator": "GT", "value": 400}],
              "sort": [{"fieldRef": "energyKwh", "direction": "DESC"}], "topN": 5}
    original = payload(d)
    draft_workspace.create_draft("d_table", session_id="s_table", name="明细", definition=d.model_dump(mode="json"))
    first = trial(d, "SET_DATA_BINDING", params)
    assert first.ok, first.errors
    draft_workspace.commit_edit("d_table", definition=first.definition.model_dump(mode="json"), snapshot={})
    restored = draft_workspace.undo("d_table")
    restored_definition = ReportDefinitionDraft.model_validate(restored.definition)
    assert payload(restored_definition)["rows"] == original["rows"]
    again = trial(restored_definition, "SET_DATA_BINDING", params)
    assert again.ok, again.errors
    for key in ("rows", "visibleRows", "columns"):
        assert payload(again.definition)[key] == payload(first.definition)[key]
