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

from app.providers import disneyplus_za as dp

logger = logging.getLogger(__name__)

STATE_VERSION = 1
LANDING = "/en-za/browse/"

DEFAULT_PAGES = 300  # per run
DEFAULT_DELAY = (4.0, 6.0)  # seconds between requests, picked at random
SAVE_EVERY = 25  # pages between saves, so a crash loses little
MAX_ERRORS_PER_TITLE = 3  # then it is set aside
FAILURES_BEFORE_STOPPING = 3  # in a row
DEFAULT_PAUSE_429 = timedelta(hours=1)
PAUSE_403 = timedelta(hours=24)
PAUSE_FAILURES = timedelta(hours=6)
MAX_PAUSE = timedelta(hours=24)
LOCK_STALE_AFTER = timedelta(hours=3)
KEEP_RUNS = 20


class CrawlLocked(dp.CatalogueError):
    """Another crawl is running (or died recently): never run two at once."""


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

    def add_seen(self, entity, title, source, now):
        """Note a title we have seen. A new one joins the queue; a repeat counts as a link.

        How many pages link to a title is used to decide what to read first.
        """
        known = self.titles.get(entity)
        if known:
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
        }
        self.queue.append(entity)
        return True

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
        return {"titles": len(self.titles), "queued": len(self.queue), **by_status}


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
    it stopped ("budget", "queue empty", "paused", "429", "403", "errors").
    """
    session = session or requests.Session()
    now = now or datetime.now(UTC)
    summary = {"pages": 0, "new_titles": 0, "gone": 0, "failed": 0, "ended": "budget"}

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
            {"when": _stamp(now), **{k: summary[k] for k in ("pages", "new_titles", "gone", "failed", "ended")}},
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
    entry.update(
        status="ok",
        checked_at=_stamp(now),
        year=details["year"],
        seasons=details["seasons"],
        page_title=name[:100],
    )
    for path, title in details["related"]:
        summary["new_titles"] += state.add_seen(entity_id(path), title, f"related:{entity}", now)
