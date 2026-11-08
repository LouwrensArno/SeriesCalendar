from datetime import date, timedelta
from unittest.mock import patch

import requests
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from app.management.commands.scrape_disneyplus_za import refresh_changed
from app.models import (
    Item,
    MediaTypes,
    Movie,
    MovieDetails,
    Sources,
    Status,
)
from app.providers import tmdb
from app.providers.services import ProviderAPIError
from app.tasks import refresh_movie_details
from app.tests.test_show_watch_providers import TMDB_PROVIDERS

METADATA = "app.models.providers.services.get_media_metadata"


def tmdb_movie(release_date="2021-06-11", providers=None):
    """What the movie metadata call returns (shortened)."""
    return {
        "details": {"release_date": release_date},
        "providers": TMDB_PROVIDERS if providers is None else providers,
    }


def track(user, media_id, title, source=Sources.TMDB.value):
    """Track a movie for the user."""
    item = Item.objects.create(
        media_id=media_id,
        source=source,
        media_type=MediaTypes.MOVIE.value,
        title=title,
        image="http://example.com/image.jpg",
    )
    Movie.objects.create(item=item, user=user, status=Status.PLANNING.value)
    return item


class MovieListTests(TestCase):
    """The movie table shows TMDB's release year and where to watch."""

    def setUp(self):
        """Track two movies (both with stored details) and log in."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.user.watch_provider_region = "US"
        self.user.save(update_fields=["watch_provider_region"])
        self.client.login(**self.credentials)
        self.url = reverse("medialist", args=[self.user.username, MediaTypes.MOVIE.value])
        self.known = track(self.user, "1", "Known Movie")
        self.undated = track(self.user, "2", "Undated Movie")
        MovieDetails.objects.create(
            item=self.known,
            release_date=date(2021, 6, 11),
            providers=tmdb.get_streaming_providers(TMDB_PROVIDERS),
        )
        MovieDetails.objects.create(item=self.undated, release_date=None, providers={})

    def page(self, **params):
        """Return the movie table page."""
        response = self.client.get(self.url, {"layout": "table", **params})
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_the_year_and_where_to_watch_columns_come_before_the_rating(self):
        """The header order is Title, Year, Where to Watch, Score."""
        content = self.page()

        order = [content.index(f">{name}<") for name in ("Title", "Year", "Where to Watch", "Score")]
        self.assertEqual(order, sorted(order))

    def test_the_release_year_is_shown_and_a_missing_one_is_a_dash(self):
        """A dated movie shows its year; an undated one doesn't."""
        content = self.page()

        known_row = content.split("Known Movie</a>")[1].split("</tr>")[0]
        undated_row = content.split("Undated Movie</a>")[1].split("</tr>")[0]
        self.assertIn("2021", known_row)
        self.assertNotIn("2021", undated_row)

    def test_the_logos_for_the_users_region_are_shown(self):
        """Provider logos appear with their names as tooltips; buying isn't streaming."""
        content = self.page()

        for name in ("Hulu", "fuboTV", "Pluto TV"):
            self.assertIn(f'title="{name}"', content)
        self.assertNotIn("Amazon Video", content)

    def test_a_region_without_streaming_shows_no_logos(self):
        """Nothing streams it in that region."""
        self.user.watch_provider_region = "ZA"
        self.user.save(update_fields=["watch_provider_region"])

        self.assertNotIn("image.tmdb.org/t/p/w500/hulu.png", self.page())

    def test_other_lists_do_not_get_the_movie_columns(self):
        """Only movies have a Year / Where to Watch column in the generic table."""
        book_url = reverse("medialist", args=[self.user.username, MediaTypes.BOOK.value])

        content = self.client.get(book_url, {"layout": "table"}).content.decode()

        self.assertNotIn(">Where to Watch<", content)

    def test_movies_without_details_are_fetched_when_the_page_loads(self):
        """A newly tracked movie gets its year and providers on the first view."""
        new = track(self.user, "75219", "New Movie")

        with patch(METADATA, return_value=tmdb_movie("2024-03-01")) as get_metadata:
            content = self.page()

        get_metadata.assert_called_once_with(MediaTypes.MOVIE.value, "75219", "tmdb")
        self.assertIn("2024", content)
        self.assertEqual(MovieDetails.objects.get(item=new).release_date, date(2024, 3, 1))

    def test_the_other_media_types_do_not_fetch_movie_details(self):
        """Opening a book list never asks TMDB for movies."""
        book_url = reverse("medialist", args=[self.user.username, MediaTypes.BOOK.value])

        with patch(METADATA) as get_metadata:
            self.client.get(book_url, {"layout": "table"})

        get_metadata.assert_not_called()


class MovieDetailsRefreshTests(TestCase):
    """Keeping the stored movie details in step with TMDB."""

    def setUp(self):
        """Create a user and a TMDB movie."""
        self.user = get_user_model().objects.create_user(username="test", password="12345")
        self.item = track(self.user, "10", "Movie")

    def test_a_movie_without_details_gets_them(self):
        """Release date and trimmed providers are stored."""
        with patch(METADATA, return_value=tmdb_movie("1999-03-31")):
            refreshed = MovieDetails.objects.refresh([self.item])

        stored = MovieDetails.objects.get(item=self.item)
        self.assertEqual(refreshed, 1)
        self.assertEqual(stored.release_date, date(1999, 3, 31))
        self.assertEqual(set(stored.providers), {"US", "GB"})

    def test_a_movie_with_no_release_date_stores_none(self):
        """TMDB gives an empty date for unreleased or unknown titles."""
        with patch(METADATA, return_value=tmdb_movie(release_date=None)):
            MovieDetails.objects.refresh([self.item])

        self.assertIsNone(MovieDetails.objects.get(item=self.item).release_date)

    def test_fresh_details_are_left_alone(self):
        """Nothing is fetched for details refreshed within a day."""
        MovieDetails.objects.create(item=self.item, providers={})

        with patch(METADATA) as get_metadata:
            refreshed = MovieDetails.objects.refresh([self.item])

        get_metadata.assert_not_called()
        self.assertEqual(refreshed, 0)

    def test_old_details_are_refreshed(self):
        """Details more than a day old are fetched again."""
        MovieDetails.objects.create(item=self.item, providers={})
        MovieDetails.objects.update(refreshed_at=timezone.now() - timedelta(days=2))

        with patch(METADATA, return_value=tmdb_movie()):
            self.assertEqual(MovieDetails.objects.refresh([self.item]), 1)

    def test_details_without_providers_are_refreshed(self):
        """Rows that never had their providers fetched get them."""
        MovieDetails.objects.create(item=self.item, providers=None)

        with patch(METADATA, return_value=tmdb_movie()):
            self.assertEqual(MovieDetails.objects.refresh([self.item]), 1)

    def test_force_refreshes_fresh_details(self):
        """The Disney+ matcher forces it."""
        MovieDetails.objects.create(item=self.item, providers={})

        with patch(METADATA, return_value=tmdb_movie()):
            self.assertEqual(MovieDetails.objects.refresh([self.item], force=True), 1)

    def test_only_tmdb_movies_are_fetched(self):
        """Other sources and other media types are skipped."""
        manual = track(self.user, "11", "Manual", source=Sources.MANUAL.value)
        show = Item.objects.create(
            media_id="12",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="Show",
            image="http://example.com/image.jpg",
        )

        with patch(METADATA) as get_metadata:
            MovieDetails.objects.refresh([manual, show])

        get_metadata.assert_not_called()

    def test_a_failure_keeps_what_was_stored_and_does_not_stop_the_rest(self):
        """One unreachable movie never blocks the others."""
        other = track(self.user, "20", "Other")
        MovieDetails.objects.create(item=self.item, release_date=date(2000, 1, 1), providers={})
        MovieDetails.objects.update(refreshed_at=timezone.now() - timedelta(days=3))

        def fetch(media_type, media_id, source):  # noqa: ARG001
            if media_id == "10":
                msg = "down"
                raise requests.ConnectionError(msg)
            return tmdb_movie("2010-10-10")

        with patch(METADATA, side_effect=fetch):
            refreshed = MovieDetails.objects.refresh([self.item, other])

        self.assertEqual(refreshed, 1)
        self.assertEqual(MovieDetails.objects.get(item=self.item).release_date, date(2000, 1, 1))
        self.assertEqual(MovieDetails.objects.get(item=other).release_date, date(2010, 10, 10))

    def test_a_provider_api_error_is_handled_too(self):
        """TMDB answering with an error is the same as not reachable."""
        with patch(METADATA, side_effect=ProviderAPIError(Sources.TMDB.value, 500)):
            self.assertEqual(MovieDetails.objects.refresh([self.item]), 0)

    def test_the_daily_task_refreshes_tracked_tmdb_movies_only(self):
        """Movies nobody tracks are not fetched."""
        Item.objects.create(
            media_id="99",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Untracked",
            image="http://example.com/image.jpg",
        )

        with patch(METADATA, return_value=tmdb_movie()) as get_metadata:
            refresh_movie_details()

        get_metadata.assert_called_once_with(MediaTypes.MOVIE.value, "10", "tmdb")

    def test_a_changed_disney_plus_match_refreshes_the_movie(self):
        """When the Disney+ matching adds a movie, its stored providers are fetched at once."""
        MovieDetails.objects.create(item=self.item, providers={})

        with patch(METADATA, return_value=tmdb_movie()) as get_metadata:
            changed = refresh_changed({}, {"movie:10": {"entity": "/en-za/browse/entity-x", "title": "Movie"}})

        self.assertEqual(changed, ["movie:10"])
        get_metadata.assert_called_once_with(MediaTypes.MOVIE.value, "10", "tmdb")
