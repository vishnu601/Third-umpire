import json

from django.core.management.base import BaseCommand, CommandError

from portal.event_export import export_event
from portal.models import Event


class Command(BaseCommand):
    help = "Export one event as JSON in fixtures.json's shape (import it elsewhere with `manage.py seed --fixtures`)."

    def add_arguments(self, parser):
        parser.add_argument("event_id", help="the event's id, e.g. evt_01")
        parser.add_argument("-o", "--output", help="write to this file instead of stdout")

    def handle(self, *args, event_id, output=None, **options):
        event = Event.objects.filter(external_id=event_id).first()
        if event is None:
            raise CommandError(f"no event {event_id!r}")
        text = json.dumps(export_event(event), indent=2, ensure_ascii=False) + "\n"
        if output:
            with open(output, "w", encoding="utf-8") as f:
                f.write(text)
            self.stderr.write(f"wrote {output}")
        else:
            self.stdout.write(text, ending="")
