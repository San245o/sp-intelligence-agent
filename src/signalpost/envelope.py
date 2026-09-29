"""The terminal envelope: exactly one per input organisation number.

The contract requires one envelope per input with a definite disposition, every
published claim pointing at evidence, and one of the six availability states on
every field. This module is the single place that shape is assembled, so the
schema stays consistent across the batch and the local scorer can rely on it.

Envelope disposition (distinct from per-field availability):
  official   identity proven, at least one published claim
  ambiguous  identity not proven to publish threshold; candidates recorded
  empty      identity fine but no facts beyond the registry existed
  blocked    everything of interest was robots/paywall blocked
  failed     the entity could not be resolved or the run errored
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .evidence import EvidenceStore, claim_key
from .identity import IdentityVerdict

SCHEMA_VERSION = "signalpost.envelope/1"

DISPOSITION_OFFICIAL = "official"
DISPOSITION_AMBIGUOUS = "ambiguous"
DISPOSITION_EMPTY = "empty"
DISPOSITION_BLOCKED = "blocked"
DISPOSITION_FAILED = "failed"


@dataclass
class CompanyEnvelope:
    organisation_number: str
    input_name: str
    disposition: str
    claims: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    identity: dict[str, Any] | None = None
    discovery: dict[str, Any] | None = None
    synthesis: dict[str, Any] | None = None
    diagnostics: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema": SCHEMA_VERSION,
            "organisation_number": self.organisation_number,
            "input_name": self.input_name,
            "disposition": self.disposition,
            "claims": self.claims,
            "evidence": self.evidence,
        }
        if self.identity is not None:
            payload["identity"] = self.identity
        if self.discovery is not None:
            payload["discovery"] = self.discovery
        if self.synthesis is not None:
            payload["synthesis"] = self.synthesis
        if self.warnings:
            payload["warnings"] = self.warnings
        payload["diagnostics"] = self.diagnostics
        return payload


def _published(claims: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [c for c in claims if c.get("availability") == "available"]


def _dedupe_claims(claims: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse claims that share an identity key, newest evidence wins.

    Two connectors can report the same field (registry website vs on-site
    canonical). Identical keys are merged so the envelope has one claim per
    (field, subject, period), which is also what makes refresh diffs stable.
    """
    by_key: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for claim in claims:
        key = claim_key(claim)
        if key not in by_key:
            by_key[key] = claim
            order.append(key)
            continue
        existing = by_key[key]
        # Prefer an available claim over a non-available one; otherwise keep the
        # higher-confidence claim and union the evidence ids.
        prefer = claim
        if existing.get("availability") == "available" and claim.get("availability") != "available":
            prefer = existing
        elif claim.get("availability") == "available" and existing.get("availability") != "available":
            prefer = claim
        elif (claim.get("confidence") or 0) <= (existing.get("confidence") or 0):
            prefer = existing
        merged_ev = list(dict.fromkeys(
            (existing.get("evidence_ids") or []) + (claim.get("evidence_ids") or [])))
        prefer = dict(prefer)
        prefer["evidence_ids"] = merged_ev
        by_key[key] = prefer
    return [by_key[k] for k in order]


def build_envelope(
    *,
    organisation_number: str,
    input_name: str,
    claims: list[dict[str, Any]],
    store: EvidenceStore,
    identity: IdentityVerdict | None,
    discovery: dict[str, Any] | None = None,
    synthesis: dict[str, Any] | None = None,
    diagnostics: dict[str, Any] | None = None,
    warnings: list[str] | None = None,
    entity_resolved: bool = True,
    run_failed: bool = False,
) -> CompanyEnvelope:
    """Assemble one company's envelope and decide its disposition.

    The identity verdict gates what "published" means: if identity did not clear
    the publish threshold, on-site claims are downgraded to `ambiguous` so a
    wrong-company fact can never leave as `available`. Registry claims (which
    are keyed on the org number itself, not on a matched page) are exempt — the
    register is authoritative for the entity by construction.
    """
    warnings = list(warnings or [])
    claims = _dedupe_claims(claims)

    identity_ok = bool(identity and identity.publishable)

    # Downgrade on-site claims when identity is not proven. A claim is "on-site"
    # if its evidence comes from a company_owned / open_data source rather than
    # the official registry/accounts.
    if not identity_ok:
        for claim in claims:
            if claim.get("availability") != "available":
                continue
            sources = {
                (store.get(eid) or {}).get("source_class")
                for eid in claim.get("evidence_ids", [])}
            registry_backed = sources & {"official_registry", "official_annual_accounts", "official_job_board"}
            if not registry_backed:
                claim["availability"] = "ambiguous"
                claim["note"] = (claim.get("note", "") +
                                 " | identity not proven to publish threshold; "
                                 "held as ambiguous").strip(" |")

    published = _published(claims)

    # --- disposition
    if run_failed or not entity_resolved:
        disposition = DISPOSITION_FAILED
    elif published and (identity_ok or any(
            (store.get(eid) or {}).get("source_class") in
            ("official_registry", "official_annual_accounts", "official_job_board")
            for c in published for eid in c.get("evidence_ids", []))):
        disposition = DISPOSITION_OFFICIAL
    elif any(c.get("availability") == "ambiguous" for c in claims):
        disposition = DISPOSITION_AMBIGUOUS
    elif any(c.get("availability") == "blocked" for c in claims) and not published:
        disposition = DISPOSITION_BLOCKED
    else:
        disposition = DISPOSITION_EMPTY

    envelope = CompanyEnvelope(
        organisation_number=organisation_number,
        input_name=input_name,
        disposition=disposition,
        claims=claims,
        evidence=store.referenced_by(claims),
        identity=identity.to_dict() if identity else None,
        discovery=discovery,
        synthesis=synthesis,
        diagnostics=diagnostics or {},
        warnings=warnings,
    )
    envelope.diagnostics.setdefault("claim_counts", {})
    envelope.diagnostics["claim_counts"] = {
        "total": len(claims),
        "available": len(published),
        "not_available": sum(1 for c in claims if c.get("availability") == "not_available"),
        "ambiguous": sum(1 for c in claims if c.get("availability") == "ambiguous"),
        "blocked": sum(1 for c in claims if c.get("availability") == "blocked"),
        "not_applicable": sum(1 for c in claims if c.get("availability") == "not_applicable"),
        "failed": sum(1 for c in claims if c.get("availability") == "failed"),
    }
    return envelope
