"""T1 through the HTML pages: accounts, events, teams, submissions, gallery."""

import io
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.utils import timezone
from PIL import Image

from portal import services
from portal.models import AuditLog, Criterion, Event, EventQuestion, EventRole, Project, Team, TeamMembership

User = get_user_model()


def make_user(email, staff=False, superuser=False):
    return User.objects.create_user(
        username=email, email=email, password="a-long-test-password", is_staff=staff, is_superuser=superuser
    )


def client_for(user=None):
    c = Client()
    if user:
        c.force_login(user)
    return c


def png(size=(8, 8), name="t.png"):
    buf = io.BytesIO()
    Image.new("RGB", size, "teal").save(buf, "PNG")
    return SimpleUploadedFile(name, buf.getvalue(), content_type="image/png")


@pytest.fixture
def media(settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path)
    return tmp_path


@pytest.fixture
def host(db):
    return make_user("host@example.org", staff=True)


@pytest.fixture
def open_event(host):
    return services.create_event(
        host, name="Open Hack", description="", submissions_open=None,
        submissions_close=timezone.now() + timedelta(days=2), judging_close=None, reviews_per_project=3,
    )


def event_form(**over):
    data = {"name": "Spring Hack", "description": "", "submissions_open": "",
            "submissions_close": "2030-01-01T18:00", "judging_close": "", "reviews_per_project": "3"}
    data.update(over)
    return data


# --- accounts ---------------------------------------------------------------------


def test_signup_logs_in_and_rejects_duplicates_and_weak_passwords(db):
    c = Client()
    resp = c.post("/signup", {"name": "Ada", "email": "Ada@Example.org", "password": "correct-horse-battery"})
    assert resp.status_code == 302
    user = User.objects.get(username="ada@example.org")
    assert c.get("/events").wsgi_request.user == user
    again = Client().post("/signup", {"name": "Ada", "email": "ada@example.org", "password": "correct-horse-battery"})
    assert again.status_code == 200 and User.objects.filter(username="ada@example.org").count() == 1
    weak = Client().post("/signup", {"name": "Bo", "email": "bo@example.org", "password": "short"})
    assert weak.status_code == 200 and not User.objects.filter(username="bo@example.org").exists()


def test_login_is_case_insensitive_on_email_and_logout_is_post(db):
    make_user("case@example.org")
    c = Client()
    resp = c.post("/login", {"username": "CASE@example.org", "password": "a-long-test-password"})
    assert resp.status_code == 302
    assert c.post("/logout").status_code == 302
    assert not c.get("/events").wsgi_request.user.is_authenticated


# --- events -------------------------------------------------------------------------


def test_only_hosts_create_events(db, host):
    anon = Client().get("/events/new")
    assert anon.status_code == 302 and anon["Location"].startswith("/login")
    assert client_for(make_user("plain@example.org")).get("/events/new").status_code == 403
    resp = client_for(host).post("/events/new", event_form())
    assert resp.status_code == 302
    event = Event.objects.get(name="Spring Hack")
    assert services.is_organizer(host, event)
    assert list(event.criteria.values_list("key", flat=True)) == ["functionality", "quality", "innovation"]
    assert all(c.anchor_low and c.anchor_high for c in event.criteria.all())


def test_event_dates_are_validated(db, host):
    resp = client_for(host).post("/events/new", event_form(submissions_open="2031-01-01T00:00"))
    assert resp.status_code == 302 and not Event.objects.filter(name="Spring Hack").exists()


@pytest.mark.parametrize("who", ["plain@example.org", None])
def test_manage_is_organizer_only(open_event, who):
    c = client_for(make_user(who)) if who else Client()
    resp = c.get(f"/events/{open_event.external_id}/manage")
    assert resp.status_code == (403 if who else 302)
    c.post(f"/events/{open_event.external_id}/manage", {"action": "add_track", "name": "Sneaky"})
    assert not open_event.tracks.filter(name="Sneaky").exists()


def test_admin_can_manage_any_event(open_event):
    admin = make_user("root@example.org", superuser=True)
    assert client_for(admin).get(f"/events/{open_event.external_id}/manage").status_code == 200


def test_organizer_configures_tracks_prizes_questions_and_weights(open_event, host):
    c = client_for(host)
    url = f"/events/{open_event.external_id}/manage"
    c.post(url, {"action": "add_track", "name": "Tools"})
    track = open_event.tracks.get(name="Tools")
    c.post(url, {"action": "add_prize", "name": "Best tool", "value": "100 USD", "track": track.external_id})
    c.post(url, {"action": "add_question", "prompt": "Which API?", "required": "1"})
    crit = open_event.criteria.get(key="innovation")
    c.post(url, {"action": "weights", f"weight_{crit.pk}": "2.5"})
    c.post(url, {"action": "details", **event_form(name="Renamed", reviews_per_project="4")})
    open_event.refresh_from_db()
    crit.refresh_from_db()
    assert open_event.prizes.get().track == track
    assert open_event.questions.get().required
    assert str(crit.weight) == "2.500"
    assert open_event.name == "Renamed" and open_event.reviews_per_project == 4
    actions = set(AuditLog.objects.filter(event=open_event).values_list("action", flat=True))
    assert {"track.added", "prize.added", "question.added", "rubric.weights_changed", "event.updated"} <= actions
    assert c.get(url).status_code == 200


def test_rubric_is_locked_once_reviews_exist(seeded):
    event = Event.objects.get(external_id="evt_01")
    c = client_for(User.objects.get(username="organizer@example.org"))
    c.post(f"/events/evt_01/manage", {"action": "add_criterion", "name": "Design"})
    assert not Criterion.objects.filter(event=event, key="design").exists()


# --- teams --------------------------------------------------------------------------


def test_team_by_invite_link(open_event):
    alice, bob, carol = (make_user(f"{n}@example.org") for n in ("alice", "bob", "carol"))
    resp = client_for(alice).post(f"/events/{open_event.external_id}/teams", {"name": "Nightshift"})
    team = Team.objects.get(name="Nightshift")
    assert resp["Location"] == f"/teams/{team.external_id}"
    assert services.has_role(alice, open_event, EventRole.Role.PARTICIPANT)
    assert client_for(bob).get(f"/join/{team.invite_token}").status_code == 200
    client_for(bob).post(f"/join/{team.invite_token}")
    assert set(team.members.all()) == {alice, bob}
    # Carol has her own team, so the invite cannot move her.
    client_for(carol).post(f"/events/{open_event.external_id}/teams", {"name": "Other"})
    client_for(carol).post(f"/join/{team.invite_token}")
    assert TeamMembership.objects.get(user=carol).team.name == "Other"
    # Outsiders cannot see the team page (and so not the invite link).
    assert client_for(make_user("eve@example.org")).get(f"/teams/{team.external_id}").status_code == 403


def test_rotated_invite_link_stops_working(open_event):
    alice = make_user("alice@example.org")
    team = services.create_team(open_event, alice, "Nightshift")
    old = team.invite_token
    client_for(alice).post(f"/teams/{team.external_id}", {"action": "rotate_invite"})
    assert client_for(make_user("bob@example.org")).post(f"/join/{old}").status_code == 404


def test_joining_after_the_deadline_is_refused_and_audited(open_event):
    team = services.create_team(open_event, make_user("alice@example.org"), "Nightshift")
    Event.objects.filter(pk=open_event.pk).update(submissions_close=timezone.now() - timedelta(minutes=1))
    resp = client_for(make_user("late@example.org")).post(f"/join/{team.invite_token}")
    assert resp.status_code == 403
    assert team.members.count() == 1
    assert AuditLog.objects.filter(action="deadline.refused", target="team.join").exists()


def test_judges_cannot_compete_in_their_event(open_event):
    judge = make_user("judge@example.org")
    services.grant_role(open_event, judge, EventRole.Role.JUDGE)
    assert client_for(judge).post(f"/events/{open_event.external_id}/teams", {"name": "Inside job"}).status_code == 403
    assert not Team.objects.filter(name="Inside job").exists()


# --- projects -----------------------------------------------------------------------


def project_form(**over):
    data = {"title": "Quiet Hours", "tagline": "Focus timer", "description": "Long text", "track": "",
            "tags": "Python, django, python", "repo_url": "https://example.org/r", "demo_video_url": "",
            "live_url": "", "action": "save"}
    data.update(over)
    return data


@pytest.fixture
def team_setup(open_event, media):
    alice = make_user("alice@example.org")
    team = services.create_team(open_event, alice, "Nightshift")
    q = EventQuestion.objects.create(event=open_event, prompt="Which API?", required=True)
    return open_event, alice, team, q


def test_draft_edit_submit_lifecycle(team_setup):
    event, alice, team, q = team_setup
    c = client_for(alice)
    resp = c.post(f"/teams/{team.external_id}/project", project_form(thumbnail=png()))
    assert resp.status_code == 302
    project = Project.objects.get(team=team)
    assert project.status == Project.Status.DRAFT and project.thumbnail
    assert sorted(project.tags.values_list("name", flat=True)) == ["django", "python"]
    # Drafts are private.
    assert "Quiet Hours" not in Client().get("/projects").content.decode()
    assert Client().get(f"/projects/{event.external_id}/{project.external_id}").status_code == 404
    assert c.get(f"/projects/{event.external_id}/{project.external_id}").status_code == 200
    # A required question blocks submission until answered.
    edit = f"/teams/{team.external_id}/project/{project.external_id}"
    c.post(edit, project_form(action="submit"))
    project.refresh_from_db()
    assert project.status == Project.Status.DRAFT
    c.post(edit, project_form(action="submit", **{f"answer_{q.pk}": "The weather API"}))
    project.refresh_from_db()
    assert project.status == Project.Status.SUBMITTED and project.submitted_at
    assert "Quiet Hours" in Client().get("/projects").content.decode()
    # Still editable after submitting, until the deadline.
    c.post(edit, project_form(title="Quiet Hours 2", live_url="https://example.org/live"))
    project.refresh_from_db()
    assert project.title == "Quiet Hours 2" and project.status == Project.Status.SUBMITTED


def test_every_field_is_stored(team_setup):
    event, alice, team, q = team_setup
    client_for(alice).post(
        f"/teams/{team.external_id}/project",
        project_form(demo_video_url="https://example.org/v", live_url="https://example.org/l",
                     gallery=[png(name="a.png"), png(name="b.png")], **{f"answer_{q.pk}": "Maps"}),
    )
    p = Project.objects.get(team=team)
    assert (p.tagline, p.description, p.repo_url, p.demo_video_url, p.live_url) == (
        "Focus timer", "Long text", "https://example.org/r", "https://example.org/v", "https://example.org/l")
    assert p.images.count() == 2
    assert p.answers.get(question=q).answer == "Maps"


def test_edits_after_the_deadline_are_refused_and_unchanged(team_setup):
    event, alice, team, q = team_setup
    project = services.create_project(team, alice, {"title": "Before"})
    Event.objects.filter(pk=event.pk).update(submissions_close=timezone.now() - timedelta(seconds=1))
    resp = client_for(alice).post(f"/teams/{team.external_id}/project/{project.external_id}", project_form(title="After"))
    assert resp.status_code == 403
    project.refresh_from_db()
    assert project.title == "Before"
    assert AuditLog.objects.filter(action="deadline.refused", target="project.update").exists()


def test_submissions_not_open_yet_are_refused(team_setup):
    event, alice, team, q = team_setup
    Event.objects.filter(pk=event.pk).update(submissions_open=timezone.now() + timedelta(hours=1))
    resp = client_for(alice).post(f"/teams/{team.external_id}/project", project_form())
    assert resp.status_code == 403 and not Project.objects.filter(team=team).exists()


def test_non_members_cannot_edit(team_setup):
    event, alice, team, q = team_setup
    project = services.create_project(team, alice, {"title": "Mine"})
    resp = client_for(make_user("eve@example.org")).post(
        f"/teams/{team.external_id}/project/{project.external_id}", project_form(title="Hijacked"))
    assert resp.status_code == 403
    project.refresh_from_db()
    assert project.title == "Mine"


def test_images_are_validated(team_setup):
    event, alice, team, q = team_setup
    c = client_for(alice)
    fake = SimpleUploadedFile("x.png", b"not an image", content_type="image/png")
    c.post(f"/teams/{team.external_id}/project", project_form(thumbnail=fake))
    assert not Project.objects.filter(team=team).exists()
    c.post(f"/teams/{team.external_id}/project", project_form(gallery=[png(name=f"{i}.png") for i in range(7)]))
    assert not Project.objects.filter(team=team).exists()


def test_bad_urls_are_rejected(team_setup):
    event, alice, team, q = team_setup
    client_for(alice).post(f"/teams/{team.external_id}/project", project_form(repo_url="javascript:alert(1)"))
    assert not Project.objects.filter(team=team).exists()


# --- gallery and demo -----------------------------------------------------------------


def test_gallery_filters_by_tag_and_searches_tagline(seeded):
    p = Project.objects.get(external_id="prj_01")
    tag = services.Tag.objects.create(name="rust")
    p.tags.add(tag)
    body = Client().get("/projects", {"tag": "rust"}).content.decode()
    assert "Glass Signal" in body and "Small Meadow" not in body
    Project.objects.filter(pk=p.pk).update(tagline="A unique zebra tagline")
    assert "Glass Signal" in Client().get("/projects", {"q": "zebra"}).content.decode()


def test_demo_login_only_in_demo_mode(seeded, settings):
    settings.DOGFOOD_SEED_SESSIONS = False
    assert Client().post("/demo/login", {"role": "organizer"}).status_code == 404
    settings.DOGFOOD_SEED_SESSIONS = True
    c = Client()
    resp = c.post("/demo/login", {"role": "organizer"})
    assert resp.status_code == 302 and resp["Location"] == "/events/evt_01/dashboard"
    assert c.get("/events").wsgi_request.user.username == "organizer@example.org"
    assert c.get("/demo/login").status_code == 405


@pytest.mark.parametrize("path", ["/", "/projects", "/events", "/events/evt_01", "/projects/evt_01/prj_01",
                                  "/login", "/signup", "/healthz"])
def test_public_pages_render(seeded, path):
    assert Client().get(path).status_code in (200, 302)
