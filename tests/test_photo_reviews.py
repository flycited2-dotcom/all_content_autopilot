import base64
from types import SimpleNamespace

import pytest

from orchestrator_bot import photo_reviews
from orchestrator_bot.store import Store
from photo_auditor import review_sync, runtime
from photo_auditor.images import digest
from photo_auditor.store import AuditStore
from tests.test_photo_auditor import picture, report


def payload(job_id=123, revision=0, color='white'):
    data = picture(color)
    return dict(audit_id=(f'{job_id:031x}' + str(revision)), job_id=job_id, revision=revision,
                sha256=digest(data), brand='CARVER', model='PPG-11000 DVR', status='review',
                summary='Неверная подпись топлива', preview=base64.b64encode(data).decode())


def test_registered_snapshot_used_for_exact_correction_and_stale_card_rejected(tmp_path):
    store = Store(tmp_path / 'orch.db')
    original = payload()
    photo_reviews.register(store, original)
    card = photo_reviews.list_cards(store)[0]
    sent = []
    factory = SimpleNamespace(correct_photo=lambda value: sent.append(value) or {'job_id': 999})
    request = photo_reviews.create_feedback(card, 'Дизельный генератор, убрать бензин', 42, 'a' * 32)
    assert photo_reviews.correct(store, factory, request)['job_id'] == 999
    assert sent[0]['notes'] == request['notes']
    modified = {**request, 'sha256': 'f' * 64}
    with pytest.raises(ValueError, match='другому снимку'):
        photo_reviews.correct(store, factory, modified)
    photo_reviews.register(store, payload(revision=1, color='blue'))
    assert photo_reviews.correct(store, factory, request)['reason'] == 'superseded'
    assert len(sent) == 1


def test_changed_image_cannot_be_registered_and_revision_cannot_move_backwards(tmp_path):
    store = Store(tmp_path / 'orch.db')
    value = payload()
    value['sha256'] = 'f' * 64
    with pytest.raises(ValueError, match='снимку'):
        photo_reviews.register(store, value)
    photo_reviews.register(store, payload(revision=1, color='blue'))
    assert photo_reviews.register(store, payload())['reason'] == 'superseded'
    assert photo_reviews.list_cards(store)[0]['revision'] == 1
    with pytest.raises(ValueError, match='ревизии изменилось'):
        photo_reviews.register(store, payload(revision=1, color='black'))


def test_sync_mirrors_latest_card_once_without_making_generation(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, 'load', lambda _: {'api_token': 'test'})
    runtime.register_job(tmp_path, 123, 'kbt', 'CARVER', 'PPG-11000 DVR', 'Исходное задание')
    audits = AuditStore(tmp_path / 'runs/photo-auditor')
    product = dict(product_id='fotogen-123', revision=0, brand='CARVER', model='PPG-11000 DVR')
    row = audits.submit(product, picture())
    audits.finish(audits.claim(), report('review'))
    remote = Store(tmp_path / 'orch.db')
    calls = []
    def deliver(value):
        calls.append(value)
        return photo_reviews.register(remote, value)
    assert review_sync.sync(audits, deliver) == 1
    assert review_sync.sync(audits, deliver) == 0
    assert len(calls) == 1
    assert photo_reviews.get_card(remote, row['id'])['sha256'] == row['sha256']


def test_batch_selection_does_not_show_other_models(tmp_path):
    store = Store(tmp_path / 'orch.db')
    photo_reviews.register(store, payload())
    assert len(photo_reviews.list_cards(store, ['excel|carver|ppg-11000 dvr'])) == 1
    assert not photo_reviews.list_cards(store, ['excel|carver|ppg-11000 dr'])


def test_batch_filter_applies_before_limit_and_uses_authoritative_supplier_key(tmp_path):
    store = Store(tmp_path / 'orch.db')
    photo_reviews.register(store, payload(), item_key='supplier|original|full-product-name')
    for job_id in range(124, 140):
        photo_reviews.register(store, payload(job_id))
    cards = photo_reviews.list_cards(store, ['supplier|original|full-product-name'], limit=2)
    assert [card['job_id'] for card in cards] == [123]
