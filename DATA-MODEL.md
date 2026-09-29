# Data model

Source of truth: [`src/portal/models.py`](src/portal/models.py) and its migrations. Everything below is
enforced by the database (constraints), not only by the code.

## Entities

```
Event ─┬─ Track ─────────────┐
       ├─ Prize (→ Track?)   │
       ├─ Criterion          │            User (Django auth; username = lower-case email)
       ├─ EventQuestion      │              │
       ├─ EventRole ─────────┼── tracks ──  ├─ EventRole (participant | judge | organizer, per event)
       ├─ JudgeInvite        │              ├─ TeamMembership ── Team
       ├─ Team ── TeamMembership            ├─ Assignment ── Project
       ├─ Project (→ Team, → Track?) ─┬─ Tag (M2M)
       │                              ├─ ProjectImage
       │                              ├─ ProjectAnswer (→ EventQuestion)
       │                              ├─ Assignment (→ judge User)
       │                              ├─ Review (→ judge User) ── CriterionScore (→ Criterion)
       │                              ├─ Vote (→ voter User)
       │                              └─ Comment (→ author User)
       └─ AuditLog (→ actor User?)
```

| Table | What it holds | Constraints that matter |
|---|---|---|
| **Event** | name, description, `submissions_open` (null = open now), `submissions_close`, `judging_close`, `results_published_at`, `reviews_per_project` (assignment target), `voting_opens` / `voting_closes` (both null = no community vote), `votes_per_voter` (default 3, 1 to 20 by the application) | `external_id` unique; opens before it closes; judging closes after submissions; **the voting window is both dates or neither, opening before it closes** (application also requires it to open no earlier than `submissions_close`) |
| **Track** | a category within an event | `(event, external_id)` unique; `PROTECT` from projects, so a track in use can't be deleted |
| **Prize** | name, free-text value ("800 USD"), optional track (blank = overall) | the count of overall prizes sets the "prize places" used for P(top k) |
| **EventRole** | user × event × role; judges also carry their fixture id (`jdg_01`) and the tracks they cover | `(user, event, role)` unique; `(event, external_id)` unique when set |
| **Team** | name, `invite_token` (random, rotatable) | `(event, external_id)` unique; token unique |
| **TeamMembership** | user on team; `event` copied from the team | **`(event, user)` unique: one team per person per event**, enforced by the database, not by a check that can race |
| **Project** | the stable submission fields: title, tagline, description, thumbnail, repo/demo-video/live URLs, tags, track; `status` draft or submitted; `submitted_at`; `withdrawn_at` and `withdrawn_reason` when an organizer takes it out of judging | `(event, external_id)` unique; **a submitted project must have `submitted_at`** |
| **Tag** | tech tags shared across events, lower-case slugs | name unique |
| **ProjectImage** | gallery images, ordered | max 6 per project (application rule) |
| **EventQuestion** / **ProjectAnswer** | organizer-defined questions and each project's answers | one answer per project per question |
| **Criterion** | rubric line: key, name, description, anchors for 1/3/5, relative `weight` | `(event, key)` unique; weight ≥ 0 |
| **JudgeInvite** | email, single-use token, who created and accepted it, when | token unique |
| **Assignment** | judge × project, `source` = auto / manual / import / tiebreak | `(judge, project)` unique |
| **Review** | one judge's review of one project + comment | **`(judge, project)` unique** |
| **CriterionScore** | one value per criterion per review | `(review, criterion)` unique; **value between 1 and 5** |
| **Vote** | one community vote: event, project, voter, `created_at`, `ip_hash` (sha256 of the secret key + client address; never the address) | **`(voter, project)` unique** |
| **Comment** | a public comment on a project: author, body, `created_at`, and when hidden: `hidden_at`, `hidden_by`, `hidden_reason` | none beyond the foreign keys; length, duplicates and rate are application rules |
| **AuditLog** | who, what (`action`), which (`target`), JSON detail, when; nullable event for account-level actions | append-only by convention (no update or delete path in the app) |

### Decisions worth defending

- **Roles are rows per event, not user flags.** The same person judges one event and competes in the next; checks
  are always "role in *this* event". Admin is Django's `is_superuser`; the right to host events is `is_staff`.
- **Scores are normalized in form: Review → CriterionScore.** Weights live on `Criterion`, not on scores, so an
  organizer can re-weight after judging and results recompute. Changing weights never rewrites a score.
- **Nothing derived is stored.** Weighted scores, bias-corrected scores, ranks and uncertainty are computed from
  reviews on demand (then cached by a fingerprint of the data), so they can't drift from the scores they come from.
- **`external_id` everywhere an import can land.** It is the fixture's string id (`prj_01`) for imported rows and a
  random id (`prj_3fa9c2d1`) for rows made in the app. Every URL uses it (database ids appear only in organizer-only form
  fields), and the import matches on it.
- **The duplicate `event` on TeamMembership** exists so the database, not application code, guarantees one team per
  person per event.
- **Uploads use random file names** (`thumbnails/<uuid>.png`), so they can't be enumerated and never collide. The
  extension is the format Pillow actually decoded, never the uploader's, and replaced images are deleted.
- **Withdrawing is a flag, not a delete.** A withdrawn project (a duplicate, a rules breach) leaves results, the
  gallery and judging queues, but it and its reviews stay for the audit trail and the exports.

## Import: fixtures.json → tables

`python manage.py seed [--fixtures path]` runs on every boot and is idempotent: every write is matched on
`external_id`, so rows are never duplicated. Tracks, judge roles, teams and memberships are upserted. The event,
projects, reviews and criterion scores are **create-only**, so live edits (new deadlines, a judge's changed score)
survive a restart. It accepts any file in the fixture's shape, so it is also the way to import an event from another
tool.

| fixtures.json | becomes |
|---|---|
| `event` | `Event` (its own `submissions_close`, so the fixture event is closed), created once |
| `tracks[]` | `Track` |
| `judges[]` | `User` (by email) + `EventRole(judge, external_id=id)` + covered tracks |
| `teams[].members[]` | `User` (by email) + `Team` + `TeamMembership` + `EventRole(participant)` |
| `projects[]` | `Project` (`summary` → `tagline`; submitted if `submitted_at` is set) |
| `scores[]` | `Assignment(source=import)` + `Review` + one `CriterionScore` per criterion |
| criteria keys seen in scores | `Criterion` with weight 1 and default anchors. Existing weights are left alone, so an organizer's changes survive reboots |

Imported users get unusable passwords. In demo mode, the seeded accounts also get the demo password. With demo mode
off, the seed revokes it: fixed sessions deleted, the demo password made unusable where still set, the demo admin
disabled.

## Export: tables → files

All organizer-only (401 anonymous, 403 anyone else), CSV with formula-injection protection (cells starting `=`, `+`,
`-` or `@` are prefixed with `'`):

| Stage | URL | One row per |
|---|---|---|
| Registration | `/api/events/<id>/teams.csv` | team member |
| Submission | `/api/events/<id>/submissions.csv` | project, drafts and withdrawn included, every field + each custom answer |
| Assignment | `/api/events/<id>/assignments.csv` | assignment, with source and whether it's reviewed |
| Scoring | `/api/export.csv?event=<id>` | review: every criterion, weighted score, leniency-adjusted score, comment |
| Results | `/api/events/<id>/results.csv` | project: rank, score, raw mean, rank band, P(first), P(prize), flags |
| Audit | `/api/events/<id>/audit.csv` | audit entry, oldest first |
| Community votes | `/api/events/<id>/votes.csv` | vote: project, voter email, time, first 12 characters of `ip_hash`, integrity flags. **403 `results_hidden` until voting closes**, because a row per vote is the tally |
| Comments | `/api/events/<id>/comments.csv` | comment, hidden ones included with who hid them and why |

The public People's choice tallies are `/events/<id>/votes` and `/api/events/<id>/votes` (JSON), visible to anyone
once voting has closed and to no one before.

**Whole-database backup and migration out:** the entire state is one SQLite file plus the uploads folder in the
`portal-data` volume. `docker compose cp portal:/data/db.sqlite3 .` gives a standard SQLite database that any tool
can read. A JSON export in the fixture's own shape (a full round trip) is on the roadmap, not built.

## Migrations

Django migrations, applied on every boot by the entrypoint (`migrate --noinput`). The schema is one initial migration
because it was designed during the event. Later changes are additive migrations: `0002_project_withdrawn`, and
`0003_t3_voting_comments` (the voting window on `Event`, `Vote`, `Comment`).
