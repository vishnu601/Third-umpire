"""Raw vs bias-corrected results for an event, as plain text for JUDGING.md."""

import statistics

from django.core.management.base import BaseCommand, CommandError

from portal import scoring, services
from portal.models import Event, EventRole, Project


class Command(BaseCommand):
    help = "Print raw vs bias-corrected rankings, judge leniency and uncertainty for an event."

    def add_arguments(self, parser):
        parser.add_argument("event", nargs="?", default="evt_01")
        parser.add_argument("--top", type=int, default=12)

    def handle(self, *args, event, top, **options):
        ev = Event.objects.filter(external_id=event).first()
        if ev is None:
            raise CommandError(f"no event {event}")
        points, _ = services._review_points(ev)
        a = services.event_results(ev)
        names = {p.id: p.external_id for p in Project.objects.filter(event=ev)}
        judges = {str(r.user_id): r.external_id for r in EventRole.objects.filter(event=ev, role="judge")}
        w = self.stdout.write

        mean = scoring.mean_scores(points)
        z = scoring.zscore_scores(points)
        order = lambda s: sorted(s, key=lambda p: (-s[p], str(p)))
        raw_rank = {p: i for i, p in enumerate(order(mean), 1)}
        z_rank = {p: i for i, p in enumerate(order(z), 1)}
        model_order = [x.project for x in a.ranking()]

        w(f"Event {ev.external_id}: {len(points)} reviews, {len(a.judges)} judges, {len(a.projects)} projects, "
          f"{a.components} connected component(s)")
        w(f"Two-way model: mu={a.mu:.3f}, residual sd={a.residual_sd:.3f}, leniency prior={a.prior_weight} "
          f"pseudo-reviews, {a.draws} bootstrap draws, prize places={a.prize_slots}")
        flat = [judges.get(j, j) for j, e in a.judges.items() if e.flat]
        w(f"Flat judges excluded from the fit: {', '.join(flat) or 'none'}")

        raw_means = [e.raw_mean for e in a.judges.values()]
        adjusted = {}
        for (j, p), v in a.reviews.items():
            if v is not None:
                adjusted.setdefault(j, []).append(v)
        w(f"Spread of judges' average given score: raw sd={statistics.pstdev(raw_means):.3f}, "
          f"after removing leniency sd={statistics.pstdev([statistics.fmean(v) for v in adjusted.values()]):.3f}")

        lens = sorted(((e.leniency, judges.get(j, j), e.reviews) for j, e in a.judges.items() if e.leniency is not None))
        w("Harshest judges:  " + ", ".join(f"{n} {l:+.2f} (n={k})" for l, n, k in lens[:4]))
        w("Most lenient:     " + ", ".join(f"{n} {l:+.2f} (n={k})" for l, n, k in lens[::-1][:4]))
        w("")
        w(f"{'model':>5} {'raw':>4} {'z':>4}  {'project':<8} {'score':>5} {'raw':>5} {'n':>3} {'band':>7} {'P1':>5} {'Ptop':>5}")
        for s in a.ranking()[:top]:
            p = s.project
            w(f"{s.rank:>5} {raw_rank[p]:>4} {z_rank[p]:>4}  {names[p]:<8} {s.score:>5.2f} {s.raw_mean:>5.2f} "
              f"{s.used_reviews:>3} {s.rank_lo:>3}-{s.rank_hi:<3} {s.p_first:>5.0%} {s.p_top:>5.0%}"
              + ("  too close" if s.contested else ""))
        moves = sorted(a.projects, key=lambda p: -abs(raw_rank[p] - a.projects[p].rank))[:5]
        w("")
        w("Largest moves, raw mean -> model: " + ", ".join(f"{names[p]} {raw_rank[p]}->{a.projects[p].rank}" for p in moves))
        w(f"Kendall tau vs model: raw mean {scoring.kendall_tau(order(mean), model_order):.2f}, "
          f"z-score {scoring.kendall_tau(order(z), model_order):.2f}")
        contested = [names[s.project] for s in a.ranking() if s.contested]
        w(f"Too close to call for the top {a.prize_slots}: {', '.join(contested) or 'none'}")
