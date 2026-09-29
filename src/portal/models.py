"""Portal schema.

Every table that can come from an import carries `external_id`, the string id
from fixtures.json (or one we mint for records created in the app). Imports
upsert on it, which is what makes `manage.py seed` safe to run on every boot.
"""

import secrets
import uuid
from pathlib import PurePosixPath

from django.conf import settings
from django.db import models
from django.db.models import F, Q
from django.utils import timezone


def mint_id(prefix):
    return f"{prefix}_{secrets.token_hex(4)}"


def mint_token():
    return secrets.token_urlsafe(18)


def _random_name(folder, filename):
    """Random file names: uploads are not enumerable and never collide."""
    ext = PurePosixPath(filename).suffix.lower()[:8]
    return f"{folder}/{uuid.uuid4().hex}{ext}"


def upload_thumbnail(instance, filename):
    return _random_name("thumbnails", filename)


def upload_gallery(instance, filename):
    return _random_name("gallery", filename)


class Event(models.Model):
    class Phase(models.TextChoices):
        NOT_OPEN = "not_open", "Not open yet"
        OPEN = "open", "Open for submissions"
        CLOSED = "closed", "Submissions closed"

    external_id = models.CharField(max_length=64, unique=True)
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    # All times are UTC. A blank open time means "open as soon as created".
    submissions_open = models.DateTimeField(null=True, blank=True)
    submissions_close = models.DateTimeField()
    judging_close = models.DateTimeField(null=True, blank=True)
    results_published_at = models.DateTimeField(null=True, blank=True)
    # Assignment target: how many judges should review each project.
    reviews_per_project = models.PositiveSmallIntegerField(default=3)
    # Community vote (T3). Both blank means the event has no public vote.
    voting_opens = models.DateTimeField(null=True, blank=True)
    voting_closes = models.DateTimeField(null=True, blank=True)
    votes_per_voter = models.PositiveSmallIntegerField(default=3)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-submissions_close"]
        constraints = [
            models.CheckConstraint(
                condition=Q(submissions_open__isnull=True) | Q(submissions_open__lt=F("submissions_close")),
                name="event_opens_before_it_closes",
            ),
            models.CheckConstraint(
                condition=Q(judging_close__isnull=True) | Q(judging_close__gte=F("submissions_close")),
                name="event_judging_closes_after_submissions",
            ),
            models.CheckConstraint(
                condition=(Q(voting_opens__isnull=True) & Q(voting_closes__isnull=True))
                | (Q(voting_opens__isnull=False) & Q(voting_closes__isnull=False) & Q(voting_opens__lt=F("voting_closes"))),
                name="event_voting_window_is_whole",
            ),
        ]

    def __str__(self):
        return self.name

    def phase(self, now=None):
        now = now or timezone.now()
        if self.submissions_open and now < self.submissions_open:
            return self.Phase.NOT_OPEN
        if now < self.submissions_close:
            return self.Phase.OPEN
        return self.Phase.CLOSED

    def accepts_submissions(self, now=None):
        return self.phase(now) == self.Phase.OPEN

    def accepts_reviews(self, now=None):
        return self.judging_close is None or (now or timezone.now()) < self.judging_close

    def voting_phase(self, now=None):
        """"none" (no community vote), "not_open", "open" or "closed"."""
        if self.voting_opens is None or self.voting_closes is None:
            return "none"
        now = now or timezone.now()
        if now < self.voting_opens:
            return "not_open"
        return "open" if now < self.voting_closes else "closed"

    @property
    def results_published(self):
        return self.results_published_at is not None


class Track(models.Model):
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="tracks")
    external_id = models.CharField(max_length=64)
    name = models.CharField(max_length=200)

    class Meta:
        ordering = ["external_id"]
        constraints = [
            models.UniqueConstraint(fields=["event", "external_id"], name="track_ext_id_per_event"),
        ]

    def __str__(self):
        return self.name


class Prize(models.Model):
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="prizes")
    # Blank track means an overall prize.
    track = models.ForeignKey(Track, on_delete=models.SET_NULL, null=True, blank=True, related_name="prizes")
    name = models.CharField(max_length=200)
    value = models.CharField(max_length=100, blank=True, help_text="Free text, e.g. 800 USD")
    position = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["position", "id"]

    def __str__(self):
        return self.name


class EventRole(models.Model):
    """A user's role in one event. Visitor is "no row"; admin is is_superuser."""

    class Role(models.TextChoices):
        PARTICIPANT = "participant"
        JUDGE = "judge"
        ORGANIZER = "organizer"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="event_roles")
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="roles")
    role = models.CharField(max_length=16, choices=Role.choices)
    # Judges keep their fixture id (jdg_01) and the tracks they cover.
    # A judge with no tracks is a generalist and can be assigned anything.
    external_id = models.CharField(max_length=64, blank=True)
    tracks = models.ManyToManyField(Track, blank=True, related_name="judges")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "event", "role"], name="one_role_row_per_user_event"),
            models.UniqueConstraint(
                fields=["event", "external_id"],
                condition=~Q(external_id=""),
                name="role_ext_id_per_event",
            ),
        ]

    def __str__(self):
        return f"{self.user} {self.role} @ {self.event}"


class Team(models.Model):
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="teams")
    external_id = models.CharField(max_length=64)
    name = models.CharField(max_length=200)
    invite_token = models.CharField(max_length=64, unique=True, default=mint_token)
    members = models.ManyToManyField(
        settings.AUTH_USER_MODEL, through="TeamMembership", through_fields=("team", "user"), related_name="teams"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["external_id"]
        constraints = [
            models.UniqueConstraint(fields=["event", "external_id"], name="team_ext_id_per_event"),
        ]

    def __str__(self):
        return self.name


class TeamMembership(models.Model):
    """`event` duplicates team.event so the database can enforce one team per person per event."""

    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name="memberships")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="team_memberships")
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="+")
    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["event", "user"], name="one_team_per_user_per_event"),
        ]

    def save(self, *args, **kwargs):
        self.event_id = self.team.event_id
        super().save(*args, **kwargs)


class Tag(models.Model):
    """Tech tags, shared across events so the gallery can filter on them."""

    name = models.SlugField(max_length=40, unique=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Project(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft"
        SUBMITTED = "submitted"

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="projects")
    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name="projects")
    track = models.ForeignKey(Track, on_delete=models.PROTECT, null=True, blank=True, related_name="projects")
    external_id = models.CharField(max_length=64)
    title = models.CharField(max_length=200)
    tagline = models.CharField(max_length=200, blank=True)
    description = models.TextField(blank=True)
    thumbnail = models.ImageField(upload_to=upload_thumbnail, blank=True)
    repo_url = models.URLField(blank=True)
    demo_video_url = models.URLField(blank=True)
    live_url = models.URLField(blank=True)
    tags = models.ManyToManyField(Tag, blank=True, related_name="projects")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT)
    submitted_at = models.DateTimeField(null=True, blank=True)
    # Set by an organizer (a duplicate, a rules breach): out of judging, results and the gallery,
    # but kept, with its reviews, for the record and the exports.
    withdrawn_at = models.DateTimeField(null=True, blank=True)
    withdrawn_reason = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["external_id"]
        constraints = [
            models.UniqueConstraint(fields=["event", "external_id"], name="project_ext_id_per_event"),
            models.CheckConstraint(
                condition=Q(status="draft") | Q(submitted_at__isnull=False),
                name="submitted_project_has_submitted_at",
            ),
        ]

    def __str__(self):
        return self.title


class ProjectImage(models.Model):
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="images")
    image = models.ImageField(upload_to=upload_gallery)
    caption = models.CharField(max_length=200, blank=True)
    position = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["position", "id"]


class EventQuestion(models.Model):
    """An organizer-defined question every submission answers."""

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="questions")
    prompt = models.CharField(max_length=300)
    help_text = models.CharField(max_length=300, blank=True)
    required = models.BooleanField(default=True)
    position = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["position", "id"]

    def __str__(self):
        return self.prompt


class ProjectAnswer(models.Model):
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="answers")
    question = models.ForeignKey(EventQuestion, on_delete=models.CASCADE, related_name="answers")
    answer = models.TextField(blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["project", "question"], name="one_answer_per_project_question"),
        ]


class Criterion(models.Model):
    """One rubric line. Weights are relative; the organizer can change them."""

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="criteria")
    key = models.SlugField(max_length=64)
    name = models.CharField(max_length=200)
    description = models.CharField(max_length=500, blank=True)
    # Anchors say what a 1, 3 and 5 look like, so judges calibrate before scoring.
    anchor_low = models.CharField(max_length=300, blank=True)
    anchor_mid = models.CharField(max_length=300, blank=True)
    anchor_high = models.CharField(max_length=300, blank=True)
    weight = models.DecimalField(max_digits=6, decimal_places=3, default=1)
    position = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["position", "key"]
        constraints = [
            models.UniqueConstraint(fields=["event", "key"], name="criterion_key_per_event"),
            models.CheckConstraint(condition=Q(weight__gte=0), name="criterion_weight_non_negative"),
        ]

    def __str__(self):
        return self.name


class JudgeInvite(models.Model):
    """A single-use link an organizer hands to a judge. We never send email."""

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="judge_invites")
    email = models.EmailField()
    token = models.CharField(max_length=64, unique=True, default=mint_token)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    accepted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    accepted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]


class Assignment(models.Model):
    class Source(models.TextChoices):
        AUTO = "auto"
        MANUAL = "manual"
        IMPORT = "import"
        TIEBREAK = "tiebreak"

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="assignments")
    judge = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="assignments")
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="assignments")
    source = models.CharField(max_length=10, choices=Source.choices, default=Source.MANUAL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["judge", "project"], name="one_assignment_per_judge_project"),
        ]


class Review(models.Model):
    """One judge's scoring of one project. Scores hang off it per criterion."""

    judge = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="reviews")
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="reviews")
    comment = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["judge", "project"], name="one_review_per_judge_project"),
        ]


class CriterionScore(models.Model):
    review = models.ForeignKey(Review, on_delete=models.CASCADE, related_name="scores")
    criterion = models.ForeignKey(Criterion, on_delete=models.PROTECT, related_name="scores")
    value = models.PositiveSmallIntegerField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["review", "criterion"], name="one_score_per_review_criterion"),
            models.CheckConstraint(condition=Q(value__gte=1, value__lte=5), name="score_on_five_point_scale"),
        ]


class Vote(models.Model):
    """One community vote. `ip_hash` is a salted hash, so the raw address is never stored."""

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="votes")
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="votes")
    voter = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="votes")
    created_at = models.DateTimeField(auto_now_add=True)
    ip_hash = models.CharField(max_length=64, blank=True)

    class Meta:
        ordering = ["created_at", "id"]
        constraints = [
            models.UniqueConstraint(fields=["voter", "project"], name="one_vote_per_voter_project"),
        ]


class Comment(models.Model):
    """A public comment on a submitted project. Organizers can hide it; the author can delete it."""

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="comments")
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="comments")
    body = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    hidden_at = models.DateTimeField(null=True, blank=True)
    hidden_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    hidden_reason = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ["created_at", "id"]


class AuditLog(models.Model):
    """Append-only record of who changed what. Organizers read it per event."""

    event = models.ForeignKey(Event, on_delete=models.CASCADE, null=True, blank=True, related_name="audit_entries")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    action = models.CharField(max_length=64)
    target = models.CharField(max_length=128, blank=True)
    detail = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]
