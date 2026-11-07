import json
import os
import tempfile
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase

from app.management.commands import scrape_disneyplus_za as command
from app.models import TV, Item, MediaTypes, Movie, Sources, Status, TVShowDetails
from app.providers import disneyplus_za as dp
from app.providers import streaming_extras as extras
from app.providers import tmdb

E_911 = "/en-za/browse/entity-b3519825-4053-4065-b3a6-e93125fb7d54"

TMDB_PROVIDERS = {
    "US": {"flatrate": [{"provider_id": 15, "provider_name": "Hulu", "logo_path": "/h.png", "display_priority": 7}]},
    "ZA": {"flatrate": [{"provider_id": 8, "provider_name": "Netflix", "logo_path": "/n.png", "display_priority": 1}]},
}


class CatalogueFileMixin:
    """Points streaming_extras at a throw-away catalogue file."""

    def use_catalogue(self, payload):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "disneyplus_za.json"
        if payload is not None:
            self.write(payload)
        patcher = patch.object(extras, "catalogue_path", return_value=self.path)
        patcher.start()
        self.addCleanup(patcher.stop)
        extras._cache.update(path=None, mtime=None, matches={})  # noqa: SLF001

    def write(self, payload):
        text = payload if isinstance(payload, str) else json.dumps(payload)
        self.path.write_text(text, encoding="utf-8")


class AddExtraProvidersTests(CatalogueFileMixin, SimpleTestCase):
    def setUp(self):
        # a fresh copy per test: a bug that edits its input must not leak into the next test
        self.providers = deepcopy(TMDB_PROVIDERS)

    MATCHES = {"matches": {"tv:75219": {"entity": E_911, "title": "9-1-1"}, "movie:5": {"entity": "/m", "title": "M"}}}

    def test_adds_disney_plus_to_south_africa_for_a_verified_show(self):
        self.use_catalogue(self.MATCHES)

        merged = extras.add_extra_providers(self.providers, "tv", 75219)

        names = [p["provider_name"] for p in merged["ZA"]["flatrate"]]
        self.assertEqual(names, ["Netflix", "Disney Plus"])
        self.assertEqual(merged["ZA"]["flatrate"][1]["provider_id"], 337)
        self.assertEqual(merged["US"], TMDB_PROVIDERS["US"])  # other regions untouched

    def test_does_not_change_what_it_was_given(self):
        self.use_catalogue(self.MATCHES)
        before = deepcopy(self.providers)

        extras.add_extra_providers(self.providers, "tv", 75219)

        self.assertEqual(self.providers, before)

    def test_two_results_never_share_the_same_provider_object(self):
        self.use_catalogue(self.MATCHES)

        first = extras.add_extra_providers({}, "tv", 75219)
        first["ZA"]["flatrate"][0]["provider_name"] = "Changed"
        second = extras.add_extra_providers({}, "tv", 75219)

        self.assertEqual(second["ZA"]["flatrate"][0]["provider_name"], "Disney Plus")

    def test_a_show_that_did_not_verify_is_left_alone(self):
        self.use_catalogue(self.MATCHES)

        self.assertIs(extras.add_extra_providers(self.providers, "tv", 12345), self.providers)

    def test_show_and_film_with_the_same_id_are_different_titles(self):
        self.use_catalogue(self.MATCHES)

        self.assertIs(extras.add_extra_providers(self.providers, "movie", 75219), self.providers)
        self.assertIn("Disney Plus", [p["provider_name"] for p in extras.add_extra_providers({}, "movie", 5)["ZA"]["flatrate"]])

    def test_south_africa_is_created_when_tmdb_has_no_entry_for_it(self):
        self.use_catalogue(self.MATCHES)

        self.assertEqual(list(extras.add_extra_providers({}, "tv", 75219)), ["ZA"])
        self.assertEqual(list(extras.add_extra_providers(None, "tv", 75219)), ["ZA"])

    def test_if_tmdb_starts_listing_disney_plus_it_is_not_added_twice(self):
        self.use_catalogue(self.MATCHES)
        listed = {"ZA": {"flatrate": [{"provider_id": 337, "provider_name": "Disney Plus"}]}}

        self.assertIs(extras.add_extra_providers(listed, "tv", 75219), listed)

    def test_no_catalogue_file_changes_nothing(self):
        self.use_catalogue(None)

        self.assertIs(extras.add_extra_providers(self.providers, "tv", 75219), self.providers)

    def test_a_damaged_catalogue_file_changes_nothing(self):
        self.use_catalogue("{not json")

        self.assertIs(extras.add_extra_providers(self.providers, "tv", 75219), self.providers)

    def test_a_catalogue_from_before_matching_existed_has_no_matches(self):
        self.use_catalogue({"titles": []})

        self.assertEqual(extras.disneyplus_matches(), {})

    def test_picks_up_a_new_catalogue_without_a_restart(self):
        self.use_catalogue({"matches": {}})
        self.assertEqual(extras.disneyplus_matches(), {})

        self.write(self.MATCHES)
        stamp = self.path.stat().st_mtime + 10
        os.utime(self.path, (stamp, stamp))

        self.assertIn("tv:75219", extras.disneyplus_matches())

    def test_filter_providers_shows_it_with_a_logo_in_priority_order(self):
        self.use_catalogue(self.MATCHES)
        merged = extras.add_extra_providers(self.providers, "tv", 75219)

        shown = tmdb.filter_providers(merged, "ZA")

        self.assertEqual([p["provider_name"] for p in shown], ["Netflix", "Disney Plus"])
        self.assertEqual(shown[1]["image"], "https://image.tmdb.org/t/p/w500/5eZ872CghnHFLB1j8grszbrx0dx.png")
        self.assertEqual(tmdb.filter_providers(merged, "US")[0]["provider_name"], "Hulu")


class TmdbMetadataUsesExtrasTests(CatalogueFileMixin, SimpleTestCase):
    def tv_response(self):
        return {
            "id": 75219, "name": "9-1-1", "number_of_episodes": 148, "poster_path": None, "overview": "x",
            "genres": [], "vote_average": 8.2, "vote_count": 10, "first_air_date": "2018-01-03",
            "last_air_date": "2026-11-05", "status": "Returning Series", "number_of_seasons": 10,
            "episode_run_time": [], "production_companies": [], "production_countries": [],
            "spoken_languages": [], "networks": [], "seasons": [], "external_ids": {},
            "next_episode_to_air": None, "last_episode_to_air": None,
            "watch/providers": {"results": deepcopy(TMDB_PROVIDERS)},
        }

    def test_show_metadata_carries_disney_plus_for_a_verified_show(self):
        self.use_catalogue({"matches": {"tv:75219": {}}})

        data = tmdb.process_tv(self.tv_response())

        self.assertIn("Disney Plus", [p["provider_name"] for p in data["providers"]["ZA"]["flatrate"]])

    def test_show_metadata_is_unchanged_without_a_match(self):
        self.use_catalogue({"matches": {}})

        data = tmdb.process_tv(self.tv_response())

        self.assertEqual(data["providers"], TMDB_PROVIDERS)

    def test_a_season_page_shows_the_shows_disney_plus_too(self):
        self.use_catalogue({"matches": {"tv:75219": {}}})
        season = {"providers": deepcopy(TMDB_PROVIDERS["ZA"] and {"ZA": TMDB_PROVIDERS["ZA"]}), "synopsis": "s", "image": "i"}
        tv_data = {"title": "9-1-1", "tvdb_id": 1, "external_links": {}, "genres": [], "synopsis": "s", "image": "i"}

        out = tmdb.enrich_season_with_tv_data(season, tv_data, 75219, 3)

        self.assertIn("Disney Plus", [p["provider_name"] for p in out["providers"]["ZA"]["flatrate"]])


class EntityMatchesTests(SimpleTestCase):
    def test_a_series_page_matches_a_show_from_the_same_year(self):
        self.assertTrue(dp.entity_matches("tv", 2018, {"seasons": 10, "year": 2018}))

    def test_a_film_page_matches_a_film(self):
        self.assertTrue(dp.entity_matches("movie", 2024, {"seasons": None, "year": 2024}))

    def test_a_film_is_not_a_show_with_the_same_name(self):
        self.assertFalse(dp.entity_matches("tv", 2024, {"seasons": None, "year": 2024}))
        self.assertFalse(dp.entity_matches("movie", 2018, {"seasons": 10, "year": 2018}))

    def test_the_year_may_be_one_out_but_not_more(self):
        self.assertTrue(dp.entity_matches("tv", 2018, {"seasons": 1, "year": 2019}))
        self.assertFalse(dp.entity_matches("tv", 2018, {"seasons": 1, "year": 2005}))

    def test_a_page_without_a_year_passes_on_type_alone(self):
        self.assertTrue(dp.entity_matches("tv", 2018, {"seasons": 1, "year": None}))
        self.assertTrue(dp.entity_matches("tv", None, {"seasons": 1, "year": 1999}))


class MatchTitlesTests(SimpleTestCase):
    SNAP = {"titles": [
        {"entity": "/a", "title": "Elementary", "collections": []},   # a 1950s film on Disney+
        {"entity": "/b", "title": "Elementary", "collections": []},   # the series
        {"entity": E_911, "title": "9-1-1", "collections": []},
    ]}
    PAGES = {
        "/a": {"seasons": None, "year": 1993},
        "/b": {"seasons": 7, "year": 2012},
        E_911: {"seasons": 10, "year": 2018},
    }

    def match(self, wanted, pages=None, **kwargs):
        calls, sleeps = [], []

        def fetch(entity):
            calls.append(entity)
            page = (pages or self.PAGES)[entity]
            if isinstance(page, Exception):
                raise page
            return page

        result = dp.match_titles(self.SNAP, wanted, fetch_details=fetch, sleep=sleeps.append, delay=1.5, **kwargs)
        return result, calls, sleeps

    def test_verifies_a_name_match_against_the_titles_own_page(self):
        result, calls, _ = self.match([("tv:75219", "9-1-1", 2018, "tv")])

        self.assertEqual(result, {"tv:75219": {"entity": E_911, "title": "9-1-1"}})
        self.assertEqual(calls, [E_911])

    def test_picks_the_right_one_when_a_name_is_used_twice(self):
        result, calls, _ = self.match([("tv:1", "Elementary", 2012, "tv")])

        self.assertEqual(result["tv:1"]["entity"], "/b")
        self.assertEqual(calls, ["/a", "/b"])  # the film was checked and rejected

    def test_a_same_named_title_of_the_wrong_kind_or_year_is_left_out(self):
        result, _, _ = self.match([("tv:1", "9-1-1", 2005, "tv"), ("movie:2", "9-1-1", 2018, "movie")])

        self.assertEqual(result, {})

    def test_titles_not_in_the_catalogue_cost_nothing(self):
        result, calls, sleeps = self.match([("tv:9", "House", 2004, "tv"), ("tv:8", "Suits", 2011, "tv")])

        self.assertEqual((result, calls, sleeps), ({}, [], []))

    def test_pauses_between_page_requests_but_not_before_the_first(self):
        _, calls, sleeps = self.match([("tv:1", "Elementary", 2012, "tv"), ("tv:2", "9-1-1", 2018, "tv")])

        self.assertEqual(len(calls), 3)
        self.assertEqual(sleeps, [1.5, 1.5])

    def test_a_page_that_cannot_be_read_is_skipped_not_fatal(self):
        pages = {**self.PAGES, E_911: dp.CatalogueError("HTTP 500")}

        result, _, _ = self.match([("tv:1", "9-1-1", 2018, "tv"), ("tv:2", "Elementary", 2012, "tv")], pages)

        self.assertEqual(list(result), ["tv:2"])


class ScrapeCommandMatchingTests(CatalogueFileMixin, TestCase):
    def snapshot(self):
        titles = [{"entity": f"/e{i}", "title": f"Filler {i}", "collections": []} for i in range(120)]
        titles.append({"entity": E_911, "title": "9-1-1", "collections": ["Hulu"]})
        return {"scraped_at": "2026-11-07T10:00:00+00:00", "region": "za", "pages_read": 5, "titles": titles}

    def track(self):
        user = get_user_model().objects.create_user(username="u")
        show = Item.objects.create(media_id="75219", source=Sources.TMDB.value, media_type=MediaTypes.TV.value, title="9-1-1", image="http://x/i.jpg")
        TV.objects.create(item=show, user=user, status=Status.PLANNING.value)
        film = Item.objects.create(media_id="5", source=Sources.TMDB.value, media_type=MediaTypes.MOVIE.value, title="Filler 3", image="http://x/i.jpg")
        Movie.objects.create(item=film, user=user, status=Status.PLANNING.value)
        Item.objects.create(media_id="9", source=Sources.TMDB.value, media_type=MediaTypes.TV.value, title="9-1-1", image="http://x/i.jpg", season_number=None)  # untracked duplicate title, other id

    METADATA = {
        "details": {"status": "Returning Series", "seasons": 10},
        "networks": ["ABC"],
        "next_episode_air_date": None,
        "providers": {"ZA": {}},
    }

    def run_command(self, *args, snapshot=None, entity=None):
        """Run the command offline. The same catalogue file is reused between calls."""
        if not hasattr(self, "path"):
            self.use_catalogue(None)
        details = {E_911: {"seasons": 10, "year": 2018}, "/e3": {"seasons": None, "year": 2020}}
        entity = entity or (lambda e, **k: {**details[e], "related": []})
        with patch.object(dp, "fetch_catalogue", return_value=snapshot or self.snapshot()), \
             patch.object(dp, "entity_details", side_effect=entity), \
             patch("app.models.providers.services.get_media_metadata", return_value=self.METADATA), \
             patch.object(command, "release_year", side_effect=lambda item: 2018 if item.media_type == "tv" else 2020):
            call_command("scrape_disneyplus_za", *args, output=str(self.path), delay=0, stdout=open(os.devnull, "w"))
        return json.loads(self.path.read_text(encoding="utf-8"))

    def test_saves_matches_for_the_shows_and_films_that_are_tracked(self):
        self.track()

        saved = self.run_command()

        self.assertEqual(sorted(saved["matches"]), ["movie:5", "tv:75219"])
        self.assertEqual(saved["matches"]["tv:75219"]["entity"], E_911)

    def test_only_tracked_titles_are_matched(self):
        saved = self.run_command()  # nothing tracked

        self.assertEqual(saved["matches"], {})
        self.assertGreater(len(saved["titles"]), 100)

    def test_no_match_option_saves_the_catalogue_only(self):
        self.track()

        saved = self.run_command("--no-match")

        self.assertEqual(saved["matches"], {})

    def test_the_saved_file_feeds_the_provider_data(self):
        self.track()
        self.run_command()
        extras._cache.update(path=None, mtime=None, matches={})  # noqa: SLF001

        merged = extras.add_extra_providers({}, "tv", 75219)

        self.assertEqual(merged["ZA"]["flatrate"][0]["provider_name"], "Disney Plus")

    def test_a_new_match_clears_the_stale_cached_copy_and_refreshes_the_stored_details(self):
        self.track()
        cache.set("tmdb_tv_75219", {"providers": "stale: cached before the match existed"})
        cache.set("tmdb_season_75219_3", {"providers": "stale"})
        cache.set("tmdb_tv_999", {"providers": "someone else's show: must stay"})

        self.run_command()

        self.assertIsNone(cache.get("tmdb_tv_75219"))
        self.assertIsNone(cache.get("tmdb_season_75219_3"))
        self.assertIsNotNone(cache.get("tmdb_tv_999"))  # only changed titles are touched
        self.assertEqual(TVShowDetails.objects.get(item__media_id="75219").status, "Returning Series")

    def test_nothing_changed_means_nothing_is_cleared_or_fetched(self):
        self.track()
        self.run_command()
        cache.set("tmdb_tv_75219", {"providers": "fresh"})
        TVShowDetails.objects.all().delete()

        self.run_command()  # the same matches as last time

        self.assertEqual(cache.get("tmdb_tv_75219"), {"providers": "fresh"})
        self.assertFalse(TVShowDetails.objects.exists())

    def test_a_match_that_is_removed_is_cleared_too(self):
        self.track()
        self.run_command()
        cache.set("tmdb_tv_75219", {"providers": "with Disney+"})
        without = {**self.snapshot(), "titles": [t for t in self.snapshot()["titles"] if t["entity"] != E_911]}

        def gone(entity, **kwargs):
            raise dp.TitleGone("404")

        saved = self.run_command(snapshot=without, entity=gone)

        self.assertNotIn("tv:75219", saved["matches"])
        self.assertIsNone(cache.get("tmdb_tv_75219"))


E_LONE_STAR = "/en-za/browse/entity-cabb865a-8f10-4e11-8c35-d07d2aae6082"
E_BLUEY = "/en-za/browse/entity-aaaaaaaa-0000-4000-8000-000000000001"


class MatchRunnerMixin:
    """Runs match_titles against canned pages and records what was fetched."""

    def snap(self):
        return {"titles": [{"entity": E_911, "title": "9-1-1", "collections": ["Hulu"]}]}

    def run_match(self, wanted, pages, previous=None, snapshot=None):
        snapshot = snapshot if snapshot is not None else self.snap()
        calls, sleeps = [], []

        def fetch(entity):
            calls.append(entity)
            page = pages[entity]
            if isinstance(page, Exception):
                raise page
            return page

        matches = dp.match_titles(snapshot, wanted, previous=previous, fetch_details=fetch,
                                  sleep=sleeps.append, delay=1.0)
        return matches, snapshot, calls, sleeps



class MatchTitlesDiscoveryTests(MatchRunnerMixin, SimpleTestCase):
    """A show's page lists related titles; they are added to the catalogue and tried."""

    def test_a_title_missing_from_the_catalogue_is_found_through_a_related_page(self):
        pages = {
            E_911: {"seasons": 10, "year": 2018, "related": [(E_LONE_STAR, "9-1-1: Lone Star")]},
            E_LONE_STAR: {"seasons": 5, "year": 2020, "related": []},
        }
        wanted = [("tv:1", "9-1-1: Lone Star", 2020, "tv"), ("tv:2", "9-1-1", 2018, "tv")]

        matches, snapshot, calls, _ = self.run_match(wanted, pages)

        self.assertEqual(sorted(matches), ["tv:1", "tv:2"])
        self.assertEqual(matches["tv:1"]["entity"], E_LONE_STAR)
        self.assertIn(E_LONE_STAR, [t["entity"] for t in snapshot["titles"]])
        self.assertEqual(calls, [E_911, E_LONE_STAR])  # each page read once

    def test_nothing_is_found_when_no_page_leads_to_it(self):
        pages = {E_911: {"seasons": 10, "year": 2018, "related": []}}

        matches, _, calls, _ = self.run_match([("tv:9", "Bluey", 2018, "tv")], pages)

        self.assertEqual((matches, calls), ({}, []))

    def test_it_stops_when_nothing_new_turns_up(self):
        # two pages that only link to each other must not loop forever
        a, b = E_911, E_LONE_STAR
        pages = {
            a: {"seasons": 1, "year": 2018, "related": [(b, "B")]},
            b: {"seasons": 1, "year": 2020, "related": [(a, "9-1-1")]},
        }

        _, _, calls, _ = self.run_match([("tv:1", "9-1-1", 2018, "tv"), ("tv:2", "Nope", 1999, "tv")], pages)

        self.assertEqual(sorted(calls), sorted([a]))  # only the name that matched was read

    def test_the_year_is_only_looked_up_when_a_match_has_to_be_checked(self):
        asked = []
        pages = {E_911: {"seasons": 10, "year": 2018, "related": []}}
        wanted = [("tv:1", "9-1-1", lambda: asked.append("9-1-1") or 2018, "tv"),
                  ("tv:2", "House", lambda: asked.append("House") or 2004, "tv")]

        matches, *_ = self.run_match(wanted, pages)

        self.assertEqual(list(matches), ["tv:1"])
        self.assertEqual(asked, ["9-1-1"])  # House isn't on the service: no lookup

    def test_a_page_read_twice_is_fetched_once_and_costs_one_pause(self):
        pages = {E_911: {"seasons": 10, "year": 2018, "related": []}}
        wanted = [("tv:1", "9-1-1", 2018, "tv"), ("movie:1", "9-1-1", 2018, "movie")]

        _, _, calls, sleeps = self.run_match(wanted, pages)

        self.assertEqual(calls, [E_911])
        self.assertEqual(sleeps, [])


class StickyMatchesTests(MatchRunnerMixin, SimpleTestCase):
    """The public pages rotate, so a missing title isn't proof it left the service."""

    OLD = {"tv:5": {"entity": E_BLUEY, "title": "Bluey"}}
    WANT = [("tv:5", "Bluey", 2018, "tv")]  # tracked, but no longer on any public page

    def test_a_previous_match_is_kept_while_its_own_page_still_matches(self):
        pages = {E_BLUEY: {"seasons": 3, "year": 2018, "related": []}}

        matches, *_ = self.run_match(self.WANT, pages, previous=self.OLD)

        self.assertEqual(matches, self.OLD)

    def test_it_is_dropped_when_its_page_is_gone(self):
        pages = {E_BLUEY: dp.TitleGone("404")}

        matches, *_ = self.run_match(self.WANT, pages, previous=self.OLD)

        self.assertEqual(matches, {})

    def test_it_is_kept_when_the_page_cannot_be_read_right_now(self):
        pages = {E_BLUEY: dp.CatalogueError("HTTP 503")}

        matches, *_ = self.run_match(self.WANT, pages, previous=self.OLD)

        self.assertEqual(matches, self.OLD)  # a network hiccup must not remove Disney+

    def test_it_is_dropped_when_the_page_is_now_a_different_kind_of_title(self):
        pages = {E_BLUEY: {"seasons": None, "year": 2018, "related": []}}  # now a film

        matches, *_ = self.run_match(self.WANT, pages, previous=self.OLD)

        self.assertEqual(matches, {})

    def test_it_is_dropped_when_the_title_is_no_longer_tracked(self):
        matches, _, calls, _ = self.run_match([], {}, previous=self.OLD)

        self.assertEqual((matches, calls), ({}, []))  # nothing to check, nothing fetched

    def test_a_fresh_match_replaces_the_old_one_without_re_checking_it(self):
        snapshot = {"titles": [{"entity": "/new", "title": "Bluey", "collections": []}]}
        pages = {"/new": {"seasons": 3, "year": 2018, "related": []}}

        matches, _, calls, _ = self.run_match(self.WANT, pages, previous=self.OLD, snapshot=snapshot)

        self.assertEqual(matches["tv:5"]["entity"], "/new")
        self.assertEqual(calls, ["/new"])


class NoMatchKeepsMatchesTests(CatalogueFileMixin, TestCase):
    def test_no_match_run_keeps_what_was_matched_before(self):
        self.use_catalogue(None)
        old = {"tv:5": {"entity": E_BLUEY, "title": "Bluey"}}
        dp.save_snapshot({"titles": [{"entity": f"/e{i}", "title": f"T{i}", "collections": []} for i in range(150)],
                          "matches": old}, self.path)
        fresh = {"scraped_at": "x", "region": "za", "pages_read": 3,
                 "titles": [{"entity": f"/e{i}", "title": f"T{i}", "collections": []} for i in range(160)]}

        with patch.object(dp, "fetch_catalogue", return_value=fresh):
            call_command("scrape_disneyplus_za", output=str(self.path), no_match=True, delay=0,
                         stdout=open(os.devnull, "w"))

        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["matches"], old)  # catalogue refreshed, matches not wiped
        self.assertEqual(len(saved["titles"]), 160)

    def test_a_brand_new_install_with_no_match_saves_an_empty_set(self):
        self.use_catalogue(None)
        fresh = {"scraped_at": "x", "region": "za", "pages_read": 3,
                 "titles": [{"entity": f"/e{i}", "title": f"T{i}", "collections": []} for i in range(150)]}

        with patch.object(dp, "fetch_catalogue", return_value=fresh):
            call_command("scrape_disneyplus_za", output=str(self.path), no_match=True, delay=0,
                         stdout=open(os.devnull, "w"))

        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["matches"], {})
