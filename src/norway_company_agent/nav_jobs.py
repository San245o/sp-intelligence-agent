"""Bounded, shared NAV job-feed discovery for one competition batch.

The feed is scanned once per run. Employer names only select detail-page
candidates; a vacancy is assigned to a company only after its employer
organisation number matches that company or a Brønnøysund subunit.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import time
import unicodedata
from email.utils import format_datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from .nav_snapshot import load_snapshot, save_snapshot
from .source_store import capture_source


FEED_ORIGIN = "https://pam-stilling-feed.nav.no"
FEED_URL = FEED_ORIGIN + "/api/v1/feed"
TOKEN_URL = FEED_ORIGIN + "/api/publicToken"
USER_AGENT = "Signalpost competition agent/1.0"
MAX_RESPONSE_BYTES = 8_000_000
JWT_PATTERN = re.compile(r"([A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)")
NAME_SPLIT = re.compile(r"\s*,\s*|\s+[\-–—]\s+")
LEGAL_SUFFIXES = {
    "as", "asa", "ans", "da", "enk", "nuf", "sa", "ks", "sti", "stiftelsen",
    "brl", "bbl", "fl i", "fl i.", "sf", "iks", "samvirkeforetak", "ba",
}


def _safe_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return False
    return parsed.scheme == "https" and parsed.hostname == "pam-stilling-feed.nav.no" and port in (None, 443) and not parsed.username and not parsed.password


class _NavRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req: Request, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Request | None:
        if not _safe_url(newurl):
            raise HTTPError(req.full_url, 403, "Refused redirect outside NAV feed host", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = build_opener(_NavRedirectHandler())


def normalize_employer_name(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode("ascii").casefold()
    tokens = re.findall(r"[a-z0-9]+", text)
    while tokens and " ".join(tokens[-2:]) in LEGAL_SUFFIXES:
        tokens = tokens[:-2]
    while tokens and tokens[-1] in LEGAL_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


@dataclass
class NavBatchResult:
    jobs_by_org: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    name_candidates_by_org: dict[str, int] = field(default_factory=dict)
    rejected_by_org: dict[str, int] = field(default_factory=dict)
    complete: bool = False
    error: str | None = None
    pages: int = 0
    feed_entries: int = 0
    candidate_ads: int = 0
    detail_requests: int = 0
    window_days: int = 45
    feed_snapshot_reused: bool = False
    feed_snapshot_retrieved_at: str | None = None
    feed_snapshot_pages: int = 0
    feed_snapshot_age_seconds: float = 0
    feed_incremental_refresh: bool = False
    metrics: dict[str, Any] = field(default_factory=lambda: {"requests": 0, "bytes": 0, "latencies_ms": []})

    def summary(self) -> dict[str, Any]:
        return {
            "status": "complete" if self.complete else "incomplete",
            "window_days": self.window_days,
            "pages": self.pages,
            "latest_ad_entries": self.feed_entries,
            "name_matched_candidate_ads": self.candidate_ads,
            "detail_requests": self.detail_requests,
            "verified_jobs": sum(len(rows) for rows in self.jobs_by_org.values()),
            "companies_with_verified_jobs": sum(bool(rows) for rows in self.jobs_by_org.values()),
            "requests": self.metrics["requests"],
            "bytes": self.metrics["bytes"],
            "error": self.error,
            "feed_snapshot_reused": self.feed_snapshot_reused,
            "feed_snapshot_retrieved_at": self.feed_snapshot_retrieved_at,
            "feed_snapshot_pages": self.feed_snapshot_pages,
            "feed_snapshot_age_seconds": self.feed_snapshot_age_seconds,
            "feed_incremental_refresh": self.feed_incremental_refresh,
            "population_coverage": "Recent changes plus retained previously seen active ads; not a full historical bootstrap",
            "match_rule": "name/subunit names shortlist only; NAV employer organisation number must exactly match the company or a mapped Brønnøysund subunit",
        }


def _request(url: str, *, token: str | None, accept: str, budget: Any, metrics: dict[str, Any], headers: dict[str, str] | None = None, timeout: float = 25.0) -> tuple[int, dict[str, str], bytes, str | None]:
    if not _safe_url(url):
        return 0, {}, b"", "unsafe_nav_url"
    if not budget.reserve(1):
        return 0, {}, b"", "request_budget_exhausted"
    request_headers = {"Accept": accept, "User-Agent": USER_AGENT, **(headers or {})}
    if token:
        request_headers["Authorization"] = f"Bearer {token}"
    request = Request(url, headers=request_headers)
    started = time.monotonic()
    status = 0
    byte_count = 0
    error: str | None = None
    response_headers: dict[str, str] = {}
    raw = b""
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            final_url = response.geturl()
            if not _safe_url(final_url):
                error = "unsafe_nav_redirect"
            else:
                status = int(response.status)
                response_headers = {key.casefold(): value for key, value in response.headers.items()}
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                byte_count = len(raw)
                if byte_count > MAX_RESPONSE_BYTES:
                    raw = b""
                    error = "nav_response_over_size_limit"
    except HTTPError as exc:
        status = exc.code
        response_headers = {key.casefold(): value for key, value in exc.headers.items()} if exc.headers else {}
        if exc.code == 304:
            error = None
        else:
            error = f"http_{exc.code}"
    except (URLError, TimeoutError, OSError) as exc:
        error = type(exc).__name__
    elapsed = int((time.monotonic() - started) * 1000)
    budget.settle(1, 1)
    metrics["requests"] += 1
    metrics["bytes"] += byte_count
    metrics["latencies_ms"].append(elapsed)
    return status, response_headers, raw, error


def _token(budget: Any, metrics: dict[str, Any]) -> tuple[str | None, str | None]:
    status, _headers, raw, error = _request(TOKEN_URL, token=None, accept="text/plain,application/json", budget=budget, metrics=metrics, timeout=15)
    if error:
        return None, error
    if status != 200:
        return None, f"token_http_{status}"
    text = raw.decode("utf-8-sig", errors="replace")
    match = JWT_PATTERN.search(text)
    if match:
        return match.group(1), None
    try:
        body = json.loads(text)
    except json.JSONDecodeError:
        body = None
    if isinstance(body, dict):
        value = body.get("token") or body.get("access_token") or body.get("publicToken")
        if isinstance(value, str) and JWT_PATTERN.fullmatch(value.strip()):
            return value.strip(), None
    return None, "token_response_did_not_contain_jwt"


def _name_index(profiles: list[dict[str, Any]], local_cache: Any) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    name_to_orgs: dict[str, set[str]] = {}
    subunit_to_parent: dict[str, str] = {}
    parent_orgs = [str(profile.get("organisation_number") or "") for profile in profiles]
    subunits_by_parent = local_cache.brreg_subunits_for_parents(parent_orgs)
    for profile in profiles:
        org = str(profile.get("organisation_number") or "")
        names = [profile.get("name")]
        for unit in subunits_by_parent.get(org, []):
            if isinstance(unit, dict):
                unit_name = unit.get("name")
                subunit_org = str(unit.get("subunit_org") or "")
                if unit_name:
                    names.append(unit_name)
                if len(subunit_org) == 9:
                    subunit_to_parent[subunit_org] = org
        for name in names:
            key = normalize_employer_name(name)
            if len(key) >= 3:
                name_to_orgs.setdefault(key, set()).add(org)
    return name_to_orgs, subunit_to_parent


def _candidate_orgs(business_name: str, name_to_orgs: dict[str, set[str]]) -> set[str]:
    normalized = normalize_employer_name(business_name)
    if not normalized:
        return set()
    candidates = set(name_to_orgs.get(normalized, set()))
    for part in NAME_SPLIT.split(business_name):
        candidates.update(name_to_orgs.get(normalize_employer_name(part), set()))
    # NAV sometimes appends a department/location to the registered name.
    for name, orgs in name_to_orgs.items():
        if normalized.startswith(name + " "):
            candidates.update(orgs)
    return candidates


def discover_nav_jobs(
    profiles: list[dict[str, Any]],
    local_cache: Any,
    budget: Any,
    *,
    window_days: int = 45,
    max_pages: int = 150,
    max_details: int = 250,
    delay_seconds: float = 0.1,
) -> NavBatchResult:
    """Read recent NAV changes once, then detail-fetch only name-matched ads."""
    result = NavBatchResult()
    result.window_days = max(1, window_days)
    result.metrics = {"requests": 0, "bytes": 0, "latencies_ms": []}
    name_to_orgs, subunit_to_parent = _name_index(profiles, local_cache)
    token, error = _token(budget, result.metrics)
    if not token:
        result.error = error
        return result

    scan_started = datetime.now(timezone.utc)
    since = scan_started - timedelta(days=max(1, window_days))
    snapshot = load_snapshot(since, preserve_active=True)
    baseline = None if snapshot else load_snapshot(since, max_age_seconds=7 * 86400, preserve_active=True)
    query_since = since
    if baseline:
        # Overlap the last successful scan start so updates during pagination
        # cannot fall into a gap. Merge removals/inactive entries as well as ads.
        query_since = datetime.fromisoformat(baseline["retrieved_at"].replace("Z", "+00:00")) - timedelta(minutes=5)
        result.feed_incremental_refresh = True
    headers = {"If-Modified-Since": format_datetime(query_since, usegmt=True)}
    page_url: str | None = None if snapshot else FEED_URL
    entries: dict[str, dict[str, Any]] = (snapshot or baseline or {}).get("entries", {}).copy()
    if snapshot:
        result.complete = True
        result.feed_snapshot_reused = True
        result.feed_snapshot_retrieved_at = snapshot["retrieved_at"]
        result.feed_snapshot_pages = snapshot["pages"]
        result.feed_snapshot_age_seconds = snapshot["age_seconds"]
    while page_url and result.pages < max_pages:
        if delay_seconds:
            time.sleep(delay_seconds)
        status, response_headers, raw, error = _request(
            page_url, token=token, accept="application/json", budget=budget, metrics=result.metrics, headers=headers
        )
        headers = {}  # NAV's time window applies to the first page only.
        if error == "request_budget_exhausted":
            result.error = error
            break
        if error:
            result.error = error
            break
        if status == 304:
            result.complete = True
            break
        if status != 200:
            result.error = f"feed_http_{status}"
            break
        try:
            body = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            result.error = "feed_malformed_json"
            break
        if not isinstance(body, dict):
            result.error = "feed_page_not_object"
            break
        result.pages += 1
        for item in body.get("items") or []:
            if not isinstance(item, dict):
                continue
            meta = item.get("_feed_entry") if isinstance(item.get("_feed_entry"), dict) else {}
            uuid = str(meta.get("uuid") or item.get("uuid") or item.get("id") or "")
            if not uuid:
                continue
            modified = str(item.get("date_modified") or meta.get("sistEndret") or "")
            prior = entries.get(uuid)
            if prior is None or modified >= prior.get("modified", ""):
                entries[uuid] = {
                    "uuid": uuid,
                    "modified": modified,
                    "status": str(meta.get("status") or item.get("status") or "").upper(),
                    "business_name": str(meta.get("businessName") or item.get("businessName") or ""),
                    "url": str(item.get("url") or f"/api/v1/feedentry/{uuid}"),
                }
        next_raw = body.get("next_url")
        if next_raw:
            candidate_url = urljoin(page_url, str(next_raw))
            if not _safe_url(candidate_url):
                result.error = "unsafe_nav_pagination_url"
                break
            page_url = candidate_url
        else:
            page_url = None
            result.complete = True
    if page_url and result.pages >= max_pages and not result.error:
        result.error = "nav_page_cap_reached"
    if result.complete and not result.error and not snapshot and (result.pages or baseline):
        result.feed_snapshot_retrieved_at = scan_started.isoformat()
        result.feed_snapshot_pages = result.pages
        try:
            save_snapshot(entries, since=since, retrieved_at=scan_started, pages=result.pages, complete=True)
        except OSError:
            pass  # Cache persistence cannot invalidate a completed live acquisition.
    result.feed_entries = len(entries)

    per_org: dict[str, int] = {}
    candidates: list[tuple[dict[str, Any], set[str]]] = []
    for entry in entries.values():
        if entry["status"] != "ACTIVE":
            continue
        orgs = _candidate_orgs(entry["business_name"], name_to_orgs)
        if not orgs:
            continue
        result.candidate_ads += 1
        for org in orgs:
            result.name_candidates_by_org[org] = result.name_candidates_by_org.get(org, 0) + 1
        candidates.append((entry, orgs))
    candidates.sort(key=lambda pair: pair[0].get("modified") or "", reverse=True)

    for entry, candidate_orgs in candidates:
        eligible_orgs = {org for org in candidate_orgs if per_org.get(org, 0) < 15}
        if not eligible_orgs:
            continue
        for org in eligible_orgs:
            per_org[org] = per_org.get(org, 0) + 1
        if result.detail_requests >= max_details:
            result.error = result.error or "nav_detail_cap_reached"
            result.complete = False
            break
        detail_url = urljoin(FEED_ORIGIN, entry["url"])
        if not _safe_url(detail_url):
            for org in eligible_orgs:
                result.rejected_by_org[org] = result.rejected_by_org.get(org, 0) + 1
            continue
        if delay_seconds:
            time.sleep(delay_seconds)
        request_count_before = result.metrics["requests"]
        status, _detail_headers, raw, error = _request(
            detail_url, token=token, accept="application/json", budget=budget, metrics=result.metrics
        )
        if result.metrics["requests"] > request_count_before:
            result.detail_requests += 1
        if error:
            if error == "request_budget_exhausted":
                result.error = error
                result.complete = False
                break
            continue
        if status != 200:
            continue
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        ad = payload.get("ad_content") or payload.get("json") or payload
        if isinstance(ad, dict) and isinstance(ad.get("json"), dict):
            ad = ad["json"]
        if not isinstance(ad, dict) or str(payload.get("status") or ad.get("status") or "").upper() != "ACTIVE":
            continue
        employer = ad.get("employer") if isinstance(ad.get("employer"), dict) else {}
        employer_org = re.sub(r"\D", "", str(employer.get("orgnr") or ""))
        if len(employer_org) != 9:
            for org in eligible_orgs:
                result.rejected_by_org[org] = result.rejected_by_org.get(org, 0) + 1
            continue
        matched: dict[str, str] = {}
        for org in eligible_orgs:
            if employer_org == org:
                matched[org] = "exact_employer_number"
            elif subunit_to_parent.get(employer_org) == org:
                matched[org] = "brreg_subunit_parent_mapping"
        if not matched:
            for org in eligible_orgs:
                result.rejected_by_org[org] = result.rejected_by_org.get(org, 0) + 1
            continue
        retrieved_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        digest = hashlib.sha256(raw).hexdigest()
        expiry = str(ad.get("expires") or "")[:10]
        today = datetime.now(timezone.utc).date().isoformat()
        if expiry and expiry < today:
            continue
        capture = capture_source(raw)
        for org, match_method in matched.items():
            result.jobs_by_org.setdefault(org, []).append({
                **capture,
                "uuid": str(ad.get("uuid") or entry["uuid"]),
                "status": "ACTIVE",
                "title": str(ad.get("title") or ad.get("jobtitle") or "").strip()[:1000],
                "employer_name": str(employer.get("name") or entry["business_name"]).strip()[:500],
                "employer_orgnr": employer_org,
                "parent_orgnr": org if match_method == "brreg_subunit_parent_mapping" else employer_org,
                "employer_homepage": str(employer.get("homepage") or "").strip()[:2000] or None,
                "published": str(ad.get("published") or "")[:40] or None,
                "expires": expiry or None,
                "modified": str(ad.get("updated") or entry.get("modified") or "")[:40] or None,
                "municipality": next((str(loc.get("municipal") or loc.get("city")) for loc in ad.get("workLocations") or [] if isinstance(loc, dict) and (loc.get("municipal") or loc.get("city"))), None),
                "detail_url": detail_url,
                "application_url": str(ad.get("applicationUrl") or "").strip()[:2000] or None,
                "content_sha256": digest,
                "retrieved_at": retrieved_at,
                "identity_method": match_method,
            })
    return result
