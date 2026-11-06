#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections import Counter
import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent.batch import profile_complete_for_modules, profiles_from_bulk, read_organisation_inputs, terminal_envelope, validate_envelopes  # noqa: E402
from norway_company_agent.search import search_company_websites  # noqa: E402
from norway_company_agent.search_providers import SEARCH_REQUEST_CEILING, provider_summary, configure_search_budget, set_search_company, plan_search_budget, company_usage  # noqa: E402
from norway_company_agent.evidence import evidence, utc_now  # noqa: E402
from norway_company_agent.identity import _tokens, apply_website_identity_gate, assess_social_identity  # noqa: E402
from norway_company_agent.local_cache import LocalCompanyCache, cache_coverage, _candidate_key  # noqa: E402
from norway_company_agent.nav_jobs import NavBatchResult, discover_nav_jobs  # noqa: E402
from norway_company_agent.official import fetch_official_modules  # noqa: E402
from norway_company_agent.discovery import BLOCKED_DISCOVERY_HOSTS  # noqa: E402
from norway_company_agent.research import summarize_company  # noqa: E402
from norway_company_agent.site_discovery import discover_site_activity, site_news_observations  # noqa: E402
from norway_company_agent.website import fetch_website, normalize_homepage, normalize_social_url  # noqa: E402
from norway_company_agent import search as search_client
from norway_company_agent import website as website_client  # noqa: E402
from norway_company_agent import network_meter  # noqa: E402
from norway_company_agent.candidate_sources import domain_guesses, relationship_candidates, subunit_guesses, operating_names  # noqa: E402
from norway_company_agent.site_jobs import extract_site_jobs  # noqa: E402
from norway_company_agent.external_collectors import enrich_external, coverage_summary  # noqa: E402
from norway_company_agent.external_footprint import aggregate_footprint  # noqa: E402
from norway_company_agent.refresh import diff_datasets  # noqa: E402
from norway_company_agent.source_types import directory_url  # noqa: E402


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    temporary.replace(path)


def record_attempt(
    profile: dict,
    *,
    module: str,
    source: str,
    strategy: str,
    outcome: str,
    reason: str | None = None,
    candidate_url: str | None = None,
    provider_status: str | None = None,
    requests: int = 0,
    identity: dict | None = None,
    evidence_url: str | None = None,
    evidence_sha256: str | None = None,
) -> None:
    """Append an auditable acquisition attempt without inventing evidence provenance."""
    rows = profile.setdefault("acquisition_attempts", [])
    rows.append({
        "sequence": len(rows) + 1,
        "organisation_number": str(profile.get("organisation_number") or ""),
        "module": module,
        "source": source,
        "strategy": strategy,
        "outcome": outcome,
        "reason": reason,
        "candidate_url": candidate_url,
        "provider_status": provider_status,
        "requests": max(0, int(requests)),
        "identity": identity,
        "evidence_url": evidence_url,
        "evidence_sha256": evidence_sha256,
        "recorded_at": utc_now(),
    })


class WebRequestBudget:
    """Atomic reservation for non-registry requests across the whole batch."""

    def __init__(self, limit: int) -> None:
        self.limit = max(0, int(limit))
        self.used = 0
        self.reserved = 0
        self.lock = threading.Lock()

    def for_company(self, limit: int) -> "CompanyWebRequestBudget":
        return CompanyWebRequestBudget(self, limit)

    def reserve(self, cost: int) -> bool:
        cost = max(0, int(cost))
        with self.lock:
            if self.used + self.reserved + cost > self.limit:
                return False
            self.reserved += cost
            return True

    def settle(self, reserved: int, actual: int) -> None:
        reserved = max(0, int(reserved))
        actual = max(0, int(actual))
        with self.lock:
            self.reserved = max(0, self.reserved - reserved)
            self.used += actual


class CompanyWebRequestBudget:
    def __init__(self, parent: WebRequestBudget, limit: int) -> None:
        self.parent = parent
        self.limit = max(0, int(limit))
        self.used = 0
        self.reserved = 0

    def reserve(self, cost: int) -> bool:
        cost = max(0, int(cost))
        with self.parent.lock:
            if self.used + self.reserved + cost > self.limit:
                return False
            if self.parent.used + self.parent.reserved + cost > self.parent.limit:
                return False
            self.reserved += cost
            self.parent.reserved += cost
            return True

    def settle(self, reserved: int, actual: int) -> None:
        reserved = max(0, int(reserved))
        actual = max(0, int(actual))
        with self.parent.lock:
            self.reserved = max(0, self.reserved - reserved)
            self.parent.reserved = max(0, self.parent.reserved - reserved)
            self.used += actual
            self.parent.used += actual


def relevant_search_candidate(profile: dict, result: dict) -> bool:
    ordered_name_tokens = list(dict.fromkeys(_tokens(profile.get("name"))))
    core = set(ordered_name_tokens)
    if not core:
        return False
    url = str(result.get("url") or "")
    host = (urlparse(url).hostname or "").casefold().removeprefix("www.")
    if directory_url(url):
        return False
    if any(host == blocked or host.endswith("." + blocked) for blocked in BLOCKED_DISCOVERY_HOSTS):
        return False
    title = str(result.get("title") or "")
    snippet = str(result.get("snippet") or "")
    title_tokens = set(_tokens(title))
    host_tokens = set(_tokens(host))
    context_tokens = set(_tokens(f"{title} {snippet}"))
    evidence_tokens = title_tokens | host_tokens | set(_tokens(snippet))
    overlap = core & evidence_tokens
    org = "".join(char for char in str(profile.get("organisation_number") or "") if char.isdigit())
    org_match = bool(org and any(
        org in "".join(char for char in text if char.isdigit())
        for text in (url, title, snippet)
    ))
    # Search result relevance is only a crawl budget gate. The fetched page still
    # has to pass the strict exact-company identity check before publication.
    if org_match and overlap:
        return True
    # Parent-linked official operating names can unlock a crawl. They do not
    # change the legal-entity publication gate after the page has been fetched.
    for alias in profile.get("discovery_operating_names") or []:
        alias_tokens = set(_tokens(alias))
        if alias_tokens and alias_tokens.issubset(evidence_tokens):
            return True
    if core.issubset(evidence_tokens):
        return True
    distinctive = {token for token in core if len(token) >= 5}
    if not distinctive:
        return False
    municipality = set(_tokens(profile.get("municipality")))
    municipality_match = bool(municipality and municipality.issubset(context_tokens))
    # A single distinctive brand token in the result title or company-like host
    # is enough to try; rejecting it here was suppressing all recovery searches
    # even though the fetched-page gate can safely reject false leads.
    distinctive_hit = distinctive & (title_tokens | host_tokens)
    if distinctive_hit and (len(core) == 1 or len(overlap) / len(core) >= 0.34):
        return True
    return municipality_match and bool(overlap) and len(overlap) / len(core) >= 0.34


def website_search_queries(profile: dict) -> list[str]:
    """Small query ladder: exact legal entity, brand plus place, then org-number anchored."""
    name = " ".join(str(profile.get("name") or "").split())
    org = "".join(char for char in str(profile.get("organisation_number") or "") if char.isdigit())
    municipality = " ".join(str(profile.get("municipality") or "").split())
    brand = name
    for _ in range(2):
        stripped = re.sub(r"\s+(?:AS|ASA|ANS|DA|ENK|SA|NUF|KS|BA|BRL|BBL|FLI|IKS|SF)\.?$", "", brand, flags=re.I).strip()
        if stripped == brand:
            break
        brand = stripped
    candidates = [
        f'"{name}" -site:proff.no -site:1881.no -site:dnb.com',
        (f'"{profile["discovery_operating_names"][0]}" "{municipality}"'
         if profile.get("discovery_operating_names") else
         f'"{brand}" "{municipality}"' if brand and municipality else f'"{brand}" Norway'),
        f'"{org}" "{name}"' if org and name else f'"{org}"',
    ]
    output: list[str] = []
    seen: set[str] = set()
    for query in candidates:
        query = " ".join(query.split())
        if query and query.casefold() not in seen:
            seen.add(query.casefold())
            output.append(query)
    return output


def assess_search_social_candidate(profile: dict, result: dict) -> dict | None:
    """Score a search-discovered handle as a lead; this does not make it publishable evidence."""
    raw_url = str(result.get("url") or "")
    normalized = normalize_social_url(raw_url)
    if not normalized:
        return None
    assessment = assess_social_identity(profile, normalized)
    if not assessment.get("publishable"):
        return None
    context = " ".join(str(result.get(key) or "") for key in ("title", "snippet")).casefold()
    company_name = str(profile.get("name") or "").casefold().strip()
    orgnr = str(profile.get("organisation_number") or "")
    municipality = str(profile.get("municipality") or "").casefold().strip()
    corroborated = bool(company_name and company_name in context) or bool(orgnr and orgnr in context)
    if len(_tokens(company_name)) <= 1:
        corroborated = corroborated or bool(municipality and len(municipality) >= 4 and municipality in context)
        if not corroborated:
            return None
    elif not corroborated:
        # Legal-name tokens in a handle identify a candidate; the result itself must
        # independently repeat the exact name or organisation number.
        return None
    return {
        **assessment,
        "url": normalized["url"],
        "platform": normalized["platform"],
        "title": str(result.get("title") or "")[:500],
        "snippet": str(result.get("snippet") or "")[:1000],
        "discovery_source": result.get("provider") or "web_search",
        "retrieved_at": utc_now(),
    }




def nav_observations(profile: dict, nav_batch: NavBatchResult) -> list[dict]:
    observations = []
    nav_jobs = nav_batch.jobs_by_org.get(profile["organisation_number"], [])
    nav_job_count = 0
    for job in nav_jobs:
        employer_org = str(job.get("employer_orgnr") or "")
        parent_org = str(job.get("parent_orgnr") or "")
        org = str(profile["organisation_number"])
        direct_match = employer_org == org
        subunit_match = not direct_match and parent_org == org and len(employer_org) == 9
        digest = str(job.get("content_sha256") or "")
        source_url = str(job.get("detail_url") or "")
        if not (direct_match or subunit_match) or len(digest) != 64 or not source_url.startswith("https://pam-stilling-feed.nav.no/"):
            continue
        observations.append({
            "id": "nav-job-" + str(job.get("uuid") or ""),
            "organisation_number": org,
            "platform": "job_board",
            "signal_type": "job_posting",
            "source_url": source_url,
            "retrieved_at": job.get("retrieved_at"),
            "content_sha256": digest,
            "snapshot_sha256": job.get("snapshot_sha256"),
            "snapshot_path": job.get("snapshot_path"),
            "exact_entity": True,
            "identity_proof": [{
                "type": "nav_employer_organisation_number",
                "employer_organisation_number": employer_org,
                "parent_organisation_number": parent_org if subunit_match else None,
                "method": "exact_employer_number" if direct_match else "brreg_subunit_parent_mapping",
            }],
            "acquisition_mode": "official_api",
            "rights_status": "approved",
            "source_class": "official_nav_job_feed",
            "metrics": {
                "title": job.get("title"),
                "job_title": job.get("title"),
                "status": "ACTIVE",
                "employer_name": job.get("employer_name"),
                "published": job.get("published"),
                "expires": job.get("expires"),
                "municipality": job.get("municipality"),
                "application_url": job.get("application_url"),
            },
            "strategy": "nav_feed_shared_batch_scan",
        })
        nav_job_count += 1
    return observations


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluator-owned Signalpost batch contract")
    parser.add_argument("--organisations", required=True, help="JSON, JSONL, or text organisation-number list")
    parser.add_argument("--bulk", required=True, help="Frozen Brreg entity snapshot")
    parser.add_argument("--output", required=True, help="Terminal envelope JSONL")
    parser.add_argument("--profiles-output", required=True)
    parser.add_argument("--attempts-output", default=None, help="Structured acquisition-attempt JSONL (defaults beside --report)")
    parser.add_argument("--report", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--expected-count", type=int, default=100)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--paid-search-budget", type=int, default=2000, help="Shared provider-credit ceiling for the whole run: search/news/Places=1, Maps=3; failed and fallback attempts reserve credits too")
    parser.add_argument("--paid-search-per-company", type=int, default=2)
    parser.add_argument("--external-request-cap", type=int, default=18)
    parser.add_argument("--checkpoint-every", type=int, default=25)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--previous-profiles", type=Path, help="Previous profiles with the identical organisation membership for material change detection")
    parser.add_argument("--changes-output", type=Path)
    parser.add_argument("--cache-dir", default=None, help="Company-keyed discovery cache (defaults to data/local-cache)")
    parser.add_argument("--fresh-web", action="store_true", help="Ignore persistent website responses for a cold-network comparison; retain within-run deduplication")
    parser.add_argument("--request-budget", type=int, default=2000, help="Maximum counted network requests for the full run, including registry calls")
    parser.add_argument("--web-requests-per-company", type=int, default=30, help="Maximum website/search/feed requests for one company")
    parser.add_argument("--generated-site-candidate-cap", type=int, default=3, help="Maximum guessed domains making HTTP attempts per company; DNS-only failures do not consume the cap")
    parser.add_argument("--site-activity-request-cap", type=int, default=16, help="Maximum estimated requests for verified-site feed/sitemap discovery per company")
    parser.add_argument("--nav-window-days", type=int, default=45, help="Recent NAV feed window shared across the batch")
    parser.add_argument("--nav-max-pages", type=int, default=120, help="Maximum shared NAV feed pages per batch")
    parser.add_argument("--nav-max-details", type=int, default=200, help="Maximum name-matched NAV ad detail fetches per batch")
    parser.add_argument("--nav-delay-seconds", type=float, default=0.1, help="Delay between shared NAV requests")
    parser.add_argument("--modules", default="registry,accounting_obligation,registry_live,financials,roles,group,locations,website,external_footprint")
    args = parser.parse_args()
    configure_search_budget(args.paid_search_budget, args.paid_search_per_company)
    website_client.READ_DISK_CACHE = not args.fresh_web
    search_client.READ_SEARCH_CACHE = not args.fresh_web
    network_meter.activate(args.request_budget)
    attempts_output = Path(args.attempts_output) if args.attempts_output else Path(args.report).with_suffix(".attempts.jsonl")

    started_at = utc_now()
    organisation_inputs = read_organisation_inputs(args.organisations)
    orgs = [item["organisation_number"] for item in organisation_inputs]
    previous_profiles = None
    if args.previous_profiles:
        previous_profiles = [json.loads(line) for line in args.previous_profiles.read_text(encoding="utf-8").splitlines() if line.strip()]
        if len(previous_profiles) != len(orgs) or {p["organisation_number"] for p in previous_profiles} != set(orgs):
            parser.error("Previous profiles must contain exactly the same organisation membership")
    if len(orgs) != args.expected_count:
        raise SystemExit(f"Expected {args.expected_count} organisations, received {len(orgs)}")
    profiles, registry_metadata = profiles_from_bulk(args.bulk, orgs)
    local_cache = LocalCompanyCache(args.cache_dir) if args.cache_dir else LocalCompanyCache()
    priority_units = local_cache.brreg_subunits_for_parents(orgs)
    for profile in profiles:
        profile["discovery_operating_names"] = operating_names(profile, priority_units.get(profile["organisation_number"], []))
    plan_search_budget(profiles, args.paid_search_budget, args.paid_search_per_company)
    annotations = {item["organisation_number"]: item for item in organisation_inputs}
    for profile in profiles:
        for key in ("evaluation_split", "sample_slice"):
            if key in annotations[profile["organisation_number"]]:
                profile[key] = annotations[profile["organisation_number"]][key]
    requested_modules = [item.strip() for item in args.modules.split(",") if item.strip()]
    fetch_modules = set(requested_modules) - {"registry", "accounting_obligation", "website", "external_footprint"}
    expected_official_requests = len(profiles) * len(fetch_modules)
    network_meter.reserve_official(expected_official_requests)
    web_request_budget = WebRequestBudget(max(0, args.request_budget - expected_official_requests))
    if min(args.nav_window_days, args.nav_max_pages, args.nav_max_details) < 0 or args.nav_delay_seconds < 0:
        raise SystemExit("NAV window, request caps, and delay must be non-negative")
    nav_batch = NavBatchResult(error="not_requested")
    operations = {
        "requests": nav_batch.metrics["requests"],
        "official_requests": 0,
        "web_requests": nav_batch.metrics["requests"],
        "bytes": nav_batch.metrics["bytes"],
        "latencies_ms": list(nav_batch.metrics["latencies_ms"]),
    }

    def enrich(profile: dict) -> tuple[dict, dict]:
        company_started = time.monotonic()
        set_search_company(profile["organisation_number"])
        profile["acquisition_attempts"] = []
        registry_record = profile.get("evidence", {}).get("registry") or {}
        record_attempt(
            profile, module="registry", source="frozen_brreg_snapshot", strategy="registry_snapshot_lookup",
            outcome="published" if registry_record.get("status") == "available" else "no_match",
            reason=registry_record.get("note"), candidate_url=registry_record.get("source_url"),
            evidence_url=registry_record.get("source_url"), evidence_sha256=registry_record.get("content_sha256") or registry_record.get("snapshot_sha256"),
        )
        records, metrics = fetch_official_modules(profile["organisation_number"], fetch_modules)
        profile["evidence"].update(records)
        for module, record in records.items():
            status = str(record.get("status") or "source_error")
            outcome = "published" if status == "available" else "provider_error" if status in {"source_error", "failed"} else "fetch_blocked" if status == "blocked" else "no_match"
            record_attempt(
                profile, module=module, source=str(record.get("source_type") or "official_registry"),
                strategy="official_module_fetch", outcome=outcome, reason=record.get("note"),
                candidate_url=record.get("source_url"), requests=1 if module in fetch_modules else 0,
                evidence_url=record.get("source_url"), evidence_sha256=record.get("content_sha256") or record.get("snapshot_sha256"),
            )
        company_web_budget = web_request_budget.for_company(args.web_requests_per_company)
        website_metrics = {"requests": 0, "bytes": 0, "latencies_ms": []}
        if "website" in requested_modules:
            website_record = None
            website_metrics = {"requests": 0, "bytes": 0, "latencies_ms": []}
            live_registry = (profile.get("evidence", {}).get("registry_live") or {}).get("value") or {}
            frozen_registry = (profile.get("evidence", {}).get("registry") or {}).get("value") or {}
            registry_email = str(live_registry.get("epostadresse") or frozen_registry.get("epostadresse") or "").strip()
            email_domain = registry_email.rsplit("@", 1)[-1].casefold() if "@" in registry_email else ""
            generic_mail_hosts = {"gmail.com", "hotmail.com", "outlook.com", "live.no", "yahoo.com", "icloud.com", "online.no", "mail.com"}
            email_site = f"https://{email_domain}/" if email_domain and email_domain not in generic_mail_hosts and "." in email_domain else None
            cached_subunits = local_cache.brreg_subunits(profile["organisation_number"])
            profile["discovery_operating_names"] = operating_names(profile, cached_subunits)
            name_guesses = domain_guesses(str(profile.get("name") or ""), str(profile.get("municipality") or ""))
            candidates = [
                (profile.get("website"), "bulk_registry"),
                (live_registry.get("website"), "live_registry"),
                (email_site, "registry_email_domain"),
                *[(item["employer_homepage"], "nav_employer_homepage") for item in nav_batch.jobs_by_org.get(profile["organisation_number"], []) if item.get("employer_homepage")],
                *[(item["url"], item["source"]) for item in local_cache.website_candidate_records(profile["organisation_number"])],
                *relationship_candidates(profile, cached_subunits),
                *[(url, "generated_name_guess") for url in name_guesses[:2]],
                *[(url, "generated_subunit_guess") for url in subunit_guesses(profile, cached_subunits)],
                *[(url, "generated_name_guess") for url in name_guesses[2:]],
            ]
            seen_candidates: set[str] = set()
            verified_site = False
            candidate_attempts: list[dict] = []
            generated_attempts = 0
            for candidate_index, (raw_candidate, candidate_source) in enumerate(candidates):
                candidate = raw_candidate
                candidate = str(candidate or "").strip()
                if not candidate or _candidate_key(candidate) in seen_candidates:
                    continue
                seen_candidates.add(_candidate_key(candidate))
                if candidate_source.startswith("generated_"):
                    if generated_attempts >= args.generated_site_candidate_cap:
                        continue
                if not company_web_budget.reserve(12):
                    candidate_attempts.append({"url": candidate, "source": candidate_source, "result": "request_budget_exhausted"})
                    record_attempt(profile, module="website", source=candidate_source, strategy="website_candidate_fetch", outcome="budget_exhausted", candidate_url=candidate, reason="Per-company or run-level web request allowance could not reserve the fetch ceiling.")
                    break
                candidate_record, candidate_metrics = fetch_website(candidate)
                # Registered/cached links can point to removed pages while the
                # origin still operates. Try that origin next after a 404/410;
                # never change protocol or work around a policy refusal.
                parsed_candidate = urlparse(normalize_homepage(candidate) or "")
                if candidate_record.get("status") == "not_found" and parsed_candidate.path.strip("/"):
                    root_url = f"{parsed_candidate.scheme}://{parsed_candidate.netloc}/"
                    if _candidate_key(root_url) not in seen_candidates:
                        candidates.insert(candidate_index + 1, (root_url, candidate_source + "_root_recovery"))
                if candidate_source.startswith("generated_") and candidate_metrics["requests"] > 0:
                    generated_attempts += 1
                company_web_budget.settle(12, candidate_metrics["requests"])
                website_metrics["requests"] += candidate_metrics["requests"]
                website_metrics["bytes"] += candidate_metrics["bytes"]
                website_metrics["latencies_ms"].extend(candidate_metrics["latencies_ms"])
                if candidate_record.get("status") == "available":
                    candidate_value = candidate_record.setdefault("value", {})
                    cached_socials = local_cache.social_candidates(profile["organisation_number"])
                    site_socials = [
                        {**item, "discovery_source": "verified_site_link", "evidence_url": item.get("evidence_url") or candidate_value.get("final_url"), "evidence_sha256": item.get("evidence_sha256") or candidate_value.get("content_sha256")}
                        for item in (candidate_value.get("social_links") or [])
                    ]
                    cached_socials = [
                        {**item, "discovery_source": "local_cache", "evidence_url": None, "evidence_sha256": None}
                        for item in cached_socials
                    ]
                    site_socials = [
                        {**item, "discovery_source": "verified_site_link", "evidence_url": item.get("evidence_url") or candidate_value.get("final_url"), "evidence_sha256": item.get("evidence_sha256") or candidate_value.get("content_sha256")}
                        for item in site_socials
                    ]
                    candidate_value["discovered_social_links"] = list({
                        (item["platform"], item["url"]): item
                        for item in [*cached_socials, *site_socials]
                    }.values())
                gated = apply_website_identity_gate(profile, candidate_record, candidate_source=candidate_source)
                website_record = gated["website"]
                assessment = gated.get("assessment") or {}
                candidate_attempts.append({
                    "url": candidate,
                    "source": candidate_source,
                    "result": "verified" if assessment.get("publishable") else candidate_record.get("status"),
                    "identity_score": assessment.get("score"),
                    "identity_reasons": assessment.get("reasons", []),
                    "requests": candidate_metrics["requests"],
                })
                status = str(candidate_record.get("status") or "source_error")
                outcome = (
                    "published" if assessment.get("publishable") else
                    "identity_rejected" if status == "available" else
                    "fetch_blocked" if status == "blocked" else
                    "provider_error" if status in {"source_error", "failed"} else
                    "not_found"
                )
                record_attempt(
                    profile, module="website", source=candidate_source, strategy="website_candidate_fetch",
                    outcome=outcome, reason="; ".join(assessment.get("reasons") or []) or candidate_record.get("note"),
                    candidate_url=candidate, requests=candidate_metrics["requests"], identity=assessment or None,
                    evidence_url=(candidate_record.get("value") or {}).get("final_url") or candidate_record.get("source_url"),
                    evidence_sha256=(candidate_record.get("value") or {}).get("content_sha256") or candidate_record.get("content_sha256"),
                )
                if assessment.get("publishable"):
                    verified_site = True
                    if candidate_source != "bulk_registry":
                        website_record["source_type"] = f"{candidate_source}_candidate_then_live_identity_check"
                        website_record["source_class"] = "live_company_website_identity_verified"
                    break
            if not verified_site and profile.get("name"):
                # Treat unrelated non-empty results as a failed retrieval stage and
                # try a bounded query ladder. Search results are leads only; every
                # fetched site still has to pass the exact-company identity gate.
                try:
                    queries = website_search_queries(profile)
                    discovery = {
                        "strategy": "bounded_relevance_aware_query_ladder",
                        "queries": [],
                        "candidate_count": 0,
                        "name_relevant_candidates": 0,
                        "search_requests": 0,
                    }
                    attempted_search_hosts: set[str] = set()
                    fetched_search_candidates = 0
                    for query_index, query in enumerate(queries, start=1):
                        if query_index > 2 and not discovery["name_relevant_candidates"]:
                            discovery["stopped_after_unproductive_searches"] = True
                            break
                        if fetched_search_candidates >= 2:
                            break
                        if not company_web_budget.reserve(SEARCH_REQUEST_CEILING):
                            discovery["budget_exhausted"] = True
                            record_attempt(profile, module="website", source="configured_search_provider_or_public_fallback", strategy="company_name_search", outcome="budget_exhausted", reason=f"Search query {query_index} could not reserve its bounded search-request ceiling.")
                            break
                        try:
                            search_results, search_requests, search_status = search_company_websites(query, timeout=4.0, use_paid=query_index == 1)
                        except Exception as exc:
                            search_results, search_requests, search_status = [], 0, "provider_error"
                            search_error = f"{type(exc).__name__}: {str(exc)[:160]}"
                        else:
                            search_error = None
                        company_web_budget.settle(SEARCH_REQUEST_CEILING, search_requests)
                        website_metrics["requests"] += search_requests
                        relevant_results = [result for result in search_results if relevant_search_candidate(profile, result)]
                        profile.setdefault('discovery_search_leads', []).extend(search_results)
                        discovery["candidate_count"] += len(search_results)
                        discovery["name_relevant_candidates"] += len(relevant_results)
                        discovery["search_requests"] += search_requests
                        discovery["queries"].append({
                            "query": query,
                            "provider_status": search_status,
                            "candidate_count": len(search_results),
                            "name_relevant_candidates": len(relevant_results),
                            "search_requests": search_requests,
                        })
                        search_outcome = (
                            "provider_error" if search_status in {"provider_error", "circuit_open", "parse_failure"} else
                            "budget_exhausted" if search_status == "budget_exhausted" else
                            "candidate_found" if relevant_results else
                            "results_without_entity_match" if search_results else
                            "empty_results" if search_status in {"empty_result", "cache_hit_empty"} else
                            "no_candidate"
                        )
                        record_attempt(
                            profile, module="website", source="configured_search_provider_or_public_fallback", strategy=f"company_name_search_{query_index}",
                            outcome=search_outcome,
                            reason=search_error or f"Search status={search_status}; {len(relevant_results)} name-relevant candidate(s) from {len(search_results)} result(s).",
                            provider_status=search_status, requests=search_requests,
                        )
                        if relevant_results:
                            for result in relevant_results:
                                candidate_url = normalize_homepage(str(result.get("url") or ""))
                                if not candidate_url or _candidate_key(candidate_url) in seen_candidates or normalize_social_url(candidate_url):
                                    continue
                                host = (urlparse(candidate_url).hostname or "").casefold().removeprefix("www.")
                                if host in attempted_search_hosts:
                                    continue
                                attempted_search_hosts.add(host)
                                seen_candidates.add(_candidate_key(candidate_url))
                                if fetched_search_candidates >= 2:
                                    break
                                if not company_web_budget.reserve(12):
                                    candidate_attempts.append({"url": candidate_url, "source": "web_search", "result": "request_budget_exhausted"})
                                    record_attempt(profile, module="website", source="web_search", strategy="website_candidate_fetch", outcome="budget_exhausted", candidate_url=candidate_url, reason="Per-company or run-level web request allowance could not reserve the fetch ceiling.")
                                    break
                                fetched_search_candidates += 1
                                candidate_record, candidate_metrics = fetch_website(candidate_url, timeout=8.0)
                                company_web_budget.settle(12, candidate_metrics["requests"])
                                website_metrics["requests"] += candidate_metrics["requests"]
                                website_metrics["bytes"] += candidate_metrics["bytes"]
                                website_metrics["latencies_ms"].extend(candidate_metrics["latencies_ms"])
                                gated = apply_website_identity_gate(profile, candidate_record, candidate_source="web_search")
                                website_record = gated["website"]
                                assessment = gated.get("assessment") or {}
                                candidate_attempts.append({
                                    "url": candidate_url,
                                    "source": "web_search",
                                    "result": "verified" if assessment.get("publishable") else candidate_record.get("status"),
                                    "identity_score": assessment.get("score"),
                                    "identity_reasons": assessment.get("reasons", []),
                                    "requests": candidate_metrics["requests"],
                                })
                                status = str(candidate_record.get("status") or "source_error")
                                outcome = (
                                    "published" if assessment.get("publishable") else
                                    "identity_rejected" if status == "available" else
                                    "fetch_blocked" if status == "blocked" else
                                    "provider_error" if status in {"source_error", "failed"} else
                                    "not_found"
                                )
                                record_attempt(
                                    profile, module="website", source="web_search", strategy="website_candidate_fetch",
                                    outcome=outcome, reason="; ".join(assessment.get("reasons") or []) or candidate_record.get("note"),
                                    candidate_url=candidate_url, requests=candidate_metrics["requests"], identity=assessment or None,
                                    evidence_url=(candidate_record.get("value") or {}).get("final_url") or candidate_record.get("source_url"),
                                    evidence_sha256=(candidate_record.get("value") or {}).get("content_sha256") or candidate_record.get("content_sha256"),
                                )
                                if assessment.get("publishable"):
                                    verified_site = True
                                    website_record["source_type"] = "search_candidate_then_live_identity_check"
                                    website_record["source_class"] = "live_company_website_identity_verified"
                                    discovery["verified_candidate"] = candidate_url
                                    break
                        if verified_site:
                            break
                        # A non-empty provider response with no usable entity lead is
                        # a retrieval failure; spend the next bounded query to recover.
                    profile["website_discovery"] = discovery
                except Exception as exc:
                    profile["website_discovery"] = {"strategy": "multi_engine_name_search", "error": f"{type(exc).__name__}: {str(exc)[:120]}"}
            elif not verified_site:
                record_attempt(profile, module="website", source="configured_search_provider_or_public_fallback", strategy="company_name_search", outcome="not_searched", reason="No registered company name was available for website search.")
            profile.setdefault("website_discovery", {})["candidate_attempts"] = candidate_attempts
            if not verified_site and website_record and website_record.get("status") == "available":
                website_record = evidence(
                    "website",
                    "not_found",
                    "verified_company_website_discovery",
                    str(profile.get("website") or "https://data.brreg.no/enhetsregisteret/api/enheter/" + profile["organisation_number"]),
                    note="Candidates were fetched, but none passed the exact-company identity check; rejected pages were not published.",
                )
            if website_record is None:
                website_record = fetch_website(None)[0]
            profile["evidence"]["website"] = website_record
            if verified_site:
                local_cache.remember_verified_site(profile["organisation_number"], website_record)
        if "external_footprint" in requested_modules:
            website_record = profile["evidence"].get("website") or {}
            website_value = website_record.get("value") or {}
            website_value.setdefault('retrieved_at', website_record.get('retrieved_at'))
            identity = website_value.get("identity_assessment") or {}
            observations = []
            social_candidates = []
            social_observations = []
            # The NAV window is fetched once for the batch; only exact-number
            # employer matches from its name-shortlisted details are publishable.
            observations.extend(nav_observations(profile, nav_batch))
            nav_job_count = len(observations)
            nav_name_candidates = nav_batch.name_candidates_by_org.get(profile["organisation_number"], 0)
            nav_identity_rejections = nav_batch.rejected_by_org.get(profile["organisation_number"], 0)
            if nav_job_count:
                nav_attempt_outcome = "published"
                nav_attempt_reason = f"Found {nav_job_count} active, unexpired vacancy/vacancies with an exact employer organisation-number match. Shared NAV scan: {nav_batch.pages} page(s)."
            elif nav_identity_rejections:
                nav_attempt_outcome = "identity_rejected"
                nav_attempt_reason = f"Checked {nav_name_candidates} name-matched ad(s); {nav_identity_rejections} had a different or missing employer organisation number."
            elif nav_batch.complete:
                nav_attempt_outcome = "no_candidate"
                nav_attempt_reason = f"Completed the shared {args.nav_window_days}-day NAV feed scan ({nav_batch.pages} page(s)); no active ad matched this company's or its subunit's registered name."
            elif nav_batch.error and ("budget" in nav_batch.error or "cap_reached" in nav_batch.error):
                nav_attempt_outcome = "budget_exhausted"
                nav_attempt_reason = f"The shared NAV scan was incomplete ({nav_batch.error}); no zero-result conclusion is made."
            elif nav_batch.error:
                nav_attempt_outcome = "provider_error"
                nav_attempt_reason = f"The shared NAV scan was incomplete ({nav_batch.error}); no zero-result conclusion is made."
            else:
                nav_attempt_outcome = "not_searched"
                nav_attempt_reason = "NAV job discovery was not requested for this run."
            record_attempt(
                profile, module="external_footprint", source="nav_job_vacancy_feed",
                strategy="nav_feed_shared_batch_scan",
                outcome=nav_attempt_outcome,
                reason=nav_attempt_reason,
                evidence_url="https://pam-stilling-feed.nav.no/api/v1/feed",
            )
            seen_social_platforms: set[str] = set()
            site_jobs = extract_site_jobs(profile)
            observations.extend(site_jobs)
            if site_jobs:
                record_attempt(profile, module="external_footprint", source="verified_company_site", strategy="captured_structured_jobposting", outcome="published", reason=f"Found {len(site_jobs)} current, employer-matched JobPosting record(s) on captured role pages.", requests=0)
            site_assessments = website_value.get("social_link_assessments") or []
            for link in site_assessments:
                platform, url = link.get("platform"), link.get("url")
                provenance = str(link.get("discovery_source") or "unknown")
                if not platform or not url:
                    continue
                if provenance == "verified_site_link" and website_record.get("status") == "available" and identity.get("publishable") and link.get("publishable"):
                    if platform in seen_social_platforms:
                        continue
                    seen_social_platforms.add(platform)
                    page_hash = str(link.get("evidence_sha256") or website_value.get("content_sha256") or "")
                    source_page = str(link.get("evidence_url") or website_value.get("final_url") or website_record.get("source_url") or "")
                    if len(page_hash) != 64 or not source_page:
                        record_attempt(profile, module="external_footprint", source=provenance, strategy="social_profile_link", outcome="evidence_missing", candidate_url=url, reason="Verified-site social link lacks its supporting page URL or capture hash.")
                        continue
                    social_observations.append({
                        "id": "social-" + __import__("hashlib").sha256(f"{profile['organisation_number']}|{platform}|{url}".encode()).hexdigest()[:24],
                        "organisation_number": profile["organisation_number"],
                        "platform": platform,
                        "signal_type": "profile_handle",
                        "source_url": url,
                        "retrieved_at": website_record.get("retrieved_at") or utc_now(),
                        "content_sha256": page_hash,
                        "exact_entity": True,
                        "identity_proof": [{"type": "verified_company_site_outbound_link", "evidence_url": source_page, "score": link.get("identity_score"), "method": link.get("method"), "matched_tokens": link.get("matched_tokens")}],
                        "acquisition_mode": "permitted_public_page",
                        "rights_status": "approved",
                        "source_class": "company_social",
                        "evidence_span": f"{platform} profile URL linked from the identity-verified company page {source_page}.",
                        "strategy": "verified_site_link",
                        "metrics": {"platform": platform, "url": url},
                    })
                    record_attempt(profile, module="external_footprint", source=provenance, strategy="social_profile_link", outcome="published", candidate_url=url, identity=link, evidence_url=source_page, evidence_sha256=page_hash)
                else:
                    social_candidates.append({"platform": platform, "url": url, "source": provenance, "identity_score": link.get("identity_score"), "reason": link.get("reason")})
                    rejected_site_link = provenance == "verified_site_link" and not link.get("publishable")
                    record_attempt(
                        profile, module="external_footprint", source=provenance, strategy="social_profile_candidate",
                        outcome="identity_rejected" if rejected_site_link else "candidate_unverified", candidate_url=url,
                        reason=(link.get("reason") or "Social handle failed the identity gate.") if rejected_site_link else "Cache or other lead is not independent first-party evidence.",
                        identity=link,
                    )

            seen_social_candidate_keys = {(item["platform"], item["url"]) for item in social_candidates}
            for cached_link in local_cache.social_candidates(profile["organisation_number"]):
                key = (cached_link.get("platform"), cached_link.get("url"))
                if not all(key) or key in seen_social_candidate_keys:
                    continue
                assessment = assess_social_identity(profile, cached_link)
                reason = assessment.get("reason") or "Local-cache lead requires independent corroboration."
                social_candidates.append({"platform": key[0], "url": key[1], "source": "local_cache", "identity_score": assessment.get("identity_score"), "reason": reason})
                seen_social_candidate_keys.add(key)
                record_attempt(profile, module="external_footprint", source="local_cache", strategy="social_profile_candidate", outcome="candidate_unverified" if assessment.get("publishable") else "identity_rejected", candidate_url=key[1], reason="Cached handle remains a candidate pending fresh independent corroboration: " + reason, identity=assessment)

            # Keep search-discovered accounts in the ledger as leads. Search-result text is not a
            # retained source capture and cannot, by itself, establish publication rights or exact entity.
            if profile.get("name") and not social_observations:
                social_query = f'"{profile["name"]}" "{profile.get("municipality") or "Norway"}" (site:linkedin.com/company OR site:instagram.com OR site:facebook.com OR site:youtube.com)'
                if company_web_budget.reserve(SEARCH_REQUEST_CEILING):
                    try:
                        search_results, search_requests, search_status = search_company_websites(social_query, timeout=4.0, use_paid=True)
                    except Exception as exc:
                        search_results, search_requests, search_status = [], 0, "provider_error"
                        search_error = f"{type(exc).__name__}: {str(exc)[:160]}"
                    else:
                        search_error = None
                    company_web_budget.settle(SEARCH_REQUEST_CEILING, search_requests)
                    website_metrics["requests"] += search_requests
                    record_attempt(profile, module="external_footprint", source="configured_search_provider_or_public_fallback", strategy="social_profile_search", outcome="provider_error" if search_status in {"provider_error", "circuit_open", "parse_failure"} else "candidate_found" if search_results else "no_candidate", reason=search_error or f"Search status={search_status}; results are discovery leads only.", provider_status=search_status, requests=search_requests)
                    for result in search_results:
                        raw_url = str(result.get("url") or "")
                        normalized = normalize_social_url(raw_url)
                        if not normalized:
                            continue
                        candidate = assess_search_social_candidate(profile, result)
                        outcome = "candidate_unverified" if candidate else "identity_rejected"
                        reason = "Search result and handle are plausible leads; independent first-party or exact official corroboration is still required." if candidate else "Social candidate failed handle/context identity checks."
                        social_candidates.append({"platform": normalized["platform"], "url": normalized["url"], "source": result.get("provider"), "identity_score": (candidate or {}).get("identity_score"), "reason": reason})
                        record_attempt(profile, module="external_footprint", source=str(result.get("provider") or "web_search"), strategy="social_profile_search_candidate", outcome=outcome, candidate_url=normalized["url"], reason=reason, provider_status=search_status)
                else:
                    record_attempt(profile, module="external_footprint", source="configured_search_provider_or_public_fallback", strategy="social_profile_search", outcome="budget_exhausted", reason="Per-company or run-level web request allowance could not reserve the search ceiling.")
            profile["discovered_social_links"] = social_candidates
            profile["social_discovery"] = {
                "status": "available" if social_observations else "candidate_only" if social_candidates else "not_found",
                "published_profiles": len(social_observations),
                "candidate_profiles": len(social_candidates),
                "platforms": sorted({item["platform"] for item in social_observations}),
                "strategy": "verified_site_publication_search_candidate_ledger",
            }
            observations.extend(social_observations)
            if website_record.get("status") == "available" and identity.get("publishable") and identity.get("content_attributable"):
                activity_allowance = {"reserved": 0, "pending": []}
                def reserve_activity(cost: int) -> bool:
                    if activity_allowance["reserved"] + cost > args.site_activity_request_cap or company_web_budget.used + company_web_budget.reserved + cost + 6 > company_web_budget.limit:
                        return False
                    if not company_web_budget.reserve(cost):
                        return False
                    activity_allowance["reserved"] += cost
                    activity_allowance["pending"].append(cost)
                    return True

                def charge_activity(met: dict) -> None:
                    reserved = activity_allowance["pending"].pop(0) if activity_allowance["pending"] else 0
                    activity_allowance["reserved"] -= max(0, reserved - int(met.get("requests") or 0))
                    company_web_budget.settle(reserved, int(met.get("requests") or 0))
                    website_metrics["requests"] += int(met.get("requests") or 0)
                    website_metrics["bytes"] += int(met.get("bytes") or 0)
                    website_metrics["latencies_ms"].extend(met.get("latencies_ms") or [])

                try:
                    activity = discover_site_activity(website_value, reserve=reserve_activity, charge=charge_activity)
                except Exception as exc:
                    activity = {"status": "source_error", "reason": f"{type(exc).__name__}: {str(exc)[:160]}", "articles": []}
                profile["site_activity"] = activity
                activity_outcome = "published" if activity.get("dated_published_pages") else "provider_error" if activity.get("status") == "source_error" else "budget_exhausted" if activity.get("status") == "budget_exhausted" else "no_qualifying_item"
                activity_reason = activity.get("reason") or (
                    f"Captured {activity.get('captured_pages', 0)} page(s), but none had a supported publication date."
                    if activity.get("captured_pages") and not activity.get("dated_published_pages") else
                    f"Captured {activity.get('captured_pages', 0)} dated publication page(s) from {len(activity.get('feeds_and_sitemaps') or [])} feed/sitemap source(s)."
                )
                record_attempt(profile, module="external_footprint", source="verified_company_site", strategy="feed_sitemap_news_discovery", outcome=activity_outcome, reason=activity_reason, requests=activity.get("requests", 0), evidence_url=website_value.get("final_url"), evidence_sha256=website_value.get("content_sha256"))
                source_hashes = {str(item.get("url")): item.get("content_sha256") for item in activity.get("feeds_and_sitemaps") or []}
                for source_attempt in activity.get("source_attempts") or []:
                    record_attempt(
                        profile, module="external_footprint", source="verified_company_site", strategy="feed_sitemap_source_fetch",
                        outcome=str(source_attempt.get("outcome") or "provider_error"), reason=source_attempt.get("reason"),
                        candidate_url=source_attempt.get("url"), requests=source_attempt.get("requests", 0),
                        evidence_url=source_attempt.get("url"), evidence_sha256=source_hashes.get(str(source_attempt.get("url") or "")),
                    )
                observations.extend(site_news_observations(profile))
            else:
                record_attempt(profile, module="external_footprint", source="verified_company_site", strategy="feed_sitemap_news_discovery", outcome="not_searched", reason="No identity-verified company website was available.")
            new_observations, collector_metrics = enrich_external(profile, company_web_budget, max_requests=args.external_request_cap)
            observations.extend(new_observations)
            for key in ("requests", "bytes"):
                website_metrics[key] += collector_metrics[key]
            website_metrics["latencies_ms"].extend(collector_metrics["latencies_ms"])
            for attempt in collector_metrics["attempts"]:
                record_attempt(profile, module="external_footprint", source="typed_external_collectors", strategy=attempt.get("category") or "captured_external_source",
                    outcome=attempt.get("outcome") or "not_found", candidate_url=attempt.get("url"), requests=attempt.get("requests", 0))
            observations = list({o["id"]: o for o in observations}.values())
            profile["external_coverage"] = coverage_summary(observations)
            profile["external_aggregate"] = aggregate_footprint(observations)
            has_nav_jobs = nav_job_count > 0
            if has_nav_jobs and (social_observations or profile.get("site_activity", {}).get("dated_published_pages")):
                footprint_source = "multi_source_external_footprint"
            elif has_nav_jobs:
                footprint_source = "nav_job_vacancy_feed"
            else:
                footprint_source = "verified_company_site_activity"
            profile["evidence"]["external_footprint"] = {
                "field": "external_footprint",
                "status": "available" if observations else "not_found",
                "source_type": footprint_source,
                "source_class": footprint_source,
                "source_url": ("https://pam-stilling-feed.nav.no/api/v1/feed" if has_nav_jobs and not social_observations and not profile.get("site_activity", {}).get("dated_published_pages") else website_value.get("final_url") or website_record.get("source_url") or ""),
                "retrieved_at": website_record.get("retrieved_at") or utc_now(),
                "value": {"observations": observations, "accepted_observations": len(observations), "platforms": sorted({item["platform"] for item in observations}), "company_owned_activity_only": not has_nav_jobs},
                "note": (
                    "; ".join(part for part in [
                        (f"{profile['social_discovery'].get('candidate_profiles', 0)} social candidate(s) withheld pending independent entity/evidence verification." if profile.get("social_discovery", {}).get("candidate_profiles") else "No publishable social link was found on a verified company page."),
                        (f"Captured {profile.get('site_activity', {}).get('captured_pages', 0)} site page(s), but none has a supported publication date." if profile.get("site_activity", {}).get("captured_pages") and not profile.get("site_activity", {}).get("dated_published_pages") else "No verified-site publication was found."),
                        ("Company-site news discovery was skipped because no identity-verified website was available." if not profile.get("site_activity") else ""),
                    ] if part) if not observations else None
                ),
            }
        profile["summary"] = summarize_company(profile)
        metric = {
            "requests": len(metrics) + website_metrics["requests"],
            "official_requests": len(metrics),
            "web_requests": website_metrics["requests"],
            "bytes": sum(item.bytes_received for item in metrics) + website_metrics["bytes"],
            "latencies_ms": [item.elapsed_ms for item in metrics] + website_metrics["latencies_ms"],
        }
        metric.update(runtime_ms=int((time.monotonic() - company_started) * 1000), **company_usage())
        profile["run_metrics"] = metric
        return profile, metric

    state: dict[str, dict] = {}
    resumed_profiles = 0
    profiles_output = Path(args.profiles_output)
    if args.resume and profiles_output.exists():
        prior = [json.loads(line) for line in profiles_output.read_text(encoding="utf-8").splitlines() if line.strip()]
        if not set(item["organisation_number"] for item in prior).issubset(set(orgs)):
            raise SystemExit("Resume profile membership is not a subset of this batch")
        state = {
            item["organisation_number"]: item
            for item in prior
            if profile_complete_for_modules(item, requested_modules)
        }
        resumed_profiles = len(state)
    pending_profiles = [profile for profile in profiles if profile["organisation_number"] not in state]
    pending_profiles.sort(key=lambda profile: (-profile.get('discovery_priority', 0), profile['organisation_number']))
    nav_executor = ThreadPoolExecutor(max_workers=1)
    nav_future = None
    if pending_profiles and ("website" in requested_modules or "external_footprint" in requested_modules):
        nav_future = nav_executor.submit(discover_nav_jobs, profiles, local_cache, web_request_budget,
            window_days=args.nav_window_days, max_pages=args.nav_max_pages,
            max_details=args.nav_max_details, delay_seconds=args.nav_delay_seconds)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(enrich, profile): profile for profile in pending_profiles}
        for index, future in enumerate(as_completed(futures), 1):
            try:
                profile, metric = future.result()
            except Exception as exc:
                profile = futures[future]
                profile["run_error"] = f"{type(exc).__name__}: {str(exc)[:240]}"
                profile.setdefault("acquisition_attempts", [])
                record_attempt(profile, module="batch", source="signalpost_runner", strategy="company_enrichment", outcome="provider_error", reason=profile["run_error"])
                for module in requested_modules:
                    profile.setdefault("evidence", {}).setdefault(
                        module,
                        evidence(module, "source_error", "signalpost_runner", "", note=profile["run_error"]),
                    )
                metric = {"requests": 0, "official_requests": 0, "web_requests": 0, "bytes": 0, "latencies_ms": []}
            state[profile["organisation_number"]] = profile
            operations["requests"] += metric["requests"]
            operations["official_requests"] += metric["official_requests"]
            operations["web_requests"] += metric["web_requests"]
            operations["bytes"] += metric["bytes"]
            operations["latencies_ms"].extend(metric["latencies_ms"])
            if index % args.checkpoint_every == 0 or index == len(pending_profiles):
                checkpoint = [state[org] for org in orgs if org in state]
                write_jsonl(profiles_output, checkpoint)
                Path(args.report).with_suffix(".provider-usage.json").write_text(json.dumps(provider_summary(), indent=2), encoding="utf-8")

    if nav_future is not None:
        try:
            nav_batch = nav_future.result()
        except Exception as exc:
            nav_batch = NavBatchResult(error=f"{type(exc).__name__}: NAV refresh failed")
        operations["requests"] += nav_batch.metrics["requests"]
        operations["web_requests"] += nav_batch.metrics["requests"]
        operations["bytes"] += nav_batch.metrics["bytes"]
        operations["latencies_ms"].extend(nav_batch.metrics["latencies_ms"])
        if "external_footprint" in requested_modules:
            for org, profile in state.items():
                if org not in {p["organisation_number"] for p in pending_profiles}:
                    continue
                record = profile.setdefault("evidence", {}).setdefault("external_footprint", {})
                value = record.get("value") or {}
                record["value"] = value
                existing = [o for o in value.get("observations", []) if o.get("source_class") != "official_nav_job_feed"]
                jobs = nav_observations(profile, nav_batch)
                value["observations"] = existing + jobs
                value["accepted_observations"] = len(existing) + len(jobs)
                value["platforms"] = sorted({o["platform"] for o in existing + jobs})
                profile["external_coverage"] = coverage_summary(existing + jobs)
                profile["external_aggregate"] = aggregate_footprint(existing + jobs)
                if jobs:
                    record.update(status="available", source_type="multi_source_external_footprint",
                        source_class="multi_source_external_footprint", source_url=record.get("source_url") or jobs[0]["source_url"],
                        retrieved_at=utc_now(), note=None)
                    value["company_owned_activity_only"] = False
                profile["acquisition_attempts"] = [a for a in profile.get("acquisition_attempts", []) if a.get("source") != "nav_job_vacancy_feed"]
                record_attempt(profile, module="external_footprint", source="nav_job_vacancy_feed",
                    strategy="nav_feed_shared_batch_scan", outcome="published" if jobs else "no_candidate" if nav_batch.complete else "provider_error",
                    reason=f"Shared concurrent NAV refresh: complete={nav_batch.complete}; pages={nav_batch.pages}; verified jobs={len(jobs)}; error={nav_batch.error}. Missing matches are not proof that no vacancies exist.",
                    evidence_url="https://pam-stilling-feed.nav.no/api/v1/feed")
    nav_executor.shutdown(wait=True)
    completed_at = utc_now()
    ordered_profiles = [state[org] for org in orgs]
    refresh_changes = diff_datasets(previous_profiles, ordered_profiles) if previous_profiles is not None else []
    if previous_profiles is not None:
        changes_path = args.changes_output or Path(args.report).with_suffix(".changes.jsonl")
        write_jsonl(changes_path, refresh_changes)
        for profile in ordered_profiles:
            profile["change_history"] = [change for change in refresh_changes if change["organisation_number"] == profile["organisation_number"]]
    for profile in ordered_profiles:
        if profile.get("acquisition_attempts"):
            continue
        for module in requested_modules:
            prior_record = profile.get("evidence", {}).get(module) or {}
            record_attempt(
                profile, module=module, source="resumed_profile", strategy="legacy_resume",
                outcome="prior_result_reused" if prior_record.get("status") == "available" else "prior_result_blank",
                reason="This profile was resumed without acquisition-attempt history; no new request was made.",
                candidate_url=prior_record.get("source_url"), evidence_url=prior_record.get("source_url"),
                evidence_sha256=prior_record.get("content_sha256") or prior_record.get("snapshot_sha256"),
            )
    envelopes = [
        terminal_envelope(profile, run_id=args.run_id, modules=requested_modules, started_at=started_at, completed_at=completed_at)
        for profile in ordered_profiles
    ]
    validation = validate_envelopes(envelopes, args.expected_count)
    company_coverage = []
    for profile in ordered_profiles:
        found = []
        blank = []
        for module in requested_modules:
            record = profile.get("evidence", {}).get(module) or {}
            if record.get("status") == "available":
                found.append(module)
            else:
                blank.append({
                    "module": module,
                    "status": record.get("status", "not_fetched"),
                    "why": record.get("note") or ("No source record was produced for this module." if not record else "Source returned no publishable value."),
                })
        company_coverage.append({
            "organisation_number": profile["organisation_number"],
            "name": profile.get("name"),
            "found": found,
            "blank": blank,
        })
    attempt_rows = [
        {"organisation_number": profile["organisation_number"], "company_name": profile.get("name"), **attempt}
        for profile in ordered_profiles
        for attempt in (profile.get("acquisition_attempts") or [])
    ]
    write_jsonl(attempts_output, attempt_rows)
    attempt_outcomes = Counter(f"{row['module']}|{row['outcome']}" for row in attempt_rows)
    blank_reasons = Counter(
        (entry["module"], entry["status"], entry["why"])
        for company in company_coverage
        for entry in company["blank"]
    )
    blank_reason_summary = [
        {"module": module, "status": status, "reason": reason, "companies": count}
        for (module, status, reason), count in blank_reasons.most_common()
    ]
    write_jsonl(profiles_output, ordered_profiles)
    write_jsonl(Path(args.output), envelopes)
    latencies = sorted(operations.pop("latencies_ms"))
    operations["p50_ms"] = latencies[len(latencies) // 2] if latencies else None
    operations["p95_ms"] = latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))] if latencies else None
    report = {
        "run_id": args.run_id,
        "started_at": started_at,
        "completed_at": completed_at,
        "expected_count": args.expected_count,
        "emitted_envelopes": len(envelopes),
        "resumed_profiles": resumed_profiles,
        "profiles_fetched_this_run": len(pending_profiles),
        "modules": requested_modules,
        "registry": registry_metadata,
        "nav_feed": {**nav_batch.summary(), "concurrent_with_company_enrichment": True},
        "search_providers": provider_summary(),
        "refresh": {"requested": previous_profiles is not None, "material_changes": len(refresh_changes),
                    "idempotent_self_comparison": diff_datasets(ordered_profiles, ordered_profiles) == [],
                    "boundary": "Missing external observations do not establish closure or zero activity; only new evidence and supported metric changes are emitted."},
        "local_cache": cache_coverage(args.cache_dir) if args.cache_dir else cache_coverage(),
        "transport": network_meter.snapshot(),
        "company_coverage": company_coverage,
        "acquisition_ledger": {
            "path": str(attempts_output.resolve()),
            "attempts": len(attempt_rows),
            "outcomes": dict(sorted(attempt_outcomes.items())),
            "blank_reasons": blank_reason_summary,
        },
        "operations": operations,
        "request_budget": {
            "total_limit": args.request_budget,
            "official_request_reserve": expected_official_requests,
            "web_request_limit": web_request_budget.limit,
            "web_requests_used": web_request_budget.used,
            "per_company_web_limit": args.web_requests_per_company,
        },
        "validation": validation,
    }
    Path(args.report).parent.mkdir(parents=True, exist_ok=True)
    Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "company_coverage"}, ensure_ascii=False, indent=2))
    for item in company_coverage:
        blanks = "; ".join(f"{entry['module']}={entry['status']} ({entry['why']})" for entry in item["blank"])
        print(f"{item['organisation_number']} {item.get('name') or ''}: found={','.join(item['found']) or 'none'}; blank={blanks or 'none'}")
    raise SystemExit(0 if validation["passed"] else 1)


if __name__ == "__main__":
    main()
