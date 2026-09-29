"""T2 through pages and APIs: invites, assignment, scoring, dashboard, exports."""

import csv
import io
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

from portal import services
from portal.models import Assignment, AuditLog, Event, EventRole, JudgeInvite, Project, Review

from .conftest import JUDGE_A, ORGANIZER, PARTICIPANT, client_as

User = get_user_model()


def make_user(email, **kw):
    return User.objects.create_user(username=email, email=email, password="a-long-test-password", **kw)


def client_for(user=None):
    c = Client()
    if user:
        c.force_login(user)
    return c


@pytest.fixture
def world(db):
    """An open event with 2 tracks, 4 submitted projects, 3 judges and an organizer."""
    host = make_user("host@example.org", is_staff=True)
    event = services.create_event(
        host, name="Open Hack", description="", submissions_open=None,
        submissions_close=timezone.now() + timedelta(days=1), judging_close=None, reviews_per_project=2,
    )
    t1 = event.tracks.create(external_id="t1", name="Tools")
    t2 = event.tracks.create(external_id="t2", name="Health")
    projects, members = [], []
    for i, track in enumerate([t1, t1, t2, t2]):
        member = make_user(f"m{i}@example.org")
        team = services.create_team(event, member, f"Team {i}")
        p = services.create_project(team, member, {"title": f"Project {i}", "track": track.external_id}, submit=True)
        projects.append(p)
        members.append(member)
    judges = []
    for i, tracks in enumerate([[t1], [t2], []]):
        u = make_user(f"j{i}@example.org")
        role = services.grant_role(event, u, EventRole.Role.JUDGE, external_id=f"j{i}")
        role.tracks.set(tracks)
        judges.append(u)
    return {"event": event, "host": host, "projects": projects, "members": members, "judges": judges}


# --- invites ------------------------------------------------------------------------


def test_invite_flow(world):
    event, host = world["event"], world["host"]
    client_for(host).post(f"/events/{event.external_id}/judges", {"action": "invite", "email": "New.Judge@example.org"})
    invite = JudgeInvite.objects.get(event=event)
    assert invite.email == "new.judge@example.org"
    wrong = make_user("someone@example.org")
    assert client_for(wrong).post(f"/invites/{invite.token}").status_code == 403
    judge = make_user("new.judge@example.org")
    assert client_for(judge).post(f"/invites/{invite.token}").status_code == 302
    assert services.is_judge(judge, event)
    assert EventRole.objects.get(event=event, user=judge, role="judge").external_id.startswith("jdg_")
    assert client_for(make_user("third@example.org")).post(f"/invites/{invite.token}").status_code == 410


def test_competitors_cannot_become_judges(world):
    event = world["event"]
    member = world["members"][0]
    invite = services.invite_judge(event, world["host"], member.email)
    client_for(member).post(f"/invites/{invite.token}")
    assert not services.is_judge(member, event)


def test_only_organizers_invite(world):
    event = world["event"]
    resp = client_for(world["judges"][0]).post(f"/events/{event.external_id}/judges", {"action": "invite", "email": "x@example.org"})
    assert resp.status_code == 403
    assert not JudgeInvite.objects.exists()


# --- assignment -----------------------------------------------------------------------


def test_auto_assign_is_balanced_track_aware_and_idempotent(world):
    event, host = world["event"], world["host"]
    j_tools, j_health, j_general = world["judges"]
    n = services.auto_assign(event, host)
    assert n == 8  # 4 projects x 2 judges
    for p in world["projects"]:
        judges = set(Assignment.objects.filter(project=p).values_list("judge_id", flat=True))
        assert len(judges) == 2
        specialist = j_tools if p.track.external_id == "t1" else j_health
        assert specialist.id in judges
        wrong_track = j_health if p.track.external_id == "t1" else j_tools
        assert wrong_track.id not in judges  # the generalist fills the second slot
    assert services.auto_assign(event, host) == 0


def test_auto_assign_never_assigns_a_judge_their_own_team(world):
    event, host = world["event"], world["host"]
    judge = world["judges"][2]
    project = world["projects"][0]
    # Force the conflict: put the generalist judge on project 0's team directly.
    from portal.models import TeamMembership
    TeamMembership.objects.create(team=project.team, user=judge)
    services.auto_assign(event, host, 3)
    assert not Assignment.objects.filter(judge=judge, project=project).exists()


def test_batch_assign_and_unassign(world):
    event, host = world["event"], world["host"]
    judge = world["judges"][0]
    c = client_for(host)
    c.post(f"/events/{event.external_id}/judges", {"action": "batch", "judge": "j0",
                                                   "projects": [p.external_id for p in world["projects"][:3]]})
    assert Assignment.objects.filter(judge=judge).count() == 3
    a = Assignment.objects.filter(judge=judge).first()
    c.post(f"/events/{event.external_id}/judges", {"action": "unassign", "assignment": a.pk})
    assert Assignment.objects.filter(judge=judge).count() == 2
    # A reviewed assignment cannot be removed.
    a = Assignment.objects.filter(judge=judge).first()
    services.save_review(a.project, judge, {"functionality": 3, "quality": 3, "innovation": 3}, "")
    c.post(f"/events/{event.external_id}/judges", {"action": "unassign", "assignment": a.pk})
    assert Assignment.objects.filter(pk=a.pk).exists()
    assert AuditLog.objects.filter(action="assignments.batch").exists()


def test_batch_assign_refuses_conflicts(world):
    event, host = world["event"], world["host"]
    member = world["members"][1]
    services.grant_role(event, member, EventRole.Role.JUDGE, external_id="jm")
    with pytest.raises(services.Refused) as e:
        services.assign_batch(event, host, member, [world["projects"][1]])
    assert e.value.status == 409


# --- scoring --------------------------------------------------------------------------


def test_judge_scores_an_assigned_project(world):
    event, host = world["event"], world["host"]
    services.auto_assign(event, host)
    judge = world["judges"][0]
    project = Assignment.objects.filter(judge=judge).first().project
    c = client_for(judge)
    url = f"/judge/{event.external_id}/{project.external_id}"
    assert c.get(url).status_code == 200
    c.post(url, {"functionality": "4", "quality": "5", "innovation": "3", "comment": "Nice"})
    review = Review.objects.get(judge=judge, project=project)
    assert {s.criterion.key: s.value for s in review.scores.all()} == {"functionality": 4, "quality": 5, "innovation": 3}
    c.post(url, {"functionality": "2", "quality": "5", "innovation": "3", "comment": "Changed my mind"})
    change = AuditLog.objects.get(action="review.changed")
    assert change.detail["before"]["functionality"] == 4 and change.detail["after"]["functionality"] == 2


def test_judges_cannot_score_what_is_not_theirs(world):
    event, host = world["event"], world["host"]
    services.auto_assign(event, host)
    judge = world["judges"][0]
    other = next(p for p in world["projects"] if not Assignment.objects.filter(judge=judge, project=p).exists())
    url = f"/judge/{event.external_id}/{other.external_id}"
    assert client_for(judge).get(url).status_code == 403
    assert client_for(judge).post(url, {"functionality": "5", "quality": "5", "innovation": "5"}).status_code == 403
    assert not Review.objects.filter(judge=judge, project=other).exists()


def test_scores_out_of_range_are_rejected(world):
    event, host = world["event"], world["host"]
    services.auto_assign(event, host)
    judge = world["judges"][0]
    project = Assignment.objects.filter(judge=judge).first().project
    client_for(judge).post(f"/judge/{event.external_id}/{project.external_id}", {"functionality": "9", "quality": "5", "innovation": "3"})
    assert not Review.objects.filter(judge=judge).exists()


def test_score_page_never_shows_a_peers_review(world):
    event, host = world["event"], world["host"]
    services.auto_assign(event, host)
    project = world["projects"][0]
    a, b = [x.judge for x in Assignment.objects.filter(project=project).select_related("judge")]
    services.save_review(project, a, {"functionality": 1, "quality": 1, "innovation": 1}, "SECRET-PEER-COMMENT")
    body = client_for(b).get(f"/judge/{event.external_id}/{project.external_id}").content.decode()
    assert "SECRET-PEER-COMMENT" not in body
    assert "checked" not in body


def test_judging_closes(world):
    event, host = world["event"], world["host"]
    services.auto_assign(event, host)
    Event.objects.filter(pk=event.pk).update(
        submissions_close=timezone.now() - timedelta(hours=2), judging_close=timezone.now() - timedelta(hours=1))
    judge = world["judges"][0]
    project = Assignment.objects.filter(judge=judge).first().project
    resp = client_for(judge).post(f"/judge/{event.external_id}/{project.external_id}", {"functionality": "3", "quality": "3", "innovation": "3"})
    assert resp.status_code == 403


@pytest.mark.parametrize("who,status", [(PARTICIPANT, 403), (ORGANIZER, 403), (None, 302)])
def test_judge_home_is_for_judges(seeded, who, status):
    assert client_as(who).get("/judge").status_code == status


def test_seeded_judge_sees_only_their_queue(seeded, fixture_data):
    body = client_as(JUDGE_A).get("/judge").content.decode()
    mine = {s["project"] for s in fixture_data["scores"] if s["judge"] == "jdg_01"}
    titles = {p["id"]: p["title"] for p in fixture_data["projects"]}
    for pid, title in titles.items():
        if pid in mine:
            assert f"/judge/evt_01/{pid}" in body
        else:
            assert f"/judge/evt_01/{pid}" not in body


# --- dashboard, integrity, exports ----------------------------------------------------------


def test_integrity_report_finds_the_planted_cases(seeded):
    r = services.integrity_report(Event.objects.get(external_id="evt_01"))
    assert [f["role"].external_id for f in r["flat_judges"]] == ["jdg_07"]
    assert [f["role"].external_id for f in r["light_judges"]] == ["jdg_01", "jdg_23"]
    assert len(r["under_reviewed"]) == 8 and all(u["reviews"] == 2 for u in r["under_reviewed"])
    assert [[p.external_id for p in d["projects"]] for d in r["duplicates"]] == [["prj_07", "prj_41"]]
    assert [x["project"].external_id for x in r["last_minute"]] == ["prj_41"]
    assert r["components"] == 1 and not r["late"]


def test_top_up_assigns_judges_to_under_reviewed_projects(seeded):
    event = Event.objects.get(external_id="evt_01")
    organizer = User.objects.get(username="organizer@example.org")
    n = services.auto_assign(event, organizer)
    assert n == 8
    r = services.integrity_report(event)
    assert all(u["pending"] == 1 for u in r["under_reviewed"])


@pytest.mark.parametrize("path", ["/events/evt_01/dashboard", "/events/evt_01/dashboard/live", "/events/evt_01/audit",
                                  "/events/evt_01/judges", "/events/evt_01/judges/jdg_07"])
def test_organizer_pages_are_organizer_only(seeded, path):
    assert client_as(ORGANIZER).get(path).status_code == 200
    assert client_as(JUDGE_A).get(path).status_code == 403
    assert client_as(PARTICIPANT).get(path).status_code == 403
    assert client_as().get(path).status_code == 302


def test_dashboard_shows_the_planted_cases(seeded):
    body = client_as(ORGANIZER).get("/events/evt_01/dashboard").content.decode()
    for needle in ("jdg_07", "jdg_01", "jdg_23", "prj_41", "prj_10"):
        assert needle in body


def test_progress_api(seeded):
    resp = client_as(ORGANIZER).get("/api/events/evt_01/progress")
    assert resp.status_code == 200
    data = resp.json()
    assert data["integrity"]["flat_judges"] == ["jdg_07"] and data["done"] == 126
    assert client_as(JUDGE_A).get("/api/events/evt_01/progress").status_code == 403
    anon = client_as().get("/api/events/evt_01/progress")
    assert anon.status_code == 401 and "Location" not in anon


@pytest.mark.parametrize("name,rows", [("teams", 91), ("submissions", 41), ("assignments", 126), ("audit", None)])
def test_stage_csv_exports(seeded, name, rows):
    url = f"/api/events/evt_01/{name}.csv"
    resp = client_as(ORGANIZER).get(url)
    assert resp.status_code == 200 and resp["Content-Type"].startswith("text/csv")
    parsed = list(csv.reader(io.StringIO(resp.content.decode())))
    assert "," in resp.content.decode().splitlines()[0]
    if rows is not None:
        assert len(parsed) - 1 == rows
    assert client_as(JUDGE_A).get(url).status_code == 403
    assert client_as(PARTICIPANT).get(url).status_code == 403
    anon = client_as().get(url)
    assert anon.status_code == 401 and "Location" not in anon
    assert client_as(ORGANIZER).get(f"/api/events/nope/{name}.csv").status_code == 403


def test_audit_page_filters(seeded):
    client_as(PARTICIPANT).post("/api/projects", '{"title":"late"}', content_type="application/json")
    body = client_as(ORGANIZER).get("/events/evt_01/audit", {"action": "deadline"}).content.decode()
    assert "deadline.refused" in body
