from datetime import date
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from app.models import (
    TV,
    Item,
    MediaTypes,
    Sources,
    Status,
    TVShowDetails,
)
from users.models import TVSortChoices, TVStatusChoices


class TVListViewTests(TestCase):
    """Test the TV list page: filtering and sorting by TMDB's show details."""

    def setUp(self):
        """Track a few shows with stored TMDB details and log in."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)
        self.url = reverse("medialist", args=[self.user.username, MediaTypes.TV.value])

        for number, (title, status, networks, seasons, next_air) in enumerate(
            (
                ("Alpha Show", "Ended", "AMC", 5, None),
                ("Bravo Show", "Returning Series", "HBO", 2, date(2026, 12, 1)),
                ("Charlie Show", "Returning Series", "ABC, FOX", 10, None),
            ),
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

    def shown(self, **params):
        """Return the titles on the page, in order."""
        response = self.client.get(self.url, {"layout": "table", "status": "All", **params})
        self.assertEqual(response.status_code, 200)
        return [tv.item.title for tv in response.context["media_list"]]

    def test_the_list_starts_on_current(self):
        """With nothing chosen yet, the list shows Current: the ended show is hidden."""
        response = self.client.get(self.url, {"layout": "table"})

        self.assertEqual(response.context["current_status"], "current")
        self.assertEqual(
            [tv.item.title for tv in response.context["media_list"]],
            ["Bravo Show", "Charlie Show"],
        )

    def test_choosing_all_is_remembered(self):
        """Switching to All shows everything, and the next visit stays on All."""
        self.client.get(self.url, {"layout": "table", "status": "All"})

        response = self.client.get(self.url, {"layout": "table"})

        self.assertEqual(response.context["current_status"], "All")
        self.assertEqual(len(response.context["media_list"]), 3)

    def test_offers_filters_and_sorts_that_match_the_columns(self):
        """The dropdowns list the show statuses and the table's columns."""
        response = self.client.get(self.url, {"layout": "table"})

        self.assertEqual(
            [label for _, label in response.context["status_choices"]],
            ["All", "Current", "Concluded"],
        )
        self.assertEqual(
            [label for _, label in response.context["sort_choices"]],
            ["Title", "Network", "Next Episode", "Seasons", "Episodes"],
        )

    def test_other_media_types_keep_their_filters(self):
        """The movies page still offers tracking statuses and rating."""
        url = reverse("medialist", args=[self.user.username, MediaTypes.MOVIE.value])

        response = self.client.get(url)

        status_labels = [label for _, label in response.context["status_choices"]]
        sort_labels = [label for _, label in response.context["sort_choices"]]
        self.assertIn("Completed", status_labels)
        self.assertNotIn("Current", status_labels)
        self.assertIn("Rating", sort_labels)
        self.assertNotIn("Network", sort_labels)

    def test_filter_current_and_concluded(self):
        """Current keeps the running shows; Concluded the ended one (nothing left to watch)."""
        self.assertEqual(
            self.shown(status="current", sort="title"),
            ["Bravo Show", "Charlie Show"],
        )
        self.assertEqual(self.shown(status="concluded", sort="title"), ["Alpha Show"])
        self.assertEqual(
            self.shown(status="All", sort="title"),
            ["Alpha Show", "Bravo Show", "Charlie Show"],
        )

    def test_sorting(self):
        """Sorting follows the columns."""
        self.assertEqual(
            self.shown(sort="network"),
            ["Charlie Show", "Alpha Show", "Bravo Show"],
        )
        self.assertEqual(
            self.shown(sort="seasons"),
            ["Charlie Show", "Alpha Show", "Bravo Show"],
        )
        self.assertEqual(
            self.shown(sort="next_episode"),
            ["Bravo Show", "Alpha Show", "Charlie Show"],
        )

    def test_choices_are_remembered_between_visits(self):
        """The chosen sort and filter are saved on the user, like the other lists."""
        self.shown(sort="network", status="current")

        self.user.refresh_from_db()
        self.assertEqual(self.user.tv_sort, "network")
        self.assertEqual(self.user.tv_status, "current")

    def test_old_filter_and_sort_values_are_ignored(self):
        """A bookmarked URL with the old options still loads, using the current ones."""
        # an unknown filter changes nothing: the list stays on the user's saved one (Current)
        current = ["Bravo Show", "Charlie Show"]
        for old in ("Completed", "airing", "ended", "canceled", "coming"):
            self.assertEqual(self.shown(status=old, sort="title"), current, old)
        # an unknown sort changes nothing either: it stays on Title
        self.assertEqual(
            self.shown(status="All", sort="score"),
            ["Alpha Show", "Bravo Show", "Charlie Show"],
        )

    def test_table_shows_the_stored_details_and_not_the_old_columns(self):
        """The table renders network, status and seasons; the old columns are gone."""
        response = self.client.get(self.url, {"layout": "table", "sort": "title", "status": "All"})

        content = response.content.decode()
        for expected in ("AMC", "ABC, FOX", "Airing", "Ended", ">Network<", ">Episodes<"):
            self.assertIn(expected, content)
        self.assertIn("2026-12-01", content)
        for gone in (">Score<", ">Start Date<", ">End Date<", ">Last Watched<"):
            self.assertNotIn(gone, content)

    def test_shows_without_details_are_fetched_when_the_page_loads(self):
        """A show TMDB hasn't been asked about yet gets its details on first view."""
        item = Item.objects.create(
            media_id="75219",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="Delta Show",
            image="http://example.com/image.jpg",
        )
        TV.objects.create(item=item, user=self.user, status=Status.PLANNING.value)

        with patch(
            "app.models.providers.services.get_media_metadata",
            return_value={
                "details": {"status": "Returning Series", "seasons": 3},
                "networks": ["Apple TV"],
                "next_episode_air_date": None,
            },
        ) as get_metadata:
            response = self.client.get(self.url, {"layout": "table", "sort": "title"})

        get_metadata.assert_called_once_with(MediaTypes.TV.value, "75219", "tmdb")
        self.assertContains(response, "Apple TV")
        self.assertTrue(TVShowDetails.objects.filter(item=item).exists())

    def test_choosing_a_filter_or_sort_sends_the_chosen_value_itself(self):
        """The hidden form is updated by Alpine only after the click, so reading it would send the previous
        filter and the first rows would not match the pages loaded after them (duplicated shows)."""
        response = self.client.get(self.url, {"layout": "table"})

        for value, _ in TVStatusChoices.choices:
            self.assertContains(response, f"hx-vals='{{\"status\": \"{value}\"}}'")
        for value, _ in TVSortChoices.choices:
            self.assertContains(response, f"hx-vals='{{\"sort\": \"{value}\"}}'")
