"""Recompute every audit hash chain; exit 1 if any is broken."""

from django.core.management.base import BaseCommand, CommandError

from portal import services
from portal.models import Event


class Command(BaseCommand):
    help = "Recompute every event's audit hash chain. Exits 1 if any chain is broken."

    def handle(self, *args, **options):
        broken = 0
        for event in [None, *Event.objects.order_by("id")]:
            report = services.verify_audit_chain(event)
            name = event.external_id if event else "(no event)"
            if report["ok"]:
                if options["verbosity"]:
                    self.stdout.write(f"{name}: ok, {report['entries']} entries, head {report['head']}")
            else:
                broken += 1
                self.stderr.write(f"{name}: BROKEN at entry #{report['broken_at']} ({report['reason']})")
        if broken:
            raise CommandError(f"{broken} audit chain(s) broken")
