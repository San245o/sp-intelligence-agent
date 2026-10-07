import concurrent.futures
import csv
import gzip
import json
import re
import socket
import ssl
import sys
import urllib.request
from pathlib import Path

# Cap socket connect/read timeout
socket.setdefaulttimeout(3.5)

# Add src to path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from signalpost.identity import (
    assess_identity,
    fold,
    is_directory_host,
    name_tokens,
    normalise_org_number,
    registrable_domain,
    IdentitySignals,
)

FOREIGN_TLDS = {
    ".nl", ".se", ".dk", ".is", ".de", ".uk", ".pl", ".fr", ".es", ".it", ".fi", ".ru", ".cn", ".in", ".be", ".ch", ".at"
}

TOXIC_HOSTS = {
    "obos.no", "usbl.no", "bate.no", "nbbo.no", "bori.no", "vestbo.no",
    "vibbo.no", "kvadratmeter.no", "mallingco.no", "hamar2hobbl.no",
    "kragero-bbl.no", "boligbyggelaget.no", "pk-eiendom.no"
}

_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE

def clean_key(s):
    return re.sub(r'[^a-z0-9]', '', fold(s))

def strip_legal(s):
    return ''.join([t for t in re.findall(r'[a-z0-9]+', fold(s)) if t not in {
        'as', 'asa', 'sb', 'da', 'enk', 'ba', 'ans', 'norge', 'norway', 'group', 'gruppen'
    }])

def clean_domain(url):
    if not url: return ""
    u = url.strip().lower()
    if u.startswith("https://"): u = u[8:]
    if u.startswith("http://"): u = u[7:]
    u = u.split("/")[0].split("?")[0].replace("www.", "").strip()
    return u

def fetch_html(url: str):
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"}
    )
    with urllib.request.urlopen(req, timeout=3.5, context=_SSL_CTX) as resp:
        final_url = resp.geturl()
        raw = resp.read(300_000)
        try:
            html = raw.decode("utf-8")
        except UnicodeDecodeError:
            html = raw.decode("latin1", errors="replace")
        return final_url, html

def verify_one_candidate(candidate):
    org, profile, domain, li_url = candidate
    
    final_url = None
    html = None
    
    # Try HTTPS first, then fallback to HTTP
    try:
        final_url, html = fetch_html(f"https://{domain}")
    except Exception:
        try:
            final_url, html = fetch_html(f"http://{domain}")
        except Exception:
            return None
            
    if not final_url or not html:
        return None
        
    final_dom = clean_domain(final_url)
    
    # 1. Guard against toxic hosts, directory hosts, and foreign ccTLDs
    if is_directory_host(final_url) or final_dom in TOXIC_HOSTS:
        return None
    parts = final_dom.split(".")
    if len(parts) >= 2 and "." + parts[-1] in FOREIGN_TLDS:
        return None
        
    # Extract regions
    title_m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    title = re.sub(r"<[^>]+>", " ", title_m.group(1)).strip() if title_m else ""
    
    desc_m = re.search(r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']*)["\']', html, re.I)
    meta_desc = desc_m.group(1).strip() if desc_m else ""
    
    footer_m = re.findall(r'<footer\b[^>]*>(.*?)</footer>', html, re.I | re.S)
    footer = " ".join(re.sub(r"<[^>]+>", " ", f) for f in footer_m)
    
    contact_m = re.findall(r'<(?:div|section|address)\b[^>]*(?:class|id)=["\'][^"\']*(?:kontakt|contact|impressum|footer|address)[^"\']*["\'][^>]*>(.*?)</(?:div|section|address)>', html, re.I | re.S)
    contact = " ".join(re.sub(r"<[^>]+>", " ", c) for c in contact_m[:4])
    
    body_clean = re.sub(r"<[^>]+>", " ", html)[:10000]
    
    signals = IdentitySignals(
        hostname=final_dom,
        title=title,
        meta_description=meta_desc,
        footer_text=footer[:3000],
        contact_text=contact[:3000],
        body_text=body_clean,
    )
    
    verdict = assess_identity(profile, signals, source_url=final_url)
    if verdict.publishable:
        return {
            "org": org,
            "domain": f"https://{final_dom}",
            "linkedin": li_url,
            "score": verdict.score,
            "status": verdict.status,
            "reasons": verdict.reasons,
            "proof_span": verdict.proof_span
        }
    return None

def main(limit: int = 1000, max_workers: int = 60):
    print(f"Loading universe & candidate records (limit={'ALL' if limit <= 0 else limit}, workers={max_workers})...", flush=True)
    univ = {}
    with gzip.open("company-universe.jsonl.gz", "rt", encoding="utf-8") as f:
        for line in f:
            d = json.loads(line)
            univ[d["organisation_number"]] = d

    by_clean = {clean_key(d["name"]): d for d in univ.values()}
    by_stripped = {strip_legal(d["name"]): d for d in univ.values() if len(strip_legal(d["name"])) >= 4}

    univ_web_path = "agent/data/universe-websites.json"
    with open(univ_web_path, "r", encoding="utf-8") as f:
        existing_websites = json.load(f)

    # Load LinkedIn profiles dataset
    li_profiles_path = "agent/data/universe_linkedin_profiles.json"
    linkedin_profiles = {}
    if Path(li_profiles_path).exists():
        try:
            with open(li_profiles_path, "r", encoding="utf-8") as f:
                linkedin_profiles = json.load(f)
        except Exception:
            pass

    # Load official 411k domains and phones
    with open("agent/data/official_411k_domains.json", "r", encoding="utf-8") as f:
        official_domains = json.load(f)

    # Load persistent rejected candidates set
    rejections_path = "agent/data/tested_domain_rejections.json"
    rejected_orgs = set()
    if Path(rejections_path).exists():
        try:
            with open(rejections_path, "r", encoding="utf-8") as f:
                rejected_orgs = set(json.load(f))
        except Exception:
            pass
    print(f"Loaded {len(existing_websites):,} existing websites and {len(rejected_orgs):,} previous rejections.", flush=True)

    candidate_list = []
    seen_orgs = set()
    statutory_added = 0

    with open("agent/data/linkedin_norway_59k.csv", "r", encoding="utf-8", errors="replace") as f:
        for r in csv.DictReader(f):
            w = clean_domain(r.get("website"))
            if not w: continue
            
            parts = w.split(".")
            if len(parts) >= 2 and "." + parts[-1] in FOREIGN_TLDS:
                continue
            if w in TOXIC_HOSTS or is_directory_host(w):
                continue
                
            cname = r.get("name", "")
            hit = by_clean.get(clean_key(cname)) or by_stripped.get(strip_legal(cname))
            if hit:
                org = hit["organisation_number"]
                if org in seen_orgs or org in existing_websites or org in rejected_orgs:
                    continue
                seen_orgs.add(org)

                # Check if statutory match with official registry
                off_entry = official_domains.get(org) or {}
                off_dom = clean_domain(off_entry.get("e") or off_entry.get("h"))
                if off_dom and off_dom == w:
                    existing_websites[org] = f"https://{w}"
                    statutory_added += 1
                    continue

                # Enrich profile with phone and municipality
                enriched_profile = dict(hit)
                if off_entry.get("p"):
                    enriched_profile["phone"] = off_entry["p"]

                candidate_list.append((org, enriched_profile, w, r.get("linkedin_url", "")))
                if limit > 0 and len(candidate_list) >= limit:
                    break

    if statutory_added > 0:
        print(f"Added {statutory_added} statutory official registry matches immediately.", flush=True)
        with open(univ_web_path, "w", encoding="utf-8") as f:
            json.dump(existing_websites, f, indent=2)

    print(f"Testing {len(candidate_list)} unproven candidates through live assess_identity()...", flush=True)
    if not candidate_list:
        print("No new candidates to test!")
        return
    
    verified_results = []
    new_rejections = set()
    processed_count = 0
    checkpoint_every = 100

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(verify_one_candidate, c): c for c in candidate_list}
        for future in concurrent.futures.as_completed(futures):
            processed_count += 1
            cand = futures[future]
            cand_org = cand[0]
            res = future.result()
            if res:
                verified_results.append(res)
                org = res["org"]
                existing_websites[org] = res["domain"]
                if org not in linkedin_profiles and res.get("linkedin"):
                    linkedin_profiles[org] = {
                        "org_nr": org,
                        "name": univ[org]["name"],
                        "website": res["domain"],
                        "linkedin_url": res["linkedin"],
                        "match_rule": "live_identity_gate_passed"
                    }
            else:
                new_rejections.add(cand_org)

            if processed_count % checkpoint_every == 0 or processed_count == len(candidate_list):
                print(f"[{processed_count}/{len(candidate_list)}] Tested. Verified: {len(verified_results)} ({len(verified_results)/processed_count*100:.1f}%), Rejected: {len(new_rejections)}", flush=True)
                # Save progress checkpoint
                with open(univ_web_path, "w", encoding="utf-8") as f:
                    json.dump(existing_websites, f, indent=2)
                with open(li_profiles_path, "w", encoding="utf-8") as f:
                    json.dump(linkedin_profiles, f, indent=2, ensure_ascii=False)
                rejected_orgs.update(new_rejections)
                with open(rejections_path, "w", encoding="utf-8") as f:
                    json.dump(sorted(list(rejected_orgs)), f)

    print(f"\nFinal Verification Results:")
    print(f"  • Total Candidates Tested: {len(candidate_list)}")
    print(f"  • Passed Strict Identity Gate: {len(verified_results)}")
    if candidate_list:
        print(f"  • Pass Rate: {len(verified_results)/len(candidate_list)*100:.1f}%")
    print(f"  • Final Total in universe-websites.json: {len(existing_websites):,}")

if __name__ == "__main__":
    lim = int(sys.argv[1]) if len(sys.argv) > 1 else 1000
    w = int(sys.argv[2]) if len(sys.argv) > 2 else 60
    main(lim, w)
