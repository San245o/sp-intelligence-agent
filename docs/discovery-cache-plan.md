# Public-source discovery and cache plan

## Objective and measurement

Improve exact-company evidence recovered per transport request. Website counts, source breadth, jobs, reviews, engagement, workforce and qualified sentiment stay separate. Three frozen, disjoint random batches compare both implementations on the same organisations within each pair. Native output counts and consistently qualified counts are reported separately. A terminal batch is not proof of precision, and synthetic tests are not independent company labels.

## Implemented in the third iteration

| Capability | Location | Effect |
|---|---|---|
| Ranked legal, brand, municipality and Norwegian-letter domain variants | `src/norway_company_agent/candidate_sources.py` | Uses bounded alternatives without treating guesses as proof |
| Mapped subunit domains, email domains and operating-name guesses | same module and official location normalization | Uses operational brands beyond the parent's legal name |
| Remembered verified organisation-to-domain leads | `data/local-cache/verified-sites.sqlite` | Future research starts with the last successful domain and verifies it again |
| Captured owned `Organization.sameAs` links | central identity gate | Finds declared profiles without scraping a platform; excludes unrelated site-builder organisations |
| Shared robots rules and copied response records | website client | Reuses acquisition while checking each company's identity independently |
| One-hour positive response cache; short failure TTL | `data/web-fetch-cache` | Retains original source retrieval times; `--fresh-web` supports cold website comparisons |
| Complete shared NAV feed index | `data/nav-feed-snapshot.json` | Reuses the global shortlist for an hour, filters each requested window, and verifies employer and expiry on live ad details |
| Retained source bytes | `data/source-captures` | Auditable website, official registry and verified NAV sources, with hashes |
| Captured direct `JobPosting` records | `src/norway_company_agent/site_jobs.py` | Accepts actual role pages with employer identity and current dates; excludes social profiles and careers indexes |

## Next experiments, in order

The fourth paired batch adds official parent-linked operating-name search leads, root recovery after a deep URL returns 404/410, diverse secondary-page selection, and same-origin role-page traversal within the existing four-page ceiling. Tests pass (140). Its frozen inputs, code hashes, interruption/restart disclosure and completed comparison are stored under `out/paired-evaluation-next-20261104`. Public ATS API readers and Common Crawl fallback are still proposed experiments, not implemented sources in this batch. Six companies used operating-name recovery searches with no accepted-site recovery; root recovery and nested role traversal had no demonstrated opportunity in this cohort.

The fourth run makes NAV indexing the first efficiency priority: its complete cold 91-page rebuild used 92 requests, about 44.1 MB and approximately 277 seconds before index publication, with no matching ad candidates. Implement incremental refresh over a retained complete baseline, including removals/status changes and explicit coverage/completeness, and avoid serializing all company enrichment behind the feed scan. Continue live employer and expiry checks on selected details; do not turn a partial refresh into a negative finding.

1. **Grow the official identity index once for the whole population.** Refresh parent and subunit website/email/contact fields from official datasets using the existing refresh scripts. Preserve source versions. Store a parent-to-subunit relation, not an assumption that every site or profile of a group belongs to every subsidiary.
2. **Build an alias graph from retained primary sources.** Each alias needs the legal entity, source URL, source hash, retrieval time and relation type: legal name, displayed brand, operating subunit, registered person or related group. Query an evidenced operating brand plus registered city/person/address when the legal name fails. A related group's alias remains a lead.
3. **Prioritize requests by evidence strength.** Official domain and email hints first, then mapped subunit hints and verified aliases, then bounded name variants and public search. Record the next lead's expected value from previous measured yield. Distinguish DNS absence, refusals, transport failure and identity rejection; their retry schedules differ.
4. **Expand structured-data discovery on verified sites.** Collect legal names, identifiers, addresses, contact points, canonical links, social declarations, feeds, sitemaps and careers endpoints. Reverse-match identifiers before adding newly mentioned companies to the identity graph. A customer or subsidiary listing does not establish ownership of the whole site.
5. **Add public ATS readers for evidenced boards.** Derive board tokens from captured careers links on the exact company's own site. Greenhouse documents public GET access to its job board data; Lever documents published postings and individual posting retrieval. Fetch and retain specific postings; check employer and location scope before assigning them to the Norwegian legal entity. Board/profile links remain hiring leads. [Greenhouse documentation](https://docs.greenhouse.io/job-board.html), [Lever documentation](https://github.com/lever/postings-api).
6. **Cache public feeds as global indexes.** NAV is the first implementation. Give every index a coverage window, source time, checksum, completeness marker and expiry. Re-match new batches locally, then refresh selected detail records. An incomplete index must never become a cached negative finding.
7. **Maintain a failure and opportunity ledger.** Report which connector failed, why it failed, what was actually tried, when to retry, and whether the source lacks a demonstrated opportunity. Missing evidence must not become zero hiring, negative sentiment or an assertion that no website exists.

## Sources requiring additional investigation

Norid offers organisation/domain lookup services, but the published lookup terms prohibit commercial use and copying significant portions of the directory. Its RDAP documentation limits anonymous searches to nameservers; authenticated searches are registrar-scoped. Exclude Norid directory harvesting from this commercial discovery pipeline under the published terms. A public organisation lookup screen is not a free bulk organisation-to-all-domains API. [Lookup service](https://www.norid.no/en/domeneoppslag/), [directory terms](https://www.norid.no/en/domeneoppslag/vilkar/), [RDAP access documentation](https://teknisk.norid.no/en/integrere-mot-norid/rdap-tjenesten/).

## Source investigation on 2026-11-04

- The local keyless crawler pool has 139,298 entries, 127 syntactically invalid entries, and 139,163 unique normalized valid hosts. Of those hosts, 139,037 already occur in the primary website candidate maps or official entity/subunit indexes. Only 126 are additional hosts in that comparison, before checking the directory and learned-site maps. This is an offline syntax/overlap result, not 126 new companies or verified websites. Do not crawl the entire pool again. Audit: `out/discovery-source-audit-20261104.json`.
- Brønnøysund supports full dataset downloads and entity/subunit update feeds. Maintain the shared register index and use evidenced subunit brands as discovery leads. These sources are already partly integrated; measure incremental recovery rather than claim a new source just because it was refreshed. [Open-data documentation](https://data.brreg.no/enhetsregisteret/api/dokumentasjon/en/index.html).
- Common Crawl's free URL index can recover captured paths for candidate domains, including contact, legal and careers pages. It accepts URL patterns, not arbitrary organisation-number full-text queries. Use a bounded experiment on unresolved known domains, then fetch current pages and check identity; archived URLs do not establish current facts. Bulk filtering belongs on the columnar index rather than repeated broad index-server queries. [Index documentation](https://index.commoncrawl.org/).
- Next bounded experiment: classify failed existing candidate leads, improve evidenced operating-name queries and inspect the 126 additional pool hosts locally for relevant name matches. Compare against an unchanged baseline on the same frozen inputs, with the same cache policy. Separately test public ATS readers derived from verified careers links. Report marginal accepted evidence, false matches and all additional transport requests for each source.

Reviews, platform engagement, workforce and sentiment require actual source evidence and source-specific qualification. The user selected available public sources for now. Cached directory ratings, a company profile link, a job-board button and company-owned promotional news cannot substitute for these categories.

## Keep/drop rule

For each experiment, freeze the input and source policy, measure exact evidence gained and transport spent, inspect all new acceptances, and check a new disjoint batch. Retain a gain only when it preserves attribution and provenance. Stop sources that repeatedly consume requests without qualified evidence. Keep raw native counts visible so evaluator policy changes cannot be presented as retrieval improvements.
