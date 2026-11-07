from datetime import timedelta
from unittest.mock import patch

import requests
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
)
from events.models import Event


class TVEpisodesLeftSortTests(TestCase):
    """Sorting the TV list by released episodes minus watched episodes."""

    # title, episodes released so far, episodes watched
    SHOWS = (
        ("Alpha Show", 10, 10),  # caught up: 0 left
        ("Bravo Show", 10, 2),  # 8 left
        ("Charlie Show", 20, 15),  # 5 left
        ("Delta Show", 0, 0),  # nothing released: 0 left
        ("Echo Show", 3, 5),  # watched more than is released: counts as 0 left
        ("Foxtrot Show", 30, 0),  # 30 left: furthest behind
    )

    def setUp(self):
        """Track shows with different released and watched counts."""
        self.user = get_user_model().objects.create_user(
            username="test",
            password="12345",
        )
        past = timezone.now() - timedelta(days=30)

        for number, (title, released, watched) in enumerate(self.SHOWS, start=1):
            media_id = str(number)
            tv_item = Item.objects.create(
                media_id=media_id,
                source=Sources.TMDB.value,
                media_type=MediaTypes.TV.value,
                title=title,
                image="http://example.com/image.jpg",
            )
            tv = TV.objects.create(
                item=tv_item,
                user=self.user,
                status=Status.PLANNING.value,  # no start-the-next-season side effects
            )
            season_item = Item.objects.create(
                media_id=media_id,
                source=Sources.TMDB.value,
                media_type=MediaTypes.SEASON.value,
                season_number=1,
                title=title,
                image="http://example.com/image.jpg",
            )
            season = Season.objects.create(
                item=season_item,
                user=self.user,
                related_tv=tv,
                status=Status.IN_PROGRESS.value,
            )
            if released:
                Event.objects.create(
                    item=season_item,
                    content_number=released,
                    datetime=past,
                )
            for episode_number in range(1, watched + 1):
                Episode.objects.create(
                    related_season=season,
                    item=Item.objects.create(
                        media_id=media_id,
                        source=Sources.TMDB.value,
                        media_type=MediaTypes.EPISODE.value,
                        season_number=1,
                        episode_number=episode_number,
                        title=title,
                        image="http://example.com/image.jpg",
                    ),
                    end_date=past,
                )

    def titles(self):
        """Return the TV list titles sorted by episodes left."""
        return [
            tv.item.title
            for tv in BasicMedia.objects.get_media_list(
                user=self.user,
                media_type=MediaTypes.TV.value,
                status_filter="All",
                sort_filter="progress",
            )
        ]

    def test_most_episodes_left_first(self):
        """The show furthest behind comes first; caught-up shows last, alphabetically."""
        self.assertEqual(
            self.titles(),
            [
                "Foxtrot Show",  # 30 left
                "Bravo Show",  # 8 left
                "Charlie Show",  # 5 left
                "Alpha Show",  # caught up
                "Delta Show",  # nothing released
                "Echo Show",  # ahead of what is released
            ],
        )

    def test_the_numbers_used_are_the_ones_the_table_shows(self):
        """Sorting uses the same watched and released counts as the Episodes column."""
        self.client.login(username="test", password="12345")

        # the page asks TMDB for shows it has no details for yet; keep it offline
        with patch(
            "app.models.providers.services.get_media_metadata",
            side_effect=requests.ConnectionError,
        ):
            response = self.client.get(
                reverse("medialist", args=[self.user.username, MediaTypes.TV.value]),
                {"layout": "table", "sort": "progress", "status": "All"},
            )

        shown = {
            tv.item.title: (tv.progress, tv.max_progress)
            for tv in response.context["media_list"]
        }
        self.assertEqual(shown["Bravo Show"], (2, 10))
        self.assertEqual(shown["Foxtrot Show"], (0, 30))
        self.assertEqual(
            [tv.item.title for tv in response.context["media_list"]][:3],
            ["Foxtrot Show", "Bravo Show", "Charlie Show"],
        )

    def test_empty_list(self):
        """A user who tracks no TV shows gets an empty list, not an error."""
        other = get_user_model().objects.create_user(username="nobody")

        result = BasicMedia.objects.get_media_list(
            user=other,
            media_type=MediaTypes.TV.value,
            status_filter="All",
            sort_filter="progress",
        )

        self.assertEqual(list(result), [])
