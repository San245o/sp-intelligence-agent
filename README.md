# Signalpost company research agent

Research Norwegian organisation numbers using official registries, independently verified company sites, dated publications, public job sources and captured external evidence. This checkout includes the imported discovery pipeline and preserves the original `signalpost` package and legacy runner.

## Run the supplied evaluator batch

Python 3.12+ is required.

```powershell
python -m pip install -r requirements-lock.txt
python -m pip install --no-deps -e .
python run.py --manifest <batch-file> --out <output-directory> --workers 8
```

For public sources without paid search:

```powershell
python run.py --manifest <batch-file> --out <output-directory> --search none
```

Inputs can be JSON arrays, JSONL, text or CSV, including gzip. `--bulk <snapshot.csv.gz>` selects a specific public registry snapshot. The default `data/brreg-enheter.csv.gz` is reused locally or downloaded from the official Brønnøysund public source if missing. New organisations outside that snapshot currently require an updated snapshot. The default paid budget is half the batch size in credits (500 for 1,000 companies), shared across providers and all key slots, with up to 2 credits per company. It favors high-potential companies; this is not a dollar-cost estimate. Automatic place discovery uses Places, not Maps. `--search none` sets the paid ceiling to zero.

Outputs include `submission.jsonl` / `envelopes.jsonl` with the flat evaluator contract, `raw_envelopes.jsonl`, `profiles.jsonl`, `run-report.json`, a structured acquisition-attempt ledger and provider usage checkpoints. Records keep real source hashes and acquisition times; the adapter does not manufacture missing evidence. Per-company operations include measured runtime and paid credits. Dollar cost is reported as unknown when credits were used without a configured USD-per-credit rate; set `SERPER_USD_PER_CREDIT` to your actual rate (including 0 for a known free allocation).

## Evidence rules

- A directory listing or company mention never establishes ownership of its host. Deep entries need independent owner-homepage/registered-domain evidence.
- Scoped owner identifiers, explicit legal footer/contact identity and exact registry corroboration can verify a site. Namesake matches, placeholder pages and contradictory identifiers are withheld.
- Company-published news and independent publisher coverage are separate source classes. Modification dates are not publication dates. Default template posts and publications before the current legal entity was established are withheld.
- A job requires a specific, current posting with exact employer proof. A board link is a lead. NAV uses public feed changes plus retained active entries; it is not a complete historical bootstrap.
- Places ratings require legal/official operating-name and exact registered street/postcode corroboration. Aggregates are not individual reviews or text sentiment.
- Audience metrics require captured numeric evidence. Upload cadence, profile links and search snippets are not engagement statistics.
- Missing evidence is unknown, not zero activity. Independent audits and hidden tournament labels remain necessary for official precision/score claims.

## Search configuration and local data

Provider keys come from environment variables (`SERPER_API_KEY`, optional `_2` / `_3`, or `SERPER_API_KEYS`) or an optional private `.local/provider-credentials.json`. No key or recoverable credential payload is included in the public source. Set `SIGNALPOST_USE_LOCAL_DEFAULTS=0` for environment-only credentials. Without keys, the pipeline uses public-source fallback. `.env.example` contains placeholders; never commit a private `.env`.

Public discovery seed maps are included under `data/local-cache`. Large SQLite indexes, raw captures, provider lead caches, NAV state and benchmark output remain local and are ignored by Git. Seeds are candidates: current source retrieval and identity checks decide publication. Optional index refresh commands are documented in [cache inventory](data/local-cache/README.md). The original root `data` maps and original `src/signalpost` remain preserved.

## Research workspace

```powershell
python scripts/build_prototype.py --input <output-directory>/profiles.jsonl --output <output-directory>/viewer.html
python scripts/ask_agent.py --help
```

The workspace provides company search, evidence filters, source-linked research answers, dated news sorted by publication time, financial/role/location evidence, explicit unknown states, and saved local views/exports. UI summaries derive from accepted observations. Company-published and independent news are visibly distinguished. Browser audit findings and limitations are in [UX review](docs/ux-review-20261105.md).

## Verification

```powershell
python -m unittest discover -s tests -p "test_*.py"
```

The current suite passes 212 tests, including the preserved original suite, ownership/source provenance, publication quality, paid-credit caps, free fallback, NAV snapshots, contract output and argument forwarding. A zero-paid live integration check exercises the actual evaluator entry point.

Read [search policy](docs/search-budget.md), [migration record](docs/migration-20261105.md), [pipeline details](docs/research-pipeline-guide.md) for measurement boundaries. Historical architecture and performance claims are preserved separately in [legacy guide](docs/legacy-agent-guide.md); they do not describe the new default paid-search path. The legacy entry remains `python scripts/run_batch.py ...`.
