"""Claims in THREAT-MODEL.md that used to rest on inspection or a one-off probe, now pinned by tests."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from portal import services
from portal.models import Assignment, EventRole, TeamMembership

from .conftest import ORGANIZER, client_as
from .test_t2_pages import world  # noqa: F401 (fixture)

User = get_user_model()
HOSTILE = '<script>alert("x")</script>'


def client_for(user=None, **kw):
    c = Client(**kw)
    if user:
        c.force_login(user)
    return c


# --- T03 CSRF ---------------------------------------------------------------------------------------


def test_html_forms_refuse_a_post_without_a_csrf_token(world):
    event, host = world["event"], world["host"]
    strict = client_for(host, enforce_csrf_checks=True)
    resp = strict.post(f"/events/{event.external_id}/manage", {"action": "add_track", "name": "Forged"})
    assert resp.status_code == 403
    assert not event.tracks.filter(name="Forged").exists()


def test_the_api_refuses_bodies_a_cross_site_form_could_send(seeded):
    for content_type in ("text/plain", "application/x-www-form-urlencoded", "multipart/form-data; boundary=x"):
        resp = client_as(ORGANIZER).generic("POST", "/api/projects", '{"title": "x"}', content_type=content_type)
        assert resp.status_code == 415, content_type


# --- T17 open redirect --------------------------------------------------------------------------------


def test_login_next_cannot_leave_the_site(db):
    User.objects.create_user("n@example.org", "n@example.org", "a-long-test-password")
    for bad in ("//evil.example", "https://evil.example", "/\\evil.example", "/\\/evil.example"):
        resp = Client().post(f"/login?next={bad}", {"username": "n@example.org", "password": "a-long-test-password"})
        assert resp.status_code == 302, bad
        assert "evil.example" not in resp["Location"], bad


# --- T09 stored XSS -----------------------------------------------------------------------------------


def test_hostile_text_is_escaped_everywhere_it_is_shown(world):
    event, host, member = world["event"], world["host"], world["members"][0]
    project = world["projects"][0]
    services.update_project(project, member, {"title": HOSTILE, "tagline": HOSTILE, "description": HOSTILE,
                                              "track": project.track.external_id}, submit=True)
    judge = world["judges"][2]
    Assignment.objects.create(event=event, judge=judge, project=project)
    services.save_review(project, judge, {c.key: 3 for c in event.criteria.all()}, HOSTILE)
    pages = [
        (Client(), "/projects"),
        (Client(), f"/projects/{event.external_id}/{project.external_id}"),
        (client_for(host), f"/events/{event.external_id}/dashboard"),
        (client_for(host), f"/events/{event.external_id}/projects/{project.external_id}/reviews"),
        (client_for(host), f"/events/{event.external_id}/audit"),
        (client_for(host), f"/teams/{project.team.external_id}"),
    ]
    for c, url in pages:
        body = c.get(url).content.decode()
        assert HOSTILE not in body, url


# --- T07 conflict of interest ---------------------------------------------------------------------------


def test_save_review_refuses_a_judge_on_the_projects_team(world):
    event, judge, project = world["event"], world["judges"][0], world["projects"][0]
    # the services never let this happen; build it by hand to prove the last check holds on its own
    TeamMembership.objects.create(team=project.team, user=judge)
    Assignment.objects.create(event=event, judge=judge, project=project)
    with pytest.raises(services.Refused) as e:
        services.save_review(project, judge, {c.key: 5 for c in event.criteria.all()}, "")
    assert e.value.code == "conflict_of_interest"


def test_a_judge_cannot_join_a_team_in_their_event(world):
    with pytest.raises(services.Refused) as e:
        services.join_team(world["projects"][0].team, world["judges"][0])
    assert e.value.code == "judges_cannot_compete"


def test_organizers_cannot_compete_in_their_own_event(world):
    event, host = world["event"], world["host"]
    with pytest.raises(services.Refused) as e:
        services.create_team(event, host, "Home team")
    assert e.value.code == "organizers_cannot_compete"
    with pytest.raises(services.Refused) as e:
        services.join_team(world["projects"][0].team, host)
    assert e.value.code == "organizers_cannot_compete"
    resp = client_for(host).post(f"/events/{event.external_id}/teams", {"name": "Home team"})
    assert resp.status_code == 403
    assert not TeamMembership.objects.filter(event=event, user=host).exists()


def test_an_organizer_of_one_event_can_compete_in_another(world):
    other_host = User.objects.create_user("h2@example.org", "h2@example.org", "a-long-test-password", is_staff=True)
    other = services.create_event(other_host, name="Other", description="", submissions_open=None,
                                  submissions_close=world["event"].submissions_close, judging_close=None,
                                  reviews_per_project=3)
    assert services.create_team(other, world["host"], "Visitors").event == other
    assert EventRole.objects.filter(event=other, user=world["host"], role=EventRole.Role.PARTICIPANT).exists()


# --- T10 uploads -----------------------------------------------------------------------------------------


def test_media_does_not_serve_files_outside_the_media_root(db, settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path / "media")
    (tmp_path / "media").mkdir()
    (tmp_path / "secret.txt").write_text("secret")
    for path in ("/media/../secret.txt", "/media/%2e%2e/secret.txt", "/media/..%2fsecret.txt"):
        resp = Client().get(path)
        assert resp.status_code in (400, 404), path
        assert b"secret" not in b"".join(getattr(resp, "streaming_content", [resp.content])), path


# --- T04 cookies under HTTPS ------------------------------------------------------------------------------


def test_https_mode_turns_on_secure_cookies_and_hsts():
    src = Path(__file__).resolve().parent.parent / "src"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("DJANGO_", "DOGFOOD_"))}
    env |= {"DJANGO_SECRET_KEY": "k" * 50, "DJANGO_HTTPS": "1"}
    code = ("import config.settings as s; print(s.SESSION_COOKIE_SECURE, s.CSRF_COOKIE_SECURE, "
            "s.SECURE_PROXY_SSL_HEADER[0], s.SECURE_HSTS_SECONDS > 0, s.SESSION_COOKIE_HTTPONLY)")
    out = subprocess.run([sys.executable, "-c", code], cwd=src, env=env, capture_output=True, text=True)
    assert out.stdout.split() == ["True", "True", "HTTP_X_FORWARDED_PROTO", "True", "True"], out.stderr
