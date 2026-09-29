import json
from datetime import timedelta

from django.utils import timezone

from portal.models import AuditLog, Event, Project

from .conftest import JUDGE_A, PARTICIPANT, client_as

PROBE = {"title": "dogfood-late-submission-probe", "summary": "probe"}


def post_json(client, body):
    return client.post("/api/projects", json.dumps(body), content_type="application/json")


def test_closed_event_refuses_participant_and_audits_it(seeded):
    resp = post_json(client_as(PARTICIPANT), PROBE)
    assert resp.status_code == 403
    assert resp.json()["error"] == "submissions_closed"
    assert not Project.objects.filter(title=PROBE["title"]).exists()
    assert AuditLog.objects.filter(action="deadline.refused", target="project.create").count() == 1


def open_event():
    Event.objects.update(submissions_close=timezone.now() + timedelta(days=1))


def test_open_event_accepts_participant_as_draft(seeded):
    open_event()
    Project.objects.filter(team__external_id="tm_01").delete()
    resp = post_json(client_as(PARTICIPANT), PROBE)
    assert resp.status_code == 201
    project = Project.objects.get(title=PROBE["title"])
    assert project.status == Project.Status.DRAFT
    assert project.team.external_id == "tm_01"
    assert resp.json()["id"] == project.external_id


def test_team_with_a_project_gets_409(seeded):
    open_event()
    assert post_json(client_as(PARTICIPANT), PROBE).status_code == 409


def test_open_event_still_validates_title(seeded):
    open_event()
    Project.objects.filter(team__external_id="tm_01").delete()
    resp = post_json(client_as(PARTICIPANT), {"summary": "no title"})
    assert resp.status_code == 400


def test_anonymous_gets_401_not_a_redirect(seeded):
    resp = post_json(client_as(), PROBE)
    assert resp.status_code == 401
    assert "Location" not in resp


def test_judge_is_not_a_participant(seeded):
    Event.objects.update(submissions_close=timezone.now() + timedelta(days=1))
    resp = post_json(client_as(JUDGE_A), PROBE)
    assert resp.status_code == 403


def test_form_encoded_post_is_rejected(seeded):
    # JSON-only is what makes skipping the CSRF token safe on this route.
    resp = client_as(PARTICIPANT).post("/api/projects", PROBE)
    assert resp.status_code == 415
