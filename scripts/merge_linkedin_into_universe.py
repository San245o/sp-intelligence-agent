import json
import os
import shutil

def main():
    print("=== Merging LinkedIn Dataset into Universe Knowledge Bases ===")
    
    # Paths
    univ_web_path = "agent/data/universe-websites.json"
    official_411k_path = "agent/data/official_411k_domains.json"
    linkedin_path = "agent/data/universe_linkedin_profiles.json"
    
    # 1. Backup original files
    shutil.copyfile(univ_web_path, univ_web_path + ".bak")
    shutil.copyfile(official_411k_path, official_411k_path + ".bak")
    print("Backups created (.bak)")

    with open(linkedin_path, "r", encoding="utf-8") as f:
        linkedin_data = json.load(f)

    # 2. Update universe-websites.json
    with open(univ_web_path, "r", encoding="utf-8") as f:
        univ_websites = json.load(f)

    added_to_univ_web = 0
    for org, entry in linkedin_data.items():
        web = entry.get("website", "").strip()
        if web:
            clean = web.replace("https://", "").replace("http://", "").strip("/")
            if org not in univ_websites or not univ_websites[org]:
                univ_websites[org] = clean
                added_to_univ_web += 1

    with open(univ_web_path, "w", encoding="utf-8") as f:
        json.dump(univ_websites, f, indent=2, ensure_ascii=False)
    print(f"Updated {univ_web_path}: added {added_to_univ_web:,} new websites (New Total: {len(univ_websites):,})")

    # 3. Update official_411k_domains.json with websites and LinkedIn URLs
    with open(official_411k_path, "r", encoding="utf-8") as f:
        official_411k = json.load(f)

    linkedin_added = 0
    websites_added_to_official = 0

    for org, entry in linkedin_data.items():
        li = entry.get("linkedin_url", "").strip()
        web = entry.get("website", "").strip()
        
        if org not in official_411k:
            official_411k[org] = {}

        # Add LinkedIn
        if li:
            official_411k[org]["l"] = li
            linkedin_added += 1

        # Add homepage if missing
        if web and not official_411k[org].get("h"):
            clean_web = web.replace("https://", "").replace("http://", "").strip("/")
            official_411k[org]["h"] = clean_web
            websites_added_to_official += 1

    with open(official_411k_path, "w", encoding="utf-8") as f:
        json.dump(official_411k, f, separators=(",", ":"), ensure_ascii=False)
    print(f"Updated {official_411k_path}:")
    print(f"  • Attached LinkedIn URLs: {linkedin_added:,}")
    print(f"  • Attached new homepages: {websites_added_to_official:,}")
    print(f"  • Total indexed companies in official_411k: {len(official_411k):,}")
    print(f"  • File size: {os.path.getsize(official_411k_path) / 1024 / 1024:.2f} MB")

    # 4. Generate crawl target list for the offline crawler
    crawl_targets = set()
    for org, entry in official_411k.items():
        if "h" in entry and entry["h"]:
            d = entry["h"].lower().split("/")[0].replace("www.", "")
            if d and "." in d:
                crawl_targets.add(d)
        if "e" in entry and entry["e"]:
            d = entry["e"].lower().split("/")[0].replace("www.", "")
            if d and "." in d:
                crawl_targets.add(d)

    targets_file = "agent/data/unique_crawler_domains.json"
    with open(targets_file, "w", encoding="utf-8") as f:
        json.dump(sorted(crawl_targets), f, indent=2)
    print(f"\nGenerated crawler targets: {targets_file}")
    print(f"Total Unique Domains to Scrape: {len(crawl_targets):,}")

if __name__ == "__main__":
    main()
