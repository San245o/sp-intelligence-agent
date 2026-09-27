"""Quick test runner: sample 50 random companies and run with 16 workers.

Usage:
    python scripts/test_random_50.py
    python scripts/test_random_50.py --workers 16 --seed 42 --out runs/test-50-random
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from signalpost.budget import RunBudget
from signalpost.config import RunConfig, REQUEST_SOFT_CAP, WALL_CLOCK_SOFT_STOP_S, EVALUATOR_MAX_SPEND_USD
from signalpost.discovery import build_search_provider
from signalpost.http import Fetcher
from signalpost.pipeline import research_company


def main():
    parser = argparse.ArgumentParser(description="Test agent on 50 random companies.")
    parser.add_argument(
        "--manifest",
        type=Path,
        default=ROOT / "data" / "submission-manifest-1000.jsonl",
        help="Path to manifest file",
    )
    parser.add_argument("--count", type=int, default=50, help="Number of random companies (default: 50)")
    parser.add_argument("--workers", type=int, default=16, help="Worker threads (default: 16)")
    parser.add_argument("--seed", type=int, default=None, help="Random seed for reproducibility")
    parser.add_argument(
        "--out",
        type=Path,
        default=ROOT / "runs" / "test-50-random",
        help="Output directory",
    )
    parser.add_argument("--aggressiveness", default="strict", choices=["strict", "moderate", "aggressive"])
    parser.add_argument("--search", default="auto")
    args = parser.parse_args()

    if not args.manifest.exists():
        print(f"Error: manifest {args.manifest} does not exist", file=sys.stderr)
        return 1

    all_companies = []
    with open(args.manifest, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                all_companies.append(json.loads(line))

    if args.seed is not None:
        random.seed(args.seed)

    sample_size = min(args.count, len(all_companies))
    sampled = random.sample(all_companies, sample_size)

    args.out.mkdir(parents=True, exist_ok=True)
    sample_manifest_path = args.out / "sample-manifest.jsonl"
    with open(sample_manifest_path, "w", encoding="utf-8") as f:
        for item in sampled:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")

    print(f"============================================================")
    print(f"  SIGNALPOST TEST RUN: {sample_size} Companies | {args.workers} Workers")
    print(f"  Manifest: {args.manifest.name} -> {sample_manifest_path.name}")
    print(f"  Output Dir: {args.out}")
    print(f"============================================================")

    scale = max(1.0, len(sampled) / 100.0)
    cfg = RunConfig(
        run_id="test-50-random",
        expected_count=len(sampled),
        max_requests=int(REQUEST_SOFT_CAP * scale),
        max_spend_usd=EVALUATOR_MAX_SPEND_USD * scale,
        wall_clock_s=int(WALL_CLOCK_SOFT_STOP_S * scale),
        workers=args.workers,
        search_provider=args.search,
    )

    budget = RunBudget(
        max_requests=cfg.max_requests,
        max_spend_usd=cfg.max_spend_usd,
        wall_clock_s=cfg.wall_clock_s,
        tier_budgets=cfg.tier_budgets,
    )
    budget.allocate(sampled)
    fetcher = Fetcher(budget, respect_robots=cfg.respect_robots)
    search = build_search_provider(args.search, fetcher)

    envelopes_path = args.out / "envelopes.jsonl"
    start_t = time.monotonic()
    done = 0
    discovered_sites = []

    def _process(profile: dict) -> dict:
        org = str(profile.get("organisation_number") or "")
        try:
            return research_company(
                profile,
                fetcher=fetcher,
                budget=budget,
                search=search,
                aggressiveness=args.aggressiveness,
            )
        except Exception as exc:
            return {
                "organisation_number": org,
                "input_name": profile.get("name", ""),
                "disposition": "failed",
                "error": f"{type(exc).__name__}: {exc}",
                "claims": [],
            }
        finally:
            budget.release_unspent(org)

    with open(envelopes_path, "w", encoding="utf-8") as out_f:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(_process, p): p for p in sampled}
            for fut in as_completed(futures):
                env = fut.result()
                out_f.write(json.dumps(env, ensure_ascii=False) + "\n")
                out_f.flush()
                done += 1

                # Check website claim
                web_claim = next(
                    (
                        c
                        for c in env.get("claims", [])
                        if c.get("field") == "website" and c.get("availability") == "available"
                    ),
                    None,
                )
                if web_claim:
                    score = env.get("identity", {}).get("score", 0.0)
                    origin = (
                        env.get("discovery", {}).get("candidates", [{}])[0].get("origin", "unknown")
                        if env.get("discovery")
                        else "unknown"
                    )
                    discovered_sites.append(
                        (
                            env.get("organisation_number"),
                            env.get("input_name"),
                            web_claim.get("value"),
                            score,
                            origin,
                        )
                    )

                reqs = budget.requests_used
                elapsed = time.monotonic() - start_t
                sys.stdout.write(
                    f"\r[{done:02d}/{sample_size}] | reqs: {reqs} | websites: {len(discovered_sites)} | elapsed: {elapsed:.1f}s"
                )
                sys.stdout.flush()

    total_time = time.monotonic() - start_t
    print("\n\n" + "=" * 60)
    print(f"  RUN COMPLETE IN {total_time:.1f}s ({total_time / sample_size:.2f}s/company)")
    print(f"  HTTP Requests Used : {budget.requests_used}")
    print(f"  Total Spend        : ${budget.spend_usd:.4f}")
    print(f"  Verified Websites  : {len(discovered_sites)} / {sample_size} ({len(discovered_sites) / sample_size * 100:.1f}%)")
    print("=" * 60)

    if discovered_sites:
        print("\nDiscovered & Verified Websites:")
        print(f"{'Org Nr':<11} | {'Company Name':<30} | {'Website':<28} | {'Score':<5} | {'Origin'}")
        print("-" * 90)
        for org, name, url, score, origin in sorted(discovered_sites, key=lambda x: str(x[1])):
            name_str = (name[:28] + "..") if len(name) > 30 else name
            url_str = (url[:26] + "..") if len(url) > 28 else url
            print(f"{org:<11} | {name_str:<30} | {url_str:<28} | {score:<5.2f} | {origin}")
    print()


if __name__ == "__main__":
    main()
