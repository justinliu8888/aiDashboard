"""Password protection for a shared, public demo deployment."""
from __future__ import annotations

import base64
import binascii
from secrets import compare_digest

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


class DemoAccessMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        token: str,
        require_token: bool = False,
        model_configured: bool = False,
    ) -> None:
        if require_token and model_configured and not token:
            raise ValueError("Set AI_ACCESS_TOKEN before enabling a public AI demo")
        self.app = app
        self.token = token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self.token or scope["path"] == "/health":
            await self.app(scope, receive, send)
            return

        authorization = Headers(scope=scope).get("authorization", "")
        scheme, _, credentials = authorization.partition(" ")
        authorized = False
        if scheme.lower() == "basic":
            try:
                decoded = base64.b64decode(credentials, validate=True).decode("utf-8")
                username, separator, password = decoded.partition(":")
                authorized = (
                    username == "demo"
                    and bool(separator)
                    and compare_digest(password.encode("utf-8"), self.token.encode("utf-8"))
                )
            except (ValueError, UnicodeError, binascii.Error):
                pass

        if not authorized:
            response = JSONResponse(
                {"detail": "Demo login required"},
                status_code=401,
                headers={
                    "WWW-Authenticate": 'Basic realm="AI Dashboard", charset="UTF-8"',
                    "Cache-Control": "no-store",
                },
            )
            await response(scope, receive, send)
            return

        # Forward ASGI messages directly so SSE is not buffered by this middleware.
        await self.app(scope, receive, send)
