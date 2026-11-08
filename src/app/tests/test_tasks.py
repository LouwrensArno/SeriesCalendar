import os
import subprocess
import sys
import tempfile
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from app.models import UserMessage, UserMessageLevel
from app.tasks import cleanup_user_messages, crawl_disneyplus_za


class CleanupUserMessagesTaskTests(TestCase):
    """Test cleanup of old shown user messages."""

    def setUp(self):
        """Create a user for task tests."""
        self.user = get_user_model().objects.create_user(
            username="test",
        )

    @override_settings(USER_MESSAGE_RETENTION_DAYS=30)
    def test_cleanup_user_messages_deletes_only_old_shown_messages(self):
        """Delete only shown messages older than the retention window."""
        now = timezone.now()
        old_shown = UserMessage.objects.create(
            user=self.user,
            level=UserMessageLevel.INFO,
            message="old shown",
            shown_at=now - timedelta(days=31),
        )
        recent_shown = UserMessage.objects.create(
            user=self.user,
            level=UserMessageLevel.INFO,
            message="recent shown",
            shown_at=now - timedelta(days=5),
        )
        unseen = UserMessage.objects.create(
            user=self.user,
            level=UserMessageLevel.INFO,
            message="unseen",
        )

        deleted_count = cleanup_user_messages()

        self.assertEqual(deleted_count, 1)
        self.assertFalse(UserMessage.objects.filter(id=old_shown.id).exists())
        self.assertTrue(UserMessage.objects.filter(id=recent_shown.id).exists())
        self.assertTrue(UserMessage.objects.filter(id=unseen.id).exists())


class CrawlDisneyplusTaskTests(SimpleTestCase):
    """The nightly Disney+ crawl is started in its own process, and only when switched on."""

    def test_it_does_nothing_when_the_crawl_is_switched_off(self):
        with tempfile.TemporaryDirectory() as folder, \
             override_settings(DISNEYPLUS_CRAWL=False, BASE_DIR=Path(folder)), \
             patch("app.tasks.subprocess.Popen") as popen:
            self.assertEqual(crawl_disneyplus_za(), "off")

        popen.assert_not_called()

    def test_it_starts_the_command_detached_and_logs_to_a_file(self):
        with tempfile.TemporaryDirectory() as folder, \
             override_settings(DISNEYPLUS_CRAWL=True, BASE_DIR=Path(folder)), \
             patch("app.tasks.subprocess.Popen") as popen:
            self.assertEqual(crawl_disneyplus_za(), "started")

            args, kwargs = popen.call_args
            self.assertEqual(args[0][1:], [str(Path(folder) / "manage.py"), "crawl_disneyplus_za"])
            self.assertTrue(kwargs["start_new_session"])
            self.assertTrue((Path(folder) / "db" / "disneyplus_crawl.log").exists())

    def test_the_file_switch_is_enough(self):
        with tempfile.TemporaryDirectory() as folder, \
             override_settings(DISNEYPLUS_CRAWL=False, BASE_DIR=Path(folder)), \
             patch("app.tasks.subprocess.Popen") as popen:
            (Path(folder) / "db").mkdir()
            (Path(folder) / "db" / "disneyplus_crawl.on").write_text("")

            self.assertEqual(crawl_disneyplus_za(), "started")

        popen.assert_called_once()

    def test_it_is_scheduled_nightly(self):
        entry = settings.CELERY_BEAT_SCHEDULE["crawl_disneyplus_za"]

        self.assertEqual(entry["task"], "Crawl Disney+ South Africa")
        self.assertEqual(entry["schedule"].hour, {settings.DISNEYPLUS_CRAWL_HOUR})


class TaskImportsTests(SimpleTestCase):
    """The refresh tasks need the TMDB provider modules, which the models only reach as attributes."""

    def test_importing_the_tasks_alone_makes_the_providers_available(self):
        code = (
            "import django; django.setup(); import app.tasks; from app import providers; "
            "print(hasattr(providers, 'services') and hasattr(providers, 'tmdb'))"
        )

        result = subprocess.run(  # noqa: S603
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            check=False,
            cwd=settings.BASE_DIR,
            env={**os.environ, "DJANGO_SETTINGS_MODULE": "config.settings"},
        )

        self.assertEqual(result.stdout.strip().splitlines()[-1], "True", result.stderr[-500:])
