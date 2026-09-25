"""Evidence records and immutable source snapshots.

Every published claim must point at an evidence record carrying source URL,
retrieval time, content hash and the span the claim was read from. Refresh
compares content hashes, so hashing must be stable and cover exactly the bytes
the extractor saw.
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from typing import Any

from .config import AVAILABILITY_STATES

# Source classes, ordered by how much weight a claim may carry.
SOURCE_OFFICIAL_REGISTRY = "official_registry"
SOURCE_OFFICIAL_ACCOUNTS = "official_annual_accounts"
SOURCE_COMPANY_OWNED = "company_owned"
SOURCE_OPEN_DATA = "open_data"
SOURCE_LICENSED = "licensed_feed"
SOURCE_PUBLIC_NEWS = "public_news"
SOURCE_CANDIDATE_ONLY = "candidate_discovery"  # never valid as claim evidence

#: Search output and other discovery signals generate candidates only. The
#: source policy forbids using them as the evidence for a published fact.
NON_PUBLISHABLE_SOURCE_CLASSES = frozenset({SOURCE_CANDIDATE_ONLY})


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_text(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def new_evidence_id(prefix: str = "ev") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def make_evidence(
    *,
    source_url: str,
    source_class: str,
    retrieved_at: str | None = None,
    content_sha256: str | None = None,
    claim_span: str | None = None,
    http_status: int | None = None,
    final_url: str | None = None,
    redirect_chain: list[str] | None = None,
    reporting_period: str | None = None,
    extractor: str | None = None,
    licence: str | None = None,
    evidence_id: str | None = None,
) -> dict[str, Any]:
    """Build one evidence record.

    `claim_span` is the literal text the claim was read from, so a reviewer can
    confirm support without re-fetching. Truncated to keep envelopes readable.
    """
    span = claim_span
    if span and len(span) > 400:
        span = span[:397] + "..."
    record: dict[str, Any] = {
        "id": evidence_id or new_evidence_id(),
        "source_url": source_url,
        "source_class": source_class,
        "retrieved_at": retrieved_at or utc_now(),
    }
    if final_url and final_url != source_url:
        record["final_url"] = final_url
    if redirect_chain:
        record["redirect_chain"] = redirect_chain
    if http_status is not None:
        record["http_status"] = http_status
    if content_sha256:
        record["content_sha256"] = content_sha256
    if span:
        record["claim_span"] = span
    if reporting_period:
        record["reporting_period"] = reporting_period
    if extractor:
        record["extractor"] = extractor
    if licence:
        record["licence"] = licence
    return record


def make_claim(
    *,
    field: str,
    value: Any,
    availability: str,
    evidence_ids: list[str] | None = None,
    confidence: float | None = None,
    reporting_period: str | None = None,
    note: str | None = None,
    subject: str | None = None,
) -> dict[str, Any]:
    """Build one claim.

    A claim with availability `available` must carry at least one evidence id.
    `not_available` means a source was checked and held nothing, which is
    materially different from a field that was never checked.
    """
    if availability not in AVAILABILITY_STATES:
        raise ValueError(f"Invalid availability state: {availability!r}")
    if availability == "available" and not evidence_ids:
        raise ValueError(f"Claim {field!r} is 'available' but cites no evidence")
    if availability == "available" and value is None:
        raise ValueError(f"Claim {field!r} is 'available' but has no value")

    claim: dict[str, Any] = {
        "field": field,
        "value": value,
        "availability": availability,
        "evidence_ids": list(evidence_ids or []),
    }
    if subject:
        claim["subject"] = subject
    if confidence is not None:
        claim["confidence"] = round(float(confidence), 3)
    if reporting_period:
        claim["reporting_period"] = reporting_period
    if note:
        claim["note"] = note
    return claim


def claim_key(claim: dict[str, Any]) -> str:
    """Stable identity for a claim across refreshes.

    Must not include the value: a claim keeps its identity when the value
    changes, which is exactly what makes a refresh diff possible.
    """
    parts = [str(claim.get("field") or "")]
    if claim.get("subject"):
        parts.append(str(claim["subject"]))
    if claim.get("reporting_period"):
        parts.append(str(claim["reporting_period"]))
    return "|".join(parts)


class EvidenceStore:
    """Collects evidence for one company and hands back ids for claims."""

    def __init__(self) -> None:
        self._records: dict[str, dict[str, Any]] = {}

    def add(self, record: dict[str, Any]) -> str:
        self._records[record["id"]] = record
        return record["id"]

    def create(self, **kwargs: Any) -> str:
        return self.add(make_evidence(**kwargs))

    def get(self, evidence_id: str) -> dict[str, Any] | None:
        return self._records.get(evidence_id)

    def all(self) -> list[dict[str, Any]]:
        return list(self._records.values())

    def referenced_by(self, claims: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Only emit evidence a claim actually cites, keeping envelopes lean."""
        wanted = {eid for claim in claims for eid in claim.get("evidence_ids", [])}
        return [record for rid, record in self._records.items() if rid in wanted]
