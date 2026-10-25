import base64
import json
import re

import pytest

from photo_auditor.layout import card_html, facts_from_specs
from tests.test_photo_auditor import picture


def test_layout_preserves_the_original_image_bytes_and_escapes_catalog_text():
    original = picture('black')
    markup = card_html(original, '<Brand>', 'ABC-12-RU', '✓ 5 кг\n✓ 1000 об/мин\n<script>bad()</script>')
    encoded = re.search('href="data:image/png;base64,([^"]+)"', markup)[1]
    assert base64.b64decode(encoded) == original
    assert '<script>' not in markup and '&lt;Brand&gt;' in markup
    assert 'preserveAspectRatio="xMidYMid meet"' in markup
    assert 'Официальная гарантия' not in markup


def test_only_existing_facts_are_used():
    assert facts_from_specs(json.dumps({'features': ['8 кг', 'Пар']})) == ['8 кг', 'Пар']
    assert facts_from_specs('') == []
    with pytest.raises(ValueError):
        facts_from_specs(json.dumps({'features': [5]}))
