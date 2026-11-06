from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from norway_company_agent.candidate_sources import domain_guesses, relationship_candidates, operating_names
from norway_company_agent.local_cache import LocalCompanyCache
from norway_company_agent.site_jobs import extract_site_jobs
from norway_company_agent.source_store import capture_source
from norway_company_agent.identity import apply_website_identity_gate


class CandidateTests(unittest.TestCase):
    def test_operating_names_remain_bounded_parent_linked_search_leads(self):
        from scripts.run_competition_batch import website_search_queries, relevant_search_candidate
        profile = {"name": "Example Holding AS", "municipality": "Bergen", "organisation_number": "923609016", "evidence": {}}
        profile["discovery_operating_names"] = operating_names(profile, [
            {"name": "Example Holding AS"}, {"name": "Fjordverksted AS", "employees": 12},
            {"name": "Fjordverksted AS", "employees": 2}, {"name": "Another Branch", "employees": None}])
        self.assertEqual(profile["discovery_operating_names"], ["Fjordverksted AS", "Another Branch"])
        self.assertIn('"Fjordverksted AS" "Bergen"', website_search_queries(profile))
        self.assertTrue(relevant_search_candidate(profile, {"url": "https://fjordverksted.no", "title": "Fjordverksted"}))
        record = {"status": "available", "value": {"final_url": "https://fjordverksted.no", "title": "Fjordverksted", "identity_text_excerpt": "Fjordverksted AS"}}
        self.assertFalse(apply_website_identity_gate(profile, record)["assessment"]["publishable"])

    def test_legal_suffixes_and_compound_brands_have_bounded_domain_variants(self):
        guesses = domain_guesses("Next Step Growth Inc NUF")
        self.assertIn("https://nextstepgrowth.com/", guesses)
        self.assertTrue(all("incnuf" not in url for url in guesses))
        self.assertLessEqual(len(guesses), 14)
        self.assertEqual(len(guesses), len(set(guesses)))
        self.assertIn("https://fagtrapp.no/", domain_guesses("Fagtrapp Østfold AS"))

    def test_subunit_email_does_not_turn_shared_mail_provider_into_company_site(self):
        profile = {"evidence": {"locations": {"value": {"locations": [{"email": "branch@gmail.com"}, {"email": "branch@ownbrand.no"}]}}}}
        self.assertEqual(relationship_candidates(profile), [("https://ownbrand.no/", "official_subunit_email")])

    def test_verified_site_is_remembered_per_org_as_a_lead(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = LocalCompanyCache(directory)
            record = {"status": "available", "retrieved_at": "2026-11-04T00:00:00Z", "value": {"final_url": "https://example.no/", "content_sha256": "a" * 64, "identity_assessment": {"publishable": True, "method": "test"}}}
            self.assertTrue(cache.remember_verified_site("923609016", record))
            self.assertEqual(cache.website_candidate_records("923609016")[0]["url"], "https://example.no/")
            self.assertEqual(cache.website_candidate_records("111111111"), [])
            record["value"]["identity_assessment"]["publishable"] = False
            self.assertFalse(cache.remember_verified_site("111111111", record))

    def test_only_the_matching_organization_schema_supplies_social_links(self):
        record = {"status": "available", "value": {"title": "Example AS", "final_url": "https://example.no/", "content_sha256": "a" * 64,
            "identity_text_excerpt": "Example AS Org 923609016", "structured_organisations": [
                {"@type": "Organization", "url": "https://example.no/", "name": "Example AS", "sameAs": ["https://facebook.com/ExampleAS"]},
                {"@type": "Organization", "name": "Website Builder", "sameAs": ["https://facebook.com/WebsiteBuilder"]}]}}
        result = apply_website_identity_gate({"organisation_number": "923609016", "name": "Example AS", "evidence": {}}, record)
        self.assertEqual(result["website"]["value"]["social_links"], [{"platform": "facebook", "url": "https://facebook.com/ExampleAS"}])


class CapturedJobTests(unittest.TestCase):
    def make_profile(self, root: Path, *, employer="Example AS", expires=None, posting_url=None, page_url="https://example.no/jobs/engineer"):
        today = datetime.now(timezone.utc).date()
        data = {"@type": "JobPosting", "title": "Software engineer", "url": posting_url or page_url,
                "datePosted": today.isoformat(), "validThrough": expires or (today + timedelta(days=30)).isoformat(),
                "hiringOrganization": {"name": employer}}
        raw = ('<html><script type="application/ld+json">' + json.dumps(data) + '</script></html>').encode()
        ref = capture_source(raw, root)
        return {"organisation_number": "923609016", "name": "Example AS", "evidence": {"website": {"status": "available", "retrieved_at": "2026-11-04T00:00:00Z", "value": {
            **ref, "content_sha256": ref["snapshot_sha256"], "final_url": page_url, "registered_domain": "example.no", "pages": [], "identity_assessment": {"publishable": True, "content_attributable": True}}}}}

    def test_actual_captured_role_has_employer_dates_and_source_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = self.make_profile(Path(directory))
            jobs = extract_site_jobs(profile)
            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0]["metrics"]["job_title"], "Software engineer")
            self.assertTrue(Path(jobs[0]["snapshot_path"]).is_file())

    def test_foreign_employer_expiry_company_profile_and_index_cannot_publish(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for kwargs in ({"employer": "Different AS"}, {"expires": "2000-01-01"},
                           {"posting_url": "https://linkedin.com/company/example"},
                           {"page_url": "https://example.no/careers"}):
                self.assertEqual(extract_site_jobs(self.make_profile(root, **kwargs)), [])
