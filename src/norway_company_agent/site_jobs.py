"""Extract current, explicit JobPosting records from already captured own-site pages."""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .identity import _tokens
from .website import _registered_domain


def _jobs(node):
    if isinstance(node, dict):
        kinds = node.get("@type")
        kinds = kinds if isinstance(kinds, list) else [kinds]
        if "JobPosting" in kinds:
            yield node
        for value in node.values():
            yield from _jobs(value)
    elif isinstance(node, list):
        for value in node:
            yield from _jobs(value)


def extract_site_jobs(profile: dict) -> list[dict]:
    website = profile.get("evidence", {}).get("website") or {}
    value = website.get("value") or {}
    identity = value.get("identity_assessment") or {}
    if website.get("status") != "available" or not identity.get("publishable") or not identity.get("content_attributable"):
        return []
    today = datetime.now(timezone.utc).date()
    org = str(profile["organisation_number"])
    core = _tokens(profile.get("name"))
    output = {}
    for page in [value, *(value.get("pages") or [])]:
        source = Path(page.get("snapshot_path") or "")
        url = str(page.get("url") or page.get("final_url") or "")
        if not source.is_file() or not url:
            continue
        try:
            raw = source.read_bytes()
        except OSError:
            continue
        digest = hashlib.sha256(raw).hexdigest()
        if digest != page.get("content_sha256"):
            continue
        soup = BeautifulSoup(raw, "lxml")
        for script in soup.select('script[type="application/ld+json"]'):
            try:
                data = json.loads(script.string or script.get_text() or "{}")
            except (ValueError, TypeError):
                continue
            for job in _jobs(data):
                employer = job.get("hiringOrganization") or {}
                if not isinstance(employer, dict):
                    continue
                identifier = employer.get("identifier") or employer.get("taxID")
                if isinstance(identifier, dict):
                    identifier = identifier.get("value")
                employer_match = str(identifier or "").replace(" ", "").removeprefix("NO").removesuffix("MVA") == org
                employer_name_match = bool(core and _tokens(employer.get("name")) == core)
                title = str(job.get("title") or "").strip()
                if job.get("url") is not None and not isinstance(job["url"], str):
                    continue
                try:
                    posting_url = urljoin(url, str(job.get("url") or url)).split("#")[0].rstrip("/")
                    parsed = urlsplit(posting_url)
                except ValueError:
                    continue
                if not (employer_match or employer_name_match) or len(title) < 6 or "linkedin" in title.casefold():
                    continue
                # A list-page link is discovery only. Publish after the actual
                # dedicated role page has been fetched and retained by the crawler.
                if posting_url != url.split("#")[0].rstrip("/") or parsed.path.rstrip("/").casefold() in {"", "/careers", "/jobs", "/karriere", "/stillinger", "/contact", "/kontakt", "/about", "/om-oss", "/news"}:
                    continue
                if parsed.scheme not in {"http", "https"} or _registered_domain(posting_url) != value.get("registered_domain"):
                    continue
                try:
                    published = date.fromisoformat(str(job.get("datePosted") or "")[:10])
                    expires = date.fromisoformat(str(job.get("validThrough") or "")[:10])
                except ValueError:
                    continue
                if published > today or expires < today:
                    continue
                output[posting_url] = {"id": "site-job-" + hashlib.sha256(f"{org}|{posting_url}".encode()).hexdigest()[:24],
                    "organisation_number": org, "platform": "job_board", "signal_type": "job_posting", "source_url": posting_url,
                    "retrieved_at": website.get("retrieved_at"), "content_sha256": digest,
                    "snapshot_path": str(source), "snapshot_sha256": digest, "exact_entity": True,
                    "identity_proof": [{"type": "captured_own_site_jobposting", "employer_org_match": employer_match, "employer_name_match": employer_name_match, "supporting_page": url}],
                    "acquisition_mode": "permitted_public_page", "rights_status": "approved", "source_class": "company_careers",
                    "evidence_span": title, "metrics": {"job_title": title, "published_at": published.isoformat(), "deadline": expires.isoformat()},
                    "strategy": "captured_structured_jobposting"}
    return list(output.values())
