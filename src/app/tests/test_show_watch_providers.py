from datetime import date
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from app.models import (
    TV,
    Item,
    MediaTypes,
    Sources,
    Status,
    TVShowDetails,
)
from app.providers import tmdb
from app.templatetags.app_tags import show_watch_providers

# What TMDB sends in `watch/providers` for a show (shortened).
TMDB_PROVIDERS = {
    "US": {
        "link": "https://www.themoviedb.org/tv/1/watch?locale=US",
        "buy": [
            {
                "provider_id": 10,
                "provider_name": "Amazon Video",
                "logo_path": "/buy.png",
                "display_priority": 9,
                "extra": "dropped",
            },
        ],
        "flatrate": [
            {
                "provider_id": 15,
                "provider_name": "Hulu",
                "logo_path": "/hulu.png",
                "display_priority": 7,
                "extra": "dropped",
            },
            {
                "provider_id": 257,
                "provider_name": "fuboTV",
                "logo_path": "/fubo.png",
                "display_priority": 11,
            },
        ],
        "free": [
            {
                "provider_id": 300,
                "provider_name": "Pluto TV",
                "logo_path": "/pluto.png",
                "display_priority": 20,
            },
        ],
        "rent": [
            {
                "provider_id": 2,
                "provider_name": "Apple TV Store",
                "logo_path": "/rent.png",
                "display_priority": 10,
            },
        ],
    },
    "ZA": {
        "link": "https://www.themoviedb.org/tv/1/watch?locale=ZA",
        "buy": [
            {
                "provider_id": 10,
                "provider_name": "Amazon Video",
                "logo_path": "/buy.png",
                "display_priority": 9,
            },
        ],
    },
    "GB": {
        "flatrate": [
            {
                "provider_id": 350,
                "provider_name": "Apple TV",
                "logo_path": "/atv.png",
                "display_priority": 5,
            },
            {
                "provider_id": 2552,
                "provider_name": "Apple TV",
                "logo_path": "/atv-plus.png",
                "display_priority": 6,
            },
        ],
    },
}


class StreamingProvidersTrimTests(SimpleTestCase):
    """Test keeping only what the TV list needs of TMDB's watch providers."""

    def test_keeps_only_free_and_subscription_providers(self):
        """Rent and buy options, links and unused fields are dropped."""
        streaming = tmdb.get_streaming_providers(TMDB_PROVIDERS)

        self.assertEqual(set(streaming["US"]), {"flatrate", "free"})
        self.assertEqual(
            streaming["US"]["flatrate"][0],
            {
                "provider_id": 15,
                "provider_name": "Hulu",
                "logo_path": "/hulu.png",
                "display_priority": 7,
            },
        )

    def test_regions_with_nothing_to_stream_are_left_out(self):
        """A region that only rents or sells the show has no entry."""
        self.assertNotIn("ZA", tmdb.get_streaming_providers(TMDB_PROVIDERS))

    def test_no_providers(self):
        """TMDB having nothing for the show gives an empty result."""
        self.assertEqual(tmdb.get_streaming_providers(None), {})
        self.assertEqual(tmdb.get_streaming_providers({}), {})

    def test_can_be_read_by_the_existing_region_filter(self):
        """The trimmed data has the shape filter_providers (the show page) reads."""
        streaming = tmdb.get_streaming_providers(TMDB_PROVIDERS)

        names = [p["provider_name"] for p in tmdb.filter_providers(streaming, "US")]

        self.assertEqual(names, ["Hulu", "fuboTV", "Pluto TV"])


class ShowWatchProvidersTagTests(TestCase):
    """Test the template tag that picks the providers to show for a user."""

    def setUp(self):
        """Create a show with stored details and a user."""
        self.user = get_user_model().objects.create_user(username="test")
        item = Item.objects.create(
            media_id="1",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="Show",
            image="http://example.com/image.jpg",
        )
        self.details = TVShowDetails.objects.create(
            item=item,
            status="Returning Series",
            providers=tmdb.get_streaming_providers(TMDB_PROVIDERS),
        )

    def names(self, region):
        """Return the provider names shown for a user in the given region."""
        self.user.watch_provider_region = region
        return [p["provider_name"] for p in show_watch_providers(self.details, self.user)]

    def test_uses_the_users_region_best_first(self):
        """Subscription and free services in the region, in TMDB's priority order."""
        self.assertEqual(self.names("US"), ["Hulu", "fuboTV", "Pluto TV"])

    def test_adds_logo_urls(self):
        """Each provider carries the full logo URL."""
        self.user.watch_provider_region = "US"

        first = show_watch_providers(self.details, self.user)[0]

        self.assertEqual(first["image"], "https://image.tmdb.org/t/p/w500/hulu.png")

    def test_nothing_in_a_region_that_has_no_streaming(self):
        """A region that only rents or buys the show shows nothing."""
        self.assertEqual(self.names("ZA"), [])
        self.assertEqual(self.names("FR"), [])

    def test_unset_and_disabled_regions_show_nothing(self):
        """No region chosen yet, or the feature switched off, shows nothing."""
        self.assertEqual(self.names("UNSET"), [])
        self.assertEqual(self.names(""), [])

    def test_a_service_listed_twice_is_shown_once(self):
        """TMDB's duplicate listings of the same service are collapsed by name."""
        self.assertEqual(self.names("GB"), ["Apple TV"])

    def test_no_details_or_providers_yet(self):
        """A show without stored details, or never fetched, shows nothing."""
        self.user.watch_provider_region = "US"
        self.assertEqual(show_watch_providers(None, self.user), [])
        self.assertEqual(show_watch_providers("", self.user), [])  # template: missing
        self.details.providers = None
        self.assertEqual(show_watch_providers(self.details, self.user), [])


class TVListWhereToWatchTests(TestCase):
    """Test the Where to Watch column on the TV list page."""

    def setUp(self):
        """Track a show with stored providers, and log in."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)
        item = Item.objects.create(
            media_id="1",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="Show",
            image="http://example.com/image.jpg",
        )
        TV.objects.create(item=item, user=self.user, status=Status.PLANNING.value)
        TVShowDetails.objects.create(
            item=item,
            status="Returning Series",
            networks="ABC",
            season_count=1,
            next_air_date=date(2026, 12, 1),
            providers=tmdb.get_streaming_providers(TMDB_PROVIDERS),
        )
        self.url = reverse("medialist", args=[self.user.username, MediaTypes.TV.value])

    def page(self, region):
        """Return the TV table page for a user in the given region."""
        self.user.watch_provider_region = region
        self.user.save(update_fields=["watch_provider_region"])
        return self.client.get(self.url, {"layout": "table"}).content.decode()

    def test_column_sits_between_network_and_status(self):
        """The header order is Network, Where to Watch, Status."""
        content = self.page("US")

        network = content.index(">Network<")
        where = content.index(">Where to Watch<")
        status = content.index(">Status<")
        self.assertLess(network, where)
        self.assertLess(where, status)

    def test_shows_the_logos_for_the_users_region(self):
        """Provider logos appear with their names as tooltips."""
        content = self.page("US")

        for name in ("Hulu", "fuboTV", "Pluto TV"):
            self.assertIn(f'title="{name}"', content)
        self.assertIn("https://image.tmdb.org/t/p/w500/hulu.png", content)
        self.assertNotIn("Amazon Video", content)  # buying isn't streaming

    def test_more_than_three_shows_a_count(self):
        """Beyond three logos, a +N with the other names as its tooltip."""
        streaming = tmdb.get_streaming_providers(TMDB_PROVIDERS)
        streaming["US"]["flatrate"].append(
            {
                "provider_id": 9,
                "provider_name": "Extra Plus",
                "logo_path": "/x.png",
                "display_priority": 30,
            },
        )
        TVShowDetails.objects.update(providers=streaming)

        content = self.page("US")

        self.assertIn(">+1<", content)
        self.assertIn('title="Extra Plus">+1', content)

    def test_other_region_without_streaming_shows_a_dash(self):
        """A region with no streaming option shows no logos."""
        content = self.page("ZA")

        self.assertNotIn("image.tmdb.org/t/p/w500/hulu.png", content)

    def test_unset_region_hints_at_the_preference(self):
        """For your own list, an unset region explains why the column is empty."""
        content = self.page("UNSET")

        self.assertIn("Set a watch provider region in your preferences", content)

    def test_details_stored_before_providers_existed_are_refreshed(self):
        """Rows without the providers field get it on the next page load."""
        TVShowDetails.objects.update(providers=None)

        with patch(
            "app.models.providers.services.get_media_metadata",
            return_value={
                "details": {"status": "Returning Series", "seasons": 1},
                "networks": ["ABC"],
                "next_episode_air_date": None,
                "providers": TMDB_PROVIDERS,
            },
        ) as get_metadata:
            self.page("US")

        get_metadata.assert_called_once()
        stored = TVShowDetails.objects.get().providers
        self.assertEqual(set(stored), {"US", "GB"})
