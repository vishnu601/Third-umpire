import pytest

from .conftest import JUDGE_A, JUDGE_B, ORGANIZER, PARTICIPANT, client_as

URL = "/api/judge/scores"


def fixture_scores_for(fixture_data, judge_id):
    return sorted(s["project"] for s in fixture_data["scores"] if s["judge"] == judge_id)


def test_judge_reads_own_scores(seeded, fixture_data):
    resp = client_as(JUDGE_A).get(URL)
    assert resp.status_code == 200
    body = resp.json()
    assert body["judge"] == "jdg_01"
    assert sorted(r["project"] for r in body["reviews"]) == fixture_scores_for(fixture_data, "jdg_01")


def test_judge_may_name_themself(seeded):
    assert client_as(JUDGE_A).get(URL, {"judge": "jdg_01"}).status_code == 200


@pytest.mark.parametrize("target", ["jdg_01", "jdg_03", "no_such_judge", ""])
def test_judge_b_cannot_read_anyone_else(seeded, target):
    resp = client_as(JUDGE_B).get(URL, {"judge": target})
    assert resp.status_code == 403
    assert "reviews" not in resp.json()


@pytest.mark.parametrize("who", [PARTICIPANT, ORGANIZER])
def test_non_judges_are_refused(seeded, who):
    assert client_as(who).get(URL).status_code == 403


def test_anonymous_gets_401_not_a_redirect(seeded):
    resp = client_as().get(URL)
    assert resp.status_code == 401
    assert "Location" not in resp


def test_unknown_session_is_anonymous(seeded):
    assert client_as("not_a_real_session").get(URL).status_code == 401
