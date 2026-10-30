from types import SimpleNamespace

import pytest

from photo_auditor.research import source_gallery
from photo_auditor.sources import Page


HTML = '''<h1>Carver PPG-11000DVR</h1>
<a class="images" href="/upload/original.jpg"><img src="/upload/thumb.jpg"
alt="Carver PPG-11000DVR" width="440" height="302"></a>
<a href="/upload/other.jpg"><img src="/upload/other-small.jpg" alt="Carver PPG-11000DR"></a>
<img src="/unrelated.jpg" alt="other">'''
URL = 'https://uralopt.ru/products/diesel-generators/carver-PPG-11000DVR/'


def test_product_gallery_original_inherits_exact_child_identity():
    page = Page();page.feed(HTML)
    assert '/upload/original.jpg' in page.product_images('PPG-11000 DVR')
    assert '/upload/other.jpg' not in page.product_images('PPG-11000 DVR')
    assert ('other', '/upload/original.jpg') not in page.gallery_images


def test_linked_original_is_tried_before_small_browser_candidate():
    reader = lambda *args: ['https://uralopt.ru/upload/thumb.jpg']
    namespace = {'product_image_candidates': reader}
    with source_gallery(SimpleNamespace(__globals__=namespace)):
        candidates = namespace['product_image_candidates'](HTML, URL, 'PPG-11000 DVR')
        assert candidates == ['https://uralopt.ru/upload/original.jpg', 'https://uralopt.ru/upload/thumb.jpg']
    assert namespace['product_image_candidates'] is reader


def test_reader_restored_after_failure_and_does_not_expand_other_page_or_host():
    reader = lambda *args: []
    namespace = {'product_image_candidates': reader}
    with pytest.raises(RuntimeError):
        with source_gallery(SimpleNamespace(__globals__=namespace)):
            wrapped = namespace['product_image_candidates']
            assert wrapped(HTML, URL, 'PPG-11000 DR') == []
            foreign = HTML.replace('/upload/original.jpg', 'https://foreign.example/original.jpg')
            assert wrapped(foreign, URL, 'PPG-11000 DVR') == []
            raise RuntimeError('source unavailable')
    assert namespace['product_image_candidates'] is reader


def test_non_browser_provider_has_no_global_patch():
    with source_gallery(object()):
        pass
