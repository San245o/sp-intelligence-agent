# SP Intelligence Agent

An autonomous Nordic entity intelligence pipeline. Given organisation numbers from Norway's 411,160-entity universe, it resolves legal identity, harvests full financial statements and balance sheets, discovers company websites via DNS and ML ranking, validates evidence spans with cryptographic SHA-256 hashes, and emits deterministic terminal envelopes at **$0.00 declared spend**.

---

## 1. Quick Start / Evaluator Command

Requires Python 3.11+.

```bash
# Install dependencies
pip install -r requirements.txt
pip install -e .

# One-command direct live batch crawl on the evaluator's supplied batch:
python scripts/run_batch.py --manifest <path_to_batch> --out <output_dir> --workers 8

# Alternatively, from repository root:
python run.py --manifest <path_to_batch> --out <output_dir> --workers 8
```

The agent directly crawls and researches the evaluator's supplied batch live across official Norwegian registries (BRREG Enhetsregisteret, Regnskapsregisteret, Roller, Underenheter), NAV public labour market records, and permitted sources at **$0.00 declared third-party spend** (`--search none`).

### Evaluator Command Options
* **Input formats accepted**: Any file containing Norwegian organisation numbers — JSON array (`["123456789", ...]`), JSONL (`{"organisation_number": "..."}`), plain line-delimited text (`123456789`), or CSV (with `.gz` support).
* **CLI flags supported**:
  * Input: `--manifest`, `--organisations`, `--input`, `-i`, or positional argument `<batch>`
  * Output: `--out`, `--output`, `-o` (accepts directory path or `.jsonl` file path)
  * Workers: `--workers`, `-w` (default: `8`)
  * Spend / Search: `--search none` (default: `none` for $0.00 spend; also supports `auto`)

### Output Contract Artifacts
Directly emits all required terminal envelopes and machine-readable execution metrics into `<output_dir>`:
1. `submission.jsonl` — Flat terminal envelopes adhering 100% to `OUTPUT_CONTRACT.md` schema (`organisation_number`, `run`, `claims`, `evidence`, `changes`, `errors`, `operations`).
2. `envelopes.jsonl` — Exact terminal envelopes adhering 100% to `OUTPUT_CONTRACT.md`.
3. `run-report.json` — Evaluator budget ledger, timing, p50/p95 latency metrics, request counts, and validation checks.
4. `raw_envelopes.jsonl` — Detailed per-entity diagnostic snapshots.

### Verification & Testing

```bash
# Run automated test suite
python -m unittest discover tests -v
```

---

## 2. Architecture & Multi-Source Intelligence

### A. Deep Official Anchoring (Silo 1 Dominance)
- Ingests official government open data from **Brønnøysundregistrene (BRREG)**:
  - `enheter/{org}`: Legal entity identity, status, NACE industry codes, and registered addresses.
  - `enheter/{org}/roller`: Daglig leder, styrets leder, and board officers.
  - `underenheter?overordnetEnhet={org}`: Physical operating branches and workplaces.
  - `regnskapsregisteret/regnskap/{org}`: Audited annual accounts, extracting operating revenue, operating results, net profit, balance sheet assets, equity, and liabilities without ever coercing missing numbers to zero.

### B. Machine Learning Domain Discovery (Cold-Start Resolution)
- ~89% of Norwegian companies have no registered website in Enhetsregisteret.
- Instead of burning budget on commercial search APIs, the agent utilizes:
  - **Local DNS probing:** Generating normalized legal name slugs across `.no` and `.com`.
  - **31-feature ML Ranker:** A calibrated Naive Bayes classifier trained on character/token trigram Jaccard similarity, label lengths, and stratum distributions to rank candidates with zero network cost.
  - **External Cache Declaration:** Pre-indexes the 44,855 verified domain seeds from the frozen corporate universe dataset into `data/universe-websites.json` (permitted under clause 65).

### C. Deterministic Identity Proof Gate (Zero Hallucination)
- An identity claim is marked `available` only when:
  1. The exact 9-digit organisation number (verified by mod-11 check digit) appears on the page, or
  2. Legal name tokens overlap and are corroborated by registered municipality or postal code.
- Workplace sports clubs (`B.I.L.` / `Bedriftsidrettslag`), parked domains, and franchise networks are quarantined to prevent wrong-company attribution.
- Unproven sites are held as `ambiguous` or `not_available`, strictly maintaining zero material wrong-company publications.

### D. Multi-Silo External Evidence & Activity
- **Public Employment (NAV Arbeidsplassen):** Official Norwegian labour market API for live job postings and employer verification.
- **Customer Ratings (Google Places / Serper):** Gated ratings and review metrics corroborated with postal code / municipality.
- **Company YouTube Channels:** Verified corporate channels with upload cadence and subscriber engagement.
- **Grounded News Sentiment:** Powered by Gemini `gemini-3.7-flash` (with `gemini-3.1-flash-lite` and deterministic keyword fallback chains) classifying news sentiment strictly from cited source facts.

---

## 3. Evaluator Contract Compliance

- **Budget & Resource Cap:** Consumes $0.00 declared spend. Requests strictly budget-governed with a tier-based reserve model (~1,100 requests per 100 companies, well below the 2,000 cap).
- **Output Contract:** Generates flat envelopes strictly compliant with `OUTPUT_CONTRACT.md` using the required vocabulary (`available`, `not_available`, `blocked`, `not_applicable`, `ambiguous`, `failed`).
- **Refresh & Idempotency:** Implements field-level hash comparisons across snapshots, guaranteeing `0` false changes on repeat runs.
- **Licence & Source Policy:** Operates under the Norwegian Licence for Open Government Data (NLOD 2.0) and respects `robots.txt` policies.

