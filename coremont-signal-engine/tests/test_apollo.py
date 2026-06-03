"""Tests for Apollo firm-name inference and people-response parsing."""
from app.ingestion.apollo import ApolloClient, firm_name_guess


def test_firm_name_guess_strips_strategy_and_vehicle_words():
    assert firm_name_guess("AQR Multi-Strategy Fund XIX, L.P.") == "AQR"
    assert firm_name_guess("Kirkoswald Global Macro Fund Ltd") == "Kirkoswald"
    assert firm_name_guess("Garda Fixed Income Relative Value Opportunity Fund Ltd.") == "Garda"
    assert firm_name_guess("Lighthouse Multi-Strategy Fund Ltd") == "Lighthouse"


def test_firm_name_guess_keeps_multiword_brands():
    # Two-word brand before a strategy word is preserved.
    assert firm_name_guess("Cole Harbor Rates & Relative Value Fund LP").startswith("Cole Harbor")


def test_firm_name_guess_ignores_sample_prefix():
    assert firm_name_guess("SAMPLE — Meridian Structured Credit Master Fund LP") == "Meridian"


def test_parse_people_handles_name_variants_and_skips_blank():
    payload = {
        "people": [
            {"name": "Jane Roe", "title": "Chief Operating Officer",
             "email": "jane@firm.com", "linkedin_url": "https://linkedin.com/in/janeroe"},
            {"first_name": "John", "last_name": "Doe", "title": "Treasurer"},
            {"title": "no name here"},  # skipped
        ]
    }
    people = ApolloClient.parse_people(payload)
    assert len(people) == 2
    assert people[0]["name"] == "Jane Roe"
    assert people[0]["email"] == "jane@firm.com"
    assert people[1]["name"] == "John Doe"
    assert people[1]["title"] == "Treasurer"
