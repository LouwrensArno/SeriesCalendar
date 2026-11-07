"""Streaming services TMDB doesn't know about, added to its watch-provider data.

TMDB lists nothing for Disney+ in South Africa, so the weekly `scrape_disneyplus_za` run
matches the shows and films you track to Disney+'s own South African catalogue and saves
the verified matches. This adds those to the provider data TMDB returns, in TMDB's own
shape, so the show pages and the TV list treat Disney+ like any other service.
"""

import json
import logging
from copy import deepcopy

from django.conf import settings

logger = logging.getLogger(__name__)

REGION = "ZA"
# Disney Plus as TMDB itself lists it elsewhere (provider 337, "Disney Plus").
DISNEY_PLUS = {
    "provider_id": 337,
    "provider_name": "Disney Plus",
    "logo_path": "/5eZ872CghnHFLB1j8grszbrx0dx.png",
    "display_priority": 6,
}
DISNEY_PLUS_IDS = {337, 122}  # 122 is "Disney+" in some Asian regions

_cache = {"path": None, "mtime": None, "matches": {}}


def catalogue_path():
    """Where the weekly scrape saves its catalogue and matches."""
    return settings.BASE_DIR / "db" / "disneyplus_za.json"


def disneyplus_matches():
    """Return {"tv:75219": {...}, "movie:1234": {...}} from the saved catalogue.

    Re-read only when the file changes. No file (or a damaged one) means no matches,
    never an error: the page just shows what TMDB knows.
    """
    path = catalogue_path()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        _cache.update(path=None, mtime=None, matches={})
        return {}
    if _cache["path"] == path and _cache["mtime"] == mtime:
        return _cache["matches"]
    try:
        matches = json.loads(path.read_text(encoding="utf-8")).get("matches") or {}
    except (OSError, ValueError):
        logger.warning("Could not read %s", path)
        matches = {}
    _cache.update(path=path, mtime=mtime, matches=matches)
    return matches


def add_extra_providers(providers, media_type, media_id):
    """Return TMDB's providers with Disney+ added for South Africa when it is verified.

    `providers` is TMDB's `watch/providers` results ({"US": {...}, ...}). The input is
    never modified. Left alone if TMDB already lists Disney+ for South Africa.
    """
    if f"{media_type}:{media_id}" not in disneyplus_matches():
        return providers
    merged = deepcopy(providers) if providers else {}
    region = merged.setdefault(REGION, {})
    listed = [p for kind in ("flatrate", "free", "ads") for p in region.get(kind, [])]
    if any(p.get("provider_id") in DISNEY_PLUS_IDS for p in listed):
        return providers
    region.setdefault("flatrate", []).append(dict(DISNEY_PLUS))
    return merged
