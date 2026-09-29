"""Organizer tools from the product review: withdraw a duplicate, read judges' comments, add a co-organizer."""

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from portal import services
from portal.models import Assignment, AuditLog, EventRole, Project

from .conftest import JUDGE_A, ORGANIZER, PARTICIPANT, client_as
from .test_t2_pages import world  # noqa: F401 (fixture)

User = get_user_model()


def client_for(user=None):
    c = Client()
    if user:
        c.force_login(user)
    return c


# --- withdraw from judging ------------------------------------------------------------------------


def test_organizer_withdraws_a_duplicate_and_it_leaves_results_and_gallery(seeded):
    before = client_as(ORGANIZER).get("/events/evt_01/results").content.decode()
    assert "prj_07" in before
    resp = client_as(ORGANIZER).post("/events/evt_01/dashboard",
                                     {"action": "withdraw", "project": "prj_07", "reason": "duplicate of prj_41"})
    assert resp.status_code == 302
    project = Project.objects.get(external_id="prj_07")
    assert project.withdrawn_at is not None and project.withdrawn_reason == "duplicate of prj_41"
    assert AuditLog.objects.filter(action="project.withdrawn", target="prj_07").exists()

    event = project.event
    ranked = {r.project for r in services.event_results(event).ranking()}
    assert project.id not in ranked  # the cache key changed with the withdrawal
    assert b"/projects/evt_01/prj_07\"" not in Client().get("/projects?event=evt_01").content

    client_as(ORGANIZER).post("/events/evt_01/dashboard", {"action": "restore", "project": "prj_07"})
    assert project.id in {r.project for r in services.event_results(event).ranking()}


@pytest.mark.parametrize("who, status", [(JUDGE_A, 403), (PARTICIPANT, 403), (None, 302)])
def test_withdraw_wrong_roles(seeded, who, status):
    resp = client_as(who).post("/events/evt_01/dashboard", {"action": "withdraw", "project": "prj_07"})
    assert resp.status_code == status
    assert Project.objects.get(external_id="prj_07").withdrawn_at is None


def test_withdrawn_projects_cannot_be_assigned_or_scored(world):
    event, host, judge, project = world["event"], world["host"], world["judges"][2], world["projects"][0]
    Assignment.objects.create(event=event, judge=judge, project=project)
    services.withdraw_project(event, host, project, "test")
    with pytest.raises(services.Refused) as e:
        services.save_review(project, judge, {c.key: 3 for c in event.criteria.all()}, "")
    assert e.value.code == "withdrawn"
    with pytest.raises(services.Refused):
        services.assign_batch(event, host, judge, [project])
    services.auto_assign(event, host, per_project=3)
    assert not Assignment.objects.filter(project=project).exclude(judge=judge).exists()


# --- judges' comments, organizer only -------------------------------------------------------------


def test_organizer_reads_every_review_of_a_project(seeded):
    resp = client_as(ORGANIZER).get("/events/evt_01/projects/prj_01/reviews")
    assert resp.status_code == 200
    project = Project.objects.get(external_id="prj_01")
    for review in project.reviews.all():
        if review.comment:
            assert review.comment[:30] in resp.content.decode()
    assert b"/events/evt_01/projects/prj_01/reviews" in client_as(ORGANIZER).get("/events/evt_01/results").content


@pytest.mark.parametrize("who, status", [(JUDGE_A, 403), (PARTICIPANT, 403), (None, 302)])
def test_review_list_wrong_roles(seeded, who, status):
    assert client_as(who).get("/events/evt_01/projects/prj_01/reviews").status_code == status


# --- co-organizers --------------------------------------------------------------------------------


def test_organizer_adds_a_co_organizer_by_email(world):
    event, host = world["event"], world["host"]
    co = User.objects.create_user("co@example.org", "co@example.org", "a-long-test-password")
    client_for(host).post(f"/events/{event.external_id}/manage", {"action": "add_organizer", "email": "CO@example.org"})
    assert services.is_organizer(co, event)
    assert AuditLog.objects.filter(event=event, action="role.granted", target="co@example.org").exists()


def test_co_organizer_needs_an_account_and_cannot_compete(world):
    event, host = world["event"], world["host"]
    with pytest.raises(services.Refused) as e:
        services.add_organizer(event, host, "nobody@example.org")
    assert e.value.code == "no_such_account"
    with pytest.raises(services.Refused) as e:
        services.add_organizer(event, host, world["members"][0].email)
    assert e.value.code == "competing"


def test_only_organizers_add_organizers(world):
    event, judge = world["event"], world["judges"][0]
    resp = client_for(judge).post(f"/events/{event.external_id}/manage", {"action": "add_organizer", "email": judge.email})
    assert resp.status_code == 403
    assert not EventRole.objects.filter(event=event, user=judge, role=EventRole.Role.ORGANIZER).exists()


# --- publishing with reviews outstanding ----------------------------------------------------------


def test_publish_warns_when_reviews_are_pending(world):
    event, host = world["event"], world["host"]
    assert "still pending" not in client_for(host).get(f"/events/{event.external_id}/manage").content.decode()
    Assignment.objects.create(event=event, judge=world["judges"][2], project=world["projects"][0])
    assert "1 review still pending" in client_for(host).get(f"/events/{event.external_id}/manage").content.decode()
