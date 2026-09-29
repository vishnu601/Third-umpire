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
python3 run.py .dogfood.toml                # acceptance checker: 7/7 (plus a T3 note; see below)
```

Or run everything a reviewer would check, from a clean volume: `make verify` (rebuild, `run.py`, 12 role-isolation
probes, the test suite if `.venv` exists).

## Acceptance report

```
claimed: T1 T2 T3
T1  gallery is public ................. PASS
T1  project from fixtures shown ....... PASS
T1  closed event refuses submissions .. PASS
T2  judge sees own scores ............. PASS
T2  judge cannot see peer scores ...... PASS
T2  participant blocked ............... PASS
T2  csv export works .................. PASS
claimed T1 T2 T3, verified T1 T2
note: claimed but not verified: T3
```

Full output: [acceptance-report.txt](acceptance-report.txt). We claim **T1, T2 and T3**. `run.py` has checks for T1
and T2 only, so it prints the `note:` line for any T3 claim; T3 is verified by hand. The evidence for every T3 item
(community voting, comments, results hidden during the window, randomized ballots, anti-abuse) is in the table
below and in `tests/test_t3.py`, and the defences are argued in [THREAT-MODEL.md](THREAT-MODEL.md) section 3. T4 is
not built and not claimed.

## Try it (demo mode)

`docker compose up` seeds the fixture event (41 projects, 30 judges, 126 reviews) and turns on **demo mode**: a bar
at the top of every page switches you between seeded roles in one click. Each role lands on its own home page. Demo
mode also opens a community vote on the fixture event (it opens at boot and closes 14 days later, and only if the
event has no vote of its own), so you can try the ballot. The first `docker compose up --build` needs the network
once, to install the Python packages; after that it runs offline.

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
6. As **Participant** (priya1), open **Vote** in the nav and cast a few votes; your own team's project has no button.
   Open the vote tallies page (`/events/<id>/votes`): counts are hidden until the vote closes, for everyone. Post a comment on a project. Then
   switch to **Organizer** and find the **Community voting** card on the dashboard: totals and flag counts, no
   per-project tallies.

## What it does, and where to check it

Every claim below has a test you can run (`.venv/bin/pytest`, 243 tests) and, for T1/T2 checker items, a `run.py`
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
| Weighted rubric, organizer-configured | weights and 1/3/5 anchors on *Manage* | `tests/test_scoring.py::test_weighted_score_uses_relative_weights`, `tests/test_t1_pages.py::test_organizer_configures_tracks_prizes_questions_and_weights` |
| Role isolation in the backend | every view checks the role; API answers 401/403, never redirects | `run.py` "judge cannot see peer scores", "participant blocked"; `tests/test_judge_scores.py`, `::test_score_page_never_shows_a_peers_review` |
| Live progress dashboard | `/events/<id>/dashboard` (refreshes every 10 s), `/api/events/<id>/progress` | `::test_dashboard_shows_the_planted_cases`, `::test_progress_api` |
| Cross-judge normalization, documented | two-way model + residual bootstrap: [JUDGING.md](JUDGING.md), [docs/proof/](docs/proof/) | `tests/test_scoring.py`, `tests/test_simulation.py`, `tests/test_results.py` |
| CSV export (at every stage) | teams, submissions, assignments, scores, results, audit | `run.py` "csv export works", `::test_stage_csv_exports`, `tests/test_export.py` |
| **T3** Community voting (authenticated) | `/events/<id>/vote`; window and votes per person set on *Manage*; own team, judges and organizers cannot vote; the window and the per-person limit are enforced on the server clock | `tests/test_t3.py::test_voting_window_validation`, `::test_window_is_enforced_on_the_server_clock`, `::test_cannot_vote_for_your_own_team`, `::test_judges_and_organizers_cannot_vote`, `::test_votes_per_voter_limit_and_retract_frees_a_vote`, `::test_second_vote_on_a_project_is_refused_and_the_database_agrees`, `::test_retract_rules` |
| Randomized ballot order | each voter gets a stable shuffle seeded by (event, voter) | `::test_ballot_order_is_stable_per_voter_and_differs_between_voters`, `::test_ballot_page_shows_state_and_takes_votes` |
| Results hidden during voting | `/events/<id>/votes` and `/api/events/<id>/votes` answer 403 `results_hidden` (401 for anonymous API calls; 404 when the event has no community vote) to everyone, organizers too, until voting closes; then public | `::test_tallies_are_hidden_from_everyone_while_voting_is_open_or_pending`, `::test_tallies_are_public_after_close`, `::test_no_vote_counts_leak_elsewhere_while_voting_is_open` |
| Project comments with moderation | comment box on each project page; authors delete their own (not once an organizer has hidden it: 409 `comment_hidden`), organizers hide (with a reason, and the body is kept in the audit entry) and unhide; bodies are escaped | `::test_post_comment_validates_and_shows_it_escaped`, `::test_authors_delete_their_own_comments_only`, `::test_organizers_hide_and_unhide_comments_and_others_cannot` |
| Anti-abuse: rate limits, duplicate detection, audit | per-account and per-address limits on votes, per-account on comments; per-account-and-address login lock; unique vote per voter and project; identical-comment refusal; a "Community voting" card on the dashboard warns about 3+ accounts on one address voting for the same project and counts accounts created after voting opened (informational only); during the window it shows flag counts, not which projects are flagged; once voting is open its start and allowance are fixed and the close can only move later (409 `voting_window_locked`); votes and comments are audited, and vote entries stay off the audit page and CSV until the close | `::test_vote_rate_limits`, `::test_vote_rate_limit_per_address`, `::test_comment_rate_limit`, `::test_duplicate_comment_is_refused`, `::test_integrity_report_flags_shared_addresses_and_new_accounts`, `::test_dashboard_card_shows_flags_and_totals_to_organizers_only`, `::test_vote_audit_trail`, `::test_the_ip_is_stored_only_as_a_salted_hash`, `tests/test_review_fixes.py::test_organizers_cannot_close_the_vote_early_to_peek`, `::test_dashboard_names_flagged_projects_only_after_the_close`, `::test_the_audit_log_does_not_reveal_votes_while_voting_is_open`, `::test_new_accounts_are_informational_not_a_warning`, `::test_a_stranger_cannot_lock_a_judge_out`, `::test_unicode_variants_of_an_email_share_one_login_counter`, `::test_ipv6_addresses_count_per_64_and_a_missing_address_is_still_limited` |
| Votes and comments exports | `/api/events/<id>/votes.csv` (after voting closes) and `/comments.csv`, organizer only | `::test_votes_csv_is_organizer_only_and_waits_for_the_close`, `::test_comments_csv_is_organizer_only` |

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
- [THREAT-MODEL.md](THREAT-MODEL.md): Sybil voters, ballot stuffing, collusion and the web attacks around them,
  each with the code that defends it, the test that proves it, and the residual risk.

## Running it for real

Demo mode is for evaluation. For an actual event:

```yaml
# docker-compose.yml, environment:
DOGFOOD_SEED_SESSIONS: "0"          # no fixed sessions, no demo password, no admin, no role switcher
DJANGO_SECRET_KEY: "<50+ random characters>"   # required: the app refuses to start without it
DJANGO_ALLOWED_HOSTS: "hack.example.org"
DJANGO_CSRF_TRUSTED_ORIGINS: "https://hack.example.org"
DJANGO_HTTPS: "1"                   # secure cookies, trust X-Forwarded-Proto, HSTS
DJANGO_PROXY_COUNT: "1"             # reverse proxies in front; without it every visitor shares one address for rate limits and vote flags
```

Turning demo mode off on an existing volume **revokes the demo on the next boot**:
- the four fixed sessions are deleted;
- the demo password stops working wherever it is still set (passwords people chose are kept);
- `admin@example.org` is disabled.

`organizer@example.org` stays: `import_fixture` makes that account an organizer of every event it imports, so an
imported event always has one. With demo mode off it has an unusable password, so nobody can sign in as it.

The seed never overwrites live data: events, projects and reviews from the fixture are created once and then left
alone, so an organizer's edits survive restarts.

Compose publishes the port on `127.0.0.1` only. Put it behind a TLS-terminating reverse proxy on the same host. Create the first admin with
`docker compose exec portal python manage.py createsuperuser` (use your email, in lower case, as the username: logins are by email); give organizers the right to host events by marking
them staff (`is_staff`). Everything lives in the `portal-data` volume (`/data`: the SQLite database, uploads and the
results cache). **Backup** is copying that volume, or `docker compose cp portal:/data/db.sqlite3 ./backup.sqlite3` (restore by copying it back while the container is stopped).
**Migration out:** `docker compose exec portal python manage.py export_event <event_id> -o /data/event.json` (or the
"Whole event (JSON)" link on the manage page) writes an event in the fixture's own shape. **Migration in**, on the
other portal:
```bash
docker compose cp event.json portal:/data/event.json
docker compose exec portal python manage.py seed --fixtures /data/event.json
```
The seed accepts any event file; demo logins are simply skipped for people the file does not have.

In the plain Docker demo every browser reaches the app through Docker's gateway, so the per-address rate limits and
the shared-address vote flag see one address for everybody. That is expected there; set `DJANGO_PROXY_COUNT` behind a
real proxy.

| Variable | Default | Meaning |
|---|---|---|
| `DOGFOOD_SEED_SESSIONS` | `0` (`1` in compose) | demo mode: fixed checker sessions, demo password, role switcher |
| `DOGFOOD_DEMO_PASSWORD` | `dogfood-demo` | password given to seeded accounts in demo mode |
| `DOGFOOD_FIXTURES` | `/app/fixtures.json` | fixture file imported on every boot (idempotent) |
| `DOGFOOD_BOOTSTRAP_DRAWS` | `400` | resamples behind rank bands |
| `DJANGO_SECRET_KEY` | none (a public dev key in demo mode or `DJANGO_DEBUG=1`) | **required in production** |
| `DJANGO_HTTPS` | `0` | behind TLS: secure cookies, proxy SSL header, HSTS |
| `DJANGO_HSTS_SECONDS` | `31536000` | HSTS max-age, used with `DJANGO_HTTPS` |
| `DJANGO_PROXY_COUNT` | `0` | number of reverse proxies in front; `X-Forwarded-For` is trusted for only this many hops. Never set it without a proxy that overwrites the header |
| `DOGFOOD_RATE_LIMITS` | `1` | `0` turns rate limits off |
| `DOGFOOD_RATE_LIMITS_SIGNUP_PER_IP` | `30` | signups per address per hour |
| `DJANGO_DEBUG` | `0` | Django debug mode. Also lifts the secret-key requirement, so development only |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | empty | comma-separated origins, e.g. `https://hack.example.org` |
| `DJANGO_ALLOWED_HOSTS` | `localhost,127.0.0.1,[::1]` | comma-separated |
| `DJANGO_DB_PATH`, `DJANGO_MEDIA_ROOT`, `DJANGO_CACHE_DIR` | under `/data` in Docker | storage locations |
| `PORT`, `WEB_WORKERS` | `8080`, `2` | gunicorn. The compose port mapping and the image's healthcheck assume 8080, so change them too if you change `PORT` |

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest                                    # 243 tests, about two minutes
cd src && DOGFOOD_SEED_SESSIONS=1 ../.venv/bin/python manage.py migrate && \
  DOGFOOD_SEED_SESSIONS=1 ../.venv/bin/python manage.py seed && \
  DOGFOOD_SEED_SESSIONS=1 ../.venv/bin/python manage.py runserver 8080
```

## Honest limitations

- **No email.** Judge invites and team invites are links the organizer or team copies by hand. That's deliberate,
  because the portal must run offline, but it means no password reset flow either (an admin resets passwords with
  `manage.py changepassword`).
- **No CAPTCHA and no email verification.** Login, signup, votes and comments are rate limited (login by account
  and address together, with a higher account-only ceiling; see `ratelimit.py`), but because the portal must run offline there is no email round trip, so anyone
  can make accounts with addresses they do not own. That is the main way to stuff the community vote. What we do
  instead: a vote needs an account, one vote per person per project, a per-person cap, hashed-address and
  account-age signals on the organizer dashboard, and an audit trail. Flags are for a human to check; the portal never
  removes a vote by itself, and organizers cannot void an individual vote or ban a voter. Flagged votes are reviewed
  in `votes.csv` after the close. Behind a shared NAT (a venue) the address checks can over-flag, and `DJANGO_PROXY_COUNT`
  must be right or every voter looks like one address.
- **Vote counts are hidden from organizers too** until voting closes, so the votes export is refused until then.
  During voting an organizer sees the total and flag counts, not who is ahead or which projects are flagged, and
  once voting has opened they cannot move the close earlier to peek. (Someone with database access can still count.)
- **SQLite** handles one writer at a time. That is fine for hundreds of participants and dozens of judges, not for a
  10,000-person public vote.
- **Uploads are served by Django itself.** That's simple and fine at hackathon scale; a large event should serve
  `/media/` from the reverse proxy. Each upload is checked with Pillow's `verify()` (format detection; the pixels
  are not decoded) and stored under the extension of the format it really is. It is then served with
  `Content-Security-Policy: default-src 'none'; img-src 'self'; sandbox` and `nosniff`, so a crafted file can't run
  script on the portal's origin. A whole form is capped at 15 MB before it is parsed.
- **The 80% rank bands run slightly narrow** (73% coverage in simulation); see JUDGING.md section 6.
- **Voting is HTML only.** Casting and taking back votes, and commenting, are form posts (the API only reads
  tallies and exports). Comments cannot be edited, only deleted by their author.
- **Not built:** T4 (REST API for every action, webhooks, certificates, signed judge records,
  widget), OpenAPI, and pairwise judging.
- **The JSON event export is a migration path, not a backup.** `export_event` carries teams, projects, judges, reviews
  and the rubric weights; it leaves out uploaded images, custom questions and answers, prizes, unreviewed assignments,
  organizers, the community-vote window (`voting_opens`, `voting_closes`, `votes_per_voter`), votes, comments and
  the audit log (see DATA-MODEL.md). The SQLite file plus the media volume is the full
  backup.
- A project reviewed only by flat judges falls back to its raw average (flagged low confidence).

## License

MIT, see [LICENSE](LICENSE). Built during the DOGFOOD 2026 window with Claude Code.
