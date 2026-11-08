"""Read the public Disney+ South Africa catalogue.

TMDB (and JustWatch, which it takes its data from) list no Disney+ availability for
South Africa, so this reads the logged-out browse pages on disneyplus.com/en-za instead:
no account, no login, no cookies. The pages are server-rendered and each shows a curated
list of titles with a link to its own page. They are capped and not paginated, so the
catalogue is gathered by following the hub pages they link to.

It is deliberately gentle: one request at a time, a pause between requests, a hard cap on
the number of pages, and an honest User-Agent. Run it at most weekly.
"""

import html
import json
import logging
import re
import time
import unicodedata
from datetime import UTC, datetime
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

BASE = "https://www.disneyplus.com"
START_URL = f"{BASE}/en-za"
REGION = "za"
USER_AGENT = (
    "Mozilla/5.0 (compatible; Yamtrack-DisneyPlusZA/1.0; "
    "personal self-hosted catalogue check; once a week)"
)

TILE_RE = re.compile(
    r'aria-label="([^"]+)"\s+href="(/en-za/browse/entity-[0-9a-f-]+)"',
)
# Hub pages (genres, brands, Hulu...). Entity pages are single titles, not hubs.
HUB_LINK_RE = re.compile(r'href="(/en-za/browse/(?!entity-)[A-Za-z0-9_-]+)"')
TITLE_RE = re.compile(r"<title[^>]*>([^<]*)</title>")
REGION_RE = re.compile(r'"region"\s*:\s*"([a-z]{2})"')
YEAR_RE = re.compile(r'"(?:releaseYear|year)"\s*:\s*"?((?:19|20)\d{2})')
SEASONS_RE = re.compile(r"(\d+)\s+Seasons?\b")
# A title the service does not carry in your region still answers 200, with an empty page
# ("hasContent": false) that has no title, no episodes and no related titles.
HAS_CONTENT_RE = re.compile(r'"hasContent"\s*:\s*(true|false)')
KIND_RE = re.compile(r'"@type"\s*:\s*"(?!BreadcrumbList|ListItem|WebPage)([A-Za-z]+)"')
EPISODES_MARKER = '"_type":"Episodes"'
TITLE_KINDS = {"TVSeries", "Movie"}  # anything else (a team, a collection...) is not a title

# A snapshot that is much smaller than the last good one means the site changed, not
# that the catalogue shrank: keep the old one.
MIN_TITLES = 100
MIN_FRACTION_OF_PREVIOUS = 0.5


class CatalogueError(Exception):
    """The catalogue could not be read, or what came back can't be trusted."""


class TitleGone(CatalogueError):
    """A title's own page no longer exists (404/410): it has left the service."""


def parse_page(page_html):
    """Return (page name, [(entity path, title)], [hub links], region) for a browse page."""
    title_match = TITLE_RE.search(page_html)
    name = html.unescape(title_match.group(1)) if title_match else ""
    name = re.sub(r"\s*\|\s*Disney\+\s*$", "", name)
    name = re.sub(r"\s+movies and shows$", "", name)

    tiles, seen = [], set()
    for label, path in TILE_RE.findall(page_html):
        if path not in seen:
            seen.add(path)
            tiles.append((path, html.unescape(label)))

    hubs = list(dict.fromkeys(HUB_LINK_RE.findall(page_html)))
    region_match = REGION_RE.search(page_html)
    return name, tiles, hubs, region_match.group(1) if region_match else None


def parse_entity(page_html):
    """Return what a single title's page says about it.

    Its year and season count, whether the service really has it (`has_content`: None if the
    page doesn't say), what kind of page it is (`kind`: "TVSeries", "Movie"...) and whether
    it lists episodes.
    """
    year = YEAR_RE.search(page_html)
    seasons = SEASONS_RE.search(page_html)
    has_content = HAS_CONTENT_RE.search(page_html)
    kind = KIND_RE.search(page_html)
    return {
        "year": int(year.group(1)) if year else None,
        "seasons": int(seasons.group(1)) if seasons else None,
        "has_content": has_content.group(1) == "true" if has_content else None,
        "kind": kind.group(1) if kind else None,
        "has_episodes": EPISODES_MARKER in page_html,
        # the "more like this" tiles: other titles on the service, free to collect
        "related": parse_page(page_html)[1],
    }


def is_playable(details):
    """Can this title really be watched? Judged from what `parse_entity` returned.

    An empty page (not carried in this region), a page that isn't a show or film, and a
    series page without any episodes are not. Facts the page doesn't give don't count
    against it, so details saved without them still pass.
    """
    if details.get("has_content") is False:
        return False
    if details.get("kind") not in (None, *TITLE_KINDS):
        return False
    return not (details.get("seasons") is not None and details.get("has_episodes") is False)


def normalize_title(title):
    """Reduce a title to lowercase letters and digits, ignoring accents and punctuation.

    So "9-1-1" matches "911", and "Shōgun" matches "Shogun".
    """
    plain = unicodedata.normalize("NFKD", title)
    return re.sub(r"[^a-z0-9]", "", plain.casefold())


def _get(session, url):
    response = session.get(
        url,
        headers={"User-Agent": USER_AGENT, "Accept-Language": "en-ZA,en;q=0.9"},
        timeout=30,
        allow_redirects=True,
    )
    return response


def fetch_catalogue(*, max_pages=100, delay=2.0, session=None, sleep=time.sleep):
    """Crawl the public South African browse pages and return the titles found.

    Starts at the landing page and follows hub links, at most `max_pages` pages in all,
    `delay` seconds apart. Raises CatalogueError if the site answers for another region
    (it picks the region from where you connect) or the landing page can't be read.
    """
    session = session or requests.Session()
    queue = [START_URL]
    visited = set()  # final URLs, so a hub reached by two names counts once
    titles = {}  # entity path -> {"title", "collections"}
    pages_read = 0
    problems = []

    while queue and pages_read < max_pages:
        url = queue.pop(0)
        if pages_read:
            sleep(delay)
        try:
            response = _get(session, url)
        except requests.RequestException as error:
            problems.append(f"{url}: {error}")
            if url == START_URL:
                msg = f"Could not reach {START_URL}: {error}"
                raise CatalogueError(msg) from error
            continue

        final_url = response.url.split("?")[0]
        if final_url in visited:
            continue
        visited.add(final_url)
        if response.status_code != 200:
            problems.append(f"{url}: HTTP {response.status_code}")
            if url == START_URL:
                msg = f"{START_URL} answered HTTP {response.status_code}"
                raise CatalogueError(msg)
            continue

        pages_read += 1
        name, tiles, hubs, region = parse_page(response.text)
        if region and region != REGION:
            msg = (
                f"Disney+ answered for region '{region}', not '{REGION}': this must run "
                "from a South African connection."
            )
            raise CatalogueError(msg)

        for path, title in tiles:
            entry = titles.setdefault(path, {"title": title, "collections": []})
            if name and name not in entry["collections"]:
                entry["collections"].append(name)

        for hub in hubs:
            full = BASE + hub
            if full not in visited and full not in queue:
                queue.append(full)

    if problems:
        logger.warning("Disney+ ZA: %s page(s) skipped: %s", len(problems), problems[:5])
    if queue:
        logger.warning("Disney+ ZA: stopped at the %s-page cap", max_pages)
    return {
        "scraped_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "region": REGION,
        "pages_read": pages_read,
        "titles": [
            {"entity": path, "title": v["title"], "collections": v["collections"]}
            for path, v in sorted(titles.items(), key=lambda kv: kv[1]["title"].lower())
        ],
    }


def load_snapshot(path):
    """Return the saved catalogue, or None if there isn't a readable one."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def check_snapshot(new, previous):
    """Refuse a catalogue that looks broken. Returns None if fine, else the reason."""
    count = len(new["titles"])
    if count < MIN_TITLES:
        return f"only {count} titles found (expected at least {MIN_TITLES})"
    if previous:
        before = len(previous.get("titles", []))
        if before and count < before * MIN_FRACTION_OF_PREVIOUS:
            return f"{count} titles, down from {before}: the site layout has probably changed"
    return None


def save_snapshot(snapshot, path):
    """Write the catalogue atomically, so a crash never leaves half a file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(snapshot, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def index_by_title(snapshot):
    """Map normalised title -> list of catalogue entries (a title can occur twice)."""
    index = {}
    for entry in (snapshot or {}).get("titles", []):
        index.setdefault(normalize_title(entry["title"]), []).append(entry)
    return index


def find_show(index, title):
    """Return the catalogue entries whose title matches `title` exactly (normalised)."""
    return index.get(normalize_title(title), [])


def entity_details(entity_path, *, session=None):
    """Fetch one title's own page and return its year and season count.

    Only used to settle a title that appears more than once; it is a large page, so it
    is not fetched for every title.
    """
    session = session or requests.Session()
    response = _get(session, BASE + entity_path)
    if response.status_code in (404, 410):
        msg = f"{entity_path} no longer exists (HTTP {response.status_code})"
        raise TitleGone(msg)
    if response.status_code != 200:
        msg = f"{entity_path} answered HTTP {response.status_code}"
        raise CatalogueError(msg)
    return parse_entity(response.text)


YEAR_TOLERANCE = 1  # TMDB and Disney+ sometimes disagree by a year (premiere vs release)


def entity_matches(media_type, year, details):
    """Is the Disney+ page `details` describes the same kind of title, from the same year?

    A series' page says "N Seasons", a movie's doesn't. The year may be off by one.
    A page that gives no year can't be checked on it, so it passes on type alone. A page for
    something that can't be watched here (see `is_playable`) never matches.
    """
    if not is_playable(details):
        return False
    is_series = details.get("seasons") is not None
    if is_series != (media_type == "tv"):
        return False
    found = details.get("year")
    return not (found and year) or abs(found - year) <= YEAR_TOLERANCE


def match_titles(
    snapshot,
    wanted,
    *,
    previous=None,
    fetch_details=None,
    delay=2.0,
    sleep=time.sleep,
):
    """Match titles you track to their Disney+ page, checking type and year.

    `wanted` is a list of (key, title, year, media_type): media_type is "tv" or "movie",
    and `year` may be a number, None, or a function that returns one (only called when
    a match has to be checked). Returns {key: {"entity", "title"}}.

    A name in the catalogue costs one request: the title's own page, to check it really
    is that show or film. That page also lists related titles, which are added to the
    catalogue (`snapshot["titles"]` grows) and tried for the titles still unmatched, so
    one page can lead to the next. A name match that fails the check is left out on
    purpose.

    The public pages are curated and rotate, so a title missing from them is not proof
    it left the service. A match from `previous` that isn't found again is therefore
    kept, after checking its own page: dropped only if that page is gone (404/410), kept
    if it still matches or if the page can't be read right now.
    """
    fetch_details = fetch_details or entity_details
    previous = previous or {}
    cache = {}  # entity -> details, or None when it couldn't be read
    state = {"fetched": 0}

    def details_of(entity):
        if entity not in cache:
            if state["fetched"]:
                sleep(delay)
            state["fetched"] += 1
            try:
                cache[entity] = fetch_details(entity)
            except TitleGone:
                cache[entity] = "gone"
            except (CatalogueError, requests.RequestException) as error:
                logger.warning("Disney+ ZA: could not read %s: %s", entity, error)
                cache[entity] = None
        return cache[entity]

    def resolve(year):
        return (year() if callable(year) else year)

    known = {entry["entity"] for entry in snapshot["titles"]}
    matches, pending = {}, list(wanted)
    progress = True
    while pending and progress:
        progress = False
        index = index_by_title(snapshot)
        unmatched = []
        for item in pending:
            key, title, year, media_type = item
            found = False
            for entry in find_show(index, title):
                details = details_of(entry["entity"])
                if not isinstance(details, dict):
                    continue
                for path, name in details.get("related", []):
                    if path not in known:
                        known.add(path)
                        snapshot["titles"].append(
                            {"entity": path, "title": name, "collections": ["Related"]},
                        )
                        progress = True
                if entity_matches(media_type, resolve(year), details):
                    matches[key] = {"entity": entry["entity"], "title": entry["title"]}
                    found = progress = True
                    break
            if not found:
                unmatched.append(item)
        pending = unmatched

    still_tracked = {item[0]: item for item in wanted}
    for key, old in previous.items():
        if key in matches or key not in still_tracked:
            continue
        details = details_of(old["entity"])
        if details == "gone":
            continue
        if details is None or entity_matches(
            still_tracked[key][3], resolve(still_tracked[key][2]), details,
        ):
            matches[key] = old
    return matches
