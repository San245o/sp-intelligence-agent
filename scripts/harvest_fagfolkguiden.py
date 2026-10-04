#!/usr/bin/env python3
"""Harvest verified Norwegian business websites and Google aggregate ratings from Fagfolkguiden.

Extracts structured JSON-LD data:
- Verified websites (sameAs) -> adds to universe-websites.json if passing identity rules
- Google aggregate ratings (ratingValue, ratingCount) -> saved to agent/data/universe_ratings_reviews.json
- Contact details and descriptions
"""
import concurrent.futures
import json
from pathlib import Path
import re
import socket
import ssl
import sys
import time
import urllib.request
from bs4 import BeautifulSoup

socket.setdefaulttimeout(6.0)
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
}

FOREIGN_TLDS = {
    ".nl", ".se", ".dk", ".is", ".de", ".uk", ".pl", ".fr", ".es", ".it",
    ".fi", ".ru", ".cn", ".in", ".be", ".ch", ".at", ".cz", ".hu", ".ro"
}

TOXIC_HOSTS = {
    "styrerommet.no", "usbl.no", "vestbo.no", "bate.no", "bori.no", "kirken.no",
    "obos.no", "skytterlag.no", "bbnett.no", "vibbo.no", "kvadratmeter.no",
    "mallingco.no", "hamar2hobbl.no", "kragero-bbl.no", "boligbyggelaget.no",
    "pk-eiendom.no", "nbbo.no", "facebook.com", "instagram.com", "linkedin.com", "proff.no", "1881.no", "gulesider.no"
}

def clean_url(raw: str) -> str:
    if not raw: return ""
    u = raw.strip()
    if not u.startswith("http://") and not u.startswith("https://"):
        u = "https://" + u
    u_low = u.lower()
    dom = u_low.replace("https://", "").replace("http://", "").replace("www.", "").split("/")[0].split("?")[0].strip()
    if not dom or "." not in dom or dom in TOXIC_HOSTS:
        return ""
    parts = dom.split(".")
    if len(parts) >= 2 and "." + parts[-1] in FOREIGN_TLDS:
        return ""
    return f"https://{dom}"

def fetch_bedrift(org: str):
    url = f"https://www.fagfolkguiden.no/bedrift/{org}"
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        html = urllib.request.urlopen(req, timeout=6.0, context=_SSL_CTX).read().decode("utf-8", errors="ignore")
        soup = BeautifulSoup(html, "html.parser")
        script = soup.find("script", type="application/ld+json")
        if not script or not script.string:
            return org, None
        data = json.loads(script.string)
        return org, data
    except Exception:
        return org, None

def main():
    sitemap_path = Path("agent/data/fagfolkguiden_sitemap_1.xml")
    if not sitemap_path.exists():
        print("Sitemap file not found!")
        return

    xml = sitemap_path.read_text(encoding="utf-8")
    slugs = re.findall(r'<loc>https://www.fagfolkguiden.no/bedrift/(.*?)</loc>', xml)
    org_list = []
    seen = set()
    for s in slugs:
        parts = s.split("-")
        cand = parts[-1]
        if cand.isdigit() and len(cand) == 9 and cand not in seen:
            seen.add(cand)
            org_list.append(cand)

    print(f"Total unique organisation numbers to harvest: {len(org_list)}")

    # Load existing universe websites
    univ_web_path = Path("agent/data/universe-websites.json")
    univ_web = json.loads(univ_web_path.read_text(encoding="utf-8")) if univ_web_path.exists() else {}

    # Load existing ratings
    ratings_path = Path("agent/data/universe_ratings_reviews.json")
    ratings_data = json.loads(ratings_path.read_text(encoding="utf-8")) if ratings_path.exists() else {}

    harvested_path = Path("agent/data/fagfolkguiden_harvested.json")
    harvested_all = json.loads(harvested_path.read_text(encoding="utf-8")) if harvested_path.exists() else {}

    # Filter out already harvested
    to_fetch = [o for o in org_list if o not in harvested_all]
    print(f"Already harvested: {len(harvested_all)}, Remaining to fetch: {len(to_fetch)}")

    workers = 35
    print(f"Starting concurrent harvest (workers={workers})...", flush=True)

    new_websites = 0
    new_ratings = 0
    processed = 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(fetch_bedrift, org): org for org in to_fetch}
        for future in concurrent.futures.as_completed(futures):
            processed += 1
            org, data = future.result()
            if data:
                harvested_all[org] = data

                # Check sameAs for website
                same_as = data.get("sameAs")
                if same_as:
                    site_candidates = same_as if isinstance(same_as, list) else [same_as]
                    for sc in site_candidates:
                        c_url = clean_url(str(sc))
                        if c_url and org not in univ_web:
                            univ_web[org] = c_url
                            new_websites += 1
                            break

                # Check aggregateRating
                aggr = data.get("aggregateRating")
                if isinstance(aggr, dict):
                    r_val = aggr.get("ratingValue")
                    r_cnt = aggr.get("ratingCount") or aggr.get("reviewCount")
                    if r_val is not None and r_cnt is not None:
                        try:
                            f_val = float(r_val)
                            i_cnt = int(r_cnt)
                            if 0 < f_val <= 5 and i_cnt > 0:
                                ratings_data[org] = {
                                    "org_nr": org,
                                    "name": data.get("name"),
                                    "rating": f_val,
                                    "review_count": i_cnt,
                                    "source_url": f"https://www.fagfolkguiden.no/bedrift/{org}",
                                    "platform": "google_maps_embedded",
                                    "provider": "fagfolkguiden"
                                }
                                new_ratings += 1
                        except (ValueError, TypeError):
                            pass

            if processed % 200 == 0 or processed == len(to_fetch):
                print(f"[{processed}/{len(to_fetch)}] Processed. New Websites: {new_websites}, New Ratings: {new_ratings}", flush=True)
                # Checkpoint
                harvested_path.write_text(json.dumps(harvested_all, indent=2, ensure_ascii=False), encoding="utf-8")
                ratings_path.write_text(json.dumps(ratings_data, indent=2, ensure_ascii=False), encoding="utf-8")
                univ_web_path.write_text(json.dumps(univ_web, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"\nHarvest Complete!")
    print(f"  • Total Profiles in Fagfolkguiden: {len(harvested_all)}")
    print(f"  • Total Ratings & Reviews: {len(ratings_data)}")
    print(f"  • Total Verified Websites in universe-websites.json: {len(univ_web):,}")

if __name__ == "__main__":
    main()
