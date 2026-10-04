import json
import gzip
import re
import urllib.request
import ssl
import socket
import concurrent.futures
from collections import Counter
from pathlib import Path
import sys

socket.setdefaulttimeout(3.5)
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from signalpost.identity import assess_identity, IdentitySignals, registrable_domain

univ = {}
with gzip.open("company-universe.jsonl.gz", "rt", encoding="utf-8") as f:
    for line in f:
        d = json.loads(line)
        univ[d["organisation_number"]] = d

official = json.load(open("agent/data/official_411k_domains.json", encoding="utf-8"))
univ_web = json.load(open("agent/data/universe-websites.json", encoding="utf-8"))

FREE_EMAIL_PROVIDERS = {
    "gmail.com", "googlemail.com", "hotmail.com", "hotmail.no", "outlook.com",
    "yahoo.com", "yahoo.no", "icloud.com", "live.com", "live.no", "me.com",
    "online.no", "broadpark.no", "start.no", "c2i.net", "frisurf.no", "spray.no",
    "getmail.no", "telenor.no", "protonmail.com", "proton.me", "mail.com",
    "inbox.com", "zoho.com", "aol.com", "bluezone.no", "epost.no", "cfl.no"
}

dom_counts = Counter()
org_to_dom = {}
for org, entry in official.items():
    if org in univ_web: continue
    e = entry.get("e", "").strip().lower()
    if not e: continue
    dom = e.split("@")[-1].split("/")[0].strip()
    if dom in FREE_EMAIL_PROVIDERS or "." not in dom: continue
    dom_counts[dom] += 1
    org_to_dom[org] = dom

single_1to1 = [(org, univ[org], dom) for org, dom in org_to_dom.items() if dom_counts[dom] == 1 and org in univ][:100]

def fetch_html(dom):
    for u in (f"https://www.{dom}", f"https://{dom}", f"http://www.{dom}", f"http://{dom}"):
        try:
            req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0 Signalpost/1.0"})
            with urllib.request.urlopen(req, timeout=3.0, context=_SSL_CTX) as resp:
                raw = resp.read(250_000)
                try: html = raw.decode("utf-8")
                except UnicodeDecodeError: html = raw.decode("latin1", errors="replace")
                return resp.geturl(), html
        except Exception:
            pass
    return None, None

def check(cand):
    org, profile, dom = cand
    final_url, html = fetch_html(dom)
    if not html: return None
    
    title_m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    title = re.sub(r"<[^>]+>", " ", title_m.group(1)).strip() if title_m else ""
    footer_m = re.findall(r"<footer\b[^>]*>(.*?)</footer>", html, re.I | re.S)
    footer = " ".join(re.sub(r"<[^>]+>", " ", f) for f in footer_m)
    contact_m = re.findall(r"<(?:div|section|address)\b[^>]*(?:class|id)=[\"'][^\"']*(?:kontakt|contact|impressum|footer|address)[^\"']*[\"'][^>]*>(.*?)</(?:div|section|address)>", html, re.I | re.S)
    contact = " ".join(re.sub(r"<[^>]+>", " ", c) for c in contact_m[:4])
    body = re.sub(r"<[^>]+>", " ", html)[:10000]

    signals = IdentitySignals(
        hostname=registrable_domain(final_url),
        title=title,
        footer_text=footer[:3000],
        contact_text=contact[:3000],
        body_text=body,
    )
    
    p = dict(profile)
    off_entry = official.get(org) or {}
    if off_entry.get("p"):
        p["phone"] = off_entry["p"]
        
    verdict = assess_identity(p, signals, source_url=final_url)
    if verdict.publishable:
        return (org, profile["name"], final_url, verdict.score)
    return None

if __name__ == "__main__":
    print(f"Testing {len(single_1to1)} single-org statutory corporate email domains...")
    with concurrent.futures.ThreadPoolExecutor(max_workers=40) as ex:
        results = [r for r in ex.map(check, single_1to1) if r]

    print(f"\nResults for 100 1-to-1 statutory email domains:")
    print(f"  • Passed strict identity gate: {len(results)} ({len(results)/len(single_1to1)*100:.1f}%)")
    for r in results[:10]:
        print(f"  • {r[0]} | {r[1]} -> {r[2]} (score: {r[3]})")
