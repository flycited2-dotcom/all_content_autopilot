"""Reuse authenticated research addresses without transferring its approval."""
import io
import json
import time

import pytest
from PIL import Image

from listener.pending import write_json
from photo_auditor import connected_research, runtime
from photo_auditor.discovery import collect
from photo_auditor.evidence import eligible
from photo_auditor.images import digest
from photo_auditor.store import AuditStore


SKU = 'L43MB-APRU'
TITLE = 'MI TV A PRO 2026 L43MB-APRU QLED 4K SmartTV Android'
PRODUCT = {'product_id': 'fotogen-3819', 'brand': 'Xiaomi', 'model': TITLE, 'revision': 0}
URLS = [f'https://shop{i}.example/p' for i in range(1, 6)]


def picture(color='white'):
    output = io.BytesIO()
    Image.new('RGB', (80, 80), color).save(output, 'PNG')
    return output.getvalue()


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, 'load', lambda _: {'api_token': 'research-handoff-test'})
    return tmp_path


def approve(root, *, job_id=3817, mode='research', brand='Xiaomi', model=SKU, urls=URLS, status='approved'):
    runtime.register_job(root, job_id, mode, brand, model, 'Телевизоры')
    store = AuditStore(root / 'runs/photo-auditor')
    product = {'product_id': f'fotogen-{job_id}', 'brand': brand, 'model': model, 'revision': 0}
    row = store.submit(product, picture())
    sources = [{'id': i, 'url': url, 'image_url': url + '.png', 'model': model,
                'official': url.startswith('https://mi.com/'), 'model_image_verified': True,
                'text': 'Old text must never become new facts.'} for i, url in enumerate(urls, 1)]
    store.finish(store.claim(audit_id=row['id']), {
        'verdict': status, 'reason': 'Recorded research decision.', 'checks': [],
        'sources': sources, 'dimensions_note': 'Не измерены'})
    return store, store.get(row['id'])


def test_short_research_sku_hands_addresses_to_descriptive_card_without_source_payload(root):
    approve(root)
    candidates, cited = connected_research.cached_candidates(root, PRODUCT, (), 5)
    assert cited == URLS
    assert candidates == [{'url': url} for url in URLS]
    assert all(set(candidate) == {'url'} for candidate in candidates)


@pytest.mark.parametrize('sku,title', [
    ('L55MC-SRU', 'TV S Mini LED 55 2026 L55MC-SRU 4K SmartTV Android'),
    ('L65MB-APRU', 'MI TV A PRO 2026 L65MB-APRU QLED 4K SmartTV Android'),
])
def test_other_exact_xiaomi_skus_keep_diagonal_and_suffix(root, sku, title):
    approve(root, model=sku)
    assert connected_research.cached_candidates(root, {**PRODUCT, 'model': title}, (), 1)[1] == URLS[:1]


@pytest.mark.parametrize('change', [
    {'brand': 'Other'}, {'model': 'MI TV A PRO 2026 L65MB-APRU QLED'},
    {'model': 'MI TV A PRO 2026 L43MB-APRU2 QLED'},
    {'model': 'MI TV A PRO 2026 L43MB-APRU-2 QLED'},
    {'model': 'MI TV L43MB-APRU и L65MB-APRU'},
])
def test_wrong_or_ambiguous_target_cannot_reuse_research_addresses(root, change):
    approve(root)
    assert connected_research.cached_candidates(root, {**PRODUCT, **change}, (), 5) is None


def test_domain_filters_exclusions_and_remaining_count_survive_handoff(root):
    approve(root, urls=['https://mi.com/p', *URLS])
    assert connected_research.cached_candidates(root, PRODUCT, ['mi.com'], 5) == (
        [{'url': 'https://mi.com/p'}], ['https://mi.com/p'])
    assert connected_research.cached_candidates(root, PRODUCT, ['mi.com'], 5, ['https://mi.com/p']) is None
    assert connected_research.cached_candidates(root, PRODUCT, (), 2, ['https://mi.com/p', URLS[0]])[1] == URLS[1:3]
    assert connected_research.cached_candidates(root, PRODUCT, (), 5, ['https://mi.com/p', *URLS]) is None
    assert connected_research.cached_candidates(root, PRODUCT, (), 0) is None


@pytest.mark.parametrize('change', ['missing', 'tampered', 'scope_identity', 'card', 'review', 'expired', 'newer'])
def test_only_recent_latest_approved_authenticated_research_is_eligible(root, change):
    store, row = approve(root, mode='ready_tv' if change == 'card' else 'research',
                         status='review' if change == 'review' else 'approved')
    path = root / 'runs/photo-auditor/job-scope/3817.json'
    if change == 'missing':
        path.unlink()
    elif change in ('tampered', 'scope_identity'):
        signed = json.loads(path.read_text(encoding='utf-8'))
        signed['payload']['model'] = 'L65MB-APRU'
        if change == 'scope_identity':
            signed['signature'] = runtime._signature(root, signed['payload'])
        write_json(path, signed)
    elif change == 'expired':
        with store.connect() as db:
            db.execute('UPDATE audits SET created=?', (time.time() - 86401,))
    elif change == 'newer':
        # A newer unresolved revision makes the old decision ineligible even if
        # the old row still says approved after an external legacy migration.
        with store.connect() as db:
            db.execute("INSERT INTO audits(id,product_id,revision,product,sha256,status,created) VALUES(?,?,?,?,?,'queued',?)",
                       ('newer', row['product_id'], 1, json.dumps({**row['product'], 'revision': 1}), row['sha256'], time.time()))
    assert connected_research.cached_candidates(root, PRODUCT, (), 5) is None


@pytest.mark.parametrize('source_change', [
    {'model': 'L65MB-APRU'}, {'model': 'L43MB-APRU2'}, {'model_image_verified': False},
    {'url': 'http://shop.example/p'}, {'url': 'https://user:secret@shop.example/p'},
])
def test_unbound_wrong_model_or_invalid_address_is_not_reused(root, source_change):
    store, row = approve(root, urls=URLS[:1])
    value = row['report']
    value['sources'][0].update(source_change)
    with store.connect() as db:
        db.execute('UPDATE audits SET report=?', (json.dumps(value),))
    assert connected_research.cached_candidates(root, PRODUCT, (), 5) is None


def test_missing_database_returns_none_without_creating_a_queue(root):
    assert connected_research.cached_candidates(root, PRODUCT, (), 5) is None
    assert not (root / 'runs/photo-auditor/audit.sqlite3').exists()


def test_exhausted_or_absent_normal_cache_falls_back_to_authenticated_addresses(root):
    approve(root)
    cache = root / 'runs/photo-auditor/research-cache' / connected_research.cache_key(PRODUCT['brand'], PRODUCT['model'])
    folder = cache / ('a' * 32)
    folder.mkdir(parents=True)
    data = json.dumps({'sources': [{'url': 'https://cached.example/p'}], 'errors': []}).encode()
    (folder / 'sources.json').write_bytes(data)
    write_json(cache / 'complete.json', {
        'brand': PRODUCT['brand'], 'model': PRODUCT['model'], 'created': time.time(),
        'folder': folder.name, 'sources_sha256': digest(data)})
    assert connected_research.cached_candidates(root, PRODUCT, (), 1)[1] == ['https://cached.example/p']
    assert connected_research.cached_candidates(root, PRODUCT, (), 2, ['https://cached.example/p'])[1] == URLS[:2]
    (cache / 'complete.json').write_text('{invalid', encoding='utf-8')
    assert connected_research.cached_candidates(root, PRODUCT, (), 1)[1] == URLS[:1]


def test_collect_refetches_pages_and_photos_and_keeps_independent_quorum(root, tmp_path):
    store, old = approve(root)
    before = store.path.read_bytes()
    destination = tmp_path / 'new-evidence'
    destination.mkdir()
    fetched, live_search = [], []
    fresh_photo = picture('navy')
    class Web:
        def reference(self, candidate, model, cited, domains):
            assert model == TITLE and candidate['url'] in cited
            fetched.append(candidate['url'])
            return {**candidate, 'image_url': candidate['url'] + '/new.png', 'model': model,
                    'official': False, 'model_image_verified': True, 'text': 'Fresh downloaded facts.'}, fresh_photo, b'fresh html'
    def research(product, domains, remaining, failed_urls):
        result = connected_research.cached_candidates(root, product, domains, remaining, failed_urls)
        if result is not None:
            return result
        live_search.append((domains, failed_urls))
        return [], []
    sources, paths, _ = collect(PRODUCT, ['mi.com'], research, Web(), destination)
    assert fetched == URLS and len(paths) == 5
    assert all(path.read_bytes() == fresh_photo for path in paths)
    assert all(source['text'] == 'Fresh downloaded facts.' and source['model'] == TITLE for source in sources)
    assert eligible(sources, manufacturer_unavailable=True)[1] == 'catalog_reference'
    assert live_search == [(['mi.com'], [])]
    assert store.path.read_bytes() == before
    assert store.get(old['id'])['report'] == old['report']
    # A verified catalog can be sufficient; rejected fresh evidence still cannot inherit approval.
    assert eligible(sources[:2], manufacturer_unavailable=True)[1] == 'catalog_reference'
    assert eligible([{**s, 'model_image_verified': False} for s in sources], manufacturer_unavailable=True)[1] == 'insufficient'
