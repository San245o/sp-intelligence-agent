"""Batch runner: resolve a manifest of companies and emit terminal envelopes.

One shared `RunBudget` governs the whole run (2,000-request / $10 / 45-min
evaluator cap, with soft margins from `config`). Tiers and grants are allocated
up front from registry facts, then companies are researched across a thread pool
— unspent grant flows back to a shared reserve so richer companies can borrow it.

Outputs:
  * <out>/envelopes.jsonl   one terminal envelope per input line
  * <out>/run-report.json   budget ledger, tier mix, disposition counts, timing

Usage:
    python scripts/run_batch.py --manifest data/smoke-manifest.jsonl --out runs/smoke
    python scripts/run_batch.py --manifest ../company-universe.jsonl.gz --out runs/full
"""
from __future__ import annotations

import argparse
import gzip
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from signalpost.budget import RunBudget  # noqa: E402
from signalpost.config import (  # noqa: E402
    EVALUATOR_MAX_SPEND_USD,
    REQUEST_SOFT_CAP,
    RunConfig,
    WALL_CLOCK_SOFT_STOP_S,
)
from signalpost.discovery import build_search_provider  # noqa: E402
from signalpost.http import Fetcher  # noqa: E402
from signalpost.pipeline import research_company  # noqa: E402


def _load_manifest(path: Path) -> list[dict]:
    opener = gzip.open if path.suffix == ".gz" else open
    rows: list[dict] = []
    with opener(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--manifest", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--limit", type=int, default=0, help="cap inputs (0 = all)")
    ap.add_argument("--max-requests", type=int, default=None, help="override request cap")
    ap.add_argument("--wall-clock-s", type=int, default=None, help="override wall-clock cap (seconds)")
    ap.add_argument("--max-spend", type=float, default=None, help="override max spend (USD)")
    ap.add_argument("--search", default="none",
                    help="search provider: none | brave (needs BRAVE_SEARCH_API_KEY)")
    ap.add_argument("--aggressiveness", default="strict",
                    choices=["strict", "moderate", "aggressive"])
    ap.add_argument("--run-id", default=None)
    args = ap.parse_args()

    profiles = _load_manifest(args.manifest)
    if args.limit:
        profiles = profiles[: args.limit]
    if not profiles:
        print("manifest is empty", file=sys.stderr)
        return 2

    run_id = args.run_id or f"run-{int(time.time())}"
    scale = max(1.0, len(profiles) / 100.0)
    max_requests = args.max_requests or int(REQUEST_SOFT_CAP * scale)
    wall_clock_s = args.wall_clock_s or int(WALL_CLOCK_SOFT_STOP_S * scale)
    max_spend = args.max_spend or (EVALUATOR_MAX_SPEND_USD * scale)

    cfg = RunConfig(
        run_id=run_id,
        expected_count=len(profiles),
        max_requests=max_requests,
        max_spend_usd=max_spend,
        wall_clock_s=wall_clock_s,
        workers=args.workers,
        search_provider=args.search,
    )

    budget = RunBudget(
        max_requests=cfg.max_requests, max_spend_usd=cfg.max_spend_usd,
        wall_clock_s=cfg.wall_clock_s, tier_budgets=cfg.tier_budgets)
    budget.allocate(profiles)
    fetcher = Fetcher(budget, respect_robots=cfg.respect_robots)
    search = build_search_provider(args.search, fetcher)

    args.out.mkdir(parents=True, exist_ok=True)
    envelopes_path = args.out / "envelopes.jsonl"

    def _work(profile: dict) -> dict:
        org = str(profile.get("organisation_number") or "")
        try:
            env = research_company(
                profile, fetcher=fetcher, budget=budget, search=search,
                aggressiveness=args.aggressiveness)
        except Exception as exc:  # never let one company sink the batch
            env = {"organisation_number": org, "disposition": "failed",
                   "error": f"{type(exc).__name__}: {exc}"}
        finally:
            budget.release_unspent(org)
        return env

    started = time.time()
    results: list[dict] = []
    total = len(profiles)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(_work, p): p for p in profiles}
        for idx, fut in enumerate(as_completed(futures), 1):
            results.append(fut.result())
            if idx % 25 == 0 or idx == total:
                elapsed = time.time() - started
                print(
                    f"[{run_id}] {idx}/{total} ({idx/total*100:.1f}%) "
                    f"| reqs: {budget._requests}/{budget._max_requests} "
                    f"| elapsed: {elapsed:.1f}s",
                    flush=True,
                )
            if budget.time_exhausted:
                # Soft wall-clock hit: stop scheduling new work by draining.
                break

    # Preserve manifest order in the output for deterministic diffs.
    order = {str(p.get("organisation_number")): i for i, p in enumerate(profiles)}
    results.sort(key=lambda e: order.get(str(e.get("organisation_number")), 1 << 30))

    with envelopes_path.open("w", encoding="utf-8") as fh:
        for env in results:
            fh.write(json.dumps(env, ensure_ascii=False) + "\n")

    dispositions: dict[str, int] = {}
    for env in results:
        d = env.get("disposition", "unknown")
        dispositions[d] = dispositions.get(d, 0) + 1

    report = {
        "run_id": run_id,
        "config": cfg.to_dict(),
        "inputs": len(profiles),
        "resolved": len(results),
        "elapsed_s": round(time.time() - started, 1),
        "dispositions": dispositions,
        "budget": budget.report(),
    }
    (args.out / "run-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[{run_id}] {len(results)}/{len(profiles)} companies -> {envelopes_path}")
    print(f"dispositions: {dispositions}")
    print(f"requests: {report['budget']['requests_used']}/{report['budget']['requests_cap']}"
          f"  spend: ${report['budget']['third_party_cost_usd']}"
          f"  elapsed: {report['elapsed_s']}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
