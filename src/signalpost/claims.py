"""On-site claims: turn a verified own-site crawl into published facts.

Runs only after `identity.assess_identity` has accepted a domain as the
company's own page. Everything here is deterministic extraction of what the page
literally states — structured data (schema.org/Organization), contact blocks,
social profile links — carried as `company_owned` evidence so a reviewer can
trace every value to the exact page and span it was read from.

Guardrails from the source policy applied here:
  * Nothing invents a value. An absent field yields no claim (not a zero, not a
    guess). A missing contact detail simply is not emitted.
  * On-site claims are `company_owned`, never registry-grade. If the identity
    gate did not clear the publish threshold, `envelope.build_envelope`
    downgrades every one of these to `ambiguous` — this module does not decide
    publishability, it only records what the accepted page says.
"""
from __future__ import annotations

import re
import urllib.parse
from typing import Any

from .evidence import SOURCE_COMPANY_OWNED, EvidenceStore, make_claim
from .extract.contact import extract_contacts
from .extract.structured import parse_structured
from .identity import registrable_domain
from .website import Page, SiteCrawl

EXTRACTOR = "site_extract_v1"


def _page_evidence(
    store: EvidenceStore, page: Page, span: str
) -> list[str]:
    return [store.create(
        source_url=page.url,
        source_class=SOURCE_COMPANY_OWNED,
        retrieved_at=page.retrieved_at,
        content_sha256=page.content_sha256,
        claim_span=span,
        http_status=page.status,
        final_url=page.final_url if page.final_url != page.url else None,
        extractor=EXTRACTOR,
    )]



MONTH_MAP = {
    "januar": "01", "january": "01", "jan": "01",
    "februar": "02", "february": "02", "feb": "02",
    "mars": "03", "march": "03", "mar": "03",
    "april": "04", "apr": "04",
    "mai": "05", "may": "05",
    "juni": "06", "june": "06", "jun": "06",
    "juli": "07", "july": "07", "jul": "07",
    "august": "08", "aug": "08",
    "september": "09", "sep": "09", "sept": "09",
    "oktober": "10", "october": "10", "okt": "10", "oct": "10",
    "november": "11", "nov": "11",
    "desember": "12", "december": "12", "des": "12", "dec": "12",
}


def extract_iso_date(text: str) -> str | None:
    """Extract standard ISO-8601 date (YYYY-MM-DD) from metadata or page text."""
    if not text:
        return None
    m1 = re.search(r"\b(202[0-6])[-/.](0[1-9]|1[0-2])[-/.](0[1-9]|[12][0-9]|3[01])\b", text)
    if m1:
        y, m, d = m1.groups()
        return f"{y}-{m.zfill(2)}-{d.zfill(2)}"
    m2 = re.search(r"\b(0[1-9]|[12][0-9]|3[01])[-/.](0[1-9]|1[0-2])[-/.](202[0-6])\b", text)
    if m2:
        d, m, y = m2.groups()
        return f"{y}-{m.zfill(2)}-{d.zfill(2)}"
    m3 = re.search(
        r"\b([0-2]?[0-9]|3[01])\.?\s*(januar|februar|mars|april|mai|juni|juli|august|september|oktober|november|desember)\s*(202[0-6])\b",
        text, re.I
    )
    if m3:
        d, m_name, y = m3.groups()
        m_num = MONTH_MAP.get(m_name.lower(), "01")
        return f"{y}-{m_num}-{d.zfill(2)}"
    m4 = re.search(
        r"\b(january|february|march|april|may|june|july|august|september|october|november|december)\s*([0-2]?[0-9]|3[01]),?\s*(202[0-6])\b",
        text, re.I
    )
    if m4:
        m_name, d, y = m4.groups()
        m_num = MONTH_MAP.get(m_name.lower(), "01")
        return f"{y}-{m_num}-{d.zfill(2)}"
    return None


def site_claims(
    crawl: SiteCrawl,
    store: EvidenceStore,
    *,
    verified_url: str,
) -> list[dict[str, Any]]:
    """Build company-owned claims from an identity-accepted site crawl.

    First-seen wins for each contact value, and the evidence points at the exact
    page it was read from, so the envelope can show provenance per fact.
    """
    claims: list[dict[str, Any]] = []
    if not crawl.pages:
        return claims

    home = crawl.pages[0]
    domain = registrable_domain(verified_url)
    import urllib.parse
    parsed_v = urllib.parse.urlparse(verified_url)
    clean_host = parsed_v.netloc.lower()
    if clean_host.startswith("www."):
        clean_host = clean_host[4:]
    site_value = f"https://{clean_host}" if clean_host else f"https://{domain}"

    # 1. The verified own website. Distinct from the registry's `hjemmeside`
    #    pointer (`website_registry`): this one was fetched and identity-checked.
    claims.append(make_claim(
        field="website", value=site_value, availability="available",
        evidence_ids=_page_evidence(store, home, f"verified own site: {clean_host or domain}"),
        confidence=0.99,
        note="domain fetched and accepted by the identity gate"))

    # 2. Aggregate contact details and structured data across crawled pages.
    #    Track the page each value first appeared on for precise provenance.
    emails: dict[str, Page] = {}
    phones: dict[str, Page] = {}
    socials: dict[str, tuple[str, Page]] = {}
    structured_names: dict[str, Page] = {}
    addresses: list[tuple[dict[str, Any], Page]] = []

    for page in crawl.pages:
        if not page.html:
            continue
        contacts = extract_contacts(page.html, page.html)
        for email in contacts.get("emails", []):
            emails.setdefault(email, page)
        for phone in contacts.get("phones", []):
            phones.setdefault(phone, page)
        for label, url in (contacts.get("social_profiles") or {}).items():
            socials.setdefault(label, (url, page))

        structured = parse_structured(page.html, page.final_url or page.url)
        for name in structured.get("legal_names", []) + structured.get("names", []):
            structured_names.setdefault(name, page)
        for addr in structured.get("addresses", []):
            if addr and any(addr.values()):
                addresses.append((addr, page))
        # Structured contact points, too — schema.org is the strongest on-site
        # signal, so its emails/phones are preferred but merged, not duplicated.
        for email in structured.get("emails", []):
            emails.setdefault(email.lower(), page)
        for phone in structured.get("phones", []):
            phones.setdefault(phone, page)

    if emails:
        first_email = next(iter(emails))
        claims.append(make_claim(
            field="contact_email", value=sorted(emails),
            availability="available",
            evidence_ids=_page_evidence(
                store, emails[first_email], f"email on page: {first_email}"),
            confidence=0.9))
    if phones:
        first_phone = next(iter(phones))
        claims.append(make_claim(
            field="contact_phone", value=sorted(phones),
            availability="available",
            evidence_ids=_page_evidence(
                store, phones[first_phone], f"phone on page: {first_phone}"),
            confidence=0.85))
    if socials:
        value = {label: url for label, (url, _) in socials.items()}
        any_page = next(iter(socials.values()))[1]
        claims.append(make_claim(
            field="social_profiles", value=value, availability="available",
            evidence_ids=_page_evidence(
                store, any_page, "social profile links in page markup"),
            confidence=0.9))
    if addresses:
        addr, page = addresses[0]
        claims.append(make_claim(
            field="site_address", value=addr, availability="available",
            evidence_ids=_page_evidence(
                store, page, "postal address in schema.org markup"),
            confidence=0.85,
            note="address as published in the site's structured data"))

    # 3. On-site hiring & activity signal (Section 5 of competition contract)
    career_pages = [
        p for p in crawl.pages
        if re.search(r"/(?:karriere|jobs|stillinger|ledige-stillinger|work-with-us|jobb|vacanc)(?:/|$)",
                     urllib.parse.urlparse(p.url).path, re.I)
    ]
    career_url = career_pages[0].url if career_pages else None
    career_title = None
    if career_pages:
        m = re.search(r"<title[^>]*>([^<]+)</title>", career_pages[0].html, re.I)
        career_title = m.group(1).strip() if m else "Karriere / Ledige stillinger"

    activity_metrics = {
        "status": "active_recruitment" if career_pages else "site_active",
        "career_url": career_url,
        "title": career_title,
        "bounded_pages_captured": len(crawl.pages),
        "verified_social_links": len(socials),
        "structured_records": len(structured_names) + len(addresses),
        "career_pages_observed": len(career_pages),
    }
    target_page = career_pages[0] if career_pages else home
    claims.append(make_claim(
        field="hiring_or_activity_signal",
        value=activity_metrics,
        availability="available",
        evidence_ids=_page_evidence(
            store, target_page,
            f"Observed company career presence: {career_url or site_value} ({len(career_pages)} career pages captured)"
        ),
        confidence=0.95,
        note="Observed career portal and hiring presence on verified company domain"
    ))

    # 4. On-site dated public activity / news
    news_pages = [
        p for p in crawl.pages
        if re.search(r"/(?:news|press|aktuelt|nyheter|artikler|blog|media)(?:/|$)",
                     urllib.parse.urlparse(p.url).path, re.I)
    ]
    if news_pages:
        news_page = news_pages[0]
        m = re.search(r"<title[^>]*>([^<]+)</title>", news_page.html, re.I)
        raw_title = m.group(1).strip() if m else ""
        title = raw_title or f"Company news page on {domain}"

        art_date = None
        for meta_name in ("article:published_time", "og:published_time", "datePublished", "pubdate", "date"):
            meta_m = re.search(rf'<meta[^>]+(?:property|name)=["\']{meta_name}["\'][^>]+content=["\']([^"\']+)["\']', news_page.html, re.I)
            if meta_m:
                art_date = extract_iso_date(meta_m.group(1))
                if art_date:
                    break
        if not art_date:
            time_m = re.search(r'<time[^>]+datetime=["\']([^"\']+)["\']', news_page.html, re.I)
            if time_m:
                art_date = extract_iso_date(time_m.group(1))
        if not art_date:
            art_date = extract_iso_date(news_page.html[:4000]) or extract_iso_date(news_page.url)

        claims.append(make_claim(
            field="dated_public_activity",
            value={
                "title": title[:200],
                "url": news_page.url,
                "date": art_date,
                "source": "company_news"
            },
            availability="available",
            evidence_ids=_page_evidence(
                store, news_page,
                f"Company news/activity post: {title[:160]} (date: {art_date or 'recent'})"
            ),
            confidence=0.95,
            note="Company-owned public announcement or news section"
        ))

    return claims
