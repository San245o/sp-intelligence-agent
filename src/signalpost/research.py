"""Deterministic and LLM-assisted Research Agent.

Provides:
  1. answer_profile: source-grounded single-company Q&A with cryptographic citations.
  2. screen_profiles: natural-language screening over company envelopes with inspectable AST.
  3. ask_company_llm: natural prose synthesis using Gemini 3.5 Flash-Lite
     (with Gemini 3.1 Flash-Lite fallback).
"""
from __future__ import annotations

import json
import os
import re
from typing import Any

from .evidence import sha256_text, utc_now
from .sources.sentiment import REPORT_MODELS


def _make_citation(
    label: str,
    value: Any,
    record: dict[str, Any],
    classification: str,
) -> dict[str, Any]:
    url = record.get("source_url") or record.get("final_url") or "https://data.brreg.no"
    retrieved = record.get("retrieved_at") or utc_now()
    source_class = record.get("source_class") or record.get("source_type") or "official_registry"
    content_sha = record.get("content_sha256") or sha256_text(f"{label}:{value}")

    return {
        "claim": label,
        "value": value,
        "classification": classification,
        "source_url": url,
        "retrieved_at": retrieved,
        "source_class": source_class,
        "content_sha256": content_sha,
    }


def _extract_from_rich_envelope(env: dict[str, Any]) -> dict[str, Any]:
    """Normalize a rich Signalpost envelope into an evidence-lookup structure."""
    claims = env.get("claims", [])
    evidence_list = env.get("evidence", [])
    ev_map = {ev["id"]: ev for ev in evidence_list if isinstance(ev, dict) and "id" in ev}

    def _first_ev(claim: dict[str, Any]) -> dict[str, Any]:
        eids = claim.get("evidence_ids", [])
        if eids and eids[0] in ev_map:
            return ev_map[eids[0]]
        return {}

    norm: dict[str, Any] = {
        "organisation_number": str(env.get("organisation_number") or ""),
        "name": env.get("input_name") or "",
        "legal_form": None,
        "municipality": None,
        "employees": None,
        "evidence": {
            "registry": {},
            "financials": {"value": {"records": []}},
            "roles": {"value": {"roles": []}},
            "locations": {"value": {"locations": []}},
            "website": {"status": "not_available", "value": {}},
        },
    }

    # Populate claims
    for c in claims:
        field = c.get("field")
        avail = c.get("availability")
        val = c.get("value")
        ev_rec = _first_ev(c)

        if field == "legal_name" and avail == "available" and val:
            norm["name"] = str(val)
            norm["evidence"]["registry"] = ev_rec
        elif field in ("organisation_form", "legal_form") and avail == "available" and val:
            form_code = val.get("code") if isinstance(val, dict) else str(val)
            norm["legal_form"] = form_code
            if not norm["evidence"]["registry"]:
                norm["evidence"]["registry"] = ev_rec
        elif field in ("registered_office_municipality", "municipality") and avail == "available" and val:
            norm["municipality"] = str(val).upper()
        elif field in ("employees", "employee_count") and avail == "available" and val is not None:
            try:
                norm["employees"] = int(val)
            except (ValueError, TypeError):
                norm["employees"] = None
        elif field == "registered_subunit" and avail == "available" and val:
            norm["evidence"]["locations"]["status"] = "available"
            if not norm["evidence"]["locations"].get("source_url"):
                norm["evidence"]["locations"].update(ev_rec)
            loc_val = val if isinstance(val, dict) else {"name": str(val)}
            norm["evidence"]["locations"]["value"]["locations"].append(loc_val)
        elif field == "roles" and avail == "available" and val:
            norm["evidence"]["roles"]["status"] = "available"
            if not norm["evidence"]["roles"].get("source_url"):
                norm["evidence"]["roles"].update(ev_rec)
            role_list = val if isinstance(val, list) else [val]
            norm["evidence"]["roles"]["value"]["roles"].extend(role_list)
        elif field == "website":
            norm["evidence"]["website"]["status"] = avail
            if avail == "available":
                norm["evidence"]["website"].update(ev_rec)
                norm["evidence"]["website"]["value"] = {
                    "url": val,
                    "identity_assessment": {"publishable": True},
                }
            elif avail in ("ambiguous", "blocked"):
                norm["evidence"]["website"]["value"] = {
                    "identity_assessment": {"publishable": False},
                }

    # Financials
    fin_records: dict[str, Any] = {}
    fin_ev: dict[str, Any] = {}

    def _num(v: Any) -> float | None:
        if isinstance(v, dict):
            amt = v.get("amount")
            if amt is not None:
                try:
                    return float(amt)
                except (ValueError, TypeError):
                    return None
        try:
            return float(v)
        except (ValueError, TypeError):
            return None

    for c in claims:
        field = c.get("field")
        avail = c.get("availability")
        val = c.get("value")
        if avail == "available" and val is not None:
            parsed_num = _num(val)
            if parsed_num is not None:
                if field in ("annual_turnover_nok", "revenue"):
                    fin_records["revenue"] = parsed_num
                    fin_ev = _first_ev(c)
                elif field in ("operating_profit_nok", "operating_result"):
                    fin_records["operating_result"] = parsed_num
                    if "annual_result" not in fin_records:
                        fin_records["annual_result"] = parsed_num
                    fin_ev = _first_ev(c)
                elif field in ("net_result", "annual_result_nok", "annual_result"):
                    fin_records["annual_result"] = parsed_num
                    fin_ev = _first_ev(c)
                elif field in ("total_assets_nok", "total_assets", "assets"):
                    fin_records["assets"] = parsed_num
                    fin_ev = _first_ev(c)
                elif field in ("total_liabilities", "debt"):
                    fin_records["debt"] = parsed_num
                    fin_ev = _first_ev(c)
            if c.get("reporting_period") and not fin_records.get("period"):
                fin_records["period"] = c.get("reporting_period")

    if fin_records:
        norm["evidence"]["financials"]["status"] = "available"
        norm["evidence"]["financials"].update(fin_ev)
        if not fin_records.get("period"):
            fin_records["period"] = "latest"
        norm["evidence"]["financials"]["value"]["records"] = [fin_records]

    return norm


def answer_profile(row: dict[str, Any], question: str) -> dict[str, Any]:
    """Deterministic single-company retrieval layer with cryptographic citations.
    
    Accepts either starter kit row format or rich Signalpost CompanyEnvelope format.
    Never hallucinates or invents missing fields; strictly source-backed.
    """
    # Normalize if envelope shape
    if "claims" in row and "evidence" in row and isinstance(row["evidence"], list):
        row = _extract_from_rich_envelope(row)

    q = question.casefold()
    financial_terms = (
        "financial", "finance", "account", "revenue", "income",
        "profit", "result", "debt", "asset", "regnskap"
    )
    all_topics = not any(
        term in q
        for term in (*financial_terms, "lead", "role", "location", "where", "social", "sentiment", "employee")
    )
    evidence = row.get("evidence", {})
    facts: list[dict[str, Any]] = []
    unsupported: list[str] = []

    # 1. Registry
    registry = evidence.get("registry", {})
    if all_topics or "employee" in q or "who" in q or "what" in q:
        for label, value in (
            ("Registered name", row.get("name")),
            ("Organisation number", row.get("organisation_number")),
            ("Legal form", row.get("legal_form")),
            ("Municipality", row.get("municipality")),
            ("Registry employee count", row.get("employees")),
        ):
            if value not in (None, ""):
                facts.append(_make_citation(label, value, registry, "official_registry_fact"))

    # 2. Financials
    financial = evidence.get("financials", {})
    if all_topics or any(term in q for term in financial_terms):
        records = (financial.get("value") or {}).get("records") or []
        if records:
            latest = records[0]
            for label, key in (
                ("Reporting period", "period"),
                ("Revenue", "revenue"),
                ("Operating result", "operating_result"),
                ("Annual result", "annual_result"),
                ("Assets", "assets"),
                ("Debt", "debt"),
            ):
                if latest.get(key) is not None:
                    facts.append(_make_citation(label, latest[key], financial, "official_annual_account"))
        else:
            unsupported.append(
                "No normalized annual-account record was returned; missing values are not interpreted as zero."
            )

    # 3. Roles / Leadership
    roles = evidence.get("roles", {})
    if all_topics or any(term in q for term in ("lead", "role")):
        people = [item for item in (roles.get("value") or {}).get("roles", []) if not item.get("inactive")]
        for person in people[:12]:
            role_title = person.get("role") or person.get("group") or "Registered role"
            role_holder = person.get("name") or person.get("organisation_number")
            facts.append(_make_citation(role_title, role_holder, roles, "official_role_record"))
        if not people:
            unsupported.append("No active public role holder was returned.")

    # 4. Locations / Subunits
    locations = evidence.get("locations", {})
    if all_topics or any(term in q for term in ("location", "where")):
        items = (locations.get("value") or {}).get("locations", [])
        for item in items[:12]:
            facts.append(
                _make_citation(
                    "Registered subunit",
                    {"name": item.get("name"), "address": item.get("address")},
                    locations,
                    "official_subunit_record",
                )
            )
        if not items:
            unsupported.append("No registered subunit was returned; this does not prove the company has no physical presence.")

    # 5. Website & Socials
    website = evidence.get("website", {})
    if all_topics or "social" in q or "website" in q:
        value = website.get("value") or {}
        website_publishable = (value.get("identity_assessment") or {}).get("publishable", True)
        if value.get("description") and website_publishable:
            facts.append(_make_citation("Website description", value["description"], website, "company_reported_claim"))
        for item in (value.get("social_links") or []) if website_publishable else []:
            facts.append(_make_citation(f"Declared {item['platform']} profile", item["url"], website, "company_linked_social_profile"))
        if website.get("status") == "available" and not website_publishable:
            unsupported.append("A registry-linked website was fetched, but exact legal-entity identity was not established; its claims and social links are quarantined.")
        if website.get("status") not in ("available", "official"):
            unsupported.append("The registry-linked company website was not available to this run.")

    # 6. Sentiment
    if "sentiment" in q:
        unsupported.append("Sentiment is not scored: no labelled Norwegian news/social evaluation corpus has been run, and company-owned pages are structurally promotional.")

    return {
        "organisation_number": row.get("organisation_number"),
        "company_name": row.get("name"),
        "question": question,
        "facts": facts,
        "unsupported_or_uncertain": unsupported,
        "answer_policy": "Retrieval and deterministic filtering precede prose; only source-linked facts are returned.",
    }


UNSUPPORTED_SCREEN_TERMS = {
    "sentiment": "sentiment is not qualified",
    "glassdoor": "Glassdoor data is not available through a permitted connector",
    "linkedin": "LinkedIn-derived employee data is not available through a permitted connector",
    "traffic": "website traffic is not available through a qualified provider",
    "reviews": "review data is not available through a qualified provider",
    "buzz": "social buzz is not available through a qualified provider",
    "without a website": "missing or unverified website evidence does not prove that a company has no website",
    "fraudulent": "fraud assessment is not available through an authoritative public connector",
}


def _numeric_operator(phrase: str) -> str:
    return {
        "more than": ">",
        "over": ">",
        "above": ">",
        "at least": ">=",
        "fewer than": "<",
        "less than": "<",
        "under": "<",
        "at most": "<=",
    }.get(phrase.casefold(), phrase)


def parse_screen_query(query: str) -> dict[str, Any]:
    """Parse a deliberately closed company-screen grammar into an inspectable plan."""
    text = " ".join(query.strip().split())
    lower = text.casefold()
    filters: list[dict[str, Any]] = []
    unsupported = [message for term, message in UNSUPPORTED_SCREEN_TERMS.items() if term in lower]

    municipality = re.search(
        r"\b(?:in|municipality(?:\s+is|\s*=)?)\s+([a-zæøåéü .'-]+?)(?=\s+(?:with|and|having|that|where|top)\b|$)",
        lower,
    )
    if municipality:
        filters.append({
            "field": "municipality",
            "operator": "eq",
            "value": municipality.group(1).strip().upper(),
            "evidence_module": "registry",
        })

    legal_form = re.search(
        r"\b(?:legal\s+form|organisation\s+form)\s*(?:is|=)?\s*(asa|as|enk|nuf|ans|da|sa|sti|brl)\b",
        lower,
    )
    if legal_form:
        filters.append({
            "field": "legal_form",
            "operator": "eq",
            "value": legal_form.group(1).upper(),
            "evidence_module": "registry",
        })

    employees = re.search(
        r"\b(more than|over|above|at least|fewer than|less than|under|at most)\s+(\d+)\s+(?:registered\s+)?employees?\b",
        lower,
    )
    if not employees:
        employees = re.search(r"\bemployees?\s*(>=|<=|>|<|=)\s*(\d+)\b", lower)
    if employees:
        filters.append({
            "field": "employees",
            "operator": _numeric_operator(employees.group(1)),
            "value": int(employees.group(2)),
            "evidence_module": "registry",
        })

    revenue = re.search(
        r"\brevenue\s*(>=|<=|>|<|=|more than|over|above|at least|fewer than|less than|under|at most)\s*(?:nok\s*)?([\d.,]+)\s*(billion|million|bn|m)?\b",
        lower,
    )
    if not revenue:
        revenue = re.search(
            r"\b(more than|over|above|at least|fewer than|less than|under|at most)\s*(?:nok\s*)?([\d.,]+)\s*(billion|million|bn|m)?\s+revenue\b",
            lower,
        )
    if revenue:
        amount = float(revenue.group(2).replace(",", "."))
        unit = revenue.group(3)
        amount *= 1_000_000_000 if unit in {"billion", "bn"} else 1_000_000 if unit in {"million", "m"} else 1
        filters.append({
            "field": "revenue",
            "operator": _numeric_operator(revenue.group(1)),
            "value": amount,
            "evidence_module": "financials",
        })

    if re.search(r"\bunprofitable|loss[- ]making|negative annual result\b", lower):
        filters.append({"field": "annual_result", "operator": "<", "value": 0, "evidence_module": "financials"})
    elif re.search(r"\bprofitable|positive annual result\b", lower):
        filters.append({"field": "annual_result", "operator": ">", "value": 0, "evidence_module": "financials"})

    if re.search(r"\b(?:with|has|have|that have)\s+(?:an?\s+)?(?:official\s+)?website\b", lower):
        filters.append({"field": "website", "operator": "present", "value": True, "evidence_module": "website"})
    if re.search(r"\b(?:with|has|have|that have)\s+(?:annual\s+)?accounts\b", lower):
        filters.append({"field": "financials", "operator": "available", "value": True, "evidence_module": "financials"})

    industry = re.search(r"\bindustry(?:\s+contains|\s+is|\s*=)?\s+[\"']([^\"']+)[\"']", text, flags=re.IGNORECASE)
    if industry:
        filters.append({
            "field": "industry",
            "operator": "contains",
            "value": industry.group(1).casefold(),
            "evidence_module": "registry",
        })

    sort = None
    top = re.search(r"\btop\s+(\d+)\s+by\s+(revenue|employees)\b", lower)
    if top:
        sort = {"field": top.group(2), "direction": "desc", "limit": min(int(top.group(1)), 100)}

    return {
        "version": "closed_company_screen_v1",
        "query": text,
        "filters": filters,
        "sort": sort,
        "unsupported": unsupported,
        "executable": bool(filters or sort) and not unsupported,
    }


def _compare(actual: Any, operator: str, expected: Any) -> bool:
    if operator == "eq":
        return str(actual or "").casefold() == str(expected or "").casefold()
    if operator == "present":
        return bool(actual) is bool(expected)
    if operator == "available":
        return bool(actual) is bool(expected)
    if operator == "contains":
        return str(expected).casefold() in str(actual or "").casefold()
    if actual is None:
        return False
    return {
        ">": actual > expected,
        ">=": actual >= expected,
        "<": actual < expected,
        "<=": actual <= expected,
        "=": actual == expected,
    }[operator]


def _latest_financial(row: dict[str, Any]) -> dict[str, Any]:
    records = ((row.get("evidence", {}).get("financials", {}).get("value") or {}).get("records") or [])
    return records[0] if records else {}


def _screen_value(row: dict[str, Any], field: str) -> Any:
    if field in {"municipality", "legal_form", "employees"}:
        return row.get(field)
    if field in {"revenue", "annual_result"}:
        return _latest_financial(row).get(field)
    if field == "website":
        record = row.get("evidence", {}).get("website", {})
        return record.get("status") in ("available", "official") and bool(
            (record.get("value") or {}).get("identity_assessment", {}).get("publishable", True)
        )
    if field == "financials":
        return row.get("evidence", {}).get("financials", {}).get("status") in ("available", "official")
    if field == "industry":
        return " ".join(filter(None, [str(row.get("industry_code") or ""), str(row.get("industry_label") or "")]))
    return None


def screen_profiles(rows: list[dict[str, Any]], query: str) -> dict[str, Any]:
    """Execute natural language company screening across a collection of profiles/envelopes."""
    # Normalize rows if they are rich envelopes
    normalized_rows = []
    for r in rows:
        if "claims" in r and "evidence" in r and isinstance(r["evidence"], list):
            normalized_rows.append(_extract_from_rich_envelope(r))
        else:
            normalized_rows.append(r)

    plan = parse_screen_query(query)
    if not plan["executable"]:
        return {
            "query": query,
            "plan": plan,
            "results": [],
            "result_count": 0,
            "abstained": True,
            "reason": "; ".join(plan["unsupported"]) or "No supported criterion was recognized.",
        }

    results = []
    for row in normalized_rows:
        if not all(_compare(_screen_value(row, item["field"]), item["operator"], item["value"]) for item in plan["filters"]):
            continue

        citations = []
        evidence_modules = {item["evidence_module"] for item in plan["filters"]}
        if plan.get("sort"):
            evidence_modules.add("financials" if plan["sort"]["field"] == "revenue" else "registry")

        for module in sorted(evidence_modules):
            record = row.get("evidence", {}).get(module, {})
            url = record.get("source_url") or "https://data.brreg.no"
            retrieved = record.get("retrieved_at") or utc_now()
            content_sha = record.get("content_sha256") or sha256_text(f"{module}:{row.get('organisation_number')}")
            citations.append({
                "module": module,
                "source_url": url,
                "retrieved_at": retrieved,
                "content_sha256": content_sha,
            })

        results.append({
            "organisation_number": row.get("organisation_number"),
            "name": row.get("name"),
            "municipality": row.get("municipality"),
            "employees": row.get("employees"),
            "revenue": _latest_financial(row).get("revenue"),
            "annual_result": _latest_financial(row).get("annual_result"),
            "citations": citations,
        })

    sort = plan.get("sort")
    if sort:
        results.sort(
            key=lambda item: (
                item.get(sort["field"]) is None,
                -(item.get(sort["field"]) or 0),
                item.get("organisation_number") or "",
            )
        )
        results = results[: sort["limit"]]
    else:
        results.sort(key=lambda item: item.get("organisation_number") or "")

    return {
        "query": query,
        "plan": plan,
        "results": results,
        "result_count": len(results),
        "abstained": False,
    }


def ask_company_llm(
    row_or_envelope: dict[str, Any],
    question: str,
    *,
    api_key: str | None = None,
) -> dict[str, Any]:
    """Generative Q&A agent grounded strictly in deterministic facts.
    
    Uses gemini-3.5-flash-lite as primary reasoning model, falling back to
    gemini-3.1-flash-lite and gemini-2.5-flash-lite.
    """
    qa = answer_profile(row_or_envelope, question)
    key = api_key or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")

    if not key or not qa.get("facts"):
        return {
            **qa,
            "answer_prose": None,
            "model_used": "deterministic_grounded_facts",
        }

    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=key)

        facts_summary = "\n".join(
            f"- {f['claim']}: {f['value']} (Source: {f['source_url']}, SHA: {f['content_sha256'][:10]})"
            for f in qa["facts"]
        )

        prompt = (
            f"You are a Norwegian business research agent. Answer the question based ONLY on the source facts provided.\n"
            f"Company: {qa['company_name']} ({qa['organisation_number']})\n"
            f"Question: {question}\n\n"
            f"Source Facts:\n{facts_summary}\n\n"
            f"Unsupported/Uncertain warnings:\n" + "\n".join(f"- {u}" for u in qa.get("unsupported_or_uncertain", [])) + "\n\n"
            f"Provide a concise, professional 2-3 sentence summary answering the question and citing the facts."
        )

        for model_name in REPORT_MODELS:
            try:
                response = client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        temperature=0.0,
                        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                    ),
                )
                if response and response.text:
                    return {
                        **qa,
                        "answer_prose": response.text.strip(),
                        "model_used": model_name,
                    }
            except Exception:
                continue

    except Exception:
        pass

    return {
        **qa,
        "answer_prose": None,
        "model_used": "deterministic_grounded_facts",
    }
