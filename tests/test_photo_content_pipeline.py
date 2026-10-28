"""Exercise the real visual-to-content gate without live model/network calls."""
import copy
import io
import json

import pytest
from PIL import Image

from photo_auditor import runtime
from photo_auditor.content_review import ASPECTS
from photo_auditor.engine import Auditor, CHECKS, feedback
from photo_auditor.images import vision_image


MODEL = 'MI TV A PRO 2026 L65MB-APRU QLED 4K SmartTV Android'
PRODUCT = {'product_id': 'fotogen-71', 'brand': 'Xiaomi', 'model': MODEL, 'revision': 0}
FACT = 'Диагональ экрана 65 дюймов'


def picture():
    output = io.BytesIO()
    Image.new('RGB', (120, 180), 'navy').save(output, 'PNG')
    return output.getvalue()


def content_result(status='supported', *, citations=(1,)):
    fact = FACT if status != 'contradicted' else 'Диагональ экрана 75 дюймов'
    claims = [
        {'id': 1, 'text': 'Xiaomi L65MB-APRU', 'kind': 'identity', 'status': 'supported',
         'confidence': .99, 'reason': 'Точная модель совпадает.', 'fix': '', 'evidence': []},
        {'id': 2, 'text': fact, 'kind': 'technical', 'status': status, 'confidence': .99,
         'reason': 'Сопоставлено с фактом точной модели.',
         'fix': 'Исправить диагональ на 65 дюймов.' if status == 'contradicted' else '',
         'evidence': [{'source_id': i, 'quote': FACT} for i in citations]
                     if status in {'supported', 'contradicted'} else []},
    ]
    return {'visible_text': [c['text'] for c in claims], 'text_inventory_complete': True,
            'claims': claims,
            'checks': [{'aspect': aspect, 'status': 'match', 'confidence': .99,
                        'observation': 'Карточка читается и соответствует заданию.', 'fix': '',
                        'claim_ids': [1] if aspect == 'identity_text' else [2] if aspect == 'factual_claims' else []}
                       for aspect in ASPECTS]}


class Provider:
    def __init__(self, *content, fallback=False, visual_status='match'):
        self.content = list(content or (content_result(), content_result()))
        self.fallback = fallback
        self.visual_status = visual_status
        self.events = []
        self.content_inputs = []

    def inspect(self, instruction, content, search=False, domains=()):
        if search:
            self.events.append('search')
            urls = ([] if domains else [f'https://shop{i}.example/p' for i in range(1, 4)]) if self.fallback else ['https://mi.com/p']
            return {'text': json.dumps({'sources': [{'url': url} for url in urls]}), 'sources': urls}
        if any(item.get('text') == 'ГОТОВАЯ КАРТОЧКА ЦЕЛИКОМ' for item in content):
            self.events.append('content')
            self.content_inputs.append((instruction, copy.deepcopy(content)))
            return {'text': json.dumps(self.content[len(self.content_inputs) - 1], ensure_ascii=False)}
        self.events.append('visual')
        ids = [1, 2, 3] if self.fallback else [1]
        return {'text': json.dumps({
            'checks': [{'aspect': aspect, 'status': self.visual_status, 'confidence': .99,
                        'observation': 'Корпус и признаки сопоставлены с источником.',
                        'fix': 'Вернуть правильную форму корпуса.' if self.visual_status == 'mismatch' else '',
                        'source_ids': ids} for aspect in CHECKS],
            'dimensions_note': 'Без масштаба не измерены.', 'limitations': [],
            'source_checks': [{'id': i, 'main_product_visible': True, 'suitable_for_repair': True,
                               'reason': 'Основной товар точной модели виден.'} for i in ids],
        }, ensure_ascii=False)}


class Web:
    def reference(self, candidate, model, cited, domains):
        assert candidate['url'] in cited and model == MODEL
        return {**candidate, 'image_url': candidate['url'] + '.png', 'model': model,
                'model_image_verified': True, 'official': candidate['url'].startswith('https://mi.com/'),
                'text': f'Телевизор Xiaomi L65MB-APRU. {FACT}.'}, picture(), b'page'


@pytest.fixture
def pipeline(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, 'load', lambda _: {'api_token': 'content-pipeline-test'})
    (tmp_path / 'rules').mkdir()
    (tmp_path / 'rules/photo-auditor-manufacturers.yaml').write_text('xiaomi: [mi.com]\n', encoding='utf-8')
    runtime.register_job(tmp_path, 71, 'ready_tv', 'Xiaomi', MODEL,
                         'Сохранить заданный тёмный фон. ' + FACT)
    folder = tmp_path / 'audit'
    folder.mkdir()
    (folder / 'generated.bin').write_bytes(picture())
    return tmp_path, folder


@pytest.mark.parametrize('status,expected,count', [
    ('supported', 'approved', 2), ('contradicted', 'regenerate', 1), ('unsupported', 'review', 1),
])
def test_real_auditor_requires_both_visual_and_complete_card_decisions(pipeline, status, expected, count):
    root, folder = pipeline
    value = content_result(status)
    provider = Provider(value, value)
    report = Auditor(root, provider, Web()).run(PRODUCT, folder)
    assert report['verdict'] == expected
    assert provider.events == ['search', 'visual', 'visual'] + ['content'] * count
    assert report['content_review']['policy_version'] == runtime.CONTENT_POLICY
    assert report['content_review']['brief_sha256'] == runtime.brief_digest(runtime.content_brief(root, PRODUCT))
    assert len(report['content_review']['passes']) == count
    assert (folder / 'comparison.jpg').exists()
    for _, content in provider.content_inputs:
        payload = json.loads(content[0]['text'])
        assert payload['product'] == PRODUCT and payload['brief']['model'] == MODEL
        assert payload['sources'][0]['text'].endswith(FACT + '.')
        assert [item for item in content if item['type'] == 'input_image'][0] == vision_image(picture())
    if expected == 'regenerate':
        assert 'Исправить диагональ на 65 дюймов.' in feedback(report)


def test_visual_mismatch_prevents_content_approval_from_masking_wrong_product(pipeline):
    root, folder = pipeline
    provider = Provider(visual_status='mismatch')
    report = Auditor(root, provider, Web()).run(PRODUCT, folder)
    assert report['verdict'] == 'regenerate'
    assert provider.events == ['search', 'visual']
    assert 'content_review' not in report


def test_missing_signed_brief_stops_before_source_or_model_calls(pipeline):
    root, folder = pipeline
    (root / 'runs/photo-auditor/job-scope/71.brief.json').unlink()
    provider = Provider()
    report = Auditor(root, provider, Web()).run(PRODUCT, folder)
    assert report['verdict'] == 'review' and 'задания' in report['reason']
    assert provider.events == []
    assert not (folder / 'source-1.bin').exists()


def test_second_content_pass_can_stop_visually_correct_card(pipeline):
    root, folder = pipeline
    provider = Provider(content_result(), content_result('unsupported'))
    report = Auditor(root, provider, Web()).run(PRODUCT, folder)
    assert report['verdict'] == 'review'
    assert len(provider.content_inputs) == 2


@pytest.mark.parametrize('identity', ['Xiaomi L65MB-APRU', 'Модель: L65MB-APRU', 'Бренд: Xiaomi'])
def test_compact_exact_identity_and_common_labels_do_not_require_full_supplier_title(pipeline, identity):
    root, folder = pipeline
    value = content_result()
    value['visible_text'][0] = value['claims'][0]['text'] = identity
    report = Auditor(root, Provider(value, value), Web()).run(PRODUCT, folder)
    assert report['verdict'] == 'approved'


@pytest.mark.parametrize('identity', ['Xiaomi L43MB-APRU', 'Xiaomi L65MB-APRU2', 'Модель: L65MB-APRU-2'])
def test_compact_identity_keeps_exact_diagonal_and_suffix_boundary(pipeline, identity):
    root, folder = pipeline
    value = content_result()
    value['visible_text'][0] = value['claims'][0]['text'] = identity
    with pytest.raises(ValueError, match='номенклатуры'):
        Auditor(root, Provider(value), Web()).run(PRODUCT, folder)


def test_no_price_or_printed_model_is_required_for_plain_product_card(pipeline):
    root, folder = pipeline
    value = content_result()
    value.update(visible_text=['Закажите сейчас'], claims=[{
        'id': 1, 'text': 'Закажите сейчас', 'kind': 'other', 'status': 'not_claim', 'confidence': .99,
        'reason': 'Обычный призыв, без обещания цены или свойств.', 'fix': '', 'evidence': []}])
    for check in value['checks']:
        check['claim_ids'] = []
    provider = Provider(value, value)
    assert Auditor(root, provider, Web()).run(PRODUCT, folder)['verdict'] == 'approved'
    assert 'отнеси к other/not_claim' in provider.content_inputs[0][0]


@pytest.mark.parametrize('citations,expected', [((1,), 'approved'), ((1, 2, 3), 'approved')])
def test_exact_downloaded_fact_is_not_blocked_by_catalog_count(pipeline, citations, expected):
    root, folder = pipeline
    value = content_result(citations=citations)
    provider = Provider(value, value, fallback=True)
    report = Auditor(root, provider, Web()).run(PRODUCT, folder)
    assert report['verdict'] == expected
    assert report['evidence_basis'] == 'catalog_reference'
    assert len(report['sources']) == 3


def test_network_failure_after_visual_approval_resumes_content_only(pipeline):
    from listener.provider_errors import ProviderError
    root, folder = pipeline

    class Interrupted(Provider):
        def inspect(self, instruction, content, **kwargs):
            if any(c.get('text') == 'ГОТОВАЯ КАРТОЧКА ЦЕЛИКОМ' for c in content):
                raise ProviderError('chatgpt', 'network', 'test-network')
            return super().inspect(instruction, content, **kwargs)

    first = Interrupted()
    with pytest.raises(ProviderError):
        Auditor(root, first, Web()).run(PRODUCT, folder)
    assert first.events == ['search', 'visual', 'visual']
    assert (folder / 'visual-checkpoint.json').exists()
    second = Provider()
    report = Auditor(root, second, Web()).run(PRODUCT, folder)
    assert report['verdict'] == 'approved'
    assert second.events == ['content', 'content']


def test_invalid_content_preserves_visual_stage_and_bounded_diagnostic(pipeline):
    from photo_auditor.content_review import ContentReviewError
    root, folder = pipeline
    bad = content_result()
    bad['claims'][1]['evidence'][0]['quote'] = 'Выдуманная цитата'
    with pytest.raises(ContentReviewError):
        Auditor(root, Provider(bad), Web()).run(PRODUCT, folder)
    diagnostic = json.loads((folder / 'content-validation.json').read_text(encoding='utf-8'))
    assert 'Цитата отсутствует' in diagnostic['error']
    assert diagnostic['pass'] == 1
    provider = Provider()
    assert Auditor(root, provider, Web()).run(PRODUCT, folder)['verdict'] == 'approved'
    assert provider.events == ['content', 'content']
