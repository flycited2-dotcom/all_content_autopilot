"""Owner-selected catalog entry points shared by browser research and auditing."""
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin

import httpx
import yaml

from photo_auditor.sources import canonical, contains_model, model_key, official


def route(brand, model, category='', root=None):
    path = Path(root or Path(__file__).resolve().parents[1]) / 'rules/product-source-routes.yaml'
    if not path.exists():
        return None
    config = yaml.safe_load(path.read_text(encoding='utf-8')) or {}
    name = model_key(brand or '')
    if name == 'карвер':
        name = 'carver'
    value = config.get(name)
    if not value:
        return None
    if not (any(t.casefold() in (category or '').casefold() for t in value['category_terms']) or
            any((model or '').strip().upper().startswith(p.upper() + '-') or
                (model or '').strip().upper().startswith(p.upper() + ' ') for p in value['model_prefixes'])):
        return None
    urls = [canonical(url) for url in value['start_urls']]
    if not all(official(url, value['domains']) for url in urls):
        raise ValueError('Стартовый каталог вне разрешённого домена')
    return {**value, 'start_urls': urls}


def instruction(brand, model, category=''):
    selected = route(brand, model, category)
    if not selected:
        return ''
    return ('\n\nПриоритетный источник владельца для этого бренда и категории: '
            + ', '.join(selected['start_urls']) + '. СНАЧАЛА открой этот каталог и найди страницу '
            'точной модели со всеми суффиксами. Бери реальное фото и характеристики со страницы '
            'товара. Категория целиком и соседняя модель не подтверждают фото. Если точная модель '
            'или её фото отсутствует/недоступно, явно отметь это и только затем ищи другой источник. '
            'Не бери цены и остатки с сайта вместо прайса владельца.')


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links, self.href, self.text = [], None, []

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            self.href, self.text = dict(attrs).get('href'), []

    def handle_data(self, data):
        if self.href is not None:
            self.text.append(data)

    def handle_endtag(self, tag):
        if tag == 'a' and self.href is not None:
            self.links.append((self.href, ' '.join(self.text)))
            self.href = None


def candidates(product, web, limit=5):
    selected = route(product['brand'], product['model'], product.get('category', ''))
    if not selected or not callable(getattr(web, 'fetch', None)):
        return []
    found = []
    for entry in selected['start_urls']:
        try:
            final, html = web.fetch(entry, 3 * 1024 * 1024)
            if not official(final, selected['domains']):
                continue
            parser = Links()
            parser.feed(html.decode('utf-8', errors='replace'))
            for href, text in parser.links:
                if not contains_model(text, product['model']):
                    continue
                url = canonical(urljoin(final, href))
                if official(url, selected['domains']) and url not in found:
                    found.append(url)
                    if len(found) >= limit:
                        return [{'url': u} for u in found]
        except (httpx.HTTPError, OSError, ValueError, UnicodeError):
            continue
    return [{'url': u} for u in found]
