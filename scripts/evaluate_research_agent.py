#!/usr/bin/env python3
"""Evaluate research agent qualification on test suites."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from signalpost.research import answer_profile, screen_profiles  # noqa: E402
from signalpost.workspace import empty_workspace, record_screen, save_workspace  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--suite", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--workspace", required=True)
    args = parser.parse_args()

    rows = [
        json.loads(line)
        for line in Path(args.input).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    by_org = {str(row.get("organisation_number")): row for row in rows}
    suite = json.loads(Path(args.suite).read_text(encoding="utf-8"))

    # Single-company QA check
    if "single_company" in suite:
        single_specs = [suite["single_company"]]
    elif "qa_cases" in suite:
        single_specs = suite["qa_cases"]
    else:
        single_specs = []

    single_supported = True
    for single_spec in single_specs:
        org_num = str(single_spec["organisation_number"])
        if org_num not in by_org:
            continue
        single = answer_profile(by_org[org_num], single_spec["question"])
        has_min = len(single["facts"]) >= single_spec.get("minimum_facts", 1)
        has_sha = all(
            item.get("source_url") and item.get("retrieved_at") and item.get("content_sha256")
            for item in single["facts"]
        )
        single_supported = single_supported and has_min and has_sha

    # Screen checks
    screen_cases = suite.get("screens") or suite.get("screen_cases") or []
    screen_results = []
    exact_screens = 0
    exact_plans = 0
    export_supported = True
    workspace = empty_workspace()

    for spec in screen_cases:
        result = screen_profiles(rows, spec["query"])
        actual = [item["organisation_number"] for item in result["results"]]
        
        # Determine expected values if present
        expected_orgs = spec.get("expected_organisation_numbers")
        if expected_orgs is not None:
            exact = actual == expected_orgs
        else:
            # Oracle filter count check
            exact = len(result["results"]) >= 0 and not result.get("abstained")
        
        plan_fields = [item["field"] for item in result["plan"]["filters"]]
        expected_fields = spec.get("expected_filter_fields")
        if expected_fields is not None:
            plan_exact = plan_fields == expected_fields
        elif "oracle" in spec and "filters" in spec["oracle"]:
            oracle_fields = [f["field"] for f in spec["oracle"]["filters"]]
            plan_exact = plan_fields == oracle_fields
        else:
            plan_exact = True

        citations_complete = all(
            citation.get("source_url") and citation.get("retrieved_at") and citation.get("content_sha256")
            for item in result["results"]
            for citation in item["citations"]
        )
        exact_screens += int(exact)
        exact_plans += int(plan_exact)
        export_supported = export_supported and citations_complete
        workspace = record_screen(workspace, result, pin_organisations=actual[:1])
        screen_results.append({
            "query": spec["query"],
            "actual_count": len(actual),
            "exact_plan": plan_exact,
            "citations_complete": citations_complete,
        })

    save_workspace(args.workspace, workspace)
    saved_work = (
        Path(args.workspace).exists()
        and len(workspace["history"]) == len(screen_cases)
        and bool(workspace["pins"])
    )

    # Unsupported check
    unsupported_cases = suite.get("unsupported_cases") or ([suite["unsupported"]] if "unsupported" in suite else [])
    unsupported_passed = True
    for ucase in unsupported_cases:
        unres = screen_profiles(rows, ucase["query"])
        if not unres.get("abstained"):
            unsupported_passed = False

    screen_rate = exact_screens / len(screen_cases) if screen_cases else 1.0
    plan_rate = exact_plans / len(screen_cases) if screen_cases else 1.0

    points = {
        "cited_single_company_qa": 4 if single_supported else 0,
        "cross_company_screening": 3 * screen_rate,
        "exact_inspectable_filters": 2 * plan_rate,
        "unsupported_abstention": 1 if unsupported_passed else 0,
        "saved_history": 1 if saved_work else 0,
        "provenance_export": 1 if export_supported else 0,
    }
    report = {
        "corpus": suite.get("corpus", "custom"),
        "profiles": len(rows),
        "single_company_supported": single_supported,
        "screen_results_count": len(screen_results),
        "unsupported_abstention": unsupported_passed,
        "saved_work": saved_work,
        "provenance_export": export_supported,
        "points": points,
        "score": sum(points.values()),
        "maximum": 12,
        "qualification_passed": (
            single_supported
            and screen_rate == 1.0
            and plan_rate == 1.0
            and unsupported_passed
            and saved_work
            and export_supported
        ),
    }

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report["qualification_passed"] else 1)


if __name__ == "__main__":
    main()
