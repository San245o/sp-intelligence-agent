"""Compatibility entry: reuse the imported pipeline with the existing manifest/output CLI."""
from __future__ import annotations
import argparse,json,runpy,shutil,sys,os
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT)]
from scripts.run_batch import _load_manifest
from norway_company_agent.external_footprint import publishable_observation


def contract_envelope(raw):
    profile=raw['profile']; claims=[]; sources=[]
    statuses={'not_found':'not_available','source_error':'failed','budget_exhausted':'failed','not_fetched':'failed'}
    def add(field,value,status,record,span='',confidence=1.0):
        digest=record.get('content_sha256') or record.get('snapshot_sha256')
        good=bool(record.get('source_url') and record.get('retrieved_at') and len(str(digest or ''))==64)
        availability=statuses.get(status,status)
        if availability not in {'available','not_available','blocked','not_applicable','ambiguous','failed'}:availability='failed'
        if availability=='available' and not good:availability='ambiguous'
        ids=[]
        if good:
            eid=f"ev-{len(sources)+1}-{profile['organisation_number']}"
            sources.append({'id':eid,'source_url':record['source_url'],'source_class':record.get('source_class') or record.get('source_type') or 'captured_source','retrieved_at':record['retrieved_at'],'content_sha256':digest,'claim_span':span or str(record.get('note') or field),'snapshot_path':record.get('snapshot_path')})
            ids=[eid]
        claims.append({'field':field,'value':value if availability=='available' else None,'availability':availability,'confidence':confidence if availability=='available' else 0.0,'evidence_ids':ids})
    records = profile.get('evidence', {})
    registry = records.get('registry') or {}
    live = records.get('registry_live') or {}
    registry_values = registry.get('value') or {}
    for field, key in [('legal_name','name'),('legal_form','legal_form'),('employee_count','employees'),('industry','industry_code')]:
        value = profile.get(key)
        add(field, value, 'available' if value is not None else 'not_available', registry, str(value))
    address = (live.get('value') or {}).get('business_address')
    if address:
        add('registered_business_address', address, live.get('status'), live, str(address))
    else:
        address = {key: registry_values.get('forretningsadresse.' + key) for key in ('adresse','postnummer','poststed','kommune','land')}
        add('registered_business_address', address, 'available' if address.get('adresse') else 'not_available', registry, str(address))
    if all(key in profile for key in ('bankrupt','liquidating')):
        add('bankruptcy_or_liquidation_status', {key:profile[key] for key in ('bankrupt','liquidating')}, 'available', registry)
    accounting = records.get('accounting_obligation') or {}
    add('accounting_obligation_status', accounting.get('value'), accounting.get('status','not_available'), accounting)
    financial = records.get('financials') or {}
    accounts = (financial.get('value') or {}).get('records') or []
    account = next((row for row in accounts if row.get('account_type') == 'SELSKAP'), {})
    for field, key in [('latest_annual_revenue','revenue'),('latest_annual_result','annual_result'),('latest_total_assets','assets'),('latest_equity','equity')]:
        amount = account.get(key)
        add(field, amount, financial.get('status') if amount is not None else 'not_available', financial, str({'value':amount,'currency':account.get('currency'),'period':account.get('period')}))
    for field, key, subkey in [('leadership','roles','roles'),('business_locations','locations','locations'),('group_structure','group',None)]:
        record = records.get(key) or {}
        value = record.get('value')
        if subkey: value = (value or {}).get(subkey)
        add(field, value, record.get('status','not_available'), record, str(value)[:2000])
    website = records.get('website') or {}
    value = website.get('value') or {}
    proof = value.get('identity_assessment') or {}
    website_status = 'ambiguous' if website.get('status') == 'available' and not proof.get('publishable') else website.get('status','not_available')
    add('official_website', value.get('final_url'), website_status, website, str(value.get('final_url') or ''), proof.get('score',0))
    category_fields = {'profile_handle':'social_profiles','place_summary':'ratings_and_reviews','review_summary':'ratings_and_reviews','review':'ratings_and_reviews','job_posting':'job_postings','workforce_snapshot':'external_workforce','profile_metrics':'buzz_or_engagement','buzz_metrics':'buzz_or_engagement','public_post':'dated_public_activity','public_mention':'dated_public_activity'}
    present = set()
    for obs in ((records.get('external_footprint') or {}).get('value') or {}).get('observations') or []:
        if not publishable_observation(obs): continue
        field = category_fields.get(obs.get('signal_type'), 'external_observation')
        add(field, obs, 'available', obs, obs.get('evidence_span',''))
        present.add(field)
    for field in set(category_fields.values()) - present:
        add(field, None, 'not_available', {})
    metric = profile.get('run_metrics') or {}
    credits = metric.get('paid_credits')
    rate = os.environ.get('SERPER_USD_PER_CREDIT')
    cost = 0.0 if credits == 0 else float(rate) * credits if rate and credits is not None else None
    operations = {'requests':metric.get('requests',0),'runtime_ms':metric.get('runtime_ms',0),'third_party_cost_usd':cost,'paid_credits':credits,'cost_basis':'Configured USD per credit' if rate else 'Zero credits' if credits == 0 else 'USD rate not supplied; credits reported'}
    errors = [{'message':profile['run_error']}] if profile.get('run_error') else []
    return {'organisation_number':profile['organisation_number'],'run':{'run_id':raw['run_id'],'started_at':raw['started_at'],'completed_at':raw['completed_at'],'terminal_status':'completed' if raw['state']!='submission_error' else 'failed'},'claims':claims,'evidence':sources,'changes':[],'errors':errors,'operations':operations}



def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest','--organisations','--input','-i',dest='manifest')
    parser.add_argument('--out','--output','-o',required=True)
    parser.add_argument('--workers','-w',type=int,default=8)
    parser.add_argument('--search',choices=['none','auto','serper'],default='auto')
    parser.add_argument('--paid-search-budget',type=int,default=40)
    parser.add_argument('--bulk',type=Path,default=ROOT/'data/brreg-enheter.csv.gz')
    parser.add_argument('--report',type=Path)
    parser.add_argument('--limit',type=int,default=0)
    arguments = list(sys.argv[1:] if argv is None else argv)
    positional_manifest = arguments.pop(0) if arguments and not arguments[0].startswith('-') else None
    args,extra=parser.parse_known_args(arguments)
    source=args.manifest or positional_manifest
    if not source:parser.error('Provide the evaluator manifest')
    rows=_load_manifest(source)
    if args.limit:rows=rows[:args.limit]
    if not rows:parser.error('Manifest contains no valid organisation numbers')
    requested=Path(args.out);directory=requested.parent if requested.suffix=='.jsonl' else requested
    directory.mkdir(parents=True,exist_ok=True)
    normalized=directory/'organisations.jsonl'
    normalized.write_text(''.join(json.dumps(row)+'\n' for row in rows),encoding='utf-8')
    if not args.bulk.exists():
        from scripts.refresh_brreg_company_cache import download
        args.bulk.parent.mkdir(parents=True,exist_ok=True)
        temporary=args.bulk.with_suffix(args.bulk.suffix+'.tmp')
        download(temporary,120,None);temporary.replace(args.bulk)
    report=args.report or directory/'run-report.json'
    command=[str(ROOT/'scripts/run_competition_batch.py'),'--organisations',str(normalized),'--bulk',str(args.bulk),'--output',str(directory/'raw_envelopes.jsonl'),'--profiles-output',str(directory/'profiles.jsonl'),'--report',str(report),'--run-id',directory.name,'--expected-count',str(len(rows)),'--workers',str(args.workers),'--paid-search-budget',str(0 if args.search=='none' else args.paid_search_budget),*extra]
    previous=sys.argv;sys.argv=command
    try:
        try:runpy.run_path(command[0],run_name='__main__')
        except SystemExit as exc:
            if exc.code not in (None,0):raise
    finally:sys.argv=previous
    raw=[json.loads(line) for line in (directory/'raw_envelopes.jsonl').read_text(encoding='utf-8').splitlines() if line]
    contracts=[contract_envelope(item) for item in raw]
    body=''.join(json.dumps(item,ensure_ascii=False)+'\n' for item in contracts)
    for path in {directory/'envelopes.jsonl',directory/'submission.jsonl',requested if requested.suffix=='.jsonl' else directory/'submission.jsonl'}:path.write_text(body,encoding='utf-8')
    return 0

if __name__=='__main__':raise SystemExit(main())
