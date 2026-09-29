import csv
import io

import pytest

from .conftest import JUDGE_A, ORGANIZER, PARTICIPANT, client_as

URL = "/api/export.csv"


def test_organizer_exports_every_review(seeded, fixture_data):
    resp = client_as(ORGANIZER).get(URL)
    assert resp.status_code == 200
    assert resp["Content-Type"].startswith("text/csv")
    rows = list(csv.DictReader(io.StringIO(resp.content.decode())))
    assert len(rows) == len(fixture_data["scores"])
    first = fixture_data["scores"][0]
    match = next(r for r in rows if r["judge_id"] == first["judge"] and r["project_id"] == first["project"])
    for key, value in first["criteria"].items():
        assert match[key] == str(value)


def test_csv_neutralises_formula_injection(seeded):
    from portal.models import Project

    Project.objects.filter(external_id="prj_01").update(title="=HYPERLINK(1)")
    body = client_as(ORGANIZER).get(URL).content.decode()
    assert "'=HYPERLINK(1)" in body


@pytest.mark.parametrize("who", [JUDGE_A, PARTICIPANT])
def test_non_organizers_are_refused(seeded, who):
    assert client_as(who).get(URL).status_code == 403


def test_anonymous_gets_401(seeded):
    resp = client_as().get(URL)
    assert resp.status_code == 401
    assert "Location" not in resp
