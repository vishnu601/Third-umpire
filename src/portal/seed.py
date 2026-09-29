"""Idempotent import of fixtures.json, plus the checker's fixed sessions.

The fixture is input, not our schema: we upsert it into the portal tables by
its string ids, so running this twice changes nothing. Rows people can edit in
the app (event dates, projects, reviews) are only created, never overwritten,
so a reboot can't revert an organizer's, participant's or judge's change.
"""

from datetime import timedelta

from django.contrib.auth import BACKEND_SESSION_KEY, HASH_SESSION_KEY, SESSION_KEY, get_user_model
from django.contrib.sessions.backends.db import SessionStore
from django.contrib.sessions.models import Session
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from django.conf import settings

from .services import DEFAULT_CRITERIA

from .models import (
    Assignment,
    Criterion,
    CriterionScore,
    Event,
    EventRole,
    Project,
    Review,
    Team,
    TeamMembership,
    Track,
)

User = get_user_model()

SEEDED_ORGANIZER_EMAIL = "organizer@example.org"

# Must match [auth] in .dogfood.toml. Keys are session cookie values.
SEEDED_SESSIONS = {
    "org_7f2a": ("organizer", SEEDED_ORGANIZER_EMAIL),
    "jdg_a_91bc": ("judge_a", "jdg_01"),  # fixture judge id
    "jdg_b_44de": ("judge_b", "jdg_02"),
    "prt_2e88": ("participant", "priya1@example.org"),  # team tm_01
}


def user_for_email(email, name=""):
    user, created = User.objects.get_or_create(username=email.lower(), defaults={"email": email})
    if created:
        user.first_name = name[:150]
        user.set_unusable_password()
        user.save()
    return user


@transaction.atomic
def import_fixture(data):
    """Upsert every record in a fixtures.json dict. Returns the Event."""
    ev = data["event"]
    event, _ = Event.objects.get_or_create(
        external_id=ev["id"],
        defaults={"name": ev["name"], "submissions_close": parse_datetime(ev["submissions_close"])},
    )

    tracks = {}
    for t in data.get("tracks", []):
        tracks[t["id"]], _ = Track.objects.update_or_create(
            event=event, external_id=t["id"], defaults={"name": t["name"]}
        )

    # Rubric: every criterion key that appears in any score, equal weights.
    # Existing weights are left alone so an organizer's edits survive reboots.
    keys = sorted({k for s in data.get("scores", []) for k in s["criteria"]})
    anchors = {key: (low, mid, high) for key, _, low, mid, high in DEFAULT_CRITERIA}
    criteria = {}
    for position, key in enumerate(keys):
        low, mid, high = anchors.get(key, ("", "", ""))
        criteria[key], _ = Criterion.objects.get_or_create(
            event=event,
            key=key,
            defaults={
                "name": key.replace("_", " ").capitalize(),
                "position": position,
                "anchor_low": low,
                "anchor_mid": mid,
                "anchor_high": high,
            },
        )

    judges = {}
    for j in data.get("judges", []):
        user = user_for_email(j["email"], j.get("name", ""))
        role, _ = EventRole.objects.update_or_create(
            event=event, user=user, role=EventRole.Role.JUDGE, defaults={"external_id": j["id"]}
        )
        role.tracks.set([tracks[t] for t in j.get("tracks", []) if t in tracks])
        judges[j["id"]] = user

    teams = {}
    for t in data.get("teams", []):
        team, _ = Team.objects.update_or_create(event=event, external_id=t["id"], defaults={"name": t["name"]})
        for email in t.get("members", []):
            user = user_for_email(email)
            # The fixture is the source of truth: move people who changed team.
            TeamMembership.objects.update_or_create(event=event, user=user, defaults={"team": team})
            EventRole.objects.get_or_create(event=event, user=user, role=EventRole.Role.PARTICIPANT)
        teams[t["id"]] = team

    projects = {}
    for p in data.get("projects", []):
        submitted_at = parse_datetime(p["submitted_at"]) if p.get("submitted_at") else None
        projects[p["id"]], _ = Project.objects.get_or_create(
            event=event,
            external_id=p["id"],
            defaults={
                "team": teams[p["team"]],
                "track": tracks.get(p.get("track")),
                "title": p["title"],
                "tagline": p.get("summary", ""),
                "repo_url": p.get("repo_url", ""),
                "status": Project.Status.SUBMITTED if submitted_at else Project.Status.DRAFT,
                "submitted_at": submitted_at,
            },
        )

    for s in data.get("scores", []):
        # A score implies the judge was assigned the project.
        Assignment.objects.get_or_create(
            event=event,
            judge=judges[s["judge"]],
            project=projects[s["project"]],
            defaults={"source": Assignment.Source.IMPORT},
        )
        review, _ = Review.objects.get_or_create(
            judge=judges[s["judge"]], project=projects[s["project"]], defaults={"comment": s.get("comment", "")}
        )
        for key, value in s["criteria"].items():
            CriterionScore.objects.get_or_create(review=review, criterion=criteria[key], defaults={"value": value})

    organizer = user_for_email(SEEDED_ORGANIZER_EMAIL, "Seeded organizer")
    if not organizer.is_staff:
        organizer.is_staff = True  # may host new events
        organizer.save(update_fields=["is_staff"])
    EventRole.objects.get_or_create(event=event, user=organizer, role=EventRole.Role.ORGANIZER)
    return event


SEEDED_ADMIN_EMAIL = "admin@example.org"


def set_demo_passwords(event):
    """Give the seeded people a known password so the login page can be tried.

    Only set once, on accounts that have no password yet, so the session
    hashes (and any password a person chose) survive reboots. Demo mode only.
    """
    admin = user_for_email(SEEDED_ADMIN_EMAIL, "Seeded admin")
    if not (admin.is_superuser and admin.is_active):
        admin.is_superuser = admin.is_staff = admin.is_active = True
        admin.save(update_fields=["is_superuser", "is_staff", "is_active"])
    people = [admin] + [resolve_seeded_user(event, who) for _, who in SEEDED_SESSIONS.values()]
    for user in people:
        if not user.has_usable_password():
            user.set_password(settings.DOGFOOD_DEMO_PASSWORD)
            user.save(update_fields=["password"])
    return [u.username for u in people]


def demo_accounts(event):
    """Everyone demo mode hands a known password or a fixed session to."""
    people = [resolve_seeded_user(event, who) for _, who in SEEDED_SESSIONS.values()]
    admin = User.objects.filter(username=SEEDED_ADMIN_EMAIL).first()
    return people + ([admin] if admin else [])


@transaction.atomic
def revoke_demo_access(event):
    """Undo demo mode when it is turned off, so its public credentials stop working.

    Deletes the fixed sessions, removes the demo password wherever it is still
    the password, and disables the seeded admin. Passwords people chose are kept.
    """
    Session.objects.filter(session_key__in=SEEDED_SESSIONS).delete()
    for user in demo_accounts(event):
        if user.check_password(settings.DOGFOOD_DEMO_PASSWORD):
            user.set_unusable_password()
            user.save(update_fields=["password"])
    User.objects.filter(username=SEEDED_ADMIN_EMAIL).update(is_superuser=False, is_staff=False, is_active=False)


def resolve_seeded_user(event, who):
    """who is an email, or a judge's fixture id."""
    if "@" in who:
        return User.objects.get(username=who.lower())
    return EventRole.objects.get(event=event, role=EventRole.Role.JUDGE, external_id=who).user


@transaction.atomic
def write_seeded_sessions(event):
    """Upsert the fixed session rows. Returns [(label, cookie header)]."""
    expires = timezone.now() + timedelta(days=365)
    printed = []
    for key, (label, who) in SEEDED_SESSIONS.items():
        user = resolve_seeded_user(event, who)
        data = {
            SESSION_KEY: str(user.pk),
            BACKEND_SESSION_KEY: "django.contrib.auth.backends.ModelBackend",
            HASH_SESSION_KEY: user.get_session_auth_hash(),
        }
        Session.objects.update_or_create(
            session_key=key,
            defaults={"session_data": SessionStore().encode(data), "expire_date": expires},
        )
        printed.append((label, f"Cookie: session={key}"))
    return printed


DEMO_ROLES = {
    "organizer": ("Organizer", SEEDED_ORGANIZER_EMAIL),
    "judge_a": ("Judge A (jdg_01)", "jdg_01"),
    "judge_b": ("Judge B (jdg_02)", "jdg_02"),
    "participant": ("Participant (priya1, team tm_01)", "priya1@example.org"),
    "admin": ("Admin", SEEDED_ADMIN_EMAIL),
}


def demo_user(role):
    """The seeded account behind a demo-mode role, or None."""
    if role not in DEMO_ROLES:
        return None
    who = DEMO_ROLES[role][1]
    if "@" in who:
        return User.objects.filter(username=who).first()
    r = EventRole.objects.filter(role=EventRole.Role.JUDGE, external_id=who).select_related("user").first()
    return r.user if r else None


def demo_home(role):
    """Where each demo role starts, so a role switch never lands on a 403."""
    event = Event.objects.filter(external_id="evt_01").first()
    if role == "participant":
        m = TeamMembership.objects.filter(user__username="priya1@example.org").select_related("team").first()
        return f"/teams/{m.team.external_id}" if m else "/projects"
    if role in ("judge_a", "judge_b"):
        return "/judge"
    if role == "organizer" and event:
        return f"/events/{event.external_id}/dashboard"
    return "/events"

