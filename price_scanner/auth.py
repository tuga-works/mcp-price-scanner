"""Bearer-token check for the HTTP transport.

A pure ASGI middleware: every HTTP request must carry
``Authorization: Bearer <PRICE_SCANNER_TOKEN>``. The comparison is
constant-time (``hmac.compare_digest``). A missing or wrong token gets a bare
401 with an empty body: no hint about what was wrong.
"""

from __future__ import annotations

import hmac

from starlette.types import ASGIApp, Receive, Scope, Send


class BearerAuthMiddleware:
    def __init__(self, app: ASGIApp, token: str) -> None:
        if not token:
            raise ValueError("BearerAuthMiddleware needs a non-empty token")
        self.app = app
        self._expected = token.encode("utf-8")

    def _authorized(self, scope: Scope) -> bool:
        presented = b""
        for name, value in scope.get("headers", []):
            if name.lower() == b"authorization":
                presented = value
                break
        scheme, _, credential = presented.partition(b" ")
        if scheme.lower() != b"bearer":
            credential = b""
        # Always run the comparison so timing does not reveal which check failed.
        return hmac.compare_digest(credential.strip(), self._expected) and bool(credential)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if not self._authorized(scope):
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"www-authenticate", b"Bearer"),
                        (b"content-length", b"0"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": b""})
            return
        await self.app(scope, receive, send)
