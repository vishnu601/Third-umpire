"""The audit log is a SHA-256 hash chain per event, so an edit, a deletion or an insertion shows up on verify."""

from datetime import timedelta

import pytest
from django.core.management import CommandError, call_command
from django.utils import timezone

from portal import auditchain, services
from portal.models import AuditLog, Event

from .conftest import JUDGE_A, ORGANIZER, PARTICIPANT, client_as

pytestmark = pytest.mark.django_db


def _event():
    return Event.objects.get(external_id="evt_01")


def _close_voting(event):
    """The seeded event has a community vote open; hashes are only shown once it has closed."""
    now = timezone.now()
    Event.objects.filter(pk=event.pk).update(voting_opens=now - timedelta(hours=2), voting_closes=now - timedelta(minutes=1))
    event.refresh_from_db()


def _write(event, n=4):
    return [services.audit(event, None, "test.entry", f"t{i}", n=i) for i in range(n)]


def test_each_entry_links_to_the_previous_one_in_its_event(seeded):
    event = _event()
    rows = _write(event)
    rows = [AuditLog.objects.get(pk=r.pk) for r in rows]
    for prev, row in zip(rows, rows[1:]):
        assert row.prev_hash == prev.hash
    assert all(len(r.hash) == 64 for r in rows)
    other = services.audit(None, None, "test.global")
    assert other.prev_hash != rows[-1].hash  # a separate chain


def test_intact_chain_verifies(seeded):
    event = _event()
    _write(event)
    report = services.verify_audit_chain(event)
    assert report["ok"] is True
    assert report["entries"] == AuditLog.objects.filter(event=event).count()
    assert report["head"] == AuditLog.objects.filter(event=event).order_by("-id").first().hash


def test_edited_detail_is_caught(seeded):
    event = _event()
    rows = _write(event)
    AuditLog.objects.filter(pk=rows[1].pk).update(detail={"n": 99})
    report = services.verify_audit_chain(event)
    assert report["ok"] is False
    assert report["broken_at"] == rows[1].pk


def test_edited_row_with_recomputed_hash_breaks_the_next_link(seeded):
    event = _event()
    rows = _write(event)
    AuditLog.objects.filter(pk=rows[1].pk).update(target="forged")
    forged = AuditLog.objects.get(pk=rows[1].pk)
    AuditLog.objects.filter(pk=forged.pk).update(hash=auditchain.entry_hash(forged, forged.prev_hash))
    report = services.verify_audit_chain(event)
    assert report["ok"] is False
    assert report["broken_at"] == rows[2].pk


def test_deleted_row_is_caught(seeded):
    event = _event()
    rows = _write(event)
    AuditLog.objects.filter(pk=rows[1].pk).delete()
    report = services.verify_audit_chain(event)
    assert report["ok"] is False
    assert report["broken_at"] == rows[2].pk


def test_verify_api_is_organizer_only(seeded):
    _write(_event())
    url = "/api/events/evt_01/audit/verify"
    assert client_as().get(url).status_code == 401
    assert client_as(JUDGE_A).get(url).status_code == 403
    assert client_as(PARTICIPANT).get(url).status_code == 403
    response = client_as(ORGANIZER).get(url)
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_audit_page_shows_the_verdict(seeded):
    _write(_event())
    body = client_as(ORGANIZER).get("/events/evt_01/audit").content.decode()
    assert "Chain intact" in body


def test_audit_csv_carries_each_hash(seeded):
    event = _event()
    _close_voting(event)
    rows = _write(event)
    body = client_as(ORGANIZER).get("/api/events/evt_01/audit.csv").content.decode()
    assert body.splitlines()[0].endswith(",hash")
    assert AuditLog.objects.get(pk=rows[-1].pk).hash in body


def test_published_results_show_the_chain_head_at_publication(seeded):
    event = _event()
    organizer = event.roles.filter(role="organizer").first().user
    services.set_results_published(event, organizer, True)
    anchor = AuditLog.objects.filter(event=event, action="results.published").order_by("-id").first().hash
    assert anchor not in client_as().get("/events/evt_01/results").content.decode()  # vote still open: withheld
    _close_voting(event)
    assert anchor in client_as().get("/events/evt_01/results").content.decode()


def test_verify_command_fails_on_a_broken_chain(seeded):
    rows = _write(_event())
    call_command("verify_audit", verbosity=0)
    AuditLog.objects.filter(pk=rows[0].pk).update(action="forged")
    with pytest.raises(CommandError):
        call_command("verify_audit", verbosity=0)


def test_head_hash_is_withheld_while_votes_are_hidden(seeded):
    # The newest entry could be a vote. Its hash is guessable by brute force (voter x project x time), so it would
    # tell an organizer who voted for what before the close. Verify still runs; it just doesn't print the head.
    event = _event()
    now = timezone.now()
    Event.objects.filter(pk=event.pk).update(voting_opens=now - timedelta(hours=1), voting_closes=now + timedelta(hours=1))
    event.refresh_from_db()
    assert event.voting_phase() == "open"
    vote = services.audit(event, None, "vote.cast", "prj_01")
    report = client_as(ORGANIZER).get("/api/events/evt_01/audit/verify").json()
    assert report["ok"] is True and report["head"] == ""
    assert report["entries"] == services.visible_audit(event).count()  # the count would time the votes
    assert vote.hash not in client_as(ORGANIZER).get("/events/evt_01/audit").content.decode()
    # The CSV's hashes would let a guess be checked too: the rows either side of a hidden vote are visible.
    after = services.audit(event, None, "test.after_vote")
    csv_body = client_as(ORGANIZER).get("/api/events/evt_01/audit.csv").content.decode()
    assert after.hash not in csv_body and vote.hash not in csv_body
    Event.objects.filter(pk=event.pk).update(voting_closes=now - timedelta(minutes=1))
    assert client_as(ORGANIZER).get("/api/events/evt_01/audit/verify").json()["head"] == after.hash
    assert after.hash in client_as(ORGANIZER).get("/api/events/evt_01/audit.csv").content.decode()
