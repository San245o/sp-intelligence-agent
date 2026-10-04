"""Verified social profiles and encyclopedic knowledge connector.

Loads exact-entity verified corporate profiles from:
1. universe_social_profiles.json (23,525 verified profiles across LinkedIn, Twitter, Facebook, YouTube)
2. wikidata_enriched.json (10,290 verified entities with Norwegian Wikipedia articles and handles)
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..evidence import (
    SOURCE_OPEN_DATA,
    EvidenceStore,
    make_claim,
    sha256_text,
    utc_now,
)

_SOCIALS_PATH = Path(__file__).resolve().parents[3] / "data" / "universe_social_profiles.json"
_WIKIDATA_PATH = Path(__file__).resolve().parents[3] / "data" / "wikidata_enriched.json"

_cached_socials: dict[str, dict[str, Any]] | None = None
_cached_wikidata: dict[str, dict[str, Any]] | None = None


def _get_socials_cache() -> dict[str, dict[str, Any]]:
    global _cached_socials
    if _cached_socials is None:
        if _SOCIALS_PATH.exists():
            try:
                _cached_socials = json.loads(_SOCIALS_PATH.read_text(encoding="utf-8"))
            except Exception:
                _cached_socials = {}
        else:
            _cached_socials = {}
    return _cached_socials


def _get_wikidata_cache() -> dict[str, dict[str, Any]]:
    global _cached_wikidata
    if _cached_wikidata is None:
        if _WIKIDATA_PATH.exists():
            try:
                _cached_wikidata = json.loads(_WIKIDATA_PATH.read_text(encoding="utf-8"))
            except Exception:
                _cached_wikidata = {}
        else:
            _cached_wikidata = {}
    return _cached_wikidata


def fetch_verified_socials(
    org: str,
    legal_name: str,
    store: EvidenceStore,
    existing_socials: dict[str, str] | None = None,
) -> tuple[dict[str, str], list[dict[str, Any]]]:
    """Supplement on-page social links with pre-verified exact-entity external channels."""
    merged = dict(existing_socials or {})
    claims: list[dict[str, Any]] = []

    social_entry = _get_socials_cache().get(org)
    wiki_entry = _get_wikidata_cache().get(org)

    new_platforms: dict[str, str] = {}
    if social_entry:
        for plat, url in (social_entry.get("platforms") or {}).items():
            if plat not in merged and url:
                merged[plat] = url
                new_platforms[plat] = url

    if wiki_entry:
        for plat, url in (wiki_entry.get("socials") or {}).items():
            if plat not in merged and url:
                merged[plat] = url
                new_platforms[plat] = url

    if not new_platforms:
        return merged, []

    source_url = (
        (wiki_entry and wiki_entry.get("wikidata_url"))
        or (social_entry and (social_entry.get("platforms") or {}).get("linkedin"))
        or f"https://data.brreg.no/enhetsregisteret/api/enheter/{org}"
    )
    span = f"Verified corporate social profiles for {legal_name} ({org}): " + ", ".join(
        f"{k}={v}" for k, v in new_platforms.items()
    )
    eid = store.create(
        source_url=source_url,
        source_class=SOURCE_OPEN_DATA,
        claim_span=span,
        content_sha256=sha256_text(span),
    )

    claim = make_claim(
        field="social_profiles",
        value=merged,
        availability="available",
        confidence=0.95,
        evidence_ids=[eid],
        note=f"Verified multi-platform corporate social presence ({len(merged)} channels)",
    )
    return merged, [claim]
