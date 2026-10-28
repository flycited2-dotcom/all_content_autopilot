from copy import deepcopy

import pytest

from photo_auditor import runtime
from photo_auditor.images import digest
from photo_auditor.reference_asset import from_audit
from photo_auditor.store import AuditStore
from tests.test_photo_auditor import picture, product, report


def visual(source_id=1, *, main=True, suitable=True):
    return {'id': source_id, 'main_product_visible': main, 'suitable_for_repair': suitable,
            'reason': 'Основной товар виден целиком.' if main else 'Показаны только съёмные детали.'}


def audit_with_sources(tmp_path, checks, *, count=1):
    data = picture('blue')
    sources = [{'id': source_id, 'official': True, 'model_image_verified': True,
                'model': product()['model'], 'image_sha256': digest(data)}
               for source_id in range(1, count + 1)]
    audit = {'id': 'a' * 32, 'product': product(),
             'report': {**report('review'), 'sources': sources}}
    if checks is not None:
        audit['report']['source_checks'] = checks
    folder = tmp_path / 'runs/photo-auditor' / audit['id']
    folder.mkdir(parents=True)
    for source in sources:
        (folder / f"source-{source['id']}.bin").write_bytes(data)
    return audit, folder


def test_accessories_only_reference_never_replaces_a_whole_appliance(tmp_path, monkeypatch):
    store = AuditStore(tmp_path / 'runs/photo-auditor')
    row = store.submit(product(), picture())
    source = {'id': 1, 'official': True, 'model_image_verified': True,
              'model': product()['model'], 'image_sha256': digest(picture('blue'))}
    original_report = {**report('review'), 'reason': 'Эталон содержит только детали без моторного блока.',
                       'sources': [source], 'source_checks': [visual(main=False, suitable=False)]}
    store.finish(store.claim(), original_report)
    folder = store.root / row['id']
    (folder / 'source-1.bin').write_bytes(picture('blue'))
    monkeypatch.setattr(runtime, '_read', lambda *args: {'mode': 'kbt', 'required': True,
        'brand': product()['brand'], 'model': product()['model']})
    before = store.get(row['id'])
    assert from_audit(tmp_path, before) is None
    assert not store.request_reference_repair(row['id'])
    after = store.get(row['id'])
    assert after['status'] == 'review' and after['revision'] == 0
    assert after['report'] == before['report']
    assert not (folder / 'before-reference-repair.json').exists()
    assert (folder / 'generated.bin').read_bytes() == picture()
    with store.connect() as db:
        assert db.execute('SELECT count(*) FROM audits').fetchone()[0] == 1


@pytest.mark.parametrize('checks', [None, [], [visual(main=True, suitable=False)],
    [visual(main=False, suitable=True)], [visual(main=False, suitable=False)],
    [{**visual(), 'main_product_visible': 1}], [{**visual(), 'suitable_for_repair': 'true'}],
    [{**visual(), 'reason': ''}], [{**visual(), 'id': True}], [{**visual(), 'id': 2}],
])
def test_repair_requires_explicit_valid_visual_suitability(tmp_path, checks):
    audit, _ = audit_with_sources(tmp_path, checks)
    assert from_audit(tmp_path, audit) is None


def test_visual_validation_must_cover_every_source_once(tmp_path):
    audit, _ = audit_with_sources(tmp_path, [visual()], count=2)
    assert from_audit(tmp_path, audit) is None
    audit['report']['source_checks'] = [visual(), visual()]
    assert from_audit(tmp_path, audit) is None


def test_repair_uses_first_explicitly_suitable_source_in_saved_gallery_order(tmp_path):
    audit, folder = audit_with_sources(tmp_path, [visual(1, main=False, suitable=False), visual(2), visual(3)], count=3)
    assert from_audit(tmp_path, audit) == folder / 'source-2.bin'
    audit['report']['source_checks'][2]['suitable_for_repair'] = False
    (folder / 'source-2.bin').write_bytes(picture('red'))
    # A missing/corrupt selected asset must not fall back to an unvalidated photo.
    assert from_audit(tmp_path, audit) is None


@pytest.mark.parametrize('changed', [
    {'model': 'different-model'}, {'model_image_verified': False}, {'official': False},
    {'image_sha256': '0' * 64},
])
def test_visual_suitability_does_not_bypass_source_provenance_or_digest(tmp_path, changed):
    audit, _ = audit_with_sources(tmp_path, [visual()])
    audit['report']['sources'][0].update(changed)
    assert from_audit(tmp_path, audit) is None


def test_complete_visual_validation_does_not_mutate_the_audit(tmp_path):
    audit, folder = audit_with_sources(tmp_path, [visual()])
    before = deepcopy(audit)
    assert from_audit(tmp_path, audit) == folder / 'source-1.bin'
    assert audit == before


@pytest.mark.parametrize('third', [visual(3, main=False, suitable=False), visual(3, suitable=False)])
def test_catalog_repair_uses_suitable_asset_even_if_other_sources_are_accessories(tmp_path, third):
    audit, folder = audit_with_sources(tmp_path, [visual(1), visual(2), third], count=3)
    audit['report'].update(verdict='regenerate', evidence_basis='secondary_consensus',
                           manufacturer_unavailable=True)
    for source in audit['report']['sources']:
        source.update(official=False, url=f"https://seller{source['id']}.ru/product/model")
    assert from_audit(tmp_path, audit) == folder / 'source-1.bin'


def test_catalog_repair_never_selects_wrong_model_asset(tmp_path):
    audit, folder = audit_with_sources(tmp_path, [visual(1), visual(2), visual(3)], count=3)
    audit['report'].update(verdict='regenerate', evidence_basis='secondary_consensus',
                           manufacturer_unavailable=True)
    for source in audit['report']['sources']:
        source.update(official=False, url=f"https://seller{source['id']}.ru/product/model")
    assert from_audit(tmp_path, audit) == folder / 'source-1.bin'
    audit['report']['sources'][2]['model'] = 'different-model'
    assert from_audit(tmp_path, audit) == folder / 'source-1.bin'
    audit['report']['sources'][2]['model'] = product()['model']
    audit['report']['sources'][2]['url'] = 'https://shop.seller1.ru/product/model'
    assert from_audit(tmp_path, audit) == folder / 'source-1.bin'
    for source in audit['report']['sources']:
        source['model'] = 'different-model'
    assert from_audit(tmp_path, audit) is None
