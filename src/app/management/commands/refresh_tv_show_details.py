from django.core.management.base import BaseCommand

from app.tasks import refresh_tv_show_details


class Command(BaseCommand):
    """Fetch TMDB's show details for every tracked TV show right now."""

    help = (
        "Refresh the stored TMDB details (status, networks, seasons, next air date) "
        "of every tracked TV show. Run once after updating, to fill the TV list's "
        "filters and sorting for the whole library; a daily task keeps them current."
    )

    def handle(self, *args, **options):  # noqa: ARG002
        """Refresh every tracked show and report how many were updated."""
        refreshed = refresh_tv_show_details()
        self.stdout.write(self.style.SUCCESS(f"Refreshed {refreshed} TV shows."))
