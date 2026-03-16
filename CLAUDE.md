# Scholar Citation Scraper

FastAPI + plain HTML tool to scrape Google Scholar citation metrics and export to CSV.

## Stack
- **Backend**: FastAPI + `scholarly` library + `beautifulsoup4`
- **Frontend**: Plain HTML/CSS/JS (no framework) — served as static files by FastAPI
- **Deployment**: Render (free tier) — config in `render.yaml`

## Running locally
```bash
pip install -r requirements.txt
uvicorn main:app --reload
# → http://localhost:8000
```

## Running tests
```bash
pip install -r requirements-dev.txt
pytest test_scraper.py -v
```
Tests cover pure functions only (no network): `extract_user_id`, `extract_org_id`,
`format_summary`, `format_per_year`, `parse_batch_csv`.
No proxy needed locally (residential IP). On cloud servers set `PROXY_MODE=free`.

## Key files
- `main.py` — FastAPI routes, background job tasks, in-memory job store
- `scraper.py` — all scraping logic + CLI entry point
- `static/index.html` — full frontend (tabs, polling, progress, CSV download)
- `render.yaml` — Render deployment config
- `requirements.txt` — Python dependencies

## Features
| Tab | Endpoint | What it does |
|---|---|---|
| Single Author | `POST /api/scrape` | Fetch metrics for one Scholar profile URL or ID |
| Institution | `POST /api/scrape-org` | Scrape all listed authors from a Scholar org page |
| Batch / CSV | `POST /api/scrape-batch` | Upload CSV of profile URLs, scrape all in batch |

All three write results to the in-memory job store and are polled via `GET /api/jobs/{job_id}`.
CSV download via `GET /api/download/{job_id}/{type}` where type = `summary` | `per_year` | `org` | `batch`.

## Proxy / CAPTCHA
- `PROXY_MODE=off` (default) — direct requests, fine locally
- `PROXY_MODE=free` — `scholarly.FreeProxies()`, use on Render
- `PROXY_MODE=tor` — local Tor daemon

## Institution scraping (approach used)
Org listing page scraped directly with `requests` + BeautifulSoup (not scholarly) to collect author IDs → then scholarly fills each author profile individually. This is more reliable than `scholarly.search_author_by_organization()` which silently returns 0 on cloud IPs.

## CSV input format for Batch tab
First column must be Scholar profile URLs. Header rows are auto-skipped (they raise `ValueError` in `extract_user_id()`). Works with or without header row.
