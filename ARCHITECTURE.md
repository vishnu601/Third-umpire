# Architecture

## Shape

```
 browser / curl / run.py
          │  HTTP :8080
 ┌────────▼──────────────────────────────────────────────┐
 │ one container (python:3.12-slim)                        │
 │  entrypoint: migrate → seed (idempotent) → gunicorn     │
 │                                                         │
 │  Django                                                 │
 │   pages.py   HTML views (forms, may redirect to login)  │
 │   views.py   JSON / CSV API (never redirects)           │
 │        │  both call only ↓                              │
 │   services.py   every rule: roles, deadline, teams,     │
 │                 projects, assignment, reviews, audit,   │
 │                 integrity report, results               │
 │        │                     │                          │
 │   models.py (ORM)       scoring.py  pure maths          │
 │                         simulation.py  proof            │
 └────────┬────────────────────────────────────────────────┘
          │ volume portal-data:/data
   db.sqlite3 · media/ (uploads) · cache/ (results analysis)
```

No other services: no Postgres, Redis, queue, email, CDN or auth provider. `docker compose up` with the network off
gives a seeded, working portal. The image needs the network once, at build time, for three pip packages: Django,
gunicorn and Pillow.

## Why this stack

- **Django + SQLite.** The organisers run about a dozen events a year of up to a few hundred people. That's a
  single-writer workload, and SQLite makes backup a file copy and self-hosting one container. Django brings
  sessions, CSRF, password hashing, migrations and a mature ORM that a volunteer maintainer can read in ten years.
- **Server-rendered templates, inline CSS, a few lines of JavaScript** (only the dashboard's 10-second refresh). No
  build step, nothing loaded from the network, and pages work with JavaScript off.
- **Pure-Python maths.** The judging engine has no numpy, so it is small enough to read in one sitting, and a
  statistician can check it line by line (`scoring.py`, about 300 lines).

## The one rule that shapes the code: rules live in `services.py`

Every decision that could be a security or integrity bug lives in one module:
- who is an organizer or judge of which event;
- is submission open;
- can this judge score this project;
- is this a conflict of interest.

It raises `Refused(status, code, message)` when the answer is no. The HTML views and the JSON views are thin
translators, so the same rule gives the same answer whether it's reached by a form or by curl:

| `Refused.status` | API (`/api/...`, `api.api_view`) | Pages (`pages.page`) |
|---|---|---|
| 401 | JSON 401 | redirect to `/login?next=` |
| 400 / 409 | JSON 400 / 409 | flash the message, redirect back to the form |
| 403 / 404 / 410 | JSON 403 / 404 / 410 | error page with that status |

**API routes never redirect.** A redirect from an API is how a failed authorization check turns into a "200" at the
end of a login page. `api_view` returns explicit statuses, rejects wrong methods with 405, and requires
`Content-Type: application/json` on writes (415 otherwise).

## Authentication and roles

- Accounts are Django users; the username is the email in lower case, and passwords have a 10-character minimum.
  The session cookie is named `session`, because that's what the checker sends.
- **Roles are per event** (`EventRole`: participant, judge, organizer), so someone can judge one hackathon and
  compete in another. A visitor has no rows; admin is `is_superuser`; `is_staff` may host new events. You become a
  participant by creating or joining a team, a judge by accepting an invite, and an organizer by creating an event.
- **Score isolation:** every score read for a judge filters on `request.user`. Naming another judge (`?judge=`), even
  one that doesn't exist, is a 403, so ids can't be probed. Organizers read scores only through organizer endpoints.
- **Conflicts of interest:** a judge is never assigned a project from their own team, can't score it, and people on a
  team can't accept a judge invite for that event. Judges can't join teams in events they judge.

## Deadlines

`services.ensure_submissions_open(event, actor, attempted)` is the only gate, and every participant write goes
through it: creating or editing a project, submitting, creating or joining a team. It compares `timezone.now()` with
the event's dates. The client's clock is never consulted.

A refusal is written to the audit log (`deadline.refused`), so the gate runs **outside** database transactions: a
rollback would erase the record.

The fixture event is seeded with its own `submissions_close` (2026-03-01), so it is closed and `run.py`'s late
submission is refused for the real reason (`403 submissions_closed`), not by accident (CSRF or a missing route).

## CSRF

Forms use Django's CSRF tokens. The JSON API is CSRF-exempt but only accepts `application/json` bodies on writes. A
cross-site form can't send that content type without a CORS preflight, which the portal never answers. Session
cookies are also `SameSite=Lax` and `HttpOnly`.

## Results engine and caching

`services.event_results(event)` builds the review points (weighted per the current rubric) and calls
`scoring.analyse`. That's a two-way least-squares fit plus a 400-draw residual bootstrap, about 2.5 s for the
fixture. The result is cached in a file-based cache shared by all gunicorn workers. The key is a fingerprint of the
data: review count, latest review update, rubric weights and prize slots. A new score or weight change is a new key,
so there's no invalidation code to get wrong. The seed step warms the cache, so the first results page is instant.

## Demo mode

`DOGFOOD_SEED_SESSIONS=1` (set in `docker-compose.yml`):
- writes the four fixed session rows the checker uses;
- gives the seeded accounts `DOGFOOD_DEMO_PASSWORD`;
- shows a role switcher (`POST /demo/login`, which is a 404 when demo mode is off).

It is off by default in `settings.py`; a real deployment turns it off in compose.

## Seeding

`manage.py seed` runs on every boot. It upserts the fixture by its string ids (`external_id` on every imported
table), so a restart changes nothing. Every imported score also creates the matching assignment, so the dashboard
reflects the fixture's real coverage, unfinished batches included.

## Tests

`tests/` has 119 pytest tests:
- the checker's 7 behaviours;
- every role and deadline rule through the real pages, with a wrong-role assertion per restricted view;
- known-answer tests for the maths;
- the simulation;
- idempotent seeding.

`make verify` runs the checker and 12 curl probes against a clean container, plus the test suite.

## Trade-offs we'd revisit at scale

- Media is served by Django; use the reverse proxy for large events.
- SQLite writes serialize; switch `DATABASES` to Postgres if a public vote is ever added. The ORM code doesn't change.
- The results cache is per-instance files; a multi-host deployment would point `CACHES` at a shared store.
