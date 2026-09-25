"""Deterministic synthesis: a plain-language brief from published claims only.

The rubric rewards a readable synthesis, but the source policy is explicit that
a model "must not decide exact identity, invent a missing field, or silently
override deterministic evidence". So this is not a language model — it is a
template that restates claims that are already `available`, each sentence backed
by the same evidence ids the claims carry. It never introduces a fact that is
not in the claim set, and it cites the evidence it draws on.

If a fact was not found, it is simply omitted from the prose; the structured
claim list (with its `not_available` / `ambiguous` states) remains the complete,
auditable record. The synthesis is a convenience view over that record.
"""
from __future__ import annotations

from typing import Any


def _by_field(claims: list[dict[str, Any]], field: str) -> list[dict[str, Any]]:
    return [c for c in claims if c.get("field") == field
            and c.get("availability") == "available"]


def _first(claims: list[dict[str, Any]], field: str) -> dict[str, Any] | None:
    got = _by_field(claims, field)
    return got[0] if got else None


def _fmt_amount(value: Any) -> str | None:
    if not isinstance(value, dict) or value.get("amount") is None:
        return None
    amount = value["amount"]
    currency = value.get("currency") or "NOK"
    try:
        return f"{currency} {float(amount):,.0f}".replace(",", " ")
    except (TypeError, ValueError):
        return None


def synthesise(
    input_name: str,
    organisation_number: str,
    claims: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build a synthesis block: headline, sentences, and the evidence they use.

    Returns a dict with `headline`, `summary` (joined prose), `sentences`
    (each with its own evidence_ids), and `facts_used` (claim count). All drawn
    strictly from `available` claims.
    """
    sentences: list[dict[str, Any]] = []

    def say(text: str, sources: list[str]) -> None:
        sentences.append({"text": text, "evidence_ids": list(dict.fromkeys(sources))})

    name_claim = _first(claims, "legal_name")
    legal_name = (name_claim or {}).get("value") or input_name

    # 1. Identity + form + status.
    form_claim = _first(claims, "organisation_form")
    status_claim = _first(claims, "operating_status")
    form_desc = ""
    if form_claim and isinstance(form_claim["value"], dict):
        form_desc = form_claim["value"].get("description") or form_claim["value"].get("code") or ""
    status_word = "registered"
    status_src: list[str] = []
    if status_claim and isinstance(status_claim["value"], dict):
        flags = status_claim["value"]
        status_src = status_claim.get("evidence_ids", [])
        if flags.get("bankrupt"):
            status_word = "bankrupt"
        elif flags.get("under_liquidation") or flags.get("under_compulsory_liquidation"):
            status_word = "under liquidation"
        elif flags.get("active"):
            status_word = "active"
    lead_prefix = f"{legal_name} (org. no. {organisation_number})"
    noun = form_desc.lower() if form_desc else "company"
    if status_word == "under liquidation":
        # Reads naturally after the noun rather than before it.
        lead = f"{lead_prefix} is a {noun} under liquidation."
    else:
        phrase = f"{status_word} {noun}".strip()
        article = "an" if phrase[:1] in "aeiou" else "a"
        lead = f"{lead_prefix} is {article} {phrase}."
    say(lead, (name_claim or {}).get("evidence_ids", []) + status_src)

    # 2. Founding + registered seat.
    founded = _first(claims, "founded_at") or _first(claims, "registered_at")
    addr = _first(claims, "registered_address")
    parts: list[str] = []
    src: list[str] = []
    if founded:
        parts.append(f"registered {str(founded['value'])[:10]}")
        src += founded.get("evidence_ids", [])
    if addr and isinstance(addr["value"], dict):
        town = addr["value"].get("postal_town") or addr["value"].get("municipality")
        if town:
            parts.append(f"seated in {str(town).title()}")
            src += addr.get("evidence_ids", [])
    if parts:
        say("It is " + " and ".join(parts) + ".", src)

    # 3. Size.
    emp = _first(claims, "employees")
    if emp and emp.get("value") is not None:
        say(f"The register records {emp['value']} employees.",
            emp.get("evidence_ids", []))

    # 4. Industry.
    nace = _first(claims, "industry_code")
    if nace and isinstance(nace["value"], dict) and nace["value"].get("description"):
        say(f"Its primary registered activity is {nace['value']['description'].lower()} "
            f"(NACE {nace['value'].get('code')}).", nace.get("evidence_ids", []))

    # 5. Latest accounts.
    revenue = _first(claims, "revenue")
    result = _first(claims, "net_result")
    if revenue:
        period = revenue.get("reporting_period") or ""
        money = _fmt_amount(revenue["value"])
        fin = f"In {period}, revenue was {money}" if money else ""
        r_src = revenue.get("evidence_ids", [])
        net = _fmt_amount(result["value"]) if result else None
        if net:
            fin += f" and the net result {net}"
            r_src += result.get("evidence_ids", [])
        if fin:
            say(fin.strip() + ".", r_src)

    # 6. Leadership.
    leaders = _by_field(claims, "leadership")
    named = [c for c in leaders if isinstance(c.get("value"), dict)]
    if named:
        ceo = next((c for c in named if c["value"].get("role") == "chief_executive"), None)
        chair = next((c for c in named if c["value"].get("role") == "board_chair"), None)
        bits: list[str] = []
        lead_src: list[str] = []
        if ceo:
            bits.append(f"{ceo['value']['name']} ({ceo['value'].get('role_title_no', 'CEO')})")
            lead_src += ceo.get("evidence_ids", [])
        if chair:
            bits.append(f"{chair['value']['name']} (chair)")
            lead_src += chair.get("evidence_ids", [])
        if bits:
            say("Leadership includes " + " and ".join(bits) + ".", lead_src)
        else:
            say(f"The register lists {len(named)} current officers.",
                [e for c in named for e in c.get("evidence_ids", [])][:3])

    # 7. Verified website.
    site = _first(claims, "website")
    if site:
        say(f"Its verified website is {site['value']}.", site.get("evidence_ids", []))

    summary = " ".join(s["text"] for s in sentences)
    return {
        "headline": legal_name,
        "summary": summary,
        "sentences": sentences,
        "facts_used": len({e for s in sentences for e in s["evidence_ids"]}),
        "method": "deterministic_template_v1",
    }
