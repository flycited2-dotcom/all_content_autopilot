from copy import deepcopy
import json

import pytest

from photo_auditor.content_review import ASPECTS, audit_content, checked_content, feedback
from tests.test_photo_auditor import picture


PRODUCT = {'product_id': 'fotogen-70', 'brand': 'JVC', 'model': 'JK-MG123', 'revision': 0}
BRIEF = {'mode': 'kbt', 'brand': 'JVC', 'model': 'JK-MG123', 'specs': 'Мощность 2000 Вт'}
SOURCES = [{'id': 1, 'url': 'https://jvc.example/JK-MG123', 'model': 'JK-MG123',
            'model_image_verified': True, 'official': True,
            'text': 'Мясорубка JVC JK-MG123. Максимальная мощность 2000 Вт. Реверс: есть.'}]


def result():
    return {'visible_text': ['JVC', 'JK-MG123', 'Мощность 2000 Вт'], 'text_inventory_complete': True,
            'claims': [
                {'id': 1, 'text': 'JVC', 'kind': 'identity', 'status': 'supported', 'confidence': .98,
                 'reason': 'Бренд совпадает с заданием.', 'fix': '', 'evidence': []},
                {'id': 2, 'text': 'JK-MG123', 'kind': 'identity', 'status': 'supported', 'confidence': .98,
                 'reason': 'Модель совпадает с заданием.', 'fix': '', 'evidence': []},
                {'id': 3, 'text': 'Мощность 2000 Вт', 'kind': 'technical', 'status': 'supported', 'confidence': .98,
                 'reason': 'Характеристика подтверждена источником.', 'fix': '',
                 'evidence': [{'source_id': 1, 'quote': 'Максимальная мощность 2000 Вт'}]}],
            'checks': [{'aspect': aspect, 'status': 'match', 'confidence': .98,
                        'observation': 'Задание выполнено, текст читается.', 'fix': '',
                        'claim_ids': [1, 2] if aspect == 'identity_text' else [3] if aspect == 'factual_claims' else []}
                       for aspect in ASPECTS]}


class Provider:
    def __init__(self, *results):
        self.results = list(results or (result(), result()))
        self.calls = []

    def inspect(self, instruction, content):
        self.calls.append((instruction, content))
        return {'text': json.dumps(self.results[len(self.calls) - 1], ensure_ascii=False)}


def run(provider=None, *, sources=SOURCES, brief=BRIEF):
    return audit_content(provider or Provider(), PRODUCT, picture(), sources, brief)


def test_complete_card_requires_two_independent_content_passes():
    provider = Provider()
    report = run(provider)
    assert report['verdict'] == 'approved' and len(report['passes']) == 2
    assert len(provider.calls) == 2
    assert 'Предыдущий ответ недоступен' in provider.calls[1][0]
    payload = json.loads(provider.calls[0][1][0]['text'])
    assert payload['brief'] == BRIEF and payload['sources'][0]['text'] == SOURCES[0]['text']
    assert provider.calls[0][1][-1]['type'] == 'input_image'


def test_unsupported_requested_fact_cannot_be_approved_as_true():
    value = result()
    value['claims'][2].update(status='unsupported', evidence=[], reason='Источник не подтверждает мощность.')
    provider = Provider(value)
    report = run(provider)
    assert report['verdict'] == 'review' and len(provider.calls) == 1


def test_confirmed_wrong_number_requests_concrete_correction():
    value = result()
    value['visible_text'][2] = 'Мощность 3000 Вт'
    value['claims'][2].update(text='Мощность 3000 Вт', status='contradicted',
                              reason='В источнике 2000 Вт, на карточке 3000 Вт.',
                              fix='Заменить 3000 Вт на подтверждённые 2000 Вт.')
    report = run(Provider(value))
    assert report['verdict'] == 'regenerate'
    assert 'Заменить 3000 Вт' in feedback(report)


@pytest.mark.parametrize('evidence', [[], [{'source_id': 1, 'quote': 'Мощность 3000 Вт'}],
                                       [{'source_id': 2, 'quote': 'Максимальная мощность 2000 Вт'}],
                                       [{'source_id': True, 'quote': 'Максимальная мощность 2000 Вт'}]])
def test_forged_or_missing_citation_is_rejected(evidence):
    value = result()
    value['claims'][2]['evidence'] = evidence
    with pytest.raises(ValueError):
        run(Provider(value))


def test_quotes_are_checked_after_unicode_and_whitespace_normalization():
    value = result()
    value['claims'][2]['evidence'][0]['quote'] = 'МАКСИМАЛЬНАЯ\n мощность\u00a02000 Вт'
    assert run(Provider(value, value))['verdict'] == 'approved'


@pytest.mark.parametrize('confidence', [True, False, float('nan'), float('inf'), -1, 1.1, '0.99'])
def test_confidence_cannot_be_boolean_nonfinite_or_outside_unit_interval(confidence):
    value = result()
    value['checks'][0]['confidence'] = confidence
    with pytest.raises(ValueError):
        run(Provider(value))


def test_second_pass_disagreement_and_missing_claim_both_block_approval():
    bad = result()
    bad['checks'][2].update(status='not_verifiable', observation='Мелкий текст не читается.')
    assert run(Provider(result(), bad))['verdict'] == 'review'
    missing = result()
    missing['claims'] = missing['claims'][:2]
    missing['visible_text'] = missing['visible_text'][:2]
    missing['checks'][1]['claim_ids'] = []
    report = run(Provider(result(), missing))
    assert report['verdict'] == 'review' and 'полный список' in report['reason']


def test_second_pass_cannot_erase_confirmed_defect_with_optimistic_checks():
    value = result()
    value['claims'][2].update(status='contradicted', fix='Исправить надпись.',
                              reason='Надпись прямо противоречит источнику.')
    report = run(Provider(result(), value))
    assert report['verdict'] == 'regenerate'


def test_unknown_style_and_channel_have_no_invented_default():
    provider = Provider()
    run(provider)
    instruction = provider.calls[0][0]
    assert 'не выдумывай требования Telegram/ВК' in instruction
    assert 'Не навязывай новый стиль' in instruction
    assert 'без обязательного требования печатать' in instruction


def test_no_printed_model_or_optional_claims_can_be_valid():
    value = result()
    value.update(visible_text=[], claims=[])
    for check in value['checks']:
        check['claim_ids'] = []
    assert run(Provider(value, value))['verdict'] == 'approved'


def test_incomplete_or_illegible_inventory_blocks_even_optimistic_checks():
    value = result()
    value['text_inventory_complete'] = False
    assert run(Provider(value))['verdict'] == 'review'


@pytest.mark.parametrize('mutate', [
    lambda v: v['checks'].pop(),
    lambda v: v['checks'][1].update(claim_ids=[]),
    lambda v: v['claims'][2].update(text='Несуществующая надпись'),
    lambda v: v['claims'][2].update(status='not_claim'),
    lambda v: v['claims'][2].update(kind=[]),
    lambda v: v['claims'][2].update(id=True),
    lambda v: v.update(text_inventory_complete=1),
])
def test_incomplete_or_unsafe_schema_cannot_pass(mutate):
    value = result()
    mutate(value)
    with pytest.raises(ValueError):
        run(Provider(value))


@pytest.mark.parametrize('brief', [{**BRIEF, 'model': 'JK-MG124'}, {**BRIEF, 'brand': 'Other'},
                                 {**BRIEF, 'mode': 'research'}, {**BRIEF, 'specs': None}])
def test_wrong_job_or_research_cannot_be_treated_as_card(brief):
    provider = Provider()
    with pytest.raises(ValueError):
        run(provider, brief=brief)
    assert not provider.calls


@pytest.mark.parametrize('source_change', [{'model': 'JK-MG124'}, {'model_image_verified': False}])
def test_unbound_or_wrong_model_source_cannot_support_claim(source_change):
    with pytest.raises(ValueError, match='Нет такого источника'):
        run(Provider(result()), sources=[{**SOURCES[0], **source_change}])


def test_explicit_unrequested_price_is_removed_without_inventing_another():
    value = result()
    value['visible_text'].append('Цена 12990 ₽')
    value['claims'].append({'id': 4, 'text': 'Цена 12990 ₽', 'kind': 'commercial', 'status': 'extraneous',
                            'confidence': .99, 'reason': 'Цена отсутствует в задании.',
                            'fix': 'Удалить добавленную цену 12990 ₽.', 'evidence': []})
    value['checks'][1]['claim_ids'].append(4)
    report = run(Provider(value))
    assert report['verdict'] == 'regenerate'
    assert 'Удалить добавленную цену' in feedback(report)
    with pytest.raises(ValueError, match='Не доказано'):
        run(Provider(value), brief={**BRIEF, 'specs': 'Цена 12990 ₽'})


def test_subjective_cta_cannot_be_removed_as_proven_extraneous_claim():
    value = result()
    value['visible_text'].append('Закажите сейчас')
    value['claims'].append({'id': 4, 'text': 'Закажите сейчас', 'kind': 'commercial', 'status': 'extraneous',
                            'confidence': .99, 'reason': 'Автор не просил такой призыв.',
                            'fix': 'Удалить призыв.', 'evidence': []})
    value['checks'][1]['claim_ids'].append(4)
    with pytest.raises(ValueError, match='Не доказано'):
        run(Provider(value))


def test_low_confidence_defect_is_review_not_an_automatic_regeneration():
    value = result()
    value['checks'][3].update(status='mismatch', confidence=.7, fix='Проверить границу кадра.')
    assert run(Provider(value))['verdict'] == 'review'


def test_factual_check_without_confirmed_contradiction_does_not_order_redraw():
    value = result()
    value['checks'][1].update(status='mismatch', fix='Нужно проверить характеристики.')
    assert run(Provider(value))['verdict'] == 'review'


def test_duplicate_source_numbers_cannot_smuggle_alternative_text():
    with pytest.raises(ValueError, match='номер источника'):
        run(Provider(), sources=[SOURCES[0], deepcopy(SOURCES[0])])


@pytest.mark.parametrize('contradicted', [False, True])
def test_one_bound_catalog_can_support_or_contradict_a_fact(contradicted):
    value = result()
    if contradicted:
        value['claims'][2].update(status='contradicted', fix='Исправить мощность.')
    assert run(Provider(value, value), sources=[{**SOURCES[0], 'official': False}])['verdict'] == ('regenerate' if contradicted else 'approved')


def test_three_independent_fact_citations_allow_catalog_fallback():
    sources = [{**SOURCES[0], 'id': n, 'official': False, 'url': f'https://catalog{n}.example/model'}
               for n in range(1, 4)]
    value = result()
    value['claims'][2]['evidence'] = [{'source_id': n, 'quote': 'Максимальная мощность 2000 Вт'}
                                    for n in range(1, 4)]
    assert run(Provider(value, value), sources=sources)['verdict'] == 'approved'
    sources[-1]['url'] = 'https://sub.catalog1.example/model'
    assert run(Provider(value, value), sources=sources)['verdict'] == 'approved'


def test_seller_price_is_taken_from_original_brief_rather_than_other_store():
    value = result()
    value['visible_text'].append('Цена 12990 ₽')
    value['claims'].append({'id': 4, 'text': 'Цена 12990 ₽', 'kind': 'commercial', 'status': 'supported',
                            'confidence': .99, 'reason': 'Цена продавца из исходного задания.', 'fix': '',
                            'evidence': [], 'brief_quote': 'Цена 12990 ₽'})
    value['checks'][1]['claim_ids'].append(4)
    assert run(Provider(value, value), brief={**BRIEF, 'specs': BRIEF['specs'] + '\nЦена 12990 ₽'})['verdict'] == 'approved'
    with pytest.raises(ValueError, match='отсутствует в задании'):
        run(Provider(value))


def test_different_text_grouping_in_second_pass_is_not_a_defect():
    second = result()
    second['visible_text'] = ['JVC JK-MG123', 'Мощность 2000 Вт']
    second['claims'] = [second['claims'][0], second['claims'][2]]
    second['claims'][0]['text'] = 'JVC JK-MG123'
    second['checks'][0]['claim_ids'] = [1]
    assert run(Provider(result(), second))['verdict'] == 'approved'


def test_visible_number_omitted_from_claims_cannot_pass():
    value = result()
    value['visible_text'].append('Скидка 50%')
    with pytest.raises(ValueError, match='Не все видимые'):
        run(Provider(value))


def test_real_quote_with_another_number_cannot_certify_false_supported_claim():
    value = result()
    value['visible_text'][2] = 'Мощность 3000 Вт'
    value['claims'][2]['text'] = 'Мощность 3000 Вт'
    assert run(Provider(value))['verdict'] == 'review'


def test_inventory_compares_resolution_multiplication_without_changing_numbers():
    from photo_auditor.content_review import _inventory_tokens
    assert _inventory_tokens('3840x2160 Mini LED') == _inventory_tokens('3840 × 2160 MiniLED')
    assert _inventory_tokens('3840х2160') == _inventory_tokens('3840 x 2160')
    assert _inventory_tokens('3840x2160') != _inventory_tokens('3840x2560')


def test_distinct_quotes_from_same_source_are_valid_without_inflating_quorum():
    value = result()
    value['claims'][2]['evidence'].append({'source_id': 1, 'quote': 'Реверс: есть'})
    assert run(Provider(value, value))['verdict'] == 'approved'
    assert run(Provider(value, value), sources=[{**SOURCES[0], 'official': False}])['verdict'] == 'approved'
    value['claims'][2]['evidence'].append({'source_id': 1, 'quote': 'Реверс: есть'})
    with pytest.raises(ValueError, match='Повтор одной цитаты'):
        run(Provider(value))


def test_source_file_metadata_is_measured_and_oem_microtext_is_not_editorial_copy():
    provider = Provider()
    run(provider)
    payload = json.loads(provider.calls[0][1][0]['text'])
    assert payload['IMAGE_METADATA'] == {'width': 120, 'height': 80, 'format': 'PNG'}
    assert 'Их не включай в visible_text/claims' in provider.calls[0][0]
    assert 'читаемым заводским делениям 0.5L/1.0L/1.5L' in provider.calls[0][0]
    assert 'Читаемый чужой бренд или модель на товаре игнорировать нельзя' in provider.calls[0][0]
    assert 'фраза «качество 2K» без явно заданных размеров' in provider.calls[0][0]
    assert 'проверь их строго по IMAGE_METADATA' in provider.calls[0][0]


def test_source_backed_product_category_is_not_a_wrong_brand_or_sku():
    value=result()
    value['visible_text'].append('Мясорубка')
    value['claims'].append({'id':4,'text':'Мясорубка','kind':'identity','status':'supported','confidence':.99,
        'reason':'Тип товара подтверждён названием источника.','fix':'',
        'evidence':[{'source_id':1,'quote':'Мясорубка JVC JK-MG123'}]})
    value['checks'][0]['claim_ids'].append(4)
    assert run(Provider(value,value))['verdict']=='approved'
    value['claims'][-1]['text']='Чайник'
    value['visible_text'][-1]='Чайник'
    with pytest.raises(ValueError,match='номенклатуры'):
        run(Provider(value))


def test_category_field_does_not_require_brand_and_sku_in_the_same_quote():
    value=result()
    value['visible_text'].append('Мясорубка')
    value['claims'].append({'id':4,'text':'Мясорубка','kind':'identity','status':'supported','confidence':.99,
        'reason':'Категория из характеристики точной модели.','fix':'',
        'evidence':[{'source_id':1,'quote':'Категория: Мясорубка'}]})
    value['checks'][0]['claim_ids'].append(4)
    sources=[{**SOURCES[0],'text':SOURCES[0]['text']+' Категория: Мясорубка.'}]
    assert run(Provider(value,value),sources=sources)['verdict']=='approved'
    value['visible_text'][-1]='Чайник';value['claims'][-1]['text']='Чайник'
    with pytest.raises(ValueError,match='номенклатуры'):
        run(Provider(value),sources=sources)


@pytest.mark.parametrize('printed,quote,verdict', [
    ('4K UHD 3840×2160', 'Разрешение 3840×2160', 'approved'),
    ('4K', 'Разрешение 1920×1080', 'review'),
    ('8K', 'Разрешение 7680×4320', 'approved'),
    ('Объём 5.0 л', 'Объём 5 л', 'approved'),
    ('Объём 5.5 л', 'Объём 5 л', 'review'),
    ('4K 4 HDMI', 'Разрешение 3840×2160, 3 HDMI', 'review'),
])
def test_numeric_guard_accepts_equivalent_values_but_rejects_wrong_specs(printed, quote, verdict):
    value=result();value['visible_text'][2]=printed
    value['claims'][2].update(text=printed,evidence=[{'source_id':1,'quote':quote}])
    sources=[{**SOURCES[0],'text':'Мясорубка JVC JK-MG123. '+quote}]
    assert run(Provider(value,value),sources=sources)['verdict']==verdict


def test_duplicate_expected_oem_brand_logo_is_not_an_omitted_editorial_claim():
    second=result();second['visible_text'].append('JVC')
    second['claims'].append({**second['claims'][0],'id':4})
    second['checks'][0]['claim_ids'].append(4)
    assert run(Provider(result(),second))['verdict']=='approved'


def test_missing_the_only_standalone_brand_label_still_disagrees():
    second=result();second['visible_text'].pop(0);second['claims'].pop(0)
    second['checks'][0]['claim_ids']=[2]
    assert run(Provider(result(),second))['verdict']=='review'
