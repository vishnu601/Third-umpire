"""Domain rules. Views (HTML and API) call these; nothing else writes.

Every refusal is a `Refused` with an HTTP status, so the same rule gives the
same answer whether it is reached by a form post or by curl.
"""

import hashlib
from collections import defaultdict
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.db import IntegrityError, transaction
from django.conf import settings
from django.core.cache import cache
from django.db.models import Count, Max
from django.utils import timezone
from django.utils.text import slugify
from PIL import Image

from . import scoring
from .models import (
    Assignment,
    AuditLog,
    Criterion,
    CriterionScore,
    Event,
    EventRole,
    JudgeInvite,
    Project,
    ProjectAnswer,
    ProjectImage,
    Review,
    Tag,
    Team,
    TeamMembership,
    mint_id,
)


class Refused(Exception):
    def __init__(self, status, code, message=""):
        super().__init__(message or code)
        self.status = status
        self.code = code
        self.message = message or code.replace("_", " ")


# --- roles --------------------------------------------------------------------


def has_role(user, event, role):
    if not user.is_authenticated:
        return False
    return EventRole.objects.filter(user=user, event=event, role=role).exists()


def is_organizer(user, event):
    return user.is_authenticated and (user.is_superuser or has_role(user, event, EventRole.Role.ORGANIZER))


def is_judge(user, event):
    return has_role(user, event, EventRole.Role.JUDGE)


def can_create_events(user):
    return user.is_authenticated and (user.is_superuser or user.is_staff)


def require_login(user):
    if not user.is_authenticated:
        raise Refused(401, "not_authenticated", "log in first")


def require_organizer(user, event):
    require_login(user)
    if not is_organizer(user, event):
        raise Refused(403, "not_an_organizer", "only this event's organizers can do that")


def require_judge(user, event):
    require_login(user)
    if not is_judge(user, event):
        raise Refused(403, "not_a_judge", "you are not a judge for this event")


def grant_role(event, user, role, actor=None, **fields):
    obj, created = EventRole.objects.get_or_create(event=event, user=user, role=role, defaults=fields)
    if created:
        audit(event, actor, "role.granted", user.username, role=role)
    return obj


# --- audit --------------------------------------------------------------------


def audit(event, actor, action, target="", **detail):
    return AuditLog.objects.create(
        event=event,
        actor=actor if actor is not None and actor.is_authenticated else None,
        action=action,
        target=str(target),
        detail=detail,
    )


# --- events -------------------------------------------------------------------


EVENT_FIELDS = ("name", "description", "submissions_open", "submissions_close", "judging_close", "reviews_per_project")

# What a 1, 3 and 5 mean. Shared anchors calibrate judges before they score,
# which shrinks the bias normalization has to remove afterwards.
DEFAULT_CRITERIA = [
    ("functionality", "Functionality", "Does not run, or the core flow is broken",
     "The core flow works, with rough edges", "Everything it claims works, including the edge cases"),
    ("quality", "Quality", "Hard to read or change, no tests",
     "Reasonable structure and some tests", "Idiomatic, well tested, easy to extend"),
    ("innovation", "Innovation", "A copy of something that already exists",
     "A known idea with a real twist", "A new idea or approach others would copy"),
]


def validate_event_fields(fields):
    validate_event_dates(fields.get("submissions_open"), fields.get("submissions_close"), fields.get("judging_close"))
    k = fields.get("reviews_per_project", 3)
    if not isinstance(k, int) or not 1 <= k <= 10:
        raise Refused(400, "invalid_reviews_per_project", "reviews per project must be between 1 and 10")


def validate_event_dates(opens, closes, judging_close):
    if closes is None:
        raise Refused(400, "invalid_dates", "a submission deadline is required")
    if opens and opens >= closes:
        raise Refused(400, "invalid_dates", "submissions must open before they close")
    if judging_close and judging_close < closes:
        raise Refused(400, "invalid_dates", "judging cannot close before submissions do")


@transaction.atomic
def create_event(actor, **fields):
    if not can_create_events(actor):
        raise Refused(403, "cannot_create_events", "ask an admin to let you host events")
    validate_event_fields(fields)
    event = Event.objects.create(external_id=mint_id("evt"), **fields)
    grant_role(event, actor, EventRole.Role.ORGANIZER, actor=actor)
    for position, (key, name, low, mid, high) in enumerate(DEFAULT_CRITERIA):
        Criterion.objects.create(
            event=event, key=key, name=name, position=position, anchor_low=low, anchor_mid=mid, anchor_high=high
        )
    audit(event, actor, "event.created", event.external_id, **_jsonable(fields))
    return event


@transaction.atomic
def update_event(event, actor, **fields):
    require_organizer(actor, event)
    validate_event_fields(fields)
    before = {f: getattr(event, f) for f in fields}
    for name, value in fields.items():
        setattr(event, name, value)
    event.save()
    changed = {f: [_jsonable_value(before[f]), _jsonable_value(v)] for f, v in fields.items() if before[f] != v}
    if changed:
        audit(event, actor, "event.updated", event.external_id, changed=changed)
    return event


def set_results_published(event, actor, published):
    require_organizer(actor, event)
    event.results_published_at = timezone.now() if published else None
    event.save(update_fields=["results_published_at"])
    audit(event, actor, "results.published" if published else "results.unpublished", event.external_id)


def _jsonable_value(v):
    return v.isoformat() if hasattr(v, "isoformat") else v


def _jsonable(d):
    return {k: _jsonable_value(v) for k, v in d.items()}


# --- teams and projects ---------------------------------------------------------


def ensure_submissions_open(event, actor, attempted):
    """The one deadline gate. Every write by participants passes through here.

    Call it outside any transaction: the refusal is audited, and a rollback
    would erase that record.
    """
    phase = event.phase()
    if phase == Event.Phase.OPEN:
        return
    audit(event, actor, "deadline.refused", attempted, phase=phase)
    if phase == Event.Phase.NOT_OPEN:
        raise Refused(403, "submissions_not_open", f"submissions open at {event.submissions_open.isoformat()}")
    raise Refused(403, "submissions_closed", f"submissions closed at {event.submissions_close.isoformat()}")


def team_of(user, event):
    if not user.is_authenticated:
        return None
    m = TeamMembership.objects.filter(user=user, event=event).select_related("team").first()
    return m.team if m else None


def is_team_member(user, team):
    return user.is_authenticated and TeamMembership.objects.filter(team=team, user=user).exists()


def create_team(event, user, name):
    require_login(user)
    ensure_submissions_open(event, user, "team.create")
    name = (name or "").strip()
    if not name or len(name) > 200:
        raise Refused(400, "invalid_name", "team name is required, at most 200 characters")
    if is_judge(user, event):
        raise Refused(403, "judges_cannot_compete", "judges of this event cannot join a team in it")
    if team_of(user, event):
        raise Refused(409, "already_on_a_team", "you are already on a team in this event")
    with transaction.atomic():
        team = Team.objects.create(event=event, external_id=mint_id("tm"), name=name)
        TeamMembership.objects.create(team=team, user=user)
        grant_role(event, user, EventRole.Role.PARTICIPANT, actor=user)
        audit(event, user, "team.created", team.external_id, name=name)
    return team


def join_team(team, user):
    require_login(user)
    event = team.event
    ensure_submissions_open(event, user, "team.join")
    if is_judge(user, event):
        raise Refused(403, "judges_cannot_compete", "judges of this event cannot join a team in it")
    current = team_of(user, event)
    if current == team:
        return team
    if current:
        raise Refused(409, "already_on_a_team", f"you are already on {current.name} in this event")
    try:
        with transaction.atomic():
            TeamMembership.objects.create(team=team, user=user)
            grant_role(event, user, EventRole.Role.PARTICIPANT, actor=user)
            audit(event, user, "team.joined", team.external_id)
    except IntegrityError:
        raise Refused(409, "already_on_a_team")
    return team


def rotate_invite(team, user):
    if not is_team_member(user, team):
        raise Refused(403, "not_on_team")
    team.invite_token = Team._meta.get_field("invite_token").default()
    team.save(update_fields=["invite_token"])
    audit(team.event, user, "team.invite_rotated", team.external_id)


MAX_TAGS = 10
MAX_GALLERY_IMAGES = 6
MAX_IMAGE_BYTES = 2 * 1024 * 1024
IMAGE_FORMATS = {"JPEG", "PNG", "GIF", "WEBP"}


def clean_url(value, label):
    value = str(value or "").strip()
    if not value:
        return ""
    if len(value) > 200 or not value.startswith(("https://", "http://")):
        raise Refused(400, "invalid_url", f"{label} must be a full http(s) URL, at most 200 characters")
    try:
        URLValidator(schemes=["http", "https"])(value)
    except ValidationError:
        raise Refused(400, "invalid_url", f"{label} is not a valid URL")
    return value


def clean_tags(raw):
    parts = raw if isinstance(raw, (list, tuple)) else str(raw or "").split(",")
    tags = []
    for part in parts:
        tag = slugify(str(part))[:40]
        if tag and tag not in tags:
            tags.append(tag)
    if len(tags) > MAX_TAGS:
        raise Refused(400, "too_many_tags", f"at most {MAX_TAGS} tech tags")
    return tags


def clean_project_fields(event, data):
    """Validate the stable submission fields. Returns (fields, tags)."""
    title = str(data.get("title") or "").strip()
    if not title or len(title) > 200:
        raise Refused(400, "invalid_title", "project name is required, at most 200 characters")
    # "summary" is the fixture's (and the checker's) name for the tagline.
    tagline = str(data.get("tagline") or data.get("summary") or "").strip()
    if len(tagline) > 200:
        raise Refused(400, "invalid_tagline", "tagline is at most 200 characters")
    description = str(data.get("description") or "").strip()
    if len(description) > 20000:
        raise Refused(400, "invalid_description", "description is at most 20,000 characters")
    track = None
    if data.get("track"):
        track = event.tracks.filter(external_id=data.get("track")).first()
        if track is None:
            raise Refused(400, "unknown_track")
    fields = {
        "title": title,
        "tagline": tagline,
        "description": description,
        "repo_url": clean_url(data.get("repo_url"), "Repository URL"),
        "demo_video_url": clean_url(data.get("demo_video_url"), "Demo video URL"),
        "live_url": clean_url(data.get("live_url"), "Live link"),
        "track": track,
    }
    return fields, clean_tags(data.get("tags"))


def clean_answers(event, data):
    """Answers to the event's custom questions, from form fields answer_<id> or a JSON "answers" object."""
    posted = data.get("answers") if isinstance(data.get("answers"), dict) else {}
    out = {}
    for q in event.questions.all():
        key = f"answer_{q.pk}"
        if key in data or str(q.pk) in posted:
            text = str(data.get(key) if key in data else posted.get(str(q.pk)) or "").strip()
            if len(text) > 5000:
                raise Refused(400, "answer_too_long", f"answer to \"{q.prompt}\" is at most 5,000 characters")
            out[q] = text
    return out


def check_image(upload, label):
    if upload.size > MAX_IMAGE_BYTES:
        raise Refused(400, "image_too_large", f"{label} must be at most 2 MB")
    try:
        with Image.open(upload) as img:
            fmt = img.format
            img.verify()
    except Exception:
        raise Refused(400, "invalid_image", f"{label} is not a readable image")
    if fmt not in IMAGE_FORMATS:
        raise Refused(400, "invalid_image", f"{label} must be JPEG, PNG, GIF or WebP")
    upload.seek(0)


def missing_required_answers(project, pending=None):
    pending = pending or {}
    saved = {a.question_id: a.answer for a in project.answers.all()}
    return [
        q.prompt
        for q in project.event.questions.filter(required=True)
        if not (pending.get(q, saved.get(q.pk)) or "").strip()
    ]


def _save_project(project, user, fields, tags, answers, files, submit, created):
    """Write fields, tags, answers and images in one transaction and audit what changed."""
    files = files or {}
    thumbnail = files.get("thumbnail")
    gallery = [f for f in files.get("gallery", []) if f]
    remove = {str(i) for i in files.get("remove_images", [])}
    if thumbnail:
        check_image(thumbnail, "Thumbnail")
    for f in gallery:
        check_image(f, "Gallery image")
    keep = project.images.exclude(pk__in=[i for i in remove if i.isdigit()]).count() if project.pk else 0
    if keep + len(gallery) > MAX_GALLERY_IMAGES:
        raise Refused(400, "too_many_images", f"at most {MAX_GALLERY_IMAGES} gallery images")

    changed = [f for f, v in fields.items() if getattr(project, f, None) != v] if not created else ["created"]
    for name, value in fields.items():
        setattr(project, name, value)
    if submit:
        missing = missing_required_answers(project, answers) if project.pk else [
            q.prompt for q in project.event.questions.filter(required=True) if not (answers.get(q) or "").strip()
        ]
        if missing:
            raise Refused(400, "answers_required", "answer the required questions before submitting: " + "; ".join(missing))
        if project.status == Project.Status.DRAFT:
            project.status = Project.Status.SUBMITTED
            project.submitted_at = timezone.now()
            changed.append("status")

    with transaction.atomic():
        if thumbnail:
            project.thumbnail = thumbnail
            changed.append("thumbnail")
        elif files.get("clear_thumbnail") and project.thumbnail:
            project.thumbnail = ""
            changed.append("thumbnail")
        project.save()
        tag_objs = [Tag.objects.get_or_create(name=t)[0] for t in tags]
        if set(project.tags.values_list("name", flat=True)) != set(tags):
            project.tags.set(tag_objs)
            changed.append("tags")
        for q, text in answers.items():
            obj, made = ProjectAnswer.objects.get_or_create(project=project, question=q, defaults={"answer": text})
            if not made and obj.answer != text:
                obj.answer = text
                obj.save(update_fields=["answer"])
                changed.append(f"answer:{q.pk}")
            elif made:
                changed.append(f"answer:{q.pk}")
        if remove:
            n, _ = project.images.filter(pk__in=[i for i in remove if i.isdigit()]).delete()
            if n:
                changed.append("gallery")
        start = project.images.count()
        for i, f in enumerate(gallery):
            ProjectImage.objects.create(project=project, image=f, position=start + i)
        if gallery:
            changed.append("gallery")
        if changed:
            action = "project.created" if created else ("project.submitted" if "status" in changed else "project.updated")
            audit(project.event, user, action, project.external_id, fields=sorted(set(changed)))
    return project


def create_project(team, user, data, files=None, submit=False):
    if not is_team_member(user, team):
        raise Refused(403, "not_a_participant", "you are not on this team")
    ensure_submissions_open(team.event, user, "project.create")
    if team.projects.exists():
        raise Refused(409, "team_already_has_project", "edit your team's existing project instead")
    fields, tags = clean_project_fields(team.event, data)
    answers = clean_answers(team.event, data)
    project = Project(event=team.event, team=team, external_id=mint_id("prj"), status=Project.Status.DRAFT)
    return _save_project(project, user, fields, tags, answers, files, submit, created=True)


def update_project(project, user, data, submit=False, files=None):
    if not is_team_member(user, project.team):
        raise Refused(403, "not_a_participant", "you are not on this team")
    ensure_submissions_open(project.event, user, "project.update")
    fields, tags = clean_project_fields(project.event, data)
    answers = clean_answers(project.event, data)
    return _save_project(project, user, fields, tags, answers, files, submit, created=False)


def can_view_project(user, project):
    if project.status == Project.Status.SUBMITTED:
        return True
    return is_team_member(user, project.team) or is_organizer(user, project.event)


# --- judges ----------------------------------------------------------------------


def invite_judge(event, actor, email):
    require_organizer(actor, event)
    email = (email or "").strip().lower()
    if "@" not in email:
        raise Refused(400, "invalid_email")
    invite = JudgeInvite.objects.create(event=event, email=email, created_by=actor)
    audit(event, actor, "judge.invited", email)
    return invite


@transaction.atomic
def accept_judge_invite(invite, user):
    require_login(user)
    if invite.accepted_at:
        if invite.accepted_by_id == user.pk:
            return invite
        raise Refused(410, "invite_used", "this invite has already been used")
    if user.email.lower() != invite.email:
        raise Refused(403, "wrong_account", f"this invite is for {invite.email}; log in with that account")
    if team_of(user, invite.event):
        raise Refused(409, "competitor_cannot_judge", "you are on a team in this event, so you cannot judge it")
    grant_role(invite.event, user, EventRole.Role.JUDGE, actor=user, external_id=mint_id("jdg"))
    invite.accepted_by = user
    invite.accepted_at = timezone.now()
    invite.save(update_fields=["accepted_by", "accepted_at"])
    audit(invite.event, user, "judge.accepted", user.username)
    return invite


def conflicted(judge_user, project):
    return TeamMembership.objects.filter(team_id=project.team_id, user=judge_user).exists()


@transaction.atomic
def auto_assign(event, actor, per_project=None):
    """Top every submitted project up to `per_project` judges (default: the event's target).

    Greedy and deterministic: projects with the fewest judges go first; each
    takes the least-loaded judges who cover its track (generalists count),
    never a judge on the project's own team. Existing assignments are kept, so
    running it again only fills gaps. Returns the number of new assignments.
    """
    require_organizer(actor, event)
    per_project = per_project or event.reviews_per_project
    if not 1 <= per_project <= 10:
        raise Refused(400, "invalid_per_project", "judges per project must be between 1 and 10")

    judges = list(EventRole.objects.filter(event=event, role=EventRole.Role.JUDGE).select_related("user").prefetch_related("tracks"))
    if not judges:
        raise Refused(409, "no_judges", "invite judges before assigning")
    covers = {j.user_id: {t.id for t in j.tracks.all()} for j in judges}
    load = defaultdict(int)
    for row in Assignment.objects.filter(event=event).values("judge_id").annotate(n=Count("id")):
        load[row["judge_id"]] = row["n"]
    existing = defaultdict(set)
    for judge_id, project_id in Assignment.objects.filter(event=event).values_list("judge_id", "project_id"):
        existing[project_id].add(judge_id)
    members = defaultdict(set)
    for team_id, user_id in TeamMembership.objects.filter(event=event).values_list("team_id", "user_id"):
        members[team_id].add(user_id)

    projects = sorted(
        Project.objects.filter(event=event, status=Project.Status.SUBMITTED),
        key=lambda p: (len(existing[p.id]), p.external_id),
    )
    created = []
    for project in projects:
        need = per_project - len(existing[project.id])
        if need <= 0:
            continue
        eligible = [
            j for j in judges
            if j.user_id not in existing[project.id] and j.user_id not in members[project.team_id]
        ]
        on_track = [j for j in eligible if not covers[j.user_id] or project.track_id in covers[j.user_id]]
        off_track = [j for j in eligible if j not in on_track]
        rank = lambda j: (load[j.user_id], j.external_id or j.user.username)
        for j in (sorted(on_track, key=rank) + sorted(off_track, key=rank))[:need]:
            created.append(Assignment(event=event, judge_id=j.user_id, project=project, source=Assignment.Source.AUTO))
            existing[project.id].add(j.user_id)
            load[j.user_id] += 1
    Assignment.objects.bulk_create(created)
    audit(event, actor, "assignments.generated", event.external_id, per_project=per_project, created=len(created))
    return len(created)


@transaction.atomic
def assign_batch(event, actor, judge_user, projects):
    """Manual (batch) assignment: one judge, many projects. Skips existing pairs."""
    require_organizer(actor, event)
    if not is_judge(judge_user, event):
        raise Refused(400, "not_a_judge", "that person is not a judge for this event")
    made, skipped = [], []
    for project in projects:
        if project.event_id != event.id or project.status != Project.Status.SUBMITTED:
            raise Refused(400, "not_assignable", f"{project.title} is not a submitted project in this event")
        if conflicted(judge_user, project):
            raise Refused(409, "conflict_of_interest", f"{judge_user.username} is on the team behind {project.title}")
        _, created = Assignment.objects.get_or_create(
            event=event, judge=judge_user, project=project, defaults={"source": Assignment.Source.MANUAL}
        )
        (made if created else skipped).append(project.external_id)
    if made:
        audit(event, actor, "assignments.batch", judge_user.username, projects=made)
    return made, skipped


def unassign(event, actor, assignment):
    require_organizer(actor, event)
    if assignment.event_id != event.id:
        raise Refused(404, "not_found")
    if Review.objects.filter(judge_id=assignment.judge_id, project_id=assignment.project_id).exists():
        raise Refused(409, "already_reviewed", "this judge has already reviewed the project; the review stays")
    assignment.delete()
    audit(event, actor, "assignments.removed", assignment.project.external_id, judge=assignment.judge.username)


def assigned_projects(judge_user, event=None):
    qs = Project.objects.filter(assignments__judge=judge_user).select_related("event", "track", "team")
    if event is not None:
        qs = qs.filter(event=event)
    return qs.distinct()


@transaction.atomic
def save_review(project, judge_user, values, comment):
    """Create or replace this judge's own review. Only assigned, unconflicted, submitted projects."""
    event = project.event
    require_judge(judge_user, event)
    if not Assignment.objects.filter(judge=judge_user, project=project).exists():
        raise Refused(403, "not_assigned", "this project is not assigned to you")
    if conflicted(judge_user, project):
        raise Refused(403, "conflict_of_interest")
    if project.status != Project.Status.SUBMITTED:
        raise Refused(409, "not_submitted")
    if not event.accepts_reviews():
        raise Refused(403, "judging_closed", "judging has closed for this event")

    criteria = list(event.criteria.all())
    clean = {}
    for c in criteria:
        try:
            v = int(values.get(c.key))
        except (TypeError, ValueError):
            raise Refused(400, "missing_score", f"score {c.name} from 1 to 5")
        if not 1 <= v <= 5:
            raise Refused(400, "score_out_of_range", f"{c.name} must be 1 to 5")
        clean[c] = v

    review, created = Review.objects.get_or_create(judge=judge_user, project=project, defaults={"comment": comment})
    before = {s.criterion.key: s.value for s in review.scores.select_related("criterion")}
    review.comment = comment
    review.save()
    for c, v in clean.items():
        CriterionScore.objects.update_or_create(review=review, criterion=c, defaults={"value": v})
    after = {c.key: v for c, v in clean.items()}
    if created or before != after:
        audit(event, judge_user, "review.created" if created else "review.changed", project.external_id,
              before=before or None, after=after)
    return review


# --- results -----------------------------------------------------------------------


def prize_slots(event):
    """How many overall placings the event awards (defaults to 3)."""
    return event.prizes.filter(track__isnull=True).count() or 3


def _review_points(event):
    weights = {c.key: c.weight for c in event.criteria.all()}
    reviews = Review.objects.filter(project__event=event).prefetch_related("scores__criterion")
    points, per_criterion = [], defaultdict(list)
    for r in reviews:
        values = {s.criterion.key: s.value for s in r.scores.all()}
        raw = scoring.weighted_score(values, weights)
        if raw is not None:
            points.append(scoring.ReviewPoint(judge=str(r.judge_id), project=r.project_id, raw=raw))
        for key, v in values.items():
            per_criterion[key].append(scoring.ReviewPoint(judge=str(r.judge_id), project=r.project_id, raw=float(v)))
    return points, per_criterion


def results_fingerprint(event):
    agg = Review.objects.filter(project__event=event).aggregate(n=Count("id"), last=Max("updated_at"))
    weights = tuple(event.criteria.values_list("key", "weight"))
    return f"{event.pk}:{agg['n']}:{agg['last'].isoformat() if agg['last'] else '-'}:{weights}:{prize_slots(event)}"


def event_results(event, draws=None):
    """The two-way model plus bootstrap for an event, cached by a fingerprint of its data.

    Callers must already have checked organizer access, or that results are published.
    """
    draws = settings.DOGFOOD_BOOTSTRAP_DRAWS if draws is None else draws
    key = "analysis:" + hashlib.sha256(f"{results_fingerprint(event)}:{draws}".encode()).hexdigest()
    result = cache.get(key)
    if result is None:
        points, _ = _review_points(event)
        result = scoring.analyse(points, prize_slots=prize_slots(event), draws=draws)
        cache.set(key, result)
    return result


def criterion_leaders(event):
    """Per-criterion winners (category awards), using the same bias model on each criterion alone."""
    _, per_criterion = _review_points(event)
    out = []
    for c in event.criteria.filter(weight__gt=0):
        ranking = scoring.analyse(per_criterion.get(c.key, []), draws=0).ranking()
        if ranking:
            out.append({"criterion": c, "project_id": ranking[0].project, "score": ranking[0].score,
                        "runner_up": ranking[1].score if len(ranking) > 1 else None})
    return out


def tiebreak_suggestions(event, analysis=None, limit=8):
    """One extra judge for each contested project, chosen to link the contested projects.

    A judge who has already reviewed another contested project compares the two
    directly, which is what shrinks the uncertainty between them. Track fit and
    low load break ties; judges on the project's team and flat judges are never
    suggested.
    """
    analysis = analysis or event_results(event)
    contested = [p for p in analysis.ranking() if p.contested][:limit]
    if not contested:
        return []
    ids = [p.project for p in contested]
    projects = {p.id: p for p in Project.objects.filter(pk__in=ids)}
    roles = list(EventRole.objects.filter(event=event, role=EventRole.Role.JUDGE).select_related("user").prefetch_related("tracks"))
    flat = {int(j) for j, e in analysis.judges.items() if e.flat}
    assigned = defaultdict(set)
    for judge_id, project_id in Assignment.objects.filter(event=event).values_list("judge_id", "project_id"):
        assigned[judge_id].add(project_id)
    members = defaultdict(set)
    for team_id, user_id in TeamMembership.objects.filter(event=event).values_list("team_id", "user_id"):
        members[team_id].add(user_id)
    suggested = defaultdict(int)
    out = []
    for ps in contested:
        project = projects[ps.project]
        best = None
        for r in roles:
            uid = r.user_id
            if uid in flat or project.id in assigned[uid] or uid in members[project.team_id]:
                continue
            links = sorted(x for x in assigned[uid] if x in projects and x != project.id)
            tracks = {t.id for t in r.tracks.all()}
            on_track = not tracks or project.track_id in tracks
            rank = (len(links) > 0, on_track, -suggested[uid], -len(assigned[uid]), r.external_id)
            if best is None or rank > best[0]:
                best = (rank, r, links, on_track)
        if best:
            _, r, links, on_track = best
            suggested[r.user_id] += 1
            out.append({
                "project": project,
                "score": ps,
                "role": r,
                "links": [projects[x] for x in links],
                "on_track": on_track,
            })
    return out


@transaction.atomic
def apply_tiebreaks(event, actor):
    require_organizer(actor, event)
    made = 0
    for s in tiebreak_suggestions(event):
        _, created = Assignment.objects.get_or_create(
            event=event, judge=s["role"].user, project=s["project"], defaults={"source": Assignment.Source.TIEBREAK}
        )
        made += created
    audit(event, actor, "assignments.tiebreak", event.external_id, created=made)
    return made


def progress(event):
    """Assignment completion per judge and per project, for the organizer dashboard."""
    assignments = list(Assignment.objects.filter(event=event).values_list("judge_id", "project_id"))
    reviewed = set(Review.objects.filter(project__event=event).values_list("judge_id", "project_id"))
    judges = {
        r.user_id: r for r in EventRole.objects.filter(event=event, role=EventRole.Role.JUDGE).select_related("user")
    }
    per_judge = defaultdict(lambda: [0, 0])
    per_project = defaultdict(lambda: [0, 0])
    for judge_id, project_id in assignments:
        done = (judge_id, project_id) in reviewed
        per_judge[judge_id][0] += 1
        per_judge[judge_id][1] += done
        per_project[project_id][0] += 1
        per_project[project_id][1] += done
    projects = {p.id: p for p in Project.objects.filter(event=event).select_related("team", "track")}
    submitted = [p for p in projects.values() if p.status == Project.Status.SUBMITTED]
    total = len(assignments)
    done = sum(1 for a in assignments if a in reviewed)
    return {
        "assigned": total,
        "done": done,
        "percent": round(100 * done / total) if total else 0,
        "projects_submitted": len(submitted),
        "projects_draft": len(projects) - len(submitted),
        "unassigned_projects": [p for p in submitted if p.id not in per_project],
        "judges": sorted(
            (
                {"role": judges[j], "assigned": a, "done": d, "percent": round(100 * d / a) if a else 0}
                for j, (a, d) in per_judge.items()
                if j in judges
            ),
            key=lambda r: (r["percent"], r["role"].external_id),
        ),
        "idle_judges": [r for j, r in judges.items() if j not in per_judge],
        "projects": sorted(
            ({"project": projects[p], "assigned": a, "done": d} for p, (a, d) in per_project.items()),
            key=lambda r: (r["done"] - r["assigned"], r["project"].external_id),
        ),
        "integrity": integrity_report(event),
    }


LAST_MINUTE = timedelta(minutes=10)


def integrity_report(event):
    """The data problems an organizer should look at before trusting a ranking.

    Each finding names the rows involved, so it can be acted on: top up
    under-reviewed projects, chase light judges, check duplicates.
    """
    weights = {c.key: c.weight for c in event.criteria.all()}
    roles = {r.user_id: r for r in EventRole.objects.filter(event=event, role=EventRole.Role.JUDGE).select_related("user")}
    reviews = list(
        Review.objects.filter(project__event=event).select_related("project").prefetch_related("scores__criterion")
    )
    by_judge = defaultdict(list)
    per_project = defaultdict(int)
    edges = []
    for r in reviews:
        raw = scoring.weighted_score({s.criterion.key: s.value for s in r.scores.all()}, weights)
        by_judge[r.judge_id].append(raw)
        per_project[r.project_id] += 1
        edges.append((r.judge_id, r.project_id))

    flat = [
        {"role": roles.get(j), "reviews": len(xs), "score": xs[0]}
        for j, xs in by_judge.items()
        if len(xs) >= 2 and len(set(round(x, 6) for x in xs if x is not None)) == 1
    ]
    counts = sorted(len(xs) for xs in by_judge.values())
    median = counts[len(counts) // 2] if counts else 0
    light = [
        {"role": roles.get(j), "reviews": len(xs), "median": median}
        for j, xs in by_judge.items()
        if median >= 3 and len(xs) * 3 <= median
    ]
    pending = defaultdict(int)
    reviewed = set(edges)
    for judge_id, project_id in Assignment.objects.filter(event=event).values_list("judge_id", "project_id"):
        if (judge_id, project_id) not in reviewed:
            pending[project_id] += 1
    projects = list(Project.objects.filter(event=event).select_related("team", "track"))
    submitted = [p for p in projects if p.status == Project.Status.SUBMITTED]
    k = event.reviews_per_project
    under = [
        {"project": p, "reviews": per_project[p.id], "pending": pending[p.id]}
        for p in submitted
        if per_project[p.id] < k
    ]
    close = event.submissions_close
    last_minute = [
        {"project": p, "before_close": close - p.submitted_at}
        for p in submitted
        if p.submitted_at and close - LAST_MINUTE <= p.submitted_at <= close
    ]
    late = [p for p in submitted if p.submitted_at and p.submitted_at > close]
    comps = scoring.components(edges)
    return {
        "flat_judges": sorted(flat, key=lambda f: f["role"].external_id if f["role"] else ""),
        "light_judges": sorted(light, key=lambda f: f["role"].external_id if f["role"] else ""),
        "under_reviewed": sorted(under, key=lambda u: (u["reviews"], u["project"].external_id)),
        "target": k,
        "duplicates": possible_duplicates(event),
        "last_minute": sorted(last_minute, key=lambda x: x["before_close"]),
        "late": late,
        "components": len(comps),
        "component_sizes": sorted(((len(js), len(ps)) for js, ps in comps), reverse=True),
        "issues": len(flat) + len(light) + len(under) + len(possible_duplicates(event)) + len(late)
        + (1 if len(comps) > 1 else 0),
    }


def possible_duplicates(event):
    """Projects sharing a normalized title within an event, or a team with several projects."""
    groups = defaultdict(list)
    for p in Project.objects.filter(event=event).select_related("team"):
        groups[("title", " ".join(p.title.lower().split()))].append(p)
        groups[("team", p.team_id)].append(p)
    seen, out = set(), []
    for (kind, _), ps in groups.items():
        key = tuple(sorted(p.id for p in ps))
        if len(ps) > 1 and key not in seen:
            seen.add(key)
            out.append({"reason": "same title" if kind == "title" else "same team", "projects": ps})
    return out
