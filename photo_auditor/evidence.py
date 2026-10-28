"""Use exact-model evidence; source availability is not an arbitrary production quota."""


def eligible(sources, manufacturer_unavailable=False):
    official = {n for n, s in enumerate(sources, 1) if s.get('official') and s.get('model_image_verified')
                and s.get('main_product_visible') is not False}
    if official:
        return official, 'official'
    verified = {n for n, s in enumerate(sources, 1) if s.get('model_image_verified')
                and s.get('main_product_visible') is not False}
    if manufacturer_unavailable and verified:
        return verified, 'catalog_reference'
    return set(), 'insufficient'


def covers(ids, eligible_ids, sources, basis):
    used = set(ids).intersection(eligible_ids)
    return bool(used) if basis in {'official', 'catalog_reference'} else False
