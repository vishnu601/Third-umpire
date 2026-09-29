# CLAUDE.md

**Read `SPEC.md` first, every session.** It is the full DOGFOOD 2026 brief:
tiers, scoring, the acceptance checker (`run.py`) and the fixture format.
`spec-official.md` is the shorter companion page. Do not edit `run.py` or
`fixtures.json`; they are the organisers' files. Strategy and the build plan
live in `notes/HACKATHON-BRIEF.md` (git-ignored).

**Code freeze: Tue 29 Sep 2026 18:00 UTC.** (SPEC.md's Monday is out of date.)

## Requirements from the live site that SPEC.md is missing

- T1 submission fields must all exist and be editable until the deadline:
  name, tagline, description, thumbnail, gallery images, demo video URL,
  repository URL, live link, tech tags, track, and organizer-defined custom
  questions.
- T2 assignment is "batch or algorithmic"; support both.
- T2 CSV export "at every stage": teams/participants, submissions,
  assignments, scores, results, audit.
- The fixture's planted cases must be visible to organizers: the flat judge
  (jdg_07), two unfinished review batches (jdg_01, jdg_23; 8 projects at 2
  reviews), the duplicate "Dry Harbour" (prj_07, prj_41).

## Stack (fixed)

- Django + SQLite. No Postgres, Redis, Celery or any other service.
- One container. `docker compose up` must migrate, seed from
  `fixtures.json` and serve on `http://localhost:8080`, with the network off.
- Server-rendered HTML for the gallery and other pages (Django templates).
  No SPA, no JS build step.
- No CDN or network assets. No external fonts, scripts, stylesheets or
  images. Anything the browser loads is served by this app.

## Layout

- `src/` Django project: `config/` (settings, urls), `portal/` (the app).
- `tests/` pytest suite (`pytest-django`). Run: `.venv/bin/pytest`.
- `docker/entrypoint.sh` migrate, seed, start gunicorn.
- `.dogfood.toml` checker config. Keep `[auth]` in sync with
  `SEEDED_SESSIONS` in `src/portal/seed.py`.

## Rules

1. **Tests before features.** Write the failing test in `tests/` first, then
   the code. Every PR keeps `pytest` green and `python3 run.py .dogfood.toml`
   passing against a running portal.
2. **API routes never redirect.** Anything under `/api/` answers with JSON
   and an explicit status: 401 when not logged in, 403 when logged in but not
   allowed. Never `login_required` (it 302s to a login page). Wrap views in
   `@api_view` from `src/portal/api.py` and check access with its
   `require_login` and the `organizer_event` helper in `views.py`; rules
   themselves live in `services.py` and raise `Refused`.
3. **Scores are scoped to the logged-in judge.** Every query that reads
   scores for a judge filters on `request.user`. If a request names another
   judge (query param, path, anything), return 403, even if that judge does
   not exist. Organizers read scores via their own organizer endpoints
   (export, dashboard), never through the judge endpoints.
4. **Role checks live in the backend.** Hiding something in a template is
   not access control. Every restricted view checks the role itself, and
   has a test that calls it with the wrong role and asserts 401/403.
5. **Deadlines are enforced on the server** using `timezone.now()`, compared
   against the event's `submissions_close` from the fixture. Never trust a
   client-sent time.
6. **Fixture import is idempotent.** `manage.py seed` must be safe to run on
   every boot: upsert by the fixture's string ids (`external_id`), never
   duplicate rows.
7. **Honest tier claims.** Only add a tier to `.dogfood.toml` `claimed` when
   every feature in that tier (see SPEC.md section 3) is built and tested,
   not just when the checker passes. Record gaps in the README.

## Seeded sessions (demo mode)

`DOGFOOD_SEED_SESSIONS=1` (on in docker-compose) turns on demo mode. `manage.py
seed` then:

- writes four fixed session rows (organizer, judge_a, judge_b, participant) so
  the checker can attach `Cookie: session=...` without logging in;
- sets `DOGFOOD_DEMO_PASSWORD` on the 5 seeded login accounts;
- makes `admin@example.org` an active superuser;
- enables the `/demo/login` role switcher;
- lets the app boot with a built-in, public `SECRET_KEY`.

A real deployment must turn it off and set `DJANGO_SECRET_KEY` (settings
refuse to load without one unless demo mode or DEBUG is on). With demo mode
off, `seed` revokes all of the above on the next boot: it deletes the fixed
sessions, makes the demo password unusable wherever it is still set, and
disables the admin. Passwords people chose themselves are kept.
