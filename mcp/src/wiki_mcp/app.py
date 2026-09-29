"""The ASGI app: the MCP endpoint at /mcp behind a bearer token, plus /healthz."""

import hmac
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from starlette.types import ASGIApp, Receive, Scope, Send

from wiki_mcp.config import Config
from wiki_mcp.kiwix import KiwixClient
from wiki_mcp.server import build_server

logger = logging.getLogger(__name__)

PUBLIC_PATHS = frozenset({"/healthz"})
_LOOPBACK = ("127.0.0.1", "127.0.0.1:*", "localhost", "localhost:*", "[::1]", "[::1]:*")


class BearerAuth:
    """Rejects any request to a non-public path that lacks the token.

    Accepts `Authorization: Bearer <token>`, and also a bare `Authorization: <token>`:
    both carry the same secret, and some MCP clients' header fields make the second
    an easy mistake to make.
    """

    def __init__(self, app: ASGIApp, token: str) -> None:
        self._app = app
        self._token = token.encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in PUBLIC_PATHS:
            await self._app(scope, receive, send)
            return
        header = dict(scope["headers"]).get(b"authorization", b"").strip()
        presented = header[7:].strip() if header[:7].lower() == b"bearer " else header
        if hmac.compare_digest(presented, self._token):
            await self._app(scope, receive, send)
            return
        client = (scope.get("client") or ("?",))[0]
        logger.warning(
            "rejected %s %s from %s: bad or missing token",
            scope.get("method"),
            scope["path"],
            client,
        )
        body = json.dumps({"error": "unauthorized"}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"www-authenticate", b"Bearer"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


def _transport_security(config: Config) -> TransportSecuritySettings:
    if not config.allowed_hosts:
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    hosts = [*_LOOPBACK]
    for host in config.allowed_hosts:
        hosts += [host] if host.endswith(":*") else [host, f"{host}:*"]
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True, allowed_hosts=hosts, allowed_origins=[]
    )


def create_app(config: Config, kiwix: KiwixClient | None = None) -> Starlette:
    kiwix = kiwix or KiwixClient(config.kiwix_url)
    mcp = build_server(config, kiwix)

    async def healthz(_request: Request) -> JSONResponse:
        # Liveness only: a kiwix outage should not get this pod restarted.
        return JSONResponse({"status": "ok"})

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        async with mcp.session_manager.run():
            try:
                yield
            finally:
                await kiwix.aclose()

    # Stateless and JSON: no sessions to lose when the pod restarts, and the simplest
    # exchange for every client.
    inner: ASGIApp = mcp.streamable_http_app(
        stateless_http=True,
        json_response=True,
        transport_security=_transport_security(config),
    )
    if config.auth_token:
        inner = BearerAuth(inner, config.auth_token)
    else:
        logger.warning("MCP_INSECURE_NO_AUTH is set: /mcp answers anyone")
    return Starlette(
        routes=[Route("/healthz", healthz), Mount("/", app=inner)],
        lifespan=lifespan,
    )
