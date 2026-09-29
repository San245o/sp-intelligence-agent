"""NAV arbeidsplassen job connector.

Queries the official public search on arbeidsplassen.nav.no.
All candidate job postings are gated by exact legal-name matching to avoid
wrong-company job attributions (Rule #4).
"""
from __future__ import annotations

import re
import unicodedata
import urllib.parse
from typing import Any

from bs4 import BeautifulSoup

from ..evidence import (
    SOURCE_OFFICIAL_JOB_BOARD,
    EvidenceStore,
    make_claim,
)
from ..http import Fetcher

SEARCH_PAGE_URL = "https://arbeidsplassen.nav.no/stillinger"
AD_URL = "https://arbeidsplassen.nav.no/stillinger/stilling/{uuid}"
LEGAL_FORM_TOKENS = {
    "as", "asa", "ans", "da", "sa", "nuf", "ba", "enk", "ks", "se", "sti", "iks", "kf", "sf"
}
EXTRACTOR = "nav_arbeidsplassen_search_v2"
LICENCE = "NAV Open Data / arbeidsplassen.nav.no terms"

NORWEGIAN_MONTHS = {
    "januar": "01", "februar": "02", "mars": "03", "april": "04",
    "mai": "05", "juni": "06", "juli": "07", "august": "08",
    "september": "09", "oktober": "10", "november": "11", "desember": "12"
}


def fold(name: str | None) -> str:
    """Casefold, normalize Norwegian characters, and strip non-alphanumeric punctuation."""
    s = str(name or "").casefold().replace("æ", "ae").replace("ø", "o").replace("å", "a").replace("aa", "a")
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", s)).strip()


def strip_legal_form(folded: str) -> str:
    toks = folded.split()
    if len(toks) > 1 and toks[-1] in LEGAL_FORM_TOKENS:
        toks = toks[:-1]
    return " ".join(toks)


def strip_branch(folded: str) -> str:
    """Strip Norwegian branch suffixes (e.g. 'avd ski', 'avdeling bergen')."""
    s = re.sub(r"\b(?:avd|avdeling)\b.*$", "", folded).strip()
    return strip_legal_form(s)


def names_match(legal_name: str | None, candidate: str | None) -> bool:
    """Exact-name gate: folded equality or stripped legal form/branch equality."""
    a, b = fold(legal_name), fold(candidate)
    if not a or not b:
        return False
    a_clean = strip_legal_form(a)
    b_clean = strip_branch(b)
    b_form = strip_legal_form(b)
    return (
        a == b
        or a_clean == b
        or a == b_form
        or a_clean == b_form
        or a_clean == b_clean
        or a == b_clean
    )


def _parse_norwegian_date(s: str | None) -> str | None:
    if not s:
        return None
    m = re.search(r"(\d{1,2})\.\s*([a-zA-ZæøåÆØÅ]+)\s*(\d{4})", str(s))
    if m:
        day = m.group(1).zfill(2)
        month_name = m.group(2).lower()
        year = m.group(3)
        month = NORWEGIAN_MONTHS.get(month_name)
        if month:
            return f"{year}-{month}-{day}"
    return None


def _date(v: Any) -> str | None:
    return str(v)[:10] if isinstance(v, str) and len(v) >= 10 else None


def _location(src: dict[str, Any]) -> str | None:
    if src.get("location"):
        return str(src["location"])
    for loc in src.get("locationList") or []:
        if isinstance(loc, dict) and (loc.get("city") or loc.get("municipal")):
            return str(loc.get("city") or loc.get("municipal"))
    return None


def fetch_nav_jobs(
    fetcher: Fetcher,
    org: str,
    legal_name: str,
    store: EvidenceStore,
) -> list[dict[str, Any]]:
    """Query NAV Arbeidsplassen for exact-name active job vacancies."""
    clean_name = str(legal_name or "").strip()
    if not clean_name:
        return [make_claim(
            field="active_job_count", value=None, availability="not_available",
            note="no company name available to search NAV jobs with")]

    # Query public search page; exact-gated on hit
    search_query = strip_legal_form(fold(clean_name)) or clean_name
    query_str = urllib.parse.urlencode({"q": search_query})
    url = f"{SEARCH_PAGE_URL}?{query_str}"

    response = fetcher.get(
        org, url, accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8", check_robots=False
    )
    if not response.ok:
        state = "not_available" if response.status in (404, 400) else "failed"
        return [make_claim(
            field="active_job_count", value=None, availability=state,
            note=f"NAV job search returned {response.error or response.status}")]

    accepted_ads: list[dict[str, Any]] = []
    seen_uuids: set[str] = set()
    raw_hits_count = 0

    # 1. Primary: Parse server-side rendered HTML articles from public stillinger page
    if response.is_html or "<article" in response.text:
        try:
            soup = BeautifulSoup(response.text, "html.parser")
            articles = soup.find_all("article")
            raw_hits_count = len(articles)
            for art in articles:
                link = art.find("a", href=re.compile(r"/stillinger/stilling/([a-f0-9\-]+)"))
                if not link:
                    continue
                uuid_m = re.search(r"/stillinger/stilling/([a-f0-9\-]+)", link["href"])
                if not uuid_m:
                    continue
                uuid_val = uuid_m.group(1)
                if uuid_val in seen_uuids:
                    continue

                title = link.get_text(strip=True)
                chunks = [t.strip() for t in art.stripped_strings]
                employer = None
                location = None
                date_str = None
                for i, c in enumerate(chunks):
                    if c == "Arbeidsgiver" and i + 1 < len(chunks):
                        employer = chunks[i + 1]
                    elif c == "Sted" and i + 1 < len(chunks):
                        location = chunks[i + 1]
                    elif re.search(r"\d{1,2}\.\s*[a-zA-ZæøåÆØÅ]+\s*\d{4}", c) and not date_str:
                        date_str = _parse_norwegian_date(c)

                if employer and names_match(clean_name, employer):
                    seen_uuids.add(uuid_val)
                    accepted_ads.append({
                        "uuid": uuid_val,
                        "title": title,
                        "businessName": employer,
                        "location": location,
                        "published": date_str,
                    })
        except Exception:
            pass

    # 2. Secondary fallback: parse JSON if response is a JSON payload (e.g. from mock or API)
    if not accepted_ads and not raw_hits_count:
        try:
            data = response.json()
            hits_obj = data.get("hits") if isinstance(data, dict) else None
            raw_hits = (hits_obj.get("hits") or []) if isinstance(hits_obj, dict) else []
            raw_hits_count = len(raw_hits)
            for hit in raw_hits:
                src = hit.get("_source") if isinstance(hit, dict) else None
                if not isinstance(src, dict) or src.get("status") != "ACTIVE":
                    continue
                biz_name = src.get("businessName")
                emp_name = (src.get("employer") or {}).get("name") if isinstance(src.get("employer"), dict) else None
                prop_emp = (src.get("properties") or {}).get("employer") if isinstance(src.get("properties"), dict) else None
                uuid_val = src.get("uuid")
                if not uuid_val or uuid_val in seen_uuids:
                    continue

                cands = [c for c in (biz_name, emp_name, prop_emp) if c]
                if any(names_match(clean_name, cand) for cand in cands):
                    seen_uuids.add(uuid_val)
                    accepted_ads.append(src)
        except Exception:
            pass

    accepted_ads.sort(key=lambda s: str(s.get("published") or ""), reverse=True)

    claims: list[dict[str, Any]] = []
    evidence_span = f"NAV search for {clean_name!r}: {raw_hits_count} hits, {len(accepted_ads)} exact matches"
    query_ev_id = store.create(
        source_url=url,
        source_class=SOURCE_OFFICIAL_JOB_BOARD,
        retrieved_at=response.retrieved_at,
        content_sha256=response.content_sha256,
        claim_span=evidence_span,
        http_status=response.status,
        extractor=EXTRACTOR,
        licence=LICENCE,
    )

    for ad in accepted_ads:
        title = str(ad.get("title") or "").strip()
        published = _date(ad.get("published"))
        expires = _date(ad.get("expires"))
        location = _location(ad)
        ad_uuid = ad.get("uuid")
        ad_url = AD_URL.format(uuid=ad_uuid)

        ad_span = f"{title} ({location or 'Norge'}) - published {published}"
        ad_ev_id = store.create(
            source_url=ad_url,
            source_class=SOURCE_OFFICIAL_JOB_BOARD,
            retrieved_at=response.retrieved_at,
            content_sha256=response.content_sha256,
            claim_span=ad_span,
            http_status=response.status,
            extractor=EXTRACTOR,
            licence=LICENCE,
        )

        claims.append(make_claim(
            field="job_posting",
            value={
                "title": title,
                "url": ad_url,
                "date_posted": published,
                "valid_through": expires,
                "location": location,
                "source": "nav",
            },
            availability="available",
            evidence_ids=[ad_ev_id],
            confidence=0.95,
            reporting_period=published,
            note=f"Active job posting: {title}",
        ))

    claims.append(make_claim(
        field="active_job_count",
        value=len(accepted_ads),
        availability="available",
        evidence_ids=[query_ev_id],
        confidence=0.95,
        note=f"{len(accepted_ads)} active postings matched in official NAV database"
        if accepted_ads else "checked NAV job database; 0 active postings matched exact company name",
    ))

    if accepted_ads:
        claims.append(make_claim(
            field="hiring_or_activity_signal",
            value={
                "active_job_ads": len(accepted_ads),
                "latest_posting": accepted_ads[0].get("title"),
                "source": "nav_active_vacancies",
            },
            availability="available",
            evidence_ids=[query_ev_id],
            confidence=0.95,
            note=f"Active hiring detected on NAV: {len(accepted_ads)} open positions",
        ))

    return claims
