"""Google News RSS connector for dated public company activity.

Fetches the structured RSS feed for exact-title company mentions in Norwegian news.
Zero API cost, structured XML, lawful access, and protected by exact title matching
to avoid wrong-company attribution.
"""
from __future__ import annotations

import re
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

from ..evidence import (
    SOURCE_PUBLIC_NEWS,
    EvidenceStore,
    make_claim,
    sha256_text,
    utc_now,
)
from ..http import Fetcher

LEGAL_SUFFIXES = {
    "as", "asa", "sa", "ba", "da", "ans", "enk", "nuf", "sti", "iks", "kf", "sf"
}
EXTRACTOR = "google_news_rss_v1"
LICENCE = "Public RSS / Fair Use Citation"


def _clean_tokens(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9æøå]+", str(text or "").casefold()) if t not in LEGAL_SUFFIXES]


CORPORATE_INDICATORS = {
    "as", "asa", "da", "ans", "aksje", "aksjer", "børs", "regnskap", "resultat",
    "cfo", "ceo", "styreleder", "direktør", "oppbud", "konkurs", "omsetning",
    "driftsresultat", "kvartal", "underskudd", "overskudd", "emisjon", "fusjon", "oppkjøp"
}


def exact_title_match(company_name: str, title: str) -> bool:
    """Verify that the exact legal name tokens appear in the headline without conflating."""
    company_tokens = _clean_tokens(company_name)
    headline_clean = str(title or "").rsplit(" - ", 1)[0]
    title_tokens = _clean_tokens(headline_clean)

    if not company_tokens or not title_tokens or len(company_tokens) > len(title_tokens):
        return False

    # Single-word dictionary names (e.g. Havnen, Lugg, AFP, Bryggen) frequently appear
    # in unrelated general news. For single-token names, require either the legal suffix
    # (e.g. "AS") or a clear corporate context token in the headline.
    if len(company_tokens) == 1:
        all_raw_headline_tokens = [
            t for t in re.findall(r"[a-z0-9æøå]+", str(headline_clean or "").casefold())
        ]
        has_legal_suffix = any(s in all_raw_headline_tokens for s in LEGAL_SUFFIXES)
        has_corp_indicator = any(ind in all_raw_headline_tokens for ind in CORPORATE_INDICATORS)
        if not (has_legal_suffix or has_corp_indicator):
            return False

    allowed_predecessors = {
        "av", "for", "fra", "hos", "i", "med", "om", "på", "til", "og", "kjøper", "velger", "ved"
    }
    for i in range(len(title_tokens) - len(company_tokens) + 1):
        if title_tokens[i : i + len(company_tokens)] == company_tokens:
            if i == 0 or title_tokens[i - 1] in allowed_predecessors:
                return True
    return False


def fetch_news_activity(
    fetcher: Fetcher,
    org: str,
    company_name: str,
    store: EvidenceStore,
    *,
    limit: int = 2,
    years: int = 1,
) -> list[dict[str, Any]]:
    """Query Google News RSS for verified company mentions in Norwegian news."""
    clean_name = re.sub(r"\s+", " ", str(company_name or "")).strip()
    tokens = _clean_tokens(clean_name)
    if not tokens or len(tokens) < 1:
        return []

    search_name = " ".join(tokens)
    query = urllib.parse.quote(f'"{search_name}" when:{years}y')
    url = f"https://news.google.com/rss/search?q={query}&hl=no&gl=NO&ceid=NO:no"

    response = fetcher.get(
        org, url, accept="application/rss+xml, application/xml", check_robots=False
    )
    if not response.ok or not response.body:
        return []

    try:
        root = ET.fromstring(response.body)
    except Exception:
        return []

    claims: list[dict[str, Any]] = []
    seen_titles: set[str] = set()

    for item in root.findall(".//item"):
        title = str(item.findtext("title") or "").strip()
        link = str(item.findtext("link") or "").strip()
        publisher = str(item.findtext("source") or "").strip()

        if not title or not exact_title_match(clean_name, title):
            continue

        normalized_title = title.casefold()
        if normalized_title in seen_titles:
            continue
        seen_titles.add(normalized_title)

        pub_date_raw = item.findtext("pubDate")
        published_at: str | None = None
        if pub_date_raw:
            try:
                published_at = (
                    parsedate_to_datetime(pub_date_raw)
                    .astimezone(timezone.utc)
                    .isoformat()
                    .replace("+00:00", "Z")
                )
            except Exception:
                published_at = None

        reporting_period = published_at[:10] if published_at else None

        evidence_id = store.create(
            source_url=link or url,
            source_class=SOURCE_PUBLIC_NEWS,
            retrieved_at=response.retrieved_at,
            content_sha256=response.content_sha256,
            claim_span=title,
            http_status=response.status,
            final_url=link or response.final_url,
            reporting_period=reporting_period,
            extractor=EXTRACTOR,
            licence=LICENCE,
        )

        claim_value = {
            "title": title,
            "publisher": publisher or "News",
            "published_at": published_at,
            "url": link,
        }

        claims.append(
            make_claim(
                field="news_mention",
                value=claim_value,
                availability="available",
                evidence_ids=[evidence_id],
                confidence=0.92,
                reporting_period=reporting_period,
                subject=publisher or "press",
            )
        )

        if len(claims) >= limit:
            break

    return claims
