# Third Umpire

**A self-hostable hackathon submission and judging portal whose judging engine tells you when the result is a
result.** One Django container, SQLite, no network. Built for DOGFOOD 2026 (Hackathon Raptors).

Most platforms average the scores and print a winner. Third Umpire corrects judge leniency with a model you can
explain in one line (*score = project quality + judge leniency + noise*), shows every project's rank band and chance
of placing, flags places that are **too close to call**, and suggests the extra reviews that would settle them. On the
shared fixture, no project wins more than 22% of resampled outcomes. See [JUDGING.md](JUDGING.md).

```bash
git clone https://github.com/vishnu601/Third-umpire.git && cd Third-umpire
docker compose up --build                   # seeded portal on http://localhost:8080
python3 run.py .dogfood.toml                # acceptance checker: 7/7
```

Or run everything a reviewer would check, from a clean volume: `make verify` (rebuild, `run.py`, 12 role-isolation
probes, the test suite if `.venv` exists).

## Acceptance report

```
claimed: T1 T2
T1  gallery is public ................. PASS
T1  project from fixtures shown ....... PASS
T1  closed event refuses submissions .. PASS
T2  judge sees own scores ............. PASS
T2  judge cannot see peer scores ...... PASS
T2  participant blocked ............... PASS
T2  csv export works .................. PASS
claimed T1 T2, verified T1 T2
```

Full output: [acceptance-report.txt](acceptance-report.txt). We claim **T1 and T2**. T3 and T4 are not built and not
claimed.

## Try it (demo mode)

`docker compose up` seeds the fixture event (41 projects, 30 judges, 126 reviews) and turns on **demo mode**: a bar
at the top of every page switches you between seeded roles in one click. Each role lands on its own home page.

| Role | Account | Starts at |
|---|---|---|
| Organizer | organizer@example.org | the live dashboard for the fixture event |
| Judge A (jdg_01) | tomas.varga@example.org | their judging queue |
| Judge B (jdg_02) | wei.lindqvist@example.org | their judging queue |
| Participant | priya1@example.org (team NorthKiln) | their team page |
| Admin | admin@example.org | events |

The login page also works for these accounts with the password `dogfood-demo` (set `DOGFOOD_DEMO_PASSWORD` to change
it). The checker's cookies (`Cookie: session=org_7f2a` and so on) are printed at boot and listed in
[.dogfood.toml](.dogfood.toml).

**A two-minute tour:**
1. As **Organizer**, open the dashboard. The integrity report has already found the fixture's planted problems:
   - a judge who scored everything the same (jdg_07);
   - two unfinished batches (jdg_01 and jdg_23, leaving 8 projects on 2 reviews), with a button that tops them up;
   - the duplicate "Dry Harbour" (prj_07, prj_41);
   - one submission 3 minutes before the deadline.
2. **Results** shows the bias-corrected ranking with rank bands, the "too close to call" warning and tie-break
   suggestions. Publish it from *Manage*.
3. As **Participant**, try to edit your project: the deadline (the fixture's own, 2026-03-01) refuses it on the
   server, and the refusal appears in the organizer's audit log.
4. As **Judge B**, your queue shows only your projects. `/api/judge/scores?judge=jdg_01` returns 403.
5. As **Organizer**, create a new event with a future deadline to walk the whole lifecycle: tracks, prizes, rubric
   weights and anchors, custom questions → team + invite link → submit → invite judges → assign → score → results →
   publish.

## What it does, and where to check it

Every claim below has a test you can run (`.venv/bin/pytest`, 157 tests) and, for T1/T2 checker items, a `run.py`
line.

| Tier item | Where | Evidence |
|---|---|---|
| **T1** Authentication and sessions | `/login`, `/signup`, `/logout` (Django sessions, cookie `session`) | `tests/test_t1_pages.py::test_signup_logs_in_and_rejects_duplicates_and_weak_passwords`, `::test_login_is_case_insensitive_on_email_and_logout_is_post` |
| Role model: visitor, participant, judge, organizer, admin | `EventRole` per event, `is_superuser` for admin, no row = visitor | `::test_only_hosts_create_events`, `::test_manage_is_organizer_only`, `::test_admin_can_manage_any_event` |
| Events with dates, tracks, prizes | `/events/new`, `/events/<id>/manage` | `::test_organizer_configures_tracks_prizes_questions_and_weights`, `::test_event_dates_are_validated` |
| Teams by invite link | `/teams/<id>` → `/join/<token>`; one team per person per event (DB constraint) | `::test_team_by_invite_link`, `::test_rotated_invite_link_stops_working` |
| Submission, draft and edit until the deadline | `/teams/<id>/project` with every stable field (name, tagline, description, thumbnail, gallery, demo video, repo, live link, tech tags, track, custom questions) | `::test_draft_edit_submit_lifecycle`, `::test_every_field_is_stored`, `::test_images_are_validated` |
| Deadline enforcement that holds | one server-side gate for API, forms, teams and joins; audited | `run.py` "closed event refuses submissions", `tests/test_submit.py`, `::test_edits_after_the_deadline_are_refused_and_unchanged` |
| Public gallery with search and filter | `/projects?q=&event=&track=&tag=`; drafts never shown | `run.py` "gallery is public", `tests/test_gallery.py` |
| **T2** Judge invitation and assignment (batch and algorithmic) | `/events/<id>/judges`, `/invites/<token>` | `tests/test_t2_pages.py::test_invite_flow`, `::test_auto_assign_is_balanced_track_aware_and_idempotent`, `::test_batch_assign_and_unassign` |
| Weighted rubric, organizer-configured | weights and 1/3/5 anchors on *Manage* | `tests/test_scoring.py::test_weighted_score_uses_relative_weights`, `::test_organizer_configures_...` |
| Role isolation in the backend | every view checks the role; API answers 401/403, never redirects | `run.py` "judge cannot see peer scores", "participant blocked"; `tests/test_judge_scores.py`, `::test_score_page_never_shows_a_peers_review` |
| Live progress dashboard | `/events/<id>/dashboard` (refreshes every 10 s), `/api/events/<id>/progress` | `::test_dashboard_shows_the_planted_cases`, `::test_progress_api` |
| Cross-judge normalization, documented | two-way model + residual bootstrap: [JUDGING.md](JUDGING.md), [docs/proof/](docs/proof/) | `tests/test_scoring.py`, `tests/test_simulation.py`, `tests/test_results.py` |
| CSV export (at every stage) | teams, submissions, assignments, scores, results, audit | `run.py` "csv export works", `::test_stage_csv_exports`, `tests/test_export.py` |

**Beyond the checklist:**
- The **integrity report** finds all of the fixture's planted cases.
- **Tie-break assignment** chooses judges who connect the contested projects.
- **Category awards** are computed per criterion.
- A **Markdown results post** can be copied for a blog.
- A readable **audit log** covers every change, including refused late submissions and review edits with before
  and after values.
- A judge is **never assigned their own team's project**.
- Organizers can **withdraw a project from judging** (the duplicate "Dry Harbour", say) straight from the
  integrity report. It is audited and reversible, and the project and its reviews stay in the exports.
- Organizers **read every judge's scores and comment** on a project from its results row. Judges only ever see
  their own.
- Organizers **add co-organizers by email** on *Manage*. *Manage* also **warns before publishing** while reviews are
  still pending, and scores are frozen while results are public.

## Docs

- [ARCHITECTURE.md](ARCHITECTURE.md): the shape of the system and why.
- [DATA-MODEL.md](DATA-MODEL.md): schema, constraints, import and export.
- [JUDGING.md](JUDGING.md): assignment, scoring maths, normalization, the proof.

## Running it for real

Demo mode is for evaluation. For an actual event:

```yaml
# docker-compose.yml, environment:
DOGFOOD_SEED_SESSIONS: "0"          # no fixed sessions, no demo password, no admin, no role switcher
DJANGO_SECRET_KEY: "<50+ random characters>"   # required: the app refuses to start without it
DJANGO_ALLOWED_HOSTS: "hack.example.org"
DJANGO_CSRF_TRUSTED_ORIGINS: "https://hack.example.org"
DJANGO_HTTPS: "1"                   # secure cookies, trust X-Forwarded-Proto, HSTS
```

Turning demo mode off on an existing volume **revokes the demo on the next boot**:
- the four fixed sessions are deleted;
- the demo password stops working wherever it is still set (passwords people chose are kept);
- `admin@example.org` is disabled.

The seed never overwrites live data: events, projects and reviews from the fixture are created once and then left
alone, so an organizer's edits survive restarts.

Compose publishes the port on `127.0.0.1` only. Put it behind a TLS-terminating reverse proxy on the same host. Create the first admin with
`docker compose exec portal python manage.py createsuperuser` (use your email, in lower case, as the username: logins are by email); give organizers the right to host events by marking
them staff (`is_staff`). Everything lives in the `portal-data` volume (`/data`: the SQLite database, uploads and the
results cache). **Backup** is copying that volume, or `docker compose cp portal:/data/db.sqlite3 ./backup.sqlite3` (restore by copying it back while the container is stopped).

| Variable | Default | Meaning |
|---|---|---|
| `DOGFOOD_SEED_SESSIONS` | `0` (`1` in compose) | demo mode: fixed checker sessions, demo password, role switcher |
| `DOGFOOD_DEMO_PASSWORD` | `dogfood-demo` | password given to seeded accounts in demo mode |
| `DOGFOOD_FIXTURES` | `/app/fixtures.json` | fixture file imported on every boot (idempotent) |
| `DOGFOOD_BOOTSTRAP_DRAWS` | `400` | resamples behind rank bands |
| `DJANGO_SECRET_KEY` | none (a public dev key in demo mode or `DJANGO_DEBUG=1`) | **required in production** |
| `DJANGO_HTTPS` | `0` | behind TLS: secure cookies, proxy SSL header, HSTS |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | empty | comma-separated origins, e.g. `https://hack.example.org` |
| `DJANGO_ALLOWED_HOSTS` | `localhost,127.0.0.1,[::1]` | comma-separated |
| `DJANGO_DB_PATH`, `DJANGO_MEDIA_ROOT`, `DJANGO_CACHE_DIR` | under `/data` in Docker | storage locations |
| `PORT`, `WEB_WORKERS` | `8080`, `2` | gunicorn |

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest                                    # 157 tests, about a minute
cd src && DOGFOOD_SEED_SESSIONS=1 ../.venv/bin/python manage.py migrate && \
  DOGFOOD_SEED_SESSIONS=1 ../.venv/bin/python manage.py seed && \
  DOGFOOD_SEED_SESSIONS=1 ../.venv/bin/python manage.py runserver 8080
```

## Honest limitations

- **No email.** Judge invites and team invites are links the organizer or team copies by hand. That's deliberate,
  because the portal must run offline, but it means no password reset flow either (an admin resets passwords with
  `manage.py changepassword`).
- **No rate limiting or CAPTCHA** on signup and login. There is no community voting (T3), which is where abuse
  matters most.
- **SQLite** handles one writer at a time. That is fine for hundreds of participants and dozens of judges, not for a
  10,000-person public vote.
- **Uploads are served by Django itself.** That's simple and fine at hackathon scale; a large event should serve
  `/media/` from the reverse proxy. Each upload is decoded with Pillow and stored under the extension of the format
  it really is. It is then served with `Content-Security-Policy: default-src 'none'; sandbox` and `nosniff`, so a
  crafted file can't run script on the portal's origin. A whole form is capped at 15 MB before it is parsed.
- **The 80% rank bands run slightly narrow** (73% coverage in simulation); see JUDGING.md section 6.
- **Not built:** T3 (voting, comments), T4 (REST API for every action, webhooks, certificates, signed judge records,
  widget), a JSON export/import of a whole event (CSV exports and the SQLite file are the way out today), OpenAPI,
  and pairwise judging.
- A project reviewed only by flat judges falls back to its raw average (flagged low confidence).

## License

MIT, see [LICENSE](LICENSE). Built during the DOGFOOD 2026 window with Claude Code.
