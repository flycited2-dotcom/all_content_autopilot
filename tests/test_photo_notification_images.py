"""Owner decisions must include the actual saved image being judged."""
import io
from pathlib import Path

import pytest
from PIL import Image

from photo_auditor import runtime, service
from photo_auditor.store import AuditStore


def picture(color='white', format='PNG'):
    output = io.BytesIO()
    Image.new('RGB', (120, 80), color).save(output, format)
    return output.getvalue()


def finished(root, status='review', *, product_id='telegram-71', managed=False):
    store = AuditStore(root / 'runs/photo-auditor')
    product = {'product_id': product_id, 'revision': 0, 'brand': 'Xiaomi', 'model': 'L43MB-APRU'}
    row = store.submit(product, picture())
    report = {'verdict': status, 'reason': 'Источники не удалось проверить.' if status == 'review' else 'Товар сопоставлен.',
              'checks': [], 'sources': [], 'dimensions_note': 'Без масштаба не измерены.'}
    if managed:
        report['content_review'] = {'verdict': status, 'reason': 'Текст и компоновка не подтверждены.',
                                    'checks': [], 'claims': []}
    store.finish(store.claim(audit_id=row['id']), report)
    return store, store.get(row['id'])


class Telegram:
    def __init__(self, fail_photo=None):
        self.events = []
        self.fail_photo = fail_photo

    def send_photo(self, owner, path, caption=None):
        path = Path(path)
        self.events.append(('photo', owner, path, caption))
        if self.fail_photo == path.name:
            raise RuntimeError('Telegram temporarily unavailable')
        assert path.is_file()

    def send(self, owner, text, keyboard=None):
        self.events.append(('text', owner, text, keyboard))


@pytest.mark.parametrize('status', ['approved', 'regenerate', 'review'])
def test_every_owner_report_shows_full_saved_image_even_without_sources_or_comparison(tmp_path, status):
    store, row = finished(tmp_path, status)
    tg = Telegram()
    service.notify(store, tg, 123)
    primary = store.root / row['id'] / 'generated.bin'
    assert tg.events[0][0:3] == ('photo', 123, primary)
    assert primary.read_bytes() == picture()
    assert any(event[0] == 'text' for event in tg.events[1:])
    assert store.get(row['id'])['notified'] == 1
    assert not (store.root / row['id'] / 'comparison.jpg').exists()


def test_comparison_sheet_is_additional_to_primary_image_and_precedes_report(tmp_path):
    store, row = finished(tmp_path)
    folder = store.root / row['id']
    (folder / 'comparison.jpg').write_bytes(picture('navy', 'JPEG'))
    tg = Telegram()
    service.notify(store, tg, 123)
    assert [event[2] for event in tg.events if event[0] == 'photo'] == [folder / 'generated.bin', folder / 'comparison.jpg']
    assert [event[0] for event in tg.events[:3]] == ['photo', 'photo', 'text']


@pytest.mark.parametrize('damage', ['missing', 'invalid_bytes', 'changed_valid_image'])
def test_missing_or_corrupt_snapshot_explains_problem_without_calibration_keyboard(tmp_path, damage):
    store, row = finished(tmp_path)
    source = store.root / row['id'] / 'generated.bin'
    if damage == 'missing':
        source.unlink()
    elif damage == 'invalid_bytes':
        source.write_bytes(b'not an image')
    else:
        source.write_bytes(picture('black'))
    tg = Telegram()
    service.notify(store, tg, 123)
    assert not any(event[0] == 'photo' for event in tg.events)
    messages = [event for event in tg.events if event[0] == 'text']
    assert messages and all(event[3] is None for event in messages)
    text = '\n'.join(event[2] for event in messages).casefold()
    assert any(word in text for word in ('изображ', 'фото', 'снимок', 'файл'))
    assert any(word in text for word in ('поврежд', 'отсутств', 'не удалось', 'измен', 'недоступ'))
    assert 'оцените результат' not in text and 'калибровк' not in text


@pytest.mark.parametrize('failed_file', ['generated.bin', 'comparison.jpg'])
def test_transient_photo_delivery_failure_keeps_report_pending_and_unsent(tmp_path, failed_file):
    store, row = finished(tmp_path)
    (store.root / row['id'] / 'comparison.jpg').write_bytes(picture('navy', 'JPEG'))
    tg = Telegram(fail_photo=failed_file)
    with pytest.raises(RuntimeError, match='temporarily unavailable'):
        service.notify(store, tg, 123)
    assert store.get(row['id'])['notified'] == 0
    assert not any(event[0] == 'text' for event in tg.events)
    recovered = Telegram()
    service.notify(store, recovered, 123)
    assert recovered.events[0][0] == 'photo'
    assert any(event[0] == 'text' for event in recovered.events)
    assert store.get(row['id'])['notified'] == 1


@pytest.mark.parametrize('managed', [False, True])
def test_review_does_not_claim_successful_product_or_content_confirmation(tmp_path, managed):
    store, _ = finished(tmp_path, managed=managed)
    tg = Telegram()
    service.notify(store, tg, 123)
    text = '\n'.join(event[2] for event in tg.events if event[0] == 'text')
    assert 'Этот результат подтверждает' not in text
    assert 'Проверены товар, надписи, факты' not in text


@pytest.mark.parametrize('status,visible', [('approved', False), ('regenerate', False), ('review', True)])
def test_research_intermediate_notifications_remain_suppressed_except_owner_review(tmp_path, monkeypatch, status, visible):
    monkeypatch.setattr(runtime, 'load', lambda _: {'api_token': 'notification-test'})
    runtime.register_job(tmp_path, 71, 'research', 'Xiaomi', 'L43MB-APRU', 'Телевизоры')
    store, row = finished(tmp_path, status, product_id='fotogen-71')
    tg = Telegram()
    service.notify(store, tg, 123)
    assert bool(tg.events) is visible
    if visible:
        assert tg.events[0][0:3] == ('photo', 123, store.root / row['id'] / 'generated.bin')
    assert store.get(row['id'])['notified'] == 1
