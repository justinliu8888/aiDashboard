"""Focused checks for public demo password protection and SSE."""
import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from app.access import DemoAccessMiddleware


def build_app(token="test-password"):
    async def page(request):
        return JSONResponse({"ok": True})

    async def stream(request):
        async def events():
            yield 'event: final\ndata: {"ok": true}\n\n'

        return StreamingResponse(events(), media_type="text/event-stream")

    app = Starlette(routes=[
        Route("/", page),
        Route("/health", page),
        Route("/api/sessions", page, methods=["GET", "POST", "DELETE"]),
        Route("/api/agent/chat/stream", stream, methods=["POST"]),
        Route("/docs", page),
    ])
    app.add_middleware(DemoAccessMiddleware, token=token)
    return app


@pytest.mark.parametrize("path,method", [
    ("/", "GET"),
    ("/docs", "GET"),
    ("/api/sessions", "GET"),
    ("/api/sessions", "POST"),
    ("/api/sessions", "DELETE"),
    ("/api/agent/chat/stream", "POST"),
])
def test_unauthorized_requests_are_rejected(path, method):
    with TestClient(build_app()) as client:
        response = client.request(method, path)
    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Basic ")


def test_login_covers_api_and_sse():
    with TestClient(build_app()) as client:
        client.auth = ("demo", "test-password")
        assert client.get("/").status_code == 200
        assert client.post("/api/sessions").status_code == 200
        with client.stream("POST", "/api/agent/chat/stream") as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            assert "event: final" in "".join(response.iter_text())


@pytest.mark.parametrize("authorization", ["Basic ???", "Bearer test-password", "Basic /w=="])
def test_malformed_credentials_are_rejected(authorization):
    with TestClient(build_app()) as client:
        assert client.get("/", headers={"Authorization": authorization}).status_code == 401


@pytest.mark.parametrize("auth", [("demo", "wrong"), ("wrong", "test-password")])
def test_incorrect_credentials_are_rejected(auth):
    with TestClient(build_app()) as client:
        assert client.get("/", auth=auth).status_code == 401


def test_health_is_available_for_cloud_probes():
    with TestClient(build_app()) as client:
        assert client.get("/health").status_code == 200


def test_public_model_requires_password():
    with pytest.raises(ValueError, match="AI_ACCESS_TOKEN"):
        DemoAccessMiddleware(build_app(), token="", require_token=True, model_configured=True)


def test_local_fake_demo_remains_usable():
    with TestClient(build_app(token="")) as client:
        assert client.get("/").status_code == 200
