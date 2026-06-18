"""Discovery probe for the SEC Form ADV "Part 1A + all Schedules" data set,
specifically the Schedule D Section 7.B.(1) private-fund table (administrator /
auditor / custodian / prime broker / gross asset value, keyed by FUND name).

Runs on a GitHub runner (reaches sec.gov). The dataset's download URLs are not
documented where we can read them, so this probe scrapes the adviserinfo data
pages for download links, then downloads a likely Schedule-D file and dumps the
file list + the 7B1 table columns + a sample row. Writes briefs/sched_d_schema.txt.
"""

from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path
from urllib.parse import urljoin

import urllib.request

UA = "Coremont Clarion Prospecting malex.clark@gmail.com"
PAGES = [
    "https://adviserinfo.sec.gov/compilation",
    "https://adviserinfo.sec.gov/adv",
]
LINK_RE = re.compile(r"""href=["']([^"']+\.(?:zip|csv|gz|xlsx))["']""", re.I)


def _get(url: str, timeout: int = 120) -> bytes | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except Exception as e:  # noqa: BLE001
        print(f"  fetch failed: {url} -> {e}")
        return None


def _links_from(url: str) -> list[str]:
    raw = _get(url, timeout=60)
    if not raw:
        return []
    html = raw.decode("latin-1", errors="replace")
    return sorted({urljoin(url, m) for m in LINK_RE.findall(html)})


def _dump_zip(name: str, blob: bytes, out: list[str]) -> None:
    try:
        zf = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile:
        out.append(f"  (not a zip; first bytes {blob[:60]!r})")
        return
    for info in zf.infolist():
        out.append(f"  FILE {info.filename} ({info.file_size:,} bytes)")
        low = info.filename.lower()
        if low.endswith(".csv") and any(k in low for k in ("7b", "schedule_d", "fund", "base")):
            try:
                data = zf.read(info.filename)[:200_000].decode("latin-1", "replace")
                header = data.splitlines()[0] if data else ""
                cols = [c.strip().strip('"') for c in header.split(",")]
                out.append(f"    {len(cols)} columns; relevant:")
                for c in cols:
                    if any(k in c.lower() for k in ("fund", "admin", "audit", "custod", "prime", "gross", "asset", "name", "crd", "7b")):
                        out.append(f"      - {c}")
            except Exception as e:  # noqa: BLE001
                out.append(f"    (header read error: {e})")


def main() -> None:
    out: list[str] = ["Schedule D 7B(1) data-set discovery", "=" * 60]
    all_links: list[str] = []
    for page in PAGES:
        out.append(f"\n## links on {page}")
        links = _links_from(page)
        for ln in links:
            out.append(f"  {ln}")
        all_links += links

    # Try to download something that looks like the Schedule-D / base dataset.
    candidates = [
        ln for ln in all_links
        if any(k in ln.lower() for k in ("schedule_d", "7b", "ia_adv_base", "adv_base", "feed", "complete"))
    ] or all_links[:3]
    out.append("\n## inspecting candidate dataset files")
    for ln in candidates[:4]:
        out.append(f"\n-- {ln}")
        blob = _get(ln)
        if not blob:
            continue
        out.append(f"   downloaded {len(blob):,} bytes")
        if ln.lower().endswith(".zip"):
            _dump_zip(ln, blob, out)
        elif ln.lower().endswith(".csv"):
            head = blob[:4000].decode("latin-1", "replace").splitlines()[:1]
            out.append(f"   csv header: {head}")

    text = "\n".join(out)
    print(text)
    try:
        dest = Path(__file__).resolve().parents[1] / "briefs" / "sched_d_schema.txt"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")
        print(f"\nwrote {dest}")
    except OSError as e:
        print(f"(write error: {e})")


if __name__ == "__main__":
    main()
