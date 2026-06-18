"""Connectivity probe: which SEC endpoints are reachable from this environment?

Run this where the scheduled agent runs (the cloud sandbox) to learn whether the
403 on www.sec.gov is IP-based or User-Agent-based, and whether the CDN-fronted
hosts (data.sec.gov, efts.sec.gov) are reachable when www.sec.gov is not.

Pure stdlib (urllib) — no edgartools needed. Writes briefs/connectivity.txt and
prints a table. Exit code is always 0 (this is a diagnostic).
"""

from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.request
from pathlib import Path

UA = os.environ.get("EDGAR_IDENTITY", "Coremont Clarion Prospecting malex.clark@gmail.com")

# (label, url, use_identity_user_agent)
PROBES = [
    ("www Archives full-index (the 403'd path), proper UA",
     "https://www.sec.gov/Archives/edgar/full-index/2026/QTR2/", True),
    ("www Archives full-index, NO custom UA (tests UA vs IP)",
     "https://www.sec.gov/Archives/edgar/full-index/2026/QTR2/", False),
    ("www browse-edgar current Form D (atom)",
     "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=D&output=atom&count=10", True),
    ("data.sec.gov submissions JSON (Apple)",
     "https://data.sec.gov/submissions/CIK0000320193.json", True),
    ("efts.sec.gov full-text search API (Form D)",
     "https://efts.sec.gov/LATEST/search-index?q=%22fund%22&forms=D&startdt=2026-06-01&enddt=2026-06-03", True),
    ("efts.sec.gov full-text search API (no UA)",
     "https://efts.sec.gov/LATEST/search-index?q=%22fund%22&forms=D", False),
]


def probe(url: str, use_ua: bool) -> str:
    headers = {"Accept-Encoding": "gzip, deflate", "Accept": "*/*"}
    if use_ua:
        headers["User-Agent"] = UA
    else:
        headers["User-Agent"] = "python-urllib/3"
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = resp.read(400)
            return f"OK {resp.status}  ({len(body)}+ bytes; {body[:60]!r})"
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code} {e.reason}"
    except urllib.error.URLError as e:
        return f"URLError {e.reason}"
    except (socket.timeout, TimeoutError):
        return "TIMEOUT"
    except Exception as e:  # noqa: BLE001
        return f"{type(e).__name__}: {e}"


def main() -> None:
    lines = [f"SEC connectivity probe  (UA = {UA!r})", "=" * 72]
    results = []
    for label, url, use_ua in PROBES:
        status = probe(url, use_ua)
        results.append({"label": label, "url": url, "result": status})
        lines.append(f"[{status.split()[0]:>8}] {label}\n           {url}\n           -> {status}")
    out = "\n".join(lines)
    print(out)
    try:
        dest = Path(__file__).resolve().parents[1] / "briefs" / "connectivity.txt"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(out + "\n\nJSON:\n" + json.dumps(results, indent=2), encoding="utf-8")
        print(f"\nwrote {dest}")
    except OSError as e:
        print(f"(could not write file: {e})")


if __name__ == "__main__":
    main()
