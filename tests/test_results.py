"""Results: organizer-only until published, uncertainty shown, tie-breaks, exports."""

import csv
import io

from django.contrib.auth import get_user_model

from portal import services
from portal.models import Assignment, Event, Review

from .conftest import JUDGE_A, ORGANIZER, PARTICIPANT, client_as

User = get_user_model()


def test_results_are_organizer_only_until_published(seeded):
    assert client_as(ORGANIZER).get("/events/evt_01/results").status_code == 200
    for who in (JUDGE_A, PARTICIPANT, None):
        assert client_as(who).get("/events/evt_01/results").status_code == 403
    Event.objects.filter(external_id="evt_01").update(results_published_at="2026-03-10T00:00:00Z")
    public = client_as().get("/events/evt_01/results")
    assert public.status_code == 200
    body = public.content.decode()
    # The public never sees per-judge leniency or tie-break internals.
    assert "Tie-break suggestions" not in body and "jdg_07" not in body


def test_organizer_view_explains_the_uncertainty(seeded):
    body = client_as(ORGANIZER).get("/events/evt_01/results").content.decode()
    assert "Too close to call" in body
    assert "flat, excluded" in body  # jdg_07
    assert "Category awards" in body and "Best Functionality" in body
    assert "Copy as Markdown" in body and "| Place | Project |" in body


def test_tiebreak_assignments_link_contested_projects(seeded):
    event = Event.objects.get(external_id="evt_01")
    analysis = services.event_results(event)
    suggestions = services.tiebreak_suggestions(event, analysis)
    assert suggestions
    for s in suggestions:
        assert not Assignment.objects.filter(judge=s["role"].user, project=s["project"]).exists()
        assert s["role"].external_id != "jdg_07"
    client_as(ORGANIZER).post("/events/evt_01/results", {"action": "tiebreak"})
    assert Assignment.objects.filter(event=event, source="tiebreak").count() == len(suggestions)
    # Judges cannot trigger it.
    assert client_as(JUDGE_A).post("/events/evt_01/results", {"action": "tiebreak"}).status_code == 403


def test_results_recompute_when_a_score_changes(seeded):
    event = Event.objects.get(external_id="evt_01")
    before = services.event_results(event)
    last = before.ranking()[-1]
    review = Review.objects.filter(project_id=last.project).first()
    review.scores.update(value=5)
    review.save()  # touches updated_at, so the cache key changes
    after = services.event_results(event)
    assert after.projects[last.project].score > before.projects[last.project].score


def test_results_csv(seeded):
    resp = client_as(ORGANIZER).get("/api/events/evt_01/results.csv")
    rows = list(csv.DictReader(io.StringIO(resp.content.decode())))
    assert len(rows) == 41
    assert rows[0]["rank"] == "1" and {"p_first", "p_prize", "too_close_to_call", "rank_band_low"} <= set(rows[0])
    assert client_as(JUDGE_A).get("/api/events/evt_01/results.csv").status_code == 403
