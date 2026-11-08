"""A resumable, polite crawl of the public Disney+ South Africa title pages.

`disneyplus_za.fetch_catalogue` reads only the curated hub pages. A show's own page also
lists related titles, so reading those pages finds titles the hubs never show. There are
thousands, so the crawl is done in small slices (a few hundred pages a night) and keeps
everything it learns in a state file, so it can stop at any time and carry on later.

Each title is read at most once: its year, whether it is a series, and the titles it
links to. Nothing is read while paused, and the crawl backs off on the first sign that the
site objects (429, 403, or a run of errors).

Reads the public pages only: no login, no cookies, one request at a time, an honest
User-Agent, a pause between requests, and it refuses to run unless switched on.

What it learns also feeds Where to Watch: a title counts as on Disney+ only if its page
says the service really carries it (see `disneyplus_za.is_playable`), because a title that
is not carried in South Africa still gets a page, just an empty one.
"""

import json
import logging
import os
import random
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import requests
from django.conf import settings

from app.providers import disneyplus_za as dp

logger = logging.getLogger(__name__)

STATE_VERSION = 1
LANDING = "/en-za/browse/"

DEFAULT_PAGES = 1000  # per run
DEFAULT_DELAY = (4.0, 6.0)  # seconds between requests, picked at random
SAVE_EVERY = 25  # pages between saves, so a crash loses little
MAX_ERRORS_PER_TITLE = 3  # then it is set aside
FAILURES_BEFORE_STOPPING = 3  # in a row
DEFAULT_PAUSE_429 = timedelta(hours=1)
PAUSE_403 = timedelta(hours=24)
PAUSE_FAILURES = timedelta(hours=6)
MAX_PAUSE = timedelta(hours=24)
LOCK_STALE_AFTER = timedelta(hours=6)
STALE_AFTER = timedelta(days=30)  # a title read longer ago than this is read again
KEEP_RUNS = 20


class CrawlLocked(dp.CatalogueError):
    """Another crawl is running (or died recently): never run two at once."""


def state_path():
    """Where the crawl keeps what it has learned: next to the database."""
    return settings.BASE_DIR / "db" / "disneyplus_za_crawl.json"


def switch_path():
    """The file whose presence switches the crawl on (for when no environment can be set)."""
    return settings.BASE_DIR / "db" / "disneyplus_crawl.on"


def crawl_enabled():
    """Is the crawl switched on: DISNEYPLUS_CRAWL=True, or the switch file exists."""
    return bool(settings.DISNEYPLUS_CRAWL) or switch_path().exists()


def entity_id(path):
    """The id part of '/en-za/browse/entity-<uuid>'. The same id is used in every region."""
    return path.rstrip("/").rsplit("/", 1)[-1]


def entity_path(entity):
    """The South African address of a title."""
    return f"{LANDING}{entity}"


def _stamp(now):
    return now.isoformat(timespec="seconds")


def _fresh_state():
    return {
        "version": STATE_VERSION,
        "region": dp.REGION,
        "titles": {},  # id -> what we know
        "queue": [],  # ids still to read
        "paused_until": None,
        "pause_reason": "",
        "runs": [],
    }


class CrawlState:
    """Everything the crawl has learned, and what it still has to read."""

    def __init__(self, data=None):
        """Wrap saved data (or start empty)."""
        self.data = data if data and data.get("version") == STATE_VERSION else _fresh_state()
        self._requeue_unjudged()

    def _requeue_unjudged(self):
        """Read again the titles saved before pages were judged on whether they can be watched."""
        queued = set(self.queue)
        for entity, entry in self.titles.items():
            if entry["status"] == "ok" and "playable" not in entry and entity not in queued:
                entry["status"] = "queued"
                self.queue.append(entity)

    @classmethod
    def load(cls, path):
        """Read the state file. A missing or damaged file starts a fresh crawl."""
        try:
            return cls(json.loads(Path(path).read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return cls()

    def save(self, path):
        """Write the state atomically, so a crash never leaves half a file."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)

    @property
    def titles(self):
        """Known titles by id."""
        return self.data["titles"]

    @property
    def queue(self):
        """Ids still to read."""
        return self.data["queue"]

    def add_seen(self, entity, title, source, now, *, count_link=True):
        """Note a title we have seen. A new one joins the queue; a repeat counts as a link.

        How many pages link to a title is used to decide what to read first.
        """
        known = self.titles.get(entity)
        if known:
            if count_link:
                known["links"] += 1
            return False
        self.titles[entity] = {
            "title": title,
            "source": source,
            "first_seen": _stamp(now),
            "links": 1,
            "status": "queued",
            "errors": 0,
            "checked_at": None,
            "year": None,
            "seasons": None,
            "page_title": "",
            "kind": None,
            "playable": None,  # True/False once read: can it really be watched here?
        }
        self.queue.append(entity)
        return True

    def requeue_stale(self, now, max_age=STALE_AFTER):
        """Queue the titles read more than `max_age` ago, to check they are still there.

        Returns how many. The crawl calls this once it has nothing new left to read.
        """
        queued = set(self.queue)
        cutoff = now - max_age
        stale = 0
        for entity, entry in self.titles.items():
            if entry["status"] == "ok" and entity not in queued and datetime.fromisoformat(entry["checked_at"]) < cutoff:
                entry["status"] = "queued"
                self.queue.append(entity)
                stale += 1
        return stale

    def seed_from_catalogue(self, snapshot, now):
        """Add the titles of the saved hub catalogue. Returns how many were new."""
        added = 0
        for entry in (snapshot or {}).get("titles", []):
            added += self.add_seen(entity_id(entry["entity"]), entry["title"], "catalogue", now)
        return added

    def pause(self, until, reason):
        """Stop reading until `until`."""
        self.data["paused_until"] = _stamp(until)
        self.data["pause_reason"] = reason

    def paused_until(self, now):
        """When the pause ends, or None if not paused."""
        raw = self.data.get("paused_until")
        if not raw:
            return None
        until = datetime.fromisoformat(raw)
        return until if now < until else None

    def next_entity(self, wanted):
        """Take the most worthwhile title off the queue, or None if it is empty.

        Titles whose name matches something you track go first, then the ones most pages
        link to, then the order they were found in.
        """
        if not self.queue:
            return None

        def rank(position):
            entry = self.titles[self.queue[position]]
            tracked = dp.normalize_title(entry["title"]) in wanted
            return (not tracked, -entry["links"], position)

        return self.queue.pop(min(range(len(self.queue)), key=rank))

    def requeue(self, entity):
        """Put a title back at the end of the queue (after a failed read)."""
        self.queue.append(entity)

    def counts(self):
        """A summary of the state, for reports."""
        by_status = {}
        for entry in self.titles.values():
            by_status[entry["status"]] = by_status.get(entry["status"], 0) + 1
        playable = sum(1 for entry in self.titles.values() if entry.get("playable"))
        return {"titles": len(self.titles), "queued": len(self.queue), **by_status, **({"playable": playable} if playable else {})}


@contextmanager
def crawl_lock(path, now=None):
    """Refuse to run twice at once. A lock older than a few hours is a crashed run's."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    now = now or datetime.now(UTC)
    if path.exists():
        age = now - datetime.fromtimestamp(path.stat().st_mtime, UTC)
        if age < LOCK_STALE_AFTER:
            msg = f"Another crawl is running (lock {path} is {int(age.total_seconds() // 60)} minutes old)."
            raise CrawlLocked(msg)
        path.unlink(missing_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as error:
        msg = f"Another crawl is running (lock {path})."
        raise CrawlLocked(msg) from error
    try:
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        yield
    finally:
        path.unlink(missing_ok=True)


def _retry_after(response, default=DEFAULT_PAUSE_429):
    """How long the site asked us to wait (Retry-After, in seconds), within sane limits."""
    try:
        wait = timedelta(seconds=int(response.headers.get("Retry-After", "")))
    except (TypeError, ValueError, AttributeError):
        return default
    return min(max(wait, timedelta(minutes=1)), MAX_PAUSE)


def run_slice(  # noqa: PLR0913
    state,
    *,
    pages=DEFAULT_PAGES,
    delay=DEFAULT_DELAY,
    wanted=frozenset(),
    session=None,
    sleep=time.sleep,
    now=None,
    jitter=random.uniform,
    save=None,
):
    """Read up to `pages` queued title pages and learn from them.

    `wanted` is a set of normalised titles you track, which are read first. `save` is
    called every few pages and at the end, even if something goes wrong part way.
    Returns a summary: pages read, new titles found, pages that no longer exist, and why
    it stopped ("budget", "queue empty", "paused", "429", "403", "errors"). `unplayable` counts
    the pages read that turned out not to be something you can watch.
    """
    session = session or requests.Session()
    now = now or datetime.now(UTC)
    summary = {"pages": 0, "new_titles": 0, "gone": 0, "failed": 0, "unplayable": 0, "ended": "budget"}

    until = state.paused_until(now)
    if until:
        summary["ended"] = "paused"
        summary["paused_until"] = _stamp(until)
        return summary

    failures_in_a_row = 0
    requests_made = 0
    deferred = []  # failed titles: tried again on a later run, never straight away
    try:
        while summary["pages"] + summary["failed"] < pages:
            entity = state.next_entity(wanted)
            if entity is None:
                summary["ended"] = "queue empty"
                break
            if requests_made:
                sleep(jitter(*delay))
            requests_made += 1
            entry = state.titles[entity]

            try:
                response = dp._get(session, dp.BASE + entity_path(entity))  # noqa: SLF001
            except requests.RequestException as error:
                logger.warning("Disney+ crawl: %s: %s", entity, error)
                response = None

            if response is not None and response.status_code in (404, 410):
                entry.update(status="gone", checked_at=_stamp(now))
                summary["gone"] += 1
                failures_in_a_row = 0
                continue
            if response is not None and response.status_code == 429:
                state.requeue(entity)
                state.pause(now + _retry_after(response), "the site answered 429 (slow down)")
                summary["ended"] = "429"
                break
            if response is not None and response.status_code == 403:
                state.requeue(entity)
                state.pause(now + PAUSE_403, "the site answered 403 (blocked)")
                summary["ended"] = "403"
                break
            if response is None or response.status_code != 200:
                summary["failed"] += 1
                failures_in_a_row += 1
                entry["errors"] += 1
                if entry["errors"] >= MAX_ERRORS_PER_TITLE:
                    entry["status"] = "error"
                else:
                    deferred.append(entity)
                if failures_in_a_row >= FAILURES_BEFORE_STOPPING:
                    state.pause(now + PAUSE_FAILURES, f"{failures_in_a_row} failed requests in a row")
                    summary["ended"] = "errors"
                    break
                continue

            failures_in_a_row = 0
            try:
                _learn(state, entity, entry, response.text, now, summary)
            except dp.CatalogueError:
                state.requeue(entity)  # nothing was learned from it: it stays to be read
                raise
            summary["pages"] += 1
            if save and summary["pages"] % SAVE_EVERY == 0:
                save(state)
    finally:
        for entity in deferred:
            state.requeue(entity)
        state.data["runs"] = [
            *state.data["runs"][-(KEEP_RUNS - 1):],
            {"when": _stamp(now), **{k: summary[k] for k in ("pages", "new_titles", "gone", "failed", "unplayable", "ended")}},
        ]
        if save:
            save(state)
    return summary


def _learn(state, entity, entry, html, now, summary):
    """Record what a title's own page says, and queue the titles it links to."""
    name, _, _, region = dp.parse_page(html)
    if region and region != dp.REGION:
        msg = (
            f"Disney+ answered for region '{region}', not '{dp.REGION}': "
            "this must run from a South African connection."
        )
        raise dp.CatalogueError(msg)
    details = dp.parse_entity(html)
    first_read = entry["checked_at"] is None  # a re-read must not count its links twice
    entry.update(
        status="ok",
        checked_at=_stamp(now),
        year=details["year"],
        seasons=details["seasons"],
        page_title=name[:100],
        kind=details["kind"],
        playable=dp.is_playable(details),
    )
    summary["unplayable"] += not entry["playable"]
    for path, title in details["related"]:
        summary["new_titles"] += state.add_seen(
            entity_id(path), title, f"related:{entity}", now, count_link=first_read,
        )


def catalogue_entries(state):
    """The titles the crawl found that can really be watched, in the hub catalogue's shape."""
    return [
        {"entity": entity_path(entity), "title": entry["title"], "collections": ["Crawl"]}
        for entity, entry in state.titles.items()
        if entry.get("playable")
    ]


def known_details(state, fallback):
    """A `fetch_details` for `match_titles` that uses what the crawl already read.

    A title the crawl has judged costs no request. One it hasn't is passed to `fallback`
    (which may fetch the page, or give up). A title whose page is gone raises `TitleGone`.
    """

    def fetch(path):
        entry = state.titles.get(entity_id(path))
        if entry and entry["status"] == "gone":
            msg = f"{path} no longer exists"
            raise dp.TitleGone(msg)
        if entry and entry.get("playable") is not None and entry["checked_at"]:
            if not entry["playable"]:
                return {"has_content": False, "year": entry["year"], "seasons": entry["seasons"], "related": []}
            return {"year": entry["year"], "seasons": entry["seasons"], "related": []}
        return fallback(path)

    return fetch


def match_tracked(snapshot, wanted, state, *, previous=None, fallback=None, delay=2.0, sleep=time.sleep):
    """Match what you track against the hub catalogue and everything the crawl has found.

    Works like `disneyplus_za.match_titles`, but the crawl's titles are candidates too and
    the pages it has read are not fetched again. `fallback(path)` is used for a title the
    crawl hasn't read yet; by default it gives up, so nothing is requested. The saved hub
    `snapshot` is left as it was.
    """
    known = {entry["entity"] for entry in snapshot["titles"]}
    extras = [entry for entry in catalogue_entries(state) if entry["entity"] not in known]
    merged = {**snapshot, "titles": [*snapshot["titles"], *extras]}
    if fallback is None:
        def fallback(path):
            msg = f"{path} has not been read yet"
            raise dp.CatalogueError(msg)
    return dp.match_titles(
        merged, wanted, previous=previous, fetch_details=known_details(state, fallback), delay=delay, sleep=sleep,
    )
