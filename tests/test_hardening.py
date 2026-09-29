"""Fixes from the AgentCheck review: each test pins down one failure it found."""

import io
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.contrib.sessions.models import Session
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import Client
from django.utils import timezone
from PIL import Image

from portal import services
from portal.models import Assignment, Event, EventRole, Project, Review
from portal.scoring import ReviewPoint, analyse

from .conftest import FIXTURES_PATH, JUDGE_A, JUDGE_B, ORGANIZER, PARTICIPANT, client_as
from .test_t1_pages import open_event, host, media, project_form, team_setup  # noqa: F401 (fixtures)
from .test_t2_pages import world  # noqa: F401 (fixture)

User = get_user_model()


def client_for(user=None):
    c = Client()
    if user:
        c.force_login(user)
    return c


def reseed(sessions=True):
    call_command("seed", fixtures=str(FIXTURES_PATH), sessions=sessions, warm=False, verbosity=0)


# --- seeding: demo access is revoked, live data is never reverted ------------------------------


def test_turning_demo_mode_off_revokes_demo_access(seeded):
    reseed(sessions=False)
    assert not Session.objects.filter(session_key__in=[ORGANIZER, JUDGE_A, JUDGE_B, PARTICIPANT]).exists()
    assert client_as(ORGANIZER).get("/api/export.csv?event=evt_01").status_code == 401
    admin = User.objects.get(username="admin@example.org")
    assert not admin.is_superuser and not admin.is_active
    for email in ("organizer@example.org", "priya1@example.org", "admin@example.org"):
        assert not User.objects.get(username=email).has_usable_password()
    resp = Client().post("/login", {"username": "organizer@example.org", "password": "dogfood-demo"})
    assert resp.status_code == 200  # form redisplayed, not logged in


def test_a_password_someone_chose_survives_demo_mode_off(seeded):
    organizer = User.objects.get(username="organizer@example.org")
    organizer.set_password("a-real-chosen-password")
    organizer.save()
    reseed(sessions=False)
    assert User.objects.get(username="organizer@example.org").check_password("a-real-chosen-password")


def test_reseeding_keeps_a_judges_edited_review(seeded):
    event = Event.objects.get(external_id="evt_01")
    judge = EventRole.objects.get(event=event, external_id="jdg_02").user
    review = Review.objects.filter(judge=judge).first()
    services.save_review(review.project, judge, {c.key: 1 for c in event.criteria.all()}, "Changed my mind.")
    reseed()
    review.refresh_from_db()
    assert review.comment == "Changed my mind."
    assert set(review.scores.values_list("value", flat=True)) == {1}


def test_reseeding_keeps_an_organizers_new_dates(seeded):
    event = Event.objects.get(external_id="evt_01")
    now = timezone.now()
    Event.objects.filter(pk=event.pk).update(submissions_open=now, submissions_close=now + timedelta(days=7))
    reseed()  # used to crash on the open-before-close constraint and stop the container booting
    event.refresh_from_db()
    assert event.submissions_close > now


# --- uploads ------------------------------------------------------------------------------------


def gif_with_script(name):
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), "red").save(buf, "GIF")
    return SimpleUploadedFile(name, buf.getvalue() + b"<script>alert(1)</script>", content_type="text/html")


def test_an_image_named_html_is_stored_with_its_real_extension(team_setup):
    event, alice, team, q = team_setup
    client_for(alice).post(f"/teams/{team.external_id}/project", project_form(thumbnail=gif_with_script("x.html")))
    project = Project.objects.get(team=team)
    assert project.thumbnail.name.endswith(".gif")


def test_media_is_served_so_it_cannot_run_script(team_setup):
    event, alice, team, q = team_setup
    client_for(alice).post(f"/teams/{team.external_id}/project", project_form(thumbnail=gif_with_script("x.gif")))
    project = Project.objects.get(team=team)
    resp = Client().get("/media/" + project.thumbnail.name)
    assert resp.status_code == 200
    assert "sandbox" in resp["Content-Security-Policy"]
    assert resp["X-Content-Type-Options"] == "nosniff"


def test_oversized_upload_bodies_are_refused_before_parsing(team_setup):
    event, alice, team, q = team_setup
    big = SimpleUploadedFile("big.png", b"0" * (20 * 1024 * 1024), content_type="image/png")
    resp = client_for(alice).post(f"/teams/{team.external_id}/project", project_form(thumbnail=big))
    assert resp.status_code in (302, 400, 413)
    assert not Project.objects.filter(team=team).exists()


def test_null_answer_is_not_stored_as_text(open_event):
    alice = User.objects.create_user("al@example.org", "al@example.org", "a-long-test-password")
    team = services.create_team(open_event, alice, "T")
    q = open_event.questions.create(prompt="Which API?", required=True)
    with pytest.raises(services.Refused) as e:
        services.create_project(team, alice, {"title": "X", "answers": {str(q.pk): None}}, submit=True)
    assert e.value.code == "answers_required"


# --- access -------------------------------------------------------------------------------------


def test_any_named_other_judge_is_refused(seeded):
    judge_b = client_as(JUDGE_B)
    assert judge_b.get("/api/judge/scores?judge=jdg_01&judge=jdg_02").status_code == 403
    assert judge_b.get("/api/judge/scores?judge=jdg_02").status_code == 200


def test_signup_next_cannot_leave_the_site(db):
    for i, bad in enumerate(("/\\evil.example", "//evil.example", "https://evil.example", "/\\/evil.example")):
        resp = Client().post(f"/signup?next={bad}", {"name": "N", "email": f"n{i}@example.org",
                                                     "password": "a-long-test-password"})
        assert resp.status_code == 302 and resp["Location"] == "/events", bad


@pytest.mark.parametrize("who, status", [(PARTICIPANT, 403), (ORGANIZER, 403), (None, 302)])
def test_score_page_wrong_roles(seeded, who, status):
    review = Review.objects.filter(judge__event_roles__external_id="jdg_02").select_related("project").first()
    resp = client_as(who).get(f"/judge/evt_01/{review.project.external_id}")
    assert resp.status_code == status


def test_results_csv_wrong_roles(seeded):
    assert client_as(PARTICIPANT).get("/api/events/evt_01/results.csv").status_code == 403
    anon = client_as().get("/api/events/evt_01/results.csv")
    assert anon.status_code == 401 and "Location" not in anon


# --- judging rules --------------------------------------------------------------------------------


def test_scores_are_frozen_once_results_are_published(world):
    event, judge, project = world["event"], world["judges"][0], world["projects"][0]
    Assignment.objects.create(event=event, judge=judge, project=project)
    services.save_review(project, judge, {c.key: 3 for c in event.criteria.all()}, "")
    services.set_results_published(event, world["host"], True)
    with pytest.raises(services.Refused) as e:
        services.save_review(project, judge, {c.key: 5 for c in event.criteria.all()}, "")
    assert e.value.code == "results_published"


def test_all_zero_weights_are_refused(world):
    event = world["event"]
    data = {"action": "weights"} | {f"weight_{c.pk}": "0" for c in event.criteria.all()}
    client_for(world["host"]).post(f"/events/{event.external_id}/manage", data)
    assert all(c.weight > 0 for c in event.criteria.all())


def test_a_track_with_prizes_or_judges_cannot_be_deleted(world):
    event = world["event"]
    spare = event.tracks.create(external_id="t3", name="Spare")
    event.prizes.create(name="Best spare", track=spare)
    client_for(world["host"]).post(f"/events/{event.external_id}/manage", {"action": "delete_track", "track": "t3"})
    assert event.tracks.filter(external_id="t3").exists()
    t2 = event.tracks.get(external_id="t2")
    Project.objects.filter(track=t2).update(track=None)
    client_for(world["host"]).post(f"/events/{event.external_id}/manage", {"action": "delete_track", "track": "t2"})
    assert event.tracks.filter(external_id="t2").exists()  # j1 covers it


def test_criterion_key_is_a_valid_slug(world):
    event = world["event"]
    client_for(world["host"]).post(f"/events/{event.external_id}/manage", {"action": "add_criterion", "name": "UX/Design"})
    assert event.criteria.filter(key="uxdesign").exists()


def test_tiebreak_twice_adds_nothing_the_second_time(seeded):
    event = Event.objects.get(external_id="evt_01")
    organizer = User.objects.get(username="organizer@example.org")
    first = services.apply_tiebreaks(event, organizer)
    assert first > 0
    assert services.apply_tiebreaks(event, organizer) == 0


def test_residual_bootstrap_on_an_exact_design_has_no_spread():
    quality = {"A": 1.0, "B": 0.4, "C": -0.3, "D": -1.1}
    leniency = {"H": -0.5, "G": 0.5, "M": 0.0}
    points = [ReviewPoint(j, p, 3 + quality[p] + leniency[j]) for j in leniency for p in quality]
    r = analyse(points, prior_weight=0, draws=50)
    for x in r.ranking():
        assert x.rank_lo == x.rank_hi == x.rank
