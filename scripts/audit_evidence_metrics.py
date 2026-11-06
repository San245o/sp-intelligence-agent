"""Apply consistent evidence definitions to both saved live outputs."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, date as CalendarDate, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit, parse_qs

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from norway_company_agent.news_quality import publication_eligibility
from norway_company_agent.identity import assess_website_identity


def direct_job_url(observation: dict) -> bool:
    parsed = urlsplit(observation.get("source_url") or "")
    host = (parsed.hostname or "").removeprefix("www.").casefold()
    path = parsed.path.casefold().rstrip("/")
    metrics = observation.get("metrics") or {}
    title = str(metrics.get("job_title") or metrics.get("title") or "").strip().casefold()
    if not title or "linkedin" in title or title in {"jobs", "careers", "join us", "ledige stillinger"}:
        return False
    for proof in observation.get("identity_proof") or []:
        if proof.get("type") == "public_ats_current_listing" and proof.get("job_id") and str(proof["job_id"]) == str(metrics.get("job_id")):
            return str(proof["job_id"]) in parsed.path.split("/") or str(proof["job_id"]) in parse_qs(parsed.query).get("gh_jid", [])
    if title in {"se våre ledige stillinger", "view jobs", "open positions", "rapporter annonse"}:
        return False
    if host == "finn.no":
        # A real role URL remains a role lead without recorded dates; report,
        # map and search-filter URLs never become vacancies through metadata.
        return bool(metrics.get("job_title") and re.fullmatch(r"/job/ad/\d+", path))
    if any(part in path.split("/") for part in ("report", "search", "share", "map")):
        return False
    if host == "linkedin.com" and not path.startswith("/jobs/view/"):
        return False
    if host in {"facebook.com", "instagram.com", "youtube.com", "x.com", "twitter.com"}:
        return False
    if host == "pam-stilling-feed.nav.no" and path.startswith("/api/v1/feedentry/"):
        return any(proof.get("type") == "nav_employer_organisation_number" for proof in observation.get("identity_proof") or [])
    if path in {"", "/jobs", "/careers", "/karriere", "/stillinger", "/ledige-stillinger"}:
        return False
    return bool(metrics.get("job_title") and (metrics.get("published_at") or metrics.get("deadline") or "/jobs/view/" in path))


def active_exact_job(observation: dict) -> bool:
    if not direct_job_url(observation):
        return False
    metrics = observation.get("metrics") or {}
    for proof in observation.get("identity_proof") or []:
        if proof.get("type") == "public_ats_current_listing" and proof.get("employer_evidence") and metrics.get("active_listing") is True:
            try:
                as_of = datetime.fromisoformat(str(proof.get("active_as_of") or "").replace("Z", "+00:00"))
                return bool(as_of.tzinfo and 0 <= (datetime.now(timezone.utc) - as_of).total_seconds() <= 86400)
            except ValueError:
                return False
    today = datetime.now(timezone.utc).date()
    try:
        expires = CalendarDate.fromisoformat(str(metrics.get("deadline") or metrics.get("expires") or "")[:10])
        published = CalendarDate.fromisoformat(str(metrics.get("published_at") or metrics.get("published") or "")[:10])
    except ValueError:
        return False
    proofs = observation.get("identity_proof") or []
    employer_proven = any(proof.get("type") == "nav_employer_organisation_number"
                          or (proof.get("type") == "captured_own_site_jobposting" and (proof.get("employer_org_match") or proof.get("employer_name_match")))
                          for proof in proofs)
    return bool(employer_proven and published <= today <= expires)


def canonical(url: str) -> str:
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc.casefold().removeprefix("www."), parsed.path.rstrip("/"), parsed.query, ""))


def evaluate(directory: Path) -> tuple[dict, set[str]]:
    report = json.loads((directory / "run-report.json").read_text(encoding="utf-8"))
    profiles = [json.loads(line) for line in (directory / "profiles.jsonl").read_text(encoding="utf-8").splitlines() if line]
    orgs = {str(row["organisation_number"]) for row in profiles}
    counts = Counter()
    gate_rejections = []
    native_social = set(); native_news = set(); native_news_items = native_job_items = 0
    owners = defaultdict(set)
    qualified = []
    for row in profiles:
        org = str(row["organisation_number"])
        website = row.get("evidence", {}).get("website") or {}
        identity = assess_website_identity(row) if website.get("status") == "available" else {}
        native = (website.get("value") or {}).get("identity_assessment") or {}
        if website.get("status") == "available" and native.get("publishable"):
            counts["native_accepted_websites"] += 1
        if website.get("status") == "available" and identity.get("publishable"):
            counts["common_gate_websites"] += 1
            if identity.get("content_attributable"):
                counts["common_gate_content_sites"] += 1
        elif website.get("status") == "available":
            gate_rejections.append({"organisation_number": org, "name": row.get("name"), "url": (website.get("value") or {}).get("final_url"), "reasons": identity.get("reasons")})
        observations = ((row.get("evidence", {}).get("external_footprint") or {}).get("value") or {}).get("observations") or []
        for obs in observations:
            if obs.get("signal_type") == "profile_handle":
                native_social.add(org)
            if obs.get("platform") in {"company_site", "news"} and obs.get("signal_type") in {"public_post", "public_mention"}:
                native_news.add(org); native_news_items += 1
            if obs.get("signal_type") == "job_posting":
                native_job_items += 1
            if obs.get("signal_type") == "job_posting" and not direct_job_url(obs):
                counts["misclassified_job_items"] += 1
            if not (str(obs.get("organisation_number")) == org and obs.get("exact_entity") is True
                    and obs.get("rights_status") == "approved" and obs.get("identity_proof")
                    and len(str(obs.get("content_sha256") or "")) == 64 and obs.get("source_url") and obs.get("retrieved_at")):
                continue
            first_party_news = obs.get("platform") == "news" and (
                str(obs.get("source_class") or "").startswith("company_")
                or any(proof.get("method") in {"verified_domain_first_party_activity", "verified_site_news_feed"}
                       for proof in obs.get("identity_proof") or []))
            site_derived = obs.get("platform") == "company_site" or first_party_news or obs.get("signal_type") == "profile_handle" or obs.get("source_class") == "company_careers"
            if site_derived and not identity.get("content_attributable"):
                continue
            qualified.append((org, obs))
            if obs.get("signal_type") in {"profile_handle", "public_post", "job_posting"}:
                owners[canonical(obs["source_url"])].add(org)
    disputed = {url: sorted(owner_set) for url, owner_set in owners.items() if len(owner_set) > 1}
    profiles_by_org = {p["organisation_number"]:p for p in profiles}
    per_company = defaultdict(set)
    social = set(); news = set(); jobs = set(); active_jobs = set(); hiring = set(); reviews = set(); workforce = set(); engagement = set(); sentiment = set()
    for org, obs in qualified:
        if canonical(obs["source_url"]) in disputed:
            continue
        platform, signal = obs.get("platform"), obs.get("signal_type")
        metrics = obs.get("metrics") or {}
        if platform != "brreg":
            per_company[org].add(platform)
        if signal == "profile_handle":
            social.add(org)
        if platform in {"company_site", "news"} and signal in {"public_post", "public_mention"}:
            date = obs.get("publication_date") or obs.get("published_at") or metrics.get("activity_date")
            if date and publication_eligibility(profiles_by_org[org], date, obs.get("source_url", ""), obs.get("evidence_span", "") + " " + str(metrics.get("title") or ""))[0]:
                counts["common_gate_dated_news_items"] += 1
                news.add(org)
        if signal == "job_posting":
            # A board/company profile link is a hiring lead, not a direct vacancy.
            if direct_job_url(obs):
                jobs.add(org); counts["direct_job_items"] += 1
                if active_exact_job(obs):
                    active_jobs.add(org); counts["verified_active_job_items"] += 1
        if signal in {"job_board_link", "hiring_signal"}:
            hiring.add(org)
        if signal in {"review", "review_summary", "place_summary"} and any(key in metrics for key in ("rating", "rating_value", "review_count")):
            reviews.add(org)
        if signal == "workforce_snapshot":
            workforce.add(org)
        if signal in {"profile_metrics", "buzz_metrics"} and any(key in metrics for key in ("followers", "subscribers", "views", "likes", "comments", "engagement_rate")):
            engagement.add(org)
        if obs.get("sentiment_label") is not None and obs.get("source_class") in {"customer_review", "employee_review", "licensed_news", "public_news", "public_mention"} and obs.get("sentiment_model_version"):
            sentiment.add(org)
    transport = report.get("transport") or {}
    hosts = transport.get("by_host") or {}
    transport_total = transport.get("attempts")
    nav_attempts = sum(value for host, value in hosts.items() if host == "pam-stilling-feed.nav.no")
    official_attempts = sum(value for host, value in hosts.items() if host == "data.brreg.no")
    duration = None
    if report.get("started_at") and report.get("completed_at"):
        duration = (datetime.fromisoformat(report["completed_at"].replace("Z", "+00:00")) - datetime.fromisoformat(report["started_at"].replace("Z", "+00:00"))).total_seconds()
    summary = {**dict(counts), "profiles": len(profiles), "companies_with_social": len(social), "companies_with_native_dated_news": len(news),
               "native_reported_social_companies": len(native_social), "native_reported_news_companies": len(native_news),
               "native_reported_news_items": native_news_items, "native_reported_job_items": native_job_items,
               "companies_with_direct_jobs": len(jobs), "companies_with_hiring_leads": len(hiring), "companies_with_reviews": len(reviews),
               "companies_with_verified_active_jobs": len(active_jobs),
               "companies_with_workforce": len(workforce), "companies_with_engagement": len(engagement), "companies_with_sentiment": len(sentiment),
               "companies_with_two_sources": sum(len(platforms) >= 2 for platforms in per_company.values()),
               "transport_attempts": transport_total, "nav_attempts": nav_attempts, "official_attempts": official_attempts,
               "comparable_discovery_attempts": transport_total - nav_attempts - official_attempts if transport_total is not None else None,
               "wall_seconds": duration, "p95_ms": (report.get("operations") or {}).get("p95_ms"),
               "validation_passed": (report.get("validation") or {}).get("passed"), "common_gate_rejections": gate_rejections,
               "cross_company_disputes": disputed}
    return summary, orgs
