import json
from pathlib import Path

import pytest

from photo_auditor.engine import Auditor
from photo_auditor.store import AuditStore
from tests.test_photo_auditor import comparison, picture, product


@pytest.mark.parametrize('official,primary_passes', [
    (True, ({1}, {2})),
    (False, ({1}, {2})),
])
def test_approval_requires_shared_eligible_primary_sources_across_visual_passes(tmp_path, official, primary_passes):
    store = AuditStore(tmp_path)
    row = store.submit(product(), picture())
    urls = (['https://midea.com/exact-model', 'https://midearussia.ru/exact-model'] if official else
            [f'https://seller{source_id}.ru/exact-model' for source_id in range(1, 5)])

    class Provider:
        pass_count = 0

        def inspect(self, instruction, content, search=False, domains=()):
            if search:
                found = urls if official or not domains else []
                return {'text': json.dumps({'sources': [{'url': url} for url in found]}), 'sources': found}
            primary = primary_passes[self.pass_count]
            self.pass_count += 1
            value = comparison()
            value['limitations'] = []
            for check in value['checks']:
                check['source_ids'] = sorted(primary)
            value['source_checks'] = [
                {'id': source_id, 'main_product_visible': source_id in primary,
                 'suitable_for_repair': source_id in primary,
                 'reason': 'Основной товар виден.' if source_id in primary else 'Видны только детали.'}
                for source_id in range(1, len(urls) + 1)]
            return {'text': json.dumps(value), 'sources': []}

    class Web:
        def reference(self, candidate, model, cited, domains):
            return {**candidate, 'official': official, 'model_image_verified': True,
                    'model': model, 'image_url': candidate['url'] + '.png'}, picture(), b'page'

    provider = Provider()
    report = Auditor(Path(__file__).resolve().parents[1], provider, Web()).run(product(), store.root / row['id'])
    assert provider.pass_count == 2
    assert report['verdict'] == 'review'
    shared = primary_passes[0] & primary_passes[1]
    assert {item['id'] for item in report['source_checks'] if item['main_product_visible']} == shared
