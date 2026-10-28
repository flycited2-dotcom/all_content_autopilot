"""Deterministic selection from actual catalog rows; never expands brand filters."""
from collections import defaultdict, deque
import unicodedata
import re


def model_name(value):
    # Supplier model fields sometimes append a color; retain the exact SKU prefix.
    value = re.split(r'\s*\((?=\s*\d[\d.,]*\s*(?:к?вт|w|кг|kg|л|l|см|мм|об)\b)',
                     str(value or '').strip(), maxsplit=1, flags=re.I)[0].rstrip()
    return re.split(r'\s+(?=(?:бел(?:ый|ая|ое)|ч[её]рн(?:ый|ая|ое)|графитов(?:ый|ая|ое)|'
                    r'серебрист(?:ый|ая|ое)|сер(?:ый|ая|ое)|бежев(?:ый|ая|ое))\b)',
                    value, maxsplit=1, flags=re.I)[0]


def brand_key(value):
    return unicodedata.normalize('NFKC', str(value or '')).casefold().strip()


def resolve_identity(row, known_brands):
    """Recover an explicitly printed brand and single SKU, without guessing a manufacturer."""
    if str(row.get('brand') or '').strip():
        return row
    title = unicodedata.normalize('NFKC', str(row.get('model') or row.get('name') or '')).strip()
    matches = []
    for brand in sorted({str(b).strip() for b in known_brands if str(b).strip()}, key=str.casefold):
        match = re.search(r'(?<!\w)' + re.escape(brand) + r'(?!\w)', title, re.I)
        if match:
            matches.append((brand, match))
    if len(matches) != 1:
        return row
    brand, match = matches[0]
    model = model_name(title[match.end():].strip(' :—-'))
    if not re.fullmatch(r'(?=.*[a-zа-яё])(?=.*\d)[a-zа-яё0-9][a-zа-яё0-9_./-]*', model, re.I):
        return row
    return {**row, 'brand': brand, 'model': model}


def select_rows(rows, *, count=None, quotas=None, mode='auto', keys=None):
    if count is not None and (type(count) is not int or count < 1):
        raise ValueError('Количество должно быть положительным целым числом')
    quotas = {brand_key(k): v for k, v in (quotas or {}).items()}
    unique = {}
    for row in rows:
        unique.setdefault(row['key'], row)
    rows = list(unique.values())
    if quotas and '*' not in quotas:
        rows = [r for r in rows if brand_key(r.get('brand')) in quotas]
    if keys is not None:
        if (not isinstance(keys, list) or not keys or any(not isinstance(key, str) for key in keys)
                or len(keys) != len(set(keys))):
            raise ValueError('Некорректный список выбранных моделей')
        by_key = {r['key']: r for r in rows}
        missing = [key for key in keys if key not in by_key]
        if missing:
            raise ValueError('Часть выбранных моделей уже недоступна; обновите выбор')
        rows = [by_key[key] for key in keys]
    elif mode == 'auto':
        # Round robin across brand + supplier; stable order makes retries reproducible.
        buckets = defaultdict(deque)
        for row in sorted(rows, key=lambda r: (r.get('name', ''), r['key'])):
            buckets[(brand_key(row.get('brand')), row.get('source', ''))].append(row)
        rows = []
        while any(buckets.values()):
            for key in sorted(buckets):
                if buckets[key]:
                    rows.append(buckets[key].popleft())
    elif mode not in ('all', 'first', 'choose', 'manual'):
        raise ValueError('Неизвестный режим выбора')
    result, used = [], defaultdict(int)
    for row in rows:
        brand = brand_key(row.get('brand'))
        cap = quotas.get(brand)
        if cap is not None and used[brand] >= cap:
            continue
        result.append(row)
        used[brand] += 1
        if count is not None and len(result) >= count:
            break
    return result
