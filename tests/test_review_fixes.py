"""Findings from the final five-reviewer pass, each pinned by a test."""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import Client
from django.utils import timezone

from portal import ratelimit, services
from portal.models import AuditLog, Comment, Event

from .conftest import FIXTURES_PATH
from .test_t2_pages import client_for, make_user, world  # noqa: F401 (world is a fixture)
from .test_t3 import _fields, codes, set_window

User = get_user_model()


# --- tallies stay hidden from organizers ------------------------------------------------------------


def test_the_audit_log_does_not_reveal_votes_while_voting_is_open(world):
    event, host = set_window(world["event"]), world["host"]
    voter = make_user("v@example.org")
    services.cast_vote(world["projects"][0], voter)
    page = client_for(host).get(f"/events/{event.external_id}/audit?action=vote").content.decode()
    csv = client_for(host).get(f"/api/events/{event.external_id}/audit.csv").content.decode()
    for body in (page, csv):
        assert "vote.cast" not in body
    Event.objects.filter(pk=event.pk).update(voting_closes=timezone.now() - timedelta(minutes=1))
    assert "vote.cast" in client_for(host).get(f"/api/events/{event.external_id}/audit.csv").content.decode()


def test_organizers_cannot_close_the_vote_early_to_peek(world):
    event, host = set_window(world["event"]), world["host"]
    now = timezone.now()
    for change in ({"voting_closes": now - timedelta(minutes=1)}, {"voting_closes": now + timedelta(minutes=5)},
                   {"voting_opens": now - timedelta(minutes=5)}, {"votes_per_voter": 5},
                   {"voting_opens": None, "voting_closes": None}):
        fields = _fields(event, voting_opens=event.voting_opens, voting_closes=event.voting_closes)
        fields.update(change)
        assert codes(services.update_event, event, host, **fields)[1] == "voting_window_locked", change
    later = event.voting_closes + timedelta(days=1)
    services.update_event(event, host, **_fields(event, voting_opens=event.voting_opens, voting_closes=later))
    assert Event.objects.get(pk=event.pk).voting_closes == later


def test_dashboard_names_flagged_projects_only_after_the_close(world, settings):
    event, host = set_window(world["event"]), world["host"]
    project = world["projects"][0]
    for i in range(services.SHARED_IP_VOTERS):
        services.cast_vote(project, make_user(f"ring{i}@example.org"), ip="203.0.113.9")
    page = client_for(host).get(f"/events/{event.external_id}/dashboard").content.decode()
    card = page.split("Community voting")[1].split("Flags are for a person")[0]
    assert "one address" in card and project.external_id not in card
    Event.objects.filter(pk=event.pk).update(voting_closes=timezone.now() - timedelta(minutes=1))
    page = client_for(host).get(f"/events/{event.external_id}/dashboard").content.decode()
    assert project.external_id in page.split("Community voting")[1].split("Flags are for a person")[0]


def test_new_accounts_are_informational_not_a_warning(world):
    event = set_window(world["event"])
    services.cast_vote(world["projects"][0], make_user("fresh@example.org"))
    report = services.vote_integrity_report(event)
    assert len(report["new_accounts"]) == 1 and report["issues"] == 0


# --- vote allowance ---------------------------------------------------------------------------------


def test_a_vote_on_a_withdrawn_project_is_given_back(world):
    event, host = set_window(world["event"], per_voter=1), world["host"]
    voter = make_user("v@example.org")
    services.cast_vote(world["projects"][0], voter)
    services.withdraw_project(event, host, world["projects"][0], "duplicate")
    assert services.votes_left(voter, event) == 1
    services.cast_vote(world["projects"][1], voter)


# --- comments ---------------------------------------------------------------------------------------


def test_a_hidden_comment_cannot_be_deleted_and_its_text_is_audited(world):
    event, host = set_window(world["event"]), world["host"]
    author = make_user("a@example.org")
    comment = services.post_comment(world["projects"][0], author, "rude words")
    services.hide_comment(comment, host, "abuse")
    assert codes(services.delete_comment, comment, author) == (409, "comment_hidden")
    assert Comment.objects.filter(pk=comment.pk).exists()
    assert AuditLog.objects.get(action="comment.hidden").detail["body"] == "rude words"


# --- rate limits ------------------------------------------------------------------------------------


def test_a_stranger_cannot_lock_a_judge_out(db):
    User.objects.create_user("judge@example.org", "judge@example.org", "a-long-test-password")
    attacker = Client(REMOTE_ADDR="198.51.100.66")
    for i in range(ratelimit.LOGIN_PER_ACCOUNT_AND_IP + 1):
        attacker.post("/login", {"username": "judge@example.org", "password": f"guess-{i}"})
    assert attacker.post("/login", {"username": "judge@example.org", "password": "x"}).status_code == 429
    judge = Client(REMOTE_ADDR="192.0.2.10")
    resp = judge.post("/login", {"username": "judge@example.org", "password": "a-long-test-password"})
    assert resp.status_code == 302


def test_unicode_variants_of_an_email_share_one_login_counter(db):
    c = Client()
    for i in range(ratelimit.LOGIN_PER_ACCOUNT_AND_IP):
        c.post("/login", {"username": "victim@example.org", "password": f"g{i}"})
    variant = "ｖictim@example.org"  # full-width v; Django's login form normalises it to victim@
    assert c.post("/login", {"username": variant, "password": "g"}).status_code == 429


def test_ipv6_addresses_count_per_64_and_a_missing_address_is_still_limited(rf, settings):
    settings.DOGFOOD_PROXY_COUNT = 0
    a = ratelimit.client_ip(rf.get("/", REMOTE_ADDR="2001:db8:1:2:aaaa::1"))
    b = ratelimit.client_ip(rf.get("/", REMOTE_ADDR="2001:db8:1:2:bbbb::9"))
    assert a == b != ratelimit.client_ip(rf.get("/", REMOTE_ADDR="2001:db8:1:3::1"))
    assert ratelimit.client_ip(rf.get("/", REMOTE_ADDR="")) == "unknown"


def test_the_cache_keeps_enough_rate_limit_counters():
    from config import settings as deployed  # tests swap CACHES for locmem; check what ships

    assert deployed.CACHES["default"]["OPTIONS"]["MAX_ENTRIES"] >= 100000


# --- the demo shows voting ------------------------------------------------------------------------------


def test_demo_mode_opens_a_community_vote_on_the_fixture_event(db):
    call_command("seed", fixtures=str(FIXTURES_PATH), sessions=True, warm=False, verbosity=0)
    event = Event.objects.get(external_id="evt_01")
    assert event.voting_phase() == "open"
    opens = event.voting_opens
    call_command("seed", fixtures=str(FIXTURES_PATH), sessions=True, warm=False, verbosity=0)
    assert Event.objects.get(pk=event.pk).voting_opens == opens  # set once, never moved by a reboot


def test_without_demo_mode_the_fixture_event_has_no_vote(db):
    call_command("seed", fixtures=str(FIXTURES_PATH), sessions=False, warm=False, verbosity=0)
    assert Event.objects.get(external_id="evt_01").voting_phase() == "none"


def test_an_open_vote_is_linked_from_the_nav_and_the_project_page(world):
    event = set_window(world["event"])
    project = world["projects"][0]
    body = Client().get(f"/projects/{event.external_id}/{project.external_id}").content.decode()
    assert f'href="/events/{event.external_id}/vote"' in body
    assert 'href="/events/{}/vote"'.format(event.external_id) in Client().get("/projects").content.decode()
