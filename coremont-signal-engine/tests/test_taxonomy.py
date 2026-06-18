from app import taxonomy


def test_strong_terms_match_and_weight():
    m = taxonomy.match_text("Meridian Structured Credit Master Fund")
    assert "structured credit" in m.matched
    assert m.positive_weight >= 15
    assert "structured credit" in m.tags


def test_short_tokens_respect_word_boundaries():
    # "abs" should not match inside "absolute"; "clo" not inside "close".
    m = taxonomy.match_text("absolute return close-ended")
    assert "abs" not in m.matched
    assert "clo" not in m.matched


def test_acronyms_match_as_words():
    m = taxonomy.match_text("RMBS, CMBS and CLO securitized exposures")
    for term in ("rmbs", "cmbs", "clo", "securitized"):
        assert term in m.matched


def test_negative_terms_penalise():
    m = taxonomy.match_text("Northpath Venture Growth Fund")
    assert m.negative_weight < 0
    assert "venture" in m.matched


def test_multiword_phrase_beats_single_word():
    m = taxonomy.match_text("global macro strategy")
    # both 'macro' and 'global macro' patterns can fire; the phrase must be present.
    assert "global macro" in m.matched
