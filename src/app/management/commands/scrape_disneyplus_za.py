from django.conf import settings
from django.core.cache import cache
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from app.models import Item, MediaTypes, Sources, TVShowDetails
from app.providers import disneyplus_za, services


def default_path():
    """Where the catalogue is kept: next to the database, so it survives rebuilds."""
    return settings.BASE_DIR / "db" / "disneyplus_za.json"


def tracked_items():
    """Every TMDB show and film that at least one user tracks."""
    return Item.objects.filter(source=Sources.TMDB.value).filter(
        Q(media_type=MediaTypes.TV.value, tv__isnull=False)
        | Q(media_type=MediaTypes.MOVIE.value, movie__isnull=False),
    ).distinct()


def release_year(item):
    """The year TMDB gives the title, or None if it can't be fetched."""
    try:
        metadata = services.get_media_metadata(item.media_type, item.media_id, item.source)
    except Exception:  # noqa: BLE001  a lookup failure must not stop the run
        return None
    details = metadata.get("details", {})
    date = details.get("first_air_date") or details.get("release_date") or ""
    return int(date[:4]) if date[:4].isdigit() else None


def refresh_changed(old, new):
    """Make a changed match show up now, not when TMDB's cached copy expires.

    TMDB data is cached for hours and already holds the old provider list, so the cached
    entries of every title whose match was added, removed or changed are cleared and the
    stored show details of the shows among them are fetched again.
    """
    changed = [
        key
        for key in set(old) | set(new)
        if (old.get(key) or {}).get("entity") != (new.get(key) or {}).get("entity")
    ]
    show_ids = []
    for key in changed:
        kind, media_id = key.split(":", 1)
        cache.delete(f"{Sources.TMDB.value}_{kind}_{media_id}")
        if kind == MediaTypes.TV.value:
            show_ids.append(media_id)
            cache.delete_pattern(f"{Sources.TMDB.value}_{MediaTypes.SEASON.value}_{media_id}_*")
    if show_ids:
        shows = Item.objects.filter(
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            media_id__in=show_ids,
        )
        TVShowDetails.objects.refresh(shows, force=True)
    return changed


class Command(BaseCommand):
    """Read the public Disney+ South Africa catalogue, save it and match your titles."""

    help = (
        "Read the public, logged-out Disney+ South Africa browse pages, save the titles, "
        "and match the shows and films you track to them so Disney+ appears under "
        "Where to Watch for South Africa. Run from a South African connection, at most "
        "weekly. A catalogue that looks broken never replaces a good one."
    )

    def add_arguments(self, parser):
        """Add the command's options."""
        parser.add_argument("--output", default=None, help="file to write")
        parser.add_argument("--max-pages", type=int, default=100)
        parser.add_argument("--delay", type=float, default=2.0, help="seconds between requests")
        parser.add_argument(
            "--no-match",
            action="store_true",
            help="only save the catalogue; don't match what you track",
        )

    def handle(self, *args, **options):  # noqa: ARG002
        """Crawl, sanity-check, match what is tracked, and save."""
        path = options["output"] or default_path()
        try:
            snapshot = disneyplus_za.fetch_catalogue(
                max_pages=options["max_pages"],
                delay=options["delay"],
            )
        except disneyplus_za.CatalogueError as error:
            raise CommandError(str(error)) from error

        problem = disneyplus_za.check_snapshot(snapshot, disneyplus_za.load_snapshot(path))
        if problem:
            msg = f"Not saved ({problem}). The previous catalogue, if any, was kept."
            raise CommandError(msg)

        previous = disneyplus_za.load_snapshot(path) or {}
        matches = previous.get("matches") or {}
        if not options["no_match"]:
            wanted = [
                (
                    f"{'tv' if item.media_type == MediaTypes.TV.value else 'movie'}:{item.media_id}",
                    item.title,
                    # the year is only looked up (on TMDB) when a name match has to be checked
                    lambda item=item: release_year(item),
                    "tv" if item.media_type == MediaTypes.TV.value else "movie",
                )
                for item in tracked_items()
            ]
            matches = disneyplus_za.match_titles(
                snapshot,
                wanted,
                previous=matches,
                delay=options["delay"],
            )
            self.stdout.write(f"{len(matches)} of {len(wanted)} tracked titles are on Disney+ ZA.")
        snapshot["matches"] = matches

        disneyplus_za.save_snapshot(snapshot, path)
        changed = refresh_changed(previous.get("matches") or {}, matches)
        self.stdout.write(
            self.style.SUCCESS(
                f"Saved {len(snapshot['titles'])} Disney+ ZA titles from "
                f"{snapshot['pages_read']} pages and {len(matches)} matches to {path}"
                f" ({len(changed)} changed).",
            ),
        )
