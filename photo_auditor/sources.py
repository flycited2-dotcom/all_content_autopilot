"""Fetch only public, cited pages and reference images linked by those pages."""
import ipaddress
import re
import socket
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from photo_auditor.images import image_bytes


def canonical(url):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise ValueError("Источник должен быть публичным HTTPS URL")
    return urlunsplit(("https", parsed.netloc.lower(), parsed.path or "/", parsed.query, ""))


def model_key(value):
    return re.sub(r"[^a-zа-яё0-9]", "", value.casefold())


def contains_model(text, model):
    # Separators may differ; a longer model/suffix must never match a short SKU.
    tokens = re.findall(r"[a-zа-яё0-9]+", model.casefold())
    pattern = r"(?<![a-zа-яё0-9])" + r"[\W_]*".join(map(re.escape, tokens)) + r"(?![a-zа-яё0-9]|[-_/.][a-zа-яё0-9])"
    return bool(tokens and re.search(pattern, text.casefold()))


def official(url, domains):
    host = urlsplit(url).hostname or ""
    return any(host == d or host.endswith("." + d) for d in domains)


class Page(HTMLParser):
    def __init__(self):
        super().__init__()
        self.text, self.images, self.skip = [], set(), 0
        self.primary_images, self.structured, self._ld = set(), [], None
        self.headings, self._heading = [], 0

    def handle_starttag(self, tag, attrs):
        attr = dict(attrs)
        if tag in ("h1", "title"):
            self._heading += 1
        if tag in ("style", "script"):
            self.skip += 1
        if tag == "script" and attr.get("type", "").lower() == "application/ld+json":
            self._ld = []
        if tag == "meta" and attr.get("property", attr.get("name", "")).lower() in ("og:image", "twitter:image"):
            self.images.add(attr.get("content", ""))
            self.primary_images.add(attr.get("content", ""))
        if tag == "img":
            for key in ("src", "data-src", "data-original"):
                if attr.get(key):
                    self.images.add(attr[key])
            for key in ("srcset", "data-srcset"):
                self.images.update(p.strip().split(" ")[0] for p in attr.get(key, "").split(",") if p.strip())
        if tag == "a" and re.search(r"\.(?:png|jpg|jpeg|webp)(?:\?|$)", attr.get("href", ""), re.I):
            self.images.add(attr["href"])

    def handle_endtag(self, tag):
        if tag in ("h1", "title"):
            self._heading = max(0, self._heading - 1)
        if tag == "script" and self._ld is not None:
            import json
            try:
                self.structured.append(json.loads("".join(self._ld)))
            except ValueError:
                pass
            self._ld = None
        if tag in ("style", "script"):
            self.skip = max(0, self.skip - 1)

    def handle_data(self, data):
        if self._heading:
            self.headings.append(data)
        if self._ld is not None:
            self._ld.append(data)
        if not self.skip:
            self.text.append(data)

    def product_images(self, model):
        # A SKU in a related-products footer does not identify the page's main photo.
        found = set(self.primary_images) if contains_model(" ".join(self.headings), model) else set()
        def walk(value, depth=0):
            if depth > 15:
                return
            if isinstance(value, list):
                for item in value:
                    walk(item, depth + 1)
            if not isinstance(value, dict):
                return
            kind = value.get("@type", [])
            if kind == "Product" or isinstance(kind, list) and "Product" in kind:
                names = " ".join(str(value.get(k, "")) for k in ("sku", "mpn", "model", "name"))
                if contains_model(names, model):
                    images = value.get("image", [])
                    for image in images if isinstance(images, list) else [images]:
                        if isinstance(image, str):
                            found.add(image)
                        elif isinstance(image, dict) and isinstance(image.get("url"), str):
                            found.add(image["url"])
            for item in value.values():
                if isinstance(item, (dict, list)):
                    walk(item, depth + 1)
        for value in self.structured:
            walk(value)
        return found


class PublicWeb:
    def __init__(self, client=None, resolver=socket.getaddrinfo):
        self.client = client or httpx.Client(timeout=25, follow_redirects=False, trust_env=False)
        self.resolver = resolver

    def fetch(self, url, maximum):
        url = canonical(url)
        for _ in range(5):
            host = urlsplit(url).hostname
            addresses = self.resolver(host, 443, type=socket.SOCK_STREAM)
            if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
                raise ValueError("Непубличный адрес источника")
            with self.client.stream("GET", url, headers={"User-Agent": "FotoAuditor/1.0"}) as response:
                if response.is_redirect:
                    url = canonical(urljoin(url, response.headers["location"]))
                    continue
                response.raise_for_status()
                content = bytearray()
                for chunk in response.iter_bytes():
                    content.extend(chunk)
                    if len(content) > maximum:
                        raise ValueError("Источник слишком большой")
                return url, bytes(content)
        raise ValueError("Слишком много перенаправлений")

    def reference(self, candidate, model, cited, domains):
        url = canonical(candidate["url"])
        visited = set()
        for value in cited:
            try:
                visited.add(canonical(value))
            except (ValueError, TypeError):
                pass
        if url not in visited:
            raise ValueError("Нет подтверждения посещения источника поиском")
        final, html = self.fetch(url, 3 * 1024 * 1024)
        page = Page()
        page.feed(html.decode("utf-8", errors="replace"))
        text = unescape(" ".join(page.text))
        if not contains_model(text, model):
            raise ValueError("На странице нет точной номенклатуры")
        image_url = canonical(urljoin(final, candidate["image_url"]))
        linked = set()
        for value in page.images:
            try:
                linked.add(canonical(urljoin(final, value)))
            except ValueError:
                pass
        primary = set()
        for value in page.product_images(model):
            try:
                primary.add(canonical(urljoin(final, value)))
            except ValueError:
                pass
        if image_url not in linked | primary:
            raise ValueError("Эталонное фото не связано со страницей товара")
        _, photo = self.fetch(image_url, 10 * 1024 * 1024)
        return {"url": final, "image_url": image_url, "official": official(final, domains),
                "model_image_verified": image_url in primary,
                "model": model, "text": text[:16000]}, image_bytes(photo), html
