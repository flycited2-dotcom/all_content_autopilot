import asyncio

import pytest

from photo_auditor.source_routes import route, instruction, candidates
from photo_auditor.discovery import collect
from photo_auditor.research import search_product


@pytest.mark.parametrize('brand', ['CARVER', 'carver', 'Карвер'])
def test_generator_route_uses_owner_catalog(brand):
    value = route(brand, 'PPG-11000 DVR', '')
    assert value['domains'] == ['uralopt.ru']
    assert value['start_urls'][0] == 'https://uralopt.ru/products/Generators/'
    assert 'СНАЧАЛА' in instruction(brand, 'PPG-5000D', 'генератор')


@pytest.mark.parametrize('brand,model,category', [('LIFAN','PPG-5000D','генератор'),
    ('CARVER','RSG-252','бензопила'), ('','PPG-5000D','генератор')])
def test_route_does_not_capture_other_products(brand, model, category):
    assert route(brand, model, category) is None
    assert instruction(brand, model, category) == ''


def test_direct_catalog_links_preserve_exact_model_suffix_and_domain():
    fetched = []
    class Web:
        def fetch(self, url, maximum):
            fetched.append(url)
            return url, b'''<a href='/products/wrong'>CARVER PPG-5000DE</a>
            <a href='https://unrelated.ru/x'>CARVER PPG-5000D</a>
            <a href='/products/exact'>CARVER PPG-5000D</a>'''
    found = candidates({'brand':'CARVER','model':'PPG-5000D'}, Web())
    assert fetched[0] == 'https://uralopt.ru/products/Generators/'
    assert found == [{'url':'https://uralopt.ru/products/exact'}]


def test_browser_research_first_request_contains_catalog():
    seen = []
    async def browser(brand, model, category, **kwargs):
        seen.append((brand, model, category))
        return 'photo', 'verified facts'
    assert asyncio.run(search_product(browser, 'CARVER', 'PPG-11000 DVR', 'генератор')) == ('photo', 'verified facts')
    assert seen[0][1] == 'PPG-11000 DVR'
    assert 'https://uralopt.ru/products/Generators/' in seen[0][2]


def test_auditor_fetches_owner_catalog_before_any_model_search(tmp_path):
    calls = []
    class Web:
        def fetch(self, url, maximum):
            calls.append(url)
            return url, b'<a href="/products/exact">CARVER PPG-5000D</a>'
        def reference(self, candidate, model, cited, domains):
            return {**candidate,'official':True,'model_image_verified':True,'model':model}, b'photo', b'page'
    def forbidden(*args, **kwargs):
        pytest.fail('Verified owner catalog must precede model search')
    sources, _, _ = collect({'brand':'CARVER','model':'PPG-5000D'}, [], forbidden, Web(), tmp_path)
    assert sources[0]['url'] == 'https://uralopt.ru/products/exact'
    assert calls[0] == 'https://uralopt.ru/products/Generators/'


def test_missing_catalog_model_falls_back_to_scoped_search(tmp_path):
    scopes = []
    class Web:
        def fetch(self, url, maximum):
            return url, b'<a href="/products/other">CARVER PPG-5000DE</a>'
    def research(product, domains, remaining, failed_urls=()):
        scopes.append(domains)
        return [], []
    assert collect({'brand':'CARVER','model':'PPG-5000D'}, [], research, Web(), tmp_path)[0] == []
    assert scopes[0] == ['uralopt.ru']
