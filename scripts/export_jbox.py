#!/usr/bin/env python3
"""Export verified Signalpost envelopes into an optimized JSON payload for JBOX UI."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

def format_nok(val: float | int | None) -> str:
    if val is None:
        return "N/A"
    abs_val = abs(val)
    sign = "-" if val < 0 else ""
    if abs_val >= 1_000_000_000:
        return f"{sign}{abs_val / 1_000_000_000:.2f}B NOK"
    if abs_val >= 1_000_000:
        return f"{sign}{abs_val / 1_000_000:.2f}M NOK"
    if abs_val >= 1_000:
        return f"{sign}{abs_val / 1_000:.1f}k NOK"
    return f"{sign}{int(val)} NOK"

def build_jbox_company(env: dict[str, Any]) -> dict[str, Any]:
    org = str(env.get("organisation_number") or "")
    name = str(env.get("input_name") or "Ukjent Selskap")
    
    claims = {c.get("field"): c for c in env.get("claims", [])}
    
    # 1. Identity
    legal_form = claims.get("legal_form", {}).get("value") or "AS"
    status_val = claims.get("operating_status", {}).get("value") or {}
    is_bankrupt = bool(status_val.get("bankrupt"))
    is_liquidating = bool(status_val.get("under_liquidation") or status_val.get("liquidating"))
    status_label = "Konkurs" if is_bankrupt else ("Under avvikling" if is_liquidating else "Aktiv")
    
    addr_val = claims.get("registered_address", {}).get("value") or {}
    municipality = addr_val.get("postal_town") or addr_val.get("kommune") or "Norge"
    street = addr_val.get("street") or ""
    postal_code = addr_val.get("postal_code") or ""
    full_address = f"{street}, {postal_code} {municipality}".strip(", ")
    
    ind_val = claims.get("industry_code", {}).get("value") or {}
    industry_code = ind_val.get("code") or "Ukjent"
    industry_desc = ind_val.get("description") or "Næring ikke spesifisert"
    
    employees = claims.get("employees", {}).get("value")
    
    # 2. Website & Socials
    web_val = claims.get("website", {}).get("value")
    web_avail = claims.get("website", {}).get("availability") == "available"
    website_url = web_val if web_avail else None
    
    socials_val = claims.get("social_profiles", {}).get("value") or {}
    socials_avail = claims.get("social_profiles", {}).get("availability") == "available"
    socials = socials_val if socials_avail else {}
    
    # 3. Financials
    rev_val = claims.get("revenue", {}).get("value")
    rev_num = rev_val.get("amount") if isinstance(rev_val, dict) else rev_val
    res_val = claims.get("operating_result", {}).get("value")
    res_num = res_val.get("amount") if isinstance(res_val, dict) else res_val
    ass_val = claims.get("total_assets", {}).get("value")
    ass_num = ass_val.get("amount") if isinstance(ass_val, dict) else ass_val
    eq_val = claims.get("equity", {}).get("value")
    eq_num = eq_val.get("amount") if isinstance(eq_val, dict) else eq_val
    
    period = claims.get("revenue", {}).get("reporting_period") or claims.get("operating_result", {}).get("reporting_period") or {}
    filing_year = str(period.get("to") or "2025")[:4] if isinstance(period, dict) else "2025"
    has_financials = rev_num is not None or res_num is not None
    
    financials = {
        "revenue_nok": rev_num,
        "revenue_display": format_nok(rev_num),
        "result_nok": res_num,
        "result_display": format_nok(res_num),
        "assets_nok": ass_num,
        "assets_display": format_nok(ass_num),
        "equity_nok": eq_num,
        "equity_display": format_nok(eq_num),
        "filing_year": filing_year,
        "has_data": has_financials,
    }
    
    # 4. People & Roles
    leadership_claims = [c for c in env.get("claims", []) if c.get("field") == "leadership"]
    people = []
    for lc in leadership_claims:
        val = lc.get("value") or {}
        if isinstance(val, dict) and val.get("name"):
            role_label = val.get("role_title_no") or val.get("role") or "Rolle"
            people.append({"role": role_label, "name": val.get("name")})
            
    # 5. Locations / Workplaces
    loc_claims = [c for c in env.get("claims", []) if c.get("field") == "location"]
    locations = []
    for lc in loc_claims:
        val = lc.get("value") or {}
        if isinstance(val, dict):
            loc_name = val.get("name") or name
            loc_addr = val.get("address") or {}
            city = loc_addr.get("postal_town") or loc_addr.get("kommune") or municipality
            locations.append({"name": loc_name, "city": city, "orgnr": val.get("organisation_number") or org})
            
    # 6. Signals & External Intelligence
    hiring_avail = claims.get("hiring_or_activity_signal", {}).get("availability") == "available"
    hiring_text = claims.get("hiring_or_activity_signal", {}).get("value") if hiring_avail else None
    
    news_avail = claims.get("dated_public_activity", {}).get("availability") == "available"
    news_text = claims.get("dated_public_activity", {}).get("value") if news_avail else None

    # Google Places ratings & reviews
    places_avail = claims.get("ratings_and_reviews", {}).get("availability") == "available"
    places_data = claims.get("ratings_and_reviews", {}).get("value") if places_avail else None

    # YouTube buzz & engagement
    buzz_avail = claims.get("buzz_or_engagement", {}).get("availability") == "available"
    buzz_data = claims.get("buzz_or_engagement", {}).get("value") if buzz_avail else None

    # NAV Job vacancies
    jobs_avail = claims.get("active_job_count", {}).get("availability") == "available"
    job_count = claims.get("active_job_count", {}).get("value") if jobs_avail else None

    # Qualified Norwegian News Sentiment
    sent_avail = claims.get("qualified_sentiment", {}).get("availability") == "available"
    sentiment_data = claims.get("qualified_sentiment", {}).get("value") if sent_avail else None

    # Coverage score (1 to 6 areas)
    areas = [
        True, # Identity always verified
        has_financials,
        bool(people),
        web_avail,
        bool(locations) or bool(socials) or bool(news_text),
        bool(places_data) or bool(buzz_data) or bool(sentiment_data) or bool(job_count),
    ]
    coverage_score = sum(1 for a in areas if a)

    # Deterministic Q&A for "Ask Signalpost"
    qna = {
        "brief": f"{name} er et norsk {legal_form} registrert i {municipality} (Org.nr: {org}). "
                 f"Selskapet opererer innen '{industry_desc}' (NACE {industry_code}) og har status som {status_label.lower()}."
                 + (f" Registrert med {employees} ansatte." if employees else " Ingen ansatte registrert i Aa-registeret."),
        "financials": (f"For regnskapsåret {filing_year} rapporterte {name} en omsetning på {format_nok(rev_num)}, "
                       f"et driftsresultat på {format_nok(res_num)}, samlede eiendeler på {format_nok(ass_num)} "
                       f"og egenkapital på {format_nok(eq_num)}.") if has_financials else "Offisielle årsregnskap er ikke registrert eller påkrevd for denne foretaksformen.",
        "leadership": ("Nøkkelpersoner registrert i Foretaksregisteret: " + ", ".join(f"{p['role']}: {p['name']}" for p in people[:4])) if people else "Ingen styre- eller ledelsesroller registrert i Brønnøysund.",
        "digital_footprint": f"Verifisert nettside: {website_url if website_url else 'Ingen verifisert nettside funnet'}. "
                             + (f"Aktive sosiale profiler: {', '.join(f'{k.title()}' for k in socials.keys())}." if socials else "Ingen verifiserte sosiale profiler."),
        "external_intelligence": (
            (f"Google Places: {places_data.get('rating')}★ ({places_data.get('reviews_count')} anmeldelser). " if places_data else "")
            + (f"NAV stillingsannonser: {job_count} aktive. " if job_count else "")
            + (f"Sentiment: {sentiment_data.get('label')} basert på {sentiment_data.get('evaluated_items_count')} oppslag. " if sentiment_data else "")
            + (f"YouTube: aktiv kanal med ferske opplastinger. " if buzz_data else "")
        ).strip() or "Ingen eksterne aktivitetssignaler observert.",
    }

    return {
        "id": org,
        "orgnr": org,
        "name": name,
        "legal_form": legal_form,
        "status": status_label,
        "is_active": not (is_bankrupt or is_liquidating),
        "municipality": municipality,
        "address": full_address,
        "industry_code": industry_code,
        "industry_desc": industry_desc,
        "employees": employees,
        "website": website_url,
        "socials": socials,
        "financials": financials,
        "people": people,
        "locations": locations,
        "hiring": hiring_text,
        "active_jobs": job_count,
        "places": places_data,
        "buzz": buzz_data,
        "sentiment": sentiment_data,
        "news": news_text,
        "coverage_score": coverage_score,
        "qna": qna,
    }

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Export Signalpost envelopes to JBOX JSON format")
    parser.add_argument("--input", default="runs/eval-fresh-100/envelopes.jsonl", help="Input envelopes.jsonl")
    parser.add_argument("--output", default="data/jbox_companies.json", help="Output JSON path")
    args = parser.parse_args()

    in_path = Path(args.input)
    if not in_path.exists():
        # Fallback to test-500-random if fresh-100 is not present
        if Path("runs/test-500-random/envelopes.jsonl").exists():
            in_path = Path("runs/test-500-random/envelopes.jsonl")
        else:
            raise SystemExit(f"Input file not found: {in_path}")

    out_path = Path(args.output)
    
    companies = []
    with in_path.open(encoding="utf-8") as f:
        for line in f:
            if not line.strip(): continue
            env = json.loads(line)
            companies.append(build_jbox_company(env))
            
    # Sort companies so the richest profiles (highest coverage) appear first
    companies.sort(key=lambda c: (c["coverage_score"], bool(c["website"]), bool(c["financials"]["has_data"])), reverse=True)
    
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(companies, f, ensure_ascii=False, indent=2)
        
    print(f"Exported {len(companies)} companies from {in_path} to {out_path} ({out_path.stat().st_size / 1024:.1f} KB)")
    
    # Also export top 100 for a ultra-fast instant demo bundle
    demo_path = out_path.parent / "jbox_top100.json"
    with demo_path.open("w", encoding="utf-8") as f:
        json.dump(companies[:100], f, ensure_ascii=False, indent=2)
    print(f"Exported top 100 showcase companies to {demo_path} ({demo_path.stat().st_size / 1024:.1f} KB)")

if __name__ == "__main__":
    main()
