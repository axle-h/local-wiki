from conftest import fixture

from wiki_mcp.extract import extract_html, find_passages


def test_mediawiki_page_keeps_the_article_and_drops_the_chrome() -> None:
    page = extract_html(fixture("mediawiki.html"))
    assert page.title == "Bicycles/Maintenance and Repair/Chains/Checking chain wear"
    assert "Chains increase in length over time" in page.text
    assert "<" not in page.text  # no HTML left
    assert "](" not in page.text  # links are stripped to their text


def test_stack_exchange_page_is_question_then_scored_answers() -> None:
    page = extract_html(fixture("stackexchange.html"))
    assert page.title == "Bicycle keeps 'skipping a beat'"
    assert page.text.startswith("## Question (score 1)")
    assert "## Answer (score 3)" in page.text
    assert page.text.index("## Question") < page.text.index("## Answer")


def test_exact_phrase_wins() -> None:
    text = "tourniquet here. " + "filler " * 400 + "apply the tourniquet firmly above the wound."
    passages = find_passages(text, "the tourniquet firmly", limit=1, width=100)
    assert "tourniquet firmly" in passages[0].text


def test_without_the_phrase_the_window_with_most_query_words_wins() -> None:
    text = "wound " * 5 + "filler " * 400 + "clean the wound then apply a dressing" + " filler" * 50
    passages = find_passages(text, "wound dressing", limit=1, width=120)
    assert "dressing" in passages[0].text and "wound" in passages[0].text


def test_passages_do_not_overlap_and_offsets_point_into_the_text() -> None:
    text = ("boil water for one minute. " + "x " * 800) * 4
    passages = find_passages(text, "boil water", limit=3, width=200)
    assert len(passages) == 3
    spans = sorted((p.offset, p.offset + len(p.text)) for p in passages)
    assert all(a_end <= b_start for (_, a_end), (b_start, _) in zip(spans, spans[1:], strict=False))
    assert all(text[p.offset :].startswith(p.text[:20]) for p in passages)


def test_no_match_returns_nothing() -> None:
    assert find_passages("nothing relevant here", "tourniquet") == []


def test_phrase_matches_come_first_then_the_best_word_matches_fill_up() -> None:
    text = "apply a tourniquet. " + "x " * 800 + "tourniquet application here. " + "y " * 800
    passages = find_passages(text, "tourniquet application", limit=2, width=100)
    assert len(passages) == 2
    assert "tourniquet application" in passages[0].text
    assert "apply a tourniquet" in passages[1].text


def test_a_contents_line_ranks_below_the_body_text_it_points_to() -> None:
    text = (
        "2-5. Pressure dressing ............................ 2-11\n"
        + "x " * 800
        + "Apply a pressure dressing over the wound and bind it firmly."
    )
    passages = find_passages(text, "pressure dressing", limit=2, width=100)
    assert "Apply a pressure dressing" in passages[0].text
