from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from contextlib import closing
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .website import normalize_homepage, normalize_social_url
from .source_types import directory_url


DEFAULT_CACHE_DIR = Path(__file__).resolve().parents[2] / "data" / "local-cache"


def _load_map(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _candidate_key(value: str) -> str:
    normalized = normalize_homepage(value)
    if not normalized:
        return ""
    parsed = urlparse(normalized)
    host = (parsed.hostname or "").casefold().removeprefix("www.")
    path = (parsed.path or "/").rstrip("/")
    return host + path


class LocalCompanyCache:
    """Company-keyed discovery seeds. Seeds are candidates, never proof of a live claim."""

    def __init__(self, directory: str | Path = DEFAULT_CACHE_DIR):
        root = Path(directory)
        self.websites = _load_map(root / "universe-websites.json")
        self.domain_rows = _load_map(root / "official_411k_domains.json")
        self.social_rows = _load_map(root / "universe_social_profiles.json")
        self.wikidata_websites = _load_map(root / "wikidata-websites.json")
        self.linkedin_profiles = _load_map(root / "universe_linkedin_profiles.json")
        self.directory_profiles = _load_map(root / "fagfolkguiden_harvested.json")
        self.review_rows = _load_map(root / "universe_ratings_reviews.json")
        self.company_database = root / "brreg_companies.sqlite"
        self.subunits_database = root / "brreg_subunits.sqlite"
        self.learned_database = root / "verified-sites.sqlite"

    def remember_verified_site(self, organisation_number: str, record: dict[str, Any]) -> bool:
        value = record.get("value") or {}
        proof = value.get("identity_assessment") or {}
        org = str(organisation_number)
        if not (re.fullmatch(r"\d{9}", org) and record.get("status") == "available" and proof.get("publishable")
                and normalize_homepage(value.get("final_url")) and not directory_url(value.get("final_url"))
                and len(str(value.get("content_sha256") or "")) == 64):
            return False
        try:
            self.learned_database.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(self.learned_database, timeout=10)) as connection:
                connection.execute("CREATE TABLE IF NOT EXISTS sites (org TEXT PRIMARY KEY, url TEXT NOT NULL, retrieved_at TEXT, source_sha256 TEXT, identity_method TEXT)")
                connection.execute("INSERT OR REPLACE INTO sites VALUES (?,?,?,?,?)", (org, value["final_url"], record.get("retrieved_at"), value["content_sha256"], proof.get("method")))
                connection.commit()
            return True
        except (OSError, sqlite3.Error):
            return False

    def learned_website(self, organisation_number: str) -> str | None:
        if not self.learned_database.is_file():
            return None
        try:
            with closing(sqlite3.connect(self.learned_database, timeout=10)) as connection:
                row = connection.execute("SELECT url FROM sites WHERE org=?", (str(organisation_number),)).fetchone()
            return str(row[0]) if row else None
        except sqlite3.Error:
            return None

    def company_context(self, organisation_number: str, company_name: str | None = None) -> dict[str, Any]:
        """Return local cache material for research; values here are not publication claims."""
        org = str(organisation_number)
        linkedin = self.linkedin_profiles.get(org)
        if not isinstance(linkedin, dict) or str(linkedin.get("org_nr") or "") != org:
            linkedin = None
        directory = self.directory_profiles.get(org)
        if not isinstance(directory, dict) or not isinstance(directory.get("identifier"), dict) or str(directory["identifier"].get("value") or "") != org:
            directory = None
        review = self.review_rows.get(org)
        if not isinstance(review, dict) or str(review.get("org_nr") or "") != org:
            review = None
        return {
            "organisation_number": org,
            "website_candidates": self.website_candidates(org, company_name),
            "social_candidates": self.social_candidates(org),
            "linkedin_profile": linkedin,
            "fagfolkguiden_profile": directory,
            "rating_review_summary": review,
            "brreg_entity": self.brreg_entity(org),
            "brreg_subunits": self.brreg_subunits(org),
            "claim_boundary": "Cached records and candidate URLs are research context; refresh the source and apply the relevant identity, freshness, and rights checks before publication.",
        }

    def brreg_subunits(self, organisation_number: str) -> list[dict[str, Any]]:
        """Read locally indexed Brønnøysund subunits for a parent company."""
        if not self.subunits_database.is_file():
            return []
        with closing(sqlite3.connect(self.subunits_database)) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT subunit_org, parent_org, name, legal_form, employees, municipality, "
                "municipality_number, industry_code, industry_label, website "
                "FROM subunits WHERE parent_org=? ORDER BY subunit_org",
                (str(organisation_number),),
            ).fetchall()
        return [dict(row) for row in rows]

    def brreg_subunits_for_parents(self, organisation_numbers: list[str]) -> dict[str, list[dict[str, str]]]:
        """Read matching subunit names/numbers for a batch in a few indexed queries."""
        parents = list(dict.fromkeys(str(org) for org in organisation_numbers if str(org).isdigit()))
        result: dict[str, list[dict[str, str]]] = {org: [] for org in parents}
        if not parents or not self.subunits_database.is_file():
            return result
        with closing(sqlite3.connect(self.subunits_database)) as connection:
            connection.row_factory = sqlite3.Row
            for start in range(0, len(parents), 500):
                chunk = parents[start:start + 500]
                marks = ",".join("?" for _ in chunk)
                rows = connection.execute(
                    f"SELECT parent_org, subunit_org, name FROM subunits WHERE parent_org IN ({marks})",
                    chunk,
                ).fetchall()
                for row in rows:
                    result[str(row["parent_org"])].append({
                        "subunit_org": str(row["subunit_org"] or ""),
                        "name": str(row["name"] or ""),
                    })
        return result

    def brreg_subunit_cache_metadata(self) -> dict[str, Any]:
        if not self.subunits_database.is_file():
            return {}
        with closing(sqlite3.connect(self.subunits_database)) as connection:
            return {key: json.loads(value) for key, value in connection.execute("SELECT key,value FROM metadata")}

    def brreg_entity(self, organisation_number: str) -> dict[str, Any] | None:
        """Read one public parent-entity record from the local Brønnøysund index."""
        if not self.company_database.is_file():
            return None
        with closing(sqlite3.connect(self.company_database)) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM companies WHERE organisation_number=?",
                (str(organisation_number),),
            ).fetchone()
        return dict(row) if row else None

    def brreg_entity_cache_metadata(self) -> dict[str, Any]:
        if not self.company_database.is_file():
            return {}
        with closing(sqlite3.connect(self.company_database)) as connection:
            return {key: json.loads(value) for key, value in connection.execute("SELECT key,value FROM metadata")}

    @staticmethod
    def _directory_company_sites(row: Any, organisation_number: str) -> list[str]:
        """Return only sameAs URLs from a directory row whose embedded ID matches the key."""
        if not isinstance(row, dict):
            return []
        identifier = row.get("identifier")
        if not isinstance(identifier, dict) or str(identifier.get("value") or "") != str(organisation_number):
            return []
        raw_urls = row.get("sameAs") or []
        if isinstance(raw_urls, str):
            raw_urls = [raw_urls]
        if not isinstance(raw_urls, list):
            return []
        output = []
        for raw in raw_urls:
            candidate = normalize_homepage(str(raw or ""))
            host = (urlparse(candidate).hostname or "").casefold().removeprefix("www.") if candidate else ""
            # Keep the directory itself and social platforms out of website candidates.
            if not host or host == "fagfolkguiden.no" or host.endswith(".fagfolkguiden.no"):
                continue
            if any(host == domain or host.endswith("." + domain) for domain in (
                "linkedin.com", "facebook.com", "instagram.com", "tiktok.com", "youtube.com",
                "youtu.be", "x.com", "twitter.com", "pinterest.com", "snapchat.com",
            )):
                continue
            output.append(candidate)
        return output

    def website_candidate_records(self, organisation_number: str, company_name: str | None = None) -> list[dict[str, str]]:
        """Return ordered website leads with source labels; every URL still needs a live identity check."""
        org = str(organisation_number)
        values: list[tuple[str, str]] = []
        learned = self.learned_website(org)
        if learned:
            values.append((learned, "previously_verified_site_lead"))
        for raw in (self.websites.get(org),):
            if isinstance(raw, str) and raw.strip():
                values.append((raw.strip(), "universe_website_cache"))
        domain_row = self.domain_rows.get(org)
        if isinstance(domain_row, dict):
            for key in ("h", "e"):
                raw = domain_row.get(key)
                if isinstance(raw, str) and raw.strip():
                    values.append((raw.strip(), "official_domain_cache"))
        wikidata_url = self.wikidata_websites.get(org)
        if isinstance(wikidata_url, str) and wikidata_url.strip():
            values.append((wikidata_url.strip(), "wikidata_cache"))
        # The company and subunit snapshots are official, organisation-number-keyed
        # sources. Subunit URLs are leads for the parent legal entity, not claims;
        # the normal fetched-page exact-name gate still decides publication.
        entity = self.brreg_entity(org)
        if entity and isinstance(entity.get("website"), str) and entity["website"].strip():
            values.append((entity["website"].strip(), "brreg_entity_cache"))
        if self.subunits_database.is_file():
            with closing(sqlite3.connect(self.subunits_database)) as connection:
                rows = connection.execute(
                    "SELECT website FROM subunits WHERE parent_org=? AND website IS NOT NULL AND TRIM(website)!='' ORDER BY subunit_org",
                    (org,),
                ).fetchall()
            values.extend((str(row[0]).strip(), "brreg_subunit_cache") for row in rows if row and row[0])
        linkedin_row = self.linkedin_profiles.get(org)
        if isinstance(linkedin_row, dict) and str(linkedin_row.get("org_nr") or "") == org:
            linkedin_website = normalize_homepage(linkedin_row.get("website"))
            if linkedin_website:
                values.append((linkedin_website, "linkedin_company_profile_cache"))
        values.extend((url, "fagfolkguiden_same_as_cache") for url in self._directory_company_sites(self.directory_profiles.get(org), org))
        if company_name:
            transliterated = str(company_name).translate(str.maketrans({"ø": "o", "å": "a", "æ": "ae", "Ø": "O", "Å": "A", "Æ": "AE"}))
            transliterated = unicodedata.normalize("NFKD", transliterated).encode("ascii", "ignore").decode().casefold()
            tokens = [token for token in re.findall(r"[a-z0-9]+", transliterated) if token not in {"as", "asa", "ans", "da", "enk", "nuf", "sa", "sti", "stiftelsen", "the", "and", "og"}]
            if tokens:
                compact = "".join(tokens)
                guesses = [f"https://{compact}.no"]
                generic_tails = {"products", "norge", "norway", "drift", "service", "services", "eiendom", "invest", "holding", "utleie", "consulting", "solutions", "teknikk", "partner", "handel", "industri", "gruppen", "group"}
                if len(tokens) > 1 and tokens[-1] in generic_tails:
                    guesses.append(f"https://{''.join(tokens[:-1])}.no")
                if "barnehage" in tokens or "barnehager" in tokens:
                    guesses.append(f"https://{''.join('bhg' if token in {'barnehage', 'barnehager'} else token for token in tokens)}.no")
                if len(tokens) <= 3:
                    guesses.append(f"https://{compact}.com")
                if len(tokens[0]) >= 3 and len(tokens) > 1:
                    guesses.extend((f"https://{tokens[0]}.no", f"https://{tokens[0]}.com"))
                if len(tokens) > 1:
                    guesses.append(f"https://{'-'.join(tokens)}.no")
                # Generated domains stay behind all organisation-keyed sources and
                # remain leads until the fetched site passes the identity gate.
                values.extend((url, "generated_name_guess") for url in guesses[:6])
        unique: list[dict[str, str]] = []
        seen: set[str] = set()
        for value, source in values:
            normalized = normalize_homepage(value)
            if not normalized:
                continue
            key = _candidate_key(normalized)
            if key not in seen:
                seen.add(key)
                unique.append({"url": normalized, "source": source})
        return unique

    def website_candidates(self, organisation_number: str, company_name: str | None = None) -> list[str]:
        """Backward-compatible URL-only view of the ordered candidate leads."""
        return [item["url"] for item in self.website_candidate_records(organisation_number, company_name)]

    def social_candidates(self, organisation_number: str) -> list[dict[str, str]]:
        row = self.social_rows.get(str(organisation_number))
        if not isinstance(row, dict) or str(row.get("org_nr") or "") != str(organisation_number):
            return []
        # A cache row is only a discovery lead; the regular exact-name social gate still decides publication.
        links = row.get("platforms")
        if not isinstance(links, dict):
            return []
        output = []
        for raw in links.values():
            if isinstance(raw, str):
                normalized = normalize_social_url(raw)
                if normalized:
                    output.append({**normalized, "discovery_source": "universe_social_profiles.json"})
        return output


def cache_coverage(directory: str | Path = DEFAULT_CACHE_DIR) -> dict[str, int | str | None]:
    root = Path(directory)
    websites = _load_map(root / "universe-websites.json")
    domains = _load_map(root / "official_411k_domains.json")
    socials = _load_map(root / "universe_social_profiles.json")
    wikidata = _load_map(root / "wikidata-websites.json")
    linkedin = _load_map(root / "universe_linkedin_profiles.json")
    directory_profiles = _load_map(root / "fagfolkguiden_harvested.json")
    reviews = _load_map(root / "universe_ratings_reviews.json")
    subunit_db = root / "brreg_subunits.sqlite"
    company_db = root / "brreg_companies.sqlite"

    valid_linkedin = {
        org: row for org, row in linkedin.items()
        if isinstance(row, dict) and str(row.get("org_nr") or "") == str(org)
    }
    linkedin_websites = {
        org for org, row in valid_linkedin.items()
        if normalize_homepage(row.get("website"))
    }
    valid_directory = {
        org: row for org, row in directory_profiles.items()
        if isinstance(row, dict)
        and isinstance(row.get("identifier"), dict)
        and str(row["identifier"].get("value") or "") == str(org)
    }
    directory_websites = {
        org for org, row in valid_directory.items()
        if LocalCompanyCache._directory_company_sites(row, org)
    }
    existing_website_companies = set(websites) | set(domains) | set(wikidata)
    expanded_website_companies = existing_website_companies | linkedin_websites | directory_websites
    other_website_companies = expanded_website_companies
    subunits = 0
    subunit_parent_companies = 0
    subunit_metadata: dict[str, Any] = {}
    if subunit_db.is_file():
        try:
            with closing(sqlite3.connect(subunit_db)) as connection:
                subunits = int(connection.execute("SELECT COUNT(*) FROM subunits").fetchone()[0])
                subunit_parent_companies = int(connection.execute("SELECT COUNT(DISTINCT parent_org) FROM subunits").fetchone()[0])
                subunit_metadata = {key: json.loads(value) for key, value in connection.execute("SELECT key,value FROM metadata")}
        except sqlite3.Error:
            pass
    entity_count = 0
    entity_metadata: dict[str, Any] = {}
    if company_db.is_file():
        try:
            with closing(sqlite3.connect(company_db)) as connection:
                entity_count = int(connection.execute("SELECT COUNT(*) FROM companies").fetchone()[0])
                entity_metadata = {key: json.loads(value) for key, value in connection.execute("SELECT key,value FROM metadata")}
        except sqlite3.Error:
            pass
    brreg_website_parents = 0
    brreg_subunit_website_parents = 0
    brreg_only_website_parents = 0
    brreg_website_orgs: set[str] = set()
    brreg_subunit_website_orgs: set[str] = set()
    if company_db.is_file():
        try:
            with closing(sqlite3.connect(company_db)) as connection:
                brreg_website_orgs = {
                    str(row[0]) for row in connection.execute(
                        "SELECT DISTINCT organisation_number FROM companies WHERE website IS NOT NULL AND TRIM(website)!=''"
                    )
                }
                brreg_website_parents = len(brreg_website_orgs)
        except sqlite3.Error:
            pass
    if subunit_db.is_file():
        try:
            with closing(sqlite3.connect(subunit_db)) as connection:
                brreg_subunit_website_orgs = {
                    str(row[0]) for row in connection.execute(
                        "SELECT DISTINCT parent_org FROM subunits WHERE website IS NOT NULL AND TRIM(website)!=''"
                    )
                }
                brreg_subunit_website_parents = len(brreg_subunit_website_orgs)
        except sqlite3.Error:
            pass
    brreg_only_website_parents = len((brreg_website_orgs | brreg_subunit_website_orgs) - other_website_companies)
    expanded_website_companies |= brreg_website_orgs | brreg_subunit_website_orgs
    result = {
        "website_seed_companies": len(websites),
        "official_domain_companies": len(domains),
        "social_profile_companies": len(socials),
        "wikidata_website_companies": len(wikidata),
        "linkedin_company_profiles": len(valid_linkedin),
        "linkedin_website_companies": len(linkedin_websites),
        "linkedin_only_website_companies": len(linkedin_websites - existing_website_companies),
        "fagfolkguiden_company_profiles": len(valid_directory),
        "fagfolkguiden_website_companies": len(directory_websites),
        "fagfolkguiden_only_website_companies": len(directory_websites - existing_website_companies - linkedin_websites),
        "cached_review_summaries": sum(isinstance(row, dict) and row.get("rating") is not None for row in reviews.values()),
        "brreg_subunits": subunits,
        "brreg_subunit_parent_companies": subunit_parent_companies,
        "brreg_entities": entity_count,
        "brreg_entity_website_companies": brreg_website_parents,
        "brreg_subunit_website_parent_companies": brreg_subunit_website_parents,
        "brreg_unique_website_companies_outside_other_maps": brreg_only_website_parents,
        "merged_companies": len(expanded_website_companies),
    }
    if subunit_metadata:
        result["brreg_subunits_retrieved_at"] = subunit_metadata.get("retrieved_at")
        result["brreg_subunits_source_sha256"] = subunit_metadata.get("source_sha256")
        result["brreg_subunits_source_last_modified"] = subunit_metadata.get("source_last_modified")
    if entity_metadata:
        result["brreg_entities_retrieved_at"] = entity_metadata.get("retrieved_at")
        result["brreg_entities_source_sha256"] = entity_metadata.get("source_sha256")
        result["brreg_entities_source_last_modified"] = entity_metadata.get("source_last_modified")
    return result
