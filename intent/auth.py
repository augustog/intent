from pathlib import Path
import hmac
import logging
import os
import secrets

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

log = logging.getLogger(__name__)


def generate_token(path: Path | None = None) -> str:
    token = secrets.token_urlsafe(32)
    if path is not None:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, token.encode())
        finally:
            os.close(fd)
        log.info("generated bearer token → %s", path)
    return token


class AuthMiddleware:
    def __init__(self, app: ASGIApp, token: str):
        self.app = app
        self.token = token

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            resp = JSONResponse({"error": "missing bearer token"}, status_code=401)
            await resp(scope, receive, send)
            return
        provided = auth[7:]
        if not hmac.compare_digest(provided, self.token):
            resp = JSONResponse({"error": "invalid bearer token"}, status_code=401)
            await resp(scope, receive, send)
            return
        await self.app(scope, receive, send)
