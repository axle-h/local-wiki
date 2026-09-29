from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from starlette.testclient import TestClient

from wiki_mcp.app import create_app
from wiki_mcp.config import Config
from wiki_mcp.kiwix import KiwixClient

FIXTURES = Path(__file__).parent / "fixtures"
TOKEN = "test-token-0123456789"
NAUTILUS_BOOK = "zimgit-medicine_en_2024-08"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


def fake_kiwix(request: httpx.Request) -> httpx.Response:
    """Just enough of kiwix-serve, answering from recorded responses."""
    path = request.url.path
    if path == "/catalog/v2/entries":
        return httpx.Response(200, text=fixture("catalog.xml"))
    if path == "/search":
        return httpx.Response(200, text=fixture("search.xml"))
    if path == "/suggest":
        if request.url.params.get("content", "").startswith("ifixit_"):
            return httpx.Response(200, text=fixture("suggest.json"))
        return httpx.Response(200, text="[]")
    if path == f"/content/{NAUTILUS_BOOK}/database.js":
        return httpx.Response(200, text=fixture("nautilus-database.js"))
    if path.endswith("/database.js"):
        return httpx.Response(404, text="not found")
    if path.startswith("/content/bicycles.stackexchange.com"):
        return httpx.Response(
            200, text=fixture("stackexchange.html"), headers={"content-type": "text/html"}
        )
    if path.startswith("/content/wikibooks"):
        return httpx.Response(
            200, text=fixture("mediawiki.html"), headers={"content-type": "text/html"}
        )
    return httpx.Response(404, text="not found")


@pytest.fixture
def kiwix() -> KiwixClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake_kiwix), base_url="http://kiwix")
    return KiwixClient("http://kiwix", client=http)


@pytest.fixture
def client(kiwix: KiwixClient) -> Iterator[TestClient]:
    config = Config(kiwix_url="http://kiwix", auth_token=TOKEN, allowed_hosts=("wiki.example",))
    with TestClient(create_app(config, kiwix), base_url="http://wiki.example") as c:
        yield c


@pytest.fixture
def rpc(client: TestClient) -> Callable[..., Any]:
    def call(method: str, params: dict[str, Any] | None = None) -> Any:
        r = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
            headers={
                "Authorization": f"Bearer {TOKEN}",
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": "2025-06-18",
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert "error" not in body, body
        return body["result"]

    return call


def tool_text(result: dict[str, Any]) -> str:
    return "\n".join(c["text"] for c in result["content"] if c["type"] == "text")
