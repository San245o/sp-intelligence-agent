"""Bounded Serper/Tavily discovery. Results are leads, never publication proof."""
from __future__ import annotations

from collections import Counter
import json
import os
import threading
import time
from datetime import datetime, timezone
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from .runtime_defaults import load_defaults
from .source_store import capture_source

SEARCH_REQUEST_CEILING = 4
_lock = threading.Lock()
_unavailable: dict[tuple[str, str], float] = {}
_counts = Counter()
_paid_limit = 40
_company_limit = 2
_company_attempts = Counter()
_company_credits = Counter()
_context = threading.local()
_allowances = None


def configure_search_budget(limit: int = 40, per_company: int = 2) -> None:
    global _paid_limit, _company_limit, _allowances
    with _lock:
        _paid_limit, _company_limit = max(0, limit), max(0, per_company)
        _counts.clear()
        _company_attempts.clear()
        _company_credits.clear()
        _allowances = None


def plan_search_budget(profiles, limit=0, per_company=2):
    """Allocate paid searches to the strongest current company opportunities.

    A 75/100 registry-completeness score is a spend eligibility gate only;
    publication still requires the stricter source-specific identity checks.
    """
    global _allowances
    configure_search_budget(limit, per_company)

    def number(value):
        try:
            return float(str(value).replace(",", "").replace(" ", ""))
        except (TypeError, ValueError):
            return 0.0

    def scores(profile):
        registry = (profile.get("evidence", {}).get("registry") or {}).get("value") or {}
        name = str(profile.get("name") or registry.get("navn") or "").strip()
        address = profile.get("business_address") or profile.get("forretningsadresse.adresse") or registry.get("forretningsadresse.adresse") or registry.get("forretningsadresse") or {}
        address_text = " ".join(str(part) for part in address) if isinstance(address, list) else str(address or "")
        postcode = str(profile.get("postcode") or profile.get("forretningsadresse.postnummer") or registry.get("forretningsadresse.postnummer") or "")
        status_text = " ".join(str(profile.get(key) or registry.get(key) or "") for key in ("konkurs", "underAvvikling", "slettedato")).casefold()
        active = not (profile.get("bankrupt") or profile.get("liquidating")) and not any(token in status_text for token in ("true", "konkurs", "avvik", "slett"))
        identity = (30 if len(name) >= 4 else 0) + (20 if address_text.strip() else 0) + (10 if len(postcode) == 4 else 0) + (20 if active else 0) + (20 if len(str(profile.get("organisation_number") or "")) == 9 and str(profile["organisation_number"]).isdigit() else 0)
        try:
            employees = int(profile.get("employees") or registry.get("antallAnsatte") or 0)
        except (TypeError, ValueError):
            employees = 0
        financials = profile.get("evidence", {}).get("financials", {}).get("value") or {}
        account = next((record for record in financials.get("records", []) if record.get("account_type") == "SELSKAP"), {})
        revenue = number(profile.get("revenue") or account.get("revenue") or financials.get("revenue") or financials.get("driftsinntekter") or registry.get("revenue") or registry.get("driftsinntekter"))
        website = profile.get("website") or registry.get("hjemmeside")
        site_value = profile.get("evidence", {}).get("website", {}).get("value") or {}
        own_site = bool((site_value.get("identity_assessment") or {}).get("content_attributable"))
        operating_names = bool(profile.get("discovery_operating_names"))
        dormant = any(token in name.casefold() for token in ("holding", "invest", "eiendom", "sameie", "borettslag"))
        potential = min(30, employees // 2) + (25 if revenue > 0 else 0) + (15 if website else 0) + (15 if own_site else 0) + (15 if operating_names else 0) - (20 if dormant else 0)
        return min(100, identity), max(0, potential)

    ranked = []
    for profile in profiles:
        confidence, potential = scores(profile)
        profile["paid_discovery_readiness_score"] = confidence
        profile["discovery_priority"] = potential
        if confidence >= 75:
            ranked.append(profile)
    ranked.sort(key=lambda profile: (-profile["discovery_priority"], -profile["paid_discovery_readiness_score"], str(profile["organisation_number"])))
    remaining = max(0, int(limit))
    allowances = {str(profile["organisation_number"]): 0 for profile in profiles}
    for profile in ranked:
        if remaining <= 0:
            break
        allocation = min(max(0, int(per_company)), remaining)
        if allocation:
            allowances[str(profile["organisation_number"])] = allocation
            remaining -= allocation
    with _lock:
        _allowances = allowances
    return allowances


def set_search_company(organisation_number: str) -> None:
    _context.org = str(organisation_number)


def _reserve_paid(credits: int) -> bool:
    org = getattr(_context, "org", "unscoped")
    with _lock:
        used = _counts["paid_credits_used"]
        allowed = _allowances.get(org, 0) if _allowances is not None else _company_limit + 2
        if used + credits + _counts["paid_credits_reserved"] > _paid_limit or _company_attempts[org] >= _company_limit or _company_credits[org] + credits > allowed:
            _counts["paid_budget_prevented"] += 1
            return False
        # Reserve before any I/O so concurrent workers cannot overspend.
        _counts["paid_reserved"] += 1
        _counts["paid_credits_reserved"] += credits
        _company_attempts[org] += 1
        _company_credits[org] += credits
        return True


def credentials() -> tuple[list[str], str]:
    configured = os.environ.get("SERPER_API_KEYS") or os.environ.get("SERPER_API_KEY") or ""
    keys = [part.strip() for part in configured.replace(";", ",").split(",") if part.strip()]
    keys.extend(os.environ.get(f"SERPER_API_KEY_{n}", "").strip() for n in (2, 3))
    keys = list(dict.fromkeys(key for key in keys if key))
    if not keys and os.environ.get("SIGNALPOST_USE_LOCAL_DEFAULTS", os.environ.get("SIGNALPOST_USE_PUBLIC_DEMO_KEYS", "1")).casefold() not in {"0", "false", "no"}:
        keys = load_defaults()
    return keys, os.environ.get("TAVILY_API_KEY", "").strip()


def provider_summary() -> dict:
    keys, tavily = credentials()
    with _lock:
        counts = dict(_counts)
    return {"serper_key_slots": len(keys), "tavily_configured": bool(tavily),
            "local_credential_fallback": not bool(os.environ.get("SERPER_API_KEYS") or os.environ.get("SERPER_API_KEY")) and bool(keys),
            "paid_request_cap": _paid_limit, "per_company_paid_request_cap": _company_limit,
            "paid_credit_cap": _paid_limit, "paid_credits_used": counts.get("paid_credits_used", 0),
            "credit_basis": "Search/news/Places=1, Maps=3, Tavily basic=1. Failed attempts reserve the same cost conservatively; successful Serper charges are reconciled with response credits.",
            "scheduled_companies": sum(value > 0 for value in _allowances.values()) if _allowances is not None else None,
            "eligible_companies": sum(value > 0 for value in _allowances.values()) if _allowances is not None else None,
            "selection_policy": "Every company receives public-source discovery. Paid credits are assigned in priority order to companies scoring at least 75/100 on registry completeness; opportunity ranking favors workforce, reported revenue when available, attributable or registered websites and operating names. Publication retains stricter source-specific identity gates.",
            "paid_requests_used": counts.get("serper_requests", 0) + counts.get("tavily_requests", 0),
            "attempts": counts, "result_policy": "Discovery leads; independently fetch source pages before publishing. Cached leads retain their original acquisition times."}


def provider_search(query: str, *, kind: str = "search", timeout: float = 8,
                    max_requests: int = SEARCH_REQUEST_CEILING) -> tuple[list[dict], int, str]:
    keys, tavily = credentials()
    slots = [("serper", key) for key in keys] + ([("tavily", tavily)] if tavily else [])
    attempts = 0
    for provider, api_key in slots:
        if attempts >= max_requests:
            break
        with _lock:
            if max(_unavailable.get((provider, api_key), 0), _unavailable.get((provider, "*"), 0)) > time.monotonic():
                continue
        if provider == "serper":
            endpoint = "https://google.serper.dev/" + ("news" if kind == "news" else "maps" if kind == "maps" else "places" if kind == "places" else "search")
            payload = {"q": query, "gl": "no", "hl": "no", "num": 10}
            headers = {"X-API-KEY": api_key, "Content-Type": "application/json"}
        else:
            if kind in {"places", "maps"}:
                continue
            endpoint = "https://api.tavily.com/search"
            payload = {"query": query, "topic": "news" if kind == "news" else "general", "search_depth": "basic", "max_results": 8, "include_answer": False, "include_raw_content": False}
            headers = {"Authorization": "Bearer " + api_key, "Content-Type": "application/json"}
        credit_cost = 3 if provider == "serper" and kind == "maps" else 1
        if not _reserve_paid(credit_cost):
            return [], attempts, "paid_budget_exhausted"
        attempts += 1
        with _lock:
            _counts["paid_reserved"] -= 1
            _counts["paid_credits_reserved"] -= credit_cost
            _counts["paid_credits_used"] += credit_cost
            _counts[provider + "_requests"] += 1
        try:
            request = Request(endpoint, data=json.dumps(payload).encode(), headers=headers, method="POST")
            with urlopen(request, timeout=timeout) as response:
                raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                raise ValueError("Oversized response")
            body = json.loads(raw)
            if not isinstance(body, dict):
                raise ValueError("Invalid response shape")
            reported = body.get("credits")
            if provider == "serper" and isinstance(reported, (int, float)) and not isinstance(reported, bool) and reported >= 0:
                with _lock:
                    _counts["serper_reported_credits"] += reported
                    if reported > credit_cost:
                        _counts["paid_credits_used"] += reported - credit_cost
                        _company_credits[getattr(_context, "org", "unscoped")] += reported - credit_cost
                        _counts["unexpected_credit_charge"] += 1
                        _unavailable[(provider, "*")] = time.monotonic() + 3600
            values = body.get("results") if provider == "tavily" else body.get("news" if kind == "news" else "places" if kind in {"places", "maps"} else "organic")
            if not isinstance(values, list):
                raise ValueError("Missing result array")
            output = []
            capture = capture_source(raw) if kind in {"places", "maps"} else {}
            for index, item in enumerate(values[:10], 1):
                if not isinstance(item, dict):
                    continue
                url = item.get("url") or item.get("link") or item.get("website")
                if kind in {"places", "maps"} and str(item.get("cid") or "").isdigit():
                    url = "https://www.google.com/maps?cid=" + str(item["cid"])
                if not isinstance(url, str) or not url.startswith(("https://", "http://")):
                    continue
                output.append({"url": url, "title": str(item.get("title") or "")[:500],
                    "snippet": str(item.get("snippet") or item.get("content") or "")[:1500],
                    "provider": provider, "rank": str(index), "date_hint": item.get("date") or item.get("published_date"),
                    **({"place_lead": {key: item.get(key) for key in ("title", "address", "phoneNumber", "website", "rating", "ratingCount", "cid", "placeId", "latitude", "longitude", "reviews")},
                        **capture, "content_sha256": capture.get("snapshot_sha256"), "retrieved_at": datetime.now(timezone.utc).isoformat(),
                        "provider_api_url": endpoint} if kind in {"places", "maps"} else {})})
            with _lock:
                _counts[provider + "_successes"] += 1
            return output, attempts, "results" if output else "empty_result"
        except HTTPError as exc:
            # Never log response bodies or headers: providers may echo secrets.
            cooldown = 3600 if exc.code in {401, 402, 403} else 60 if exc.code == 429 else 15
            with _lock:
                _unavailable[(provider, api_key)] = time.monotonic() + cooldown
                if exc.code in {402, 429}:
                    # Service/account throttles are not bypassed by key rotation.
                    _unavailable[(provider, "*")] = time.monotonic() + cooldown
                _counts[provider + "_http_" + str(exc.code)] += 1
        except Exception:
            with _lock:
                _unavailable[(provider, api_key)] = time.monotonic() + 15
                _counts[provider + "_transport_or_parse_errors"] += 1
    return [], attempts, "provider_error" if attempts else "not_configured" if not slots else "circuit_open"


def company_usage() -> dict:
    org = getattr(_context, "org", "unscoped")
    with _lock:
        return {"paid_credits": _company_credits[org], "paid_attempts": _company_attempts[org]}
