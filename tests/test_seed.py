from django.contrib.sessions.models import Session
from django.core.management import call_command

from portal.models import CriterionScore, Event, EventRole, Project, Review, Team, Track

from .conftest import FIXTURES_PATH, JUDGE_A, JUDGE_B, ORGANIZER, PARTICIPANT


def counts():
    return {
        "events": Event.objects.count(),
        "tracks": Track.objects.count(),
        "teams": Team.objects.count(),
        "projects": Project.objects.count(),
        "reviews": Review.objects.count(),
        "criterion_scores": CriterionScore.objects.count(),
        "judges": EventRole.objects.filter(role=EventRole.Role.JUDGE).count(),
    }


def test_imports_every_fixture_record(seeded, fixture_data):
    assert counts() == {
        "events": 1,
        "tracks": len(fixture_data["tracks"]),
        "teams": len(fixture_data["teams"]),
        "projects": len(fixture_data["projects"]),
        "reviews": len(fixture_data["scores"]),
        "criterion_scores": sum(len(s["criteria"]) for s in fixture_data["scores"]),
        "judges": len(fixture_data["judges"]),
    }


def test_event_keeps_fixture_close_date(seeded, fixture_data):
    event = Event.objects.get(external_id=fixture_data["event"]["id"])
    assert event.submissions_close.isoformat().replace("+00:00", "Z") == (
        fixture_data["event"]["submissions_close"]
    )


def test_seed_is_idempotent(seeded):
    before = counts()
    call_command("seed", fixtures=str(FIXTURES_PATH), sessions=True, verbosity=0)
    call_command("seed", fixtures=str(FIXTURES_PATH), sessions=True, verbosity=0)
    assert counts() == before
    assert Session.objects.count() == 4


def test_seeded_sessions_resolve_to_the_right_people(seeded):
    expected = {
        ORGANIZER: ("organizer@example.org", EventRole.Role.ORGANIZER),
        JUDGE_A: ("tomas.varga@example.org", EventRole.Role.JUDGE),
        JUDGE_B: (None, EventRole.Role.JUDGE),
        PARTICIPANT: ("priya1@example.org", EventRole.Role.PARTICIPANT),
    }
    for key, (email, role) in expected.items():
        data = Session.objects.get(session_key=key).get_decoded()
        user_id = int(data["_auth_user_id"])
        roles = EventRole.objects.filter(user_id=user_id)
        assert list(roles.values_list("role", flat=True)) == [role]
        if email:
            assert roles.get().user.email == email


def test_judges_a_and_b_are_different_fixture_judges(seeded):
    a = Session.objects.get(session_key=JUDGE_A).get_decoded()["_auth_user_id"]
    b = Session.objects.get(session_key=JUDGE_B).get_decoded()["_auth_user_id"]
    assert a != b


def test_seed_without_sessions_writes_none(db):
    call_command("seed", fixtures=str(FIXTURES_PATH), sessions=False, verbosity=0)
    assert Session.objects.count() == 0
    assert Project.objects.exists()
