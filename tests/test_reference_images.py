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
