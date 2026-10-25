"""Code-rendered repair cards: the product photograph stays an unchanged asset."""
import base64
import html
import json
import io
from pathlib import Path
from PIL import Image, ImageChops

from photo_auditor.images import image_bytes


def facts_from_specs(specs):
    text = (specs or '').strip()
    try:
        value = json.loads(text)
    except (ValueError, TypeError):
        value = None
    if isinstance(value, dict):
        values = value.get('features') or []
    else:
        values = text.splitlines()
    if not isinstance(values, list) or any(not isinstance(v, str) for v in values):
        raise ValueError('Характеристики должны быть строками из исходного задания')
    return [v.strip().lstrip('✓•—- ').strip() for v in values if v.strip()][:8]


def card_html(photo, brand, model, specs):
    data = image_bytes(photo)
    with Image.open(io.BytesIO(data)) as asset:
        mime = {'PNG': 'image/png', 'JPEG': 'image/jpeg', 'WEBP': 'image/webp'}[asset.format]
        width, height = asset.size
        # Read the white margins to choose an SVG viewport; the embedded asset is unchanged.
        rgb = asset.convert('RGB')
        corners = [rgb.getpixel(p) for p in [(0, 0), (width - 1, 0), (0, height - 1), (width - 1, height - 1)]]
        box = None
        if all(min(pixel) >= 248 for pixel in corners):
            mask = ImageChops.difference(rgb, Image.new('RGB', asset.size, 'white')).convert('L')
            box = mask.point(lambda p: 255 if p > 7 else 0).getbbox()
        if box:
            margin = max(8, round(max(box[2] - box[0], box[3] - box[1]) * .04))
            box = (max(0, box[0] - margin), max(0, box[1] - margin),
                   min(width, box[2] + margin), min(height, box[3] + margin))
        else:
            box = (0, 0, width, height)
        viewport = f'{box[0]} {box[1]} {box[2] - box[0]} {box[3] - box[1]}'
    image = 'data:' + mime + ';base64,' + base64.b64encode(data).decode('ascii')
    facts = facts_from_specs(specs)
    escape = lambda value: html.escape(str(value), quote=True)
    rows = ''.join('<li><span class="tick">✓</span><span>' + escape(fact) + '</span></li>' for fact in facts)
    facts_title = '<h1>Характеристики</h1>' if facts else ''
    return '''<!doctype html><html lang="ru"><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data:; style-src 'unsafe-inline'">
<style>
*{box-sizing:border-box}body{margin:0;width:1600px;height:1600px;font-family:"Segoe UI",Arial,sans-serif;color:#14233b;
background:linear-gradient(135deg,#edf3fa 0%,#fff 70%)}
main{padding:76px;height:100%;display:flex;flex-direction:column;gap:44px}
header{display:flex;justify-content:space-between;align-items:flex-start;gap:40px;min-height:160px}
.brand{font-weight:800;font-size:86px;line-height:1.15;max-width:620px;overflow-wrap:anywhere}
.model{padding:25px 32px;border-radius:22px;background:#153a66;color:white;max-width:730px;font-size:44px;
font-weight:650;overflow-wrap:anywhere;text-align:right}
.body{display:grid;grid-template-columns:58% 42%;flex:1;min-height:0;background:white;border:1px solid #dee7f0;
border-radius:36px;box-shadow:0 18px 50px #14233b10;overflow:hidden}
.photo{padding:44px;display:flex;align-items:center;justify-content:center;background:white;min-width:0;min-height:0}
svg{width:100%;height:100%;display:block}
.facts{padding:46px 40px 40px 12px;display:flex;flex-direction:column;justify-content:center;gap:28px}
h1{font-size:42px;line-height:1.2;margin:0 0 12px;font-weight:700}
ul{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:22px}
li{display:flex;gap:18px;font-size:31px;line-height:1.35;padding:24px 22px;background:#f1f6fc;border-radius:22px;
overflow-wrap:anywhere}.tick{color:#1467c5;font-weight:800}
footer{font-size:28px;color:#51637b;letter-spacing:.2px;padding:0 6px}
</style><main><header><div class="brand">''' + escape(brand) + '''</div><div class="model">''' + escape(model) + '''</div></header>
<section class="body"><div class="photo"><svg role="img" aria-label="''' + escape(brand + ' ' + model) + '''" viewBox="''' + viewport + '''" preserveAspectRatio="xMidYMid meet"><image width="''' + str(width) + '''" height="''' + str(height) + '''" href="''' + image + '''"/></svg></div>
<div class="facts">''' + facts_title + '''<ul>''' + rows + '''</ul></div></section>
<footer>Наличие и условия заказа уточняйте у менеджера</footer></main></html>'''


async def render_card(source, output, brand, model, specs):
    from playwright.async_api import async_playwright
    source, output = Path(source), Path(output)
    if source.resolve() == output.resolve():
        raise ValueError('Исходное фото нельзя перезаписывать')
    markup = card_html(source.read_bytes(), brand, model, specs)
    output.parent.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as engine:
        browser = await engine.chromium.launch(channel='chrome', headless=True)
        try:
            page = await browser.new_page(viewport={'width': 1600, 'height': 1600}, device_scale_factor=1)
            await page.route('**/*', lambda route: route.abort())
            await page.set_content(markup, wait_until='load')
            await page.evaluate('document.fonts.ready')
            valid = await page.locator('svg image').evaluate('''(node) => new Promise(resolve => {
                const image = new Image(); image.onload = () => resolve(image.naturalWidth > 0);
                image.onerror = () => resolve(false); image.src = node.getAttribute('href'); })''')
            if not valid:
                raise ValueError('Не удалось отобразить исходную фотографию')
            for font in (30, 28, 26):
                if await page.locator('.facts').evaluate('(node) => node.scrollHeight <= node.clientHeight'):
                    break
                await page.locator('.facts').evaluate('''(node, size) => {
                    node.querySelector('ul').style.gap = '16px';
                    for (const row of node.querySelectorAll('li')) {
                        row.style.fontSize = size + 'px'; row.style.padding = '18px 20px';
                    }
                }''', font)
            if not await page.locator('.facts').evaluate('(node) => node.scrollHeight <= node.clientHeight'):
                raise ValueError('Характеристики не помещаются в карточку; требуется другой макет')
            if not await page.locator('main').evaluate('''(node) => [...node.querySelectorAll('header,.body,footer')]
                .every(el => { const r = el.getBoundingClientRect(); return r.bottom <= 1600 && r.right <= 1600; })'''):
                raise ValueError('Содержимое не помещается в карточку')
            await page.screenshot(path=str(output), type='png')
        finally:
            await browser.close()
    image_bytes(output.read_bytes())
    return output
