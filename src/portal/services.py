"""Domain rules. Views (HTML and API) call these; nothing else writes.

Every refusal is a `Refused` with an HTTP status, so the same rule gives the
same answer whether it is reached by a form post or by curl.
"""

import hashlib
import random
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.db import IntegrityError, transaction
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.files.storage import default_storage
from django.db.models import Count, Max
from django.utils import timezone
from django.utils.text import slugify
from PIL import Image

from . import scoring
from .models import (
    Assignment,
    AuditLog,
    Comment,
    Criterion,
    CriterionScore,
    Event,
    EventQuestion,
    EventRole,
    JudgeInvite,
    Prize,
    Project,
    ProjectAnswer,
    ProjectImage,
    Review,
    Tag,
    Team,
    TeamMembership,
    Track,
    Vote,
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


def add_organizer(event, actor, email):
    """Give an existing account organizer rights on this event. Audited by grant_role."""
    require_organizer(actor, event)
    email = str(email or "").strip().lower()
    user = get_user_model().objects.filter(username=email).first()
    if user is None:
        raise Refused(404, "no_such_account", "no account uses that email; ask them to sign up first")
    if TeamMembership.objects.filter(event=event, user=user).exists():
        raise Refused(409, "competing", "that person is on a team in this event")
    return grant_role(event, user, EventRole.Role.ORGANIZER, actor=actor)


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


EVENT_FIELDS = (
    "name", "description", "submissions_open", "submissions_close", "judging_close", "reviews_per_project",
    "voting_opens", "voting_closes", "votes_per_voter",
)

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
    validate_voting_window(fields)


MAX_VOTES_PER_VOTER = 20


def validate_voting_window(fields):
    """The community vote is all or nothing, comes after submissions close, and has a sane ballot size."""
    opens, closes = fields.get("voting_opens"), fields.get("voting_closes")
    if (opens is None) != (closes is None):
        raise Refused(400, "invalid_voting_window", "set both voting dates, or leave both empty for no community vote")
    if opens is not None:
        if opens >= closes:
            raise Refused(400, "invalid_voting_window", "voting must open before it closes")
        if fields.get("submissions_close") and opens < fields["submissions_close"]:
            raise Refused(400, "invalid_voting_window", "voting cannot open before submissions close")
    n = fields.get("votes_per_voter", 3)
    if not isinstance(n, int) or not 1 <= n <= MAX_VOTES_PER_VOTER:
        raise Refused(400, "invalid_votes_per_voter", f"votes per voter must be between 1 and {MAX_VOTES_PER_VOTER}")


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


# --- event setup: tracks, prizes, rubric, questions ------------------------------------------


def _required_text(value, field, limit=200):
    text = str(value or "").strip()[:limit]
    if not text:
        raise Refused(400, f"missing_{field}", f"{field} is required")
    return text


@transaction.atomic
def add_track(event, actor, name):
    require_organizer(actor, event)
    track = Track.objects.create(event=event, external_id=mint_id("trk"), name=_required_text(name, "name"))
    audit(event, actor, "track.added", track.external_id, name=track.name)
    return track


@transaction.atomic
def delete_track(event, actor, track):
    require_organizer(actor, event)
    if track.projects.exists():
        raise Refused(409, "track_in_use", "projects are entered in this track; move them first")
    if track.prizes.exists():
        raise Refused(409, "track_in_use", "a prize is tied to this track; delete or move the prize first")
    if track.judges.exists():
        raise Refused(409, "track_in_use", "judges cover this track; change their tracks first")
    external_id, name = track.external_id, track.name
    track.delete()
    audit(event, actor, "track.deleted", external_id, name=name)


@transaction.atomic
def add_prize(event, actor, name, value="", track_id=None):
    require_organizer(actor, event)
    track = None
    if track_id:
        track = event.tracks.filter(external_id=track_id).first()
        if track is None:
            raise Refused(400, "unknown_track")
    prize = Prize.objects.create(
        event=event,
        name=_required_text(name, "name"),
        value=str(value or "").strip()[:100],
        track=track,
        position=event.prizes.count(),
    )
    audit(event, actor, "prize.added", prize.pk, name=prize.name, value=prize.value)
    return prize


@transaction.atomic
def delete_prize(event, actor, prize):
    require_organizer(actor, event)
    pk, name = prize.pk, prize.name
    prize.delete()
    audit(event, actor, "prize.deleted", pk, name=name)


@transaction.atomic
def set_weights(event, actor, raw_weights):
    """raw_weights maps criterion pk to the posted text. Criteria left out keep their weight."""
    require_organizer(actor, event)
    criteria = list(event.criteria.all())
    new = {}
    for c in criteria:
        raw = raw_weights.get(c.pk)
        if raw is None:
            new[c] = c.weight
            continue
        try:
            w = Decimal(str(raw))
        except InvalidOperation:
            raise Refused(400, "invalid_weight", f"weight for {c.name} is not a number")
        if not w.is_finite() or not (0 <= w <= 100):
            raise Refused(400, "invalid_weight", "weights must be between 0 and 100")
        new[c] = w
    if criteria and sum(new.values()) == 0:
        raise Refused(400, "invalid_weight", "at least one criterion needs a weight above 0")
    changed = {}
    for c, w in new.items():
        if w != c.weight:
            changed[c.key] = [str(c.weight), str(w)]
            c.weight = w
            c.save(update_fields=["weight"])
    if changed:
        audit(event, actor, "rubric.weights_changed", event.external_id, changed=changed)
    return changed


@transaction.atomic
def add_criterion(event, actor, name):
    require_organizer(actor, event)
    name = _required_text(name, "name")
    key = slugify(name)[:64]
    if not key:
        raise Refused(400, "invalid_name", "a criterion name needs at least one letter or digit")
    if event.criteria.filter(key=key).exists():
        raise Refused(409, "criterion_exists", "a criterion with that name exists")
    if Review.objects.filter(project__event=event).exists():
        raise Refused(409, "rubric_locked", "reviews exist; a new criterion would leave them incomplete")
    criterion = Criterion.objects.create(event=event, key=key, name=name, position=event.criteria.count())
    audit(event, actor, "rubric.criterion_added", key, name=name)
    return criterion


@transaction.atomic
def update_criterion_text(event, actor, criterion, **texts):
    require_organizer(actor, event)
    for f in ("description", "anchor_low", "anchor_mid", "anchor_high"):
        setattr(criterion, f, str(texts.get(f) or "").strip()[:300])
    criterion.save()
    audit(event, actor, "rubric.anchors_changed", criterion.key)
    return criterion


@transaction.atomic
def add_question(event, actor, prompt, help_text="", required=False):
    require_organizer(actor, event)
    q = EventQuestion.objects.create(
        event=event,
        prompt=_required_text(prompt, "question", 300),
        help_text=str(help_text or "").strip()[:300],
        required=bool(required),
        position=event.questions.count(),
    )
    audit(event, actor, "question.added", q.pk, prompt=q.prompt, required=q.required)
    return q


@transaction.atomic
def delete_question(event, actor, question):
    require_organizer(actor, event)
    if question.answers.exclude(answer="").exists():
        raise Refused(409, "question_answered", "teams have answered this question; mark it optional instead")
    pk, prompt = question.pk, question.prompt
    question.delete()
    audit(event, actor, "question.deleted", pk, prompt=prompt)


@transaction.atomic
def toggle_question(event, actor, question):
    require_organizer(actor, event)
    question.required = not question.required
    question.save(update_fields=["required"])
    audit(event, actor, "question.changed", question.pk, required=question.required)
    return question


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
    if is_organizer(user, event):
        raise Refused(403, "organizers_cannot_compete", "organizers of this event cannot join a team in it")
    if team_of(user, event):
        raise Refused(409, "already_on_a_team", "you are already on a team in this event")
    try:
        with transaction.atomic():
            team = Team.objects.create(event=event, external_id=mint_id("tm"), name=name)
            TeamMembership.objects.create(team=team, user=user)
            grant_role(event, user, EventRole.Role.PARTICIPANT, actor=user)
            audit(event, user, "team.created", team.external_id, name=name)
    except IntegrityError:  # a concurrent create or join won the race for this user
        raise Refused(409, "already_on_a_team", "you are already on a team in this event")
    return team


def join_team(team, user):
    require_login(user)
    event = team.event
    ensure_submissions_open(event, user, "team.join")
    if is_judge(user, event):
        raise Refused(403, "judges_cannot_compete", "judges of this event cannot join a team in it")
    if is_organizer(user, event):
        raise Refused(403, "organizers_cannot_compete", "organizers of this event cannot join a team in it")
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
IMAGE_FORMATS = {"JPEG": ".jpg", "PNG": ".png", "GIF": ".gif", "WEBP": ".webp"}
MAX_UPLOAD_BYTES = (1 + MAX_GALLERY_IMAGES) * MAX_IMAGE_BYTES + 1024 * 1024  # a whole project form


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
            raw = data.get(key) if key in data else posted.get(str(q.pk))
            text = ("" if raw is None else str(raw)).strip()
            if len(text) > 5000:
                raise Refused(400, "answer_too_long", f"answer to \"{q.prompt}\" is at most 5,000 characters")
            out[q] = text
    return out


def check_image(upload, label):
    if upload.size > MAX_IMAGE_BYTES:
        raise Refused(400, "image_too_large", f"{label} must be at most {MAX_IMAGE_BYTES // 2**20} MB")
    try:
        with Image.open(upload) as img:
            fmt = img.format
            img.verify()
    except Exception:
        raise Refused(400, "invalid_image", f"{label} is not a readable image")
    if fmt not in IMAGE_FORMATS:
        raise Refused(400, "invalid_image", f"{label} must be JPEG, PNG, GIF or WebP")
    upload.seek(0)
    # The stored name's extension comes from what the bytes are, never from the
    # client's file name: /media/ picks Content-Type from it, and x.html would
    # be served as a page.
    upload.name = "upload" + IMAGE_FORMATS[fmt]


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

    orphans = []  # files to delete once the new state is saved
    with transaction.atomic():
        if thumbnail:
            if project.thumbnail:
                orphans.append(project.thumbnail.name)
            project.thumbnail = thumbnail
            changed.append("thumbnail")
        elif files.get("clear_thumbnail") and project.thumbnail:
            orphans.append(project.thumbnail.name)
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
            doomed = project.images.filter(pk__in=[i for i in remove if i.isdigit()])
            orphans += [img.image.name for img in doomed]
            n, _ = doomed.delete()
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
    for name in orphans:
        transaction.on_commit(lambda name=name: default_storage.delete(name))
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
        Project.objects.filter(event=event, status=Project.Status.SUBMITTED, withdrawn_at__isnull=True),
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
        if project.event_id != event.id or project.status != Project.Status.SUBMITTED or project.withdrawn_at:
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
    qs = Project.objects.filter(assignments__judge=judge_user, withdrawn_at__isnull=True).select_related(
        "event", "track", "team"
    )
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
    if project.withdrawn_at:
        raise Refused(409, "withdrawn", "an organizer withdrew this project from judging")
    if not event.accepts_reviews():
        raise Refused(403, "judging_closed", "judging has closed for this event")
    if event.results_published_at:
        raise Refused(403, "results_published", "results are published; an organizer must unpublish before scores change")

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
    reviews = Review.objects.filter(project__event=event, project__withdrawn_at__isnull=True).prefetch_related(
        "scores__criterion"
    )
    points, per_criterion = [], defaultdict(list)
    for r in reviews:
        values = {s.criterion.key: s.value for s in r.scores.all()}
        raw = scoring.weighted_score(values, weights)
        if raw is not None:
            vector = tuple(values[k] for k in sorted(values))
            points.append(scoring.ReviewPoint(judge=str(r.judge_id), project=r.project_id, raw=raw, vector=vector))
        for key, v in values.items():
            per_criterion[key].append(scoring.ReviewPoint(judge=str(r.judge_id), project=r.project_id, raw=float(v)))
    return points, per_criterion


def results_fingerprint(event):
    agg = Review.objects.filter(project__event=event).aggregate(n=Count("id"), last=Max("updated_at"))
    weights = tuple(event.criteria.values_list("key", "weight"))
    withdrawn = tuple(event.projects.filter(withdrawn_at__isnull=False).order_by("pk").values_list("pk", flat=True))
    return (f"{event.pk}:{agg['n']}:{agg['last'].isoformat() if agg['last'] else '-'}:{weights}:{prize_slots(event)}"
            f":{withdrawn}")


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
    # A link is a comparison the model already has, so it comes from reviews, not from
    # assignments that may never be scored.
    reviewed = defaultdict(set)
    for judge_id, project_id in Review.objects.filter(project__event=event).values_list("judge_id", "project_id"):
        reviewed[judge_id].add(project_id)
    # A project whose tie-break is still waiting for its score needs no second one.
    pending = {
        project_id
        for judge_id, project_id in Assignment.objects.filter(event=event, source=Assignment.Source.TIEBREAK)
        .values_list("judge_id", "project_id")
        if project_id not in reviewed[judge_id]
    }
    contested = [p for p in contested if p.project not in pending]
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
            links = sorted(x for x in reviewed[uid] if x in projects and x != project.id)
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


@transaction.atomic
def withdraw_project(event, actor, project, reason=""):
    """Take a project out of judging (a duplicate, a rules breach). Its reviews stay, unused."""
    require_organizer(actor, event)
    if project.event_id != event.id:
        raise Refused(404, "not_found")
    if project.withdrawn_at:
        return project
    project.withdrawn_at = timezone.now()
    project.withdrawn_reason = str(reason or "").strip()[:300]
    project.save(update_fields=["withdrawn_at", "withdrawn_reason"])
    audit(event, actor, "project.withdrawn", project.external_id, reason=project.withdrawn_reason)
    return project


@transaction.atomic
def restore_project(event, actor, project):
    require_organizer(actor, event)
    if project.event_id != event.id:
        raise Refused(404, "not_found")
    if not project.withdrawn_at:
        return project
    project.withdrawn_at = None
    project.withdrawn_reason = ""
    project.save(update_fields=["withdrawn_at", "withdrawn_reason"])
    audit(event, actor, "project.restored", project.external_id)
    return project


def pending_reviews(event):
    """Assignments on projects still in judging that have no review yet."""
    reviewed = set(Review.objects.filter(project__event=event).values_list("judge_id", "project_id"))
    pairs = Assignment.objects.filter(event=event, project__withdrawn_at__isnull=True).values_list("judge_id", "project_id")
    return sum(1 for pair in pairs if pair not in reviewed)


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
        "voting": voting_summary(event),
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
    points = []
    for r in reviews:
        values = {s.criterion.key: s.value for s in r.scores.all()}
        raw = scoring.weighted_score(values, weights)
        by_judge[r.judge_id].append(raw)
        per_project[r.project_id] += 1
        edges.append((r.judge_id, r.project_id))
        points.append(scoring.ReviewPoint(judge=r.judge_id, project=r.project_id, raw=raw,
                                          vector=tuple(values[k] for k in sorted(values))))

    flat_ids = scoring.flat_judges(points)  # same rule as the results model
    flat = [{"role": roles.get(j), "reviews": len(by_judge[j]), "score": by_judge[j][0]} for j in flat_ids]
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
    submitted = [p for p in projects if p.status == Project.Status.SUBMITTED and not p.withdrawn_at]
    k = event.reviews_per_project
    under = [
        {"project": p, "reviews": per_project[p.id], "pending": pending[p.id]}
        for p in submitted
        if per_project[p.id] < k
    ]
    close = event.submissions_close
    last_minute = [
        {"project": p, "before_close": close - p.submitted_at, "minutes": int((close - p.submitted_at).total_seconds() // 60)}
        for p in submitted
        if p.submitted_at and close - LAST_MINUTE <= p.submitted_at <= close
    ]
    late = [p for p in submitted if p.submitted_at and p.submitted_at > close]
    comps = scoring.components((j, p) for j, p in edges if j not in flat_ids)  # flat judges link nothing
    duplicates = possible_duplicates(event)
    return {
        "flat_judges": sorted(flat, key=lambda f: f["role"].external_id if f["role"] else ""),
        "light_judges": sorted(light, key=lambda f: f["role"].external_id if f["role"] else ""),
        "under_reviewed": sorted(under, key=lambda u: (u["reviews"], u["project"].external_id)),
        "target": k,
        "duplicates": duplicates,
        "last_minute": sorted(last_minute, key=lambda x: x["before_close"]),
        "late": late,
        "components": len(comps),
        "component_sizes": sorted(((len(js), len(ps)) for js, ps in comps), reverse=True),
        "issues": len(flat) + len(light) + len(under) + len(duplicates) + len(late)
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


# --- community voting (T3) ------------------------------------------------------------


def ip_hash(ip):
    """A salted hash of the client address. The raw address is never stored or audited."""
    return hashlib.sha256((settings.SECRET_KEY + (ip or "")).encode()).hexdigest()


def votable_projects(event):
    return Project.objects.filter(event=event, status=Project.Status.SUBMITTED, withdrawn_at__isnull=True)


def ballot_order(event, user, projects):
    """Every voter sees the projects in their own shuffled order, the same one on every reload.

    Seeding from (event, voter) spreads position bias across the field instead of always
    favouring whatever the database lists first.
    """
    seed = int.from_bytes(hashlib.sha256(f"{event.pk}:{user.pk}".encode()).digest()[:8], "big")
    ordered = sorted(projects, key=lambda p: p.external_id)  # a stable base, whatever the query returned
    random.Random(seed).shuffle(ordered)
    return ordered


def votes_left(user, event):
    return max(0, event.votes_per_voter - Vote.objects.filter(voter=user, event=event).count())


def vote_conflict(user, event):
    """Why this person may not vote in this event at all, or None."""
    if is_judge(user, event) or is_organizer(user, event):
        return "judges and organizers of this event cannot vote in it"
    return None


def _open_voting_window(event):
    phase = event.voting_phase()
    if phase == "open":
        return
    message = {
        "none": "this event has no community vote",
        "not_open": f"voting opens at {event.voting_opens.isoformat() if event.voting_opens else ''}",
        "closed": f"voting closed at {event.voting_closes.isoformat() if event.voting_closes else ''}",
    }[phase]
    raise Refused(403, "voting_closed", message)


def _vote_limits(user, ip):
    from . import ratelimit  # deferred: ratelimit imports Refused from this module

    ratelimit.hit("vote-user", user.pk, 30, 600)
    ratelimit.hit("vote-ip", ip, 120, 600)


def cast_vote(project, user, ip=""):
    """One community vote for one project. Every rule is checked here, on the server clock."""
    require_login(user)
    _vote_limits(user, ip)
    event = project.event
    _open_voting_window(event)
    if project.status != Project.Status.SUBMITTED:
        raise Refused(404, "not_found", "no such project")
    if project.withdrawn_at:
        raise Refused(409, "withdrawn", "an organizer withdrew this project")
    conflict = vote_conflict(user, event)
    if conflict:
        raise Refused(403, "conflict_of_interest", conflict)
    if is_team_member(user, project.team):
        raise Refused(403, "own_project", "you cannot vote for your own team's project")
    if Vote.objects.filter(voter=user, project=project).exists():
        raise Refused(409, "already_voted", "you already voted for this project")
    if votes_left(user, event) == 0:
        raise Refused(409, "no_votes_left", f"you have used all {event.votes_per_voter} of your votes; take one back first")
    try:
        with transaction.atomic():
            vote = Vote.objects.create(event=event, project=project, voter=user, ip_hash=ip_hash(ip))
            if Vote.objects.filter(voter=user, event=event).count() > event.votes_per_voter:  # lost a race
                raise Refused(409, "no_votes_left", "you have used all your votes")
            audit(event, user, "vote.cast", project.external_id)
    except IntegrityError:  # the unique constraint caught a concurrent double vote
        raise Refused(409, "already_voted", "you already voted for this project")
    return vote


@transaction.atomic
def retract_vote(project, user, ip=""):
    require_login(user)
    _vote_limits(user, ip)
    _open_voting_window(project.event)
    vote = Vote.objects.filter(voter=user, project=project).first()
    if vote is None:
        raise Refused(404, "no_such_vote", "you have not voted for this project")
    vote.delete()
    audit(project.event, user, "vote.retracted", project.external_id)


def ballot(event, user):
    """What one voter sees: their shuffled ballot, what they voted for and why a vote might be refused."""
    voted = set(Vote.objects.filter(voter=user, event=event).values_list("project_id", flat=True))
    mine = {m.team_id for m in TeamMembership.objects.filter(user=user, event=event)}
    projects = votable_projects(event).select_related("team", "track")
    conflict = vote_conflict(user, event)
    is_open = event.voting_phase() == "open"
    left = votes_left(user, event)
    rows = [
        {
            "project": p,
            "voted": p.id in voted,
            "own": p.team_id in mine,
            "can_vote": is_open and not conflict and p.team_id not in mine and p.id not in voted and left > 0,
        }
        for p in ballot_order(event, user, projects)
    ]
    return {"rows": rows, "left": left, "limit": event.votes_per_voter, "conflict": conflict, "phase": event.voting_phase()}


def require_votes_visible(event, user):
    """Tallies stay hidden from everyone, organizers included, until voting closes."""
    phase = event.voting_phase()
    if phase == "none":
        raise Refused(404, "no_community_vote", "this event has no community vote")
    if phase != "closed":
        require_login(user)  # anonymous callers get 401 on the API; nobody gets the numbers
        raise Refused(403, "results_hidden", f"vote counts are hidden until voting closes at {event.voting_closes.isoformat()}")


def vote_tallies(event, user):
    """People's choice: every votable project with its votes, best first. Ties share a rank."""
    require_votes_visible(event, user)
    counts = dict(Vote.objects.filter(event=event).values_list("project_id").annotate(n=Count("id")))
    rows = sorted(
        ({"project": p, "votes": counts.get(p.id, 0)} for p in votable_projects(event).select_related("team", "track")),
        key=lambda r: (-r["votes"], r["project"].title.lower(), r["project"].external_id),
    )
    rank = 0
    for i, r in enumerate(rows):
        if i == 0 or r["votes"] != rows[i - 1]["votes"]:
            rank = i + 1
        r["rank"] = rank
    return rows


SHARED_IP_VOTERS = 3


def vote_integrity_report(event):
    """Patterns that suggest ballot stuffing. Flags are for a human to look at; nothing is removed.

    - shared_ip: SHARED_IP_VOTERS or more distinct accounts behind one hashed address voted for the same project;
    - new_accounts: the voter's account was created after voting opened.
    `flags` maps a vote's id to its reasons, for the CSV.
    """
    votes = list(Vote.objects.filter(event=event).select_related("project", "voter"))
    groups = defaultdict(list)
    for v in votes:
        if v.ip_hash:
            groups[(v.project_id, v.ip_hash)].append(v)
    flags = defaultdict(list)
    shared = []
    for (_, h), vs in groups.items():
        if len({v.voter_id for v in vs}) >= SHARED_IP_VOTERS:
            shared.append({"project": vs[0].project, "accounts": len({v.voter_id for v in vs}), "ip_hash": h[:12]})
            for v in vs:
                flags[v.id].append("shared_ip")
    late = []
    if event.voting_opens:
        for v in votes:
            if v.voter.date_joined > event.voting_opens:
                late.append({"project": v.project, "voter": v.voter})
                flags[v.id].append("new_account")
    return {
        "total": len(votes),
        "voters": len({v.voter_id for v in votes}),
        "shared_ip": sorted(shared, key=lambda s: (-s["accounts"], s["project"].external_id)),
        "new_accounts": late,
        "flags": dict(flags),
        "issues": len(shared) + len(late),
    }


def voting_summary(event):
    """The dashboard's community-vote card: flags and a total, never a per-project count."""
    phase = event.voting_phase()
    if phase == "none":
        return None
    return {"phase": phase, "report": vote_integrity_report(event)}


# --- comments (T3) --------------------------------------------------------------------

MAX_COMMENT = 2000


def post_comment(project, user, body):
    require_login(user)
    from . import ratelimit  # deferred: ratelimit imports Refused from this module

    ratelimit.hit("comment-user", user.pk, 10, 600)
    if project.status != Project.Status.SUBMITTED:
        raise Refused(404, "not_found", "no such project")
    if project.withdrawn_at:
        raise Refused(409, "withdrawn", "an organizer withdrew this project; it takes no new comments")
    body = str(body or "").strip()
    if not 1 <= len(body) <= MAX_COMMENT:
        raise Refused(400, "invalid_comment", f"a comment is 1 to {MAX_COMMENT} characters")
    if Comment.objects.filter(project=project, author=user, body=body).exists():
        raise Refused(409, "duplicate_comment", "you already posted exactly that on this project")
    with transaction.atomic():
        comment = Comment.objects.create(project=project, author=user, body=body)
        audit(project.event, user, "comment.posted", project.external_id, comment=comment.pk)
    return comment


@transaction.atomic
def delete_comment(comment, user):
    """Authors delete their own comments. Organizers hide instead, so the record stays."""
    require_login(user)
    if comment.author_id != user.pk:
        raise Refused(403, "not_your_comment", "you can only delete your own comments")
    audit(comment.project.event, user, "comment.deleted", comment.project.external_id, comment=comment.pk)
    comment.delete()


@transaction.atomic
def hide_comment(comment, actor, reason):
    event = comment.project.event
    require_organizer(actor, event)
    reason = str(reason or "").strip()[:300]
    if not reason:
        raise Refused(400, "missing_reason", "say why the comment is hidden")
    if comment.hidden_at:
        return comment
    comment.hidden_at, comment.hidden_by, comment.hidden_reason = timezone.now(), actor, reason
    comment.save(update_fields=["hidden_at", "hidden_by", "hidden_reason"])
    audit(event, actor, "comment.hidden", comment.project.external_id, comment=comment.pk, reason=reason)
    return comment


@transaction.atomic
def unhide_comment(comment, actor):
    event = comment.project.event
    require_organizer(actor, event)
    if not comment.hidden_at:
        return comment
    comment.hidden_at, comment.hidden_by, comment.hidden_reason = None, None, ""
    comment.save(update_fields=["hidden_at", "hidden_by", "hidden_reason"])
    audit(event, actor, "comment.unhidden", comment.project.external_id, comment=comment.pk)
    return comment


def visible_comments(project, user):
    """The public sees non-hidden comments; organizers see every comment, hidden ones marked."""
    qs = project.comments.select_related("author", "hidden_by")
    if not is_organizer(user, project.event):
        qs = qs.filter(hidden_at__isnull=True)
    return qs
