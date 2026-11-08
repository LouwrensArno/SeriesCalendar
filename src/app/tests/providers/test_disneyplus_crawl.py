import io
import json
import tempfile
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import requests
from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command
from django.test import SimpleTestCase, TestCase, override_settings

from app.management.commands import crawl_disneyplus_za as command
from app.models import TV, Item, MediaTypes, Sources, Status
from app.providers import disneyplus_crawl as crawl
from app.providers import disneyplus_za as dp

NOW = datetime(2026, 11, 7, 22, 0, tzinfo=UTC)


def eid(n):
    """A title id, in the shape Disney+ uses."""
    return f"entity-a{n:07d}"


def title_page(name, *, seasons=None, year=2018, related=(), region="za", has_content=True, kind=None):
    """A title's own page, as the real one renders it (has_content=False: not carried here)."""
    tiles = "".join(
        f'<a aria-label="{t}" href="/en-za/browse/{e}" rel="noreferrer">x</a>' for e, t in related
    )
    series = f"<span>• {seasons} Seasons</span>" if seasons else ""
    kind = kind or ("TVSeries" if seasons else "Movie")
    episodes = '{"_type":"Episodes"}' if seasons else "null"
    if not has_content:
        return (
            "<html><head></head><body>"
            f'<script id="__NEXT_DATA__">{{"pageProps":{{"hasContent":false,"region":"{region}"}}}}</script>'
            "</body></html>"
        )
    return (
        f"<html><head><title>Watch {name} | Full episodes | Disney+</title></head><body>"
        f'<script id="__NEXT_DATA__">{{"pageProps":{{"hasContent":true,"region":"{region}",'
        f'"ldJSON":{{"@graph":[{{"@type":"{kind}"}}]}},"episodes":{episodes}}}}}</script>'
        f'{series}<script>{{"releaseYear":"{year}"}}</script>{tiles}</body></html>'
    )


class FakeResponse:
    def __init__(self, status=200, text="", headers=None):
        self.status_code, self.text, self.headers = status, text, headers or {}


class FakeSite:
    """Serves title pages by id, records every request, and can be made to misbehave."""

    def __init__(self, pages):
        self.pages = pages  # id -> html | int status | (status, headers) | Exception
        self.requests = []

    def get(self, url, headers=None, timeout=None, allow_redirects=True):
        entity = url.rsplit("/", 1)[-1]
        self.requests.append((entity, headers))
        page = self.pages.get(entity, 404)
        if isinstance(page, Exception):
            raise page
        if isinstance(page, tuple):
            return FakeResponse(page[0], "", page[1])
        if isinstance(page, int):
            return FakeResponse(page, "")
        return FakeResponse(200, page)

    def fetched(self):
        return [e for e, _ in self.requests]


def state_with(*titles, links=None):
    """A crawl state with these (id number, name) titles queued."""
    state = crawl.CrawlState()
    for n, name in titles:
        state.add_seen(eid(n), name, "catalogue", NOW)
        if links:
            state.titles[eid(n)]["links"] = links.get(n, 1)
    return state


def run(state, site, **kwargs):
    sleeps = []
    summary = crawl.run_slice(
        state, session=site, sleep=sleeps.append, now=kwargs.pop("now", NOW),
        jitter=lambda lo, hi: (lo + hi) / 2, **kwargs,
    )
    return summary, sleeps


class CrawlStateTests(SimpleTestCase):
    def test_a_new_title_is_queued_once_and_a_repeat_counts_as_a_link(self):
        state = crawl.CrawlState()

        self.assertTrue(state.add_seen(eid(1), "Bluey", "related:x", NOW))
        self.assertFalse(state.add_seen(eid(1), "Bluey", "related:y", NOW))
        self.assertFalse(state.add_seen(eid(1), "Bluey", "related:z", NOW))

        self.assertEqual(state.queue, [eid(1)])
        self.assertEqual(state.titles[eid(1)]["links"], 3)
        self.assertEqual(state.titles[eid(1)]["source"], "related:x")  # where it was first seen

    def test_seeding_from_the_hub_catalogue_uses_ids_not_addresses(self):
        state = crawl.CrawlState()
        snapshot = {"titles": [{"entity": f"/en-za/browse/{eid(1)}", "title": "A", "collections": []},
                               {"entity": f"/en-za/browse/{eid(2)}", "title": "B", "collections": []}]}

        self.assertEqual(state.seed_from_catalogue(snapshot, NOW), 2)
        self.assertEqual(state.seed_from_catalogue(snapshot, NOW), 0)  # nothing new the second time
        self.assertEqual(state.queue, [eid(1), eid(2)])

    def test_the_same_id_serves_every_region(self):
        self.assertEqual(crawl.entity_id("/en-gb/browse/entity-abc"), "entity-abc")
        self.assertEqual(crawl.entity_path("entity-abc"), "/en-za/browse/entity-abc")

    def test_save_and_load_round_trip_without_a_temp_file_left(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "db" / "crawl.json"
            state = state_with((1, "Shōgun"))

            state.save(path)

            self.assertEqual(crawl.CrawlState.load(path).data, state.data)
            self.assertEqual([p.name for p in path.parent.iterdir()], ["crawl.json"])

    def test_a_missing_damaged_or_old_file_starts_a_fresh_crawl(self):
        with tempfile.TemporaryDirectory() as folder:
            for text in (None, "{not json", json.dumps({"version": 0, "titles": {"x": 1}})):
                path = Path(folder) / "c.json"
                path.unlink(missing_ok=True)
                if text:
                    path.write_text(text)
                state = crawl.CrawlState.load(path)
                self.assertEqual((state.titles, state.queue), ({}, []))

    def test_titles_you_track_are_read_first_then_the_most_linked_then_the_oldest(self):
        state = state_with((1, "Old one"), (2, "Popular"), (3, "Bluey"), (4, "Another"), links={2: 5, 4: 2})

        order = []
        while (entity := state.next_entity({dp.normalize_title("Bluey")})):
            order.append(entity)

        self.assertEqual(order, [eid(3), eid(2), eid(4), eid(1)])

    def test_an_empty_queue_gives_nothing(self):
        self.assertIsNone(crawl.CrawlState().next_entity(frozenset()))

    def test_counts(self):
        state = state_with((1, "A"), (2, "B"))
        state.titles[eid(1)]["status"] = "ok"
        state.queue.remove(eid(1))

        self.assertEqual(state.counts(), {"titles": 2, "queued": 1, "ok": 1})


class RunSliceTests(SimpleTestCase):
    def pages(self, n):
        return {eid(i): title_page(f"Show {i}", seasons=2, year=2000 + i) for i in range(1, n + 1)}

    def test_reads_up_to_the_budget_and_leaves_the_rest_queued(self):
        state = state_with(*[(i, f"Show {i}") for i in range(1, 6)])
        site = FakeSite(self.pages(5))

        summary, _ = run(state, site, pages=3)

        self.assertEqual((summary["pages"], summary["ended"]), (3, "budget"))
        self.assertEqual(len(site.requests), 3)
        self.assertEqual(len(state.queue), 2)

    def test_records_what_a_page_says(self):
        state = state_with((1, "Show 1"), (2, "Film"))
        site = FakeSite({eid(1): title_page("Show 1", seasons=7, year=2012), eid(2): title_page("Film", year=1993)})

        run(state, site)

        show, film = state.titles[eid(1)], state.titles[eid(2)]
        self.assertEqual((show["status"], show["year"], show["seasons"]), ("ok", 2012, 7))
        self.assertEqual((film["status"], film["year"], film["seasons"]), ("ok", 1993, None))
        self.assertEqual(show["page_title"], "Watch Show 1 | Full episodes")
        self.assertEqual(show["checked_at"], "2026-11-07T22:00:00+00:00")

    def test_a_page_records_whether_it_can_really_be_watched(self):
        state = state_with((1, "Show"), (2, "Film"), (3, "Team"), (4, "Elsewhere"))
        site = FakeSite({
            eid(1): title_page("Show", seasons=2),
            eid(2): title_page("Film"),
            eid(3): title_page("Team", kind="ItemList"),
            eid(4): title_page("Elsewhere", has_content=False),
        })

        summary, _ = run(state, site)

        self.assertEqual([state.titles[eid(i)]["playable"] for i in range(1, 5)], [True, True, False, False])
        self.assertEqual(state.titles[eid(3)]["kind"], "ItemList")
        self.assertEqual((summary["pages"], summary["unplayable"]), (4, 2))
        self.assertEqual(state.data["runs"][-1]["unplayable"], 2)

    def test_a_series_page_without_a_list_of_episodes_is_not_watchable(self):
        state = state_with((1, "Teaser"))
        page = title_page("Teaser", seasons=1).replace('"_type":"Episodes"', '"_type":"Other"')

        run(state, FakeSite({eid(1): page}))

        self.assertFalse(state.titles[eid(1)]["playable"])

    def test_a_reread_does_not_count_its_links_twice(self):
        state = state_with((1, "Show"), (2, "Other"))
        site = FakeSite({
            eid(1): title_page("Show", seasons=1, related=[(eid(2), "Other")]),
            eid(2): title_page("Other"),
        })
        run(state, site, pages=1)
        links = state.titles[eid(2)]["links"]
        state.titles[eid(1)]["status"] = "queued"
        state.queue.append(eid(1))

        run(state, site, pages=1, wanted={dp.normalize_title("Show")})

        self.assertEqual(state.titles[eid(2)]["links"], links)

    def test_related_titles_join_the_queue_and_count_their_links(self):
        state = state_with((1, "Show 1"), (2, "Show 2"))
        site = FakeSite({
            eid(1): title_page("Show 1", related=[(eid(3), "New"), (eid(2), "Show 2")]),
            eid(2): title_page("Show 2", related=[(eid(3), "New")]),
            eid(3): title_page("New"),
        })

        summary, _ = run(state, site, pages=2)

        self.assertEqual(summary["new_titles"], 1)
        self.assertEqual(state.titles[eid(3)]["links"], 2)  # linked from both pages
        self.assertEqual(state.titles[eid(3)]["source"], f"related:{eid(1)}")
        self.assertEqual(state.queue, [eid(3)])
        self.assertEqual(state.titles[eid(2)]["links"], 2)  # already known: counted, not re-queued

    def test_waits_between_requests_but_not_before_the_first(self):
        state = state_with(*[(i, f"S{i}") for i in range(1, 4)])

        _, sleeps = run(state, FakeSite(self.pages(3)), delay=(4.0, 6.0))

        self.assertEqual(sleeps, [5.0, 5.0])

    def test_picks_its_pauses_from_the_range_it_is_given(self):
        state = state_with(*[(i, f"S{i}") for i in range(1, 4)])
        asked = []
        crawl.run_slice(state, session=FakeSite(self.pages(3)), sleep=lambda s: None, now=NOW,
                        jitter=lambda lo, hi: asked.append((lo, hi)) or lo, delay=(4.0, 6.0))

        self.assertEqual(asked, [(4.0, 6.0), (4.0, 6.0)])

    def test_a_title_is_read_once_and_the_crawl_resumes_where_it_stopped(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "crawl.json"
            state = state_with(*[(i, f"Show {i}") for i in range(1, 7)])
            site = FakeSite(self.pages(6))
            run(state, site, pages=2, save=lambda s: s.save(path))

            resumed = crawl.CrawlState.load(path)  # a later night, a fresh process
            run(resumed, site, pages=10, save=lambda s: s.save(path))

            self.assertEqual(sorted(site.fetched()), sorted(eid(i) for i in range(1, 7)))
            self.assertEqual(len(site.fetched()), 6)  # none twice
            self.assertEqual(crawl.CrawlState.load(path).counts()["ok"], 6)

    def test_an_empty_queue_ends_the_run(self):
        summary, _ = run(state_with(), FakeSite({}))

        self.assertEqual((summary["pages"], summary["ended"]), (0, "queue empty"))

    def test_a_page_that_no_longer_exists_is_set_aside_and_the_crawl_carries_on(self):
        state = state_with((1, "Gone"), (2, "Here"))
        site = FakeSite({eid(1): 404, eid(2): title_page("Here")})

        summary, _ = run(state, site)

        self.assertEqual((summary["gone"], summary["pages"]), (1, 1))
        self.assertEqual(state.titles[eid(1)]["status"], "gone")
        self.assertEqual(state.queue, [])  # not tried again

    def test_410_counts_as_gone_too(self):
        state = state_with((1, "Gone"))

        run(state, FakeSite({eid(1): 410}))

        self.assertEqual(state.titles[eid(1)]["status"], "gone")

    def test_a_429_stops_at_once_and_pauses_for_as_long_as_the_site_asks(self):
        state = state_with((1, "A"), (2, "B"), (3, "C"))
        site = FakeSite({eid(1): title_page("A"), eid(2): (429, {"Retry-After": "7200"}), eid(3): title_page("C")})

        summary, _ = run(state, site)

        self.assertEqual(summary["ended"], "429")
        self.assertEqual(site.fetched(), [eid(1), eid(2)])  # never touched the third
        self.assertIn(eid(2), state.queue)  # the title that was refused is still to do
        self.assertEqual(state.paused_until(NOW), NOW + timedelta(hours=2))

    def test_a_429_without_a_retry_after_pauses_for_an_hour_and_a_huge_one_is_capped(self):
        for header, expected in (({}, timedelta(hours=1)), ({"Retry-After": "999999"}, timedelta(hours=24)),
                                 ({"Retry-After": "soon"}, timedelta(hours=1)), ({"Retry-After": "5"}, timedelta(minutes=1))):
            state = state_with((1, "A"))
            run(state, FakeSite({eid(1): (429, header)}))
            self.assertEqual(state.paused_until(NOW), NOW + expected, header)

    def test_while_paused_nothing_is_requested_and_it_resumes_afterwards(self):
        state = state_with((1, "A"), (2, "B"))
        run(state, FakeSite({eid(1): (429, {"Retry-After": "3600"})}))

        site = FakeSite({eid(1): title_page("A"), eid(2): title_page("B")})
        during, _ = run(state, site, now=NOW + timedelta(minutes=30))
        after, _ = run(state, site, now=NOW + timedelta(hours=1, minutes=1))

        self.assertEqual((during["ended"], during["pages"], site.fetched()[:0]), ("paused", 0, []))
        self.assertEqual((after["pages"], after["ended"]), (2, "queue empty"))
        self.assertEqual(len(site.fetched()), 2)

    def test_a_403_means_blocked_and_pauses_for_a_day(self):
        state = state_with((1, "A"), (2, "B"))

        summary, _ = run(state, FakeSite({eid(1): 403}))

        self.assertEqual(summary["ended"], "403")
        self.assertEqual(state.paused_until(NOW), NOW + timedelta(hours=24))

    def test_three_failures_in_a_row_stop_the_run_and_pause_it(self):
        state = state_with(*[(i, f"S{i}") for i in range(1, 6)])
        site = FakeSite({eid(i): 503 for i in range(1, 6)})

        summary, _ = run(state, site)

        self.assertEqual((summary["ended"], summary["failed"]), ("errors", 3))
        self.assertEqual(len(site.requests), 3)
        self.assertEqual(state.paused_until(NOW), NOW + timedelta(hours=6))

    def test_a_success_in_between_resets_the_count_of_failures(self):
        pages = {eid(1): 500, eid(2): 500, eid(3): title_page("C"), eid(4): 500, eid(5): 500, eid(6): title_page("F")}
        state = state_with(*[(i, f"S{i}") for i in range(1, 7)])

        summary, _ = run(state, FakeSite(pages), pages=20)

        self.assertNotEqual(summary["ended"], "errors")
        self.assertIsNone(state.paused_until(NOW))

    def test_a_network_error_counts_as_a_failure(self):
        state = state_with((1, "A"))

        summary, _ = run(state, FakeSite({eid(1): requests.ConnectionError("reset")}))

        self.assertEqual(summary["failed"], 1)
        self.assertEqual(state.titles[eid(1)]["errors"], 1)
        self.assertEqual(state.queue, [eid(1)])  # will be tried again

    def test_a_title_that_keeps_failing_is_set_aside(self):
        state = state_with((1, "Broken"))
        site = FakeSite({eid(1): 500})

        for night in range(3):
            run(state, site, now=NOW + timedelta(days=night))

        self.assertEqual(state.titles[eid(1)]["status"], "error")
        self.assertEqual(state.queue, [])

    def test_the_wrong_region_aborts_without_recording_or_losing_the_title(self):
        state = state_with((1, "A"), (2, "B"))
        site = FakeSite({eid(1): title_page("A", region="us")})
        saved = []

        with self.assertRaisesRegex(dp.CatalogueError, "region 'us'"):
            run(state, site, save=lambda s: saved.append(dict(s.data)))

        self.assertEqual(state.titles[eid(1)]["status"], "queued")
        self.assertIn(eid(1), state.queue)
        self.assertEqual(len(site.requests), 1)  # it did not carry on
        self.assertTrue(saved)  # the progress so far was still saved

    def test_progress_is_saved_every_so_often_and_at_the_end(self):
        n = crawl.SAVE_EVERY * 2 + 3
        state = state_with(*[(i, f"S{i}") for i in range(1, n + 1)])
        saves = []

        run(state, FakeSite(self.pages(n)), pages=n, save=lambda s: saves.append(s.counts()["ok"]))

        self.assertEqual(saves, [crawl.SAVE_EVERY, crawl.SAVE_EVERY * 2, n])

    def test_progress_is_saved_even_when_something_goes_wrong_part_way(self):
        state = state_with(*[(i, f"S{i}") for i in range(1, 4)])
        saves = []

        class Exploding(FakeSite):
            def get(self, url, **kwargs):
                if url.endswith(eid(3)):
                    raise KeyboardInterrupt
                return super().get(url, **kwargs)

        with self.assertRaises(KeyboardInterrupt):
            run(state, Exploding(self.pages(3)), save=lambda s: saves.append(s.counts()["ok"]))

        self.assertEqual(saves[-1], 2)  # the two pages already read were kept

    def test_only_the_last_runs_are_kept_in_the_history(self):
        state = crawl.CrawlState()
        for night in range(crawl.KEEP_RUNS + 5):
            run(state, FakeSite({}), now=NOW + timedelta(days=night))

        self.assertEqual(len(state.data["runs"]), crawl.KEEP_RUNS)
        self.assertEqual(state.data["runs"][-1]["ended"], "queue empty")

    def test_requests_carry_the_honest_user_agent_and_no_login(self):
        state = state_with((1, "A"))
        site = FakeSite(self.pages(1))

        run(state, site)

        headers = site.requests[0][1]
        self.assertIn("Yamtrack", headers["User-Agent"])
        self.assertNotIn("Cookie", headers)
        self.assertNotIn("Authorization", headers)


class JudgedAgainTests(SimpleTestCase):
    def test_titles_saved_before_pages_were_judged_are_read_again(self):
        old = {"title": "Old", "source": "catalogue", "first_seen": "x", "links": 4, "status": "ok", "errors": 0,
               "checked_at": "2026-11-07T16:29:12+00:00", "year": 2020, "seasons": 5, "page_title": "p"}
        state = crawl.CrawlState({"version": 1, "region": "za", "titles": {eid(1): old}, "queue": [], "runs": []})

        self.assertEqual((state.titles[eid(1)]["status"], state.queue), ("queued", [eid(1)]))
        self.assertEqual(state.titles[eid(1)]["links"], 4)

    def test_a_judged_title_is_left_alone_and_nothing_is_queued_twice(self):
        state = state_with((1, "A"), (2, "B"))
        state.titles[eid(1)].update(status="ok", playable=True, checked_at="2026-11-07T16:29:12+00:00")
        state.queue.remove(eid(1))
        del state.titles[eid(2)]["playable"]
        state.titles[eid(2)]["status"] = "ok"  # unjudged, but already in the queue

        again = crawl.CrawlState(state.data)

        self.assertEqual(again.queue.count(eid(2)), 1)
        self.assertEqual(again.titles[eid(1)]["status"], "ok")

    def test_stale_titles_are_queued_for_a_fresh_look(self):
        state = state_with((1, "Old"), (2, "Recent"), (3, "Gone"))
        for n, when, status in ((1, "2026-09-01T00:00:00+00:00", "ok"), (2, "2026-11-01T00:00:00+00:00", "ok"),
                                (3, "2026-09-01T00:00:00+00:00", "gone")):
            state.titles[eid(n)].update(status=status, playable=True, checked_at=when)
        state.queue.clear()

        self.assertEqual(state.requeue_stale(NOW), 1)
        self.assertEqual(state.queue, [eid(1)])
        self.assertEqual(state.requeue_stale(NOW), 0)  # not queued twice


class MatchingTests(SimpleTestCase):
    def read(self, pages, titles):
        state = state_with(*titles)
        run(state, FakeSite(pages))
        return state

    def snapshot(self):
        return {"titles": [], "scraped_at": "x"}

    def test_only_titles_that_can_be_watched_are_offered_as_candidates(self):
        state = self.read(
            {eid(1): title_page("Bluey", seasons=3, year=2018), eid(2): title_page("Gone Here", has_content=False)},
            [(1, "Bluey"), (2, "Gone Here")],
        )

        self.assertEqual(
            crawl.catalogue_entries(state),
            [{"entity": f"/en-za/browse/{eid(1)}", "title": "Bluey", "collections": ["Crawl"]}],
        )

    def test_a_crawled_title_matches_what_you_track_without_any_request(self):
        state = self.read({eid(1): title_page("Bluey", seasons=3, year=2018)}, [(1, "Bluey")])
        wanted = [("tv:82728", "Bluey", 2018, "tv")]

        def no_network(path):
            raise AssertionError(path)

        matches = crawl.match_tracked(self.snapshot(), wanted, state, fallback=no_network, delay=0)

        self.assertEqual(matches, {"tv:82728": {"entity": f"/en-za/browse/{eid(1)}", "title": "Bluey"}})

    def test_a_title_that_is_not_carried_here_does_not_match(self):
        state = self.read({eid(1): title_page("Rookie", has_content=False)}, [(1, "Rookie")])
        snapshot = {"titles": [{"entity": f"/en-za/browse/{eid(1)}", "title": "Rookie", "collections": []}]}

        matches = crawl.match_tracked(snapshot, [("movie:1", "Rookie", 2018, "movie")], state, delay=0)

        self.assertEqual(matches, {})

    def test_the_right_kind_and_year_are_still_required(self):
        state = self.read({eid(1): title_page("Elementary", seasons=7, year=2012)}, [(1, "Elementary")])

        matches = crawl.match_tracked(
            self.snapshot(),
            [("movie:1", "Elementary", 2012, "movie"), ("tv:2", "Elementary", 1953, "tv")],
            state,
            delay=0,
        )

        self.assertEqual(matches, {})

    def test_a_title_the_crawl_has_not_read_yet_is_left_to_the_fallback(self):
        state = state_with((1, "Unread"))
        snapshot = {"titles": [{"entity": f"/en-za/browse/{eid(1)}", "title": "Unread", "collections": []}]}
        asked = []

        def fallback(path):
            asked.append(path)
            return {"year": 2020, "seasons": 1, "related": []}

        matches = crawl.match_tracked(snapshot, [("tv:1", "Unread", 2020, "tv")], state, fallback=fallback, delay=0)

        self.assertEqual(asked, [f"/en-za/browse/{eid(1)}"])
        self.assertIn("tv:1", matches)

    def test_by_default_an_unread_title_is_not_requested_and_does_not_match(self):
        state = state_with((1, "Unread"))
        snapshot = {"titles": [{"entity": f"/en-za/browse/{eid(1)}", "title": "Unread", "collections": []}]}

        self.assertEqual(crawl.match_tracked(snapshot, [("tv:1", "Unread", 2020, "tv")], state, delay=0), {})

    def test_a_kept_match_whose_page_is_gone_or_no_longer_watchable_is_dropped(self):
        state = self.read(
            {eid(1): title_page("Bluey", seasons=3), eid(2): title_page("Other", seasons=1, has_content=False)},
            [(1, "Bluey"), (2, "Other")],
        )
        state.titles[eid(1)].update(status="gone")
        previous = {
            "tv:1": {"entity": f"/en-za/browse/{eid(1)}", "title": "Bluey"},
            "tv:2": {"entity": f"/en-za/browse/{eid(2)}", "title": "Other"},
        }

        matches = crawl.match_tracked(
            self.snapshot(), [("tv:1", "Bluey", None, "tv"), ("tv:2", "Other", None, "tv")], state,
            previous=previous, delay=0,
        )

        self.assertEqual(matches, {})

    def test_the_saved_hub_catalogue_is_not_changed_by_matching(self):
        state = self.read({eid(1): title_page("Bluey", seasons=3, year=2018)}, [(1, "Bluey")])
        snapshot = self.snapshot()

        crawl.match_tracked(snapshot, [("tv:1", "Bluey", 2018, "tv")], state, delay=0)

        self.assertEqual(snapshot["titles"], [])


class SwitchTests(SimpleTestCase):
    def test_the_file_switches_the_crawl_on_when_the_environment_is_off(self):
        with tempfile.TemporaryDirectory() as folder, \
             override_settings(DISNEYPLUS_CRAWL=False, BASE_DIR=Path(folder)):
            self.assertFalse(crawl.crawl_enabled())
            (Path(folder) / "db").mkdir()
            (Path(folder) / "db" / "disneyplus_crawl.on").write_text("")

            self.assertTrue(crawl.crawl_enabled())

    def test_the_environment_alone_is_enough(self):
        with tempfile.TemporaryDirectory() as folder, \
             override_settings(DISNEYPLUS_CRAWL=True, BASE_DIR=Path(folder)):
            self.assertTrue(crawl.crawl_enabled())

    def test_the_default_budget_is_a_thousand_pages_a_night(self):
        self.assertEqual(crawl.DEFAULT_PAGES, 1000)


class CrawlLockTests(SimpleTestCase):
    def test_the_lock_exists_while_running_and_is_removed_after(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "c.lock"
            with crawl.crawl_lock(path, NOW):
                self.assertTrue(path.exists())
            self.assertFalse(path.exists())

    def test_a_second_run_is_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "c.lock"
            with crawl.crawl_lock(path, datetime.now(UTC)), self.assertRaises(crawl.CrawlLocked), \
                 crawl.crawl_lock(path, datetime.now(UTC)):
                pass
            self.assertFalse(path.exists())  # the first run's cleanup still happened

    def test_the_lock_is_released_when_the_run_fails(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "c.lock"
            with self.assertRaises(RuntimeError), crawl.crawl_lock(path, NOW):
                raise RuntimeError("boom")
            self.assertFalse(path.exists())

    def test_a_lock_left_by_a_crashed_run_is_replaced_after_a_few_hours(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "c.lock"
            path.write_text("123")
            later = datetime.fromtimestamp(path.stat().st_mtime, UTC) + crawl.LOCK_STALE_AFTER + timedelta(minutes=1)

            with crawl.crawl_lock(path, later):
                self.assertTrue(path.exists())

            fresh = datetime.fromtimestamp(path.stat().st_mtime, UTC) if path.exists() else None
            self.assertIsNone(fresh)


class CrawlCommandBase(TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.state_file = Path(self.folder.name) / "crawl.json"
        self.catalogue = Path(self.folder.name) / "catalogue.json"
        dp.save_snapshot({"titles": [{"entity": f"/en-za/browse/{eid(i)}", "title": f"T{i}", "collections": []}
                                     for i in range(1, 4)]}, self.catalogue)
        patcher = patch.object(command, "catalogue_path", return_value=self.catalogue)
        patcher.start()
        self.addCleanup(patcher.stop)

    def call(self, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            call_command("crawl_disneyplus_za", *args, "--state", str(self.state_file), stdout=out)
        return out.getvalue()

    def fake_slice(self, **fixed):
        summary = {"pages": 2, "new_titles": 1, "gone": 0, "failed": 0, "ended": "budget", **fixed}
        return patch.object(crawl, "run_slice", return_value=summary)


class CrawlCommandTests(CrawlCommandBase):
    def test_it_is_off_unless_switched_on(self):
        with override_settings(DISNEYPLUS_CRAWL=False), self.fake_slice() as read, \
             self.assertRaisesRegex(CommandError, "switched off"):
            self.call()

        read.assert_not_called()
        self.assertFalse(self.state_file.exists())

    def test_it_is_off_by_default(self):
        from django.conf import settings  # noqa: PLC0415

        self.assertFalse(settings.DISNEYPLUS_CRAWL)

    def test_status_works_when_switched_off_and_never_touches_the_site(self):
        with override_settings(DISNEYPLUS_CRAWL=False), self.fake_slice() as read:
            output = self.call("--status")

        read.assert_not_called()
        self.assertIn("Crawl state: 0 queued, 0 titles", output)

    def test_the_first_run_seeds_the_queue_from_the_hub_catalogue_and_saves_progress(self):
        with override_settings(DISNEYPLUS_CRAWL=True), self.fake_slice() as read:
            output = self.call()

        self.assertIn("Started a new crawl with 3 titles", output)
        self.assertIn("Read 2 pages (1 new titles", output)
        self.assertEqual(len(crawl.CrawlState.load(self.state_file).titles), 3)
        self.assertEqual(read.call_args.kwargs["pages"], crawl.DEFAULT_PAGES)
        self.assertEqual(read.call_args.kwargs["delay"], crawl.DEFAULT_DELAY)

    def test_a_later_run_carries_on_without_seeding_again(self):
        with override_settings(DISNEYPLUS_CRAWL=True), self.fake_slice():
            self.call()
            output = self.call()

        self.assertNotIn("Started a new crawl", output)

    def test_titles_you_track_are_passed_so_they_are_read_first(self):
        user = get_user_model().objects.create_user(username="u")
        item = Item.objects.create(media_id="1", source=Sources.TMDB.value, media_type=MediaTypes.TV.value,
                                   title="Bluey", image="http://x/i.jpg")
        TV.objects.create(item=item, user=user, status=Status.PLANNING.value)

        with override_settings(DISNEYPLUS_CRAWL=True), self.fake_slice() as read:
            self.call("--pages", "50", "--delay-min", "3", "--delay-max", "4")

        self.assertEqual(read.call_args.kwargs["wanted"], {"bluey"})
        self.assertEqual((read.call_args.kwargs["pages"], read.call_args.kwargs["delay"]), (50, (3.0, 4.0)))

    def test_a_pause_is_reported(self):
        with override_settings(DISNEYPLUS_CRAWL=True), self.fake_slice(ended="429", paused_until="2026-11-08T00:00:00+00:00"):
            output = self.call()

        self.assertIn("Stopped: 429", output)
        self.assertIn("Paused until 2026-11-08", output)

    def test_unkind_delays_are_refused(self):
        with override_settings(DISNEYPLUS_CRAWL=True), self.fake_slice() as read:
            for args in (("--delay-min", "0.2"), ("--delay-min", "5", "--delay-max", "3")):
                with self.assertRaisesRegex(CommandError, "delay"):
                    self.call(*args)

        read.assert_not_called()

    def test_a_crawl_already_running_is_refused(self):
        lock = Path(f"{self.state_file}.lock")
        lock.write_text("1")

        with override_settings(DISNEYPLUS_CRAWL=True), self.fake_slice() as read, \
             self.assertRaisesRegex(CommandError, "Another crawl is running"):
            self.call()

        read.assert_not_called()

    def test_a_wrong_region_is_a_clear_error_and_the_progress_is_kept(self):
        def boom(state, **kwargs):
            state.titles[eid(1)]["status"] = "ok"
            raise dp.CatalogueError("Disney+ answered for region 'us', not 'za'")

        with override_settings(DISNEYPLUS_CRAWL=True), patch.object(crawl, "run_slice", side_effect=boom), \
             self.assertRaisesRegex(CommandError, "region 'us'"):
            self.call()

        self.assertEqual(crawl.CrawlState.load(self.state_file).titles[eid(1)]["status"], "ok")


class WhereToWatchUpdateTests(CrawlCommandBase):
    """After a slice the command matches what you track against everything crawled so far."""

    def setUp(self):
        super().setUp()
        state = crawl.CrawlState()
        state.add_seen(eid(1), "T1", "catalogue", NOW)
        state.queue.clear()
        state.titles[eid(1)].update(status="ok", playable=True, year=2018, seasons=2, checked_at=NOW.isoformat())
        state.save(self.state_file)
        user = get_user_model().objects.create_user(username="viewer")
        item = Item.objects.create(media_id="5", source=Sources.TMDB.value, media_type=MediaTypes.TV.value,
                                   title="T1", image="http://x/i.jpg")
        TV.objects.create(item=item, user=user, status=Status.PLANNING.value)
        for target, value in (
            (command, {"refresh_changed": lambda old, new: list(set(old) | set(new))}),
        ):
            patcher = patch.multiple(target, **value)
            patcher.start()
            self.addCleanup(patcher.stop)
        year = patch("app.management.commands.scrape_disneyplus_za.release_year", return_value=2018)
        year.start()
        self.addCleanup(year.stop)

    def saved_matches(self):
        return (dp.load_snapshot(self.catalogue) or {}).get("matches")

    def test_a_crawled_title_you_track_is_matched_and_saved(self):
        with override_settings(DISNEYPLUS_CRAWL=True), self.fake_slice():
            output = self.call()

        self.assertEqual(self.saved_matches(), {"tv:5": {"entity": f"/en-za/browse/{eid(1)}", "title": "T1"}})
        self.assertIn("1 of 1 tracked titles are on Disney+ ZA.", output)
        self.assertIn("Where to Watch updated for 1 titles.", output)

    def test_the_saved_hub_catalogue_keeps_only_its_own_titles(self):
        with override_settings(DISNEYPLUS_CRAWL=True), self.fake_slice():
            self.call()

        self.assertEqual(len(dp.load_snapshot(self.catalogue)["titles"]), 3)

    def test_nothing_is_rewritten_when_the_matches_have_not_changed(self):
        with override_settings(DISNEYPLUS_CRAWL=True), self.fake_slice():
            self.call()
            with patch.object(command, "refresh_changed") as refresh:
                output = self.call()

        refresh.assert_not_called()
        self.assertNotIn("updated", output)

    def test_no_match_leaves_where_to_watch_alone(self):
        with override_settings(DISNEYPLUS_CRAWL=True), self.fake_slice():
            output = self.call("--no-match")

        self.assertIsNone(self.saved_matches())
        self.assertNotIn("tracked titles", output)

    def test_without_a_hub_catalogue_it_says_so_and_carries_on(self):
        self.catalogue.unlink()

        with override_settings(DISNEYPLUS_CRAWL=True), self.fake_slice():
            output = self.call()

        self.assertIn("Where to Watch not updated", output)

    def test_when_nothing_new_is_left_titles_read_long_ago_are_checked_again(self):
        state = crawl.CrawlState.load(self.state_file)
        state.titles[eid(1)]["checked_at"] = (NOW - timedelta(days=90)).isoformat()
        state.save(self.state_file)

        with override_settings(DISNEYPLUS_CRAWL=True), self.fake_slice() as read:
            output = self.call()

        self.assertIn("checking 1 titles last read over a month ago", output)
        self.assertEqual(read.call_args.args[0].queue, [eid(1)])

    def test_the_run_reports_how_many_pages_were_not_watchable(self):
        with override_settings(DISNEYPLUS_CRAWL=True), self.fake_slice(unplayable=3):
            output = self.call()

        self.assertIn("3 not watchable here", output)

    def test_the_file_switch_lets_the_command_run(self):
        with override_settings(DISNEYPLUS_CRAWL=False), patch.object(crawl, "crawl_enabled", return_value=True), \
             self.fake_slice() as read:
            self.call()

        read.assert_called_once()


class WeeklyScrapeUsesTheCrawlTests(TestCase):
    """The weekly hub scrape matches against what the crawl already read, instead of re-fetching it."""

    def test_a_title_only_the_crawl_found_is_matched_without_fetching_its_page(self):
        scrape = "app.management.commands.scrape_disneyplus_za"
        with tempfile.TemporaryDirectory() as folder:
            state_file, catalogue = Path(folder) / "crawl.json", Path(folder) / "c.json"
            state = crawl.CrawlState()
            state.add_seen(eid(1), "Reservation Dogs", "related:x", NOW)
            state.queue.clear()
            state.titles[eid(1)].update(status="ok", playable=True, year=2021, seasons=3, checked_at=NOW.isoformat())
            state.save(state_file)
            hub = {
                "scraped_at": "2026-11-08T10:00:00+00:00", "region": "za", "pages_read": 7,
                "titles": [{"entity": f"/e{i}", "title": f"T{i}", "collections": []} for i in range(150)],
            }
            user = get_user_model().objects.create_user(username="viewer")
            item = Item.objects.create(media_id="129", source=Sources.TMDB.value, media_type=MediaTypes.TV.value,
                                       title="Reservation Dogs", image="http://x/i.jpg")
            TV.objects.create(item=item, user=user, status=Status.PLANNING.value)

            with patch.object(crawl, "state_path", return_value=state_file), \
                 patch.object(dp, "fetch_catalogue", return_value=hub), \
                 patch.object(dp, "entity_details", side_effect=AssertionError("fetched a page")), \
                 patch(f"{scrape}.release_year", return_value=2021), \
                 patch(f"{scrape}.refresh_changed", return_value=["tv:129"]):
                call_command("scrape_disneyplus_za", output=str(catalogue), stdout=io.StringIO())

            saved = dp.load_snapshot(catalogue)
            self.assertEqual(saved["matches"], {"tv:129": {"entity": f"/en-za/browse/{eid(1)}", "title": "Reservation Dogs"}})
            self.assertEqual(len(saved["titles"]), 150)  # the crawl's titles are not copied into the hub catalogue
