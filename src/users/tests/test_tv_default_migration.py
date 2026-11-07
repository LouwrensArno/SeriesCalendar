import importlib

from django.apps import apps
from django.contrib.auth import get_user_model
from django.test import TestCase


class TVDefaultMigrationTests(TestCase):
    """Migration 0062 moves TV lists still on "All" to Current, the new default."""

    def setUp(self):
        """Load the migration's data functions."""
        self.migration = importlib.import_module(
            "users.migrations.0062_tv_status_default_current",
        )
        self.user_model = get_user_model()

    def make(self, name, status):
        user = self.user_model.objects.create_user(username=name)
        self.user_model.objects.filter(pk=user.pk).update(tv_status=status)
        return user

    def status(self, user):
        user.refresh_from_db()
        return user.tv_status

    def test_all_becomes_current_and_other_choices_are_kept(self):
        on_all = self.make("a", "All")
        on_concluded = self.make("b", "concluded")
        on_current = self.make("c", "current")

        self.migration.all_to_current(apps, None)

        self.assertEqual(self.status(on_all), "current")
        self.assertEqual(self.status(on_concluded), "concluded")
        self.assertEqual(self.status(on_current), "current")

    def test_rolling_back_restores_the_old_default(self):
        user = self.make("a", "current")
        kept = self.make("b", "concluded")

        self.migration.current_to_all(apps, None)

        self.assertEqual(self.status(user), "All")
        self.assertEqual(self.status(kept), "concluded")

    def test_new_users_get_current(self):
        self.assertEqual(self.user_model.objects.create_user(username="new").tv_status, "current")
