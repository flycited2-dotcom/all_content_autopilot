"""Bounded, verified source collection shared by research and independent auditing."""
from datetime import datetime, timezone
from urllib.parse import urlsplit

import httpx

from photo_auditor.images import digest
from photo_auditor.sources import canonical, official


def owner(url):
    host = urlsplit(url).hostname or ''
    parts = host.lower().removeprefix('www.').split('.')
    count = 3 if '.'.join(parts[-2:]) in {'co.uk', 'com.au', 'co.jp', 'com.cn'} else 2
    return '.'.join(parts[-count:])


def collect(product, domains, research, web, folder):
    sources, paths, failures, hosts, failed_urls = [], [], [], set(), []
    from photo_auditor.source_routes import candidates as preferred_candidates, route
    preferred = route(product['brand'], product['model'], product.get('category', ''))
    if preferred:
        domains = preferred['domains']
    direct = preferred_candidates(product, web)
    # A blocked shop must not consume the last search while only two usable
    # catalogs remain. Keep the number of searches and saved sources bounded.
    scopes = ([domains] if direct else []) + ([domains, (), (), ()] if domains else [(), (), ()])
    for index, scope in enumerate(scopes):
        if index and (any(s['official'] and s.get('model_image_verified') for s in sources) or
                      any(s.get('model_image_verified') for s in sources)):
            break
        remaining = 5 - len(sources)
        if not remaining:
            break
        if direct and index == 0:
            candidates, cited = direct, [c['url'] for c in direct]
        else:
            candidates, cited = research(product, scope, remaining,
                                         failed_urls=[*failed_urls, *(s['url'] for s in sources)])
        for candidate in candidates:
            if len(sources) == 5:
                break
            try:
                if not isinstance(candidate, dict) or set(candidate) not in ({'url'}, {'url', 'image_url'}):
                    raise ValueError('Неверный источник')
                url = canonical(candidate['url'])
                if owner(url) in hosts or url in failed_urls:
                    continue
                if scope and not official(url, domains):
                    raise ValueError('Результат вне официального домена')
                source, photo, html = web.reference(candidate, product['model'], cited, domains)
                if owner(source['url']) in hosts:
                    continue
                n = len(sources) + 1
                path = folder / f'source-{n}.bin'
                path.write_bytes(photo)
                (folder / f'source-{n}.html').write_bytes(html)
                source.update(id=n, image_sha256=digest(photo), page_sha256=digest(html),
                              fetched_at=datetime.now(timezone.utc).isoformat())
                sources.append(source); paths.append(path); hosts.add(owner(source['url']))
            except Exception as exc:
                try:
                    url = canonical(candidate.get('url', ''))
                    failed_urls.append(url)
                except (AttributeError, ValueError, TypeError):
                    url = ''
                failure = {'error': type(exc).__name__, 'official': bool(url and official(url, domains))}
                if isinstance(exc, httpx.HTTPStatusError):
                    failure['http_status'] = exc.response.status_code
                if url:
                    parsed = urlsplit(url)
                    failure['url'] = 'https://' + parsed.netloc + parsed.path
                failures.append(failure)
        if scope and any(s['official'] and s.get('model_image_verified') for s in sources):
            break
    return sources, paths, failures
