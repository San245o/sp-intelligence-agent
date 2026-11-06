"""Read documented public Greenhouse/Lever boards evidenced by the company's site."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from urllib.parse import urljoin, urlsplit, quote
from urllib.request import Request

from bs4 import BeautifulSoup

from .external_collectors import legal_proof, observation, own_domain
from .source_store import capture_source
from .website import SAFE_OPENER, USER_AGENT


def evidenced_boards(profile):
    value = profile.get("evidence", {}).get("website", {}).get("value") or {}
    if not own_domain(profile):
        return []
    boards = {}
    for page in [value, *(value.get("pages") or [])]:
        path = Path(page.get("snapshot_path") or "")
        if not path.is_file(): continue
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != page.get("content_sha256"): continue
        base = page.get("url") or page.get("final_url") or ""
        soup = BeautifulSoup(raw, "lxml")
        for node in soup.select("a[href],iframe[src]"):
            url = urljoin(base, str(node.get("href") or node.get("src") or ""))
            parsed = urlsplit(url); host = (parsed.hostname or "").casefold()
            parts = [p for p in parsed.path.split("/") if p]
            provider = "greenhouse" if host in {"boards.greenhouse.io", "job-boards.greenhouse.io"} else "lever" if host in {"jobs.lever.co", "jobs.eu.lever.co"} else None
            if provider and parts:
                token = parts[0]
                if token.replace("-", "").replace("_", "").isalnum():
                    boards[(provider, host, token)] = {"provider": provider, "host": host, "token": token, "supporting_page": base, "supporting_sha256": page["content_sha256"]}
    return list(boards.values())[:2]


def collect_ats(profile, budget):
    output, attempts = [], []
    metrics = {"requests": 0, "bytes": 0, "latencies_ms": [], "attempts": attempts}
    for board in evidenced_boards(profile):
        token = quote(board["token"], safe="")
        endpoint = f'https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true' if board["provider"] == "greenhouse" else f'https://api{".eu" if board["host"] == "jobs.eu.lever.co" else ""}.lever.co/v0/postings/{token}?mode=json'
        if not budget.reserve(1):
            attempts.append({"url": endpoint, "outcome": "budget_exhausted"}); break
        status = "source_error"
        import time
        started = time.monotonic()
        try:
            metrics["requests"] += 1
            with SAFE_OPENER.open(Request(endpoint, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}), timeout=10) as response:
                raw = response.read(2_000_001)
                if urlsplit(response.geturl()).hostname != urlsplit(endpoint).hostname:
                    raise ValueError("Unexpected API redirect")
            metrics["bytes"] += len(raw)
            if len(raw) > 2_000_000: raise ValueError("Oversized board")
            body = json.loads(raw)
            jobs = body.get("jobs", []) if isinstance(body, dict) else body
            if not isinstance(jobs, list): raise ValueError("Invalid jobs array")
            capture = capture_source(raw)
            now = datetime.now(timezone.utc).isoformat()
            for job in jobs[:150]:
                if not isinstance(job, dict): continue
                title = str(job.get("title") or job.get("text") or "").strip()
                url = str(job.get("absolute_url") or job.get("hostedUrl") or "")
                content = str(job.get("content") or job.get("descriptionPlain") or "")
                text = BeautifulSoup(content, "lxml").get_text(" ", strip=True)
                proof = legal_proof(profile, text)
                if len(title) < 6 or not url.startswith("https://") or not job.get("id") or not proof: continue
                if str(job.get("internal_job_id", "")) == "None": continue
                document = {**capture, "content_sha256": capture["snapshot_sha256"], "url": url, "retrieved_at": now}
                listing_proof = {"type": "public_ats_current_listing", "provider": board["provider"], "job_id": str(job["id"]),
                    "employer_evidence": proof, "active_as_of": now, "api_url": endpoint,
                    "supporting_company_page": board["supporting_page"], "supporting_company_sha256": board["supporting_sha256"]}
                metrics_value = {"job_title": title, "job_id": str(job["id"]), "active_listing": True, "active_as_of": now, "api_url": endpoint}
                # Keep modification time separate; never fabricate publication/expiry.
                if job.get("first_published_at"): metrics_value["published_at"] = str(job["first_published_at"])
                if job.get("updated_at"): metrics_value["modified_at"] = str(job["updated_at"])
                item = observation(profile, document, [listing_proof], "job_posting", platform="job_board", source_class="public_ats",
                    span=title + " " + text[:800], values=metrics_value)
                item.update(acquisition_mode="official_api", strategy="evidenced_public_ats_listing")
                output.append(item)
            status = "fetched"
        except Exception:
            pass
        finally:
            budget.settle(1, 1)
            metrics["latencies_ms"].append(int((time.monotonic() - started) * 1000))
            attempts.append({"url": endpoint, "outcome": status, "requests": 1})
    return output, metrics
