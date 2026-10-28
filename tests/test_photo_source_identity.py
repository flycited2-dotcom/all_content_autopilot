"""Source binding uses explicit nomenclature rather than supplier marketing text."""
import io
import socket

import httpx
import pytest
from PIL import Image

from photo_auditor.sources import Page, PublicWeb, contains_model, source_model


@pytest.mark.parametrize("title,sku", [
    ("MI TV A PRO 2026 L65MB-APRU QLED 4K SmartTV Android", "L65MB-APRU"),
    ("MI TV A PRO 2026 L43MB-APRU QLED 4K SmartTV Android", "L43MB-APRU"),
    ("TV S Mini LED 55 2026 L55MC-SRU 4K SmartTV Android", "L55MC-SRU"),
    ("Телевизор Xiaomi TV A Pro 65 2026 L65MB-APRU", "L65MB-APRU"),
])
def test_supplier_description_matches_exact_sku_independent_of_marketing_order(title, sku):
    assert source_model(title) == sku.casefold()
    assert contains_model(f"Xiaomi {sku}", title)
    assert contains_model(f"Телевизор Xiaomi {sku} QLED 2026", title)


@pytest.mark.parametrize("source", [
    "Xiaomi L43MB-APRU", "Xiaomi L65MB-SRU", "Xiaomi L65MB-APRU2",
    "Xiaomi L65MB-APRU-2", "Xiaomi L65MB-APRU/2", "Xiaomi XL65MB-APRU",
    "Xiaomi L65MB-AP", "Xiaomi L65MB",
])
def test_descriptive_identity_retains_diagonal_and_complete_suffix(source):
    assert not contains_model(source, "MI TV A PRO 2026 L65MB-APRU QLED 4K SmartTV Android")


@pytest.mark.parametrize("title", [
    "TV L65MB-APRU и L43MB-APRU", "TV L65MB-APRU / L55MC-SRU",
    "TV L65MB-APRU2", "TV L65MB-APRU-2", "TV L65MB-APRU/2",
    "TV L65MB-APRU и L43MB-APRU2", "TV L65MB-APRU/L43MB-APRU",
])
def test_ambiguous_or_extended_requested_tv_sku_cannot_collapse_to_shorter_model(title):
    assert source_model(title) is None
    assert not contains_model(title, title)
    assert not contains_model("Xiaomi L65MB-APRU", title)


@pytest.mark.parametrize("model", [
    "ABC-12-RU", "WMG-3672GS (2000Вт, реверс, 3реш)",
    "JVC JK-MG123", "TV SOMETHING-123", "Другое устройство L65MB-APRU",
])
def test_unrelated_nomenclature_keeps_original_exact_matching(model):
    assert source_model(model) == model
    assert contains_model(model, model)


def test_product_photo_binding_accepts_exact_sku_in_main_title_but_not_footer():
    model = "MI TV A PRO 2026 L65MB-APRU QLED 4K SmartTV Android"
    page = Page()
    page.feed('<h1>Xiaomi L65MB-APRU</h1><meta property="og:image" content="/tv.jpg">')
    assert page.product_images(model) == {"/tv.jpg"}
    related = Page()
    related.feed('<h1>Xiaomi L43MB-APRU</h1><meta property="og:image" content="/wrong.jpg">'
                 '<footer>Похожие товары: Xiaomi L65MB-APRU</footer>')
    assert related.product_images(model) == set()


def test_verified_reference_keeps_raw_signed_identity_while_binding_sku():
    model = "MI TV A PRO 2026 L65MB-APRU QLED 4K SmartTV Android"
    image = io.BytesIO()
    Image.new("RGB", (80, 80), "white").save(image, "PNG")
    photo = image.getvalue()
    def handle(request):
        if request.url.path == "/p":
            return httpx.Response(200, text=(
                '<h1>Телевизор Xiaomi L65MB-APRU</h1>'
                '<script type="application/ld+json">'
                '{"@type":"Product","sku":"L65MB-APRU","image":"/tv.png"}</script>'
            ))
        return httpx.Response(200, content=photo)
    resolver = lambda *a, **k: [(socket.AF_INET, 0, 0, "", ("93.184.216.34", 443))]
    web = PublicWeb(httpx.Client(transport=httpx.MockTransport(handle)), resolver)
    candidate = {"url": "https://example.com/p"}
    source, received, _ = web.reference(candidate, model, [candidate["url"]], ["example.com"])
    assert received == photo
    assert source["model_image_verified"] is True
    assert source["model"] == model
