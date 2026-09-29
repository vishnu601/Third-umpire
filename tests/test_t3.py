"""T3: community voting, randomized ballots, hidden tallies, comments, anti-abuse."""

import csv
import io
import re
from datetime import timedelta

import pytest
from django.contrib.auth.models import AnonymousUser
from django.db import IntegrityError
from django.utils import timezone

from portal import services
from portal.models import AuditLog, Comment, Event, Project, Vote
from portal.services import Refused

from .test_t2_pages import client_for, make_user, world  # noqa: F401  (world is a fixture)

HOUR = timedelta(hours=1)


def set_window(event, state="open", per_voter=3):
    """Move the event's clocks: submissions closed, voting before / open / after now."""
    now = timezone.now()
    opens, closes = {
        "before": (now + HOUR, now + 2 * HOUR),
        "open": (now - HOUR, now + HOUR),
        "after": (now - 2 * HOUR, now - HOUR),
    }[state]
    Event.objects.filter(pk=event.pk).update(
        submissions_close=now - 3 * HOUR, voting_opens=opens, voting_closes=closes, votes_per_voter=per_voter
    )
    event.refresh_from_db()
    return event


def codes(fn, *args, **kw):
    """Run a service call that must be refused; return (status, code)."""
    with pytest.raises(Refused) as e:
        fn(*args, **kw)
    return e.value.status, e.value.code


@pytest.fixture
def voter(db):
    return make_user("voter@example.org")


def actions(event, prefix):
    return list(AuditLog.objects.filter(event=event, action__startswith=prefix).values_list("action", flat=True))


# --- event fields ---------------------------------------------------------------------


def _fields(event, **over):
    f = {
        "name": event.name, "description": "", "submissions_open": None, "submissions_close": event.submissions_close,
        "judging_close": None, "reviews_per_project": 2, "voting_opens": None, "voting_closes": None, "votes_per_voter": 3,
    }
    f.update(over)
    return f


def test_voting_window_validation(world):
    event, host = world["event"], world["host"]
    close = event.submissions_close
    ok = _fields(event, voting_opens=close + HOUR, voting_closes=close + 2 * HOUR, votes_per_voter=5)
    services.update_event(event, host, **ok)
    event.refresh_from_db()
    assert event.voting_phase() == "not_open" and event.votes_per_voter == 5
    assert any(a == "event.updated" for a in actions(event, "event"))
    bad = [
        _fields(event, voting_opens=close + HOUR),  # one date only
        _fields(event, voting_closes=close + HOUR),
        _fields(event, voting_opens=close + 2 * HOUR, voting_closes=close + HOUR),  # backwards
        _fields(event, voting_opens=close - HOUR, voting_closes=close + HOUR),  # opens before submissions close
        _fields(event, votes_per_voter=0),
        _fields(event, votes_per_voter=21),
    ]
    for fields in bad:
        status, _ = codes(services.update_event, event, host, **fields)
        assert status == 400


def test_event_form_sets_voting_window_and_wrong_role_cannot(world):
    event, host = world["event"], world["host"]
    close = (event.submissions_close + HOUR).strftime("%Y-%m-%dT%H:%M")
    end = (event.submissions_close + 2 * HOUR).strftime("%Y-%m-%dT%H:%M")
    form = {
        "action": "details", "name": "Open Hack", "description": "", "submissions_open": "",
        "submissions_close": event.submissions_close.strftime("%Y-%m-%dT%H:%M"), "judging_close": "",
        "reviews_per_project": "2", "voting_opens": close, "voting_closes": end, "votes_per_voter": "4",
    }
    url = f"/events/{event.external_id}/manage"
    assert client_for(world["judges"][0]).post(url, form).status_code == 403
    assert client_for(world["members"][0]).post(url, form).status_code == 403
    assert client_for(host).post(url, form).status_code == 302
    event.refresh_from_db()
    assert event.votes_per_voter == 4 and event.voting_opens is not None
    page = client_for(host).get(url).content.decode()
    assert 'name="voting_opens"' in page and 'name="votes_per_voter"' in page


# --- casting and retracting ------------------------------------------------------------


def test_voting_needs_login(world):
    set_window(world["event"])
    assert codes(services.cast_vote, world["projects"][0], AnonymousUser())[0] == 401
    resp = client_for().post(f"/events/{world['event'].external_id}/vote/{world['projects'][0].external_id}/cast")
    assert resp.status_code == 302 and "/login" in resp["Location"]
    assert not Vote.objects.exists()


def test_window_is_enforced_on_the_server_clock(world, voter):
    event, p = world["event"], world["projects"][0]
    for state in ("before", "after"):
        set_window(event, state)
        p.refresh_from_db()
        assert codes(services.cast_vote, p, voter) == (403, "voting_closed")
    set_window(event, "open")
    services.cast_vote(Project.objects.get(pk=p.pk), voter)
    assert Vote.objects.count() == 1


def test_no_window_means_no_voting(world, voter):
    # the fixture event has no community vote at all
    assert codes(services.cast_vote, world["projects"][0], voter) == (403, "voting_closed")
    resp = client_for(voter).get(f"/events/{world['event'].external_id}/vote")
    assert resp.status_code == 404


def test_cannot_vote_for_your_own_team(world):
    set_window(world["event"])
    member = world["members"][0]
    assert codes(services.cast_vote, world["projects"][0], member) == (403, "own_project")
    services.cast_vote(world["projects"][1], member)  # another team's project is fine
    assert Vote.objects.filter(voter=member).count() == 1


def test_judges_and_organizers_cannot_vote(world):
    set_window(world["event"])
    for who in (world["judges"][0], world["host"]):
        assert codes(services.cast_vote, world["projects"][0], who) == (403, "conflict_of_interest")
    assert not Vote.objects.exists()


def test_only_submitted_unwithdrawn_projects_take_votes(world, voter):
    event = set_window(world["event"])
    p = world["projects"][0]
    services.withdraw_project(event, world["host"], p, "duplicate")
    p.refresh_from_db()
    assert codes(services.cast_vote, p, voter) == (409, "withdrawn")
    Project.objects.filter(pk=world["projects"][1].pk).update(status="draft")
    assert codes(services.cast_vote, Project.objects.get(pk=world["projects"][1].pk), voter)[0] == 404


def test_votes_per_voter_limit_and_retract_frees_a_vote(world, voter):
    event = set_window(world["event"], per_voter=2)
    a, b, c = world["projects"][:3]
    services.cast_vote(a, voter)
    services.cast_vote(b, voter)
    assert codes(services.cast_vote, c, voter) == (409, "no_votes_left")
    services.retract_vote(a, voter)
    services.cast_vote(c, voter)
    assert set(Vote.objects.filter(voter=voter).values_list("project_id", flat=True)) == {b.id, c.id}
    assert services.votes_left(voter, event) == 0


def test_second_vote_on_a_project_is_refused_and_the_database_agrees(world, voter):
    set_window(world["event"])
    p = world["projects"][0]
    services.cast_vote(p, voter)
    assert codes(services.cast_vote, p, voter) == (409, "already_voted")
    with pytest.raises(IntegrityError):
        Vote.objects.create(event=p.event, project=p, voter=voter)


def test_retract_rules(world, voter):
    event = set_window(world["event"])
    p = world["projects"][0]
    assert codes(services.retract_vote, p, voter) == (404, "no_such_vote")
    assert codes(services.retract_vote, p, AnonymousUser())[0] == 401
    services.cast_vote(p, voter)
    services.retract_vote(p, voter)
    assert not Vote.objects.exists()
    services.cast_vote(p, voter)
    set_window(event, "after")
    assert codes(services.retract_vote, Project.objects.get(pk=p.pk), voter) == (403, "voting_closed")
    assert Vote.objects.count() == 1


def test_the_ip_is_stored_only_as_a_salted_hash(world, voter, settings):
    set_window(world["event"])
    services.cast_vote(world["projects"][0], voter, ip="203.0.113.9")
    vote = Vote.objects.get()
    assert len(vote.ip_hash) == 64 and "203.0.113.9" not in vote.ip_hash
    assert vote.ip_hash == services.ip_hash("203.0.113.9") != services.ip_hash("203.0.113.10")
    entry = AuditLog.objects.get(action="vote.cast")
    assert entry.target == world["projects"][0].external_id and "203.0.113.9" not in str(entry.detail)
    settings.SECRET_KEY = "another-secret"
    assert services.ip_hash("203.0.113.9") != vote.ip_hash  # salted


def test_vote_rate_limits(world, voter):
    set_window(world["event"])
    p = world["projects"][0]
    services.cast_vote(p, voter)
    for _ in range(29):
        assert codes(services.cast_vote, p, voter)[1] == "already_voted"
    assert codes(services.cast_vote, p, voter) == (429, "rate_limited")


def test_vote_rate_limit_per_address(world):
    set_window(world["event"])
    p = world["projects"][0]
    users = [make_user(f"ip{i}@example.org") for i in range(121)]
    for u in users[:120]:
        codes(services.retract_vote, p, u, ip="198.51.100.7")  # 404 no_such_vote, but each call is counted
    assert codes(services.retract_vote, p, users[120], ip="198.51.100.7") == (429, "rate_limited")


def test_vote_audit_trail(world, voter):
    event = set_window(world["event"])
    p = world["projects"][0]
    services.cast_vote(p, voter)
    services.retract_vote(p, voter)
    assert actions(event, "vote") == ["vote.retracted", "vote.cast"]  # newest first


# --- the ballot ------------------------------------------------------------------------


def _many_projects(world, n=10):
    """Grow the world to n submitted projects (submissions must still be open, so add them first)."""
    event = world["event"]
    for i in range(4, n):
        member = make_user(f"extra{i}@example.org")
        team = services.create_team(event, member, f"Extra {i}")
        world["projects"].append(services.create_project(team, member, {"title": f"Project {i}"}, submit=True))


def _order_on_page(client, event):
    html = client.get(f"/events/{event.external_id}/vote").content.decode()
    return re.findall(r'/projects/[^/]+/(prj_\w+)"', html)


def test_ballot_order_is_stable_per_voter_and_differs_between_voters(world):
    _many_projects(world)
    event = set_window(world["event"])
    a, b, c = (make_user(f"v{i}@example.org") for i in range(3))
    orders = {u.pk: _order_on_page(client_for(u), event) for u in (a, b, c)}
    assert all(len(o) == 10 for o in orders.values())
    assert len({tuple(o) for o in orders.values()}) > 1  # different voters, different orders
    assert _order_on_page(client_for(a), event) == orders[a.pk]  # same voter, same order on reload
    ids = sorted(p.external_id for p in world["projects"])
    assert ids != orders[a.pk] or ids != orders[b.pk]  # not just the database order


def test_ballot_page_shows_state_and_takes_votes(world, voter):
    event = set_window(world["event"], per_voter=2)
    own = world["members"][0]
    url = f"/events/{event.external_id}/vote"
    assert "/login" in client_for().get(url)["Location"]
    page = client_for(own).get(url).content.decode()
    assert "Your team's project" in page and "of 2 votes left" in page
    c = client_for(voter)
    p1, p2, p3 = world["projects"][1:4]
    assert c.post(f"{url}/{p1.external_id}/cast").status_code == 302
    assert Vote.objects.filter(voter=voter, project=p1).exists()
    resp = c.post(f"{url}/{p1.external_id}/cast", follow=True)  # a second vote is flashed, not a 500
    assert "already voted" in resp.content.decode()
    c.post(f"{url}/{p2.external_id}/cast")
    resp = c.post(f"{url}/{p3.external_id}/cast", follow=True)
    assert "used all 2" in resp.content.decode() and Vote.objects.filter(voter=voter).count() == 2
    page = c.get(url).content.decode()
    assert "Take back" in page and "csrfmiddlewaretoken" in page
    assert c.post(f"{url}/{p1.external_id}/retract").status_code == 302
    assert Vote.objects.filter(voter=voter).count() == 1
    assert c.get(f"{url}/{p1.external_id}/cast").status_code == 405  # POST only


def test_ballot_for_judges_and_organizers_has_no_vote_buttons(world):
    event = set_window(world["event"])
    for who in (world["judges"][0], world["host"]):
        page = client_for(who).get(f"/events/{event.external_id}/vote").content.decode()
        assert "cannot vote" in page and ">Vote</button>" not in page
    resp = client_for(world["judges"][0]).post(f"/events/{event.external_id}/vote/{world['projects'][0].external_id}/cast")
    assert resp.status_code == 403


def test_retract_route_wrong_roles(world, voter):
    event = set_window(world["event"])
    p = world["projects"][0]
    services.cast_vote(p, voter)
    url = f"/events/{event.external_id}/vote/{p.external_id}/retract"
    assert "/login" in client_for().post(url)["Location"]
    for other in (world["judges"][0], world["host"], world["members"][1]):  # only your own vote can be taken back
        assert client_for(other).post(url).status_code == 404
    assert Vote.objects.count() == 1
    assert client_for(voter).post(url).status_code == 302 and not Vote.objects.exists()


def test_vote_page_after_and_before_the_window(world, voter):
    event = world["event"]
    set_window(event, "before")
    page = client_for(voter).get(f"/events/{event.external_id}/vote").content.decode()
    assert "Not open yet" in page and ">Vote</button>" not in page
    set_window(event, "after")
    page = client_for(voter).get(f"/events/{event.external_id}/vote").content.decode()
    assert "Voting is over" in page and ">Vote</button>" not in page


def test_event_page_links_to_the_vote_only_when_a_window_is_set(world):
    event = world["event"]
    assert "/vote" not in client_for().get(f"/events/{event.external_id}").content.decode()
    set_window(event, "open")
    assert f"/events/{event.external_id}/vote" in client_for().get(f"/events/{event.external_id}").content.decode()
    set_window(event, "after")
    assert f"/events/{event.external_id}/votes" in client_for().get(f"/events/{event.external_id}").content.decode()


# --- tallies are hidden until the window closes -----------------------------------------------


def test_tallies_are_hidden_from_everyone_while_voting_is_open_or_pending(world, voter):
    event = world["event"]
    for state in ("before", "open"):
        set_window(event, state)
        api, page = f"/api/events/{event.external_id}/votes", f"/events/{event.external_id}/votes"
        assert client_for().get(api).status_code == 401
        for who in (voter, world["members"][0], world["judges"][0], world["host"]):
            resp = client_for(who).get(api)
            assert resp.status_code == 403 and resp.json()["error"] == "results_hidden"
            assert client_for(who).get(page).status_code == 403
        assert client_for().get(page).status_code == 302  # the page sends anonymous visitors to log in


def test_tallies_are_public_after_close(world):
    event = set_window(world["event"])
    a, b, c = (make_user(f"t{i}@example.org") for i in range(3))
    p0, p1, p2 = world["projects"][:3]
    for u in (a, b, c):
        services.cast_vote(p1, u)
    for u in (a, b):
        services.cast_vote(p2, u)
    services.cast_vote(p0, c)
    services.cast_vote(world["projects"][3], c)
    set_window(event, "after")
    resp = client_for().get(f"/api/events/{event.external_id}/votes")
    assert resp.status_code == 200
    rows = resp.json()["results"]
    assert [(r["project"], r["votes"], r["rank"]) for r in rows] == [
        (p1.external_id, 3, 1), (p2.external_id, 2, 2),
        (p0.external_id, 1, 3), (world["projects"][3].external_id, 1, 3),  # a tie shares a place
    ]
    page = client_for().get(f"/events/{event.external_id}/votes")
    assert page.status_code == 200 and p1.title in page.content.decode()


def test_no_vote_counts_leak_elsewhere_while_voting_is_open(world):
    event = set_window(world["event"])
    voters = [make_user(f"leak{i}@example.org") for i in range(2)]
    for u in voters:
        services.cast_vote(world["projects"][1], u)
    host = client_for(world["host"])
    card = host.get(f"/events/{event.external_id}/dashboard/live").content.decode()
    assert "Community voting" in card and "<strong>2</strong> votes" in card
    assert world["projects"][1].title not in card  # no per-project figures, and nothing flagged names it
    for url in ("/projects", f"/projects/{event.external_id}/{world['projects'][1].external_id}"):
        html = client_for(world["host"]).get(url).content.decode()
        assert "2 votes" not in html and "votes</td>" not in html


# --- comments --------------------------------------------------------------------------------


def comment_url(p):
    return f"/projects/{p.event.external_id}/{p.external_id}/comments"


def test_comment_needs_login_and_a_public_project(world, voter):
    p = world["projects"][0]
    assert codes(services.post_comment, p, AnonymousUser(), "hi")[0] == 401
    resp = client_for().post(comment_url(p), {"body": "hi"})
    assert resp.status_code == 302 and "/login" in resp["Location"]
    Project.objects.filter(pk=p.pk).update(status="draft")
    assert codes(services.post_comment, Project.objects.get(pk=p.pk), voter, "hi")[0] == 404
    p2 = world["projects"][1]
    services.withdraw_project(p2.event, world["host"], p2, "dupe")
    assert codes(services.post_comment, Project.objects.get(pk=p2.pk), voter, "hi") == (409, "withdrawn")


def test_post_comment_validates_and_shows_it_escaped(world, voter):
    p = world["projects"][0]
    assert codes(services.post_comment, p, voter, "   ") == (400, "invalid_comment")
    assert codes(services.post_comment, p, voter, "x" * 2001) == (400, "invalid_comment")
    c = client_for(voter)
    resp = c.post(comment_url(p), {"body": "  <script>alert(1)</script> nice  "})
    assert resp.status_code == 302
    assert Comment.objects.get().body == "<script>alert(1)</script> nice"  # stripped, stored as typed
    html = c.get(f"/projects/{p.event.external_id}/{p.external_id}").content.decode()
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "1 to 2000" in c.post(comment_url(p), {"body": ""}, follow=True).content.decode()  # flashed
    assert actions(p.event, "comment") == ["comment.posted"]


def test_duplicate_comment_is_refused(world, voter):
    p, q = world["projects"][:2]
    services.post_comment(p, voter, "Great work")
    assert codes(services.post_comment, p, voter, " Great work ") == (409, "duplicate_comment")
    services.post_comment(q, voter, "Great work")  # another project is fine
    services.post_comment(p, make_user("other@example.org"), "Great work")  # another author is fine
    assert Comment.objects.count() == 3
    assert "already posted" in client_for(voter).post(comment_url(p), {"body": "Great work"}, follow=True).content.decode()


def test_comment_rate_limit(world, voter):
    p = world["projects"][0]
    for i in range(10):
        services.post_comment(p, voter, f"comment {i}")
    assert codes(services.post_comment, p, voter, "one more") == (429, "rate_limited")


def test_authors_delete_their_own_comments_only(world, voter):
    p = world["projects"][0]
    mine = services.post_comment(p, voter, "mine")
    other = make_user("other@example.org")
    assert codes(services.delete_comment, mine, other) == (403, "not_your_comment")
    assert codes(services.delete_comment, mine, world["host"]) == (403, "not_your_comment")  # organizers hide instead
    assert client_for().post(f"/comments/{mine.pk}/delete").status_code == 302  # anonymous: to the login page
    assert client_for(other).post(f"/comments/{mine.pk}/delete").status_code == 403
    assert Comment.objects.filter(pk=mine.pk).exists()
    assert client_for(voter).post(f"/comments/{mine.pk}/delete").status_code == 302
    assert not Comment.objects.exists()
    assert actions(p.event, "comment") == ["comment.deleted", "comment.posted"]


def test_organizers_hide_and_unhide_comments_and_others_cannot(world, voter):
    p = world["projects"][0]
    c = services.post_comment(p, voter, "buy pills at spam.example")
    host = client_for(world["host"])
    hide, unhide = f"/comments/{c.pk}/hide", f"/comments/{c.pk}/unhide"
    for who in (None, voter, world["judges"][0], world["members"][0]):
        r1 = client_for(who).post(hide, {"reason": "spam"})
        r2 = client_for(who).post(unhide)
        assert r1.status_code == r2.status_code == (302 if who is None else 403)
    c.refresh_from_db()
    assert c.hidden_at is None
    assert host.post(hide, {"reason": " "}).status_code == 302  # flashed back to the page
    assert Comment.objects.get().hidden_at is None  # a reason is required
    assert host.post(hide, {"reason": "spam"}).status_code == 302
    c.refresh_from_db()
    assert c.hidden_at and c.hidden_reason == "spam" and c.hidden_by == world["host"]
    page_url = f"/projects/{p.event.external_id}/{p.external_id}"
    assert "spam.example" not in client_for().get(page_url).content.decode()  # not public
    assert "spam.example" not in client_for(voter).get(page_url).content.decode()
    organizer_view = host.get(page_url).content.decode()
    assert "spam.example" in organizer_view and "Hidden from the public" in organizer_view
    assert host.post(unhide).status_code == 302
    assert "spam.example" in client_for().get(page_url).content.decode()
    assert actions(p.event, "comment") == ["comment.unhidden", "comment.hidden", "comment.posted"]
    assert AuditLog.objects.get(action="comment.hidden").detail["reason"] == "spam"


def test_project_page_shows_the_comment_form_to_logged_in_users(world, voter):
    p = world["projects"][0]
    url = f"/projects/{p.event.external_id}/{p.external_id}"
    assert "Log in</a> to comment" in client_for().get(url).content.decode()
    assert 'name="body"' in client_for(voter).get(url).content.decode()


# --- integrity report ------------------------------------------------------------------------


def test_integrity_report_flags_shared_addresses_and_new_accounts(world):
    event = set_window(world["event"])
    p0, p1 = world["projects"][:2]
    old = timezone.now() - 10 * HOUR
    ring = [make_user(f"ring{i}@example.org") for i in range(3)]
    pair = [make_user(f"pair{i}@example.org") for i in range(2)]
    for u in ring + pair:
        type(u).objects.filter(pk=u.pk).update(date_joined=old)  # long-standing accounts
    for u in ring:
        services.cast_vote(p0, u, ip="192.0.2.1")
    for u in pair:
        services.cast_vote(p1, u, ip="192.0.2.2")  # two accounts behind one address is not enough to flag
    fresh = make_user("fresh@example.org")  # created after voting opened? no: window opened an hour ago, so yes
    services.cast_vote(p1, fresh, ip="192.0.2.3")
    r = services.vote_integrity_report(event)
    assert r["total"] == 6 and r["voters"] == 6
    assert [(s["project"], s["accounts"]) for s in r["shared_ip"]] == [(p0, 3)]
    assert [x["voter"] for x in r["new_accounts"]] == [fresh]
    flagged = {v.voter.email: r["flags"].get(v.id) for v in Vote.objects.select_related("voter")}
    assert flagged["ring0@example.org"] == ["shared_ip"] and flagged["fresh@example.org"] == ["new_account"]
    assert flagged["pair0@example.org"] is None
    # the same three accounts on different addresses are not flagged
    Vote.objects.filter(voter=ring[0]).update(ip_hash=services.ip_hash("192.0.2.99"))
    assert services.vote_integrity_report(event)["shared_ip"] == []


def test_dashboard_card_shows_flags_and_totals_to_organizers_only(world):
    event = set_window(world["event"])
    ring = [make_user(f"ring{i}@example.org") for i in range(3)]
    for u in ring:
        services.cast_vote(world["projects"][0], u, ip="192.0.2.1")
    live = f"/events/{event.external_id}/dashboard/live"
    html = client_for(world["host"]).get(live).content.decode()
    assert "Community voting" in html and "3 accounts behind one address" in html and "<strong>3</strong> votes" in html
    assert client_for(world["judges"][0]).get(live).status_code == 403
    assert client_for(world["members"][0]).get(live).status_code == 403
    assert client_for().get(live).status_code == 302


def test_dashboard_has_no_voting_card_without_a_window(world):
    html = client_for(world["host"]).get(f"/events/{world['event'].external_id}/dashboard/live").content.decode()
    assert "Community voting" not in html


# --- exports ---------------------------------------------------------------------------------


def rows_of(resp):
    return list(csv.reader(io.StringIO(resp.content.decode())))


def test_votes_csv_is_organizer_only_and_waits_for_the_close(world):
    event = set_window(world["event"])
    ring = [make_user(f"ring{i}@example.org") for i in range(3)]
    for u in ring:
        services.cast_vote(world["projects"][0], u, ip="192.0.2.1")
    url = f"/api/events/{event.external_id}/votes.csv"
    assert client_for().get(url).status_code == 401
    for who in (ring[0], world["judges"][0], world["members"][0]):
        assert client_for(who).get(url).status_code == 403
    resp = client_for(world["host"]).get(url)  # a row per vote is the tally, hidden from organizers too
    assert resp.status_code == 403 and resp.json()["error"] == "results_hidden"
    set_window(event, "after")
    resp = client_for(world["host"]).get(url)
    assert resp.status_code == 200 and resp["Content-Type"].startswith("text/csv")
    rows = rows_of(resp)
    assert rows[0] == ["project_id", "voter_email", "created_at", "ip_hash", "flags"]
    assert len(rows) == 4 and {r[0] for r in rows[1:]} == {world["projects"][0].external_id}
    assert all(len(r[3]) == 12 and r[3] == services.ip_hash("192.0.2.1")[:12] for r in rows[1:])
    assert all("shared_ip" in r[4] for r in rows[1:])
    assert {r[1] for r in rows[1:]} == {u.email for u in ring}


def test_comments_csv_is_organizer_only(world, voter):
    event = world["event"]
    p = world["projects"][0]
    c = services.post_comment(p, voter, "=HYPERLINK(evil)")
    services.hide_comment(c, world["host"], "spam")
    url = f"/api/events/{event.external_id}/comments.csv"
    assert client_for().get(url).status_code == 401
    for who in (voter, world["judges"][0], world["members"][0]):
        assert client_for(who).get(url).status_code == 403
    resp = client_for(world["host"]).get(url)
    assert resp.status_code == 200
    rows = rows_of(resp)
    assert rows[0][:2] == ["comment_id", "project_id"]
    assert rows[1][1] == p.external_id and rows[1][2] == voter.email
    assert rows[1][4] == "'=HYPERLINK(evil)"  # spreadsheet formulas are neutralised
    assert rows[1][6] == world["host"].email and rows[1][7] == "spam"


def test_export_links_are_on_the_manage_page(world):
    html = client_for(world["host"]).get(f"/events/{world['event'].external_id}/manage").content.decode()
    assert "/votes.csv" in html and "/comments.csv" in html


def test_votes_json_for_an_unknown_event_is_404_and_no_window_is_404(world):
    assert client_for(world["host"]).get("/api/events/nope/votes").status_code == 404
    assert client_for(world["host"]).get(f"/api/events/{world['event'].external_id}/votes").status_code == 404


def test_fixture_event_has_no_vote_and_seed_still_runs(seeded):
    ev = Event.objects.get()
    assert ev.voting_phase() == "none" and ev.votes_per_voter == 3
