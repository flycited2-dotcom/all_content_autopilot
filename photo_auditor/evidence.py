"""Manufacturer first; a documented fallback requires three independent catalogs."""
from photo_auditor.discovery import owner


def eligible(sources, manufacturer_unavailable=False):
    official = {n for n, s in enumerate(sources, 1) if s.get('official') and s.get('model_image_verified')}
    if official:
        return official, 'official'
    verified = {n for n, s in enumerate(sources, 1) if s.get('model_image_verified')}
    if manufacturer_unavailable and len({owner(sources[n-1]['url']) for n in verified}) >= 3:
        return verified, 'secondary_consensus'
    return set(), 'insufficient'


def covers(ids, eligible_ids, sources, basis):
    used = set(ids).intersection(eligible_ids)
    return (bool(used) if basis == 'official' else
            len({owner(sources[n-1]['url']) for n in used}) >= 3)
