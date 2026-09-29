import pytest
from conftest import NAUTILUS_BOOK, fixture

from wiki_mcp.kiwix import (
    Book,
    Document,
    KiwixClient,
    KiwixError,
    content_path,
    match_documents,
    parse_catalog,
    parse_nautilus,
    parse_search,
    parse_suggest,
    title_candidates,
    title_matches,
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


def test_a_total_formatted_with_thousands_separators_still_parses() -> None:
    xml = fixture("search.xml").replace(
        "<opensearch:totalResults>600<", "<opensearch:totalResults>40,000<"
    )
    hits, total = parse_search(xml)
    assert total == 40_000 and len(hits) == 3


def test_documents_match_whole_words_and_their_plurals_only() -> None:
    def doc(title: str) -> Document:
        return Document(book=MEDICAL, title=title, description="", author="", url=f"/x/{title}")

    docs = [doc("Water Treatment"), doc("Burn Care")]
    assert [h.title for h in match_documents(docs, "treat a burn", limit=5)] == ["Burn Care"]
    assert [h.title for h in match_documents(docs, "burns", limit=5)] == ["Burn Care"]


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("treat a burn", ["Treat burn", "Treat", "Burn"]),
        ("car battery not charging", ["Car battery charging", "Car battery", "Battery charging"]),
        ("hypothermia symptoms", ["Hypothermia symptoms", "Hypothermia", "Symptoms"]),
        ("nosebleed", ["Nosebleed"]),
    ],
)
def test_title_candidates_try_phrases_then_single_words_for_short_queries(
    query: str, expected: list[str]
) -> None:
    assert title_candidates(query) == expected


async def test_the_wikipedia_article_named_by_the_query_comes_first(kiwix: KiwixClient) -> None:
    results = await kiwix.search("treat a burn", 10)
    first = results.reference_hits[0]
    assert (first.title, first.source) == ("Burn", "Wikipedia")
    assert first.snippet.startswith("A burn is an injury")
    # "Treat" is a disambiguation page, so it is left out.
    assert all(h.title != "Treat" for h in results.reference_hits)
    # "First aid" came back from the per-book search, but its title shares no keyword.
    assert [h.title for h in results.reference_hits] == ["Burn"]


async def test_a_generic_title_match_must_mention_the_other_keywords(kiwix: KiwixClient) -> None:
    titles = [h.title for h in (await kiwix.search("hypothermia symptoms", 10)).reference_hits]
    assert "Hypothermia" in titles
    assert "Symptom" not in titles  # the Symptom article never mentions hypothermia


async def test_reference_hits_are_not_repeated_in_the_general_list(kiwix: KiwixClient) -> None:
    results = await kiwix.search("treat a burn", 10)
    references = {h.url for h in results.reference_hits}
    assert not references & {h.url for h in results.text_hits}


@pytest.mark.parametrize(
    ("title", "matched"),
    [
        ("First Aid/Burns", 1),
        ("Self-Reliance Handbook/Purifying Water", 2),
        ("Muggles' Guide to Harry Potter/Magic/Blood Blisterpod", 0),
    ],
)
def test_title_matches_counts_keywords_allowing_endings(title: str, matched: int) -> None:
    assert title_matches(title, ["burn", "purify", "water"]) == matched
