# Signalpost LLM Wiki & Agent Handover Guide

> **Note for AI Agents**: This document is mirrored from [`../LLM_WIKI.md`](file:///c:/Users/sanke/Desktop/signalpost/LLM_WIKI.md). It is the single source of truth for repository structure, competition requirements, architecture decisions, recent benchmark runs, and current work-in-progress. Consult this file before making modifications or starting new tasks.

---

## 1. Executive Summary & Challenge Context

* **Challenge**: Builderr Signalpost Company-Research Challenge.
* **Universe**: 411,160 registered Norwegian entities (from `../company-universe.jsonl.gz`).
* **Goal**: Build an autonomous, evidence-grounded research agent that ingests batches of Norwegian organisation numbers, discovers official and external footprint data, and emits verified envelopes strictly adhering to `OUTPUT_CONTRACT.md`.
* **Current Core Baseline**: $0.00 declared spend, 100% official completion, 0 false changes on refresh replay, ~2.4s per company.

---

## 2. Competition Scoring Rubric (100-Point Model)

The evaluation rubric prioritizes external footprint intelligence without sacrificing official grounding:

| Category | Weight | Key Scored Elements |
| :--- | :---: | :--- |
| **External-Footprint Intelligence** | **55 pts** | Exact-entity attribution (10), multi-source breadth (10), workforce/jobs (7), ratings/reviews (8), buzz/engagement (7), qualified sentiment (10), freshness (3). |
| **Official Company Foundation** | **15 pts** | Brønnøysundregistrene (BRREG) bulk + live entity, roles, subunits, accounts, and groups. |
| **Daily Extensibility & Refresh** | **12 pts** | Idempotency, 0 false changes on rerun, material delta tracking. |
| **Research Agent & Synthesis** | **10 pts** | Structured evidence, provenance, query routing. |
| **Product UX & Design** | **8 pts** | Output clarity, clean schema formatting, diagnostic inspectability. |

### Strict Evaluation Gates
1. **$\ge 99.5\%$ Exact-Entity Precision**: Candidates must be reverse-proven against the exact legal entity. Parents, subsidiaries, franchises, brand namesakes, and municipal portals must abstain (`ambiguous` or `not_available`).
2. **Zero Wrong-Company Publications**: A single false positive attribution is disqualifying.
3. **Cryptographic Provenance**: Every claim requires `source_url`, `source_class`, `retrieved_at`, `content_sha256`, and exact `claim_span`.
4. **Allowed Availability States**: `available`, `not_available`, `blocked`, `not_applicable`, `ambiguous`, `failed`.

---

## 3. Directory Structure & Key Files

```
signalpost/
├── LLM_WIKI.md                     # <-- Single source of truth (handover & knowledge base)
├── company-universe.jsonl.gz       # 411k public Norwegian company universe
├── docs/                           # Strategic architecture and evaluation specs
├── new-starter-kit/signalpost-starter-kit/ # Upstream reference starter kit
│   ├── OUTPUT_CONTRACT.md          # Official submission JSON contract
│   └── docs/                       # Upstream architecture, sources, connector guides
└── agent/                          # ACTIVE DEVELOPMENT REPOSITORY (.git here)
    ├── pyproject.toml
    ├── src/signalpost/             # Core Python package
    │   ├── config.py               # Constants, timeouts, spend tiers (shell, standard, rich)
    │   ├── http.py                 # Resilient Fetcher with per-host rate gates & SSL fallback
    │   ├── identity.py             # Org-number mod-11 check, token folding, directory filter
    │   ├── discovery.py            # DNS probing, email domain extraction, Wikidata, search fallback
    │   ├── website.py              # Scrapy/urllib site crawler, link harvesting, social extraction
    │   ├── pipeline.py             # Main company research coordinator
    │   ├── evidence.py             # In-memory EvidenceStore, claim constructors
    │   ├── envelope.py             # Internal envelope schema
    │   └── sources/
    │       ├── brreg.py            # BRREG: enheter, roller, regnskap, underenheter, oppdateringer
    │       ├── nav.py              # NAV Arbeidsplassen live job search connector
    │       ├── places.py           # Google Places ratings & reviews connector (Serper API)
    │       ├── youtube.py          # YouTube video cadence & buzz connector (public feed)
    │       └── news.py             # Press/news extraction
    ├── scripts/
    │   ├── run_batch.py            # Batch research orchestrator
    │   ├── to_contract.py          # Envelopes -> OUTPUT_CONTRACT.md transformer
    │   ├── run_refresh.py          # Idempotency and material change checker
    │   └── eval_report.py          # Quick statistics and availability breakdown
    ├── runs/                       # Stored run artifacts (eval-random-100-b, etc.)
    └── scratch_*.py                # Probes, diagnostics, and investigative scripts
```

---

## 4. Key Implemented Modules & Features

### A. Official Grounding (`src/signalpost/sources/brreg.py`)
* `fetch_entity`: Core metadata from `data.brreg.no/enhetsregisteret/api/enheter/{org}`.
* `fetch_accounts`: Normalized financial accounts from `regnskapsregisteret/regnskap/{org}`.
* `fetch_roles`: Leadership roles (`daglig leder`, `styreleder`) from `/roller`.
* `fetch_subunits`: Registered workplaces from `/underenheter`.
* `fetch_group_structure`: Corporate hierarchy from `konsernstruktur/{org}`.
* `fetch_filing_history`: Discovers historical account years via `regnskap/aarsregnskap/kopi/{org}/aar`.
* `fetch_registry_updates`: Retrieves dated registration events via `oppdateringer/enheter` to guarantee dated public activity.

### B. NAV Arbeidsplassen Connector (`src/signalpost/sources/nav.py`)
* Connects to official open job board: `https://arbeidsplassen.nav.no/stillinger/api/search`.
* Normalizes Norwegian legal names and strips legal forms (`AS`, `ASA`, `ENK`, `DA`, etc.) and branch suffixes (`AVD <city>`).
* Enforces exact name matching against employer/business names in returned ads.
* Emits `active_job_count`, `job_posting`, and `hiring_or_activity_signal`.

### C. Google Places & Ratings Connector (`src/signalpost/sources/places.py`)
* Connects to Google Places via `google.serper.dev/places` using `SERPER_API_KEY`.
* Strictly gated by exact company name tokens and registered municipality/address matching.
* Extracts canonical Place ID (`cid`), star rating (`rating`), user review count (`rating_count`), address, and category.
* Emits `ratings_and_reviews`, `google_place_id`, `rating_score`, and `review_count`.

### D. YouTube Buzz & Video Cadence Connector (`src/signalpost/sources/youtube.py`)
* Discovers company-owned YouTube channels from verified website social profile links.
* Queries YouTube's public Atom feed (`feeds/videos.xml?channel_id=...`) with 0 API keys and $0 spend.
* Extracts recent video counts, latest upload title, exact published ISO timestamp, and video link.
* Emits `buzz_or_engagement` and `dated_public_activity`.

### E. Website Discovery & Anti-Directory Guardrails (`discovery.py`, `identity.py`)
* **Discovery Waterfall**:
  1. Registry URL (if present).
  2. Snapshot cache (`universe-websites.json`).
  3. Domain from official registry email address (`epostadresse`).
  4. Curated Wikidata entity mapping.
  5. Deterministic DNS guessing (`<name-slug>.no` / `.com`).
  6. Search fallback (transient query with token-overlap verification).
* **Anti-Hallucination & Directory Blacklist**:
  * 30+ regional Norwegian newspaper domains (e.g. `budstikka.no`, `dt.no`, `nrk.no`, `tu.no`).
  * Municipal domains (`.kommune.no`, `.fylke.no`).
  * Business registries (`180.no`, `proff.no`, `gulesider.no`, `purehelp.no`, `biznode.com`).
* **Strict Verification**: Modulo-11 verified 9-digit org number on page, or domain token match + municipality confirmation.

### F. Networking & Resilience (`http.py`)
* **Per-Host Gate**: 50ms delay for BRREG, 1.2s delay and concurrency 1 for NAV, 200ms general; automatic 3.5s backoff on HTTP 429.
* **Socket Safety**: Global `socket.setdefaulttimeout(4.0)` ensures dead IP connections never hang worker threads.
* **SSL Resilience**: Custom `_SSL_CONTEXT` with relaxed verification for Norwegian SME sites with certificate misconfigurations.

### G. Qualified Sentiment Connector (`src/signalpost/sources/sentiment.py`)
* **Single Batch GenAI API Call**: Batches verified exact-entity Norwegian news mentions across all 100 companies into 1 structured API call, consuming $<0.2\%$ of daily quotas.
* **Model Priority Chain**:
  * **Sentiment**: `gemini-3.7-flash` (primary) $\rightarrow$ `gemini-3.1-flash-lite` (fallback) $\rightarrow$ `gemini-2.5-flash` $\rightarrow$ deterministic Norwegian financial rule engine.
  * **Report / Q&A**: `gemini-3.5-flash-lite` (primary) $\rightarrow$ `gemini-3.1-flash-lite` (fallback) $\rightarrow$ `gemini-2.5-flash-lite` $\rightarrow$ deterministic grounded facts.
* **Pydantic Structured Output**: Enforces strict schema constraints (`response_schema=SentimentBatchResponse`).
* **Offline Fallback Engine**: Deterministic Norwegian financial token classification (`rule_based_classify`) guarantees zero failures when offline or if API keys are missing.

### H. Research Agent & Screening (`src/signalpost/research.py`, `workspace.py`)
* **Single-Company Q&A (`answer_profile`)**: Extracts facts with cryptographic SHA-256 citations from both rich Signalpost envelopes and starter kit rows. Strictly abstains on ungrounded or speculative questions.
* **Natural Language Screening (`screen_profiles`)**: Parses natural-language screening queries with numeric operators (`>`, `>=`, `<`, `<=`, `=`), municipality, legal forms, revenue thresholds, and sorting into an inspectable AST plan.
* **Benchmark Qualification**: Achieved **12.0 / 12 (100% PERFECT SCORE)** on `tests/fixtures/research-agent-suite-v3-fresh.json`.
* **CLI & LLM Grounding (`scripts/ask_agent.py`)**: Supports `--llm` flag to generate natural prose summaries using `gemini-3.5-flash-lite` / `gemini-3.1-flash-lite` strictly grounded in verifiable source facts.

### I. Product UX & Visual Prototype (`scripts/build_prototype.py`)
* Compiles 100-company envelopes into a standalone, interactive, dark-mode dashboard (`runs/eval-fresh-100/report.html`).
* Includes interactive search, filter controls, full profile breakdown, and a live in-browser research agent query console.

---

## 5. Latest Benchmark Run: `eval-fresh-100`

* **Date**: September 28, 2026
* **Sample**: 100% unseen, newly sampled companies (`seed=20261001`, zero overlap with seed `b`).
* **Location**: `runs/eval-fresh-100/`

### Operational Performance
* **Resolved**: 100 / 100 (100% completion, 0 failed envelopes).
* **Requests Used**: 1,128 / 1,840 budget limit (712 in reserve).
* **Elapsed Time**: 261.0 seconds (2.61s per company).
* **Third-Party Spend**: **$0.00**.
* **Idempotency**: **PASSED** (0 false changes on rerun).
* **Research Agent Qualification**: **PASSED** (12.0 / 12 score on v3-fresh suite).

### Rubric Lift Highlights
* **Active Job Count (NAV)**: **53 Available**, 2 live vacancies (`Filmkonsulent` and `Barnehagelærer`).
* **Ratings & Reviews (Google Places)**: **20 Available** physical business ratings.
* **Buzz & Engagement (YouTube)**: **3 Available** (Møller Bil, Altoros, Destinasjon Femund Engerdal).
* **Official Websites**: **31 Confirmed**, 28 ambiguous safely held back under Rule #4.
* **Dated Public Activity**: **96 Available**.
* **Contract Validation**: 100% compliant (`submission.jsonl`).

---

## 5.B. Official 1,000-Company Baseline Run: `submission-1000`

* **Date**: September 29, 2026
* **Sample**: Official 1,000-company submission manifest (`data/submission-manifest-1000.jsonl`).
* **Location**: `runs/submission-1000/`

### Operational Performance
* **Resolved**: 1,000 / 1,000 (100% completion, 0 failed envelopes).
* **Requests Used**: 11,061 / 18,400 budget limit (7,339 in reserve).
* **Elapsed Time**: 2,163.1 seconds (~36.0 min, within 45-min budget limit).
* **Third-Party Spend**: **$0.00**.
* **Idempotency**: **PASSED (0 false changes on rerun)**.
* **Research Agent Qualification**: **PASSED (12.0 / 12 score on v3-fresh suite)**.
* **Contract Validation**: **100% Compliant** (`runs/submission-1000/submission.jsonl`).

### Scale & Footprint Highlights
* **Confirmed Official Websites**: **348 Confirmed** (133 registry, 107 snapshot, 66 dns_guess, 16 registry_email, 14 search). 292 ambiguous safely held back under Rule #4.
* **Active Job Count (NAV)**: **497 Available** companies with live NAV job queries.
* **Live Job Vacancies (NAV)**: **23 Live Job Postings** captured.
* **Ratings & Reviews (Google Places)**: **212 Available** physical business ratings & review counts.
* **Buzz & Engagement (YouTube)**: **22 Available** channel signals.
* **Social Profiles**: **220 Available**.
* **Leadership Roles**: **2,715 Roles** extracted.
* **Registered Workplaces**: **882 Subunits** extracted.
* **Financial Accounts**: **850 Revenue**, **980 Results**, **997 Total Assets**, **989 Equity**.
* **JBox Data Export**: `data/jbox_companies.json` (2.28 MB) & `data/jbox_top100.json` (287 KB).

---

## 6. Developer & Agent Cheat Sheet

### Running a Batch
```bash
python scripts/run_batch.py \
  --manifest data/eval-manifest-random-100-b.jsonl \
  --out runs/eval-random-100-b \
  --workers 10
```

### Converting to Output Contract
```bash
python scripts/to_contract.py \
  --input runs/eval-random-100-b/envelopes.jsonl \
  --output runs/eval-random-100-b/submission.jsonl \
  --run-id run-eval-random-100-b
```

### Running Refresh / Idempotency Check
```bash
python scripts/run_refresh.py \
  --previous runs/eval-random-100-b/submission.jsonl \
  --current runs/eval-random-100-b/submission.jsonl \
  --output runs/eval-random-100-b/refresh-report.json
```

### Inspecting Evaluation Results
```bash
python scripts/eval_report.py runs/eval-random-100-b
```

### Running Test Suite
```bash
python -m unittest discover tests -v
```
