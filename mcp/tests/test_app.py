from collections.abc import Callable
from typing import Any

import pytest
from conftest import TOKEN, tool_text
from starlette.testclient import TestClient

INIT = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
HEADERS = {"Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-06-18"}


def test_healthz_needs_no_token(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}


@pytest.mark.parametrize("auth", [None, "Bearer wrong", "wrong", f"Basic {TOKEN}"])
def test_mcp_rejects_a_missing_or_wrong_token(client: TestClient, auth: str | None) -> None:
    headers = HEADERS | ({"Authorization": auth} if auth else {})
    r = client.post("/mcp", json=INIT, headers=headers)
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize("auth", [f"Bearer {TOKEN}", f"bearer {TOKEN}", TOKEN])
def test_mcp_accepts_the_token_with_or_without_the_bearer_prefix(
    client: TestClient, auth: str
) -> None:
    r = client.post("/mcp", json=INIT, headers=HEADERS | {"Authorization": auth})
    assert r.status_code == 200, r.text


def test_an_unlisted_host_is_refused_even_with_the_token(client: TestClient) -> None:
    r = client.post(
        "/mcp",
        json=INIT,
        headers=HEADERS | {"Authorization": f"Bearer {TOKEN}", "Host": "evil.example"},
    )
    assert r.status_code == 421


def test_exactly_two_tools(rpc: Callable[..., Any]) -> None:
    tools = rpc("tools/list")["tools"]
    assert [t["name"] for t in tools] == ["search_library", "read_library"]
    read = next(t for t in tools if t["name"] == "read_library")
    assert read["inputSchema"]["required"] == ["url"]


def test_search_puts_reference_articles_first(rpc: Callable[..., Any]) -> None:
    text = tool_text(
        rpc("tools/call", {"name": "search_library", "arguments": {"query": "treat a burn"}})
    )
    assert text.index("## Encyclopedia and handbook") < text.index("## Everything else")
    assert "1. **Burn** — Wikipedia" in text


def test_search_lists_articles_books_and_titles(rpc: Callable[..., Any]) -> None:
    text = tool_text(
        rpc("tools/call", {"name": "search_library", "arguments": {"query": "kettle wound"}})
    )
    assert "## Everything else (600 matches in all)" in text
    assert "url: /content/wikibooks_en_all_maxi_2026-04/Bicycles/" in text
    assert "## Books and manuals" in text and "Wound Closure Manual" in text
    assert "## Matching titles" in text and "**Kettle** — iFixit" in text


def test_read_returns_the_article_text(rpc: Callable[..., Any]) -> None:
    url = "/content/bicycles.stackexchange.com_en_all_2026-08/questions/21454/x"
    text = tool_text(rpc("tools/call", {"name": "read_library", "arguments": {"url": url}}))
    assert text.startswith("# Bicycle keeps 'skipping a beat'\nSource: Bicycles Q&A")
    assert "## Answer (score 3)" in text


def test_read_pages_long_documents_and_says_how_to_continue(rpc: Callable[..., Any]) -> None:
    # The Stack Exchange fixture is about 3k characters of text; a small page forces paging.
    url = "/content/bicycles.stackexchange.com_en_all_2026-08/questions/21454/x"
    first = tool_text(rpc("tools/call", {"name": "read_library", "arguments": {"url": url}}))
    assert "To continue" not in first  # fits in the default page
    found = tool_text(
        rpc("tools/call", {"name": "read_library", "arguments": {"url": url, "find": "chain"}})
    )
    assert 'Passages about "chain"' in found and "## Passage 1 (offset" in found


def test_read_explains_a_bad_url_instead_of_failing(rpc: Callable[..., Any]) -> None:
    text = tool_text(rpc("tools/call", {"name": "read_library", "arguments": {"url": "/search?x"}}))
    assert "Not a library URL" in text


def test_tools_say_what_the_library_holds_and_describe_every_parameter(
    rpc: Callable[..., Any],
) -> None:
    # Clients such as LM Studio never show the model the server instructions, so the
    # tool descriptions alone must tell it when to reach for the library.
    tools = {t["name"]: t for t in rpc("tools/list")["tools"]}
    assert "Wikipedia" in tools["search_library"]["description"]
    assert "first aid" in tools["search_library"]["description"]
    for tool in tools.values():
        for name, schema in tool["inputSchema"]["properties"].items():
            assert schema.get("description"), f"{tool['name']}.{name} has no description"
