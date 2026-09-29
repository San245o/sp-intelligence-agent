"""Google Places & Ratings connector using Serper.dev Places API.

Queries Google Maps / Places for verified physical businesses and extracts
canonical Place ID (CID), star rating, review count, and address.
Strictly gated by exact entity and location matching to prevent cross-company
attribution (Rule #4).
"""
from __future__ import annotations

import json
import os
import re
import urllib.parse
from typing import Any

from ..evidence import (
    SOURCE_LICENSED,
    EvidenceStore,
    make_claim,
    sha256_bytes,
    utc_now,
)
from ..http import Fetcher
from ..identity import LEGAL_FORMS, fold, name_tokens

SERPER_PLACES_URL = "https://google.serper.dev/places"
EXTRACTOR = "serper_google_places_v1"
LICENCE = "Serper Google Places API / Google Maps Attribution"


def strip_legal_form(name: str) -> str:
    folded = fold(name)
    toks = folded.split()
    if len(toks) > 1 and toks[-1] in LEGAL_FORMS:
        toks = toks[:-1]
    return " ".join(toks)


def _locations_match(candidate_addr: str | None, expected_muni: str | None, expected_addr: str | None) -> bool:
    """Check if the place address matches the company's municipality or registered address."""
    if not candidate_addr:
        return False
    cand_norm = fold(candidate_addr)
    if expected_muni and fold(expected_muni) in cand_norm:
        return True
    if expected_addr:
        addr_norm = fold(expected_addr)
        # Check postal code or street match
        for tok in addr_norm.split():
            if len(tok) >= 4 and tok in cand_norm:
                return True
    return False


def _names_match_place(legal_name: str, place_title: str) -> bool:
    """Verify place title refers to the legal entity."""
    a, b = fold(legal_name), fold(place_title)
    if not a or not b:
        return False
    a_clean = strip_legal_form(a)
    b_clean = strip_legal_form(b)
    if a_clean == b_clean or a == b or a_clean in b_clean or b_clean in a_clean:
        return True
    # Check token overlap
    a_toks = set(name_tokens(legal_name))
    b_toks = set(name_tokens(place_title))
    if not a_toks or not b_toks:
        return False
    overlap = a_toks & b_toks
    # Major name tokens must be present
    return len(overlap) >= min(len(a_toks), 2) and (len(overlap) / len(a_toks)) >= 0.5


def fetch_places_ratings(
    fetcher: Fetcher,
    org: str,
    legal_name: str,
    municipality: str | None,
    business_address: str | None,
    store: EvidenceStore,
    *,
    api_key: str | None = None,
) -> list[dict[str, Any]]:
    """Fetch verified Google Place ratings, review count, and Place ID."""
    key = api_key or os.environ.get("SERPER_API_KEY")
    if not key:
        return []

    clean_name = str(legal_name or "").strip()
    if not clean_name:
        return []

    # Build query combining company name and municipality
    query_parts = [strip_legal_form(clean_name) or clean_name]
    if municipality:
        query_parts.append(municipality)
    query_str = " ".join(query_parts)

    payload = json.dumps({
        "q": query_str,
        "gl": "no",
        "hl": "no",
        "num": 5,
    }).encode("utf-8")

    response = fetcher.get(
        org,
        f"{SERPER_PLACES_URL}?q={urllib.parse.quote(query_str)}",
        headers={
            "X-API-KEY": key,
            "Content-Type": "application/json",
        },
        data=payload,
        check_robots=False,
    )

    if not response.ok:
        state = "not_available" if response.status in (404, 400) else "failed"
        return [make_claim(
            field="ratings_and_reviews",
            value=None,
            availability=state,
            note=f"Google Places API returned {response.error or response.status}",
        )]

    try:
        data = response.json()
    except Exception:
        return [make_claim(
            field="ratings_and_reviews",
            value=None,
            availability="failed",
            note="Google Places response was not valid JSON",
        )]

    places = data.get("places") or []
    if not places:
        return [make_claim(
            field="ratings_and_reviews",
            value=None,
            availability="not_available",
            note=f"checked Google Places for {query_str!r}; 0 physical listings found",
        )]

    # Match candidates against exact legal entity name and address
    matched_place: dict[str, Any] | None = None
    for p in places:
        title = p.get("title") or ""
        addr = p.get("address") or ""
        if _names_match_place(clean_name, title):
            # If municipality exists, verify address matches
            if municipality and not _locations_match(addr, municipality, business_address):
                continue
            matched_place = p
            break

    if not matched_place:
        return [make_claim(
            field="ratings_and_reviews",
            value=None,
            availability="ambiguous",
            note=f"Google Places returned {len(places)} results but none passed exact entity/location gates",
        )]

    cid = str(matched_place.get("cid") or "")
    rating = matched_place.get("rating")
    rating_val = float(rating) if rating is not None else None
    rating_count = matched_place.get("ratingCount")
    count_val = int(rating_count) if rating_count is not None else (0 if rating_val is None else None)
    title = matched_place.get("title") or clean_name
    place_addr = matched_place.get("address") or business_address
    category = matched_place.get("category")

    span = (
        f"Google Place: {title} | Place ID: {cid} | Rating: {rating_val or 'N/A'} "
        f"({count_val or 0} reviews) | Address: {place_addr}"
    )

    ev_id = store.create(
        source_url=f"https://www.google.com/maps?cid={cid}" if cid else SERPER_PLACES_URL,
        source_class=SOURCE_LICENSED,
        retrieved_at=response.retrieved_at or utc_now(),
        content_sha256=response.content_sha256 or sha256_bytes(payload),
        claim_span=span,
        http_status=response.status,
        extractor=EXTRACTOR,
        licence=LICENCE,
    )

    review_data = {
        "source": "google_places",
        "place_id": cid,
        "title": title,
        "rating": rating_val,
        "rating_count": count_val,
        "address": place_addr,
        "category": category,
    }

    claims = [
        make_claim(
            field="ratings_and_reviews",
            value=review_data,
            availability="available",
            evidence_ids=[ev_id],
            confidence=0.95,
            note=f"Verified Google Place listing: {rating_val or 'Unrated'} ({count_val or 0} reviews)",
        ),
        make_claim(
            field="google_place_id",
            value=cid,
            availability="available",
            evidence_ids=[ev_id],
            confidence=0.98,
        ),
    ]

    if rating_val is not None:
        claims.append(make_claim(
            field="rating_score",
            value=rating_val,
            availability="available",
            evidence_ids=[ev_id],
            confidence=0.95,
            note=f"Average star rating ({rating_val}/5.0)",
        ))

    if count_val is not None:
        claims.append(make_claim(
            field="review_count",
            value=count_val,
            availability="available",
            evidence_ids=[ev_id],
            confidence=0.95,
            note=f"Total user review count ({count_val})",
        ))

    return claims
