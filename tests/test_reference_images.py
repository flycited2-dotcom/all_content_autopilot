import socket
import httpx

from photo_auditor.sources import Page, PublicWeb


def test_official_lazy_product_photo_wins_over_generic_social_logo():
    page = Page()
    page.feed('<title>Midea MF200W90WBS/W-RU</title>'
              '<meta property="og:image" content="/midea-logo-share.png">'
              '<meta property="og:image" content="https://www.midea.comnull">'
              '<img src="data:image/png;base64,AAAA" data-src="/exact-model.webp" '
              'alt="Стиральная машина MF200W90WBS/W-RU">'
              '<img data-src="/other-model.webp" alt="MF200W90WBS/S-RU">')
    assert page.product_images('MF200W90WBS-W-RU') == {'/exact-model.webp'}
    assert not any(url.startswith('data:') for url in page.images)


def test_model_in_recommendation_alt_does_not_override_wrong_page_heading():
    page = Page()
    page.feed('<h1>ABC-12-RU-2</h1><img src="/related.jpg" alt="ABC-12-RU">')
    assert page.product_images('ABC-12-RU') == set()


def test_model_bound_large_photo_is_preferred_over_short_social_thumbnail():
    from tests.test_photo_auditor import picture
    requests = []
    def handle(request):
        requests.append(str(request.url))
        if request.url.path == '/model':
            return httpx.Response(200, text='<title>ABC-12-RU</title>'
                '<meta property="og:image" content="/Small-1.jpg">'
                '<img alt="ABC-12-RU" src="/Small-1.jpg" '
                'srcset="/medium02.jpg 600w, /original-model-detail.jpg 1600w">')
        return httpx.Response(200, content=picture())
    resolver = lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443))]
    web = PublicWeb(httpx.Client(transport=httpx.MockTransport(handle)), resolver)
    source, _, _ = web.reference({'url': 'https://manufacturer.example/model'}, 'ABC-12-RU',
                                 ['https://manufacturer.example/model'], ['manufacturer.example'])
    assert source['image_url'] == 'https://manufacturer.example/original-model-detail.jpg'
    assert requests[-1] == source['image_url']
    assert source['model_image_verified']


def test_transient_protocol_error_is_retried_with_public_address_check():
    requests, resolutions = [], []
    def handle(request):
        requests.append(request)
        if len(requests) == 1:
            raise httpx.RemoteProtocolError('connection closed')
        return httpx.Response(200, content=b'official page')
    def resolver(*args, **kwargs):
        resolutions.append(args[0])
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443))]
    web = PublicWeb(httpx.Client(transport=httpx.MockTransport(handle)), resolver)
    assert web.fetch('https://manufacturer.example/model', 1024)[1] == b'official page'
    assert len(requests) == 2 and resolutions == ['manufacturer.example']
