"""Enrich existing exact-company records without repeating registry/site acquisition."""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]
from norway_company_agent.external_collectors import enrich_external, coverage_summary
from norway_company_agent.external_footprint import aggregate_footprint
from norway_company_agent.search_providers import configure_search_budget, set_search_company, provider_summary, plan_search_budget
from norway_company_agent.research import summarize_company
from norway_company_agent import network_meter
from scripts.run_competition_batch import WebRequestBudget, CompanyWebRequestBudget, write_jsonl


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--profiles", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--paid-search-budget", type=int, default=None)
    parser.add_argument("--paid-search-per-company", type=int, default=2)
    parser.add_argument("--public-request-budget", type=int, default=800)
    parser.add_argument("--requests-per-company", type=int, default=18)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    rows = [json.loads(x) for x in args.profiles.read_text(encoding="utf-8").splitlines() if x]
    configure_search_budget(args.paid_search_budget if args.paid_search_budget is not None else max(1, len(rows) // 2), args.paid_search_per_company)
    network_meter.activate(args.public_request_budget)
    if len({r["organisation_number"] for r in rows}) != len(rows):
        parser.error("Duplicate input organisation numbers")
    plan_search_budget(rows, args.paid_search_budget if args.paid_search_budget is not None else max(1, len(rows) // 2), args.paid_search_per_company)
    started = datetime.now(timezone.utc).isoformat()
    budget = WebRequestBudget(args.public_request_budget)
    def enrich(row):
        set_search_company(row["organisation_number"])
        before = coverage_summary((row.get("evidence", {}).get("external_footprint", {}).get("value") or {}).get("observations") or [])
        new, metrics = enrich_external(row, CompanyWebRequestBudget(budget, args.requests_per_company), max_requests=args.requests_per_company)
        record = row.setdefault("evidence", {}).setdefault("external_footprint", {})
        value = record.setdefault("value", {})
        combined = list({o["id"]: o for o in [*(value.get("observations") or []), *new]}.values())
        value.update(observations=combined, accepted_observations=len(combined), platforms=sorted({o["platform"] for o in combined}))
        record.update(field="external_footprint", status="available" if combined else "not_found", source_type="typed_multi_source_external_footprint",
            source_class="typed_multi_source_external_footprint", source_url=combined[0]["source_url"] if combined else record.get("source_url", ""),
            retrieved_at=datetime.now(timezone.utc).isoformat())
        row["external_coverage"] = coverage_summary(combined)
        row["external_aggregate"] = aggregate_footprint(combined)
        row["external_enrichment_metrics"] = metrics
        row["summary"] = summarize_company(row)
        return row, before
    state, baselines, errors = {}, {}, []
    args.output_dir.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(enrich, row): row for row in rows}
        for index, future in enumerate(as_completed(futures), 1):
            original = futures[future]
            try:
                row, before = future.result()
                baselines[row["organisation_number"]] = before
            except Exception as exc:
                row = original
                errors.append({"organisation_number": row["organisation_number"], "error_type": type(exc).__name__})
            state[row["organisation_number"]] = row
            if index % 10 == 0:
                write_jsonl(args.output_dir / "profiles.jsonl", [state[r["organisation_number"]] for r in rows if r["organisation_number"] in state])
                print(f"{index}/{len(rows)} external profiles checkpointed", flush=True)
    ordered = [state[r["organisation_number"]] for r in rows]
    write_jsonl(args.output_dir / "profiles.jsonl", ordered)
    counts = Counter()
    for row in ordered:
        for key, count in (row.get("external_coverage") or {}).items():
            if isinstance(count, int) and count > 0: counts["companies_with_" + key] += 1; counts[key] += count
    report = {"started_at": started, "completed_at": datetime.now(timezone.utc).isoformat(), "companies": len(rows), "errors": errors,
        "search_providers": provider_summary(), "transport": network_meter.snapshot(), "coverage": dict(counts),
        "boundary": "Incremental enrichment of saved records; baseline registry/website retrieval times remain unchanged. This is not a fresh matched baseline comparison or an independent precision qualification.",
        "baseline_coverage": baselines}
    (args.output_dir / "enrichment-report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({k:v for k,v in report.items() if k != "baseline_coverage"}, indent=2))


if __name__ == "__main__":
    main()
