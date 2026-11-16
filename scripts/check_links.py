#!/usr/bin/env python3
"""
Weekly check of every provider website in data/directory.csv.

Writes a report to the GitHub Actions run summary. The run is marked as
failed (so GitHub emails the repo owner) only when at least one link is
clearly broken: page not found, site gone, or the domain no longer exists.
Sites that block automated checks (403/429) or time out are listed as
"couldn't verify" but don't fail the run.
"""
import csv
import os
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

CSV_PATH = Path(__file__).resolve().parent.parent / "data" / "directory.csv"
TIMEOUT = 20
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; WA-Directory-LinkCheck/1.0)"}
BROKEN_CODES = {404, 410}


def normalize(url):
    url = (url or "").strip()
    if not url:
        return None
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


def check(url):
    """Return (status, detail) where status is ok / broken / unverified."""
    for method in ("HEAD", "GET"):
        try:
            req = urllib.request.Request(url, headers=HEADERS, method=method)
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return "ok", str(resp.status)
        except urllib.error.HTTPError as e:
            if method == "HEAD" and e.code in (403, 405, 429, 501):
                continue  # some servers reject HEAD; retry with GET
            if e.code in BROKEN_CODES:
                return "broken", f"HTTP {e.code} (page not found)"
            if e.code in (401, 403, 429):
                return "unverified", f"HTTP {e.code} (site blocks automated checks)"
            if e.code >= 500:
                return "unverified", f"HTTP {e.code} (site error, may be temporary)"
            return "unverified", f"HTTP {e.code}"
        except urllib.error.URLError as e:
            reason = str(e.reason)
            if "Name or service not known" in reason or "nodename nor servname" in reason \
                    or "No address associated" in reason or "getaddrinfo failed" in reason:
                return "broken", "Domain no longer exists"
            if "CERTIFICATE" in reason.upper() or "SSL" in reason.upper():
                return "unverified", "Security certificate problem"
            return "unverified", f"Could not connect ({reason[:60]})"
        except Exception as e:  # timeouts and anything unexpected
            return "unverified", f"{type(e).__name__}"
    return "unverified", "No response"


def main():
    with open(CSV_PATH, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    targets = []
    for row in rows:
        name = " ".join((row.get("Clinician/Group Name") or "").split())
        url = normalize(row.get("Website"))
        if name and url:
            targets.append((name, url))

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda t: (t[0], t[1], *check(t[1])), targets))

    broken = [r for r in results if r[2] == "broken"]
    unverified = [r for r in results if r[2] == "unverified"]
    ok = len(results) - len(broken) - len(unverified)

    lines = ["## Provider website check", "",
             f"Checked {len(results)} websites: {ok} working, {len(broken)} broken, "
             f"{len(unverified)} couldn't verify.", ""]
    if broken:
        lines += ["### Broken (fix in the sheet)", "", "| Provider | Website | Problem |", "|---|---|---|"]
        lines += [f"| {n} | {u} | {d} |" for n, u, _, d in sorted(broken)]
        lines.append("")
    if unverified:
        lines += ["### Couldn't verify (open these by hand if they keep showing up)", "",
                  "| Provider | Website | Note |", "|---|---|---|"]
        lines += [f"| {n} | {u} | {d} |" for n, u, _, d in sorted(unverified)]
        lines.append("")
    report = "\n".join(lines)
    print(report)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as s:
            s.write(report + "\n")
    if broken:
        print(f"::error::{len(broken)} provider website(s) appear broken. See the run summary.")
        sys.exit(1)


if __name__ == "__main__":
    main()
