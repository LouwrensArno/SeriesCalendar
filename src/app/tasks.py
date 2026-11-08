import logging
import subprocess
import sys
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from app.models import (
    Item,
    MediaTypes,
    MovieDetails,
    Sources,
    TVShowDetails,
    UserMessage,
)
from app.providers import disneyplus_crawl as crawl
from app.providers import services, tmdb  # noqa: F401  (the models reach them as app.providers.*)

logger = logging.getLogger(__name__)


def tracked_tv_items():
    """Return every TMDB TV show item that at least one user tracks."""
    return Item.objects.filter(
        media_type=MediaTypes.TV.value,
        source=Sources.TMDB.value,
        tv__isnull=False,
    ).distinct()


@shared_task(name="Cleanup user messages")
def cleanup_user_messages():
    """Delete shown user messages older than the configured retention window."""
    cutoff = timezone.now() - timedelta(days=settings.USER_MESSAGE_RETENTION_DAYS)
    deleted_count, _ = UserMessage.objects.filter(
        shown_at__isnull=False,
        shown_at__lt=cutoff,
    ).delete()

    logger.info("Deleted %s old shown user messages.", deleted_count)

    return deleted_count


@shared_task(name="Crawl Disney+ South Africa")
def crawl_disneyplus_za():
    """Start tonight's slice of the Disney+ South Africa crawl, if it is switched on.

    It runs in its own process: it takes over an hour and the worker does one task at a time,
    so waiting for it here would hold up the release notifications. The command takes its own
    lock, so a second start while one is running is refused and logged.
    """
    if not crawl.crawl_enabled():
        return "off"
    log_path = settings.BASE_DIR / "db" / "disneyplus_crawl.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as log:
        log.write(f"\n--- {timezone.now().isoformat(timespec='seconds')} ---\n".encode())
        log.flush()
        subprocess.Popen(  # noqa: S603
            [sys.executable, str(settings.BASE_DIR / "manage.py"), "crawl_disneyplus_za"],
            cwd=settings.BASE_DIR,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    return "started"


@shared_task(name="Refresh TV show details")
def refresh_tv_show_details():
    """Refresh TMDB's details (status, networks, seasons, next air date) of every tracked show."""
    return TVShowDetails.objects.refresh(tracked_tv_items(), force=True)


def tracked_movie_items():
    """Return every TMDB movie item that at least one user tracks."""
    return Item.objects.filter(
        media_type=MediaTypes.MOVIE.value,
        source=Sources.TMDB.value,
        movie__isnull=False,
    ).distinct()


@shared_task(name="Refresh movie details")
def refresh_movie_details():
    """Refresh TMDB's details (release date, where to watch) of every tracked movie."""
    return MovieDetails.objects.refresh(tracked_movie_items())
