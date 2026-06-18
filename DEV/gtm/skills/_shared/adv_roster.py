"""SEC Form ADV firm roster — the authoritative ADV data source.

Form ADV is NOT on EDGAR and the public adviserinfo API exposes no AUM
(detail endpoints are 403-gated). The SEC instead publishes a monthly FOIA
CSV of every SEC-registered adviser with full Item 5 data:
https://www.sec.gov/foia/docs/invafoia

Drop the latest "Firm Roster" CSV into data/adv/ (any *.csv; newest mtime
wins) — refresh monthly. Lookup precedence: CRD > CIK > normalized name.

Known gap: exempt reporting advisers (many sub-$150M launches) are in a
separate ERA file; add it alongside when needed — same schema family.
"""

from __future__ import annotations

import csv
import re
from pathlib import Path

from gtm.skills._shared.sources import AdvProfile

DATA_DIR = Path(__file__).resolve().parents[3] / "data" / "adv"

_SOCIAL = ("instagram.", "linkedin.", "twitter.", "x.com", "facebook.", "youtube.")
_NAME_NOISE = re.compile(r"[^A-Z0-9 ]")
# Only legal-form suffixes: stripping business words (MANAGEMENT, CAPITAL...)
# makes distinct firms collide ("Millennium Management" vs "Millennium Advisors")
_SUFFIXES = (" LLC", " LLP", " LP", " L P", " LTD", " LIMITED", " INC", " CORP")


def _norm_name(name: str) -> str:
    text = _NAME_NOISE.sub("", name.upper().replace(".", " ").replace(",", " "))
    text = " ".join(text.split())
    changed = True
    while changed:
        changed = False
        for suffix in _SUFFIXES:
            if text.endswith(suffix):
                text = text[: -len(suffix)].strip()
                changed = True
    return text


def _money(value: str | None) -> float | None:
    if not value:
        return None
    text = value.replace(",", "").replace("$", "").strip()
    if not text or text == ".00":
        return 0.0
    try:
        return float(text)
    except ValueError:
        return None


def _website(value: str | None) -> str | None:
    if not value:
        return None
    url = value.strip().lower()
    if any(s in url for s in _SOCIAL):
        return None
    host = re.sub(r"^https?://", "", url).split("/")[0]
    return host.removeprefix("www.") or None


class AdvRoster:
    """Lazy-loaded, indexed view of the monthly ADV firm roster CSV."""

    def __init__(self, csv_path: str | Path | None = None) -> None:
        self.csv_path = Path(csv_path) if csv_path else self._newest_csv()
        self._by_crd: dict[str, dict] = {}
        self._by_cik: dict[str, dict] = {}
        self._by_name: dict[str, dict] = {}
        self._loaded = False

    @staticmethod
    def _newest_csv() -> Path | None:
        if not DATA_DIR.exists():
            return None
        candidates = sorted(DATA_DIR.glob("*.csv"), key=lambda p: p.stat().st_mtime)
        return candidates[-1] if candidates else None

    @property
    def available(self) -> bool:
        return self.csv_path is not None and Path(self.csv_path).exists()

    def _load(self) -> None:
        if self._loaded or not self.available:
            self._loaded = True
            return
        with open(self.csv_path, encoding="utf-8-sig", errors="replace", newline="") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                crd = (row.get("Organization CRD#") or "").strip()
                if not crd:
                    continue
                self._by_crd[crd] = row
                cik = (row.get("CIK#") or "").strip().lstrip("0")
                if cik:
                    self._by_cik[cik] = row
                for name_col in ("Primary Business Name", "Legal Name"):
                    key = _norm_name(row.get(name_col) or "")
                    if key:
                        self._by_name.setdefault(key, row)
        self._loaded = True

    def lookup(
        self,
        crd: str | None = None,
        cik: str | None = None,
        name: str | None = None,
    ) -> AdvProfile | None:
        self._load()
        row = None
        if crd:
            row = self._by_crd.get(str(crd).strip())
        if row is None and cik:
            row = self._by_cik.get(str(cik).strip().lstrip("0"))
        if row is None and name:
            row = self._by_name.get(_norm_name(name))
        if row is None:
            return None

        raum = _money(row.get("5F(2)(c)"))
        filed = (row.get("Latest ADV Filing Date") or "").strip()
        aum_as_of = None
        if re.fullmatch(r"\d{2}/\d{2}/\d{4}", filed):
            month, day, year = filed.split("/")
            aum_as_of = f"{year}-{month}-{day}"

        return AdvProfile(
            crd=(row.get("Organization CRD#") or "").strip(),
            firm_name=(row.get("Primary Business Name") or row.get("Legal Name") or "").strip(),
            regulatory_aum_usd=raum / 1_000_000 if raum else None,
            aum_as_of=aum_as_of,
            website=_website(row.get("Website Address")),
            headquarters_city=(row.get("Main Office City") or "").strip() or None,
            headquarters_country=(row.get("Main Office Country") or "").strip() or None,
            raw={
                "cik": (row.get("CIK#") or "").strip(),
                "sec_number": (row.get("SEC#") or "").strip(),
                "sec_status": (row.get("SEC Current Status") or "").strip(),
                "roster_file": str(self.csv_path.name),
            },
        )
