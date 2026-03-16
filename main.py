"""
FastAPI backend for the Google Scholar Citation Scraper.
Run locally:  uvicorn main:app --reload
"""

import asyncio
import io
import csv
import os
import uuid
from typing import Optional

from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from scraper import (
    extract_user_id, fetch_author, format_summary, format_per_year,
    extract_org_id, fetch_org_authors, fetch_authors_batch, ORG_CSV_FIELDS,
    init_proxy,
)

app = FastAPI(title="Scholar Scraper")

# PROXY_MODE env var controls how scholarly avoids CAPTCHA blocks.
#   "free"  — use FreeProxies (public proxies, no cost, default for cloud)
#   "tor"   — use local Tor daemon
#   "off"   — no proxy (fine for local dev on a residential IP)
_proxy_mode = os.getenv("PROXY_MODE", "off").strip().lower()
if _proxy_mode != "off":
    init_proxy(_proxy_mode)
else:
    print("PROXY_MODE=off — requests go direct (fine locally, may CAPTCHA on cloud).")

# In-memory job store: job_id → job dict
jobs: dict[str, dict] = {}


# ── Models ─────────────────────────────────────────────────────────────────────

class ScrapeRequest(BaseModel):
    user: str  # user ID or full Scholar profile URL

class ScrapeOrgRequest(BaseModel):
    org: str   # org ID or full Scholar org URL


# ── Single-author background task ─────────────────────────────────────────────

async def run_scrape(job_id: str, user_input: str) -> None:
    try:
        user_id = extract_user_id(user_input)
    except ValueError as e:
        jobs[job_id] = {"status": "error", "error": str(e)}
        return

    jobs[job_id]["user_id"] = user_id

    loop = asyncio.get_event_loop()
    try:
        author = await loop.run_in_executor(None, lambda: fetch_author(user_id))
    except Exception as exc:
        msg = str(exc)
        if "captcha" in msg.lower() or "blocked" in msg.lower() or "403" in msg:
            jobs[job_id] = {
                "status": "error",
                "error": (
                    "Google Scholar blocked the request (CAPTCHA / rate-limit). "
                    "Wait a few minutes and try again."
                ),
            }
        else:
            jobs[job_id] = {"status": "error", "error": msg}
        return

    jobs[job_id] = {
        "status": "done",
        "name": author.get("name", user_id),
        "affiliation": author.get("affiliation", ""),
        "user_id": user_id,
        "summary": format_summary(author),
        "per_year": format_per_year(author),
    }


# ── Org background task ────────────────────────────────────────────────────────

async def run_scrape_org(job_id: str, org_input: str) -> None:
    try:
        org_id = extract_org_id(org_input)
    except ValueError as e:
        jobs[job_id] = {"status": "error", "error": str(e)}
        return

    jobs[job_id].update({"org_id": org_id, "progress": {"current": 0, "total": None}})

    def progress_cb(current: int, total: Optional[int]) -> None:
        jobs[job_id]["progress"] = {"current": current, "total": total}
        if total is None:
            jobs[job_id]["status"] = "collecting"
        else:
            jobs[job_id]["status"] = "processing"

    loop = asyncio.get_event_loop()
    try:
        results = await loop.run_in_executor(
            None, lambda: fetch_org_authors(org_id, delay=3.0, progress_cb=progress_cb)
        )
    except Exception as exc:
        msg = str(exc)
        if "captcha" in msg.lower() or "blocked" in msg.lower() or "403" in msg:
            jobs[job_id] = {
                "status": "error",
                "error": (
                    "Google Scholar blocked the request (CAPTCHA / rate-limit). "
                    "The server IP may be flagged. Wait and retry, or use a proxy."
                ),
            }
        else:
            jobs[job_id] = {"status": "error", "error": msg}
        return

    jobs[job_id].update({
        "status": "done",
        "results": results,
        "progress": {"current": len(results), "total": len(results)},
    })


# ── Batch background task ──────────────────────────────────────────────────────

async def run_scrape_batch(job_id: str, user_ids: list[str]) -> None:
    jobs[job_id].update({"progress": {"current": 0, "total": len(user_ids)}})

    def progress_cb(current: int, total: int) -> None:
        jobs[job_id]["progress"] = {"current": current, "total": total}
        jobs[job_id]["status"] = "processing"

    loop = asyncio.get_event_loop()
    try:
        results = await loop.run_in_executor(
            None, lambda: fetch_authors_batch(user_ids, delay=3.0, progress_cb=progress_cb)
        )
    except Exception as exc:
        msg = str(exc)
        if "captcha" in msg.lower() or "blocked" in msg.lower() or "403" in msg:
            jobs[job_id] = {
                "status": "error",
                "error": (
                    "Google Scholar blocked the request (CAPTCHA / rate-limit). "
                    "The server IP may be flagged. Wait and retry, or use a proxy."
                ),
            }
        else:
            jobs[job_id] = {"status": "error", "error": msg}
        return

    jobs[job_id].update({
        "status": "done",
        "results": results,
        "progress": {"current": len(results), "total": len(results)},
    })


# ── API routes — single author ─────────────────────────────────────────────────

@app.post("/api/scrape")
async def start_scrape(body: ScrapeRequest):
    job_id = str(uuid.uuid4())
    jobs[job_id] = {"status": "running"}
    asyncio.create_task(run_scrape(job_id, body.user))
    return {"job_id": job_id}


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.get("/api/download/{job_id}/{file_type}")
async def download_csv(job_id: str, file_type: str):
    """file_type: 'summary' | 'per_year' | 'org'"""
    job = jobs.get(job_id)
    if not job or job.get("status") != "done":
        raise HTTPException(status_code=404, detail="Job not ready or not found")

    user_id = job.get("user_id", job_id)
    org_id  = job.get("org_id", job_id)
    buf = io.StringIO()

    if file_type == "summary":
        writer = csv.DictWriter(buf, fieldnames=["metric", "all_time", "since_5y"])
        writer.writeheader()
        writer.writerows(job["summary"])
        filename = f"{user_id}_summary.csv"

    elif file_type == "per_year":
        writer = csv.DictWriter(buf, fieldnames=["year", "citations"])
        writer.writeheader()
        writer.writerows(job["per_year"])
        filename = f"{user_id}_per_year.csv"

    elif file_type == "org":
        writer = csv.DictWriter(buf, fieldnames=ORG_CSV_FIELDS)
        writer.writeheader()
        writer.writerows(job.get("results", []))
        filename = f"org_{org_id}_authors.csv"

    elif file_type == "batch":
        writer = csv.DictWriter(buf, fieldnames=ORG_CSV_FIELDS)
        writer.writeheader()
        writer.writerows(job.get("results", []))
        filename = "batch_authors.csv"

    else:
        raise HTTPException(status_code=400, detail="Invalid file_type")

    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── API routes — institution ───────────────────────────────────────────────────

@app.post("/api/scrape-org")
async def start_scrape_org(body: ScrapeOrgRequest):
    job_id = str(uuid.uuid4())
    jobs[job_id] = {"status": "starting"}
    asyncio.create_task(run_scrape_org(job_id, body.org))
    return {"job_id": job_id}


# ── API routes — batch CSV upload ─────────────────────────────────────────────

@app.post("/api/scrape-batch")
async def start_scrape_batch(file: UploadFile = File(...)):
    content = await file.read()
    try:
        text = content.decode("utf-8-sig")  # handle BOM from Excel-exported CSVs
    except UnicodeDecodeError:
        text = content.decode("latin-1")

    # Extract first column from every non-blank line; skip headers/invalid entries
    user_ids: list[str] = []
    for line in text.splitlines():
        raw = line.split(",")[0].strip().strip('"').strip("'")
        if not raw:
            continue
        try:
            user_ids.append(extract_user_id(raw))
        except ValueError:
            pass  # skip header rows and non-URL strings

    if not user_ids:
        raise HTTPException(
            status_code=400,
            detail="No valid Scholar profile URLs found in the uploaded CSV.",
        )

    job_id = str(uuid.uuid4())
    jobs[job_id] = {"status": "processing"}
    asyncio.create_task(run_scrape_batch(job_id, user_ids))
    return {"job_id": job_id, "count": len(user_ids)}


# ── Serve frontend ─────────────────────────────────────────────────────────────
app.mount("/", StaticFiles(directory="static", html=True), name="static")
