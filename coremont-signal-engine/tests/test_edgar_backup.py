"""Tests for the deterministic daily-index backup discovery path."""
from app.ingestion import edgar_client as ec


def _blank_record(acc: str, cik: str) -> ec.FormDRecord:
    return ec.FormDRecord(
        accession_no=acc, cik=cik, issuer_name="x", jurisdiction=None,
        entity_type=None, hq_city=None, hq_state=None, filing_date=None,
        first_sale_date=None, is_amendment=False, industry_group=None,
        investment_fund_type=None, offering_amount=None, amount_sold=None,
        remaining_amount=None,
    )


def test_name_matches_icp_screen():
    assert ec.name_matches_icp("AQR Multi-Strategy Fund XIX, L.P.")
    assert ec.name_matches_icp("Garda Fixed Income Relative Value Opportunity Fund")
    assert ec.name_matches_icp("Kirkoswald Global Macro Fund Ltd")
    # Out-of-ICP names are screened out.
    assert not ec.name_matches_icp("Smith Family Holdings LLC")
    assert not ec.name_matches_icp("Northpath Venture Growth Fund")


def test_index_backup_filters_by_icp_name_and_dedupes():
    client = ec.EdgarClient()
    icp = ec.IndexEntry("D", "AQR Multi-Strategy Fund XIX LP", "12345",
                        "2026-06-01", "0001-26-000001")
    noise = ec.IndexEntry("D", "Smith Family Holdings LLC", "67890",
                          "2026-06-01", "0002-26-000002")
    # Same two entries returned for every requested day → exercises de-dup too.
    client.fetch_daily_index = lambda day: [icp, noise]

    fetched: list[str] = []

    def fake_form_d(cik, acc, date_filed=None):
        fetched.append(acc)
        return _blank_record(acc, cik)

    client.fetch_form_d = fake_form_d
    records = client.fetch_form_d_by_index(days=5)
    client.close()

    # Only the ICP-named issuer is downloaded, exactly once across all days.
    assert fetched == [icp.accession_no]
    assert len(records) == 1
    assert records[0].cik == "12345"
