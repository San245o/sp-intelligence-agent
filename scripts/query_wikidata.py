import urllib.request
import urllib.parse
import json

sparql = """
SELECT ?item ?itemLabel ?org_nr ?website ?article ?twitter ?facebook ?linkedin ?youtube ?instagram WHERE {
  ?item wdt:P2333 ?org_nr .
  OPTIONAL { ?item wdt:P856 ?website . }
  OPTIONAL { ?item wdt:P2002 ?twitter . }
  OPTIONAL { ?item wdt:P2013 ?facebook . }
  OPTIONAL { ?item wdt:P6674 ?linkedin . }
  OPTIONAL { ?item wdt:P2397 ?youtube . }
  OPTIONAL { ?item wdt:P2003 ?instagram . }
  OPTIONAL {
    ?article schema:about ?item ;
             schema:isPartOf <https://no.wikipedia.org/> .
  }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "nb,nn,no,en". }
}
"""

url = 'https://query.wikidata.org/sparql?query=' + urllib.parse.quote(sparql) + '&format=json'
req = urllib.request.Request(url, headers={'User-Agent': 'SignalpostResearch/1.0 (contact@signalpost.local)'})

try:
    with urllib.request.urlopen(req, timeout=45) as resp:
        data = json.loads(resp.read().decode('utf-8'))
        bindings = data['results']['bindings']
        print(f"Total rows retrieved from Wikidata: {len(bindings):,}")
        
        by_org = {}
        for b in bindings:
            org = b.get('org_nr', {}).get('value', '').strip()
            if not org or len(org) != 9 or not org.isdigit():
                continue
            if org not in by_org:
                by_org[org] = {
                    "org_nr": org,
                    "name": b.get('itemLabel', {}).get('value', ''),
                    "wikidata_url": b.get('item', {}).get('value', ''),
                    "wikipedia_no": b.get('article', {}).get('value', ''),
                    "website": b.get('website', {}).get('value', ''),
                    "socials": {}
                }
            entry = by_org[org]
            if not entry["website"] and b.get('website'):
                entry["website"] = b['website']['value']
            for soc_key in ('twitter', 'facebook', 'linkedin', 'youtube', 'instagram'):
                if b.get(soc_key) and soc_key not in entry["socials"]:
                    entry["socials"][soc_key] = b[soc_key]['value']

        print(f"Unique entities with valid 9-digit org nr: {len(by_org):,}")
        has_web = sum(1 for e in by_org.values() if e["website"])
        has_soc = sum(1 for e in by_org.values() if e["socials"])
        has_wiki = sum(1 for e in by_org.values() if e["wikipedia_no"])
        print(f"  • With official website: {has_web:,}")
        print(f"  • With social profiles (Twitter/FB/LI/YT/IG): {has_soc:,}")
        print(f"  • With Norwegian Wikipedia article: {has_wiki:,}")

        # Save to agent/data/wikidata_enriched.json
        out_path = "agent/data/wikidata_enriched.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(by_org, f, indent=2, ensure_ascii=False)
        print(f"Saved enriched Wikidata corpus to {out_path}!")

except Exception as e:
    print('Error querying Wikidata SPARQL:', e)
