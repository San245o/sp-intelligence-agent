import csv
import concurrent.futures
import gzip
import hashlib
import json
import os
import sys
import ssl
import urllib.request
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from signalpost.identity import assess_identity, IdentitySignals, is_directory_host

_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE

def clean_domain(url):
    if not url: return ""
    u = url.strip().lower()
    if u.startswith("https://"): u = u[8:]
    if u.startswith("http://"): u = u[7:]
    u = u.split("/")[0].split("?")[0].replace("www.", "").strip()
    return u

def fetch_and_hash(url: str, timeout: float = 3.5):
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"}
    )
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
        final_url = resp.geturl()
        raw = resp.read(300_000)
        h = hashlib.sha256(raw).hexdigest()
        try:
            html = raw.decode("utf-8")
        except UnicodeDecodeError:
            html = raw.decode("latin1", errors="replace")
        return final_url, html, h

def verify_single_row(row, univ):
    org = row["Organisation Number"].strip()
    name = row["Company Name"].strip()
    url = row["Official Website URL"].strip()
    status = row["Domain Status"].strip()

    if not url or status != "verified":
        return (org, name, url, "SKIPPED_NOT_FOUND", 0.0, None, None)

    profile = univ.get(org, {"organisation_number": org, "name": name})

    try:
        final_url, html, sha256 = fetch_and_hash(url)
    except Exception as e:
        err_msg = str(e).splitlines()[0][:35]
        return (org, name, url, f"FETCH_ERR: {err_msg}", 0.0, None, None)

    final_dom = clean_domain(final_url)
    title_m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    title = re.sub(r"<[^>]+>", " ", title_m.group(1)).strip() if title_m else ""
    desc_m = re.search(r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']*)["\']', html, re.I)
    meta_desc = desc_m.group(1).strip() if desc_m else ""
    footer_m = re.findall(r'<footer\b[^>]*>(.*?)</footer>', html, re.I | re.S)
    footer = " ".join(re.sub(r"<[^>]+>", " ", f) for f in footer_m)
    body_clean = re.sub(r"<[^>]+>", " ", html)[:10000]

    signals = IdentitySignals(
        hostname=final_dom,
        title=title,
        meta_description=meta_desc,
        footer_text=footer[:3000],
        contact_text=footer[:3000],
        body_text=body_clean,
    )

    verdict = assess_identity(profile, signals, source_url=final_url)

    if verdict.publishable:
        clean_target_url = f"https://{final_dom}"
        return (org, name, clean_target_url, f"VERIFIED (score={verdict.score})", verdict.score, sha256, clean_target_url)
    else:
        reason = verdict.reasons[0] if verdict.reasons else "score_too_low"
        return (org, name, url, f"REJECTED: {reason[:30]}", verdict.score, sha256, None)

def main():
    csv_path = ROOT / "data" / "ai_studio_batch_input.csv"
    univ_web_path = ROOT / "data" / "universe-websites.json"

    with open(univ_web_path, "r", encoding="utf-8") as f:
        universe_websites = json.load(f)

    univ = {}
    with gzip.open("company-universe.jsonl.gz", "rt", encoding="utf-8") as f:
        for line in f:
            d = json.loads(line)
            univ[d["organisation_number"]] = d

    rows = []
    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for r in reader:
            if r.get("Organisation Number"):
                rows.append(r)

    print(f"Ingesting {len(rows)} resolutions from AI Studio with 25 concurrent workers...")

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=25) as executor:
        futures = {executor.submit(verify_single_row, r, univ): r for r in rows}
        for future in concurrent.futures.as_completed(futures):
            results.append(future.result())

    # Sort results by org
    results.sort(key=lambda x: x[0])

    newly_added = 0
    already_cached = 0
    passed_count = 0
    rejected_count = 0

    for org, name, target, verdict, score, sha256, clean_url in results:
        if clean_url:
            passed_count += 1
            if org in universe_websites:
                already_cached += 1
            else:
                universe_websites[org] = clean_url
                newly_added += 1
        else:
            rejected_count += 1

    # Save cache updates
    with open(univ_web_path, "w", encoding="utf-8") as f:
        json.dump(universe_websites, f, indent=2)

    print("\n" + "=" * 80)
    print("AI STUDIO INGESTION & STRICT IDENTITY VERIFICATION REPORT")
    print("=" * 80)
    print(f"Total AI Studio Resolutions Processed: {len(results)}")
    print(f"  Passed Strict Identity Gate (score >= 0.92): {passed_count} ({passed_count/len(results)*100:.1f}%)")
    print(f"  Rejected (Subsidiary/Ambiguous/Fetch error):   {rejected_count}")
    print(f"  Newly Added to universe-websites.json:        {newly_added}")
    print(f"  Already Present in universe-websites.json:    {already_cached}")
    print(f"Final Total in universe-websites.json:          {len(universe_websites):,}")
    print("=" * 80 + "\n")

    print(f"{'Org Nr':10} | {'Company Name':30} | {'Verdict':30} | {'Live SHA-256'}")
    print("-" * 95)
    for org, name, target, verdict, score, sha256, clean_url in results[:35]:
        sha_str = sha256[:16] + "..." if sha256 else "None"
        print(f"{org:10} | {name[:30]:30} | {verdict[:30]:30} | {sha_str}")
    if len(results) > 35:
        print(f"... and {len(results)-35} more companies processed.")

if __name__ == "__main__":
    main()
