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
 │   errors.py Refused     simulation.py  proof            │
 │   ratelimit.py          event_export.py  JSON export    │
 └────────┬────────────────────────────────────────────────┘
          │ volume portal-data:/data
   db.sqlite3 · media/ (uploads) · cache/ (results, rate limits)
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

It raises `Refused(status, code, message)` (defined in `errors.py`, so `ratelimit.py` can raise it too) when the
answer is no. The HTML views and the JSON views are thin
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
`scoring.analyse`. That's a two-way least-squares fit plus a 400-draw residual bootstrap, about 0.4 s for the
fixture. The result is cached in a file-based cache shared by all gunicorn workers. The key is a fingerprint of the
data: review count, latest review update, rubric weights, prize slots and which projects are withdrawn. A new score or weight change is a new key,
so there's no invalidation code to get wrong. The seed step warms the cache, so the first results page is instant.

## Community voting and comments

All of it lives in `services.py` (`cast_vote`, `retract_vote`, `ballot`, `vote_tallies`, `vote_integrity_report`,
`post_comment`, `hide_comment`, ...), so the form post and any future API give the same refusals.

- **Window.** `Event.voting_opens` / `voting_closes` (both blank = no vote). `Event.voting_phase()` is compared with
  `timezone.now()` on every cast and retract (`403 voting_closed`); the client never sends a time. Validation
  (`validate_voting_window`) makes the window all-or-nothing and forbids opening it before submissions close. Once
  voting is open (`lock_open_voting_window`), its start and allowance are fixed and the close can only move later
  (409 `voting_window_locked`), so an organizer cannot end the vote early to see the tallies. The audit page and
  `audit.csv` leave out `vote.*` rows until the close (`visible_audit`), because they name a project.
- **Who may vote.** Any logged-in account except the event's judges and organizers (`conflict_of_interest`) and
  members of the project's own team (`own_project`). A vote is a `Vote` row with a unique `(voter, project)`
  constraint; the per-person cap (`votes_per_voter`) is checked before and after the insert so a race cannot exceed it. A vote on a
  project an organizer later withdraws is given back to the voter.
- **Ballot order.** `ballot_order` shuffles the eligible projects with `random.Random(seed)` where the seed is a hash
  of `(event, voter)`: a voter's order is stable across reloads, different voters get different orders, so position
  bias spreads out.
- **Hidden tallies.** `require_votes_visible` is the only gate for tallies and the votes export: until
  `voting_closes` it answers 401 (API, anonymous) or 403 `results_hidden` to everyone, organizers included. Nothing
  else (gallery, project page, dashboard) computes per-project counts; during the window the dashboard card shows a
  total and flag counts only, and which projects are flagged appears after the close.
- **Abuse controls.** Rate limits use `ratelimit.hit` (`vote-user` 30/10 min, `vote-ip` 120/10 min,
  `comment-user` 10/10 min). `Vote.ip_hash` is `sha256(SECRET_KEY + client_ip)`, so the address is never stored or
  audited. `vote_integrity_report` warns about 3+ accounts on one hashed address voting for one project, and counts
  votes from accounts created after voting opened as an informational signal (not a warning). Both are shown to
  organizers and put in the votes CSV; nothing is removed automatically, and organizers cannot void a vote or ban a
  voter. Every cast, retraction, comment, deletion, hide and unhide is audited.
- **Comments.** Plain text, stored as typed, escaped by Django's autoescaping when rendered. Authors delete their
  own; organizers hide with a reason (kept in the row, and the hide's audit entry records the comment body too) and can
  unhide. An author cannot delete a comment an organizer has hidden (409 `comment_hidden`), so the evidence stays.
  An identical body from the same author on the same project is refused.

## Rate limits and client addresses

`ratelimit.hit(scope, ident, limit, window)` is a fixed-window counter in the shared file cache (keys are hashed; the
count is not atomic, so a few extra attempts can slip through when workers race). It raises `Refused(429)`. The
limits:

| Scope | Limit |
|---|---|
| login, per account and address together | 10 attempts per 15 min |
| login, per account alone | 100 per 15 min |
| login, per address | 50 per 15 min |
| signup, per address | 30 per hour (`DOGFOOD_RATE_LIMITS_SIGNUP_PER_IP`) |
| votes | 30 per account and 120 per address per 10 min |
| comments | 10 per account per 10 min |

Keying the tight login lock on account and address means a stranger cannot lock a named judge out with ten guesses;
the account-only ceiling still stops a distributed guess. The account key is NFKC-normalised and lower-cased, so
full-width Unicode variants of an email share one counter.

`ratelimit.client_ip(request)` is the address used for these limits and for the vote address hash. It is
`REMOTE_ADDR` unless `DJANGO_PROXY_COUNT` says how many of our own reverse proxies sit in front, in which case it is
the entry the outermost of them appended to `X-Forwarded-For`; the client-controlled first entries are never read.
IPv6 addresses are collapsed to their /64, and an empty address counts as one shared "unknown" address rather than
skipping the limit. The cache has `MAX_ENTRIES` 100000, so counters are not culled at random (Django's default is
300). `DOGFOOD_RATE_LIMITS=0` turns all of it off.

## Demo mode

`DOGFOOD_SEED_SESSIONS=1` (set in `docker-compose.yml`):
- writes the four fixed session rows the checker uses;
- gives the seeded accounts `DOGFOOD_DEMO_PASSWORD`;
- makes `admin@example.org` an active superuser;
- shows a role switcher (`POST /demo/login`, which is a 404 when demo mode is off);
- opens a community vote on the fixture event if it has none (opens at boot, closes 14 days later), so the ballot can
  be tried;
- lets the app boot on a built-in, public `SECRET_KEY`. With demo mode and `DEBUG` both off, settings refuse to load
  without `DJANGO_SECRET_KEY`.

It is off by default in `settings.py`; a real deployment turns it off in compose. The next boot with it off
**revokes** the demo (`seed.revoke_demo_access`): the fixed sessions are deleted, the demo password is made
unusable wherever it is still set, and the admin account is disabled. Passwords people chose are kept.

## Seeding

`manage.py seed` runs on every boot and matches the fixture by its string ids (`external_id` on every imported
table), so it never duplicates a row. Reference rows (tracks, judges' roles, teams, memberships) are upserted. `import_fixture` also makes
`organizer@example.org` an organizer of every event it imports. It accepts any event file, and the demo logins are
skipped for people the file lacks.
Everything people edit in the app (the event's dates and settings, projects, reviews and their scores) is
**create-only**: imported once, then never overwritten, so a restart can't revert an organizer's or a judge's
change. Every imported score also creates the matching assignment, so the dashboard
reflects the fixture's real coverage, unfinished batches included.

## Tests

`tests/` has 243 tests:
- the checker's 7 behaviours;
- every role and deadline rule through the real pages, with a wrong-role assertion per restricted view;
- known-answer tests for the maths;
- the simulation;
- idempotent, create-only seeding and demo revocation;
- the security fixes from review (`tests/test_hardening.py`) and the organizer tools (`tests/test_product.py`);
- community voting and comments (`tests/test_t3.py`);
- rate limits and client-address handling (`tests/test_ratelimit.py`);
- the JSON event export and its round trip (`tests/test_event_export.py`);
- the threat-model claims that needed a test of their own (`tests/test_threats.py`).

`make verify` runs the checker and 12 curl probes against a clean container, plus the test suite.

## Trade-offs we'd revisit at scale

- Media is served by Django; use the reverse proxy for large events.
- SQLite writes serialize; switch `DATABASES` to Postgres for a large public vote. The ORM code doesn't change.
- The results cache is per-instance files; a multi-host deployment would point `CACHES` at a shared store.
