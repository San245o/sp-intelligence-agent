"""Per-company research pipeline: registry -> discovery -> identity -> site.

`research_company` runs one organisation number end to end and returns a
terminal envelope dict. The order is deliberate and each step is gated by the
company's spend tier so the run budget concentrates where evidence exists:

  1. BRREG entity (always). Authoritative identity + registry facts. If the
     number is not in the register the company cannot be resolved -> failed.
  2. Registry enrichment. Accounts for every filing entity; roles and subunits
     for standard/rich tiers. All authoritative, all cheap.
  3. Website discovery + identity gate (standard/rich only). Candidates are
     generated deterministically, ordered by the local ranker, then fetched in
     order until one clears the identity publish threshold. Only an accepted
     site is crawled for on-site claims.
  4. Envelope assembly. `build_envelope` dedupes claims, downgrades any on-site
     claim to `ambiguous` when identity was not proven, and fixes disposition.
  5. Synthesis. A deterministic brief over the *final* claim states.

No cloud LLM anywhere. The only model is the local candidate ranker, used as a
fetch order and a prior — never to decide identity.
"""
from __future__ import annotations

from typing import Any

from .budget import RunBudget
from .claims import site_claims
from .config import TIER_SHELL
from .discovery import (
    STRICT,
    SearchProvider,
    discover,
    record_candidate_evidence,
)
from .envelope import build_envelope
from .evidence import EvidenceStore, make_claim
from .extract.contact import page_signals
from .extract.structured import parse_structured
from .identity import assess_identity, registrable_domain
from .ranker import model_info, rank
from .sources.brreg import (
    fetch_accounts,
    fetch_entity,
    fetch_group_structure,
    fetch_roles,
    fetch_subunits,
)
from .sources.news import fetch_news_activity
from .synthesis import synthesise
from .website import crawl_site

#: How many discovery candidates the identity gate will fetch, by spend tier.
IDENTITY_FETCH_CAP = {"shell": 0, "standard": 2, "rich": 3}


def _enrich_profile(profile: dict[str, Any], entity: dict[str, Any]) -> dict[str, Any]:
    """Fill identity/discovery inputs from the authoritative register entry."""
    enriched = dict(profile)
    if not enriched.get("name"):
        enriched["name"] = entity.get("navn")
    enriched["business_address"] = entity.get("forretningsadresse")
    if not enriched.get("website"):
        enriched["website"] = entity.get("hjemmeside")
    if not enriched.get("legal_form"):
        enriched["legal_form"] = (entity.get("organisasjonsform") or {}).get("kode")
    if enriched.get("employees") is None and entity.get("harRegistrertAntallAnsatte"):
        enriched["employees"] = entity.get("antallAnsatte")
    if "bankrupt" not in enriched:
        enriched["bankrupt"] = bool(entity.get("konkurs"))
    if not enriched.get("latest_submitted_accounts"):
        enriched["latest_submitted_accounts"] = entity.get("sisteInnsendteAarsregnskap")
    return enriched


def research_company(
    profile: dict[str, Any],
    *,
    fetcher: Any,
    budget: RunBudget,
    search: SearchProvider | None = None,
    known_domains: dict[str, str] | None = None,
    aggressiveness: str = STRICT,
) -> dict[str, Any]:
    """Resolve one company and return its terminal envelope as a dict."""
    org = str(profile.get("organisation_number") or "").strip()
    input_name = str(profile.get("name") or "")
    store = EvidenceStore()
    claims: list[dict[str, Any]] = []
    warnings: list[str] = []
    diagnostics: dict[str, Any] = {}

    # --- 1. Registry entity -------------------------------------------------
    entity, entity_claims = fetch_entity(fetcher, org, store)
    claims += entity_claims
    if entity is None:
        # Not in the register, or the fetch failed: the entity cannot be
        # resolved, so the run for this input is terminal-failed.
        diagnostics["spend_tier"] = budget.tier_of(org)
        envelope = build_envelope(
            organisation_number=org, input_name=input_name, claims=claims,
            store=store, identity=None, diagnostics=diagnostics,
            warnings=warnings, entity_resolved=False)
        return envelope.to_dict()

    enriched = _enrich_profile(profile, entity)
    spend_tier = budget.tier_of(org)
    diagnostics["spend_tier"] = spend_tier

    # --- 2. Registry enrichment --------------------------------------------
    # Accounts matter even for dormant holding entities (their accounts are the
    # whole story), so they run for every tier. Roles and subunits are only
    # worth their requests where there is an operating business.
    claims += fetch_accounts(fetcher, org, store)
    if spend_tier != TIER_SHELL:
        claims += fetch_roles(fetcher, org, store)
        claims += fetch_subunits(fetcher, org, store)
        if entity.get("erIKonsern"):
            group_info, group_claims = fetch_group_structure(fetcher, org, store)
            claims += group_claims
            if group_info and group_info.get("parent_name"):
                enriched["parent_name"] = group_info["parent_name"]
        claims += fetch_news_activity(
            fetcher, org, enriched.get("name") or input_name, store
        )

    # --- 3. Website discovery + identity gate ------------------------------
    identity_verdict = None
    discovery_info: dict[str, Any] | None = None
    fetch_cap = IDENTITY_FETCH_CAP.get(spend_tier, 2)

    if fetch_cap > 0:
        disc = discover(
            enriched, tier=aggressiveness, search=search,
            known_domains=known_domains)
        ranked = rank(enriched, disc.candidates)
        discovery_info = {
            "dns_probed": disc.dns_probed,
            "dns_resolved": disc.dns_resolved,
            "registry_url": disc.registry_url,
            "notes": disc.notes,
            "ranker": model_info(),
            "candidates": [{
                "url": rc.candidate.url,
                "origin": rc.candidate.origin,
                "probability": rc.probability,
                "scored_by": rc.scored_by,
            } for rc in ranked],
        }

        best = None
        best_response = None
        best_domain = ""
        fetched = 0
        for rc in ranked:
            if fetched >= fetch_cap:
                break
            candidate = rc.candidate
            record_candidate_evidence(store, candidate)
            response = fetcher.get(org, candidate.url)
            fetched += 1
            if response.error == "budget_exhausted":
                warnings.append("request budget exhausted during discovery")
                break
            if response.blocked_by_robots:
                continue
            if not response.ok or not response.is_html:
                continue
            source_url = response.final_url or candidate.url
            structured = parse_structured(response.text, source_url)
            names = structured.get("legal_names", []) + structured.get("names", [])
            signals = page_signals(
                response.text,
                hostname=registrable_domain(source_url),
                structured_names=names)
            verdict = assess_identity(enriched, signals, source_url=source_url)
            if best is None or verdict.score > best.score:
                best = verdict
                best_response = response
                best_domain = registrable_domain(source_url)
            if verdict.publishable:
                break

        identity_verdict = best
        diagnostics["identity_candidates_fetched"] = fetched

        # Crawl only an accepted own-site; an unproven page yields no on-site claims.
        if best is not None and best.publishable and best_response is not None:
            crawl = crawl_site(fetcher, org, best_domain, home=best_response)
            claims += site_claims(crawl, store, verified_url=f"https://{best_domain}")
            discovery_info["crawl"] = {
                "domain": best_domain,
                "pages_fetched": crawl.fetched,
                "pages_kept": len(crawl.pages),
                "skipped_robots": crawl.skipped_robots,
                "notes": crawl.notes,
            }
        else:
            # Emit honest availability states for external web surface
            claims.append(make_claim(
                field="website", value=None,
                availability="not_available" if not identity_verdict else "ambiguous",
                confidence=0.6 if not identity_verdict else 0.5,
                note="no verified company website found; candidates did not pass exact-identity gate"
            ))
            claims.append(make_claim(
                field="hiring_or_activity_signal", value=None,
                availability="not_applicable",
                confidence=1.0,
                note="no verified company website available to extract activity from"
            ))
            claims.append(make_claim(
                field="dated_public_activity", value=None,
                availability="not_applicable",
                confidence=1.0,
                note="no verified company website available to extract dated activity from"
            ))

    # --- 4. Envelope --------------------------------------------------------
    envelope = build_envelope(
        organisation_number=org, input_name=input_name, claims=claims,
        store=store, identity=identity_verdict, discovery=discovery_info,
        diagnostics=diagnostics, warnings=warnings, entity_resolved=True)

    # --- 5. Synthesis over the final claim states ---------------------------
    envelope.synthesis = synthesise(input_name, org, envelope.claims)
    return envelope.to_dict()
