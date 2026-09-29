"""Turning library content into compact text an LLM can read.

Pages are converted to markdown with links and images dropped: the search tool is
how a model moves around, and every link costs tokens on a small context window.
"""

import io
import logging
import re
from dataclasses import dataclass

from bs4 import BeautifulSoup, Tag
from markdownify import markdownify
from pypdf import PdfReader

# pypdf warns at length about fonts it cannot fully decode; the text still comes out.
logging.getLogger("pypdf").setLevel(logging.ERROR)

#: Chrome, navigation and apparatus that never helps answer a question.
_REMOVE = ",".join(
    [
        "script",
        "style",
        "noscript",
        "nav",
        "header",
        "footer",
        "form",
        "iframe",
        "svg",
        "button",
        "input",
        ".mw-editsection",
        ".mw-jump-link",
        ".navbox",
        ".vertical-navbox",
        ".navbox-styles",
        "#toc",
        ".toc",
        ".catlinks",
        ".printfooter",
        ".noprint",
        "#siteSub",
        "#contentSub",
        ".subpages",
        "sup.reference",
        "ol.references",
        ".reflist",
        ".mw-references-wrap",
        ".left-sidebar",
        "#sidebar",
        ".js-post-menu",
        ".post-signature",
        ".comments",
        ".js-comments-container",
    ]
)
_ROOTS = ("#mw-content-text", "#mainbar", "main", "article", "#content", "body")
_STOPWORDS = frozenset(
    "and are can for from has have how its not that the their this was what when "
    "where which who why will with without you your".split()
)


@dataclass(frozen=True)
class Page:
    title: str
    text: str


def _markdown(node: Tag | str) -> str:
    md = markdownify(str(node), heading_style="ATX", strip=["a", "img"], bullets="-")
    md = re.sub(r"[ \t]+\n", "\n", md)
    md = re.sub(r"\n{3,}", "\n\n", md)
    return md.strip()


def _vote(post: Tag) -> str:
    count = post.select_one(".js-vote-count")
    return (count.get("data-value") or count.get_text(strip=True)) if count else "?"


def _stack_exchange(soup: BeautifulSoup) -> str | None:
    question = soup.select_one("#question")
    body = question.select_one(".js-post-body") if question else None
    if question is None or body is None:
        return None
    tags = [t.get_text(strip=True) for t in soup.select("#question .post-tag")]
    parts = [f"## Question (score {_vote(question)})", _markdown(body)]
    if tags:
        parts.append("Tags: " + ", ".join(tags))
    for answer in soup.select(".answer"):
        answer_body = answer.select_one(".js-post-body")
        if answer_body is None:
            continue
        accepted = (
            "accepted-answer" in (answer.get("class") or [])
            or answer.get("itemprop") == "acceptedAnswer"
        )
        label = f"score {_vote(answer)}" + (", accepted" if accepted else "")
        parts += [f"## Answer ({label})", _markdown(answer_body)]
    return "\n\n".join(parts)


def extract_html(raw: bytes | str) -> Page:
    soup = BeautifulSoup(raw, "lxml")
    heading = soup.select_one("#question-header h1, h1#firstHeading, h1")
    title = heading.get_text(" ", strip=True) if heading else ""
    if not title and soup.title:
        title = soup.title.get_text(strip=True)
    for node in soup.select(_REMOVE):
        node.decompose()

    text = _stack_exchange(soup)
    if text is None:
        root = next((r for sel in _ROOTS if (r := soup.select_one(sel)) is not None), soup)
        text = _markdown(root)
    return Page(title=title, text=text)


def extract_pdf(data: bytes) -> Page:
    reader = PdfReader(io.BytesIO(data))
    meta_title = (reader.metadata.title if reader.metadata else None) or ""
    pages = []
    for number, page in enumerate(reader.pages, start=1):
        text = re.sub(r"[ \t]+\n", "\n", page.extract_text() or "").strip()
        if text:
            pages.append(f"[page {number}]\n{text}")
    return Page(title=str(meta_title).strip(), text="\n\n".join(pages))


#: Dot or underscore leaders, as on a table of contents line.
_LEADER = re.compile(r"(?:\.\s?){6,}|_{6,}")


@dataclass(frozen=True)
class Passage:
    offset: int
    text: str


def _snap(text: str, start: int, end: int) -> tuple[int, int]:
    """Widen [start, end) to whitespace so a passage does not begin or end mid-word."""
    start = max(0, start)
    end = min(len(text), end)
    while start > 0 and not text[start - 1].isspace():
        start -= 1
    while end < len(text) and not text[end].isspace():
        end += 1
    return start, end


def find_passages(text: str, query: str, limit: int = 5, width: int = 1400) -> list[Passage]:
    """The passages of `text` that best match `query`.

    Occurrences of the exact phrase come first. The rest of the places are filled
    by occurrences of any query word, scored by how many *distinct* query words sit
    within the same window, so "tourniquet application" prefers a paragraph that
    mentions both over one that mentions either. Either way, a match on a table of
    contents line (dot leaders) ranks below one in the body text it points to.
    """
    lowered = text.lower()
    phrase = " ".join(query.lower().split())
    if not phrase:
        return []
    half = width // 2

    def in_contents(pos: int) -> bool:
        return _LEADER.search(lowered, max(0, pos - 80), pos + 160) is not None

    # (is the phrase, is body text, distinct query words nearby, -position)
    anchors = [
        (1, not in_contents(m.start()), 0, -m.start())
        for m in re.finditer(re.escape(phrase), lowered)
    ]
    words = [
        w for w in re.findall(r"[\w']+", phrase) if len(w) > 2 and w not in _STOPWORDS
    ] or phrase.split()
    for word in words:
        for m in re.finditer(r"\b" + re.escape(word), lowered):
            window = lowered[max(0, m.start() - half) : m.start() + half]
            nearby = sum(1 for w in words if w in window)
            anchors.append((0, not in_contents(m.start()), nearby, -m.start()))

    anchors.sort(reverse=True)
    chosen: list[tuple[int, int]] = []
    for *_, neg_pos in anchors:
        pos = -neg_pos
        start, end = _snap(text, pos - half, pos + half)
        if all(end <= s or start >= e for s, e in chosen):
            chosen.append((start, end))
        if len(chosen) == limit:
            break
    return [Passage(offset=s, text=text[s:e].strip()) for s, e in chosen]
