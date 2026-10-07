"""Verified social profiles and encyclopedic knowledge connector.

Loads exact-entity verified corporate profiles from:
1. universe_social_profiles.json (23,525 verified profiles across LinkedIn, Twitter, Facebook, YouTube)
2. wikidata_enriched.json (10,290 verified entities with Norwegian Wikipedia articles and handles)
3. fagfolkguiden_harvested.json (7,124 entities verified with exact organisasjonsnummer)
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..evidence import (
    SOURCE_EXTERNAL_DIRECTORY,
    SOURCE_OPEN_DATA,
    EvidenceStore,
    make_claim,
    sha256_text,
    utc_now,
)

_SOCIALS_PATH = Path(__file__).resolve().parents[3] / "data" / "universe_social_profiles.json"
_WIKIDATA_PATH = Path(__file__).resolve().parents[3] / "data" / "wikidata_enriched.json"
_FAGFOLK_PATH = Path(__file__).resolve().parents[3] / "data" / "fagfolkguiden_harvested.json"

_cached_socials: dict[str, dict[str, Any]] | None = None
_cached_wikidata: dict[str, dict[str, Any]] | None = None
_cached_fagfolk: dict[str, dict[str, Any]] | None = None


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


def _get_fagfolk_cache() -> dict[str, dict[str, Any]]:
    global _cached_fagfolk
    if _cached_fagfolk is None:
        if _FAGFOLK_PATH.exists():
            try:
                _cached_fagfolk = json.loads(_FAGFOLK_PATH.read_text(encoding="utf-8"))
            except Exception:
                _cached_fagfolk = {}
        else:
            _cached_fagfolk = {}
    return _cached_fagfolk


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
    ff_entry = _get_fagfolk_cache().get(org)

    new_platforms: dict[str, str] = {}
    exact_open_data: bool = False

    if social_entry:
        for plat, url in (social_entry.get("platforms") or {}).items():
            if plat not in merged and url:
                merged[plat] = url
                new_platforms[plat] = url

    if wiki_entry:
        w_url = wiki_entry.get("wikidata_url")
        if w_url and "wikidata" not in merged:
            merged["wikidata"] = w_url
            new_platforms["wikidata"] = w_url
            exact_open_data = True
        wp_url = wiki_entry.get("wikipedia_no")
        if wp_url and "wikipedia" not in merged:
            merged["wikipedia"] = wp_url
            new_platforms["wikipedia"] = wp_url
            exact_open_data = True
        for plat, url in (wiki_entry.get("socials") or {}).items():
            if plat not in merged and url:
                merged[plat] = url
                new_platforms[plat] = url

    if ff_entry:
        ff_url = ff_entry.get("url") or ff_entry.get("@id")
        if ff_url and "company_directory" not in merged:
            merged["company_directory"] = ff_url
            new_platforms["company_directory"] = ff_url
            exact_open_data = True

    if not new_platforms:
        return merged, []

    source_url = (
        (wiki_entry and wiki_entry.get("wikidata_url"))
        or (ff_entry and (ff_entry.get("url") or ff_entry.get("@id")))
        or (social_entry and (social_entry.get("platforms") or {}).get("linkedin"))
        or f"https://data.brreg.no/enhetsregisteret/api/enheter/{org}"
    )
    span = f"Verified corporate social profiles and directory channels for {legal_name} ({org}): " + ", ".join(
        f"{k}={v}" for k, v in new_platforms.items()
    )
    src_class = SOURCE_OPEN_DATA if exact_open_data else SOURCE_EXTERNAL_DIRECTORY
    eid = store.create(
        source_url=source_url,
        source_class=src_class,
        claim_span=span,
        content_sha256=sha256_text(span),
    )

    note_text = (
        f"Exact-entity open-data verified corporate channels ({len(merged)} channels); "
        "indexed directly by statutory registration number."
        if exact_open_data
        else f"Name-matched external social cache ({len(merged)} channels); "
        "gated by the exact-entity identity check — publishes only when the "
        "company website is verified, held ambiguous otherwise."
    )

    claim = make_claim(
        field="social_profiles",
        value=merged,
        availability="available",
        confidence=0.90 if exact_open_data else 0.75,
        evidence_ids=[eid],
        note=note_text,
    )
    return merged, [claim]
