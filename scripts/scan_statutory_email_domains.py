import concurrent.futures
from collections import Counter
import gzip
import json
from pathlib import Path
import re
import socket
import ssl
import sys
import urllib.request

socket.setdefaulttimeout(3.0)
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from signalpost.identity import (
    assess_identity,
    IdentitySignals,
    registrable_domain,
    fold,
    is_directory_host
)

FREE_EMAIL_PROVIDERS = {
    "gmail.com", "googlemail.com", "hotmail.com", "hotmail.no", "outlook.com",
    "yahoo.com", "yahoo.no", "icloud.com", "live.com", "live.no", "me.com",
    "online.no", "broadpark.no", "start.no", "c2i.net", "frisurf.no", "spray.no",
    "getmail.no", "telenor.no", "protonmail.com", "proton.me", "mail.com",
    "inbox.com", "zoho.com", "aol.com", "bluezone.no", "epost.no", "cfl.no",
    "lyse.net", "haugnett.no", "enivest.net", "ebnett.no", "vikenfiber.no", "mac.com"
}

FOREIGN_TLDS = {
    ".nl", ".se", ".dk", ".is", ".de", ".uk", ".pl", ".fr", ".es", ".it",
    ".fi", ".ru", ".cn", ".in", ".be", ".ch", ".at", ".cz", ".hu", ".ro"
}

TOXIC_HOSTS = {
    "styrerommet.no", "usbl.no", "vestbo.no", "bate.no", "bori.no", "kirken.no",
    "obos.no", "skytterlag.no", "bbnett.no", "vibbo.no", "kvadratmeter.no",
    "mallingco.no", "hamar2hobbl.no", "kragero-bbl.no", "boligbyggelaget.no",
    "pk-eiendom.no", "nbbo.no"
}

def clean_domain(url: str) -> str:
    if not url: return ""
    u = url.strip().lower()
    if u.startswith("https://"): u = u[8:]
    if u.startswith("http://"): u = u[7:]
    return u.split("/")[0].split("?")[0].replace("www.", "").strip()

def fetch_html(dom: str):
    urls = [f"https://www.{dom}", f"https://{dom}", f"http://www.{dom}", f"http://{dom}"]
    for u in urls:
        try:
            req = urllib.request.Request(
                u,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Signalpost/1.0"}
            )
            with urllib.request.urlopen(req, timeout=3.0, context=_SSL_CTX) as resp:
                final_url = resp.geturl()
                final_dom = clean_domain(final_url)
                if is_directory_host(final_url) or final_dom in TOXIC_HOSTS:
                    return None, None
                parts = final_dom.split(".")
                if len(parts) >= 2 and "." + parts[-1] in FOREIGN_TLDS:
                    return None, None
                raw = resp.read(250_000)
                try:
                    html = raw.decode("utf-8")
                except UnicodeDecodeError:
                    html = raw.decode("latin1", errors="replace")
                return final_url, html
        except Exception:
            pass
    return None, None

def verify_candidate(candidate):
    org, profile, dom, phone = candidate
    final_url, html = fetch_html(dom)
    if not html:
        return None

    title_m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    title = re.sub(r"<[^>]+>", " ", title_m.group(1)).strip() if title_m else ""
    
    desc_m = re.search(r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']*)["\']', html, re.I)
    meta_desc = desc_m.group(1).strip() if desc_m else ""
    
    footer_m = re.findall(r"<footer\b[^>]*>(.*?)</footer>", html, re.I | re.S)
    footer = " ".join(re.sub(r"<[^>]+>", " ", f) for f in footer_m)
    
    contact_m = re.findall(r"<(?:div|section|address)\b[^>]*(?:class|id)=[\"'][^\"']*(?:kontakt|contact|impressum|footer|address)[^\"']*[\"'][^>]*>(.*?)</(?:div|section|address)>", html, re.I | re.S)
    contact = " ".join(re.sub(r"<[^>]+>", " ", c) for c in contact_m[:4])
    
    body = re.sub(r"<[^>]+>", " ", html)[:10000]

    signals = IdentitySignals(
        hostname=registrable_domain(final_url),
        title=title,
        meta_description=meta_desc,
        footer_text=footer[:3000],
        contact_text=contact[:3000],
        body_text=body,
    )

    enriched = dict(profile)
    if phone:
        enriched["phone"] = phone

    verdict = assess_identity(enriched, signals, source_url=final_url)
    if verdict.publishable:
        final_dom = clean_domain(final_url)
        return {
            "org": org,
            "domain": f"https://{final_dom}",
            "score": verdict.score,
            "status": verdict.status,
            "reasons": verdict.reasons,
            "proof_span": verdict.proof_span
        }
    return None

def main(limit: int = 0, max_workers: int = 80):
    print(f"Loading 411k universe, official domains, and previous records...", flush=True)
    univ = {}
    with gzip.open("company-universe.jsonl.gz", "rt", encoding="utf-8") as f:
        for line in f:
            d = json.loads(line)
            univ[d["organisation_number"]] = d

    official_path = "agent/data/official_411k_domains.json"
    with open(official_path, "r", encoding="utf-8") as f:
        official = json.load(f)

    univ_web_path = "agent/data/universe-websites.json"
    with open(univ_web_path, "r", encoding="utf-8") as f:
        existing_websites = json.load(f)

    rejections_path = "agent/data/tested_domain_rejections.json"
    rejected_orgs = set()
    if Path(rejections_path).exists():
        try:
            with open(rejections_path, "r", encoding="utf-8") as f:
                rejected_orgs = set(json.load(f))
        except Exception:
            pass

    print(f"Loaded {len(existing_websites):,} existing websites and {len(rejected_orgs):,} previous rejections.", flush=True)

    # Count domain frequency to ensure we test true 1-to-1 company domains first
    dom_counts = Counter()
    org_to_dom = {}
    org_to_phone = {}

    for org, entry in official.items():
        if org in existing_websites or org in rejected_orgs or org not in univ:
            continue
        e = entry.get("e", "").strip().lower()
        if not e: continue
        dom = e.split("@")[-1].split("/")[0].strip()
        if dom in FREE_EMAIL_PROVIDERS or "." not in dom or dom in TOXIC_HOSTS:
            continue
        parts = dom.split(".")
        if len(parts) >= 2 and "." + parts[-1] in FOREIGN_TLDS:
            continue
        dom_counts[dom] += 1
        org_to_dom[org] = dom
        if entry.get("p"):
            org_to_phone[org] = entry["p"]

    # Candidate selection: dedicated 1-to-1 domains
    candidate_list = []
    for org, dom in org_to_dom.items():
        if dom_counts[dom] == 1:
            candidate_list.append((org, univ[org], dom, org_to_phone.get(org)))
            if limit > 0 and len(candidate_list) >= limit:
                break

    print(f"Total dedicated 1-to-1 statutory email domain candidates: {len(candidate_list):,}", flush=True)
    if not candidate_list:
        print("No new candidates to test!")
        return

    verified_results = []
    new_rejections = set()
    processed_count = 0
    checkpoint_every = 100

    print(f"Starting parallel verification (workers={max_workers})...", flush=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(verify_candidate, c): c for c in candidate_list}
        for future in concurrent.futures.as_completed(futures):
            processed_count += 1
            cand = futures[future]
            cand_org = cand[0]
            res = future.result()
            if res:
                verified_results.append(res)
                existing_websites[cand_org] = res["domain"]
            else:
                new_rejections.add(cand_org)

            if processed_count % checkpoint_every == 0 or processed_count == len(candidate_list):
                print(f"[{processed_count}/{len(candidate_list)}] Tested. Verified: {len(verified_results)} ({len(verified_results)/processed_count*100:.1f}%), Rejected: {len(new_rejections)}", flush=True)
                # Checkpoint
                with open(univ_web_path, "w", encoding="utf-8") as f:
                    json.dump(existing_websites, f, indent=2)
                rejected_orgs.update(new_rejections)
                with open(rejections_path, "w", encoding="utf-8") as f:
                    json.dump(sorted(list(rejected_orgs)), f)

    print(f"\nFinal Email Domain Verification Results:")
    print(f"  • Total Candidates Tested: {len(candidate_list)}")
    print(f"  • Passed Strict Identity Gate: {len(verified_results)}")
    if candidate_list:
        print(f"  • Pass Rate: {len(verified_results)/len(candidate_list)*100:.1f}%")
    print(f"  • Grand Total in universe-websites.json: {len(existing_websites):,}")

if __name__ == "__main__":
    lim = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    w = int(sys.argv[2]) if len(sys.argv) > 2 else 80
    main(lim, w)
