import importlib

from django.apps import apps
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase

from users.tests.test_tv_preferences import without_check_constraints


class TVStatusMigrationTests(TestCase):
    """Migration 0061 resets a saved TV filter the All/Current/Concluded choices lack."""

    def setUp(self):
        """Load the migration's data functions."""
        if connection.vendor != "sqlite":
            self.skipTest("needs SQLite to bypass the CHECK constraint")
        self.migration = importlib.import_module(
            "users.migrations.0061_tv_status_current_concluded",
        )

    def stored_status(self, user):
        user.refresh_from_db()
        return user.tv_status

    def test_a_saved_old_filter_goes_back_to_all(self):
        user_model = get_user_model()
        users = {}
        for old in ("airing", "ended", "canceled", "coming", "Completed"):
            users[old] = user_model.objects.create_user(username=f"u-{old}")
            with without_check_constraints():
                user_model.objects.filter(pk=users[old].pk).update(tv_status=old)

        self.migration.reset_tv_status(apps, None)

        for old, user in users.items():
            self.assertEqual(self.stored_status(user), "All", old)

    def test_new_filters_are_kept(self):
        user_model = get_user_model()
        kept = {}
        for value in ("All", "current", "concluded"):
            kept[value] = user_model.objects.create_user(username=f"k-{value}")
            user_model.objects.filter(pk=kept[value].pk).update(tv_status=value)

        self.migration.reset_tv_status(apps, None)

        for value, user in kept.items():
            self.assertEqual(self.stored_status(user), value)

    def test_rolling_back_leaves_only_values_the_previous_choices_accept(self):
        user_model = get_user_model()
        user = user_model.objects.create_user(username="back")
        user_model.objects.filter(pk=user.pk).update(tv_status="concluded")

        self.migration.restore_tv_status(apps, None)

        self.assertEqual(self.stored_status(user), "All")
