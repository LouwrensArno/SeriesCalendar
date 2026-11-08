from datetime import UTC, datetime

from django.core.management.base import BaseCommand, CommandError

from app.management.commands.scrape_disneyplus_za import default_path as catalogue_path
from app.management.commands.scrape_disneyplus_za import refresh_changed, tracked_items, wanted_titles
from app.providers import disneyplus_crawl as crawl
from app.providers import disneyplus_za as dp

state_path = crawl.state_path


class Command(BaseCommand):
    """Read one slice of the public Disney+ South Africa title pages."""

    help = (
        "Read a slice (up to a thousand) of the public Disney+ South Africa title pages, to "
        "find titles the curated hub pages never show. Resumable: it carries on where it "
        "stopped. Then it matches what you track against everything found so far, so Where "
        "to Watch stays current. Slow and polite by design, and OFF unless "
        "DISNEYPLUS_CRAWL=True (or the file db/disneyplus_crawl.on exists). "
        "Run from a South African connection, at night."
    )

    def add_arguments(self, parser):
        """Add the command's options."""
        parser.add_argument("--pages", type=int, default=crawl.DEFAULT_PAGES, help="pages to read this run")
        parser.add_argument("--delay-min", type=float, default=crawl.DEFAULT_DELAY[0])
        parser.add_argument("--delay-max", type=float, default=crawl.DEFAULT_DELAY[1])
        parser.add_argument("--state", default=None, help="state file")
        parser.add_argument("--status", action="store_true", help="only report where the crawl is")
        parser.add_argument("--no-match", action="store_true", help="don't update Where to Watch afterwards")

    def handle(self, *args, **options):  # noqa: ARG002
        """Seed the queue if empty, read a slice, match, and report."""
        path = options["state"] or state_path()
        state = crawl.CrawlState.load(path)

        if options["status"]:
            self.report(state)
            return

        if not crawl.crawl_enabled():
            msg = "The Disney+ crawl is switched off. Set DISNEYPLUS_CRAWL=True to run it."
            raise CommandError(msg)
        if options["delay_min"] < 1 or options["delay_max"] < options["delay_min"]:
            msg = "--delay-min must be at least 1 second, and --delay-max not below it."
            raise CommandError(msg)

        now = datetime.now(UTC)
        try:
            with crawl.crawl_lock(f"{path}.lock", now):
                if not state.titles:
                    seeded = state.seed_from_catalogue(dp.load_snapshot(catalogue_path()), now)
                    state.save(path)  # the seeding itself must survive whatever happens next
                    self.stdout.write(f"Started a new crawl with {seeded} titles from the hub catalogue.")
                wanted = {dp.normalize_title(item.title) for item in tracked_items()}
                if not state.queue:
                    again = state.requeue_stale(now)
                    if again:
                        self.stdout.write(f"Nothing new to read: checking {again} titles last read over a month ago.")
                summary = crawl.run_slice(
                    state,
                    pages=options["pages"],
                    delay=(options["delay_min"], options["delay_max"]),
                    wanted=wanted,
                    save=lambda s: s.save(path),
                )
        except crawl.CrawlLocked as error:
            raise CommandError(str(error)) from error
        except dp.CatalogueError as error:
            state.save(path)
            raise CommandError(str(error)) from error

        self.stdout.write(
            f"Read {summary['pages']} pages ({summary['new_titles']} new titles, "
            f"{summary['gone']} gone, {summary['failed']} failed, "
            f"{summary.get('unplayable', 0)} not watchable here). Stopped: {summary['ended']}.",
        )
        if summary.get("paused_until"):
            self.stdout.write(f"Paused until {summary['paused_until']}.")
        self.report(state)
        if not options["no_match"]:
            self.update_where_to_watch(state)

    def update_where_to_watch(self, state):
        """Match what you track against the hub catalogue plus everything crawled, offline."""
        path = catalogue_path()
        snapshot = dp.load_snapshot(path)
        if not snapshot:
            self.stdout.write("No hub catalogue saved yet (run scrape_disneyplus_za first): Where to Watch not updated.")
            return
        before = snapshot.get("matches") or {}
        wanted = wanted_titles()
        matches = crawl.match_tracked(snapshot, wanted, state, previous=before, delay=0)
        self.stdout.write(f"{len(matches)} of {len(wanted)} tracked titles are on Disney+ ZA.")
        if matches == before:
            return
        snapshot["matches"] = matches
        dp.save_snapshot(snapshot, path)
        changed = refresh_changed(before, matches)
        self.stdout.write(f"Where to Watch updated for {len(changed)} titles.")

    def report(self, state):
        """Say where the crawl stands."""
        counts = state.counts()
        self.stdout.write(
            "Crawl state: "
            + ", ".join(f"{counts[k]} {k}" for k in sorted(counts))
            + (f". Paused until {state.data['paused_until']} ({state.data['pause_reason']})."
               if state.data.get("paused_until") else "."),
        )
