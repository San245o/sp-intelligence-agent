# Budgeted external discovery

The runner supports Serper and Tavily plus public-source readers. Environment variables override optional private local configuration loaded by `runtime_defaults.load_defaults()`. The public repository contains no keys or recoverable credential payload. Without credentials the pipeline uses public sources. `.env.example` documents private overrides; it is an example, not an automatically loaded file. Tavily requires its own configured credential.

## Spending policy

- The default budget is half the batch size (500 credits for 1,000 companies); an explicit `--paid-search-budget` overrides it. It is shared across all provider keys and categories, not a per-key quota. Search/news/Places reserve one credit; optional Maps reserves three. Serper response `credits` fields reconcile actual charges. Failed attempts reserve credits conservatively. An unexpected higher charge stops that provider for investigation.
- `--paid-search-per-company 2` limits paid attempts per company, including credential fallback. Every company receives public-source discovery. Paid credits go only to companies scoring at least 75/100 on registry identity completeness, then rank by employees, reported revenue when available, verified or registered websites, and operating names. The score controls spend priority only; it never relaxes publication identity checks. A usable street/postcode and an operating-business signal are required before a paid place lookup.
- Automatic place discovery uses **Places**, not Maps. Maps remains available in the adapter for an explicitly selected detail requirement and is charged at three credits.
- General web leads, including website-discovery results, are reused across relevant evidence categories. News responses remain in the news category; Places responses remain place candidates. Search leads persist with source-specific TTLs, and Places can reuse an existing Maps response for the same query. No fresh source retrieval time is invented on cache hits.
- Only the first website query can spend paid credits. A second paid attempt may search an exact company name, registered municipality and selected social domains for candidate profiles. Those leads still require independent identity corroboration. Subsequent website recovery uses cached leads/public fallback. A paid cap or an empty paid general search never disables the public general-search fallback.
- Registered domains, captured pages, public feeds, evidenced ATS boards and cached leads come before another paid query. Account/service throttling is not bypassed by rotating keys.
- The configured limit is a run-level control. Source reports expose attempts, credits, scheduled companies, cached results and withholding reasons. Dollar cost is not inferred without the user's actual plan pricing.

## Evidence paths

| Path | Publication condition |
|---|---|
| New company website | Captured source passes the existing identity gate and ownership checks; registry/directory listings and unrelated publisher articles are excluded |
| Places rating/count | Provider API capture plus legal/official operating name and exact registered street/postcode; source/provider attribution retained |
| Independent dated news | Captured publisher article, supported publication date and corroborated company identity; historical publication dates remain historical |
| Public ATS jobs | Greenhouse/Lever board link evidenced on the company's site, currently published specific role and per-role employer evidence; no fabricated publication/expiry date |
| Workforce | Exact-company structured metadata or standalone annual-report text; group counts and ambiguous extraction abstain |
| Engagement | Attributable captured media statistics; snippets and profile links do not become follower or interaction counts |
| Sentiment | Individual review rating polarity is explicitly labelled as rating-derived; aggregate ratings and company marketing do not become text sentiment; independent NLP qualification remains required |

The dashboard reads observations embedded in each profile and provides source-linked evidence cards. Research answers include Places summaries, workforce and public metrics. Refresh diffs ignore acquisition-time-only changes and do not treat missing observations as closure or zero activity. `--previous-profiles` requires identical organisation membership; `--changes-output` controls the change-event JSONL path. Completed profiles resume without new acquisition.

NAV retains a complete baseline, applies incremental changes including inactivation, and overlaps the previous scan start to avoid pagination gaps. A failed/incomplete refresh never replaces a complete index. The feed refresh runs alongside company enrichment. Coverage remains recent changes plus previously seen active ads, not a complete historical bootstrap.

## Local validation

The current suite passes 209 tests. A live full-runner smoke test passed with zero paid credits; resuming it fetched zero profiles and made zero transport attempts. Details and paid-credit reconciliation are under `out/provider-upgrade-20261104`.

The same-cohort recovery check is documented in `out/discovery-recall-fix-20261104/report.md`. It fixes missed retained articles, visible article-header dates, a 700 KB article limit, shallow sitemap traversal, translated duplicates and paid/public routing. New fetch-cache records use extraction version 2 so older records do not bypass homepage ownership extraction. This recovery check is separate from the original frozen paired benchmark.
