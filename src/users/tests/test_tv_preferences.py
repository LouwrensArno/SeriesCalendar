import importlib
from contextlib import contextmanager

from django.apps import apps
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase

from users.models import TVSortChoices, TVStatusChoices


class TVListPreferencesTests(TestCase):
    """The TV list has its own filter and sort options, following its columns."""

    def setUp(self):
        """Create a user with the default preferences."""
        self.user = get_user_model().objects.create_user(username="test")

    def test_defaults(self):
        """A new user sorts TV by title and sees every status."""
        self.assertEqual(self.user.tv_sort, TVSortChoices.TITLE)
        self.assertEqual(self.user.tv_status, TVStatusChoices.ALL)

    def test_new_options_are_accepted(self):
        """Every column the table can sort by, and every show status, is valid."""
        for value in ("title", "network", "next_episode", "seasons", "progress"):
            self.assertEqual(self.user.get_valid_preference("tv_sort", value), value)
        for value in ("All", "airing", "ended", "canceled", "coming"):
            self.assertEqual(self.user.get_valid_preference("tv_status", value), value)

    def test_old_options_are_refused(self):
        """The old tracking-status filter and removed sorts keep the current value."""
        for value in ("score", "start_date", "end_date"):
            self.assertEqual(self.user.get_valid_preference("tv_sort", value), "title")
        for value in ("Completed", "In progress", "Planning", "Paused", "Dropped"):
            self.assertEqual(self.user.get_valid_preference("tv_status", value), "All")

    def test_other_media_types_keep_their_options(self):
        """Only TV changed: movies still sort by rating and filter by tracking status."""
        self.assertEqual(self.user.get_valid_preference("movie_sort", "score"), "score")
        self.assertEqual(
            self.user.get_valid_preference("movie_status", "Completed"),
            "Completed",
        )
        # ...and don't accept TV's
        self.assertEqual(
            self.user.get_valid_preference("movie_sort", "network"),
            "score",
        )
        self.assertEqual(
            self.user.get_valid_preference("movie_status", "airing"),
            "All",
        )


@contextmanager
def without_check_constraints():
    """Switch off SQLite's CHECK constraints for a moment.

    The migration deals with rows that hold values the new constraint refuses, and
    runs backwards with the constraints already removed. Neither can be reproduced
    with them in place. Only SQLite (the test database) can do this.
    """
    with connection.cursor() as cursor:
        cursor.execute("PRAGMA ignore_check_constraints = ON")
        try:
            yield
        finally:
            cursor.execute("PRAGMA ignore_check_constraints = OFF")


class TVPreferencesMigrationTests(TestCase):
    """The migration resets stored TV values the new choices don't have."""

    def setUp(self):
        """Load the migration's data functions."""
        if connection.vendor != "sqlite":
            self.skipTest("needs SQLite to bypass the CHECK constraint")
        self.migration = importlib.import_module(
            "users.migrations.0060_tv_show_details",
        )

    def test_reset_replaces_only_values_that_no_longer_exist(self):
        """Old values go to the new defaults; values that still exist stay put."""
        user_model = get_user_model()
        old = user_model.objects.create_user(username="old")
        kept = user_model.objects.create_user(username="kept")
        with without_check_constraints():
            user_model.objects.filter(pk=old.pk).update(
                tv_sort="score",
                tv_status="Completed",
            )
        user_model.objects.filter(pk=kept.pk).update(
            tv_sort="progress",
            tv_status="airing",
        )

        self.migration.reset_tv_preferences(apps, None)

        old.refresh_from_db()
        kept.refresh_from_db()
        self.assertEqual((old.tv_sort, old.tv_status), ("title", "All"))
        self.assertEqual((kept.tv_sort, kept.tv_status), ("progress", "airing"))

    def test_reverse_restores_values_the_old_choices_accept(self):
        """Rolling back the migration doesn't leave values the old constraint rejects."""
        user_model = get_user_model()
        user = user_model.objects.create_user(username="new")
        user_model.objects.filter(pk=user.pk).update(
            tv_sort="network",
            tv_status="airing",
        )

        with without_check_constraints():
            self.migration.restore_tv_preferences(apps, None)

        user.refresh_from_db()
        self.assertEqual((user.tv_sort, user.tv_status), ("score", "All"))
