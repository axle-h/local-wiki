"""Talking to kiwix-serve: the catalog, search, title suggestions and raw content.

kiwix-serve already ranks a full-text search across every book that has a
full-text index. Two kinds of book fall outside that, and this module covers them:

- Title-only books (no full-text index, e.g. iFixit): searched through kiwix's
  per-book title suggestions.
- "Nautilus" libraries (the zimgit PDF collections): a JavaScript page over a
  `database.js` that lists each PDF's title, description and author. kiwix indexes
  none of it, so the list is loaded here and matched against the query directly.
"""

import ast
import asyncio
import html
import json
import logging
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from urllib.parse import quote, unquote, urlsplit

import httpx

logger = logging.getLogger(__name__)

_ATOM = "{http://www.w3.org/2005/Atom}"
_CATALOG_TTL_SECONDS = 300.0
#: Books whose pages have no readable text; searching their titles only adds noise.
_SKIP_TITLE_SEARCH_PREFIXES = ("maps_",)
_MAX_CONTENT_BYTES = 150 * 1024 * 1024
_STOPWORDS = frozenset(
    "a an and are as at be by can do for from how i in is it my of on or the to what "
    "when where which who why with without you your".split()
)


@dataclass(frozen=True)
class Book:
    #: The id kiwix serves the book under, e.g. `wikibooks_en_all_maxi_2026-04`.
    id: str
    title: str
    fulltext: bool


@dataclass(frozen=True)
class Document:
    """One file listed in a nautilus library's `database.js`."""

    book: Book
    title: str
    description: str
    author: str
    url: str


@dataclass(frozen=True)
class Hit:
    title: str
    source: str
    url: str
    snippet: str = ""


@dataclass
class SearchResults:
    text_hits: list[Hit] = field(default_factory=list)
    text_total: int = 0
    title_hits: list[Hit] = field(default_factory=list)
    document_hits: list[Hit] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Content:
    url: str
    content_type: str
    body: bytes


class KiwixError(Exception):
    pass


def _clean_snippet(raw: str) -> str:
    text = re.sub(r"<[^>]+>", "", html.unescape(raw))
    text = re.sub(r"\s+", " ", text).strip(" .")
    return text


def parse_catalog(xml: str) -> list[Book]:
    root = ET.fromstring(xml)
    books = []
    for entry in root.iter(f"{_ATOM}entry"):
        href = next(
            (
                link.get("href", "")
                for link in entry.iter(f"{_ATOM}link")
                if link.get("type") == "text/html"
            ),
            "",
        )
        if not href.startswith("/content/"):
            continue
        tags = (entry.findtext(f"{_ATOM}tags") or "").split(";")
        books.append(
            Book(
                id=href.removeprefix("/content/").strip("/"),
                title=(entry.findtext(f"{_ATOM}title") or "").strip(),
                fulltext="_ftindex:yes" in tags,
            )
        )
    return books


def parse_search(xml: str) -> tuple[list[Hit], int]:
    root = ET.fromstring(xml)
    channel = root.find("channel")
    if channel is None:
        return [], 0
    total = int(channel.findtext("{http://a9.com/-/spec/opensearch/1.1/}totalResults") or 0)
    hits = [
        Hit(
            title=(item.findtext("title") or "").strip(),
            source=(item.findtext("book/title") or "").strip(),
            url=(item.findtext("link") or "").strip(),
            # kiwix's <b> highlights arrive as real child elements, not escaped text.
            snippet=_clean_snippet(
                "".join(d.itertext()) if (d := item.find("description")) is not None else ""
            ),
        )
        for item in channel.iter("item")
    ]
    return hits, total


def parse_suggest(raw: str, book: Book) -> list[Hit]:
    # kiwix emits the list with its own formatting; it is valid JSON.
    return [
        Hit(
            title=html.unescape(s.get("value", "")).strip(),
            source=book.title,
            url=f"/content/{book.id}/{quote(s['path'], safe='/')}",
        )
        for s in json.loads(raw)
        if s.get("kind") == "path" and s.get("path")
    ]


def parse_nautilus(js: str, book: Book) -> list[Document]:
    """Read `var DATABASE = [{...}, ...];`, which is a Python-compatible literal."""
    start, end = js.find("["), js.rfind("]")
    if not js.lstrip().startswith("var DATABASE") or start < 0 or end < start:
        return []
    try:
        rows = ast.literal_eval(js[start : end + 1])
    except (ValueError, SyntaxError) as e:
        logger.warning("could not parse %s/database.js: %s", book.id, e)
        return []
    docs = []
    for row in rows:
        files = row.get("fp") or []
        if not files:
            continue
        docs.append(
            Document(
                book=book,
                title=str(row.get("ti", "")).strip(),
                description=str(row.get("dsc", "")).strip(),
                author=str(row.get("aut", "")).strip(),
                url=f"/content/{book.id}/files/{quote(files[0])}",
            )
        )
    return docs


def query_terms(query: str) -> list[str]:
    words = re.findall(r"[\w']+", query.lower())
    return [w for w in words if len(w) > 2 and w not in _STOPWORDS] or words


def match_documents(docs: list[Document], query: str, limit: int) -> list[Hit]:
    terms = query_terms(query)
    scored = []
    for doc in docs:
        title, rest = doc.title.lower(), f"{doc.description} {doc.author}".lower()
        score = sum(2 * (t in title) + (t in rest) for t in terms)
        if score:
            scored.append((score, doc))
    scored.sort(key=lambda pair: -pair[0])
    return [
        Hit(
            title=doc.title,
            source=f"{doc.book.title} (PDF)",
            url=doc.url,
            snippet=" — ".join(x for x in (doc.description, doc.author) if x),
        )
        for _, doc in scored[:limit]
    ]


def content_path(url: str) -> str:
    """Normalise whatever the caller passed (a full URL, or a path) to `/content/...`."""
    path = urlsplit(url.strip()).path if "://" in url else url.strip().split("?", 1)[0]
    if not path.startswith("/"):
        path = "/" + path
    if not path.startswith("/content/") or ".." in unquote(path).split("/"):
        raise KiwixError(
            f"Not a library URL: {url!r}. Use a `url` exactly as the search tool returned it "
            "(it starts with /content/)."
        )
    return path


class KiwixClient:
    def __init__(self, base_url: str, client: httpx.AsyncClient | None = None) -> None:
        self._http = client or httpx.AsyncClient(
            base_url=base_url, timeout=httpx.Timeout(30.0, connect=5.0), follow_redirects=True
        )
        self._books: list[Book] = []
        self._books_at = 0.0
        self._books_lock = asyncio.Lock()
        # Books never change under a given id, so a library's document list is kept for good.
        self._nautilus: dict[str, list[Document]] = {}

    async def aclose(self) -> None:
        await self._http.aclose()

    async def books(self) -> list[Book]:
        async with self._books_lock:
            if time.monotonic() - self._books_at > _CATALOG_TTL_SECONDS or not self._books:
                r = await self._http.get("/catalog/v2/entries", params={"count": "-1"})
                r.raise_for_status()
                self._books = parse_catalog(r.text)
                self._books_at = time.monotonic()
            return self._books

    async def book_for(self, path: str) -> Book | None:
        book_id = path.removeprefix("/content/").split("/", 1)[0]
        return next((b for b in await self.books() if b.id == book_id), None)

    async def document_for(self, path: str) -> Document | None:
        """The catalog entry for a nautilus PDF, whose own metadata title is often a chapter's."""
        book = await self.book_for(path)
        if book is None:
            return None
        wanted = unquote(path)
        return next((d for d in await self._documents(book) if unquote(d.url) == wanted), None)

    async def _documents(self, book: Book) -> list[Document]:
        if book.id not in self._nautilus:
            r = await self._http.get(f"/content/{book.id}/database.js")
            self._nautilus[book.id] = parse_nautilus(r.text, book) if r.is_success else []
        return self._nautilus[book.id]

    async def _fulltext(self, query: str, limit: int) -> tuple[list[Hit], int]:
        r = await self._http.get(
            "/search", params={"pattern": query, "format": "xml", "pageLength": str(limit)}
        )
        if r.status_code == 404:  # kiwix's answer to "nothing matched"
            return [], 0
        r.raise_for_status()
        return parse_search(r.text)

    async def _titles(self, book: Book, query: str, limit: int) -> list[Hit]:
        r = await self._http.get(
            "/suggest", params={"content": book.id, "term": query, "count": str(limit)}
        )
        return parse_suggest(r.text, book) if r.is_success else []

    async def search(self, query: str, limit: int) -> SearchResults:
        results = SearchResults()
        books = await self.books()
        title_only = [
            b for b in books if not b.fulltext and not b.id.startswith(_SKIP_TITLE_SEARCH_PREFIXES)
        ]
        docs_per_book = await asyncio.gather(*(self._documents(b) for b in title_only))
        documents = [d for docs in docs_per_book for d in docs]
        suggest_books = [b for b, docs in zip(title_only, docs_per_book, strict=True) if not docs]

        fulltext, *titles = await asyncio.gather(
            self._fulltext(query, limit),
            *(self._titles(b, query, 3) for b in suggest_books),
            return_exceptions=True,
        )
        if isinstance(fulltext, BaseException):
            logger.warning("full-text search failed: %s", fulltext)
            results.errors.append(f"full-text search failed: {fulltext}")
        else:
            results.text_hits, results.text_total = fulltext
        for book, found in zip(suggest_books, titles, strict=True):
            if isinstance(found, BaseException):
                logger.warning("title search in %s failed: %s", book.id, found)
            else:
                results.title_hits.extend(found)
        listed = {h.url for h in results.text_hits}
        results.title_hits = [h for h in results.title_hits if h.url not in listed][:limit]
        results.document_hits = match_documents(documents, query, min(limit, 5))
        return results

    async def fetch(self, path: str) -> Content:
        async with self._http.stream("GET", path) as r:
            if r.status_code == 404:
                raise KiwixError(f"Nothing at {path}. Check the URL against the search results.")
            r.raise_for_status()
            size = int(r.headers.get("content-length") or 0)
            if size > _MAX_CONTENT_BYTES:
                raise KiwixError(f"{path} is {size // 1_000_000} MB, too large to read here.")
            body = await r.aread()
            return Content(
                url=path,
                content_type=r.headers.get("content-type", "").split(";")[0].strip().lower(),
                body=body,
            )
