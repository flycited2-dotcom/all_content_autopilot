import json
import sqlite3

import pytest

from orchestrator_bot.catalog_selection import model_name
from photo_auditor.connected_research import checked_facts
from photo_auditor.discovery import collect, owner
from photo_auditor.engine import decision
from photo_auditor import progress
from photo_auditor.store import AuditStore
from tests.test_photo_auditor import product, picture, report, comparison


def test_catalog_strips_specifications_but_preserves_real_sku_suffixes():
    assert model_name('WMG-3672GS (2000Вт,DС,2.7кг/мин.,мет.корпус)') == 'WMG-3672GS'
    assert model_name('WMG-3672GS(2000Вт,реверс)') == 'WMG-3672GS'
    assert model_name('AB-123 (WH)') == 'AB-123 (WH)'
    assert model_name('MF205W80WB/T-RU') == 'MF205W80WB/T-RU'


def secondary_sources():
    return [{'url': f'https://seller{i}.ru/product/model', 'official': False,
             'model_image_verified': True} for i in range(3)]


def test_catalog_reference_is_usable_when_official_reference_is_unavailable():
    sources = secondary_sources()
    value = comparison()
    for check in value['checks']:
        check['source_ids'] = [1, 2, 3]
    assert decision(value, sources)[0] == 'review'
    assert decision(value, sources, manufacturer_unavailable=True)[0] == 'approved'
    sources[2]['url'] = 'https://shop.seller1.ru/another'
    assert decision(value, sources, manufacturer_unavailable=True)[0] == 'approved'
    assert owner('https://www.example.co.uk/a') == owner('https://shop.example.co.uk/b')


def test_catalog_count_does_not_override_low_confidence_or_missing_citations():
    value = comparison()
    assert decision(value, secondary_sources(), manufacturer_unavailable=True)[0] == 'approved'
    value['checks'][0]['confidence'] = .84
    for check in value['checks']:
        check['source_ids'] = [1, 2, 3]
    assert decision(value, secondary_sources(), manufacturer_unavailable=True)[0] == 'review'


def test_extract_facts_rejects_invented_numbers_and_nonexistent_quotes():
    sources = [{'text': 'Мощность 1500 Вт. Реверс есть. Материал корпуса пластик.'}]
    facts = {'features': [{'text': t, 'source_id': 1} for t in
                        ['Мощность 1500 Вт', 'Реверс есть', 'Материал корпуса пластик']]}
    assert len(checked_facts(json.dumps(facts), sources)) == 3
    facts['features'][0]['text'] = 'Мощность 3000 Вт'
    with pytest.raises(ValueError, match='нет в тексте'):
        checked_facts(json.dumps(facts), sources)


def test_microdata_binds_main_product_image_without_accepting_related_products():
    from photo_auditor.sources import Page
    page = Page()
    page.feed('''<h1>Мясорубка Blackton Bt MG1113</h1>
      <section itemscope itemtype="https://schema.org/Product">
      <meta itemprop="name" content=""><meta itemprop="image" content="/main.jpg">
      <div itemscope itemtype="https://schema.org/Product">
      <meta itemprop="name" content="Blackton MG1112">
      <meta itemprop="image" content="/related.jpg"></div></section>''')
    assert page.product_images('MG1113') == {'/main.jpg'}
    assert page.product_images('MG1112') == {'/related.jpg'}
    assert page.product_images('MG111') == set()


def test_product_gallery_order_keeps_assembled_product_before_accessories():
    import socket
    import httpx
    from photo_auditor.sources import PublicWeb
    def handle(request):
        if request.url.path == '/p':
            return httpx.Response(200, text='''<h1>JVC JK-MG123</h1>
            <img alt="JVC JK-MG123" src="/zzz-assembled-product.jpg">
            <img alt="JVC JK-MG123" src="/a.jpg">''')
        return httpx.Response(200, content=picture())
    resolver = lambda *a, **k: [(socket.AF_INET, 0, 0, '', ('93.184.216.34', 443))]
    web = PublicWeb(httpx.Client(transport=httpx.MockTransport(handle)), resolver)
    source, _, _ = web.reference({'url': 'https://jvc-rus.ru/p'}, 'JK-MG123',
                                 ['https://jvc-rus.ru/p'], ['jvc-rus.ru'])
    assert source['image_url'].endswith('/zzz-assembled-product.jpg')


def test_explicit_recheck_revokes_approval_until_new_check_finishes(tmp_path):
    store = AuditStore(tmp_path)
    row = store.submit(product(), picture())
    store.finish(store.claim(), report('approved'))
    assert store.recheck(row['id'])
    assert store.get(row['id'])['status'] == 'queued'
    assert store.get(row['id'])['report'] is None
    assert store.latest('fotogen-123')['revision'] == 0


def test_second_auditor_exits_before_opening_telegram_or_api(tmp_path, monkeypatch):
    from listener import lifecycle
    from photo_auditor import __main__ as command
    (tmp_path / 'rules').mkdir()
    (tmp_path / 'rules/photo-auditor-manufacturers.yaml').write_text('{}')
    monkeypatch.setattr(command, 'ROOT', tmp_path)
    monkeypatch.setattr(lifecycle, 'acquire_instance_lock', lambda port: None)
    monkeypatch.setattr(command.time, 'sleep', lambda seconds: None)
    monkeypatch.setattr(command, 'load', lambda root: pytest.fail('Second bot must not read credentials or connect'))
    monkeypatch.setattr(command, 'make_server', lambda *args: pytest.fail('Second bot must not bind API'))
    assert command.main(['run']) == 0


def test_scheduler_restart_waits_for_previous_auditor_to_finish(tmp_path, monkeypatch):
    from listener import lifecycle
    from photo_auditor import __main__ as command
    (tmp_path / 'rules').mkdir()
    (tmp_path / 'rules/photo-auditor-manufacturers.yaml').write_text('{}')
    monkeypatch.setattr(command, 'ROOT', tmp_path)
    locked = iter([None, None, object()])
    waits = []
    monkeypatch.setattr(lifecycle, 'acquire_instance_lock', lambda port: next(locked))
    monkeypatch.setattr(command.time, 'sleep', waits.append)
    class ReachedStartup(Exception):
        pass
    def reached(root):
        raise ReachedStartup()
    monkeypatch.setattr(command, 'load', reached)
    with pytest.raises(ReachedStartup):
        command.main(['run'])
    assert waits == [.5, .5]


def test_auditor_stops_when_scheduler_parent_disappears(monkeypatch):
    from listener import lifecycle
    from photo_auditor.service import watch_parent
    stopped = []
    class Stop:
        def wait(self, seconds):
            return bool(stopped)
        def set(self):
            stopped.append(True)
    monkeypatch.setattr(lifecycle, 'parent_alive', lambda pid: False)
    watch_parent(123, Stop())
    assert stopped == [True]


def test_research_recovers_photo_and_facts_together_and_detects_corruption(tmp_path):
    store = AuditStore(tmp_path / 'runs/photo-auditor')
    store.submit(product(), picture())
    specs = json.dumps({'exact_model': True, 'features': ['one', 'two', 'three']})
    progress.save_research(tmp_path, 'fotogen-123', 'Midea', 'ABC-12-RU', specs)
    restored = progress.restore_research(tmp_path, 'fotogen-123', 'Midea', 'ABC-12-RU')
    assert restored['path'].read_bytes() == picture() and restored['specs'] == specs
    manifest = progress.folder(tmp_path, 'fotogen-123') / 'research.json'
    value = json.loads(manifest.read_text())
    value['specs'] = 'invented'
    manifest.write_text(json.dumps(value))
    from photo_auditor.integration import AuditHold
    with pytest.raises(AuditHold, match='характеристики изменились'):
        progress.restore_research(tmp_path, 'fotogen-123', 'Midea', 'ABC-12-RU')


def test_same_research_job_can_resume_without_new_queue_record(tmp_path):
    from orchestrator_bot.photo_recovery import reconcile
    from tests.test_photo_recovery import databases, event
    state, queue = databases(tmp_path)
    with sqlite3.connect(state) as db:
        db.execute('ALTER TABLE excel_items ADD COLUMN research_job INTEGER')
        db.execute('UPDATE excel_items SET research_job=123,card_job=NULL')
    with sqlite3.connect(queue) as db:
        db.execute("UPDATE jobs SET mode='research'")
    assert reconcile(state, queue, event())['resumed']
    with sqlite3.connect(state) as db:
        assert db.execute('SELECT status,research_job,card_job FROM excel_items').fetchone() == ('research', 123, None)
    with sqlite3.connect(queue) as db:
        assert db.execute('SELECT count(*) FROM jobs').fetchone()[0] == 1
