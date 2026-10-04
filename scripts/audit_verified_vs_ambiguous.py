import json

envs = [json.loads(l) for l in open('agent/runs/eval-100-b/envelopes.jsonl', encoding='utf-8') if l.strip()]
univ_web = json.load(open('agent/data/universe-websites.json', encoding='utf-8'))

strictly_verified = []
ambiguous_or_unverified = []

for e in envs:
    org = e['organisation_number']
    name = e.get('input_name')
    web_claim = next((c for c in e['claims'] if c['field'] == 'website'), {})
    reg_val = next((c.get('value') for c in e['claims'] if c['field'] == 'website_registry'), None)
    
    val = web_claim.get('value')
    avail = web_claim.get('availability')
    in_univ = univ_web.get(org)
    
    if in_univ:
        strictly_verified.append((org, name, in_univ, 'universe_verified'))
    elif avail == 'available' and val:
        strictly_verified.append((org, name, val, 'gate_verified'))
    elif reg_val:
        ambiguous_or_unverified.append((org, name, reg_val, avail, web_claim.get('note')))

print(f"STRICTLY VERIFIED WEBSITES: {len(strictly_verified)}")
print(f"AMBIGUOUS / UNVERIFIED REGISTRY STRINGS: {len(ambiguous_or_unverified)}")

print("\nAmbiguous / Unverified examples (Declared in registry, but failed or quarantined by Identity Gate):")
for x in ambiguous_or_unverified:
    print(f"  • {x[1]} ({x[0]}): registry='{x[2]}', gate_status={x[3]}")
