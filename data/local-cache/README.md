# Local discovery cache

These files were copied from the existing `agent/data` cache and remain local inputs to the Signalpost batch. They are keyed by Norwegian organisation number where available:

- `universe-websites.json`, `official_411k_domains.json`, and `wikidata-websites.json` supply website candidates.
- `universe_social_profiles.json` supplies social profile candidates.
- `universe_linkedin_profiles.json` supplies LinkedIn leads and, newly, its company website field as an additional website candidate.
- `fagfolkguiden_harvested.json` supplies company-number-keyed directory records. Its external `sameAs` links can seed website checks when the embedded organisation number matches the key.
- `brreg_subunits.sqlite` is a locally indexed snapshot of 870,000+ Brønnøysund subunits. It can be queried by parent organisation number for operating locations, industry, employee count, and registered website. Registered websites are passed to the competition runner as source-labelled candidates, then fetched live and checked against the parent company identity. The index records the source URL, retrieval time, last-modified value, source hash, and NLOD license.
- `brreg_companies.sqlite` is a locally indexed snapshot of 1.17 million Brønnøysund entities. It can be queried by organisation number and includes legal name/form, employee count, status flags, municipality, industry, registered website, and latest filed accounts year. Registered websites are passed to the competition runner as candidates; cached facts themselves are not published as fresh claims. Personal contact fields and street addresses are not stored. Social profiles are not part of the Brønnøysund dataset.
- `universe_ratings_reviews.json` and Fagfolkguiden's cached ratings remain context only; a connector must refresh the page, capture source evidence, check identity and pass its rights review before emitting them as observations.
- `unique_crawler_domains.json` is a domain pool without company-number keys. It needs identity-safe mapping before use.
- Company-name `.no` domain guesses and keyless DuckDuckGo Lite/Bing RSS search supply fallback candidates when registry and cached candidates fail.

Cached and searched URLs are leads only: the batch fetches each candidate and applies the parked-domain and exact-company identity gate against the homepage and fetched same-site pages. Fagfolkguiden itself and social-network links are excluded from its website candidate list. Once a site passes, a bounded RSS/sitemap pass can capture dated company-owned news pages. Social links are published only when the site passes the gate and each handle independently passes the name check. Search results themselves are not claims or retained evidence.

The current LinkedIn cache has about 20,000 exact-keyed company profiles. Roughly 4,650 contain website candidates absent from the existing website, official-domain, and Wikidata candidate maps. Fagfolkguiden has about 7,100 company rows and 1,500 precomputed rating summaries; it adds a small number of website leads beyond the existing maps and LinkedIn. `cache_coverage()` reports these source counts and the expanded unique website-candidate pool.

To refresh the cache, use permitted discovery connectors in `scripts/` to collect new candidates or observations. Save their outputs under `data/local-cache` only when their source terms permit retention. Do not save raw search provider results when the connector policy says they must be discarded.

Refresh the open subunit snapshot and build its SQLite index with:

```powershell
python scripts/refresh_brreg_subunit_cache.py
```

Refresh the full entity register and build its SQLite index with:

```powershell
python scripts/refresh_brreg_company_cache.py
```

NAV jobs are discovered live during `scripts/run_competition_batch.py`: one recent feed scan is shared across the whole batch, candidates are shortlisted by registered parent/subunit names, and detail pages are fetched only for those candidates. The detail's employer organisation number must match exactly before a job is attributed. The rotating public experimental token is fetched at runtime and is not saved or printed. Default caps are 120 feed pages and 200 detail requests per run, and every NAV request is charged to the same global 2,000-request budget. This keeps the submission independent of a prebuilt, potentially stale NAV snapshot. See the competition wiki for the matching and coverage limits.

The refresh command sends the saved source ETag and skips downloading/rebuilding when Brønnøysund says the snapshot has not changed. Pass `--keep-snapshot` if you need to retain the compressed source file as well as the indexed SQLite cache.

Inspect pool sizes or retrieve one company's local candidates, directory context, cached review summary, and subunits with:

```powershell
python scripts/inspect_local_cache.py --coverage-only
python scripts/inspect_local_cache.py --organisation 925732532
```

Pass `--cache-dir PATH` to use a different local cache folder. The default is this directory.
