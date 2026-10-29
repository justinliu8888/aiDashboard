"""Template layout updates through the public HTTP API and session recovery."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import pytest

from app.services.report_design_service import report_design_service
from tests.test_report_template_generation import RecordingModel, _body, _candidate


@pytest.fixture
def template(client):
    candidate = _candidate(
        metrics=["总耗电", "PUE"],
        sections=[
            {"id": "energy", "title": "总耗电", "metricIntents": ["总耗电"],
             "componentType": "metric_card"},
            {"id": "pue", "title": "PUE", "metricIntents": ["PUE"],
             "componentType": "metric_card"},
        ],
    )
    report_design_service.reset_for_tests(model_call=RecordingModel(candidate))
    try:
        built = client.post("/api/agent/chat", json=_body("生成能耗和 PUE 报表"))
        assert built.status_code == 200
        confirmed = client.post(
            "/api/agent/chat", json=_body("确认", message_id="confirm")
        )
        assert confirmed.status_code == 200
        yield confirmed.json()["domainObjects"]["templateDesign"]
    finally:
        report_design_service.reset_for_tests()


def _layout_request(template):
    return {
        "templateId": template["id"],
        "baseRevision": template["revision"],
        "items": [
            {"regionId": template["regions"][0]["id"], "placement": {
                "type": "GRID", "column": 1, "row": 1,
                "columnSpan": 6, "rowSpan": 2,
            }},
            {"regionId": template["regions"][1]["id"], "placement": {
                "type": "GRID", "column": 7, "row": 1,
                "columnSpan": 6, "rowSpan": 2,
            }},
        ],
    }


def test_layout_save_advances_revision_and_preserves_data(client, template):
    before = client.get("/api/sessions/sess_tpl_1").json()
    assert any(region["data"] for region in template["regions"])
    assert any(region["dataStatus"] == "placeholder" for region in template["regions"])
    request = _layout_request(template)

    response = client.patch("/api/sessions/sess_tpl_1/template-layout", json=request)

    assert response.status_code == 200
    saved = response.json()["templateDesign"]
    assert saved["revision"] == 2
    assert saved["layout"] == {"columns": 12, "items": request["items"]}
    assert {key: value for key, value in saved.items() if key not in ("revision", "layout")} == {
        key: value for key, value in template.items() if key not in ("revision", "layout")
    }
    restored = client.get("/api/sessions/sess_tpl_1").json()
    assert restored["snapshot"]["templateDesign"] == saved
    assert restored["snapshot"]["reportPlan"] == before["snapshot"]["reportPlan"]
    assert restored["messages"] == before["messages"]
    assert restored["state"] == before["state"]


def test_invalid_layout_and_revision_conflicts_never_partially_write(client, template):
    path = "/api/sessions/sess_tpl_1/template-layout"
    before = client.get("/api/sessions/sess_tpl_1").json()
    request = _layout_request(template)
    invalid_requests = []
    for field, value in (
        ("column", 0), ("row", -1), ("columnSpan", 0), ("rowSpan", 0),
        ("column", True), ("row", 1.5), ("columnSpan", "6"),
        ("column", 8), ("columnSpan", 13), ("type", "ABSOLUTE"),
        ("column", 6),
    ):
        invalid = deepcopy(request)
        invalid["items"][1]["placement"][field] = value
        invalid_requests.append(invalid)
    missing = deepcopy(request)
    missing["items"].pop()
    duplicate = deepcopy(request)
    duplicate["items"][1]["regionId"] = duplicate["items"][0]["regionId"]
    unknown = deepcopy(request)
    unknown["items"][1]["regionId"] = "unknown"
    invalid_requests.extend([missing, duplicate, unknown])
    for invalid in invalid_requests:
        assert client.patch(path, json=invalid).status_code == 422
        assert client.get("/api/sessions/sess_tpl_1").json() == before

    wrong_template = {**request, "templateId": "another_template"}
    assert client.patch(path, json=wrong_template).status_code == 409
    assert client.patch("/api/sessions/missing/template-layout", json=request).status_code == 404
    assert client.get("/api/sessions/sess_tpl_1").json() == before

    alternative = deepcopy(request)
    alternative["items"][0]["placement"]["row"] = 3
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda body: client.patch(path, json=body), [request, alternative]))
    assert sorted(response.status_code for response in responses) == [200, 409]
    winner = next(response.json()["templateDesign"] for response in responses if response.status_code == 200)
    assert winner["revision"] == 2
    assert winner["regions"] == template["regions"]
    after = client.get("/api/sessions/sess_tpl_1").json()
    assert after["snapshot"]["templateDesign"] == winner
    assert after["messages"] == before["messages"]
    assert client.patch(path, json=request).status_code == 409
    assert client.get("/api/sessions/sess_tpl_1").json() == after
