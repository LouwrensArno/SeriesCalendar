import io
import json
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import requests
from django.core.management import CommandError, call_command
from django.test import SimpleTestCase

from app.providers import disneyplus_za as dp

E1 = "/en-za/browse/entity-b3519825-4053-4065-b3a6-e93125fb7d54"
E2 = "/en-za/browse/entity-cabb865a-8f10-4e11-8c35-d07d2aae6082"
E3 = "/en-za/browse/entity-05eb6a8e-90ed-4947-8c0b-e6536cbddd5f"
HUB_HULU = "/en-za/browse/page-ff723d29-20d5-4303-9cce-4a9aac8e269e"
HUB_DRAMA = "/en-za/browse/page-97e41a96-dcbc-4fa1-9703-fe06faa6740d"


def tile(title, entity):
    """Markup of one title tile, as the real page renders it."""
    return (
        f'<li data-testid="collection-tile-0"><div class="block">'
        f'<a aria-label="{title}" href="{entity}" rel="noreferrer" class="x">'
        f"<picture></picture></a></div></li>"
    )


def page(name, tiles=(), hubs=(), region="za"):
    """A browse page: title, tiles, links to other hubs and the embedded region."""
    links = "".join(f'<a href="{h}">hub</a>' for h in hubs)
    return (
        f"<html><head><title>{name} | Disney+</title></head><body>"
        f'<script id="__NEXT_DATA__" type="application/json">{{"pageProps":{{"region":"{region}"}}}}</script>'
        f"{links}{''.join(tile(t, e) for t, e in tiles)}</body></html>"
    )


class FakeResponse:
    def __init__(self, text, status=200, url=""):
        self.text, self.status_code, self.url = text, status, url


class FakeSession:
    """Serves canned pages by path and records every request."""

    def __init__(self, pages, redirects=None):
        self.pages = pages
        self.redirects = redirects or {}
        self.requests = []

    def get(self, url, headers=None, timeout=None, allow_redirects=True):
        path = url.replace(dp.BASE, "")
        self.requests.append((path, headers))
        path = self.redirects.get(path, path)
        if path in self.pages:
            body = self.pages[path]
            if isinstance(body, Exception):
                raise body
            return FakeResponse(body, 200, dp.BASE + path)
        return FakeResponse("not found", 404, dp.BASE + path)


class ParsePageTests(SimpleTestCase):
    def test_reads_title_tiles_hubs_and_region(self):
        html = page(
            "Hulu movies and shows",
            tiles=[("9-1-1", E1), ("9-1-1: Nashville", E2)],
            hubs=[HUB_DRAMA, "/en-za/browse/hulu"],
        )

        name, tiles, hubs, region = dp.parse_page(html)

        self.assertEqual(name, "Hulu")
        self.assertEqual(tiles, [(E1, "9-1-1"), (E2, "9-1-1: Nashville")])
        self.assertEqual(hubs, [HUB_DRAMA, "/en-za/browse/hulu"])
        self.assertEqual(region, "za")

    def test_unescapes_html_in_titles(self):
        name, tiles, _, _ = dp.parse_page(page("Action &amp; Adventure movies and shows", [("Bob&#x27;s Burgers &amp; Co", E1)]))

        self.assertEqual(name, "Action & Adventure")
        self.assertEqual(tiles, [(E1, "Bob's Burgers & Co")])

    def test_a_title_shown_twice_on_one_page_is_listed_once(self):
        _, tiles, _, _ = dp.parse_page(page("X", [("The Bear", E3), ("The Bear", E3)]))

        self.assertEqual(tiles, [(E3, "The Bear")])

    def test_title_pages_are_not_treated_as_hubs(self):
        html = page("X", hubs=[E1, HUB_DRAMA, "/en-za/legal/terms", "/en-us/browse/page-123"])

        _, _, hubs, _ = dp.parse_page(html)

        self.assertEqual(hubs, [HUB_DRAMA])

    def test_page_without_region_or_tiles(self):
        name, tiles, hubs, region = dp.parse_page("<html><title>Oops</title></html>")

        self.assertEqual((tiles, hubs, region), ([], [], None))
        self.assertEqual(name, "Oops")


# The markers real pages carry (trimmed from live pages read on 2026-11-08).
SERIES_PAGE = (
    '{"hasContent":true,"stitchDocument":{"mainContent":[{"_type":"DetailEntityHero","releaseYear":"2021 – 2023",'
    '"seasonsAvailable":"3 Seasons"},{"_type":"Episodes","title":"EPISODES"},'
    '{"_type":"Metadata","ldJSON":{"@context":"http://schema.org","@graph":[{"@type":"TVSeries","datePublished":"2021"},'
    '{"@type":"BreadcrumbList","itemListElement":[{"@type":"ListItem","item":{"@type":"WebPage"}}]}]}}]}}'
    "<span>3 Seasons</span>"
)
MOVIE_PAGE = (
    '{"hasContent":true,"stitchDocument":{"mainContent":[{"_type":"DetailEntityHero","releaseYear":"2026"},'
    '{"_type":"Metadata","ldJSON":{"@graph":[{"@type":"Movie"},{"@type":"BreadcrumbList"}]}}]}}'
)
TEAM_PAGE = (
    '{"hasContent":true,"stitchDocument":{"mainContent":[{"_type":"Metadata","ldJSON":{"@graph":[{"@type":"ItemList"}]}}]}}'
)
EMPTY_PAGE = '{"hasContent":false,"stitchDocument":{"mainContent":[]},"region":"za"}'


class EntityPageTests(SimpleTestCase):
    def test_reads_year_and_seasons(self):
        html = '<span>• 10 Seasons</span><script>{"releaseYear":"2018"}</script>'
        details = dp.parse_entity(html)

        self.assertEqual((details["year"], details["seasons"], details["related"]), (2018, 10, []))

    def test_a_series_page_the_service_really_carries(self):
        details = dp.parse_entity(SERIES_PAGE)

        self.assertEqual(
            (details["has_content"], details["kind"], details["has_episodes"], details["seasons"], details["year"]),
            (True, "TVSeries", True, 3, 2021),
        )
        self.assertTrue(dp.is_playable(details))

    def test_a_film_page_has_no_episodes_and_is_still_playable(self):
        details = dp.parse_entity(MOVIE_PAGE)

        self.assertEqual((details["kind"], details["has_episodes"], details["seasons"]), ("Movie", False, None))
        self.assertTrue(dp.is_playable(details))

    def test_the_empty_page_of_a_title_not_carried_here_is_not_playable(self):
        details = dp.parse_entity(EMPTY_PAGE)

        self.assertEqual((details["has_content"], details["kind"]), (False, None))
        self.assertFalse(dp.is_playable(details))

    def test_a_page_that_is_not_a_show_or_film_is_not_playable(self):
        self.assertFalse(dp.is_playable(dp.parse_entity(TEAM_PAGE)))

    def test_a_series_page_without_episodes_is_not_playable(self):
        page = SERIES_PAGE.replace('"_type":"Episodes"', '"_type":"Nothing"')

        self.assertFalse(dp.is_playable(dp.parse_entity(page)))

    def test_what_a_page_does_not_say_does_not_count_against_it(self):
        self.assertTrue(dp.is_playable({"year": 2018, "seasons": 10, "related": []}))
        self.assertTrue(dp.is_playable(dp.parse_entity("nothing")))

    def test_collects_the_related_titles_the_page_links_to(self):
        html = page("9-1-1", tiles=[("9-1-1: Lone Star", E2), ("Grey's Anatomy", E3)]) + "10 Seasons"

        self.assertEqual(
            dp.parse_entity(html)["related"],
            [(E2, "9-1-1: Lone Star"), (E3, "Grey's Anatomy")],
        )

    def test_one_season_and_numeric_year(self):
        details = dp.parse_entity('1 Season {"year":2024}')

        self.assertEqual((details["year"], details["seasons"]), (2024, 1))

    def test_missing_fields_are_none(self):
        self.assertEqual(
            dp.parse_entity("nothing"),
            {"year": None, "seasons": None, "has_content": None, "kind": None, "has_episodes": False, "related": []},
        )


class NormalizeTitleTests(SimpleTestCase):
    def test_ignores_punctuation_case_and_accents(self):
        for a, b in (
            ("9-1-1", "911"),
            ("Shōgun", "Shogun"),
            ("Grey's Anatomy", "greys anatomy"),
            ("Pokémon", "POKEMON"),
            ("S.W.A.T.", "SWAT"),
        ):
            self.assertEqual(dp.normalize_title(a), dp.normalize_title(b), (a, b))

    def test_different_titles_stay_different(self):
        self.assertNotEqual(dp.normalize_title("9-1-1"), dp.normalize_title("9-1-1: Nashville"))
        self.assertNotEqual(dp.normalize_title("House"), dp.normalize_title("House of Cards"))


class FetchCatalogueTests(SimpleTestCase):
    def crawl(self, pages, **kwargs):
        session = FakeSession(pages, kwargs.pop("redirects", None))
        sleeps = []
        result = dp.fetch_catalogue(session=session, sleep=sleeps.append, **kwargs)
        return result, session, sleeps

    def pages(self):
        return {
            "/en-za": page("Disney+ South Africa", hubs=[HUB_HULU, HUB_DRAMA]),
            HUB_HULU: page("Hulu movies and shows", [("9-1-1", E1), ("The Bear", E3)], hubs=[HUB_DRAMA]),
            HUB_DRAMA: page("Drama movies and shows", [("9-1-1", E1), ("9-1-1: Nashville", E2)], hubs=[HUB_HULU]),
        }

    def test_follows_hubs_and_collects_each_title_once_with_its_collections(self):
        result, session, _ = self.crawl(self.pages())

        self.assertEqual(result["region"], "za")
        self.assertEqual(result["pages_read"], 3)
        by_title = {t["title"]: t for t in result["titles"]}
        self.assertEqual(sorted(by_title), ["9-1-1", "9-1-1: Nashville", "The Bear"])
        self.assertEqual(sorted(by_title["9-1-1"]["collections"]), ["Drama", "Hulu"])
        self.assertEqual(by_title["The Bear"]["collections"], ["Hulu"])
        # every page is requested exactly once, even though hubs link to each other
        self.assertEqual(sorted(p for p, _ in session.requests), sorted(self.pages()))

    def test_waits_between_requests_but_not_before_the_first(self):
        _, session, sleeps = self.crawl(self.pages(), delay=2.5)

        self.assertEqual(len(session.requests), 3)
        self.assertEqual(sleeps, [2.5, 2.5])

    def test_never_reads_more_pages_than_the_cap(self):
        result, session, _ = self.crawl(self.pages(), max_pages=2)

        self.assertEqual(result["pages_read"], 2)
        self.assertEqual(len(session.requests), 2)

    def test_identifies_itself_honestly_and_sends_no_cookies_or_login(self):
        _, session, _ = self.crawl(self.pages())

        for _, headers in session.requests:
            self.assertIn("Yamtrack", headers["User-Agent"])
            self.assertNotIn("Cookie", headers)
            self.assertNotIn("Authorization", headers)

    def test_a_missing_hub_is_skipped(self):
        pages = self.pages()
        del pages[HUB_DRAMA]

        result, _, _ = self.crawl(pages)

        self.assertEqual(result["pages_read"], 2)
        self.assertEqual(sorted(t["title"] for t in result["titles"]), ["9-1-1", "The Bear"])

    def test_a_network_error_on_a_hub_is_skipped(self):
        pages = self.pages()
        pages[HUB_DRAMA] = requests.ConnectionError("reset")

        result, _, _ = self.crawl(pages)

        self.assertEqual(result["pages_read"], 2)

    def test_a_hub_reached_by_two_names_is_read_once(self):
        pages = self.pages()
        pages["/en-za"] = page("Landing", hubs=["/en-za/browse/hulu", HUB_HULU])

        result, _, _ = self.crawl(pages, redirects={"/en-za/browse/hulu": HUB_HULU})

        # landing, Hulu (once, though linked under two names) and the Drama hub it links to
        self.assertEqual(result["pages_read"], 3)
        by_title = {t["title"]: t for t in result["titles"]}
        self.assertEqual(by_title["The Bear"]["collections"], ["Hulu"])

    def test_wrong_region_is_refused(self):
        pages = self.pages()
        pages[HUB_HULU] = page("Hulu", [("9-1-1", E1)], region="us")

        with self.assertRaisesRegex(dp.CatalogueError, "region 'us'"):
            self.crawl(pages)

    def test_unreachable_landing_page_is_an_error(self):
        with self.assertRaises(dp.CatalogueError):
            self.crawl({})  # the landing page 404s
        with self.assertRaises(dp.CatalogueError):
            self.crawl({"/en-za": requests.ConnectionError("offline")})


class SnapshotTests(SimpleTestCase):
    def snap(self, count):
        return {"titles": [{"entity": f"/e{i}", "title": f"T{i}", "collections": []} for i in range(count)]}

    def test_too_few_titles_is_refused(self):
        self.assertIn("only 5 titles", dp.check_snapshot(self.snap(5), None))

    def test_a_big_drop_from_the_previous_catalogue_is_refused(self):
        self.assertIn("down from 1000", dp.check_snapshot(self.snap(300), self.snap(1000)))

    def test_a_normal_catalogue_passes(self):
        self.assertIsNone(dp.check_snapshot(self.snap(900), self.snap(1000)))
        self.assertIsNone(dp.check_snapshot(self.snap(200), None))

    def test_save_and_load_round_trip_without_leaving_a_temp_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "db" / "disneyplus_za.json"
            snap = {"region": "za", "titles": [{"entity": "/e", "title": "Shōgun", "collections": []}]}

            dp.save_snapshot(snap, path)

            self.assertEqual(dp.load_snapshot(path), snap)
            self.assertEqual([p.name for p in path.parent.iterdir()], ["disneyplus_za.json"])
            self.assertIn("Shōgun", path.read_text(encoding="utf-8"))  # not \u escaped

    def test_loading_a_missing_or_damaged_file_gives_none(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertIsNone(dp.load_snapshot(Path(folder) / "nope.json"))
            bad = Path(folder) / "bad.json"
            bad.write_text("{not json")
            self.assertIsNone(dp.load_snapshot(bad))

    def test_find_show_matches_by_normalised_title(self):
        snap = {"titles": [
            {"entity": E1, "title": "9-1-1", "collections": []},
            {"entity": E2, "title": "9-1-1: Nashville", "collections": []},
            {"entity": "/s", "title": "Shōgun", "collections": []},
        ]}
        index = dp.index_by_title(snap)

        self.assertEqual([e["entity"] for e in dp.find_show(index, "9-1-1")], [E1])
        self.assertEqual([e["entity"] for e in dp.find_show(index, "Shogun")], ["/s"])
        self.assertEqual(dp.find_show(index, "House"), [])
        self.assertEqual(dp.find_show(dp.index_by_title(None), "9-1-1"), [])


class EntityDetailsTests(SimpleTestCase):
    def test_fetches_one_title_page(self):
        session = FakeSession({E1: '10 Seasons {"releaseYear":"2018"}'})

        details = dp.entity_details(E1, session=session)

        self.assertEqual((details["year"], details["seasons"], details["related"]), (2018, 10, []))
        self.assertEqual(len(session.requests), 1)

    def test_a_page_that_is_gone_is_told_apart_from_an_error(self):
        with self.assertRaises(dp.TitleGone):
            dp.entity_details(E1, session=FakeSession({}))  # 404

    def test_any_other_failure_is_a_plain_error(self):
        class Failing(FakeSession):
            def get(self, url, **kwargs):
                return FakeResponse("boom", 500, url)

        with self.assertRaises(dp.CatalogueError) as caught:
            dp.entity_details(E1, session=Failing({}))
        self.assertNotIsInstance(caught.exception, dp.TitleGone)


class ScrapeCommandTests(SimpleTestCase):
    def run_command(self, snapshot, path, previous=None):
        if previous is not None:
            dp.save_snapshot(previous, path)
        out = io.StringIO()
        with patch.object(dp, "fetch_catalogue", return_value=snapshot), redirect_stdout(out):
            call_command("scrape_disneyplus_za", output=str(path), no_match=True, stdout=out)
        return out.getvalue()

    def snap(self, count):
        return {
            "scraped_at": "2026-11-07T10:00:00+00:00", "region": "za", "pages_read": 7,
            "titles": [{"entity": f"/e{i}", "title": f"T{i}", "collections": []} for i in range(count)],
        }

    def test_saves_the_catalogue(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "c.json"

            output = self.run_command(self.snap(150), path)

            self.assertIn("Saved 150 Disney+ ZA titles from 7 pages", output)
            self.assertEqual(len(dp.load_snapshot(path)["titles"]), 150)

    def test_a_broken_catalogue_never_replaces_a_good_one(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "c.json"
            with self.assertRaisesRegex(CommandError, "Not saved"):
                self.run_command(self.snap(120), path, previous=self.snap(900))

            self.assertEqual(len(dp.load_snapshot(path)["titles"]), 900)  # untouched

    def test_a_scrape_failure_is_reported_as_a_command_error(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(
            dp, "fetch_catalogue", side_effect=dp.CatalogueError("wrong region"),
        ), self.assertRaisesRegex(CommandError, "wrong region"):
            call_command("scrape_disneyplus_za", output=str(Path(folder) / "c.json"))

    def test_default_location_is_next_to_the_database(self):
        from app.management.commands.scrape_disneyplus_za import default_path

        self.assertEqual(default_path().parts[-2:], ("db", "disneyplus_za.json"))
