from __future__ import annotations
import hashlib,json,sys,unittest
from pathlib import Path
from unittest.mock import patch
from bs4 import BeautifulSoup
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT)]
from norway_company_agent.identity import assess_website_identity,apply_website_identity_gate
from norway_company_agent.external_collectors import collect_external
from norway_company_agent.source_store import capture_source
from scripts.run_competition_batch import WebRequestBudget,CompanyWebRequestBudget

class RecallRegressions(unittest.TestCase):
    def profile(self):
        return {'organisation_number':'923609016','name':'Example AS','employees':5,'municipality':'Bergen','industry_code':'47.100','evidence':{'registry':{'value':{'forretningsadresse.adresse':'Testveien 12','forretningsadresse.postnummer':'5000'}},'website':{'status':'available','retrieved_at':'2026-11-05T00:00:00Z','value':{'final_url':'https://example.no','title':'Example','identity_text_excerpt':'Example','content_sha256':'a'*64,'identity_assessment':{'publishable':True,'content_attributable':True},'pages':[]}}}}
    def test_scoped_schema_identifier_verifies_owner_without_visible_org(self):
        p=self.profile();v=p['evidence']['website']['value']
        v['structured_organisations']=[{'@type':'Organization','url':'https://example.no','name':'Example','identifier':{'value':'NO923609016MVA'}}]
        self.assertTrue(assess_website_identity(p)['content_attributable'])
        v['structured_organisations'][0]['url']='https://publisher.no'
        v['title']='Unrelated';v['identity_text_excerpt']='Unrelated'
        self.assertFalse(assess_website_identity(p)['publishable'])
    def test_contact_org_corroborates_homepage_brand_but_article_does_not(self):
        p=self.profile();v=p['evidence']['website']['value'];v['final_url']='https://brand.com'
        v['pages']=[{'url':'https://brand.com/contact','identity_text_excerpt':'Example AS Org 923 609 016'}]
        self.assertTrue(assess_website_identity(p)['publishable'])
        v['pages'][0]['url']='https://brand.com/news/customer'
        self.assertFalse(assess_website_identity(p)['publishable'])
    def test_opaque_youtube_sameas_requires_owned_organization(self):
        p=self.profile();v=p['evidence']['website']['value'];v['structured_organisations']=[{'@type':'Organization','url':'https://example.no','name':'Example AS','taxID':'923609016','sameAs':['https://www.youtube.com/channel/UCabcdefghijklmnopqrstuv']}]
        result=apply_website_identity_gate(p,p['evidence']['website'])
        self.assertTrue(any(o['publishable'] for o in result['website']['value']['social_link_assessments']))
    def test_places_precedes_paid_news_and_careers_query_is_free(self):
        p=self.profile();budget=CompanyWebRequestBudget(WebRequestBudget(100),60)
        with patch('norway_company_agent.external_collectors.search_company_websites',return_value=([],0,'empty_result')) as searched:
            collect_external(p,budget)
        self.assertEqual(searched.call_args_list[0].kwargs['kind'],'places')
        career=next(c for c in searched.call_args_list if 'stilling' in c.args[0])
        self.assertFalse(career.kwargs['use_paid'])
    def test_report_link_filter_happens_before_limit(self):
        p=self.profile();raw=b'<a href="/careers">Careers</a><a href="/jobs">Jobs</a><a href="/annual-report.pdf">Report</a>'
        capture=capture_source(raw);p['evidence']['website']['value'].update(capture,content_sha256=capture['snapshot_sha256'])
        with patch('norway_company_agent.external_collectors.acquire',return_value=None) as fetch,patch('norway_company_agent.external_collectors.search_company_websites',return_value=([],0,'empty_result')):
            collect_external(p,CompanyWebRequestBudget(WebRequestBudget(100),60),categories=('workforce',))
        self.assertEqual(fetch.call_args_list[0].args[0],'https://example.no/annual-report.pdf')

class OwnershipRegressions(unittest.TestCase):
    def test_directory_entry_title_cannot_brand_the_owner_homepage(self):
        p={'organisation_number':'923609016','name':'ALFA EXAMPLE AS','municipality':'Bergen','evidence':{'registry':{'value':{'forretningsadresse.adresse':'Testveien 12','forretningsadresse.postnummer':'5000'}},'website':{'status':'available','value':{'final_url':'https://directory.test/vendors/alfa-example','title':'Alfa Example AS','identity_text_excerpt':'Alfa Example AS 923609016 Testveien 12 5000 Bergen','ownership_homepage_required':True,'ownership_homepage':{'url':'https://directory.test/','title':'Find Example vendors','identity_text_excerpt':'All suppliers across Norway'},'pages':[{'url':'https://directory.test/vendors/alfa-example','title':'Alfa Example AS','identity_text_excerpt':'Alfa Example AS 923609016 Testveien 12 5000 Bergen'}]}}}}
        self.assertFalse(assess_website_identity(p)['publishable'])
    def test_same_host_listing_schema_is_not_homepage_owner_schema(self):
        p=RecallRegressions().profile();v=p['evidence']['website']['value'];v['title']='Directory';v['identity_text_excerpt']='Directory';v['final_url']='https://directory.test/'
        v['structured_organisations']=[{'@type':'Organization','url':'https://directory.test/company/example','name':'Example AS','taxID':'923609016'}]
        self.assertFalse(assess_website_identity(p)['publishable'])
    def test_contradictory_organization_cannot_supply_opaque_sameas(self):
        p=RecallRegressions().profile();v=p['evidence']['website']['value'];v['identity_text_excerpt']='Example AS Org 923609016'
        v['structured_organisations']=[{'@type':'Organization','url':'https://foreign.test','name':'Example AS','taxID':'999111222','sameAs':['https://youtube.com/channel/UCabcdefghijklmnopqrstuv']}]
        result=apply_website_identity_gate(p,p['evidence']['website'])
        self.assertFalse(result['website']['value']['social_links'])
    def test_single_letter_name_distinguishes_company_from_namesake(self):
        p=RecallRegressions().profile();p['name']='Marianne S AS';v=p['evidence']['website']['value'];v.update(final_url='https://marianne.no',title='Marianne Juvik Saebo',identity_text_excerpt='Marianne singer')
        self.assertFalse(assess_website_identity(p)['publishable'])

class PublicationQualityRegressions(unittest.TestCase):
    def test_hostname_echo_and_coming_placeholder_are_not_identity(self):
        p=RecallRegressions().profile();p['name']='EL SERVICE AS';v=p['evidence']['website']['value'];v.update(final_url='https://el-service.no/',title='www.el-service.no',identity_text_excerpt='www.el-service.no Her kommer: www.el-service.no')
        self.assertFalse(assess_website_identity(p)['publishable'])
    def test_news_before_exact_entity_founding_is_withheld(self):
        from norway_company_agent.news_quality import publication_eligibility
        p=RecallRegressions().profile();p['evidence']['registry']['value']['stiftelsesdato']='2023-02-13'
        self.assertEqual(publication_eligibility(p,'2022-08-16','https://example.no/news/bmo','Sibling company recruitment'),(False,'predates_exact_legal_entity'))
        self.assertEqual(publication_eligibility(p,'2023-02-13','https://example.no/news/open','Opening'),(True,None))
    def test_default_wordpress_post_is_not_company_news(self):
        from norway_company_agent.news_quality import publication_eligibility
        self.assertEqual(publication_eligibility(RecallRegressions().profile(),'2026-11-01','https://example.no/hello-world/','Hello world! This is your first post. Edit or delete it.'),(False,'default_template_content'))
    def test_legitimate_hello_world_topic_without_template_instructions_is_retained(self):
        from norway_company_agent.news_quality import publication_eligibility
        self.assertTrue(publication_eligibility(RecallRegressions().profile(),'2026-11-01','https://example.no/news/new-course','Our Hello World course opens in Bergen')[0])

class EntryForwardingRegression(unittest.TestCase):
    def test_unknown_option_zero_is_not_consumed_as_positional_manifest(self):
        import tempfile
        from scripts import run_research_batch as entry
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);bulk=root/'bulk.gz';bulk.write_bytes(b'fixture')
            forwarded=[]
            def stop(path,run_name):
                forwarded.extend(sys.argv)
                raise SystemExit(37)
            with patch.object(entry,'_load_manifest',return_value=[{'organisation_number':'923609016'}]),patch.object(entry.runpy,'run_path',side_effect=stop):
                with self.assertRaises(SystemExit):entry.main(['--manifest','fixture.jsonl','--out',str(root/'result'),'--bulk',str(bulk),'--web-requests-per-company','0','--nav-window-days','0'])
            self.assertEqual(forwarded[forwarded.index('--web-requests-per-company')+1],'0')
            self.assertEqual(forwarded[forwarded.index('--nav-window-days')+1],'0')


    def test_thousand_company_entry_forwards_500_credit_default_and_zero_override(self):
        import tempfile
        from scripts import run_research_batch as entry
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);bulk=root/'bulk.gz';bulk.write_bytes(b'fixture')
            rows=[{'organisation_number':str(900000000+i)} for i in range(1000)]
            for extra,expected in [([], '500'),(['--paid-search-budget','0'], '0')]:
                forwarded=[]
                def stop(path,run_name):
                    forwarded.extend(sys.argv)
                    raise SystemExit(37)
                with patch.object(entry,'_load_manifest',return_value=rows),patch.object(entry.runpy,'run_path',side_effect=stop):
                    with self.assertRaises(SystemExit):entry.main(['--manifest','fixture.jsonl','--out',str(root/'result'),'--bulk',str(bulk),*extra])
                self.assertEqual(forwarded[forwarded.index('--paid-search-budget')+1],expected)

class ContractRegression(unittest.TestCase):
    def test_contract_does_not_publish_unverified_website_or_manufacture_hash(self):
        from scripts.run_research_batch import contract_envelope
        profile={'organisation_number':'923609016','name':'Example AS','evidence':{'website':{'status':'available','value':{'final_url':'https://publisher.no','identity_assessment':{'publishable':False}}}}}
        result=contract_envelope({'profile':profile,'run_id':'test','started_at':'2026-11-05','completed_at':'2026-11-05','state':'complete'})
        website=next(c for c in result['claims'] if c['field']=='official_website')
        self.assertEqual(website['availability'],'ambiguous');self.assertIsNone(website['value'])
        self.assertEqual(result['evidence'],[])

if __name__=='__main__':unittest.main()
