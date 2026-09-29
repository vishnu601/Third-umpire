import json

from django.conf import settings
from django.core.management.base import BaseCommand

from portal import services
from portal.seed import import_fixture, set_demo_passwords, write_seeded_sessions


class Command(BaseCommand):
    help = "Import fixtures.json (idempotent) and optionally write the checker's test sessions."

    def add_arguments(self, parser):
        parser.add_argument("--fixtures", default=settings.DOGFOOD_FIXTURES)
        parser.add_argument(
            "--sessions",
            action="store_true",
            default=settings.DOGFOOD_SEED_SESSIONS,
            help="write the fixed sessions from .dogfood.toml (demo only)",
        )
        parser.add_argument("--no-warm", dest="warm", action="store_false", help="skip precomputing results")

    def handle(self, *args, fixtures, sessions, warm=True, **options):
        with open(fixtures, encoding="utf-8") as f:
            event = import_fixture(json.load(f))
        self.stdout.write(f"imported {fixtures} into event {event.external_id} ({event.name})")
        if warm:
            services.event_results(event)  # precompute so the first results page view is instant

        if not sessions:
            return
        people = set_demo_passwords(event)
        self.stdout.write("seeded. test logins:")
        for label, header in write_seeded_sessions(event):
            self.stdout.write(f"  {label:<12} {header}")
        self.stdout.write(f"login page accounts (password from DOGFOOD_DEMO_PASSWORD): {', '.join(people)}")
