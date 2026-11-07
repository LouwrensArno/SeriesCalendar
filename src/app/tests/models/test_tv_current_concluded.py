from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from app.models import (
    TV,
    BasicMedia,
    Episode,
    Item,
    MediaTypes,
    Season,
    Sources,
    Status,
    TVShowDetails,
)
from events.models import Event


class TVCurrentConcludedTests(TestCase):
    """The TV filter: All, Current and Concluded.

    Current: airing or coming on TMDB, or any show with released episodes left to watch.
    Concluded: ended or canceled on TMDB with no released episodes left to watch.
    """

    # title, TMDB status ("" = details not fetched yet), episodes released, episodes watched
    SHOWS = (
        ("Airing caught up", "Returning Series", 10, 10),
        ("Airing behind", "Returning Series", 10, 3),
        ("Coming nothing out", "In Production", 0, 0),
        ("Planned", "Planned", 0, 0),
        ("Ended behind", "Ended", 10, 4),
        ("Ended caught up", "Ended", 10, 10),
        ("Ended ahead", "Ended", 3, 5),  # watched more than is released: nothing left
        ("Ended nothing out", "Ended", 0, 0),
        ("Canceled behind", "Canceled", 8, 1),
        ("Canceled caught up", "Canceled", 8, 8),
        ("Unknown behind", "", 6, 2),
        ("Unknown caught up", "", 6, 6),
    )

    def setUp(self):
        """Track the shows, each with its released and watched episodes."""
        # nothing here may reach the real TMDB: fail loudly if anything tries
        blocker = patch(
            "app.models.providers.services.get_media_metadata",
            side_effect=AssertionError("test tried to contact TMDB"),
        )
        blocker.start()
        self.addCleanup(blocker.stop)
        self.user = get_user_model().objects.create_user(username="test", password="12345")
        past = timezone.now() - timedelta(days=30)

        for number, (title, status, released, watched) in enumerate(self.SHOWS, start=1):
            media_id = str(number)
            tv_item = Item.objects.create(
                media_id=media_id, source=Sources.TMDB.value, media_type=MediaTypes.TV.value,
                title=title, image="http://example.com/i.jpg",
            )
            tv = TV.objects.create(item=tv_item, user=self.user, status=Status.PLANNING.value)
            if status:
                TVShowDetails.objects.create(item=tv_item, status=status, providers={})
            season_item = Item.objects.create(
                media_id=media_id, source=Sources.TMDB.value, media_type=MediaTypes.SEASON.value,
                season_number=1, title=title, image="http://example.com/i.jpg",
            )
            season = Season.objects.create(
                item=season_item, user=self.user, related_tv=tv, status=Status.IN_PROGRESS.value,
            )
            if released:
                Event.objects.create(item=season_item, content_number=released, datetime=past)
            self.watch(season, media_id, title, range(1, watched + 1), past)

    def watch(self, season, media_id, title, numbers, when):
        """Record watched episodes. Written in bulk because Episode.save() asks TMDB."""
        items = Item.objects.bulk_create([
            Item(
                media_id=media_id, source=Sources.TMDB.value, media_type=MediaTypes.EPISODE.value,
                season_number=1, episode_number=number, title=title, image="http://example.com/i.jpg",
            )
            for number in numbers
        ])
        Episode.objects.bulk_create([
            Episode(related_season=season, item=item, end_date=when) for item in items
        ])

    def titles(self, show_status, user=None):
        """Return the titles the TV list shows for a filter, in title order."""
        return [
            tv.item.title
            for tv in BasicMedia.objects.get_media_list(
                user=user or self.user,
                media_type=MediaTypes.TV.value,
                status_filter="All",
                sort_filter="title",
                show_status=show_status,
            )
        ]

    def test_all_applies_no_filtering(self):
        everything = sorted(title for title, *_ in self.SHOWS)

        self.assertEqual(self.titles("All"), everything)
        self.assertEqual(self.titles(None), everything)

    def test_current(self):
        self.assertEqual(
            self.titles("current"),
            [
                "Airing behind",        # airing, and episodes to watch
                "Airing caught up",     # airing: current even with nothing to watch
                "Canceled behind",      # canceled, but episodes still to watch
                "Coming nothing out",   # coming
                "Ended behind",         # ended, but episodes still to watch
                "Planned",              # coming
                "Unknown behind",       # status not known yet, and episodes to watch
                "Unknown caught up",    # status not known yet: stays visible, not hidden
            ],
        )

    def test_concluded(self):
        self.assertEqual(
            self.titles("concluded"),
            [
                "Canceled caught up",
                "Ended ahead",          # watched more than has been released
                "Ended caught up",
                "Ended nothing out",
            ],
        )

    def test_a_show_whose_status_is_not_known_yet_is_current_never_concluded(self):
        # Current is the default list: a newly tracked show must not vanish from it
        # while its TMDB details are still to be fetched.
        for title in ("Unknown behind", "Unknown caught up"):
            self.assertIn(title, self.titles("current"))
            self.assertNotIn(title, self.titles("concluded"))

    def test_every_show_is_in_exactly_one_group(self):
        everything = {title for title, *_ in self.SHOWS}
        current, concluded = set(self.titles("current")), set(self.titles("concluded"))

        self.assertFalse(current & concluded)
        self.assertEqual(current | concluded, everything)

    def test_watching_the_last_episode_moves_an_ended_show_to_concluded(self):
        self.assertIn("Ended behind", self.titles("current"))
        season = Season.objects.get(item__media_id="5", item__season_number=1)
        self.watch(season, "5", "Ended behind", range(5, 11), timezone.now())

        self.assertIn("Ended behind", self.titles("concluded"))
        self.assertNotIn("Ended behind", self.titles("current"))

    def test_an_ended_show_with_a_new_released_episode_moves_back_to_current(self):
        self.assertIn("Ended caught up", self.titles("concluded"))
        season_item = Item.objects.get(media_id="6", media_type=MediaTypes.SEASON.value)
        Event.objects.create(item=season_item, content_number=11, datetime=timezone.now() - timedelta(days=1))

        self.assertIn("Ended caught up", self.titles("current"))
        self.assertNotIn("Ended caught up", self.titles("concluded"))

    def test_only_the_users_own_shows_count(self):
        other = get_user_model().objects.create_user(username="other")

        self.assertEqual(self.titles("current", user=other), [])
        self.assertEqual(self.titles("concluded", user=other), [])

    def test_the_page_uses_the_filter_with_the_same_numbers_as_the_table(self):
        self.client.login(username="test", password="12345")
        url = reverse("medialist", args=[self.user.username, MediaTypes.TV.value])

        response = self.client.get(url, {"layout": "table", "status": "concluded", "sort": "title"})

        shown = {tv.item.title: (tv.progress, tv.max_progress) for tv in response.context["media_list"]}
        self.assertEqual(shown["Ended caught up"], (10, 10))
        self.assertEqual(shown["Ended ahead"], (5, 3))
        self.assertEqual(len(shown), 4)
