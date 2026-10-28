import asyncio
import json
from pathlib import Path

import httpx
import pytest

from photo_auditor import integration
from photo_auditor.engine import Auditor, checked_comparison, decision, POLICY_VERSION
from photo_auditor.images import digest
from photo_auditor.store import AuditStore
from tests.test_photo_auditor import comparison, picture, product


def scoped_comparison():
    return {**comparison(), 'limitations': [
        {'kind': 'hidden_parts', 'note': 'Внутренний шнек закрыт корпусом и не проверяется.'},
        {'kind': 'unshown_accessories', 'note': 'Запасные насадки не выложены в кадре.'}],
        'source_checks': [{'id': 1, 'main_product_visible': True, 'suitable_for_repair': True,
                           'reason': 'Эталон показывает собранную мясорубку.'}]}


def sources():
    return [{'official': True, 'model_image_verified': True}]


def test_hidden_parts_and_unshown_accessories_do_not_block_matching_visible_product():
    value = checked_comparison(json.dumps(scoped_comparison()), sources())
    assert decision(value, sources())[0] == 'approved'


@pytest.mark.parametrize('status, verdict', [('mismatch', 'regenerate'), ('not_verifiable', 'review')])
def test_limitations_never_erase_a_visible_difference_or_uncertainty(status, verdict):
    value = scoped_comparison()
    value['checks'][0].update(status=status, fix='Вернуть основной корпус')
    assert decision(checked_comparison(json.dumps(value), sources()), sources())[0] == verdict


def test_accessory_only_reference_cannot_confirm_a_whole_product():
    value = scoped_comparison()
    value['source_checks'][0].update(main_product_visible=False, suitable_for_repair=False,
                                     reason='На эталоне только насадки, корпуса нет.')
    assert decision(checked_comparison(json.dumps(value), sources()), sources())[0] == 'review'


def test_incomplete_checks_cannot_approve():
    assert decision({'checks': []}, sources())[0] == 'review'


def test_fresh_reply_cannot_omit_visual_source_assessment():
    with pytest.raises(ValueError, match='области проверки'):
        checked_comparison(json.dumps(comparison()), sources(), require_scope=True)
    # Historic reports remain readable without pretending they satisfy the new protocol.
    assert checked_comparison(json.dumps(comparison()), sources())


@pytest.mark.parametrize('change', [
    {'main_product_visible': False}, {'main_product_visible': 'true'}, {'id': True},
    {'id': 2}, {'reason': ''}, {'suitable_for_repair': 1},
])
def test_invalid_reference_suitability_cannot_authorize_a_repair(change):
    value = scoped_comparison()
    value['source_checks'][0].update(change)
    with pytest.raises(ValueError):
        checked_comparison(json.dumps(value), sources())


def test_source_suitability_requires_all_unique_references():
    value = scoped_comparison()
    with pytest.raises(ValueError):
        checked_comparison(json.dumps(value), sources() * 2)
    value['source_checks'] *= 2
    with pytest.raises(ValueError):
        checked_comparison(json.dumps(value), sources() * 2)


def test_visible_conflict_cannot_be_classified_as_a_nonblocking_limitation():
    value = scoped_comparison()
    value['limitations'] = [{'kind': 'different_variant', 'note': 'Иной корпус'}]
    with pytest.raises(ValueError):
        checked_comparison(json.dumps(value), sources())


def test_two_visual_passes_keep_limits_and_conservatively_merge_repair_suitability(tmp_path):
    store = AuditStore(tmp_path)
    row = store.submit(product(), picture())
    instructions = []

    class Provider:
        def inspect(self, instruction, content, search=False, domains=()):
            if search:
                url = 'https://midea.com/exact-model'
                return {'text': json.dumps({'sources': [{'url': url}]}), 'sources': [url]}
            instructions.append(instruction)
            response = scoped_comparison()
            if len(instructions) == 2:
                response['source_checks'][0]['suitable_for_repair'] = False
            return {'text': json.dumps(response), 'sources': []}

    class Web:
        def reference(self, candidate, model, cited, domains):
            return {**candidate, 'official': True, 'model_image_verified': True,
                    'model': model, 'image_url': 'https://midea.com/main.png'}, picture(), b'page'

    report = Auditor(Path(__file__).resolve().parents[1], Provider(), Web()).run(product(), store.root / row['id'])
    assert report['verdict'] == 'approved'
    assert report['policy_version'] == POLICY_VERSION
    assert len(instructions) == 2 and report['limitations']
    assert report['source_checks'][0]['suitable_for_repair'] is False
    assert all('непоказанные принадлежности' in i.lower() or 'не требуй разборки' in i.lower() for i in instructions)


def test_inconclusive_review_never_replaces_photo_or_calls_generator(tmp_path, monkeypatch):
    monkeypatch.setattr(integration, 'load', lambda root: {'api_token': 'test'})
    monkeypatch.setattr(AuditStore, 'request_reference_repair', lambda *a: pytest.fail('Review must not become repair'))
    path = tmp_path / 'photo.png'
    path.write_bytes(picture())

    def respond(request):
        payload = json.loads(request.content)
        return httpx.Response(200, json={'id': 'a' * 32, 'status': 'review', 'product': payload['product'],
                                       'sha256': digest(picture())})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            async def forbidden(notes):
                pytest.fail('An inconclusive audit cannot start a new image')
            with pytest.raises(integration.AuditHold, match='ручной проверки'):
                await integration.checked_output(tmp_path, 'fotogen-123', 'Midea', 'ABC-12-RU',
                    path, None, forbidden, client=client, repair_reference=True)

    asyncio.run(run())
    assert path.read_bytes() == picture()
