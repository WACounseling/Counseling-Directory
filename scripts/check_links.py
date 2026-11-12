#!/usr/bin/env python3
"""
Weekly broken-link check for the Counseling Directory.

Visits every provider website in data/directory.csv and writes a report to the
GitHub Actions run summary:
  - Broken: the page or the whole site is gone (404/410, or the web address
    no longer exists). The run fails when any are found, so GitHub emails you.
  - Couldn't confirm: the site blocked the automated check, timed out, or had
    a temporary error. Usually fine; worth a quick look if it repeats.
  - Moved to a different website: the link now forwards somewhere else.

Usage: python3 scripts/check_links.py
Uses only Python's standard library.
"""
import csv
import os
import socket
import ssl
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parent.parent
CSV_PATH = Path(os.environ.get("LINKS_CSV", REPO_ROOT / "data" / "directory.csv"))
TIMEOUT = 20
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
}


def normalize(url):
    url = (url or "").strip()
    if not url:
        return None
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


def base_domain(url):
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def fetch(url, method):
    req = urllib.request.Request(url, headers=HEADERS, method=method)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.status, resp.geturl()


def check(url):
    """Returns (status, detail, final_url). status: ok | broken | unsure."""
    last_error = None
    for method in ("HEAD", "GET"):
        try:
            code, final = fetch(url, method)
            return "ok", str(code), final
        except urllib.error.HTTPError as e:
            if e.code in (404, 410):
                if method == "HEAD":
                    last_error = ("broken", f"Page not found ({e.code})")
                    continue  # some servers answer HEAD wrongly; confirm with GET
                return "broken", f"Page not found ({e.code})", None
            last_error = ("unsure", f"Site returned error {e.code}")
        except urllib.error.URLError as e:
            reason = e.reason
            if isinstance(reason, socket.gaierror):
                return "broken", "Web address no longer exists (domain not found)", None
            if isinstance(reason, ConnectionRefusedError):
                last_error = ("broken", "Site refused the connection")
            elif isinstance(reason, (ssl.SSLError, ssl.CertificateError)):
                last_error = ("unsure", "Security certificate problem")
            elif isinstance(reason, (socket.timeout, TimeoutError)):
                last_error = ("unsure", "Timed out")
            else:
                last_error = ("unsure", f"Couldn't connect ({reason})")
        except (socket.timeout, TimeoutError):
            last_error = ("unsure", "Timed out")
        except Exception as e:  # anything unexpected is reported, never crashes the run
            last_error = ("unsure", f"Unexpected error ({type(e).__name__})")
    return last_error[0], last_error[1], None


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
        results = list(pool.map(lambda t: (t[0], t[1]) + check(t[1]), targets))

    broken = [r for r in results if r[2] == "broken"]
    unsure = [r for r in results if r[2] == "unsure"]
    moved = [r for r in results if r[2] == "ok" and r[4] and base_domain(r[4]) != base_domain(r[1])]

    lines = ["## Weekly website check", "",
             f"Checked **{len(results)}** provider websites. "
             f"{len(broken)} broken, {len(unsure)} couldn't be confirmed, {len(moved)} moved.", ""]

    def section(title, note, items):
        if not items:
            return
        lines.extend([f"### {title} ({len(items)})", note, ""])
        lines.extend(items)
        lines.append("")

    section("Broken", "Update or remove these in the sheet.",
            [f"- **{n}**: {d}. Listed as {u}" for n, u, s, d, f in broken])
    section("Moved to a different website", "Check that the new site is still the same provider, then update the sheet.",
            [f"- **{n}**: {u} now goes to {f}" for n, u, s, d, f in moved])
    section("Couldn't confirm", "Many sites block automated checks, so these are often fine. "
            "Open them yourself if one shows up several weeks in a row.",
            [f"- {n}: {d}. {u}" for n, u, s, d, f in unsure])
    if not (broken or moved or unsure):
        lines.append("Every website responded normally.")

    report = "\n".join(lines)
    print(report)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as s:
            s.write(report + "\n")

    if broken:
        print(f"::error::{len(broken)} provider website(s) appear broken. See the run summary for the list.")
        sys.exit(1)


if __name__ == "__main__":
    main()
