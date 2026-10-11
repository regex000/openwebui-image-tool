"""Request-size limiting.

FastAPI parses the JSON body *before* running the auth dependency, so without a cap an
unauthenticated client could make the process buffer an arbitrarily large body.
"""

from __future__ import annotations

from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class BodySizeLimitMiddleware:
    """Reject request bodies larger than ``max_bytes`` with ``413``.

    Declared ``Content-Length`` values are rejected up front; chunked/streamed bodies are
    counted as they arrive and abort as soon as the limit is crossed.
    """

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        declared = dict(scope["headers"]).get(b"content-length", b"")
        if declared.isdigit() and int(declared) > self.max_bytes:
            await JSONResponse({"detail": "Request body too large."}, status_code=413)(
                scope, receive, send
            )
            return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    # Raised inside the route, so Starlette's exception middleware
                    # turns it into a clean 413 response.
                    raise HTTPException(status_code=413, detail="Request body too large.")
            return message

        await self.app(scope, limited_receive, send)
