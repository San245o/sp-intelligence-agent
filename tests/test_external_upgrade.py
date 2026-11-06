from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]
from norway_company_agent import search_providers as providers, search, nav_jobs
from norway_company_agent.external_collectors import legal_proof, place_observation, parse_document, promote_site_lead
from norway_company_agent.identity import assess_website_identity
from norway_company_agent.refresh import diff_profile
from norway_company_agent.source_store import capture_source
from norway_company_agent.external_footprint import publishable_observation
from norway_company_agent.runtime_defaults import load_defaults
from norway_company_agent import runtime_defaults
from bs4 import BeautifulSoup


class Response:
    def __init__(self, data): self.data = json.dumps(data).encode()
    def read(self, limit=-1): return self.data[:limit]
    def __enter__(self): return self
    def __exit__(self, *args): return False


class ProviderTests(unittest.TestCase):
    def test_parallel_maps_calls_reserve_three_credits_each(self):
        def query(n):
            providers.set_search_company(str(n))
            return providers.provider_search('business '+str(n),kind='maps')
        with patch.object(providers,'urlopen',return_value=Response({'places':[],'credits':3})) as opened:
            with ThreadPoolExecutor(max_workers=12) as pool: list(pool.map(query,range(100)))
        self.assertEqual(opened.call_count,13)
        self.assertEqual(providers.provider_summary()['paid_credits_used'],39)
        self.assertLessEqual(providers.provider_summary()['paid_credits_used'],40)

    def test_search_plan_prioritizes_operating_company_within_total_allocation(self):
        profiles=[{'organisation_number':'123456789','name':'Example Holding AS','employees':1,'evidence':{'registry':{'value':{'forretningsadresse.adresse':'Gate 1','forretningsadresse.postnummer':'0123'}}}},
                  {'organisation_number':'987654321','name':'Workshop AS','employees':50,'website':'https://workshop.no','evidence':{'registry':{'value':{'forretningsadresse.adresse':'Gate 2','forretningsadresse.postnummer':'0123'}}}}]
        plan=providers.plan_search_budget(profiles,limit=1,per_company=2)
        self.assertEqual(sum(plan.values()),1)
        self.assertEqual(plan['987654321'],1)
        self.assertEqual(plan['123456789'],0)

    def test_empty_address_cannot_earn_readiness_points(self):
        profile={'organisation_number':'123456789','name':'Example AS','employees':100}
        plan=providers.plan_search_budget([profile],limit=1,per_company=2)
        self.assertEqual(profile['paid_discovery_readiness_score'],70)
        self.assertEqual(sum(plan.values()),0)

    def test_missing_payroll_can_receive_paid_credit_with_registered_identity_and_site(self):
        profile={'organisation_number':'123456789','name':'Example AS','employees':None,'website':'https://example.no','evidence':{'registry':{'value':{'forretningsadresse.adresse':'Gate 1','forretningsadresse.postnummer':'0123'}}}}
        self.assertEqual(providers.plan_search_budget([profile],limit=1)['123456789'],1)


    def test_exhausted_paid_budget_keeps_public_search_available(self):
        import io
        with tempfile.TemporaryDirectory() as directory, patch.object(search, 'SEARCH_CACHE_DIR', Path(directory)), \
             patch.object(search, 'provider_search', return_value=([],0,'paid_budget_exhausted')), \
             patch.object(search.urllib.request, 'urlopen', return_value=io.BytesIO(b'<a class="result-link" href="https://example.no">Example AS</a>')) as opened:
            search._cache.clear()
            with patch.object(search, '_open_until', 0):
                results, requests, status = search.search_company_websites('public fallback regression')
        self.assertEqual(status,'results')
        self.assertEqual(results[0]['url'],'https://example.no')
        self.assertEqual(requests,1)
        self.assertEqual(opened.call_count,1)

    def test_paid_empty_search_uses_public_fallback_without_repaying(self):
        import io
        with tempfile.TemporaryDirectory() as directory, patch.object(search,'SEARCH_CACHE_DIR',Path(directory)), \
             patch.object(search,'provider_search',return_value=([],1,'empty_result')) as paid, \
             patch.object(search.urllib.request,'urlopen',side_effect=lambda *a,**k:io.BytesIO(b'<a class="result-link" href="https://example.no">Example AS</a>')):
            search._cache.clear()
            with patch.object(search,'_open_until',0):
                first,calls,status=search.search_company_websites('paid empty fallback regression')
                search._cache.clear()
                second,cached_calls,_=search.search_company_websites('paid empty fallback regression')
        self.assertEqual(status,'results');self.assertEqual(calls,2)
        self.assertEqual(first,second);self.assertEqual(cached_calls,1)
        self.assertEqual(paid.call_count,1)

    def setUp(self):
        self.env = patch.dict('os.environ', {"SERPER_API_KEYS": "test-one,test-two,test-three", "TAVILY_API_KEY": "", "SIGNALPOST_USE_PUBLIC_DEMO_KEYS": "0"})
        self.env.start(); self.addCleanup(self.env.stop)
        providers.configure_search_budget(40, 2)
        providers._unavailable.clear()
        providers.set_search_company("test")

    def test_parallel_queries_cannot_exceed_shared_budget(self):
        def query(n):
            providers.set_search_company(str(n))
            return providers.provider_search("company " + str(n))
        with patch.object(providers, "urlopen", return_value=Response({"organic": []})) as opened:
            with ThreadPoolExecutor(max_workers=12) as pool: list(pool.map(query, range(100)))
        self.assertEqual(opened.call_count, 40)
        self.assertEqual(providers.provider_summary()["paid_requests_used"], 40)

    def test_per_company_cap_includes_failed_key_fallback(self):
        with patch.object(providers, "urlopen", side_effect=HTTPError("https://google.serper.dev/search", 401, "invalid", {}, None)) as opened:
            results, count, status = providers.provider_search("example")
        self.assertEqual(count, 2)
        self.assertEqual(opened.call_count, 2)
        self.assertEqual(status, "paid_budget_exhausted")
        self.assertNotIn("test-one", json.dumps(providers.provider_summary()))

    def test_account_throttle_does_not_rotate_to_another_key(self):
        with patch.object(providers, "urlopen", side_effect=HTTPError("https://google.serper.dev/search", 429, "throttled", {}, None)) as opened:
            _, count, _ = providers.provider_search("example")
        self.assertEqual(count, 1)
        self.assertEqual(opened.call_count, 1)

    def test_tavily_uses_header_and_returns_only_discovery_leads(self):
        with patch.dict('os.environ', {"SERPER_API_KEYS": "", "SERPER_API_KEY": "", "SERPER_API_KEY_2": "", "SERPER_API_KEY_3": "", "TAVILY_API_KEY": "test-tavily"}), patch.object(providers, "urlopen", return_value=Response({"results": [{"url": "https://example.no", "title": "Example AS", "content": "lead"}]})) as opened:
            results, count, _ = providers.provider_search("example")
        self.assertEqual(count, 1)
        request = opened.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer test-tavily")
        self.assertNotIn("test-tavily", request.data.decode())
        self.assertEqual(results[0]["provider"], "tavily")
        self.assertNotIn("exact_entity", results[0])

    def test_cached_leads_make_no_paid_call_and_preserve_source_time(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(search, "SEARCH_CACHE_DIR", Path(directory)), patch.object(providers, "urlopen", return_value=Response({"organic": [{"link": "https://example.no", "title": "Example AS"}]})) as opened:
            search._cache.clear()
            first, calls, _ = search.search_company_websites("unique cached test")
            search._cache.clear()
            second, cached_calls, _ = search.search_company_websites("unique cached test")
        self.assertEqual(calls, 1)
        self.assertEqual(cached_calls, 0)
        self.assertEqual(first, second)
        self.assertEqual(opened.call_count, 1)

    def test_private_defaults_and_environment_overrides_without_committed_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'private.json'
            with patch.object(runtime_defaults,'PRIVATE_CONFIG',path):
                self.assertEqual(load_defaults(),[])
                path.write_text(json.dumps({'keys':['local-one','local-two','local-three']}),encoding='utf-8')
                self.assertEqual(load_defaults(),['local-one','local-two','local-three'])
                self.assertEqual(providers.credentials()[0],["test-one","test-two","test-three"])


class TypedEvidenceTests(unittest.TestCase):
    def test_news_results_do_not_become_the_review_and_careers_search_pool(self):
        from norway_company_agent.external_collectors import collect_external
        from scripts.run_competition_batch import WebRequestBudget,CompanyWebRequestBudget
        with patch('norway_company_agent.external_collectors.search_company_websites',return_value=([],0,'empty_result')) as searched:
            collect_external(self.profile(),CompanyWebRequestBudget(WebRequestBudget(100),30),categories=('news','reviews','careers'))
        self.assertEqual([call.kwargs['kind'] for call in searched.call_args_list],['news','search','search'])
        self.assertIn('anmeldelser',searched.call_args_list[1].args[0])
        self.assertIn('stilling',searched.call_args_list[2].args[0])

    def test_third_party_deep_page_is_not_homepage_ownership_proof(self):
        p=self.profile()
        p['evidence']['website']={'status':'available','value':{'final_url':'https://publisher.no/customer/example',
            'title':'Example AS','identity_text_excerpt':'Example AS 923609016 Bergen',
            'ownership_homepage_required':True,'ownership_homepage':{'title':'Publisher AS','identity_text_excerpt':'Publisher AS 999111222'},
            'pages':[{'title':'Example AS','identity_text_excerpt':'Example AS 923609016 Bergen'}]}}
        self.assertFalse(assess_website_identity(p)['publishable'])

    def test_news_platform_label_does_not_make_first_party_posts_independent(self):
        from scripts.audit_evidence_metrics import evaluate
        p=self.profile();p['evidence']['website']={'status':'not_found'}
        base={'organisation_number':p['organisation_number'],'platform':'news','signal_type':'public_post','source_url':'https://publisher.no/story','retrieved_at':'2026-11-04','content_sha256':'a'*64,'exact_entity':True,'rights_status':'approved','published_at':'2026-11-01'}
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'run-report.json').write_text('{}',encoding='utf-8')
            p['evidence']['external_footprint']={'value':{'observations':[{**base,'source_class':'company_news','identity_proof':[{'method':'verified_domain_first_party_activity'}]}]}}
            (root/'profiles.jsonl').write_text(json.dumps(p)+'\n',encoding='utf-8')
            first_party,_=evaluate(root)
            self.assertEqual(first_party.get('common_gate_dated_news_items',0),0)
            p['evidence']['external_footprint']['value']['observations']=[{**base,'source_class':'public_news','identity_proof':[{'type':'captured_exact_organisation_number'}]}]
            (root/'profiles.jsonl').write_text(json.dumps(p)+'\n',encoding='utf-8')
            independent,_=evaluate(root)
            self.assertEqual(independent.get('common_gate_dated_news_items',0),1)

    def test_places_without_registered_street_do_not_spend_a_query(self):
        from norway_company_agent.external_collectors import collect_external
        p=self.profile();p['evidence']['registry']['value']['forretningsadresse.adresse']=''
        with patch('norway_company_agent.external_collectors.search_company_websites') as search_call:
            _,status,metrics=collect_external(p,None,categories=('places',))
        search_call.assert_not_called()
        self.assertEqual(metrics['requests'],0)
        self.assertEqual(status['places']['status'],'skipped_no_exact_place_opportunity')

    def test_registry_listing_never_becomes_company_owned_site(self):
        for url in ('https://virksomhet.brreg.no/nb/oppslag/enheter/923609016', 'https://www.vexter.no/selskap/example/923609016', 'https://regnskapstall.no/informasjon-om-example'):
            profile = self.profile()
            profile['evidence']['website'] = {'status': 'available', 'value': {'final_url': url, 'title': 'Example AS', 'identity_text_excerpt': 'Example AS Org 923609016'}}
            self.assertFalse(assess_website_identity(profile)['publishable'])

    def test_publisher_article_with_org_number_is_not_company_website(self):
        p=self.profile();p['evidence']['website']={'status':'available','value':{'final_url':'https://publisher.no/article','title':'Example AS wins contract','identity_text_excerpt':'Example AS 923609016 Bergen','article_document':True,'structured_organisations':[{'name':'Publisher AS','url':'https://publisher.no'}]}}
        self.assertFalse(assess_website_identity(p)['publishable'])

    def test_place_summary_is_available_to_cited_research_answers(self):
        from norway_company_agent.research import answer_profile
        p=self.profile();p['evidence']['external_footprint']={'status':'available','value':{'observations':[{'organisation_number':p['organisation_number'],'signal_type':'place_summary','platform':'google_places','source_url':'https://www.google.com/maps?cid=12','retrieved_at':'2026-11-04','content_sha256':'a'*64,'identity_proof':[{'type':'exact_address'}],'exact_entity':True,'rights_status':'approved','metrics':{'rating':4.4,'review_count':20}}]}}
        answer=answer_profile(p,'What reviews and ratings were found?')
        self.assertTrue(any(f.get('value',{}).get('metrics',{}).get('rating')==4.4 for f in answer['facts']))

    def test_publisher_story_cannot_be_promoted_to_owned_website(self):
        profile = self.profile(); profile['evidence'].pop('website')
        document = self.document({'@type': 'NewsArticle', 'datePublished': '2026-11-01'}, text='Example AS 923609016 Bergen', url='https://publisher.no/story')
        self.assertEqual(promote_site_lead(profile, document), [])
        self.assertNotIn('website', profile['evidence'])

    def test_external_refresh_ignores_retrieval_time_and_unconfirmed_absence(self):
        import copy
        previous = self.profile()
        item = {'id': 'place', 'organisation_number': previous['organisation_number'], 'exact_entity': True, 'rights_status': 'approved', 'platform': 'google_places', 'signal_type': 'place_summary', 'source_url': 'https://maps.google.com/?cid=12', 'retrieved_at': '2026-11-01', 'content_sha256': 'a'*64, 'metrics': {'rating': 4.4, 'review_count': 20}}
        previous['evidence']['external_footprint'] = {'value': {'observations': [item]}}
        current = copy.deepcopy(previous); current['evidence']['external_footprint']['value']['observations'][0]['retrieved_at'] = '2026-11-04'
        self.assertEqual(diff_profile(previous,current), [])
        current['evidence']['external_footprint']['value']['observations'][0]['metrics']['review_count'] = 21
        self.assertEqual(len(diff_profile(previous,current)), 1)
        current['evidence']['external_footprint']['value']['observations'] = []
        self.assertEqual(diff_profile(previous,current), [])

    def profile(self):
        return {"organisation_number": "923609016", "name": "Example AS", "municipality": "Bergen",
            "evidence": {"registry": {"value": {"forretningsadresse.adresse": "Testveien 12", "forretningsadresse.postnummer": "5000"}},
                "website": {"value": {"final_url": "https://example.no", "identity_assessment": {"content_attributable": True}}}}}

    def document(self, node, text="Example AS Bergen", url="https://publisher.no/story"):
        raw = ('<html><title>Example AS</title><script type="application/ld+json">' + json.dumps(node) + '</script><p>' + text + '</p></html>').encode()
        return {**capture_source(raw), "url": url, "raw": raw, "soup": BeautifulSoup(raw, "lxml"), "text": text,
            "content_sha256": __import__('hashlib').sha256(raw).hexdigest(), "retrieved_at": "2026-11-04T10:00:00+00:00", "content_type": "text/html"}

    def test_namesake_without_legal_corroboration_abstains(self):
        self.assertFalse(legal_proof(self.profile(), "Example makes products in California"))
        self.assertFalse(legal_proof(self.profile(), "Other AS Bergen"))

    def test_place_rating_requires_exact_registered_address(self):
        capture = capture_source(b'{"rating":4.4,"ratingCount":20}')
        lead = {**capture, "content_sha256": capture["snapshot_sha256"], "url": "https://www.google.com/maps?cid=12", "retrieved_at": "2026-11-04T10:00:00+00:00", "provider": "serper",
            "place_lead": {"title": "Example AS", "address": "Testveien 12, 5000 Bergen", "rating": 4.4, "ratingCount": 20}}
        item = place_observation(self.profile(), lead)
        self.assertTrue(publishable_observation(item))
        self.assertNotIn("sentiment_label", item)
        lead["place_lead"]["address"] = "Testveien 12, 6000 Other"
        self.assertIsNone(place_observation(self.profile(), lead))

    def test_company_marketing_cannot_become_independent_news_or_review(self):
        node = {"@type": "Article", "datePublished": "2026-11-01", "headline": "We are the best"}
        output, _ = parse_document(self.profile(), self.document(node, url="https://example.no/news"), category="news")
        self.assertEqual(output, [])

    def test_workforce_uses_matching_legal_schema_not_site_builder(self):
        nodes = [{"@type": "Organization", "name": "Example AS", "taxID": "923609016", "numberOfEmployees": 8},
                 {"@type": "Organization", "name": "Builder AS", "taxID": "999999999", "numberOfEmployees": 900}]
        output, _ = parse_document(self.profile(), self.document(nodes, url="https://example.no/about"), category="workforce")
        self.assertEqual(len(output), 1)
        self.assertEqual(output[0]["metrics"]["employees"], 8)

    def test_review_polarity_is_explicitly_rating_derived(self):
        node = {"@type": "Review", "itemReviewed": {"name": "Example AS", "taxID": "923609016"}, "reviewRating": {"ratingValue": 2, "bestRating": 5}, "reviewBody": "The delivered item was damaged."}
        output, _ = parse_document(self.profile(), self.document(node), category="reviews")
        self.assertEqual(output[0]["sentiment_label"], "negative")
        self.assertEqual(output[0]["sentiment_basis"], "individual_review_rating_not_text_inference")


class IncrementalNavTests(unittest.TestCase):
    def baseline(self):
        now = datetime.now(timezone.utc)
        return {"retrieved_at": (now - timedelta(hours=2)).isoformat(), "entries": {
            "keep": {"uuid": "keep", "modified": now.isoformat(), "status": "ACTIVE", "business_name": "Unrelated", "url": "/api/v1/feedentry/keep"},
            "remove": {"uuid": "remove", "modified": (now-timedelta(days=1)).isoformat(), "status": "ACTIVE", "business_name": "Unrelated", "url": "/api/v1/feedentry/remove"}}, "pages": 91, "age_seconds": 7200}

    def test_incremental_refresh_keeps_known_ads_and_applies_inactivation(self):
        baseline = self.baseline()
        changed = {"items": [{"uuid": "remove", "date_modified": datetime.now(timezone.utc).isoformat(), "status": "INACTIVE"}]}
        with patch.object(nav_jobs, "_name_index", return_value=({}, {})), patch.object(nav_jobs, "_token", return_value=("test-token", None)), patch.object(nav_jobs, "load_snapshot", side_effect=[None, baseline]), patch.object(nav_jobs, "_request", return_value=(200, {}, json.dumps(changed).encode(), None)) as request, patch.object(nav_jobs, "save_snapshot") as save:
            result = nav_jobs.discover_nav_jobs([], None, None, delay_seconds=0)
        self.assertTrue(result.complete)
        self.assertTrue(result.feed_incremental_refresh)
        entries = save.call_args.args[0]
        self.assertEqual(entries["keep"]["status"], "ACTIVE")
        self.assertEqual(entries["remove"]["status"], "INACTIVE")
        self.assertIn("If-Modified-Since", request.call_args.kwargs["headers"])

    def test_partial_incremental_refresh_never_overwrites_complete_baseline(self):
        with patch.object(nav_jobs, "_name_index", return_value=({}, {})), patch.object(nav_jobs, "_token", return_value=("test-token", None)), patch.object(nav_jobs, "load_snapshot", side_effect=[None, self.baseline()]), patch.object(nav_jobs, "_request", return_value=(0, {}, b'', 'timeout')), patch.object(nav_jobs, "save_snapshot") as save:
            result = nav_jobs.discover_nav_jobs([], None, None, delay_seconds=0)
        self.assertFalse(result.complete)
        save.assert_not_called()
