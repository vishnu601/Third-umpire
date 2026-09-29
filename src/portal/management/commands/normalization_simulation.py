"""Ground-truth recovery on the event's own judge-project design."""

from django.core.management.base import BaseCommand, CommandError

from portal import services, simulation
from portal.models import Event


class Command(BaseCommand):
    help = "Simulate events with a known true ranking on this event's design and compare methods."

    def add_arguments(self, parser):
        parser.add_argument("event", nargs="?", default="evt_01")
        parser.add_argument("--runs", type=int, default=300)
        parser.add_argument("--band-runs", type=int, default=40)
        parser.add_argument("--seed", type=int, default=20260929)

    def handle(self, *args, event, runs, band_runs, seed, **options):
        ev = Event.objects.filter(external_id=event).first()
        if ev is None:
            raise CommandError(f"no event {event}")
        points, _ = services._review_points(ev)
        c = simulation.calibrate(points)
        edges = [(p.judge, p.project) for p in points]
        r = simulation.simulate(
            edges, runs=runs, seed=seed, mu=c["mu"], sd_quality=c["sd_quality"], sd_leniency=c["sd_leniency"],
            sd_noise=c["sd_noise"], band_runs=band_runs,
        )
        w = self.stdout.write
        w(f"Design: {len(edges)} reviews from {ev.external_id}. Calibrated from its own fit: "
          f"quality sd={c['sd_quality']:.2f}, leniency sd={c['sd_leniency']:.2f}, noise sd={c['sd_noise']:.2f}, "
          f"mu={c['mu']:.2f}. {runs} simulated events, seed {seed}.")
        w("")
        w(f"{'method':<20} {'Kendall tau':>16} {'true winner found':>18} {'true top-3 found':>17}")
        for name, m in r["methods"].items():
            t, win, top = m["kendall_tau"], m["winner"], m["top3_overlap"]
            w(f"{name:<20} {t['mean']:>9.3f} ± {t['sd']:.3f} {win['mean']:>17.0%} {top['mean']:>17.0%}")
        if r["band_coverage"] is not None:
            w("")
            w(f"80% rank bands contained the true rank {r['band_coverage']:.0%} of the time "
              f"({band_runs} simulated events x {len(set(p for _, p in edges))} projects).")
