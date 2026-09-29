import csv
import json

from django.db.models import Prefetch, Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render

from . import scoring, services
from .api import ApiError, api_view, json_body, require_login
from .event_export import export_event
from .models import (
    Assignment, Comment, CriterionScore, Event, EventRole, Project, Review, Tag, TeamMembership, Track, Vote,
)

# --- pages -------------------------------------------------------------------


def gallery(request):
    """Public gallery: submitted projects only, with search and event/track/tag filters."""
    q = request.GET.get("q", "").strip()
    event = request.GET.get("event", "").strip()
    track = request.GET.get("track", "").strip()
    tag = request.GET.get("tag", "").strip()

    projects = (
        Project.objects.filter(status=Project.Status.SUBMITTED, withdrawn_at__isnull=True)
        .select_related("track", "team", "event")
        .prefetch_related("tags")
    )
    tracks = Track.objects.select_related("event")
    if q:
        projects = projects.filter(
            Q(title__icontains=q) | Q(tagline__icontains=q) | Q(description__icontains=q) | Q(team__name__icontains=q)
        )
    if event:
        projects = projects.filter(event__external_id=event)
        tracks = tracks.filter(event__external_id=event)
    if track:
        projects = projects.filter(track__external_id=track)
    if tag:
        projects = projects.filter(tags__name=tag)

    return render(
        request,
        "portal/gallery.html",
        {
            "projects": projects.distinct(),
            "events": Event.objects.all(),
            "tracks": tracks,
            "tags": Tag.objects.filter(projects__status=Project.Status.SUBMITTED, projects__withdrawn_at__isnull=True).distinct(),
            "q": q,
            "event": event,
            "track": track,
            "tag": tag,
        },
    )


# --- api ---------------------------------------------------------------------


@api_view(["POST"])
def submit_project(request):
    """Create the caller's team's project as a draft, while the event is open."""
    require_login(request)
    body = json_body(request)
    memberships = TeamMembership.objects.filter(user=request.user).select_related("team__event")
    if body.get("event"):
        memberships = memberships.filter(event__external_id=body["event"])
    memberships = list(memberships)
    if not memberships:
        raise ApiError(403, "not_a_participant", "you are not on a team in this event")
    if len(memberships) > 1:
        raise ApiError(400, "event_required", 'you are on teams in several events; send "event"')
    project = services.create_project(memberships[0].team, request.user, body)
    return JsonResponse({"id": project.external_id, "title": project.title, "status": project.status}, status=201)


@api_view(["GET"])
def judge_scores(request):
    """The logged-in judge's own reviews. Naming any other judge is a 403."""
    require_login(request)
    own_ids = list(
        EventRole.objects.filter(user=request.user, role=EventRole.Role.JUDGE).values_list("external_id", flat=True)
    )
    if not own_ids:
        raise ApiError(403, "not_a_judge")
    named = request.GET.getlist("judge")
    if any(j not in own_ids for j in named):
        raise ApiError(403, "not_your_scores", "judges can only read their own scores")

    reviews = (
        Review.objects.filter(judge=request.user)
        .select_related("project__event")
        .prefetch_related(Prefetch("scores", queryset=CriterionScore.objects.select_related("criterion")))
        .order_by("project__external_id")
    )
    return JsonResponse(
        {
            "judge": own_ids[0],
            "reviews": [
                {
                    "event": r.project.event.external_id,
                    "project": r.project.external_id,
                    "title": r.project.title,
                    "scores": {s.criterion.key: s.value for s in r.scores.all()},
                    "comment": r.comment,
                }
                for r in reviews
            ],
        }
    )


def csv_cell(value):
    """Stop spreadsheet apps from executing cells as formulas."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def csv_response(filename, header, rows):
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    writer = csv.writer(response)
    writer.writerow(header)
    for row in rows:
        writer.writerow([csv_cell(x) for x in row])
    return response


def fmt(x):
    return "" if x is None else round(x, 3)


def organized_events(request):
    """Events the caller organizes, optionally narrowed by ?event=. 401/403 otherwise."""
    require_login(request)
    if request.user.is_superuser:
        events = Event.objects.all()
    else:
        events = Event.objects.filter(roles__user=request.user, roles__role=EventRole.Role.ORGANIZER)
    if request.GET.get("event"):
        events = events.filter(external_id=request.GET["event"])
    events = list(events.distinct().order_by("external_id"))
    if not events:
        raise ApiError(403, "not_an_organizer")
    return events


def organizer_event(request, event_id):
    require_login(request)
    event = Event.objects.filter(external_id=event_id).first()
    # Unknown and not-yours look the same, so ids cannot be probed.
    if event is None or not services.is_organizer(request.user, event):
        raise ApiError(403, "not_an_organizer")
    return event


@api_view(["GET"])
def export_csv(request):
    """One row per review, for every event the caller organizes (or ?event=)."""
    events = organized_events(request)
    keys = sorted({c.key for e in events for c in e.criteria.all()})
    header = [
        "event_id", "project_id", "project_title", "track", "judge_id", "judge_name",
        *keys, "weighted_score", "leniency_adjusted_score", "comment",
    ]

    def rows():
        for event in events:
            weights = {c.key: c.weight for c in event.criteria.all()}
            normalized = services.event_results(event).reviews
            judge_ids = dict(
                EventRole.objects.filter(event=event, role=EventRole.Role.JUDGE).values_list("user_id", "external_id")
            )
            reviews = (
                Review.objects.filter(project__event=event)
                .select_related("judge", "project__track")
                .prefetch_related(Prefetch("scores", queryset=CriterionScore.objects.select_related("criterion")))
                .order_by("project__external_id", "judge__username")
            )
            for r in reviews:
                p = r.project
                values = {s.criterion.key: s.value for s in r.scores.all()}
                yield [
                    event.external_id,
                    p.external_id,
                    p.title,
                    p.track.external_id if p.track else "",
                    judge_ids.get(r.judge_id, ""),
                    r.judge.get_full_name() or r.judge.username,
                    *[values.get(k, "") for k in keys],
                    fmt(scoring.weighted_score(values, weights)),
                    fmt(normalized.get((str(r.judge_id), p.id))),
                    r.comment,
                ]

    return csv_response("scores.csv", header, rows())


@api_view(["GET"])
def results_csv(request, event_id):
    """The bias-corrected ranking with its uncertainty, one row per project. Organizers only."""
    event = organizer_event(request, event_id)
    analysis = services.event_results(event)
    projects = {p.id: p for p in Project.objects.filter(event=event).select_related("team", "track")}
    header = [
        "rank", "project_id", "project_title", "team", "track", "reviews", "reviews_used", "raw_mean", "score",
        "rank_band_low", "rank_band_high", "p_first", "p_prize", "too_close_to_call", "low_confidence",
    ]
    rows = (
        [
            s.rank, projects[s.project].external_id, projects[s.project].title, projects[s.project].team.name,
            projects[s.project].track.external_id if projects[s.project].track else "",
            s.reviews, s.used_reviews, fmt(s.raw_mean), fmt(s.score), s.rank_lo, s.rank_hi,
            fmt(s.p_first), fmt(s.p_top), "yes" if s.contested else "no", "yes" if s.low_confidence else "no",
        ]
        for s in analysis.ranking()
    )
    return csv_response(f"results-{event.external_id}.csv", header, rows)


@api_view(["GET"])
def progress_json(request, event_id):
    """Judging progress for the organizer dashboard, as JSON."""
    event = organizer_event(request, event_id)
    p = services.progress(event)
    return JsonResponse(
        {
            "event": event.external_id,
            "assigned": p["assigned"],
            "done": p["done"],
            "percent": p["percent"],
            "projects_submitted": p["projects_submitted"],
            "projects_draft": p["projects_draft"],
            "unassigned_projects": [x.external_id for x in p["unassigned_projects"]],
            "judges": [
                {"judge": j["role"].external_id, "assigned": j["assigned"], "done": j["done"]} for j in p["judges"]
            ],
            "integrity": {
                "flat_judges": [f["role"].external_id for f in p["integrity"]["flat_judges"] if f["role"]],
                "light_judges": [f["role"].external_id for f in p["integrity"]["light_judges"] if f["role"]],
                "under_reviewed": [u["project"].external_id for u in p["integrity"]["under_reviewed"]],
                "duplicates": [[x.external_id for x in d["projects"]] for d in p["integrity"]["duplicates"]],
                "last_minute": [x["project"].external_id for x in p["integrity"]["last_minute"]],
                "components": p["integrity"]["components"],
            },
        }
    )


# --- CSV at every stage (organizers only) ------------------------------------------


@api_view(["GET"])
def teams_csv(request, event_id):
    """Registration stage: one row per team member."""
    event = organizer_event(request, event_id)
    rows = (
        [m.team.external_id, m.team.name, m.user.email, m.user.get_full_name(), m.joined_at.isoformat()]
        for m in TeamMembership.objects.filter(event=event).select_related("team", "user").order_by("team__external_id", "user__username")
    )
    return csv_response(f"teams-{event.external_id}.csv", ["team_id", "team_name", "member_email", "member_name", "joined_at"], rows)


@api_view(["GET"])
def submissions_csv(request, event_id):
    """Submission stage: every project, drafts included, with the stable fields and answers."""
    event = organizer_event(request, event_id)
    questions = list(event.questions.all())
    projects = (
        Project.objects.filter(event=event)
        .select_related("team", "track")
        .prefetch_related("tags", "answers", "images")
        .order_by("external_id")
    )
    header = [
        "project_id", "team_id", "team_name", "status", "submitted_at", "title", "tagline", "description", "track",
        "tech_tags", "repo_url", "demo_video_url", "live_url", "thumbnail", "gallery_images",
        "withdrawn_at", "withdrawn_reason", *[f"q{q.pk}: {q.prompt}" for q in questions],
    ]

    def rows():
        for p in projects:
            answers = {a.question_id: a.answer for a in p.answers.all()}
            yield [
                p.external_id, p.team.external_id, p.team.name, p.status,
                p.submitted_at.isoformat() if p.submitted_at else "", p.title, p.tagline, p.description,
                p.track.external_id if p.track else "", " ".join(t.name for t in p.tags.all()),
                p.repo_url, p.demo_video_url, p.live_url, p.thumbnail.name if p.thumbnail else "", p.images.count(),
                p.withdrawn_at.isoformat() if p.withdrawn_at else "", p.withdrawn_reason,
                *[answers.get(q.pk, "") for q in questions],
            ]

    return csv_response(f"submissions-{event.external_id}.csv", header, rows())


@api_view(["GET"])
def assignments_csv(request, event_id):
    """Assignment stage: who reviews what, and whether it is done."""
    event = organizer_event(request, event_id)
    judge_ids = dict(EventRole.objects.filter(event=event, role=EventRole.Role.JUDGE).values_list("user_id", "external_id"))
    reviewed = set(Review.objects.filter(project__event=event).values_list("judge_id", "project_id"))
    rows = (
        [a.project.external_id, a.project.title, judge_ids.get(a.judge_id, ""), a.judge.email, a.source,
         a.created_at.isoformat(), "yes" if (a.judge_id, a.project_id) in reviewed else "no"]
        for a in Assignment.objects.filter(event=event).select_related("project", "judge").order_by("project__external_id", "judge__username")
    )
    return csv_response(
        f"assignments-{event.external_id}.csv",
        ["project_id", "project_title", "judge_id", "judge_email", "source", "assigned_at", "reviewed"],
        rows,
    )


@api_view(["GET"])
def export_json(request, event_id):
    """The whole event in fixtures.json's shape, importable with `manage.py seed --fixtures`. Organizers only."""
    event = organizer_event(request, event_id)
    response = HttpResponse(
        json.dumps(export_event(event), indent=2, ensure_ascii=False) + "\n",
        content_type="application/json; charset=utf-8",
    )
    response["Content-Disposition"] = f'attachment; filename="event-{event.external_id}.json"'
    return response


@api_view(["GET"])
def votes_json(request, event_id):
    """People's choice tallies. 401 anonymous and 403 everyone else until voting closes; then public."""
    event = Event.objects.filter(external_id=event_id).first()
    if event is None:
        raise ApiError(404, "not_found", "no such event")
    rows = services.vote_tallies(event, request.user)
    return JsonResponse(
        {
            "event": event.external_id,
            "voting_closed_at": event.voting_closes.isoformat(),
            "results": [
                {"rank": r["rank"], "project": r["project"].external_id, "title": r["project"].title, "votes": r["votes"]}
                for r in rows
            ],
        }
    )


@api_view(["GET"])
def votes_csv(request, event_id):
    """Every vote, one row each, with its integrity flags. Organizers only, and only after voting closes:
    a row per vote is the tally, and the tally is hidden from organizers until then too."""
    event = organizer_event(request, event_id)
    services.require_votes_visible(event, request.user)
    flags = services.vote_integrity_report(event)["flags"]
    rows = (
        [v.project.external_id, v.voter.email, v.created_at.isoformat(), v.ip_hash[:12], " ".join(flags.get(v.id, []))]
        for v in Vote.objects.filter(event=event).select_related("project", "voter").order_by("created_at", "id")
    )
    return csv_response(f"votes-{event.external_id}.csv", ["project_id", "voter_email", "created_at", "ip_hash", "flags"], rows)


@api_view(["GET"])
def comments_csv(request, event_id):
    """Every comment, hidden ones included with who hid them and why."""
    event = organizer_event(request, event_id)
    rows = (
        [c.pk, c.project.external_id, c.author.email, c.created_at.isoformat(), c.body,
         c.hidden_at.isoformat() if c.hidden_at else "", c.hidden_by.email if c.hidden_by else "", c.hidden_reason]
        for c in Comment.objects.filter(project__event=event).select_related("project", "author", "hidden_by")
        .order_by("created_at", "id")
    )
    return csv_response(
        f"comments-{event.external_id}.csv",
        ["comment_id", "project_id", "author_email", "created_at", "body", "hidden_at", "hidden_by", "hidden_reason"],
        rows,
    )


@api_view(["GET"])
def audit_csv(request, event_id):
    """The audit trail, oldest first."""
    event = organizer_event(request, event_id)
    rows = (
        [e.created_at.isoformat(), e.actor.username if e.actor else "", e.action, e.target, json.dumps(e.detail, sort_keys=True)]
        for e in services.visible_audit(event).select_related("actor").order_by("created_at", "id")
    )
    return csv_response(f"audit-{event.external_id}.csv", ["at", "actor", "action", "target", "detail"], rows)

