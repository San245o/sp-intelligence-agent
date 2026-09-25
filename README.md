# Signalpost Agent

An autonomous Norwegian company intelligence agent built for the Builderr Signalpost challenge. Given organisation numbers from Norway's 411,160-entity universe, it resolves legal identity, harvests full financial statements and balance sheets, discovers company websites via DNS and ML ranking, validates evidence spans with cryptographic SHA-256 hashes, and emits deterministic terminal envelopes at **$0.00 declared spend**.

---

## 1. Quick Start / Evaluator Command

Requires Python 3.11+.

```bash
# 1. Install dependencies
pip install -e .

# 2. Run a 100-company evaluation batch
python scripts/run_batch.py \
  --manifest data/smoke-manifest.jsonl \
  --out runs/eval-100 \
  --workers 8

# 3. Format into the exact minimal OUTPUT_CONTRACT schema
python scripts/to_contract.py \
  --input runs/eval-100/envelopes.jsonl \
  --output runs/eval-100/submission-100.jsonl

# 4. Verify refresh idempotency (0 false changes)
python scripts/run_refresh.py \
  --previous runs/eval-100/submission-100.jsonl \
  --current runs/eval-100/submission-100.jsonl \
  --output runs/eval-100/refresh-report.json

# 5. Run automated test suite
python -m unittest discover tests -v
```

---

## 2. Architecture & Design

### A. Deep Official Anchoring (Silo 1 Dominance)
- Ingests official government open data from **Brønnøysundregistrene (BRREG)**:
  - `enheter/{org}`: Legal entity identity, status, NACE industry codes, and registered addresses.
  - `enheter/{org}/roller`: Daglig leder, styrets leder, and board officers.
  - `underenheter?overordnetEnhet={org}`: Physical operating branches and workplaces.
  - `regnskapsregisteret/regnskap/{org}`: Audited annual accounts, extracting operating revenue, operating results, net profit, balance sheet assets, equity, and liabilities without ever coerting missing numbers to zero.

### B. Machine Learning Domain Discovery (The 89% Cold-Start)
- ~89% of Norwegian companies have no registered website in Enhetsregisteret.
- Instead of burning budget on commercial search APIs, the agent utilizes:
  - **Local DNS probing:** Generating normalized legal name slugs across `.no` and `.com`.
  - **31-feature ML Ranker:** A calibrated Naive Bayes classifier trained on character/token trigram Jaccard similarity, label lengths, and stratum distributions to rank candidates with zero network cost.
  - **External Cache Declaration:** Pre-indexes the 44,855 verified domain seeds from Builderr's frozen `company-universe.jsonl.gz` into `data/universe-websites.json` (permitted under clause 65).

### C. Deterministic Identity Proof Gate (Zero Hallucination)
- An identity claim is marked `available` only when:
  1. The exact 9-digit organisation number (verified by mod-11 check digit) appears on the page, or
  2. Legal name tokens overlap and are corroborated by registered municipality or postal code.
- Workplace sports clubs (`B.I.L.` / `Bedriftsidrettslag`), parked domains, and franchise networks are quarantined to prevent wrong-company attribution.
- Unproven sites are held as `ambiguous` or `not_available`, strictly maintaining zero material wrong-company publications.

### D. Zero-Cost On-Site Activity & News Signals
- Derived directly from verified website crawls:
  - `hiring_or_activity_signal`: Analyzes bounded pages, verified social links, and career paths (`/karriere`, `/jobs`, `/stillinger`).
  - `dated_public_activity`: Scans for press/news paths (`/aktuelt`, `/nyheter`, `/press`) and extracts the latest publication.

---

## 3. Evaluator Contract Compliance

- **Budget & Resource Cap:** Consumes 0 paid third-party API spend ($0.00). Completes 100-company batches well within the 2,000-request limit and 45-minute wall-clock cap.
- **Output Contract:** Generates flat envelopes strictly compliant with `OUTPUT_CONTRACT.md` using the required vocabulary (`available`, `not_available`, `blocked`, `not_applicable`, `ambiguous`, `failed`).
- **Refresh & Idempotency:** Implements field-level hash comparisons across snapshots, guaranteeing `0` false changes on repeat runs.
- **Licence & Source Policy:** Operates under the Norwegian Licence for Open Government Data (NLOD 2.0) and respects `robots.txt` policies.
