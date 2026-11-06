from __future__ import annotations

import copy
import hashlib
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]

from norway_company_agent import website
from norway_company_agent.identity import assess_website_identity
from norway_company_agent.research import answer_profile
from norway_company_agent.source_store import capture_source
from scripts.evaluate_research_agent import evaluate_external_qa
from norway_company_agent import network_meter
from norway_company_agent.external_tasks import plan_external_tasks
from norway_company_agent.site_discovery import _page_publication_date, _article_candidate, _date_only
from bs4 import BeautifulSoup
from scripts.audit_evidence_metrics import direct_job_url, active_exact_job
from norway_company_agent.official import normalize_financials


class Response:
    def __init__(self, body):
        self.body = body
    def read(self, limit=-1):
        return self.body[:limit] if limit >= 0 else self.body
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False


class AcquisitionTests(unittest.TestCase):
    def test_job_url_classifier_distinguishes_finn_role_from_report_and_filters(self):
        self.assertTrue(direct_job_url({"source_url": "https://www.finn.no/job/ad/444005057", "metrics": {"job_title": "Rådgivende Ingeniør Bygg"}}))
        for url, title in [("https://www.finn.no/report/ad?adId=444005057", "Rapporter annonse"), ("https://www.finn.no/job/search?industry=4", "Bygg og anlegg")]:
            self.assertFalse(direct_job_url({"source_url": url, "metrics": {"job_title": title, "published_at": "2026-01-02"}}))

    def test_priority_crawl_reaches_role_without_exceeding_secondary_limit(self):
        homepage = b'<html><title>Example AS</title><a href="/contact/a">Contact</a><a href="/contact/b">Contact branch</a><a href="/careers">Careers</a><a href="/about">About</a><a href="/news">News</a></html>'
        class HomepageResponse(Response):
            headers = {"content-type": "text/html"}
            def geturl(self):
                return "https://example.no/"
        called = []
        def secondary(url, **kwargs):
            called.append(url)
            return {"url": url, "role_links": ["https://example.no/careers/engineer"] if url.endswith("/careers") else []}, [], 1, 20, 1, None
        with patch.object(website, "assert_public_url"), patch.object(website, "_robots_check", return_value=(True, 0)), patch.object(website.SAFE_OPENER, "open", return_value=HomepageResponse(homepage)), patch.object(website, "_fetch_secondary_page", side_effect=secondary):
            record, metrics = website._fetch_website("https://example.no/", max_secondary_pages=4)
        self.assertEqual(record["status"], "available")
        self.assertEqual(len(called), 4)
        self.assertIn("https://example.no/about", called)
        self.assertIn("https://example.no/careers/engineer", called)
        self.assertNotIn("https://example.no/contact/b", called)
        self.assertEqual(metrics["requests"], 5)

    def test_role_links_exclude_external_boards_downloads_and_index_self_links(self):
        soup = BeautifulSoup('<a href="/careers/engineer">Engineer</a><a href="/careers">Jobs</a><a href="https://other.no/jobs/engineer">Other</a><a href="/careers/jobs.pdf">PDF</a>', "lxml")
        self.assertEqual(website._role_links("https://example.no/careers", soup), ["https://example.no/careers/engineer"])
        self.assertEqual(website._role_links("https://example.no/news", soup), [])

    def setUp(self):
        website._ROBOTS_CACHE.clear()
        website._FETCH_CACHE.clear()
        storage = patch.object(website, "FETCH_CACHE_DIR", None)
        storage.start()
        self.addCleanup(storage.stop)

    def test_robots_reused_but_permissions_are_checked_for_each_path(self):
        with patch.object(website, "assert_public_url"), patch.object(website.SAFE_OPENER, "open", return_value=Response(b"User-agent: *\nDisallow: /private\n")) as opened:
            self.assertEqual(website._robots_check("https://example.no/", 1), (True, 1))
            self.assertEqual(website._robots_check("https://example.no/private", 1), (False, 0))
            self.assertEqual(opened.call_count, 1)

    def test_simultaneous_robots_reads_make_one_network_request(self):
        with patch.object(website, "assert_public_url"), patch.object(website.SAFE_OPENER, "open", return_value=Response(b"User-agent: *\nAllow: /")) as opened:
            with ThreadPoolExecutor(max_workers=8) as executor:
                results = list(executor.map(lambda _: website._robots_check("https://example.no/", 1), range(8)))
            self.assertEqual(sum(result[1] for result in results), 1)
            self.assertEqual(opened.call_count, 1)

    def test_missing_robots_is_distinct_from_refused_or_failed_robots(self):
        for code, expected in ((404, True), (403, False), (429, False), (503, False)):
            website._ROBOTS_CACHE.clear()
            with patch.object(website, "assert_public_url"), patch.object(website.SAFE_OPENER, "open", side_effect=HTTPError("https://example.no/robots.txt", code, "error", {}, None)):
                self.assertEqual(website._robots_check("https://example.no/", 1), (expected, 1))

    def test_acquisition_cache_does_not_share_identity_mutations_or_refresh_dates(self):
        record = {"status": "available", "retrieved_at": "2026-11-04T00:00:00Z", "value": {"pages": []}}
        with patch.object(website, "_fetch_website", return_value=(record, {"requests": 2, "bytes": 10, "latencies_ms": [1]})) as fetch:
            first, _ = website.fetch_website("https://example.no/")
            first["value"]["identity_assessment"] = {"publishable": True}
            second, metrics = website.fetch_website("https://example.no/")
            self.assertNotIn("identity_assessment", second["value"])
            self.assertEqual(second["retrieved_at"], record["retrieved_at"])
            self.assertEqual(metrics["requests"], 0)
            self.assertEqual(fetch.call_count, 1)

    def test_capture_bytes_match_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            ref = capture_source(b"company page", Path(directory))
            self.assertEqual(Path(ref["snapshot_path"]).read_bytes(), b"company page")
            self.assertEqual(ref["snapshot_sha256"], hashlib.sha256(b"company page").hexdigest())

    def test_transport_reserve_protects_registry_first_attempts_from_discovery_and_retries(self):
        network_meter.activate(3)
        network_meter.reserve_official(2)
        self.addCleanup(network_meter.deactivate)
        sys.audit("urllib.Request", "https://example.no/", None, {}, "GET")
        with self.assertRaises(network_meter.RequestLimitExceeded):
            sys.audit("urllib.Request", "https://example.no/news", None, {}, "GET")
        sys.audit("urllib.Request", "https://data.brreg.no/entity/1", None, {}, "GET")
        with self.assertRaises(network_meter.RequestLimitExceeded):
            sys.audit("urllib.Request", "https://data.brreg.no/entity/1", None, {}, "GET")
        sys.audit("urllib.Request", "https://data.brreg.no/entity/2", None, {}, "GET")
        self.assertEqual(network_meter.snapshot()["attempts"], 3)
        self.assertEqual(network_meter.snapshot()["official_reserve_remaining"], 0)

    def test_registry_budget_exhaustion_returns_failure_without_retry_or_hash(self):
        from norway_company_agent.http import fetch_json
        with patch("urllib.request.urlopen", side_effect=network_meter.RequestLimitExceeded), patch("time.sleep") as sleep:
            result = fetch_json("https://data.brreg.no/entity/1")
        self.assertEqual(result.error, "request_budget_exhausted")
        self.assertEqual(result.status, 0)
        self.assertIsNone(result.content_sha256)
        sleep.assert_not_called()

    def test_website_budget_stop_is_explicit_and_not_cached(self):
        with patch.object(website, "_fetch_website", side_effect=network_meter.RequestLimitExceeded):
            record, metrics = website.fetch_website("https://example.no/")
        self.assertEqual(record["status"], "not_fetched")
        self.assertEqual(record["note"], "request_budget_exhausted")
        self.assertTrue(metrics["budget_exhausted"])
        self.assertFalse(website._FETCH_CACHE)

    def test_transport_meter_caps_attempts_including_redirects(self):
        network_meter.activate(2)
        self.addCleanup(network_meter.deactivate)
        sys.audit("urllib.Request", "https://example.no/", None, {}, "GET")
        sys.audit("urllib.Request", "https://www.example.no/", None, {}, "GET")
        with self.assertRaises(network_meter.RequestLimitExceeded):
            sys.audit("urllib.Request", "https://example.no/private", None, {}, "GET")
        self.assertEqual(network_meter.snapshot()["attempts"], 2)
        self.assertEqual(network_meter.snapshot()["prevented_attempts"], 1)

    def test_disk_cache_retains_source_time_and_reacquires_if_capture_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ref = capture_source(b"source", root / "sources")
            record = {"status": "available", "retrieved_at": "2026-11-04T00:00:00Z", "value": ref}
            with patch.object(website, "FETCH_CACHE_DIR", root / "cache"), patch.object(website, "READ_DISK_CACHE", True), patch.object(website, "_fetch_website", return_value=(record, {"requests": 2, "bytes": 6, "latencies_ms": [1]})) as fetch:
                website.fetch_website("https://example.no/")
                website._FETCH_CACHE.clear()
                cached, metrics = website.fetch_website("https://example.no/")
                self.assertEqual(metrics["requests"], 0)
                self.assertEqual(cached["retrieved_at"], record["retrieved_at"])
                Path(ref["snapshot_path"]).unlink()
                website.fetch_website("https://example.no/")
                self.assertEqual(fetch.call_count, 2)


class IdentityRegressionTests(unittest.TestCase):
    def profile(self, name, title, text, url="https://example.com/", registry=None):
        return {"organisation_number": "923609016", "name": name, "evidence": {
            "registry": {"value": registry or {}},
            "website": {"status": "available", "value": {"final_url": url, "title": title, "identity_text_excerpt": text, "pages": []}}}}

    def test_foreign_namesake_and_scattered_words_do_not_pass(self):
        for row in (
            self.profile("TIMETRACK AS", "TimeTrack", "TimeTrack attendance software", "https://timetrack.in/login/"),
            self.profile("TROOST PARTNER CONSULT AS", "Peter Troost Monument", "Troost makes monuments. Contact a partner to consult."),
            self.profile("SNACKBOX AS", "SnackBox", "Premium snack care packages in the USA", "https://snackboxusa.com/"),
        ):
            self.assertFalse(assess_website_identity(row)["publishable"])

    def test_conflicting_org_overrides_matching_name(self):
        row = self.profile("Example AS", "Example AS", "Example AS Org 987 654 321", "https://example.no/")
        self.assertFalse(assess_website_identity(row)["publishable"])

    def test_adjacent_unrelated_numbers_cannot_manufacture_company_org(self):
        row = self.profile("Example AS", "Foreign site", "923 items, 609 customers, 016 projects")
        self.assertFalse(assess_website_identity(row)["publishable"])

    def test_brand_can_be_confirmed_by_registered_phone(self):
        row = self.profile("Example Norge AS", "Example Norge", "Example Norge Call +47 22 33 44 55", registry={"telefon": "22334455"})
        self.assertTrue(assess_website_identity(row)["publishable"])

    def test_joined_brand_is_recognized_without_accepting_a_domain_echo(self):
        row = self.profile("Nord Kledning AS", "NordKledning", "Exterior cladding", "https://nordkledning.no/")
        self.assertTrue(assess_website_identity(row)["publishable"])
        row["evidence"]["website"]["value"]["title"] = "nordkledning.no"
        self.assertFalse(assess_website_identity(row)["publishable"])

    def test_careof_address_does_not_match_a_single_letter_on_foreign_site(self):
        row = self.profile("Rivet AS", "Industrial Rivet", "35 Maple Street, New Jersey USA", "https://rivet.com/", {"forretningsadresse.adresse": "c/o Trym Eiken Colletts gate 54", "forretningsadresse.postnummer": "0456"})
        self.assertFalse(assess_website_identity(row)["publishable"])

    def test_registered_person_corroborates_brand_but_self_named_company_is_not_second_proof(self):
        row = self.profile("Kurated By AS", "Kurated By", "Our founder Ellen Ostmoen", "https://kuratedby.com/")
        row["evidence"]["roles"] = {"value": {"roles": [{"name": "Ellen Ostmoen"}]}}
        self.assertTrue(assess_website_identity(row)["publishable"])
        row["name"] = "Ellen Ostmoen AS"
        row["evidence"]["website"]["value"]["title"] = "Ellen Ostmoen"
        self.assertFalse(assess_website_identity(row)["publishable"])


class PublicationTests(unittest.TestCase):
    def test_visible_article_header_date_excludes_modified_and_related_dates(self):
        html='<article><header><h1>New project</h1><span>15. oktober 2026</span></header><p>Since 2020</p></article><aside>Related: 9. oktober 2026</aside>'
        self.assertEqual(_page_publication_date(BeautifulSoup(html,'lxml'),'https://example.no/news/a'),('2026-10-15','page:article_header_visible_date'))
        self.assertEqual(_page_publication_date(BeautifulSoup(html.replace('15. oktober','Oppdatert 15. oktober'),'lxml'),'https://example.no/news/a'),('',''))

    def test_retained_article_is_enriched_without_fetching_it_again(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from norway_company_agent.site_discovery import discover_site_activity
        from norway_company_agent.source_store import capture_source
        raw=b'<article><header><h1>New project</h1><span>15. oktober 2026</span></header><p>Company project details.</p></article>'
        captured=capture_source(raw)
        url='https://example.no/news/a'
        website={'final_url':'https://example.no','registered_domain':'example.no','pages':[{'url':url,**captured,'content_sha256':captured['snapshot_sha256']}]}
        with patch('norway_company_agent.site_discovery._read',return_value=(None,{'requests':1,'bytes':0,'latencies_ms':[],'outcome':'not_found'})) as read, \
             patch('norway_company_agent.site_discovery.robots_sitemaps',return_value=[]):
            result=discover_site_activity(website,reserve=lambda n:True,charge=lambda m:None)
        self.assertEqual(result['dated_published_pages'],1)
        self.assertEqual(len(website['pages']),1)
        self.assertEqual(website['pages'][0]['published'],'2026-10-15')
        self.assertNotIn(url,[call.args[0] for call in read.call_args_list])

    def test_jsonld_publication_must_describe_this_article(self):
        soup = BeautifulSoup('<script type="application/ld+json">{"@type":"NewsArticle","url":"https://example.no/news/a","datePublished":"2026-10-01","dateModified":"2026-11-01"}</script>', "lxml")
        self.assertEqual(_page_publication_date(soup, "https://example.no/news/a"), ("2026-10-01", "page:Article.datePublished"))
        self.assertEqual(_page_publication_date(soup, "https://example.no/news/b"), ("", ""))

    def test_modified_only_metadata_and_xml_sitemaps_are_not_articles(self):
        soup = BeautifulSoup('<script type="application/ld+json">{"@type":"NewsArticle","dateModified":"2026-11-01"}</script>', "lxml")
        self.assertEqual(_page_publication_date(soup, "https://example.no/news/a"), ("", ""))
        self.assertFalse(_article_candidate("https://example.no/news-sitemap.xml"))
        self.assertFalse(_article_candidate("https://example.no/news/"))
        self.assertTrue(_article_candidate("https://example.no/innsikt/new-product"))
        self.assertEqual(_date_only("2026-99-32"), "")


class ExternalAnswerTests(unittest.TestCase):
    def row(self):
        return {"organisation_number": "923609016", "name": "Example AS", "evidence": {"external_footprint": {
            "status": "available", "value": {"observations": [{"organisation_number": "923609016", "platform": "company_site", "signal_type": "public_post", "exact_entity": True,
                "rights_status": "approved", "identity_proof": [{"type": "org"}], "source_url": "https://example.no/news/a", "retrieved_at": "2026-11-04T00:00:00Z", "content_sha256": "a" * 64,
                "metrics": {"title": "Launch", "activity_date": "2026-11-01"}}]}}}}

    def test_qa_answers_use_observation_citations_and_reject_other_company(self):
        row = self.row()
        facts = answer_profile(row, "Show dated news")["facts"]
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]["source_url"], "https://example.no/news/a")
        row["evidence"]["external_footprint"]["value"]["observations"][0]["organisation_number"] = "111111111"
        self.assertEqual(answer_profile(row, "Show dated news")["facts"], [])

    def test_news_is_not_used_as_review_or_sentiment_evidence(self):
        self.assertEqual(answer_profile(self.row(), "Show reviews")["facts"], [])
        self.assertEqual(answer_profile(self.row(), "What is the sentiment?")["facts"], [])

    def test_external_qa_gate_requires_supported_and_abstention_cases(self):
        row = self.row()
        supported = {"id": "news", "organisation_number": row["organisation_number"], "question": "Show news", "required_claims": ["Dated company news"]}
        abstain = {"id": "reviews", "organisation_number": row["organisation_number"], "question": "Show reviews", "must_abstain": True}
        corpus = {row["organisation_number"]: row}
        self.assertFalse(evaluate_external_qa(corpus, [supported])["passed"])
        self.assertTrue(evaluate_external_qa(corpus, [supported, abstain])["passed"])

    def test_independent_discovery_keeps_official_contacts_without_a_website(self):
        profile = {"organisation_number": "923609016", "name": "Example AS", "evidence": {"registry": {"value": {"telefon": "22334455", "forretningsadresse.adresse": "Examplegata 1"}}}}
        tasks = plan_external_tasks(profile)
        self.assertTrue({"google_places_api", "licensed_news_search", "jobs_provider", "permitted_search_api"} <= {task["connector"] for task in tasks})
        self.assertTrue(all(task["registry_phone"] == "22334455" for task in tasks))

    def test_company_profile_is_not_a_vacancy_even_with_a_job_title(self):
        self.assertFalse(direct_job_url({"source_url": "https://linkedin.com/company/navic", "metrics": {"job_title": "NAVIC Group", "published_at": "2026-11-01"}}))
        self.assertTrue(direct_job_url({"source_url": "https://pam-stilling-feed.nav.no/api/v1/feedentry/123", "metrics": {"title": "Software engineer"}, "identity_proof": [{"type": "nav_employer_organisation_number"}]}))

    def test_old_role_page_without_expiry_and_employer_proof_is_not_verified_active(self):
        role = {"source_url": "https://example.no/career/design-engineer", "metrics": {"job_title": "Design engineer", "published_at": "2023-09-21"}, "identity_proof": [{"type": "verified_domain_first_party_job"}]}
        self.assertTrue(direct_job_url(role))
        self.assertFalse(active_exact_job(role))

    def test_newest_accounts_are_selected_before_truncating_history(self):
        records = [{"id": str(year), "regnskapsperiode": {"tilDato": f"{year}-12-31"}} for year in (2022, 2023, 2024, 2025)]
        self.assertEqual([record["record_id"] for record in normalize_financials(records)["records"]], ["2025", "2024", "2023"])

    def test_company_accounts_precede_group_accounts_in_same_year(self):
        records = [{"id": "group", "regnskapstype": "KONSERN", "regnskapsperiode": {"tilDato": "2025-12-31"}},
                   {"id": "company", "regnskapstype": "SELSKAP", "regnskapsperiode": {"tilDato": "2025-12-31"}}]
        result = normalize_financials(records)["records"]
        self.assertEqual(result[0]["record_id"], "company")
        self.assertEqual(result[0]["account_type"], "SELSKAP")


if __name__ == "__main__":
    unittest.main()
