import base64

import pytest

from photo_auditor import correction_reference, runtime
from photo_auditor.sources import source_model, contains_model
from photo_auditor.store import AuditStore
from photo_auditor.images import digest
from tests.test_photo_auditor import picture, report
from tests.test_photo_reviews import payload
from orchestrator_bot.store import Store
from orchestrator_bot.photo_reviews import register, get_card, list_cards


def test_beko_descriptive_title_keeps_exact_freezer_sku_and_suffix():
    title = 'Beko Морозильная камера Beko B1RFNK292W, белый, 255л, 7 ящиков'
    assert source_model(title) == 'b1rfnk292w'
    assert contains_model('Морозильная камера Beko B1RFNK292W', title)
    assert not contains_model('Beko B1RFNK292S', title)
    assert not contains_model('Beko B3RFNK292W', title)
    assert not contains_model('Beko B1RFNK292W2', title)
    assert source_model('Beko B1RFNK292W и B3RFNK292W') is None


def test_correction_reference_is_bound_to_exact_card_and_original_is_preserved(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, 'load', lambda _: {'api_token': 'test'})
    runtime.register_job(tmp_path, 7, 'kbt', 'Beko', 'B1RFNK292W', 'Исходное задание')
    audits = AuditStore(tmp_path / 'runs/photo-auditor')
    row = audits.submit(dict(product_id='fotogen-7', brand='Beko', model='B1RFNK292W', revision=0), picture('red'), picture('blue'))
    audits.finish(audits.claim(), report('review'))
    row = audits.get(row['id'])
    source = tmp_path / 'full.png';source.write_bytes(picture('white'))
    correction_reference.save(tmp_path, row, source, page_url='https://beko.ru/product/b1rfnk292w',
                              image_url='https://beko.ru/full.png', note='Полный вертикальный корпус', main_product_verified=True)
    assert correction_reference.read(tmp_path, row)['data'] == source.read_bytes()
    assert (audits.root / row['id'] / 'supplier.bin').read_bytes() == picture('blue')
    other = {**row, 'sha256': 'f' * 64}
    with pytest.raises(ValueError):
        correction_reference.read(tmp_path, other)
    (correction_reference.folder(tmp_path, row['id']) / 'reference.bin').write_bytes(picture('black'))
    with pytest.raises(ValueError, match='изменена'):
        correction_reference.read(tmp_path, row)


def test_refresh_keeps_verified_mirrored_assets_and_pagination_after_filter(tmp_path):
    store = Store(tmp_path / 'orch.db')
    card = payload()
    full = picture('blue')
    original = {**card, 'supplier': base64.b64encode(full).decode(), 'supplier_sha256': digest(full),
                'correction_reference': base64.b64encode(full).decode(), 'correction_reference_sha256': digest(full),
                'correction_reference_note': 'Полный вид', 'correction_reference_url': 'https://beko.ru/product/model'}
    register(store, original)
    register(store, card)
    kept = get_card(store, card['audit_id'])
    assert kept['supplier_sha256'] == digest(full) and kept['correction_reference_sha256'] == digest(full)
    for job in range(124, 140):
        register(store, payload(job), item_key='batch|exact')
    assert len(list_cards(store, ['batch|exact'], limit=13, offset=0)) == 13
    assert len(list_cards(store, ['batch|exact'], limit=13, offset=13)) == 3
