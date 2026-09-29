"""The MCP surface: two tools, `search_library` and `read_library`, shaped for small local models.

Neither tool needs the model to know which book to look in. `search_library` covers
the whole library in one call, and `read_library` takes a URL exactly as it was returned.

The descriptions carry the weight of getting a model to use the tools at all. Many
clients (LM Studio among them) never show the model the server's `instructions`, so
each tool description says on its own what the library holds and when to reach for it.
"""

import asyncio
import logging
from collections import OrderedDict
from typing import Annotated
from urllib.parse import unquote

from mcp.server import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

from wiki_mcp import __version__
from wiki_mcp.config import Config
from wiki_mcp.extract import Page, extract_html, extract_pdf, find_passages
from wiki_mcp.kiwix import Content, Hit, KiwixClient, KiwixError, content_path

logger = logging.getLogger(__name__)

#: What the library holds, in the words a model will match a question against.
#: Keep in step with k8s/zim-library.yaml when content is added or removed.
LIBRARY = """\
The library holds all of English Wikipedia, plus Wikibooks (textbooks and how-to \
guides), Wiktionary (a dictionary), iFixit repair guides, the NHS medicines A-Z, \
Q&A sites on home improvement and DIY, electronics, car mechanics, bicycles, cooking, \
gardening, the outdoors, amateur radio, woodworking, pets and engineering, and PDF \
manuals on first aid, emergency and field medicine, water treatment, food \
preservation and post-disaster survival.\
"""

INSTRUCTIONS = f"""\
An offline reference library that works without internet. {LIBRARY}

Look things up here before answering from memory: call `search_library` with a few \
keywords, then `read_library` with the url of the best result. For long documents \
(PDF books especially), pass `find` to `read_library` to jump to the passages you need.\
"""

SEARCH_DESCRIPTION = f"""\
Search a large offline reference library and get a ranked list of matching articles, \
Q&A threads and books, each with a url to read.

{LIBRARY}

Use this whenever answering needs facts, explanations, instructions or advice: health \
and first aid, repairs and DIY, how something works, science, history, people, places, \
cooking, gardening, the meaning of a word. Prefer it to answering from memory; it is \
reliable reference material and it works without internet.

Then call read_library with the url of the most promising result to get its full text.\
"""

READ_DESCRIPTION = """\
Get the full text of an article, Q&A thread or book found with search_library.

Long documents come back in parts; the end of each part gives the offset to continue \
from. For long documents, especially PDF books, pass `find` with a word or phrase to \
get only the passages about it instead of reading from the start.\
"""

_READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
_MAX_LIMIT = 25
_CACHE_SIZE = 16


class PageCache:
    """Extracted pages by URL. A big PDF takes seconds to extract; paging must not repeat that."""

    def __init__(self, size: int = _CACHE_SIZE) -> None:
        self._pages: OrderedDict[str, Page] = OrderedDict()
        self._size = size

    def get(self, key: str) -> Page | None:
        page = self._pages.get(key)
        if page is not None:
            self._pages.move_to_end(key)
        return page

    def put(self, key: str, page: Page) -> None:
        self._pages[key] = page
        self._pages.move_to_end(key)
        while len(self._pages) > self._size:
            self._pages.popitem(last=False)


def _format_hits(heading: str, hits: list[Hit], start: int) -> list[str]:
    lines = [f"## {heading}"]
    for n, hit in enumerate(hits, start=start):
        lines.append(f"{n}. **{hit.title}** — {hit.source}\n   url: {hit.url}")
        if hit.snippet:
            snippet = hit.snippet if len(hit.snippet) <= 300 else hit.snippet[:300] + "…"
            lines.append(f"   {snippet}")
    return lines


def _page_from(content: Content) -> Page:
    if content.content_type == "application/pdf":
        return extract_pdf(content.body)
    if content.content_type in ("text/html", "application/xhtml+xml"):
        page = extract_html(content.body)
        # Most pages open with their own title as a heading, which `read_library`'s header already
        # gives. Dropped here, before caching, so `find` offsets and paging share one text.
        if page.title:
            page = Page(page.title, page.text.removeprefix(f"# {page.title}").lstrip())
        return page
    if content.content_type.startswith("text/"):
        return Page(title="", text=content.body.decode("utf-8", errors="replace"))
    raise KiwixError(
        f"{content.url} is {content.content_type or 'binary'} content, which has no text to read."
    )


def build_server(config: Config, kiwix: KiwixClient) -> MCPServer:
    mcp: MCPServer = MCPServer(
        name="wiki",
        title="Offline library",
        version=__version__,
        instructions=INSTRUCTIONS,
    )
    cache = PageCache()

    async def load(path: str) -> Page:
        page = cache.get(path)
        if page is None:
            content = await kiwix.fetch(path)
            # PDF extraction is CPU-bound and can take seconds; keep the event loop free.
            page = await asyncio.to_thread(_page_from, content)
            cache.put(path, page)
        return page

    @mcp.tool(
        title="Search the offline library",
        description=SEARCH_DESCRIPTION,
        annotations=_READ_ONLY,
        structured_output=False,
    )
    async def search_library(
        query: Annotated[
            str,
            Field(
                description="A few keywords, not a whole sentence, "
                'e.g. "treat a burn", "purify water boiling" or "bike chain skipping".'
            ),
        ],
        limit: Annotated[int, Field(description="How many results to return, 1 to 25.")] = 10,
    ) -> str:
        limit = max(1, min(limit, _MAX_LIMIT))
        try:
            results = await kiwix.search(query, limit)
        except Exception as e:  # the library being down is an answer, not a crash
            logger.exception("search failed")
            return f"The library could not be searched: {e}"

        lines = [f'# Results for "{query}"']
        n = 1
        if results.text_hits:
            lines += _format_hits(
                f"Articles ({len(results.text_hits)} of {results.text_total} matches)",
                results.text_hits,
                n,
            )
            n += len(results.text_hits)
        if results.document_hits:
            lines += _format_hits("Books and manuals", results.document_hits, n)
            n += len(results.document_hits)
        if results.title_hits:
            lines += _format_hits("Matching titles", results.title_hits, n)
        if n == 1 and not results.title_hits:
            lines.append("Nothing matched. Try fewer or different keywords.")
        lines += [f"Note: {err}" for err in results.errors]
        return "\n".join(lines)

    @mcp.tool(
        title="Read from the offline library",
        description=READ_DESCRIPTION,
        annotations=_READ_ONLY,
        structured_output=False,
    )
    async def read_library(
        url: Annotated[
            str,
            Field(description="A url exactly as search_library returned it (starts /content/)."),
        ],
        find: Annotated[
            str | None,
            Field(
                description='Optional word or phrase, e.g. "tourniquet". Returns only the '
                "passages that mention it; best for long documents and PDF books."
            ),
        ] = None,
        offset: Annotated[
            int,
            Field(
                description="Optional. Where to continue a long document, as given at the "
                "end of the previous part or next to a passage."
            ),
        ] = 0,
    ) -> str:
        try:
            path = content_path(url)
            page = await load(path)
            book = await kiwix.book_for(path)
            document = await kiwix.document_for(path)
        except KiwixError as e:
            return str(e)
        except Exception as e:
            logger.exception("read failed for %s", url)
            return f"Could not read {url}: {e}"

        source = book.title if book else path.split("/")[2]
        title = (document and document.title) or page.title or unquote(path.rsplit("/", 1)[-1])
        header = [f"# {title}", f"Source: {source} · url: {path}"]
        text = page.text
        if not text:
            return "\n".join([*header, "", "This page has no readable text."])

        if find:
            passages = find_passages(text, find)
            if not passages:
                return "\n".join(
                    [
                        *header,
                        "",
                        f'No passages about "{find}" in this document. '
                        "Try other words, or read it from the start without `find`.",
                    ]
                )
            lines = [*header, f'Passages about "{find}" ({len(text):,} characters in total):']
            for n, p in enumerate(passages, start=1):
                lines += [f"\n## Passage {n} (offset {p.offset})", p.text]
            lines.append("\nTo read on from a passage, call read_library with its offset.")
            return "\n".join(lines)

        offset = max(0, min(offset, len(text)))
        end = min(len(text), offset + config.page_chars)
        if end < len(text):  # end the part on a paragraph or line break where one is close
            cut = max(text.rfind("\n\n", offset, end), text.rfind("\n", offset, end))
            if cut > offset + config.page_chars // 2:
                end = cut
        lines = [*header, "", text[offset:end].strip(), ""]
        if end < len(text):
            lines.append(
                f"[Characters {offset:,}–{end:,} of {len(text):,}. "
                f"To continue, call read_library with offset={end}.]"
            )
        elif offset:
            lines.append(f"[End of document. Characters {offset:,}–{end:,}.]")
        return "\n".join(lines)

    return mcp
