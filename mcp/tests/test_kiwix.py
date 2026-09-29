import pytest
from conftest import NAUTILUS_BOOK, fixture

from wiki_mcp.kiwix import (
    Book,
    KiwixClient,
    KiwixError,
    content_path,
    match_documents,
    parse_catalog,
    parse_nautilus,
    parse_search,
    parse_suggest,
)

MEDICAL = Book(id=NAUTILUS_BOOK, title="Medical Library", fulltext=False)


def test_catalog_lists_books_with_their_content_ids_and_index_flag() -> None:
    books = {b.id: b for b in parse_catalog(fixture("catalog.xml"))}
    assert books["wikibooks_en_all_maxi_2026-04"].title == "Wikibooks"
    assert books["wikibooks_en_all_maxi_2026-04"].fulltext
    assert not books["ifixit_en_all_2025-12"].fulltext


def test_search_results_carry_title_source_url_and_a_plain_snippet() -> None:
    hits, total = parse_search(fixture("search.xml"))
    assert total == 600
    assert [h.source for h in hits] == ["Bicycles Q&A", "Wikibooks", "Bicycles Q&A"]
    assert hits[1].url.startswith("/content/wikibooks_en_all_maxi_2026-04/Bicycles/")
    assert "<b>" not in hits[1].snippet and "chain" in hits[1].snippet


def test_suggestions_become_hits_with_escaped_paths() -> None:
    book = Book(id="ifixit_en_all_2025-12", title="iFixit", fulltext=False)
    hits = parse_suggest(fixture("suggest.json"), book)
    assert hits[0].title == "Kettle"
    assert hits[0].url == "/content/ifixit_en_all_2025-12/Device/Bouilloire"


def test_nautilus_database_lists_pdfs_under_files() -> None:
    docs = parse_nautilus(fixture("nautilus-database.js"), MEDICAL)
    assert docs[0].title == "Emergency War Surgery"
    assert docs[0].url == (
        f"/content/{NAUTILUS_BOOK}/files/First%20Aid%20and%20Medicine%20%281%29.pdf"
    )


def test_a_page_that_is_not_a_nautilus_database_yields_nothing() -> None:
    assert parse_nautilus("<html>not found</html>", MEDICAL) == []


def test_documents_match_on_title_before_description() -> None:
    docs = parse_nautilus(fixture("nautilus-database.js"), MEDICAL)
    hits = match_documents(docs, "how do I close a wound?", limit=3)
    assert hits[0].title == "Wound Closure Manual"
    assert hits[0].source == "Medical Library (PDF)"


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("/content/wikibooks_x/Page", "/content/wikibooks_x/Page"),
        ("content/wikibooks_x/Page", "/content/wikibooks_x/Page"),
        ("https://wiki.ax-h.com/content/wikibooks_x/Page?x=1", "/content/wikibooks_x/Page"),
    ],
)
def test_content_path_accepts_paths_and_full_urls(given: str, expected: str) -> None:
    assert content_path(given) == expected


@pytest.mark.parametrize("bad", ["/catalog/v2/entries", "/content/../catalog", "http://evil/x"])
def test_content_path_refuses_anything_outside_content(bad: str) -> None:
    with pytest.raises(KiwixError):
        content_path(bad)


async def test_search_merges_fulltext_titles_and_documents(kiwix: KiwixClient) -> None:
    results = await kiwix.search("kettle wound", 10)
    assert results.text_total == 600
    assert any(h.title == "Kettle" for h in results.title_hits)
    assert any(h.title == "Wound Closure Manual" for h in results.document_hits)
    # A nautilus library is searched through its database, never through kiwix's suggestions.
    assert all(NAUTILUS_BOOK not in h.url for h in results.title_hits)


async def test_title_matches_already_in_the_article_list_are_not_repeated(
    kiwix: KiwixClient,
) -> None:
    results = await kiwix.search("kettle", 10)
    text_urls = {h.url for h in results.text_hits}
    assert not text_urls & {h.url for h in results.title_hits}


async def test_a_pdf_is_known_by_its_catalog_title_however_its_url_is_escaped(
    kiwix: KiwixClient,
) -> None:
    for url in (
        f"/content/{NAUTILUS_BOOK}/files/First%20Aid%20and%20Medicine%20%281%29.pdf",
        f"/content/{NAUTILUS_BOOK}/files/First%20Aid%20and%20Medicine%20(1).pdf",
    ):
        doc = await kiwix.document_for(url)
        assert doc is not None and doc.title == "Emergency War Surgery"
