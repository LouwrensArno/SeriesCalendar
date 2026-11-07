import logging
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from app.models import Item, MediaTypes, Sources, TVShowDetails, UserMessage

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


@shared_task(name="Refresh TV show details")
def refresh_tv_show_details():
    """Refresh TMDB's details (status, networks, seasons, next air date) of every tracked show."""
    return TVShowDetails.objects.refresh(tracked_tv_items(), force=True)
