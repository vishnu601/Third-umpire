"""Export one event as JSON in the shape of fixtures.json: the way out of the portal.

`export_event(event)` is the inverse of `seed.import_fixture`: same top-level keys (`event`, `tracks`, `judges`,
`teams`, `projects`, `scores`), same field names, the string ids are our `external_id`s, timestamps are ISO 8601 UTC
with a `Z`. `manage.py seed --fixtures <file>` reads the result into another portal.

The organisers' fixture exports as the same data it came in as. Data the fixture has no place for is carried as
optional keys, written only when they hold something, so a fixture-born event exports exactly as it came in and an
app-made event loses as little as possible. `seed.import_fixture` reads them back.

Nothing secret leaves: no password hashes, session keys, team invite tokens, judge invite tokens, IP addresses.
"""

from datetime import timezone as dt_timezone

from .models import EventRole, Project, Review, TeamMembership
from .seed import CRITERION_FIELDS, default_criterion


def iso(value):
    """ISO 8601 in UTC with a Z, as the fixture writes it. Whole seconds stay whole seconds."""
    if value is None:
        return None
    return value.astimezone(dt_timezone.utc).isoformat().replace("+00:00", "Z")


def _email(user):
    return user.email or user.username


def _judge_id(role):
    # Every judge made by an import or an invite has an id; the fallback only guards rows made by hand.
    return role.external_id or f"jdg_u{role.user_id}"


def _event_dict(event):
    out = {"id": event.external_id, "name": event.name, "submissions_close": iso(event.submissions_close)}
    optional = {
        "description": event.description,
        "submissions_open": iso(event.submissions_open),
        "judging_close": iso(event.judging_close),
        "results_published_at": iso(event.results_published_at),
        "reviews_per_project": event.reviews_per_project if event.reviews_per_project != 3 else None,
    }
    out.update({k: v for k, v in optional.items() if v not in (None, "")})
    return out


def _criteria(event, scored_keys):
    """The rubric, but only when the import could not rebuild it from the scores alone."""
    exported = [
        {"key": c.key, **{f: (float(c.weight) if f == "weight" else getattr(c, f)) for f in CRITERION_FIELDS}}
        for c in event.criteria.all()
    ]
    derived = [{"key": key, **default_criterion(key, i)} for i, key in enumerate(sorted(scored_keys))]
    if sorted(exported, key=lambda r: r["key"]) == sorted(derived, key=lambda r: r["key"]):
        return None
    return exported


def _project_dict(p):
    out = {
        "id": p.external_id,
        "team": p.team.external_id,
        "track": p.track.external_id if p.track else None,
        "title": p.title,
        "summary": p.tagline,
        "repo_url": p.repo_url,
        # The import treats a set submitted_at as "submitted", so only a submitted project carries one.
        "submitted_at": iso(p.submitted_at) if p.status == Project.Status.SUBMITTED else None,
    }
    optional = {
        "description": p.description,
        "demo_video_url": p.demo_video_url,
        "live_url": p.live_url,
        "tags": sorted(t.name for t in p.tags.all()),
        "withdrawn_at": iso(p.withdrawn_at),
        "withdrawn_reason": p.withdrawn_reason,
    }
    out.update({k: v for k, v in optional.items() if v not in (None, "", [])})
    return out


def export_event(event):
    """The event as a dict in fixtures.json's shape, ready for `json.dump`."""
    roles = list(
        EventRole.objects.filter(event=event, role=EventRole.Role.JUDGE)
        .select_related("user")
        .prefetch_related("tracks")
        .order_by("external_id", "id")
    )
    judge_ids = {r.user_id: _judge_id(r) for r in roles}

    memberships = {}
    for m in TeamMembership.objects.filter(event=event).select_related("user").order_by("id"):
        memberships.setdefault(m.team_id, []).append(_email(m.user))

    reviews = (
        Review.objects.filter(project__event=event)
        .select_related("project")
        .prefetch_related("scores__criterion")
        .order_by("project__external_id", "judge_id")
    )
    # A review by someone who is no longer a judge of this event has no judge id to point at, so it cannot be carried.
    scores = [
        {
            "judge": judge_ids[r.judge_id],
            "project": r.project.external_id,
            "criteria": {s.criterion.key: s.value for s in sorted(r.scores.all(), key=lambda s: s.criterion.key)},
            "comment": r.comment,
        }
        for r in reviews
        if r.judge_id in judge_ids
    ]

    data = {
        "event": _event_dict(event),
        "tracks": [{"id": t.external_id, "name": t.name} for t in event.tracks.order_by("external_id")],
        "judges": [
            {
                "id": _judge_id(r),
                "name": r.user.get_full_name(),
                "email": _email(r.user),
                "tracks": sorted(t.external_id for t in r.tracks.all()),
            }
            for r in roles
        ],
        "teams": [
            {"id": t.external_id, "name": t.name, "members": memberships.get(t.id, [])}
            for t in event.teams.order_by("external_id")
        ],
        "projects": [
            _project_dict(p)
            for p in event.projects.select_related("team", "track").prefetch_related("tags").order_by("external_id")
        ],
        "scores": scores,
    }
    criteria = _criteria(event, {k for s in scores for k in s["criteria"]})
    if criteria is not None:
        data["criteria"] = criteria
    return data
