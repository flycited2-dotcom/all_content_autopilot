import json

import pytest

from photo_auditor import presentation, runtime
from photo_auditor.integration import AuditHold
from tests.test_photo_auditor import picture, product
from tests.test_photo_content_review import Provider, SOURCES, result
from photo_auditor.content_review import audit_content


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, 'load', lambda _: {'api_token': 'test'})
    runtime.register_job(tmp_path, 123, 'kbt', 'Midea', 'ABC-12-RU', 'Сохранить заданные эталоны.')
    return tmp_path


def test_original_prompt_and_reference_pixels_are_frozen_and_part_of_approval(root):
    reference = root / 'template.png'
    reference.write_bytes(picture('blue'))
    before = runtime.brief_digest(runtime.content_brief(root, product()))
    presentation.capture(root, 123, 'Белый фон, крупный товар, без цен.', [reference])
    brief = runtime.content_brief(root, product())
    assert brief['presentation']['prompt'] == 'Белый фон, крупный товар, без цен.'
    assert runtime.brief_digest(brief) != before
    assert presentation.images(root, brief) == [picture('blue')]
    reference.write_bytes(picture('red'))
    assert presentation.images(root, runtime.content_brief(root, product())) == [picture('blue')]
    with pytest.raises(AuditHold, match='Изменились правила'):
        presentation.capture(root, 123, 'Белый фон, крупный товар, без цен.', [reference])


def test_changed_prompt_cannot_silently_replace_original_design(root):
    presentation.capture(root, 123, 'Оригинальный шаблон', [])
    with pytest.raises(AuditHold, match='Изменились правила'):
        presentation.capture(root, 123, 'Другой шаблон', [])
    assert runtime.content_brief(root, product())['presentation']['prompt'] == 'Оригинальный шаблон'


def test_tampered_style_image_or_signature_blocks_content_approval(root):
    reference = root / 'template.png'
    reference.write_bytes(picture())
    presentation.capture(root, 123, 'Оригинальный шаблон', [reference])
    brief = runtime.content_brief(root, product())
    fingerprint = brief['presentation']['references'][0]['sha256']
    frozen = root / 'runs/photo-auditor/brief-assets' / (fingerprint + '.bin')
    frozen.write_bytes(picture('black'))
    with pytest.raises(AuditHold, match='Не подтверждены'):
        runtime.content_brief(root, product())
    frozen.write_bytes(picture())
    path = presentation._path(root, 123)
    value = json.loads(path.read_text(encoding='utf-8'))
    value['payload']['prompt'] = 'Чужой шаблон'
    path.write_text(json.dumps(value), encoding='utf-8')
    with pytest.raises(AuditHold, match='Не подтверждены'):
        runtime.content_brief(root, product())


def test_missing_or_cross_job_presentation_cannot_reuse_prior_approval(root):
    presentation.capture(root, 123, 'Оригинальный шаблон', [])
    brief = runtime.content_brief(root, product())
    report = {'content_review': {'policy_version': runtime.CONTENT_POLICY, 'verdict': 'approved',
                                'brief_sha256': runtime.brief_digest(brief)}}
    assert runtime.approval_covers_brief(root, product(), report)
    presentation._path(root, 123).unlink()
    assert not runtime.approval_covers_brief(root, product(), report)
    runtime.register_job(root, 124, 'kbt', 'Midea', 'ABC-12-RU', 'Сохранить заданные эталоны.')
    presentation.capture(root, 124, 'Оригинальный шаблон', [])
    presentation._path(root, 123).write_bytes(presentation._path(root, 124).read_bytes())
    assert not runtime.approval_covers_brief(root, product(), report)


def test_style_frames_are_separate_from_product_sources_in_both_passes():
    product_value = {'product_id': 'fotogen-70', 'brand': 'JVC', 'model': 'JK-MG123', 'revision': 0}
    brief = {'mode': 'kbt', 'brand': 'JVC', 'model': 'JK-MG123', 'specs': 'Мощность 2000 Вт',
             'presentation': {'prompt': 'Оригинальный шаблон', 'references': [{'name': 'style.png', 'sha256': 'a' * 64}]}}
    provider = Provider()
    report = audit_content(provider, product_value, picture(), SOURCES, brief, presentation_images=[picture('blue')])
    assert report['verdict'] == 'approved'
    for _, content in provider.calls:
        assert content[-2] == {'type': 'input_text', 'text': 'STYLE_REFERENCE 1'}
        assert content[-1]['type'] == 'input_image'
    with pytest.raises(ValueError, match='все эталоны'):
        audit_content(Provider(), product_value, picture(), SOURCES, brief)
