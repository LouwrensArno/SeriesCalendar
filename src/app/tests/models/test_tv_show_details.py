from datetime import date, timedelta
from unittest.mock import patch

import requests
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from app.models import (
    TV,
    BasicMedia,
    Item,
    MediaTypes,
    Sources,
    Status,
    TVShowDetails,
)
from app.providers import services
from app.tasks import refresh_tv_show_details, tracked_tv_items

METADATA_PATH = "app.models.providers.services.get_media_metadata"


def metadata(status="Returning Series", networks=None, seasons=3, next_air=None):
    """Return the parts of a TMDB TV metadata dict that the details are read from."""
    return {
        "details": {"status": status, "seasons": seasons},
        "networks": ["ABC", "FOX"] if networks is None else networks,
        "next_episode_air_date": next_air,
    }


class TVShowDetailsRefreshTests(TestCase):
    """Test fetching and storing TMDB's show details."""

    def setUp(self):
        """Create a tracked TMDB show."""
        self.user = get_user_model().objects.create_user(username="test")
        self.item = Item.objects.create(
            media_id="75219",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="9-1-1",
            image="http://example.com/image.jpg",
        )

    @patch(METADATA_PATH)
    def test_stores_what_tmdb_reports(self, get_metadata):
        """Status, networks, seasons and next air date are stored on the show."""
        get_metadata.return_value = metadata(next_air="2026-11-12")

        refreshed = TVShowDetails.objects.refresh([self.item])

        self.assertEqual(refreshed, 1)
        details = TVShowDetails.objects.get(item=self.item)
        self.assertEqual(details.status, "Returning Series")
        self.assertEqual(details.networks, "ABC, FOX")
        self.assertEqual(details.season_count, 3)
        self.assertEqual(details.next_air_date, date(2026, 11, 12))
        get_metadata.assert_called_once_with(MediaTypes.TV.value, "75219", "tmdb")

    @patch(METADATA_PATH)
    def test_show_without_next_episode_or_network(self, get_metadata):
        """Missing values are stored as empty, not as the string 'None'."""
        get_metadata.return_value = metadata(status="Ended", networks=[], next_air=None)

        TVShowDetails.objects.refresh([self.item])

        details = TVShowDetails.objects.get(item=self.item)
        self.assertEqual(details.networks, "")
        self.assertIsNone(details.next_air_date)

    @patch(METADATA_PATH)
    def test_only_tmdb_tv_shows_are_looked_up(self, get_metadata):
        """Movies and shows from other sources have no TMDB show details."""
        movie = Item.objects.create(
            media_id="278",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Movie",
            image="http://example.com/image.jpg",
        )
        manual = Item.objects.create(
            media_id="abc",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.TV.value,
            title="Manual show",
            image="http://example.com/image.jpg",
        )

        self.assertEqual(TVShowDetails.objects.refresh([movie, manual]), 0)
        get_metadata.assert_not_called()

    @patch(METADATA_PATH)
    def test_fresh_details_are_not_fetched_again(self, get_metadata):
        """A show refreshed recently, with no past next air date, is left alone."""
        get_metadata.return_value = metadata(next_air="2999-01-01")
        TVShowDetails.objects.refresh([self.item])
        get_metadata.reset_mock()

        self.assertEqual(TVShowDetails.objects.refresh([self.item]), 0)
        get_metadata.assert_not_called()

    @patch(METADATA_PATH)
    def test_old_details_are_refreshed(self, get_metadata):
        """Details older than max_age are fetched again."""
        get_metadata.return_value = metadata()
        TVShowDetails.objects.refresh([self.item])
        TVShowDetails.objects.update(refreshed_at=timezone.now() - timedelta(days=2))
        get_metadata.reset_mock()

        self.assertEqual(TVShowDetails.objects.refresh([self.item]), 1)
        get_metadata.assert_called_once()

    @patch(METADATA_PATH)
    def test_an_aired_next_episode_triggers_a_refresh(self, get_metadata):
        """Once the stored next episode has aired, a newer one is probably scheduled."""
        yesterday = timezone.localdate() - timedelta(days=1)
        TVShowDetails.objects.create(
            item=self.item,
            status="Returning Series",
            next_air_date=yesterday,
        )
        get_metadata.return_value = metadata(next_air="2999-01-08")

        self.assertEqual(TVShowDetails.objects.refresh([self.item]), 1)

        self.assertEqual(
            TVShowDetails.objects.get(item=self.item).next_air_date,
            date(2999, 1, 8),
        )

    @patch(METADATA_PATH)
    def test_force_refreshes_even_fresh_details(self, get_metadata):
        """The daily task forces a refresh of everything."""
        get_metadata.return_value = metadata()
        TVShowDetails.objects.refresh([self.item])

        self.assertEqual(TVShowDetails.objects.refresh([self.item], force=True), 1)

    @patch(METADATA_PATH)
    def test_a_failed_lookup_keeps_what_was_stored(self, get_metadata):
        """TMDB being unreachable never wipes or breaks the stored details."""
        TVShowDetails.objects.create(
            item=self.item,
            status="Ended",
            networks="AMC",
            providers={},
        )
        get_metadata.side_effect = requests.ConnectionError("offline")

        self.assertEqual(TVShowDetails.objects.refresh([self.item], force=True), 0)

        details = TVShowDetails.objects.get(item=self.item)
        self.assertEqual((details.status, details.networks), ("Ended", "AMC"))

    @patch(METADATA_PATH)
    def test_provider_errors_do_not_stop_the_other_shows(self, get_metadata):
        """One show failing doesn't stop the rest from being refreshed."""
        other = Item.objects.create(
            media_id="95396",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="Severance",
            image="http://example.com/image.jpg",
        )
        get_metadata.side_effect = [
            services.ProviderAPIError(
                Sources.TMDB.value,
                requests.HTTPError(response=type("R", (), {"status_code": 404})()),
            ),
            metadata(networks=["Apple TV"]),
        ]

        refreshed = TVShowDetails.objects.refresh([self.item, other])

        self.assertEqual(refreshed, 1)
        self.assertFalse(TVShowDetails.objects.filter(item=self.item).exists())
        self.assertEqual(TVShowDetails.objects.get(item=other).networks, "Apple TV")


class TVShowDetailsListTests(TestCase):
    """Test filtering and sorting the TV list by the stored show details."""

    SHOWS = (
        # title, status, networks, seasons, next air date
        ("Bravo Show", "Returning Series", "HBO", 2, date(2026, 12, 1)),
        ("Alpha Show", "Ended", "AMC", 5, None),
        ("Charlie Show", "Canceled", "ABC, FOX", 1, None),
        ("Delta Show", "In Production", "", None, date(2026, 11, 20)),
        ("Echo Show", "Returning Series", "Apple TV", 10, date(2026, 11, 15)),
    )

    def setUp(self):
        """Track a handful of shows with different details."""
        self.user = get_user_model().objects.create_user(username="test")
        for number, (title, status, networks, seasons, next_air) in enumerate(
            self.SHOWS,
            start=1,
        ):
            item = Item.objects.create(
                media_id=str(number),
                source=Sources.TMDB.value,
                media_type=MediaTypes.TV.value,
                title=title,
                image="http://example.com/image.jpg",
            )
            TV.objects.create(item=item, user=self.user, status=Status.PLANNING.value)
            TVShowDetails.objects.create(
                item=item,
                status=status,
                networks=networks,
                season_count=seasons,
                next_air_date=next_air,
                providers={},
            )
        # tracked, but TMDB details were never fetched
        bare = Item.objects.create(
            media_id="99",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="Zulu Show",
            image="http://example.com/image.jpg",
        )
        TV.objects.create(item=bare, user=self.user, status=Status.PLANNING.value)

    def titles(self, sort="title", show_status=None):
        """Return the titles of the user's TV list in order."""
        queryset = BasicMedia.objects.get_media_list(
            user=self.user,
            media_type=MediaTypes.TV.value,
            status_filter="All",
            sort_filter=sort,
            show_status=show_status,
        )
        return [tv.item.title for tv in queryset]

    def test_current_is_airing_or_coming_and_concluded_is_ended_or_canceled(self):
        """With no released episodes to watch, the show's TMDB status decides."""
        self.assertEqual(
            self.titles(show_status="current"),
            # Returning, In Production, Returning, and Zulu: no status known yet
            ["Bravo Show", "Delta Show", "Echo Show", "Zulu Show"],
        )
        self.assertEqual(
            self.titles(show_status="concluded"),
            ["Alpha Show", "Charlie Show"],  # Ended, Canceled
        )

    def test_a_show_without_details_is_current_so_it_is_shown_and_gets_fetched(self):
        """No status yet: Current (never Concluded), so the default list keeps it."""
        self.assertIn("Zulu Show", self.titles(show_status="current"))
        self.assertNotIn("Zulu Show", self.titles(show_status="concluded"))

    def test_all_shows_everything_including_shows_without_details(self):
        """'All' (or no filter) is no filtering: shows TMDB hasn't been asked about stay."""
        everything = [
            "Alpha Show",
            "Bravo Show",
            "Charlie Show",
            "Delta Show",
            "Echo Show",
            "Zulu Show",
        ]
        self.assertEqual(self.titles(show_status="All"), everything)
        self.assertEqual(self.titles(show_status=None), everything)

    def test_sort_by_network_puts_blank_networks_last(self):
        """Networks sort alphabetically, ignoring case; no network goes last."""
        self.assertEqual(
            self.titles(sort="network"),
            [
                "Charlie Show",  # ABC, FOX
                "Alpha Show",  # AMC
                "Echo Show",  # Apple TV
                "Bravo Show",  # HBO
                "Delta Show",  # (blank)
                "Zulu Show",  # (no details)
            ],
        )

    def test_sort_by_next_episode_soonest_first_and_none_last(self):
        """The show airing soonest comes first; shows with no next episode last."""
        self.assertEqual(
            self.titles(sort="next_episode"),
            [
                "Echo Show",  # 15 Nov
                "Delta Show",  # 20 Nov
                "Bravo Show",  # 1 Dec
                "Alpha Show",  # nothing scheduled, then alphabetical
                "Charlie Show",
                "Zulu Show",  # no details at all
            ],
        )

    def test_sort_by_seasons_most_first_and_unknown_last(self):
        """Most seasons first; shows with an unknown count last."""
        self.assertEqual(
            self.titles(sort="seasons"),
            [
                "Echo Show",  # 10
                "Alpha Show",  # 5
                "Bravo Show",  # 2
                "Charlie Show",  # 1
                "Delta Show",  # unknown
                "Zulu Show",  # no details
            ],
        )

    def test_sort_by_title_is_case_insensitive(self):
        """Title sorting ignores case."""
        self.assertEqual(self.titles(sort="title")[0], "Alpha Show")
        self.assertEqual(self.titles(sort="title")[-1], "Zulu Show")


class TVShowDetailsTaskTests(TestCase):
    """Test the daily refresh task."""

    def setUp(self):
        """Create a tracked show and one nobody tracks."""
        self.user = get_user_model().objects.create_user(username="test")
        self.tracked = Item.objects.create(
            media_id="75219",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="Tracked",
            image="http://example.com/image.jpg",
        )
        TV.objects.create(item=self.tracked, user=self.user, status=Status.PLANNING.value)
        Item.objects.create(
            media_id="1",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="Nobody tracks this",
            image="http://example.com/image.jpg",
        )

    def test_only_tracked_shows_are_refreshed(self):
        """Shows that no user tracks are not looked up."""
        self.assertEqual(list(tracked_tv_items()), [self.tracked])

    @patch(METADATA_PATH)
    def test_task_refreshes_everything_tracked_even_if_fresh(self, get_metadata):
        """The task forces a refresh of every tracked show."""
        get_metadata.return_value = metadata()

        self.assertEqual(refresh_tv_show_details(), 1)
        self.assertEqual(refresh_tv_show_details(), 1)

        self.assertEqual(TVShowDetails.objects.count(), 1)
