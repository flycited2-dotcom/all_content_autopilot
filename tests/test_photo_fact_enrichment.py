"""Sparse official pages must lead to fact research, before another paid render."""
import json
import pytest

from photo_auditor import runtime
from photo_auditor.engine import Auditor
from tests.test_photo_content_pipeline import PRODUCT, MODEL, FACT, Provider, Web, picture, content_result


@pytest.mark.parametrize('extra_url', ['https://catalog.example/p', 'https://mi.com/specifications'])
def test_sparse_source_gets_verified_facts_and_two_fresh_editorial_passes(tmp_path, monkeypatch, extra_url):
    monkeypatch.setattr(runtime, 'load', lambda _: {'api_token': 'fact-enrichment-test'})
    (tmp_path / 'rules').mkdir()
    (tmp_path / 'rules/photo-auditor-manufacturers.yaml').write_text('xiaomi: [mi.com]\n', encoding='utf-8')
    runtime.register_job(tmp_path, 71, 'ready_tv', 'Xiaomi', MODEL, 'Тёмная карточка. ' + FACT)
    folder = tmp_path / 'audit'; folder.mkdir()
    (folder / 'generated.bin').write_bytes(picture())
    facts = [FACT, 'Мощность звука 20 Вт', 'Частота обновления 60 Гц']

    class SparseWeb(Web):
        def reference(self, candidate, model, cited, domains):
            source, photo, html = super().reference(candidate, model, cited, domains)
            source['text'] = 'Телевизор Xiaomi L65MB-APRU. ' + (
                '. '.join(facts) if candidate['url'] == extra_url else '')
            return source, photo, html

    class ResearchProvider(Provider):
        def __init__(self):
            super().__init__(); self.editorial_source_counts = []; self.fact_queries = []

        def inspect(self, instruction, content, search=False, domains=()):
            if search:
                query = json.loads(content[0]['text'])
                self.fact_queries.append(query)
                supplemental = bool(query.get('facts_to_verify'))
                urls = ([extra_url] if not domains or extra_url.startswith('https://mi.com/') else []) if supplemental else ['https://mi.com/p']
                return {'text': json.dumps({'sources': [{'url': u} for u in urls]}), 'sources': urls}
            if any(i.get('text') == 'ГОТОВАЯ КАРТОЧКА ЦЕЛИКОМ' for i in content):
                count = len(json.loads(content[0]['text'])['sources'])
                self.editorial_source_counts.append(count)
                value = content_result()
                value['claims'] = value['claims'][:1]
                for n, text in enumerate(facts, 2):
                    value['claims'].append({'id': n, 'text': text, 'kind': 'technical',
                        'status': 'supported' if count == 2 else 'unsupported', 'confidence': .99,
                        'reason': 'Сверено с загруженной страницей.', 'fix': '',
                        'evidence': [{'source_id': 2, 'quote': text}] if count == 2 else []})
                value['visible_text'] = [c['text'] for c in value['claims']]
                for check in value['checks']:
                    if check['aspect'] == 'factual_claims': check['claim_ids'] = [2, 3, 4]
                return {'text': json.dumps(value, ensure_ascii=False)}
            return super().inspect(instruction, content)

    provider = ResearchProvider()
    report = Auditor(tmp_path, provider, SparseWeb()).run(PRODUCT, folder)
    assert report['verdict'] == 'approved'
    assert provider.editorial_source_counts == [1, 2, 2]
    assert any(q['facts_to_verify'] == facts for q in provider.fact_queries)
    assert report['content_review']['additional_fact_sources'] is True
    assert len(report['content_review']['passes']) == 2
    assert report['sources'][1]['purpose'] == 'factual_verification'
    assert (folder / 'source-2.bin').read_bytes() == picture()
    assert (folder / 'source-2.html').read_bytes() == b'page'
    assert len(report['comparison_passes']) == 2
