import gzip
import csv
import json
import os
import tldextract

def main():
    manifest_path = "data/submission-manifest-1000.jsonl"
    manifest_orgs = set()
    if os.path.exists(manifest_path):
        with open(manifest_path, "r", encoding="utf-8") as f:
            for line in f:
                data = json.loads(line)
                org = str(data.get("org_nr") or data.get("organisasjonsnummer"))
                manifest_orgs.add(org)

    print(f"Manifest target orgs: {len(manifest_orgs)}")

    csv_path = "data/brreg-enheter.csv.gz"
    output_domains_path = "data/official_411k_domains.json"

    # We will build an index of org_nr -> {hjemmeside, email_domain, email, phone, municipality}
    # Free generic email providers to exclude from company domain extraction
    GENERIC_DOMAINS = {
        "gmail.com", "hotmail.com", "outlook.com", "yahoo.com", "icloud.com",
        "live.com", "online.no", "broadpark.no", "frisurf.no", "lyse.net",
        "c2i.net", "getmail.no", "altibox.no", "hesbynett.no", "ntebb.no",
        "enivest.net", "bluezone.no", "ebpost.no", "haugnett.no", "me.com",
        "msn.com", "protonmail.com", "proton.me", "start.no"
    }

    results = {}
    stats = {
        "total_rows": 0,
        "with_hjemmeside": 0,
        "with_email": 0,
        "with_corp_domain": 0,
        "manifest_hits": 0,
        "manifest_corp_domains": 0
    }

    manifest_matches = {}

    with gzip.open(csv_path, "rt", encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        for row in reader:
            stats["total_rows"] += 1
            org = row.get("organisasjonsnummer", "").strip()
            if not org:
                continue

            hjemmeside = row.get("hjemmeside", "").strip()
            email = row.get("epostadresse", "").strip()
            phone = row.get("telefon", "").strip() or row.get("mobil", "").strip()
            kommune = row.get("forretningsadresse.kommune", "").strip() or row.get("postadresse.kommune", "").strip()

            email_domain = ""
            if email and "@" in email:
                stats["with_email"] += 1
                domain = email.split("@")[-1].strip().lower()
                # filter out generic webmail
                if domain and domain not in GENERIC_DOMAINS:
                    email_domain = domain

            if hjemmeside:
                stats["with_hjemmeside"] += 1

            if email_domain:
                stats["with_corp_domain"] += 1

            # Only store in 411k domain index if there is a website or corporate email domain
            if hjemmeside or email_domain:
                entry = {}
                if hjemmeside:
                    entry["h"] = hjemmeside
                if email_domain:
                    entry["e"] = email_domain
                if phone:
                    entry["p"] = phone
                if kommune:
                    entry["k"] = kommune
                results[org] = entry

            if org in manifest_orgs:
                stats["manifest_hits"] += 1
                if hjemmeside or email_domain:
                    stats["manifest_corp_domains"] += 1
                manifest_matches[org] = {
                    "hjemmeside": hjemmeside,
                    "email": email,
                    "email_domain": email_domain,
                    "phone": phone,
                    "kommune": kommune,
                    "navn": row.get("navn")
                }

    print(f"Scanned {stats['total_rows']:,} rows in Brreg bulk export.")
    print(f"Total companies with hjemmeside: {stats['with_hjemmeside']:,}")
    print(f"Total companies with email: {stats['with_email']:,}")
    print(f"Total companies with corporate domain: {stats['with_corp_domain']:,}")
    print(f"Total entries stored in official index: {len(results):,}")
    print(f"Manifest hits: {stats['manifest_hits']} / {len(manifest_orgs)}")
    print(f"Manifest companies with valid domain/hjemmeside: {stats['manifest_corp_domains']} / {len(manifest_orgs)}")

    with open(output_domains_path, "w", encoding="utf-8") as f:
        json.dump(results, f, separators=(",", ":"))
    print(f"Saved {output_domains_path} ({os.path.getsize(output_domains_path) / 1024 / 1024:.2f} MB)")

if __name__ == "__main__":
    main()
