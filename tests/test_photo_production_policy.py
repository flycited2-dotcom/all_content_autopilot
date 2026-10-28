import json

import pytest

from photo_auditor.engine import Auditor, decision
from tests.test_photo_auditor import comparison, picture
from tests.test_photo_content_review import Provider, run, result, SOURCES


def test_brand_without_official_domain_can_finish_against_exact_verified_catalog(tmp_path):
    (tmp_path / 'rules').mkdir()
    (tmp_path / 'rules/photo-auditor-manufacturers.yaml').write_text('{}')
    folder = tmp_path / 'audit'
    folder.mkdir()
    (folder / 'generated.bin').write_bytes(picture())
    product = {'product_id': 'new-brand-product', 'brand': 'Unknown Brand', 'model': 'ABC-12-RU', 'revision': 0}
    events = []

    class Provider:
        def inspect(self, instruction, content, search=False, domains=()):
            if search:
                events.append('search')
                return {'text': json.dumps({'sources': [{'url': 'https://catalog.example/exact'}]}),
                        'sources': ['https://catalog.example/exact']}
            events.append('visual')
            value = comparison()
            value.update(limitations=[], source_checks=[{'id': 1, 'main_product_visible': True,
                'suitable_for_repair': True, 'reason': 'The whole exact product is visible.'}])
            return {'text': json.dumps(value)}

    class Web:
        def reference(self, candidate, model, cited, domains):
            return {**candidate, 'model': model, 'official': False, 'model_image_verified': True,
                    'image_url': candidate['url'] + '.png'}, picture(), b'exact model page'

    report = Auditor(tmp_path, Provider(), Web()).run(product, folder)
    assert report['verdict'] == 'approved'
    assert report['evidence_basis'] == 'catalog_reference'
    assert events == ['search', 'visual', 'visual']


@pytest.mark.parametrize('change', [{'model_image_verified': False}, {'main_product_visible': False}])
def test_unverified_or_accessory_reference_still_cannot_approve(change):
    sources = [{'official': False, 'model_image_verified': True, 'main_product_visible': True,
                'url': 'https://catalog.example/exact', **change}]
    assert decision(comparison(), sources, manufacturer_unavailable=True)[0] == 'review'


def test_conflict_remains_review_and_confirmed_visible_error_still_requires_correction():
    sources = [{'official': False, 'model_image_verified': True, 'url': 'https://catalog.example/exact'}]
    unknown = comparison()
    unknown['checks'][0]['status'] = 'not_verifiable'
    assert decision(unknown, sources, manufacturer_unavailable=True)[0] == 'review'
    assert decision(comparison('mismatch'), sources, manufacturer_unavailable=True)[0] == 'regenerate'


def test_matching_tv_perspective_does_not_create_a_false_production_hold():
    sources=[{'official':False,'model_image_verified':True,'url':'https://catalog.example/exact'}]
    value=comparison()
    for check,confidence in zip(value['checks'],(.94,.94,.93,.88,.95,.90)):
        check['confidence']=confidence
    assert decision(value,sources,manufacturer_unavailable=True)[0]=='approved'
    value['checks'][3]['confidence']=.84
    assert decision(value,sources,manufacturer_unavailable=True)[0]=='review'
    value['checks'][3].update(status='not_verifiable',confidence=.99)
    assert decision(value,sources,manufacturer_unavailable=True)[0]=='review'


def omission():
    value = result()
    value['claims'][2].update(status='unsupported', evidence=[], repair_action='remove_claim',
        required_by_brief=False, fix='Удалить неподтверждённую надпись о мощности.')
    return value


def test_unconfirmed_optional_copy_is_corrected_instead_of_stopping_the_factory():
    from photo_auditor.content_review import feedback
    report = run(Provider(omission()))
    assert report['verdict'] == 'regenerate'
    assert 'Удалить неподтверждённую' in feedback(report)


@pytest.mark.parametrize('change', [{'required_by_brief': True}, {'kind': 'identity'},
                                   {'kind': 'commercial'}, {'required_by_brief': 0}, {'fix': ''}])
def test_automatic_omission_cannot_remove_identity_business_terms_or_required_content(change):
    value = omission()
    value['claims'][2].update(change)
    with pytest.raises(ValueError):
        run(Provider(value))


def test_uncertain_omission_does_not_authorize_paid_correction():
    value = omission()
    value['claims'][2]['confidence'] = .7
    assert run(Provider(value))['verdict'] == 'review'


def test_fetched_exact_model_page_is_evidence_even_without_search_citation_metadata():
    import httpx,socket
    from photo_auditor.sources import PublicWeb
    def handler(request):
        if request.url.path == '/product':
            return httpx.Response(200,text='<h1>Blackton TP433</h1><meta property="og:image" content="/real.png"><img src="/related.png">')
        return httpx.Response(200,content=picture('blue' if request.url.path=='/real.png' else 'red'))
    resolver=lambda *args,**kwargs:[(socket.AF_INET,0,0,'',('93.184.216.34',443))]
    web=PublicWeb(httpx.Client(transport=httpx.MockTransport(handler)),resolver)
    source,photo,_=web.reference({'url':'https://catalog.example/product','image_url':'https://catalog.example/related.png'},'TP433',[],[])
    assert source['model_image_verified'] is True
    assert photo==picture('blue')


def test_pending_research_source_url_is_reused_without_inheriting_approval(tmp_path,monkeypatch):
    from photo_auditor import runtime,progress,connected_research
    monkeypatch.setattr(runtime,'load',lambda _: {'api_token':'test'})
    runtime.register_job(tmp_path,3820,'research','Blackton','TP433','Термопоты')
    progress.save_research(tmp_path,'fotogen-3820','Blackton','TP433',json.dumps({'exact_model':True,
        'source_url':'https://blackton.tv/catalog/tp433/','photo_url':'https://blackton.tv/a.jpg','features':['x']}))
    product={'product_id':'fotogen-3820','brand':'Blackton','model':'TP433','revision':0}
    found=connected_research.cached_candidates(tmp_path,product,['blackton.tv'],5)
    assert found==([{'url':'https://blackton.tv/catalog/tp433/'}],['https://blackton.tv/catalog/tp433/'])
    assert connected_research.cached_candidates(tmp_path,product,['blackton.tv'],5,['https://blackton.tv/catalog/tp433/']) is None
