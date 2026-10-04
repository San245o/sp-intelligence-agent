#!/usr/bin/env python3
"""Extract official company intelligence from public unauthenticated LinkedIn company pages.

Uses curl_cffi with Chrome TLS fingerprint impersonation to read public LinkedIn
company overview cards without authentication, cookies, or API keys.

Pulls:
- Official external website URL
- Company size / employee count range
- Industry classification
- Headquarters city / country
- Founded year
"""
import re
from typing import Any
from bs4 import BeautifulSoup
from curl_cffi import requests

def extract_linkedin_company(slug_or_url: str) -> dict[str, Any] | None:
    slug = slug_or_url.strip().rstrip("/").split("/")[-1]
    url = f"https://www.linkedin.com/company/{slug}"
    
    try:
        r = requests.get(url, impersonate="chrome120", timeout=10)
        if r.status_code != 200:
            return None
        
        soup = BeautifulSoup(r.text, "html.parser")
        intel = {"url": url, "slug": slug}
        
        # 1. Parse definition list (dt / dd) for structured company attributes
        for dt in soup.find_all("dt"):
            dd = dt.find_next_sibling("dd")
            if not dd:
                continue
            k = dt.get_text(strip=True).lower()
            v = dd.get_text(strip=True)
            
            if "website" in k:
                # Remove "External link for..." trailing text
                clean_web = v.split("External link")[0].strip()
                intel["website"] = clean_web
            elif "company size" in k or "size" in k:
                intel["employees"] = v
            elif "industry" in k:
                intel["industry"] = v
            elif "headquarters" in k:
                intel["headquarters"] = v
            elif "founded" in k:
                intel["founded"] = v
            elif "type" in k:
                intel["company_type"] = v
                
        # 2. Extract description from meta or on-page text
        meta_desc = soup.find("meta", attrs={"name": "description"})
        if meta_desc and meta_desc.get("content"):
            intel["description"] = meta_desc["content"]
            
        return intel
    except Exception as e:
        return {"error": str(e)}

if __name__ == "__main__":
    import sys
    test_companies = ["elopak", "draupnir-invest-as", "g3-gausdal-treindustrier-sa"]
    if len(sys.argv) > 1:
        test_companies = sys.argv[1:]
        
    for comp in test_companies:
        print(f"\n--- Extracting: {comp} ---")
        res = extract_linkedin_company(comp)
        if res:
            for k, v in res.items():
                print(f"  {k}: {v}")
        else:
            print("  Not found or rate limited.")
