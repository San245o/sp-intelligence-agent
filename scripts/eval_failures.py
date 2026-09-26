#!/usr/bin/env python3
"""Evaluate 100-company batch run and isolate failure patterns.

Compares our agent's terminal envelopes against the ground truth manifest
and the competition starter kit envelopes. Isolates any companies where our code
failed or did not resolve, and groups them into failure pattern clusters.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def get_claim(claims: list[dict[str, Any]], field: str) -> dict[str, Any] | None:
    for c in claims:
        if c.get("field") == field:
            return c
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=Path("data/eval-manifest-100.jsonl"))
    parser.add_argument("--agent-envelopes", type=Path, default=Path("runs/eval-100/envelopes.jsonl"))
    parser.add_argument("--comp-envelopes", type=Path, default=Path("../signalpost-starter-kit/out/eval-100-envelopes.jsonl"))
    parser.add_argument("--output", type=Path, default=Path("runs/eval-100/failure-analysis.json"))
    args = parser.parse_args()

    manifest_rows = load_jsonl(args.manifest)
    agent_rows = load_jsonl(args.agent_envelopes)
    comp_rows = load_jsonl(args.comp_envelopes)

    manifest_by_org = {r["organisation_number"]: r for r in manifest_rows}
    agent_by_org = {r["organisation_number"]: r for r in agent_rows}
    comp_by_org = {r["organisation_number"]: r for r in comp_rows}

    print(f"Loaded: Manifest={len(manifest_rows)}, Agent={len(agent_rows)}, Comp={len(comp_rows)}")

    failures = []
    website_misses = []
    pattern_clusters = defaultdict(list)

    total_companies = len(manifest_rows)
    agent_dispositions = Counter()
    agent_website_availabilities = Counter()

    for org, m_row in manifest_by_org.items():
        a_row = agent_by_org.get(org)
        c_row = comp_by_org.get(org)

        if not a_row:
            failures.append({
                "organisation_number": org,
                "name": m_row.get("name"),
                "reason": "Missing completely from agent output",
                "cluster": "system_error"
            })
            pattern_clusters["system_error"].append(org)
            continue

        dispo = a_row.get("disposition", "unknown")
        agent_dispositions[dispo] += 1

        claims = a_row.get("claims", [])
        web_claim = get_claim(claims, "website")
        web_avail = web_claim.get("availability") if web_claim else "not_available"
        agent_website_availabilities[web_avail] += 1

        registry_web = (m_row.get("website") or "").strip()
        comp_web_ev = (c_row.get("evidence", {}).get("website", {}) if c_row else {})
        comp_web_status = comp_web_ev.get("status")

        # 1. Check for fatal execution errors
        if dispo == "failed" or a_row.get("error"):
            failures.append({
                "organisation_number": org,
                "name": m_row.get("name"),
                "error": a_row.get("error", "disposition=failed"),
                "cluster": "fatal_execution_error",
                "diagnostics": a_row.get("diagnostics")
            })
            pattern_clusters["fatal_execution_error"].append(org)
            continue

        # 2. Check for website resolution failures where registry or comp had a website
        has_verified_web = (web_avail == "available")
        discovery_info = a_row.get("discovery") or {}
        candidates = discovery_info.get("candidates", [])
        dns_resolved = discovery_info.get("dns_resolved", [])

        if not has_verified_web:
            # Categorize why website was not verified
            cluster = "unknown"
            explanation = ""

            if not registry_web and not dns_resolved:
                # No website registered, no generated domain resolved
                cluster = "shell_holding_no_dns"
                explanation = "Holding/shell entity with no registered website and no live domain matching company name."
            elif not registry_web and dns_resolved:
                # DNS resolved a candidate, but identity gate rejected it
                cluster = "identity_gate_rejected_unregistered"
                explanation = f"DNS resolved {dns_resolved}, but identity gate safely rejected it to prevent wrong-company hallucination."
            elif registry_web and not dns_resolved:
                # Registry listed a domain, but DNS failed or timed out
                cluster = "dead_registry_domain"
                explanation = f"Registry listed {registry_web}, but DNS/HTTP resolution failed (dead/parked domain)."
            elif registry_web and dns_resolved:
                # Registry listed domain or candidates resolved, but identity gate rejected
                cluster = "identity_gate_rejected_registered"
                explanation = f"Registry listed {registry_web} and DNS resolved, but identity gate score was below publish threshold."

            # Check if workplace sports club
            name = m_row.get("name", "")
            if any(tok in name.upper() for tok in ["B.I.L", "B.I.L.", "BEDRIFTSIDRETTSLAG"]):
                cluster = "workplace_sports_club_quarantined"
                explanation = "Workplace sports club quarantined to prevent attributing parent corporate domain."

            website_misses.append({
                "organisation_number": org,
                "name": name,
                "legal_form": m_row.get("legal_form"),
                "employees": m_row.get("employees"),
                "registry_website": registry_web,
                "agent_website_availability": web_avail,
                "comp_website_status": comp_web_status,
                "dns_resolved": dns_resolved,
                "candidate_count": len(candidates),
                "cluster": cluster,
                "explanation": explanation
            })
            pattern_clusters[cluster].append(org)

    report = {
        "summary": {
            "total_companies": total_companies,
            "agent_resolved": len(agent_rows),
            "competition_resolved": len(comp_rows),
            "fatal_failures_count": len(pattern_clusters["fatal_execution_error"]),
            "agent_dispositions": dict(agent_dispositions),
            "agent_website_availabilities": dict(agent_website_availabilities),
            "pattern_counts": {k: len(v) for k, v in pattern_clusters.items()},
        },
        "clusters": dict(pattern_clusters),
        "website_misses": website_misses,
        "fatal_failures": [f for f in failures if f.get("cluster") == "fatal_execution_error"]
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n" + "="*70)
    print("                 EVALUATION & FAILURE PATTERN REPORT")
    print("="*70)
    print(f"Total Companies Evaluated: {total_companies}")
    print(f"Agent Dispositions:        {dict(agent_dispositions)}")
    print(f"Agent Website Coverage:    {dict(agent_website_availabilities)}")
    print(f"Fatal Code Errors:         {len(pattern_clusters['fatal_execution_error'])}")
    print("\nPattern Clusters (Where Website Was Not Published):")
    for cluster_name, org_list in sorted(pattern_clusters.items(), key=lambda x: len(x[1]), reverse=True):
        print(f"  * {cluster_name:<38s}: {len(org_list):2d} companies")

    print("\n" + "="*70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
