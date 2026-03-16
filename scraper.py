"""
Google Scholar Citation Scraper — core logic + CLI
Usage:
  python scraper.py YA43PbsAAAAJ
  python scraper.py https://scholar.google.com/citations?user=YA43PbsAAAAJ
  python scraper.py YA43PbsAAAAJ --output-dir ./results
  python scraper.py YA43PbsAAAAJ --use-tor
"""

import argparse
import csv
import os
import re
import sys
import time
from typing import Callable, Optional
from urllib.parse import urlparse, parse_qs

import requests
from bs4 import BeautifulSoup

def init_proxy(mode: str = "free") -> None:
    """
    Configure a proxy for scholarly to reduce CAPTCHA blocks on cloud IPs.

    mode="free"  — use scholarly's built-in FreeProxies (public proxy lists, no signup)
    mode="tor"   — use local Tor daemon (must be running on port 9050)
    """
    try:
        from scholarly import scholarly, ProxyGenerator
    except ImportError:
        return

    pg = ProxyGenerator()
    try:
        if mode == "tor":
            if not pg.Tor_Internal(tor_cmd="tor"):
                print("Warning: Tor not found — falling back to FreeProxies.")
                pg.FreeProxies()
        else:
            pg.FreeProxies()
        scholarly.use_proxy(pg)
        print(f"Proxy configured for scholarly (mode={mode}).")
    except Exception as exc:
        print(f"Warning: could not configure proxy: {exc}")


def _fetch_url(url: str, retries: int = 3) -> str:
    """GET a URL with retry/backoff. Org listing pages are rarely blocked directly."""
    last_exc: Exception = RuntimeError("No attempts made")
    for attempt in range(retries):
        try:
            resp = requests.get(url, headers=_ORG_HEADERS, timeout=20)
            resp.raise_for_status()
            return resp.text
        except Exception as exc:
            last_exc = exc
            if attempt < retries - 1:
                time.sleep(4 * (attempt + 1))
    raise last_exc


def extract_user_id(value: str) -> str:
    """Accept a raw user ID or a full Scholar profile URL."""
    value = value.strip()
    if value.startswith("http"):
        parsed = urlparse(value)
        params = parse_qs(parsed.query)
        if "user" not in params:
            raise ValueError("Could not find 'user' parameter in the provided URL.")
        return params["user"][0]
    return value


def fetch_author(user_id: str, use_tor: bool = False) -> dict:
    try:
        from scholarly import scholarly, ProxyGenerator
    except ImportError:
        raise RuntimeError("scholarly is not installed. Run: pip install scholarly")

    if use_tor:
        pg = ProxyGenerator()
        if not pg.Tor_Internal(tor_cmd="tor"):
            print("Warning: Could not connect to Tor. Proceeding without proxy.")
        else:
            scholarly.use_proxy(pg)

    author = scholarly.search_author_id(user_id)
    author = scholarly.fill(author, sections=["basics", "indices", "counts"])
    return author


def format_summary(author: dict) -> list[dict]:
    return [
        {
            "metric": "Citations",
            "all_time": author.get("citedby", ""),
            "since_5y": author.get("citedby5y", ""),
        },
        {
            "metric": "h-index",
            "all_time": author.get("hindex", ""),
            "since_5y": author.get("hindex5y", ""),
        },
        {
            "metric": "i10-index",
            "all_time": author.get("i10index", ""),
            "since_5y": author.get("i10index5y", ""),
        },
    ]


def format_per_year(author: dict) -> list[dict]:
    cites_per_year: dict = author.get("cites_per_year", {})
    return [
        {"year": year, "citations": count}
        for year, count in sorted(cites_per_year.items())
    ]


def write_summary_csv(rows: list[dict], path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["metric", "all_time", "since_5y"])
        writer.writeheader()
        writer.writerows(rows)
    print(f"Summary written to: {path}")


# ── Institution / Org scraping ─────────────────────────────────────────────────

def extract_org_id(value: str) -> str:
    """Accept a raw org ID or a full Scholar org URL."""
    value = value.strip()
    if value.startswith("http"):
        parsed = urlparse(value)
        params = parse_qs(parsed.query)
        if "org" not in params:
            raise ValueError("Could not find 'org' parameter in the provided URL.")
        return params["org"][0]
    return value


_ORG_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def _get_org_author_ids(org_id: str) -> list[str]:
    """
    Scrape the Google Scholar org listing page to collect all author IDs.
    Paginates via cstart until no more results are found.
    """
    author_ids: list[str] = []
    cstart = 0
    page_size = 100

    while True:
        url = (
            f"https://scholar.google.com/citations"
            f"?view_op=view_org&org={org_id}&hl=en&pagesize={page_size}&cstart={cstart}"
        )
        html = _fetch_url(url)
        soup = BeautifulSoup(html, "html.parser")

        # Each author card has an anchor inside .gs_ai_name
        links = soup.select(".gs_ai_name a")
        if not links:
            break

        for link in links:
            href = link.get("href", "")
            m = re.search(r"[?&]user=([^&]+)", href)
            if m:
                author_ids.append(m.group(1))

        # Stop if fewer results than a full page — last page reached
        if len(links) < page_size:
            break

        cstart += page_size
        time.sleep(1.5)  # polite delay between listing pages

    return author_ids


def fetch_org_authors(
    org_id: str,
    delay: float = 3.0,
    progress_cb: Optional[Callable[[int, Optional[int]], None]] = None,
) -> list[dict]:
    """
    Fetch all listed authors from a Google Scholar org page.

    Phase 1: scrape org listing directly (requests+BS4) → author ID list.
    Phase 2: use scholarly.fill() per author ID → citation metrics.

    progress_cb(current, total):
      total=None  → still collecting IDs
      total=N     → filling author N of total
    """
    try:
        from scholarly import scholarly
    except ImportError:
        raise RuntimeError("scholarly is not installed. Run: pip install scholarly")

    # Phase 1 — collect author IDs from the org listing page
    if progress_cb:
        progress_cb(0, None)

    author_ids = _get_org_author_ids(org_id)
    total = len(author_ids)

    if total == 0:
        raise RuntimeError(
            "No authors found for this institution. "
            "The org ID may be wrong, or Google Scholar blocked the listing request."
        )

    # Phase 2 — fetch metrics for each author via scholarly
    results = []
    for i, scholar_id in enumerate(author_ids):
        if progress_cb:
            progress_cb(i, total)

        try:
            author = scholarly.search_author_id(scholar_id)
            filled = scholarly.fill(author, sections=["basics", "indices", "counts"])
            error = ""
        except Exception as exc:
            filled = {"scholar_id": scholar_id}
            error = str(exc)

        sid = filled.get("scholar_id", scholar_id)
        results.append({
            "name":        filled.get("name", ""),
            "affiliation": filled.get("affiliation", ""),
            "scholar_id":  sid,
            "scholar_url": (
                f"https://scholar.google.com/citations?user={sid}" if sid else ""
            ),
            "citedby":    filled.get("citedby", ""),
            "citedby5y":  filled.get("citedby5y", ""),
            "hindex":     filled.get("hindex", ""),
            "hindex5y":   filled.get("hindex5y", ""),
            "i10index":   filled.get("i10index", ""),
            "i10index5y": filled.get("i10index5y", ""),
            "error":      error,
        })

        if delay > 0 and i < total - 1:
            time.sleep(delay)

    if progress_cb:
        progress_cb(total, total)

    return results


def fetch_authors_batch(
    user_ids: list[str],
    delay: float = 3.0,
    progress_cb: Optional[Callable[[int, int], None]] = None,
) -> list[dict]:
    """
    Fetch citation metrics for a pre-supplied list of Scholar user IDs.
    Output dict shape is identical to fetch_org_authors() — same 11 fields,
    so ORG_CSV_FIELDS and the /api/download/../org endpoint are fully reused.
    """
    try:
        from scholarly import scholarly
    except ImportError:
        raise RuntimeError("scholarly is not installed. Run: pip install scholarly")

    total = len(user_ids)
    results = []

    for i, uid in enumerate(user_ids):
        if progress_cb:
            progress_cb(i, total)

        try:
            author = scholarly.search_author_id(uid)
            filled = scholarly.fill(author, sections=["basics", "indices", "counts"])
            error = ""
        except Exception as exc:
            filled = {}
            error = str(exc)

        results.append({
            "name":        filled.get("name", ""),
            "affiliation": filled.get("affiliation", ""),
            "scholar_id":  uid,
            "scholar_url": f"https://scholar.google.com/citations?user={uid}",
            "citedby":     filled.get("citedby", ""),
            "citedby5y":   filled.get("citedby5y", ""),
            "hindex":      filled.get("hindex", ""),
            "hindex5y":    filled.get("hindex5y", ""),
            "i10index":    filled.get("i10index", ""),
            "i10index5y":  filled.get("i10index5y", ""),
            "error":       error,
        })

        if delay > 0 and i < total - 1:
            time.sleep(delay)

    if progress_cb:
        progress_cb(total, total)

    return results


ORG_CSV_FIELDS = [
    "name", "affiliation", "scholar_id", "scholar_url",
    "citedby", "citedby5y", "hindex", "hindex5y",
    "i10index", "i10index5y", "error",
]


def write_org_csv(rows: list[dict], path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=ORG_CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    print(f"Org authors written to: {path}")


# ── Per-year CSV ───────────────────────────────────────────────────────────────

def write_per_year_csv(rows: list[dict], path: str) -> None:
    if not rows:
        print("Warning: No per-year citation data returned.")
        return
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["year", "citations"])
        writer.writeheader()
        writer.writerows(rows)
    print(f"Per-year data written to: {path}")


# ── CLI entry point ────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch Google Scholar citation metrics and export to CSV."
    )
    parser.add_argument(
        "user",
        help="Scholar user ID (e.g. YA43PbsAAAAJ) or full profile URL",
    )
    parser.add_argument(
        "--output-dir",
        default=".",
        help="Directory to write CSV files (default: current directory)",
    )
    parser.add_argument(
        "--use-tor",
        action="store_true",
        help="Route requests through Tor to avoid IP blocking (requires Tor)",
    )
    args = parser.parse_args()

    try:
        user_id = extract_user_id(args.user)
    except ValueError as e:
        sys.exit(str(e))

    output_dir = args.output_dir
    os.makedirs(output_dir, exist_ok=True)

    print(f"Fetching profile for user ID: {user_id} ...")
    try:
        author = fetch_author(user_id, use_tor=args.use_tor)
    except Exception as exc:
        msg = str(exc)
        if "captcha" in msg.lower() or "blocked" in msg.lower() or "403" in msg:
            sys.exit(
                "Google Scholar blocked the request (CAPTCHA/rate-limit).\n"
                "Tips:\n"
                "  1. Wait a few minutes and retry.\n"
                "  2. Run with --use-tor (requires Tor to be installed).\n"
                "  3. Use a VPN or residential proxy.\n"
                f"Original error: {exc}"
            )
        sys.exit(f"Error: {exc}")

    print(f"Author: {author.get('name', user_id)}")

    safe_id = re.sub(r"[^\w\-]", "_", user_id)
    write_summary_csv(format_summary(author), os.path.join(output_dir, f"{safe_id}_summary.csv"))
    write_per_year_csv(format_per_year(author), os.path.join(output_dir, f"{safe_id}_per_year.csv"))


if __name__ == "__main__":
    main()
