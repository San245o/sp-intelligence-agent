"""Controlled news recovery on an existing cohort; no paid/search API calls.

Reuses hash-verified captures where available, then fetches public company pages.
This is a development before/after test, not a fresh paired competition run.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from norway_company_agent import site_discovery as activity
from norway_company_agent.identity import apply_website_identity_gate
from norway_company_agent.external_collectors import aggregate_footprint, coverage_summary
from norway_company_agent.source_store import DEFAULT_SOURCE_DIR


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--site-request-cap', type=int, default=16)
    args = parser.parse_args()
    destination = Path(args.output)
    destination.mkdir(parents=True, exist_ok=True)
    rows = [json.loads(line) for line in Path(args.input).read_text(encoding='utf-8').splitlines() if line]
    original_read = activity._read
    captures = {}
    for profile in rows:
        value = profile.get('evidence', {}).get('website', {}).get('value') or {}
        for page in [value, *(value.get('pages') or [])]:
            url = page.get('url') or page.get('final_url')
            digest = page.get('content_sha256')
            if url and digest:
                captures[url] = (page.get('snapshot_path') or str(DEFAULT_SOURCE_DIR / (digest + '.bin')), digest, page.get('retrieved_at') or profile['evidence']['website'].get('retrieved_at'))
        for source in profile.get('site_activity', {}).get('feeds_and_sitemaps') or []:
            digest = source.get('content_sha256')
            if digest:
                captures[source['url']] = (str(DEFAULT_SOURCE_DIR / (digest + '.bin')), digest, profile['evidence']['website'].get('retrieved_at'))

    def cached_read(url, domain, limit, timeout):
        if url in captures:
            path, digest, retrieved_at = captures[url]
            try:
                raw = Path(path).read_bytes()
                if len(raw) <= limit and hashlib.sha256(raw).hexdigest() == digest:
                    return raw, {'requests': 0, 'bytes': 0, 'latencies_ms': [], 'outcome': 'retained_capture', 'content_sha256': digest, 'retrieved_at': retrieved_at}
            except OSError:
                pass
        return original_read(url, domain, limit, timeout)

    def enrich(profile):
        record = profile.get('evidence', {}).get('website') or {}
        if record.get('value'):
            record['value'].setdefault('retrieved_at', record.get('retrieved_at'))
        before = activity.site_news_observations(profile)
        audit = {'organisation_number': profile['organisation_number'], 'name': profile['name'], 'before_news': len(before), 'public_requests': 0}
        if record.get('status') != 'available':
            return profile, audit
        gated = apply_website_identity_gate(profile, record, candidate_source='retained_same_cohort_validation')
        profile['evidence']['website'] = gated['website']
        identity = gated['assessment']
        if not identity.get('publishable') or not identity.get('content_attributable'):
            audit['ownership_withheld'] = identity.get('reasons')
            return profile, audit
        used = 0
        def reserve(cost): return used + cost <= args.site_request_cap
        def charge(metrics):
            nonlocal used
            used += metrics['requests']
        profile['site_activity'] = activity.discover_site_activity(record['value'], reserve=reserve, charge=charge)
        new = activity.site_news_observations(profile)
        old = ((profile.get('evidence', {}).get('external_footprint') or {}).get('value') or {}).get('observations') or []
        observations = list({item['id']: item for item in [*old, *new]}.values())
        profile.setdefault('evidence', {}).setdefault('external_footprint', {}).setdefault('value', {})['observations'] = observations
        profile['evidence']['external_footprint']['value'].update(accepted_observations=len(observations), platforms=sorted({o['platform'] for o in observations}))
        profile['external_coverage'] = coverage_summary(observations)
        profile['external_aggregate'] = aggregate_footprint(observations)
        audit.update(after_news=len(new), public_requests=used, retained_reads=sum(a.get('outcome') == 'retained_capture' for a in profile['site_activity']['source_attempts']),
                     new_urls=sorted({o['source_url'] for o in new} - {o['source_url'] for o in before}))
        print(json.dumps(audit, ensure_ascii=False), flush=True)
        return profile, audit

    results = {}
    audits = []
    with patch.object(activity, '_read', side_effect=cached_read):
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            for future in as_completed([pool.submit(enrich, p) for p in rows]):
                profile, audit = future.result()
                results[profile['organisation_number']] = profile
                audits.append(audit)
    output = [results[p['organisation_number']] for p in rows]
    (destination / 'profiles.jsonl').write_text(''.join(json.dumps(p, ensure_ascii=False)+'\n' for p in output), encoding='utf-8')
    report = {'profiles': len(rows), 'same_membership': set(results) == {p['organisation_number'] for p in rows},
              'paid_api_calls': 0, 'before_news_items': sum(a['before_news'] for a in audits),
              'after_news_items': sum(a.get('after_news', a['before_news']) for a in audits),
              'after_news_companies': sum(a.get('after_news', a['before_news']) > 0 for a in audits),
              'public_requests': sum(a['public_requests'] for a in audits), 'companies': sorted(audits, key=lambda a: a['organisation_number']),
              'boundary': 'Controlled development recovery on the frozen previous 100. Uses retained captures and additional public fetches; reference output unchanged. Not a fresh paired benchmark.'}
    (destination / 'recovery-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps({k: v for k, v in report.items() if k != 'companies'}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
