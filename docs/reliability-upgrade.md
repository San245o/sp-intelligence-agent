# Discovery reliability update — 4 November 2026

The collector now shares robots rules for five minutes per scheme and host. It evaluates each requested path against the cached rules. Missing robots files (404/410) permit ordinary reads; refusals and unavailable policy responses stop the crawl and are cached for one minute. Concurrent requests to the same origin share the first robots read.

Extracted website responses are copied before company-specific identity assessment. In-memory and atomic disk caches preserve source retrieval timestamps. Positive disk entries require a retained homepage capture with a matching hash and expire after one hour; failures expire after 30 seconds. `--fresh-web` bypasses disk reads for a cold comparison while keeping within-run deduplication. Company-keyed seed maps remain discovery leads.

The central identity gate checks contiguous names, bounded organisation numbers, conflicting organisation numbers, registered domain and email relationships, registered contact details, and homepage brand context. Secondary pages listing another company do not establish homepage ownership. Knowing a registered website and attributing all its content are separate decisions. Company-site news and social outputs require content attribution.

Homepage, secondary-page and news-source bytes are stored by SHA-256 under `data/source-captures`. Stored response JSON lives under `data/web-fetch-cache`. Neither is a source of independent gold labels. Source captures make identity decisions inspectable and reproducible.

The run report includes a separate `transport` counter and hard cap for urllib attempts, including redirects and failed connections. The existing operations counters remain for comparisons with older runs; they are acquisition estimates and may differ from transport attempts. Response parsing success is distinct from network success.

External research answers cite accepted observations and abstain for absent or mismatched evidence. The evaluator now emits `external_footprint_qa_passed`, which the proxy scorer already expected. A suite must contain both supported and abstention cases; absent external cases cannot unlock the QA gate.

Use `scripts/compare_discovery_runs.py` to compare matching company sets, `scripts/audit_source_captures.py` to verify website bytes, and `scripts/plan_external_connectors.py` to export independent source discovery tasks. Tasks carry registry contact context even when no website is found. A queued task is not collected evidence.

The local 140-test suite covers existing behavior and the new identity, caching, concurrency, budget, capture and external-answer regressions, including bounded careers traversal, operating-name search leads that do not weaken the identity gate, and distinguishing FINN role URLs from report and search links. The saved-page replay retains 18 previously audited websites and withholds the eight previous uncertain or incorrect matches. These are development checks, not independent precision qualification.

Winning the external rubric still requires qualified independent reviews, workforce, engagement and sentiment sources, an independently labelled audit, and evaluation on unseen companies. Company news and cached directory ratings cannot substitute for those measurements. No provider API credentials were found in this session's environment.


The completed three-pair evaluation is recorded in `out/paired-evaluation-20261104/report.md`, with frozen manifest checks, native and common-rule counts, transport measurements, corrected job classifications, financial-source re-extraction, and outstanding scoring gaps. The public-source cache expansion plan is in `docs/discovery-cache-plan.md`.

The fourth fresh paired 100-company evaluation is complete in `out/paired-evaluation-next-20261104/report.md`. Both emitted 100 valid terminal envelopes. Ours had 26 common-rule website links / 26 attributable sites, versus 22 / 21; social companies were 12 / 10 and common-rule dated items 28 / 28. Native website links were 26 / 27 and native dated items 28 / 50. Comparable discovery transport was 1,182 / 1,386, but our cold blocking NAV scan made total runtime 632 / 365 seconds. This cohort did not demonstrate incremental recall from operating-name searches, root recovery or nested careers links. The interruption/restart cost boundary and evaluator correction are disclosed in the report.
