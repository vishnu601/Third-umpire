"""Event export: the whole event as JSON in fixtures.json's shape, and the way back in."""

import json
from datetime import timedelta
from io import StringIO

from django.core.management import call_command

from portal import services
from portal.event_export import export_event
from portal.models import Assignment, Criterion, Event, EventRole, Project, Review, Tag, Team, TeamMembership
from portal.seed import import_fixture

from .conftest import JUDGE_A, ORGANIZER, PARTICIPANT, client_as
from .test_t2_pages import make_user, world  # noqa: F401  (world is a fixture)


def canonical(data):
    """The fixture with order removed where order carries no meaning."""
    return {
        "event": data["event"],
        "tracks": sorted(data["tracks"], key=lambda t: t["id"]),
        "judges": sorted(({**j, "tracks": sorted(j["tracks"])} for j in data["judges"]), key=lambda j: j["id"]),
        "teams": sorted(({**t, "members": sorted(t["members"])} for t in data["teams"]), key=lambda t: t["id"]),
        "projects": sorted(data["projects"], key=lambda p: p["id"]),
        "scores": sorted(data["scores"], key=lambda s: (s["project"], s["judge"])),
    }


# --- round trip on the organisers' fixture --------------------------------------------


def test_fixture_event_exports_as_the_fixture(seeded, fixture_data):
    exported = export_event(Event.objects.get(external_id="evt_01"))
    assert list(exported) == list(fixture_data)  # same top-level keys, nothing extra
    assert canonical(exported) == canonical(fixture_data)
    # Same field names on every record, not only the same values.
    assert {tuple(p) for p in exported["projects"]} == {tuple(p) for p in fixture_data["projects"]}
    assert {tuple(j) for j in exported["judges"]} == {tuple(j) for j in fixture_data["judges"]}


def test_exported_fixture_imports_into_a_fresh_database(seeded, fixture_data):
    exported = export_event(Event.objects.get(external_id="evt_01"))
    wipe(Event.objects.get(external_id="evt_01"))
    assert not Project.objects.exists() and not Review.objects.exists()
    import_fixture(json.loads(json.dumps(exported)))
    assert canonical(export_event(Event.objects.get(external_id="evt_01"))) == canonical(fixture_data)
    assert Review.objects.count() == len(fixture_data["scores"])


def test_export_then_import_is_idempotent(seeded):
    event = Event.objects.get(external_id="evt_01")
    exported = export_event(event)
    before = (Project.objects.count(), Review.objects.count(), TeamMembership.objects.count(), Team.objects.count())
    import_fixture(exported)
    assert (Project.objects.count(), Review.objects.count(), TeamMembership.objects.count(), Team.objects.count()) == before


# --- an event made in the app --------------------------------------------------------------


def build_app_event(world):  # noqa: F811
    event, host = world["event"], world["host"]
    p0, p1, p2, p3 = world["projects"]
    # Rich data the fixture has no field for.
    event.description = "An app-made event"
    event.judging_close = event.submissions_close + timedelta(days=2)
    event.save()
    p0.description, p0.demo_video_url, p0.live_url = "Long text", "https://example.org/v", "https://example.org/l"
    p0.save()
    p0.tags.set([Tag.objects.get_or_create(name=n)[0] for n in ("django", "sqlite")])
    Criterion.objects.filter(event=event, key="quality").update(weight=2.5)
    # A draft, and a team with no project at all.
    draft_member = make_user("draft@example.org")
    draft_team = services.create_team(event, draft_member, "Draft Team")
    services.create_project(draft_team, draft_member, {"title": "Only a draft"}, submit=False)
    services.create_team(event, make_user("lonely@example.org"), "No Project Team")
    # Reviews through the real service.
    judge = world["judges"][0]
    services.assign_batch(event, host, judge, [p0, p1, p3])
    services.save_review(p0, judge, {"functionality": 5, "quality": 4, "innovation": 3}, "Nice")
    services.save_review(p1, judge, {"functionality": 2, "quality": 2, "innovation": 1}, "")
    services.save_review(p3, judge, {"functionality": 3, "quality": 3, "innovation": 3}, "was withdrawn later")
    services.withdraw_project(event, host, p3, "duplicate")  # its review stays, and so must its export
    return event


def wipe(event):
    """Remove an event as an empty database would not have it (scores and projects are PROTECTed from a bare cascade)."""
    Review.objects.filter(project__event=event).delete()
    event.projects.all().delete()
    event.delete()


def test_app_made_event_exports_with_random_ids_and_optional_keys(world):
    event = build_app_event(world)
    data = export_event(event)
    assert data["event"]["id"].startswith("evt_") and data["event"]["id"] != "evt_01"
    assert data["event"]["description"] == "An app-made event"
    assert data["event"]["reviews_per_project"] == 2
    assert data["event"]["judging_close"].endswith("Z")
    assert all(t["id"].startswith("tm_") for t in data["teams"])
    by_title = {p["title"]: p for p in data["projects"]}
    assert len(by_title) == 5 and by_title["Only a draft"]["submitted_at"] is None
    assert by_title["Project 0"]["tags"] == ["django", "sqlite"]
    assert by_title["Project 0"]["demo_video_url"] == "https://example.org/v"
    assert by_title["Project 3"]["withdrawn_reason"] == "duplicate" and by_title["Project 3"]["withdrawn_at"].endswith("Z")
    assert "withdrawn_at" not in by_title["Project 1"]
    assert {t["name"] for t in data["teams"]} >= {"Draft Team", "No Project Team"}
    assert len(data["scores"]) == 3
    assert next(c for c in data["criteria"] if c["key"] == "quality")["weight"] == 2.5
    assert set(data["judges"][0]) == {"id", "name", "email", "tracks"}
    json.dumps(data)  # serialisable as is


def test_app_made_event_survives_export_and_import(world):
    event = build_app_event(world)
    before = export_event(event)
    external_id = event.external_id
    wipe(event)  # teams, memberships, criteria, tracks go with it; users stay, as on another portal
    assert not Project.objects.filter(event__external_id=external_id).exists()

    imported = import_fixture(json.loads(json.dumps(before)))

    assert imported.external_id == external_id
    assert canonical(export_event(imported)) == canonical(before)
    assert Project.objects.filter(event=imported).count() == 5
    assert Project.objects.get(event=imported, title="Project 3").withdrawn_reason == "duplicate"
    assert set(Project.objects.get(event=imported, title="Project 0").tags.values_list("name", flat=True)) == {"django", "sqlite"}
    assert imported.description == "An app-made event" and imported.reviews_per_project == 2
    assert Criterion.objects.get(event=imported, key="quality").weight == 2.5
    assert Review.objects.filter(project__event=imported).count() == 3
    assert Assignment.objects.filter(event=imported).count() == 3
    # Members still one team each, and the judge kept the id and tracks.
    assert TeamMembership.objects.filter(event=imported).count() == 6
    role = EventRole.objects.get(event=imported, external_id="j0")
    assert list(role.tracks.values_list("external_id", flat=True)) == ["t1"]


def test_app_made_event_imports_next_to_the_original_under_a_new_id(world):
    event = build_app_event(world)
    data = json.loads(json.dumps(export_event(event)))
    data["event"]["id"] = "evt_copy"
    copy = import_fixture(data)
    assert copy.pk != event.pk
    assert canonical(export_event(copy))["projects"] == canonical(export_event(event))["projects"]
    assert Review.objects.filter(project__event=copy).count() == 3
    assert Review.objects.filter(project__event=event).count() == 3


# --- nothing secret leaves --------------------------------------------------------------------


def test_export_carries_no_credentials_tokens_or_addresses(world):
    event = build_app_event(world)
    text = json.dumps(export_event(event)).lower()
    for team in Team.objects.filter(event=event):
        assert team.invite_token not in text
    for user in [world["host"], *world["members"], *world["judges"]]:
        assert user.password not in text
    for needle in ("pbkdf2", "argon2", "bcrypt", "password", "session", "csrf", "token", "ip_address", "127.0.0.1"):
        assert needle not in text
    assert "host@example.org" not in text  # organizers are not exported


def test_seeded_export_has_no_password_hashes_or_sessions(seeded):
    text = client_as(ORGANIZER).get("/api/events/evt_01/export.json").content.decode().lower()
    for needle in ("pbkdf2", "unusable", "session", "org_7f2a", "jdg_a_91bc", "password"):
        assert needle not in text


# --- management command -------------------------------------------------------------------------


def test_command_writes_stdout_and_file_that_seed_can_import(seeded, fixture_data, tmp_path):
    out = StringIO()
    call_command("export_event", "evt_01", stdout=out)
    assert canonical(json.loads(out.getvalue())) == canonical(fixture_data)

    path = tmp_path / "evt.json"
    call_command("export_event", "evt_01", output=str(path), stdout=StringIO())
    assert canonical(json.loads(path.read_text())) == canonical(fixture_data)

    wipe(Event.objects.get(external_id="evt_01"))
    call_command("seed",fixtures=str(path), sessions=False, warm=False, verbosity=0)
    assert Review.objects.count() == len(fixture_data["scores"])


def test_command_unknown_event_is_an_error(seeded):
    import pytest
    from django.core.management.base import CommandError

    with pytest.raises(CommandError):
        call_command("export_event", "evt_nope", stdout=StringIO())


# --- the API route ---------------------------------------------------------------------------------


URL = "/api/events/evt_01/export.json"


def test_organizer_downloads_the_event(seeded, fixture_data):
    resp = client_as(ORGANIZER).get(URL)
    assert resp.status_code == 200
    assert resp["Content-Type"].startswith("application/json")
    assert "attachment" in resp["Content-Disposition"] and "evt_01" in resp["Content-Disposition"]
    assert canonical(json.loads(resp.content)) == canonical(fixture_data)


def test_export_anonymous_gets_401_not_a_redirect(seeded):
    resp = client_as().get(URL)
    assert resp.status_code == 401
    assert "Location" not in resp


def test_export_participant_and_judge_get_403(seeded):
    assert client_as(PARTICIPANT).get(URL).status_code == 403
    assert client_as(JUDGE_A).get(URL).status_code == 403


def test_export_unknown_event_is_403_for_everyone_but_never_leaks(seeded):
    assert client_as(ORGANIZER).get("/api/events/evt_nope/export.json").status_code == 403


def test_organizer_of_another_event_cannot_export_this_one(world, seeded):
    other = make_user("other-host@example.org", is_staff=True)
    from django.test import Client

    c = Client()
    c.force_login(other)
    assert c.get(URL).status_code == 403
    assert c.get(f"/api/events/{world['event'].external_id}/export.json").status_code == 403


def test_manage_page_links_the_export(seeded):
    body = client_as(ORGANIZER).get("/events/evt_01/manage").content.decode()
    assert "/api/events/evt_01/export.json" in body
