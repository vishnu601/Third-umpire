"""Server-rendered pages. Every rule lives in services.py; these only translate
HTTP forms into service calls and `Refused` into responses.

Pages (unlike /api/) may redirect: 401 goes to the login page. 403, 404 and
410 render an error page with that status. 400 and 409 on a form post flash
the message and send you back to the form.
"""

from datetime import datetime, timezone as dt_timezone
from functools import wraps

from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, login
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth.views import LoginView, redirect_to_login
from django.core.exceptions import ValidationError
from django.db.models import Count, Q
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST
from django.views.static import serve

from . import ratelimit, services
from .models import (
    Assignment,
    AuditLog,
    Criterion,
    Event,
    EventQuestion,
    EventRole,
    JudgeInvite,
    Prize,
    Project,
    Review,
    Team,
    Track,
)
from .forms import EmailAuthenticationForm
from .seed import demo_home, demo_user
from .services import Refused

User = get_user_model()


def page(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        try:
            return view(request, *args, **kwargs)
        except Refused as e:
            if e.status == 401:
                return redirect_to_login(request.get_full_path())
            if request.method == "POST" and e.status in (400, 409):
                messages.error(request, e.message)
                return redirect(request.get_full_path(), permanent=False)
            return render(request, "portal/error.html", {"error": e}, status=e.status)

    return wrapper


def parse_dt(value, label):
    """<input type=datetime-local> values, read as UTC."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        raise Refused(400, "invalid_date", f"{label} is not a valid date and time")
    return dt if dt.tzinfo else dt.replace(tzinfo=dt_timezone.utc)


def _missing(field):
    raise Refused(400, f"missing_{field}", f"{field} is required")


def event_fields(post):
    try:
        k = int(post.get("reviews_per_project") or 3)
    except ValueError:
        raise Refused(400, "invalid_reviews_per_project", "reviews per project must be a number")
    return {
        "reviews_per_project": k,
        "name": (post.get("name") or "").strip()[:200] or _missing("name"),
        "description": (post.get("description") or "").strip(),
        "submissions_open": parse_dt(post.get("submissions_open"), "Submissions open"),
        "submissions_close": parse_dt(post.get("submissions_close"), "Submissions close"),
        "judging_close": parse_dt(post.get("judging_close"), "Judging close"),
    }


# --- accounts ------------------------------------------------------------------


class SignupForm(forms.Form):
    name = forms.CharField(max_length=150)
    email = forms.EmailField()
    password = forms.CharField(widget=forms.PasswordInput)

    def clean_email(self):
        email = self.cleaned_data["email"].lower()
        if User.objects.filter(username=email).exists():
            raise ValidationError("An account with this email already exists.")
        return email

    def clean(self):
        data = super().clean()
        if "password" in data:
            try:
                validate_password(data["password"], User(username=data.get("email", ""), email=data.get("email", "")))
            except ValidationError as e:
                self.add_error("password", e)
        return data


_login = LoginView.as_view(template_name="portal/login.html", authentication_form=EmailAuthenticationForm)


@page
def login_view(request):
    """Django's login, behind a per-account and a per-address limit on attempts."""
    if request.method == "POST":
        ratelimit.hit("login-ip", ratelimit.client_ip(request), ratelimit.LOGIN_PER_IP, ratelimit.LOGIN_WINDOW)
        account = (request.POST.get("username") or "").strip().lower()
        ratelimit.hit("login-account", account, ratelimit.LOGIN_PER_ACCOUNT, ratelimit.LOGIN_WINDOW)
    return _login(request)


@page
def signup(request):
    form = SignupForm(request.POST or None)
    if request.method == "POST":
        ratelimit.hit("signup-ip", ratelimit.client_ip(request), settings.DOGFOOD_RATE_LIMITS_SIGNUP_PER_IP,
                      ratelimit.SIGNUP_WINDOW)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        user = User.objects.create_user(
            username=d["email"], email=d["email"], password=d["password"], first_name=d["name"]
        )
        services.audit(None, user, "account.created", user.username)
        login(request, user, backend="django.contrib.auth.backends.ModelBackend")
        return redirect(safe_next(request) or "/events")
    return render(request, "portal/signup.html", {"form": form})


def safe_next(request):
    """Only same-site relative paths, so ?next= cannot bounce people off-site."""
    nxt = request.GET.get("next", "")
    if "\\" in nxt:  # browsers read a backslash as a slash, Django's check does not
        return ""
    allowed = url_has_allowed_host_and_scheme(nxt, {request.get_host()}, require_https=request.is_secure())
    return nxt if allowed and nxt.startswith("/") else ""


# --- events ----------------------------------------------------------------------


def event_list(request):
    events = Event.objects.annotate(n_projects=Count("projects", filter=Q(projects__status="submitted")))
    return render(
        request, "portal/events.html", {"events": events, "can_create": services.can_create_events(request.user)}
    )


@page
def event_new(request):
    services.require_login(request.user)
    if not services.can_create_events(request.user):
        raise Refused(403, "cannot_create_events", "ask an admin to let you host events")
    if request.method == "POST":
        event = services.create_event(request.user, **event_fields(request.POST))
        messages.success(request, "Event created. Add tracks, prizes and your rubric next.")
        return redirect(f"/events/{event.external_id}/manage")
    return render(request, "portal/event_new.html")


@page
def event_detail(request, event_id):
    event = get_object_or_404(Event, external_id=event_id)
    user = request.user
    return render(
        request,
        "portal/event.html",
        {
            "event": event,
            "phase": event.phase(),
            "tracks": event.tracks.all(),
            "prizes": event.prizes.select_related("track"),
            "criteria": event.criteria.all(),
            "my_team": services.team_of(user, event),
            "is_organizer": services.is_organizer(user, event),
            "is_judge": services.is_judge(user, event),
        },
    )


@page
def event_manage(request, event_id):
    event = get_object_or_404(Event, external_id=event_id)
    services.require_organizer(request.user, event)
    if request.method == "POST":
        _manage_action(request, event)
        return redirect(request.path)
    return render(
        request,
        "portal/manage.html",
        {
            "event": event,
            "tracks": event.tracks.annotate(n=Count("projects")),
            "prizes": event.prizes.select_related("track"),
            "criteria": event.criteria.all(),
            "questions": event.questions.all(),
            "has_reviews": Review.objects.filter(project__event=event).exists(),
            "pending_reviews": services.pending_reviews(event),
            "organizers": EventRole.objects.filter(event=event, role=EventRole.Role.ORGANIZER)
            .select_related("user")
            .order_by("user__username"),
        },
    )


def _manage_action(request, event):
    post, user = request.POST, request.user
    action = post.get("action")
    if action == "details":
        services.update_event(event, user, **event_fields(post))
        messages.success(request, "Event details saved.")
    elif action == "add_track":
        services.add_track(event, user, post.get("name"))
    elif action == "delete_track":
        services.delete_track(event, user, get_object_or_404(Track, event=event, external_id=post.get("track")))
    elif action == "add_prize":
        services.add_prize(event, user, post.get("name"), post.get("value"), post.get("track"))
    elif action == "delete_prize":
        services.delete_prize(event, user, get_object_or_404(Prize, event=event, pk=post.get("prize")))
    elif action == "weights":
        services.set_weights(event, user, {c.pk: post.get(f"weight_{c.pk}") for c in event.criteria.all()})
        messages.success(request, "Rubric weights saved.")
    elif action == "add_criterion":
        services.add_criterion(event, user, post.get("name"))
    elif action == "criterion_text":
        c = get_object_or_404(Criterion, event=event, pk=post.get("criterion"))
        services.update_criterion_text(event, user, c, **{f: post.get(f) for f in ("description", "anchor_low", "anchor_mid", "anchor_high")})
        messages.success(request, f"Saved the description and anchors for {c.name}.")
    elif action == "add_question":
        services.add_question(event, user, post.get("prompt"), post.get("help_text"), post.get("required") == "1")
    elif action == "delete_question":
        services.delete_question(event, user, get_object_or_404(EventQuestion, event=event, pk=post.get("question")))
    elif action == "toggle_question":
        services.toggle_question(event, user, get_object_or_404(EventQuestion, event=event, pk=post.get("question")))
    elif action == "add_organizer":
        role = services.add_organizer(event, user, post.get("email"))
        messages.success(request, f"{role.user.username} is now an organizer of this event.")
    elif action == "publish":
        services.set_results_published(event, user, True)
        messages.success(request, "Results are public.")
    elif action == "unpublish":
        services.set_results_published(event, user, False)
        messages.success(request, "Results are hidden again.")
    else:
        raise Refused(400, "unknown_action")


# --- teams and projects --------------------------------------------------------------


@page
@require_POST
def team_create(request, event_id):
    event = get_object_or_404(Event, external_id=event_id)
    team = services.create_team(event, request.user, request.POST.get("name"))
    return redirect(f"/teams/{team.external_id}")


def _team_or_404(team_id):
    return get_object_or_404(Team.objects.select_related("event"), external_id=team_id)


@page
def team_detail(request, team_id):
    team = _team_or_404(team_id)
    services.require_login(request.user)
    member = services.is_team_member(request.user, team)
    if not (member or services.is_organizer(request.user, team.event)):
        raise Refused(403, "not_on_team", "only team members and organizers can see this page")
    if request.method == "POST":
        if request.POST.get("action") != "rotate_invite":
            raise Refused(400, "unknown_action")
        services.rotate_invite(team, request.user)
        messages.success(request, "New invite link made; the old one no longer works.")
        return redirect(request.path)
    return render(
        request,
        "portal/team.html",
        {
            "team": team,
            "event": team.event,
            "member": member,
            "members": team.members.all(),
            "projects": team.projects.select_related("track"),
            "invite_url": request.build_absolute_uri(f"/join/{team.invite_token}"),
            "open": team.event.accepts_submissions(),
        },
    )


@page
def join(request, token):
    team = get_object_or_404(Team.objects.select_related("event"), invite_token=token)
    services.require_login(request.user)
    if request.method == "POST":
        services.join_team(team, request.user)
        messages.success(request, f"You joined {team.name}.")
        return redirect(f"/teams/{team.external_id}")
    return render(request, "portal/join.html", {"team": team, "event": team.event})


@page
def project_edit(request, team_id, project_id=None):
    team = _team_or_404(team_id)
    services.require_login(request.user)
    if not services.is_team_member(request.user, team):
        raise Refused(403, "not_on_team", "only team members can edit the team's project")
    project = get_object_or_404(Project, team=team, external_id=project_id) if project_id else None
    if request.method == "POST":
        if int(request.META.get("CONTENT_LENGTH") or 0) > services.MAX_UPLOAD_BYTES:
            raise Refused(400, "upload_too_large", "that form is larger than the upload limit; use smaller images")
        submit = request.POST.get("action") == "submit"
        files = {
            "thumbnail": request.FILES.get("thumbnail"),
            "gallery": request.FILES.getlist("gallery"),
            "remove_images": request.POST.getlist("remove_image"),
            "clear_thumbnail": request.POST.get("clear_thumbnail") == "1",
        }
        if project is None:
            project = services.create_project(team, request.user, request.POST, files=files, submit=submit)
        else:
            services.update_project(project, request.user, request.POST, submit=submit, files=files)
        messages.success(request, "Submitted. You can keep editing until the deadline." if submit else "Draft saved.")
        return redirect(f"/teams/{team.external_id}")
    return render(
        request,
        "portal/project_form.html",
        {
            "team": team,
            "event": team.event,
            "project": project,
            "tracks": team.event.tracks.all(),
            "questions": _questions_with_answers(team.event, project),
            "tags": ", ".join(project.tags.values_list("name", flat=True)) if project else "",
            "open": team.event.accepts_submissions(),
        },
    )


def _questions_with_answers(event, project):
    saved = {a.question_id: a.answer for a in project.answers.all()} if project else {}
    return [(q, saved.get(q.pk, "")) for q in event.questions.all()]


@page
def project_detail(request, event_id, project_id):
    project = get_object_or_404(
        Project.objects.select_related("event", "team", "track"), event__external_id=event_id, external_id=project_id
    )
    if not services.can_view_project(request.user, project):
        raise Refused(404, "not_found", "no such project")
    return render(
        request,
        "portal/project.html",
        {
            "project": project,
            "members": project.team.members.all(),
            "answers": project.answers.select_related("question").order_by("question__position"),
            "can_edit": services.is_team_member(request.user, project.team) and project.event.accepts_submissions(),
        },
    )


# --- judging ------------------------------------------------------------------------------


@page
def judges(request, event_id):
    event = get_object_or_404(Event, external_id=event_id)
    services.require_organizer(request.user, event)
    if request.method == "POST":
        _judges_action(request, event)
        return redirect(request.path)
    roles = (
        EventRole.objects.filter(event=event, role=EventRole.Role.JUDGE)
        .select_related("user")
        .prefetch_related("tracks")
        .order_by("external_id")
    )
    load = dict(Assignment.objects.filter(event=event).values_list("judge_id").annotate(n=Count("id")))
    done = dict(Review.objects.filter(project__event=event).values_list("judge_id").annotate(n=Count("id")))
    projects = (
        Project.objects.filter(event=event, status=Project.Status.SUBMITTED, withdrawn_at__isnull=True)
        .select_related("track")
        .annotate(n_assigned=Count("assignments", distinct=True), n_reviews=Count("reviews", distinct=True))
        .order_by("n_reviews", "external_id")
    )
    return render(
        request,
        "portal/judges.html",
        {
            "event": event,
            "judges": [(r, load.get(r.user_id, 0), done.get(r.user_id, 0)) for r in roles],
            "projects": projects,
            "invites": event.judge_invites.select_related("accepted_by"),
            "base": request.build_absolute_uri("/invites/"),
        },
    )


def _judges_action(request, event):
    post, user = request.POST, request.user
    action = post.get("action")
    if action == "invite":
        services.invite_judge(event, user, post.get("email"))
        messages.success(request, "Invite created. Copy its link to the judge; the portal sends no email.")
    elif action == "assign":
        try:
            k = int(post.get("per_project") or event.reviews_per_project)
        except ValueError:
            raise Refused(400, "invalid_per_project", "judges per project must be a number")
        n = services.auto_assign(event, user, k)
        messages.success(request, f"{n} new assignment{'s' if n != 1 else ''}. Every submitted project now has up to {k} judges.")
    elif action == "batch":
        role = get_object_or_404(EventRole, event=event, role=EventRole.Role.JUDGE, external_id=post.get("judge"))
        projects = list(Project.objects.filter(event=event, external_id__in=post.getlist("projects")))
        if not projects:
            raise Refused(400, "no_projects", "pick at least one project")
        made, skipped = services.assign_batch(event, user, role.user, projects)
        messages.success(request, f"Assigned {len(made)} project(s) to {role.external_id}" + (f"; {len(skipped)} already assigned." if skipped else "."))
    elif action == "unassign":
        a = get_object_or_404(Assignment.objects.select_related("project", "judge"), event=event, pk=post.get("assignment"))
        services.unassign(event, user, a)
        messages.success(request, "Assignment removed.")
    else:
        raise Refused(400, "unknown_action")


@page
def judge_detail(request, event_id, judge_id):
    """Organizer view of one judge's queue (for unassigning)."""
    event = get_object_or_404(Event, external_id=event_id)
    services.require_organizer(request.user, event)
    role = get_object_or_404(EventRole.objects.select_related("user"), event=event, role=EventRole.Role.JUDGE, external_id=judge_id)
    reviewed = set(Review.objects.filter(judge=role.user, project__event=event).values_list("project_id", flat=True))
    rows = [
        (a, a.project_id in reviewed)
        for a in Assignment.objects.filter(event=event, judge=role.user).select_related("project__track").order_by("project__external_id")
    ]
    return render(request, "portal/judge_detail.html", {"event": event, "role": role, "rows": rows})


@page
def invite_accept(request, token):
    invite = get_object_or_404(JudgeInvite.objects.select_related("event"), token=token)
    services.require_login(request.user)
    if request.method == "POST":
        services.accept_judge_invite(invite, request.user)
        messages.success(request, f"You are now a judge for {invite.event.name}.")
        return redirect("/judge")
    return render(request, "portal/invite.html", {"invite": invite, "event": invite.event})


@page
def judge_home(request):
    services.require_login(request.user)
    roles = EventRole.objects.filter(user=request.user, role=EventRole.Role.JUDGE).select_related("event")
    if not roles:
        raise Refused(403, "not_a_judge", "you are not a judge for any event")
    reviewed = set(Review.objects.filter(judge=request.user).values_list("project_id", flat=True))
    events = []
    for role in roles:
        projects = services.assigned_projects(request.user, role.event).order_by("external_id")
        events.append({"event": role.event, "rows": [(p, p.id in reviewed) for p in projects]})
    return render(request, "portal/judge_home.html", {"events": events})


@page
def score(request, event_id, project_id):
    project = get_object_or_404(
        Project.objects.select_related("event", "team", "track"), event__external_id=event_id, external_id=project_id
    )
    event = project.event
    services.require_judge(request.user, event)
    if not Assignment.objects.filter(judge=request.user, project=project).exists():
        raise Refused(403, "not_assigned", "this project is not assigned to you")
    if request.method == "POST":
        services.save_review(project, request.user, request.POST, (request.POST.get("comment") or "").strip())
        messages.success(request, f"Saved your review of {project.title}.")
        return redirect("/judge")
    # Only the caller's own review is ever loaded here.
    review = Review.objects.filter(judge=request.user, project=project).prefetch_related("scores__criterion").first()
    mine = {s.criterion.key: s.value for s in review.scores.all()} if review else {}
    return render(
        request,
        "portal/score.html",
        {
            "project": project,
            "event": event,
            "criteria": [(c, mine.get(c.key)) for c in event.criteria.all()],
            "review": review,
            "scale": range(1, 6),
            "open": event.accepts_reviews(),
        },
    )


# --- organizer views ----------------------------------------------------------------------


@page
def dashboard(request, event_id):
    event = get_object_or_404(Event, external_id=event_id)
    services.require_organizer(request.user, event)
    if request.method == "POST":
        action = request.POST.get("action")
        project = get_object_or_404(Project, event=event, external_id=request.POST.get("project"))
        if action == "withdraw":
            services.withdraw_project(event, request.user, project, request.POST.get("reason"))
            messages.success(request, f"{project.external_id} is withdrawn from judging, results and the gallery.")
        elif action == "restore":
            services.restore_project(event, request.user, project)
            messages.success(request, f"{project.external_id} is back in judging.")
        else:
            raise Refused(400, "unknown_action")
        return redirect(request.path)
    return render(request, "portal/dashboard.html", {"event": event, "p": services.progress(event)})


@page
def project_reviews(request, event_id, project_id):
    """Organizer only: every judge's scores and comment on one project."""
    event = get_object_or_404(Event, external_id=event_id)
    services.require_organizer(request.user, event)
    project = get_object_or_404(Project.objects.select_related("team", "track"), event=event, external_id=project_id)
    criteria = list(event.criteria.all())
    roles = dict(EventRole.objects.filter(event=event, role=EventRole.Role.JUDGE).values_list("user_id", "external_id"))
    rows = []
    for r in project.reviews.select_related("judge").prefetch_related("scores__criterion").order_by("created_at"):
        values = {s.criterion.key: s.value for s in r.scores.all()}
        rows.append({"review": r, "judge": roles.get(r.judge_id) or r.judge.username,
                     "values": [values.get(c.key) for c in criteria]})
    return render(request, "portal/project_reviews.html",
                  {"event": event, "project": project, "criteria": criteria, "rows": rows})


@page
def dashboard_live(request, event_id):
    """HTML fragment the dashboard polls every few seconds."""
    event = get_object_or_404(Event, external_id=event_id)
    services.require_organizer(request.user, event)
    return render(request, "portal/_progress.html", {"event": event, "p": services.progress(event)})


def result_rows(event):
    analysis = services.event_results(event)
    projects = {p.id: p for p in Project.objects.filter(event=event).select_related("team", "track")}
    roles = {
        str(r.user_id): r
        for r in EventRole.objects.filter(event=event, role=EventRole.Role.JUDGE).select_related("user")
    }
    ranking = [(projects[r.project], r) for r in analysis.ranking()]
    judges = sorted(
        ((roles.get(j), e) for j, e in analysis.judges.items()),
        key=lambda x: (x[1].leniency is None, -(x[1].leniency or 0)),
    )
    leaders = [dict(x, project=projects.get(x["project_id"])) for x in services.criterion_leaders(event)]
    return analysis, ranking, judges, leaders


def results_markdown(event, analysis, ranking, leaders):
    """Paste-ready results post, in the format Raptors publishes."""
    lines = [f"## {event.name}: results", ""]
    lines.append("| Place | Project | Team | Score (1-5) | 80% rank band | Chance of placing |")
    lines.append("|---|---|---|---|---|---|")
    for project, s in ranking[: max(10, analysis.prize_slots)]:
        lines.append(
            f"| {s.rank} | {project.title} | {project.team.name} | {s.score:.2f} | {s.rank_lo}-{s.rank_hi} | {s.p_top:.0%} |"
        )
    if leaders:
        lines += ["", "**Category awards**", ""]
        lines += [f"- Best {x['criterion'].name}: {x['project'].title} ({x['score']:.2f})" for x in leaders if x["project"]]
    lines += [
        "",
        f"Scores are bias-corrected with a two-way model (project quality + judge leniency) over "
        f"{sum(s.reviews for _, s in ranking)} reviews. Rank bands and chances come from "
        f"{analysis.draws} bootstrap resamples; places whose chance is between 10% and 90% are too close to call.",
    ]
    return "\n".join(lines)


@page
def results(request, event_id):
    event = get_object_or_404(Event, external_id=event_id)
    organizer = services.is_organizer(request.user, event)
    if request.method == "POST":
        services.require_organizer(request.user, event)
        if request.POST.get("action") != "tiebreak":
            raise Refused(400, "unknown_action")
        n = services.apply_tiebreaks(event, request.user)
        messages.success(request, f"{n} tie-break assignment{'s' if n != 1 else ''} made. They show up in each judge's queue.")
        return redirect(request.path)
    if not organizer and not event.results_published:
        raise Refused(403, "results_not_published", "results are not published yet")
    analysis, ranking, judges, leaders = result_rows(event)
    ctx = {"event": event, "analysis": analysis, "ranking": ranking, "leaders": leaders, "organizer": organizer}
    if organizer:
        ctx["judges"] = judges
        ctx["suggestions"] = services.tiebreak_suggestions(event, analysis)
        ctx["markdown"] = results_markdown(event, analysis, ranking, leaders)
    return render(request, "portal/results.html", ctx)


AUDIT_GROUPS = ["deadline", "project", "team", "review", "assignments", "judge", "rubric", "event", "results", "role"]


@page
def audit_log(request, event_id):
    event = get_object_or_404(Event, external_id=event_id)
    services.require_organizer(request.user, event)
    entries = AuditLog.objects.filter(event=event).select_related("actor")
    action = request.GET.get("action", "").strip()
    if action:
        entries = entries.filter(action__startswith=action)
    entries = list(entries[:500])
    for e in entries:
        e.detail_text = ", ".join(f"{k}={v}" for k, v in sorted(e.detail.items())) if e.detail else ""
    return render(
        request,
        "portal/audit.html",
        {"event": event, "entries": entries, "action": action, "action_choices": AUDIT_GROUPS},
    )


@require_POST
def demo_login(request):
    """Demo mode only: one click to act as a seeded role. 404 when demo mode is off."""
    if not settings.DOGFOOD_SEED_SESSIONS:
        raise Http404
    user = demo_user(request.POST.get("role", ""))
    if user is None:
        raise Http404
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    messages.info(request, f"Demo mode: you are now {user.username}.")
    return redirect(demo_home(request.POST.get("role")))


def health(request):
    return HttpResponse("ok", content_type="text/plain")


def media(request, path):
    """Uploaded images, served with headers that stop the browser treating one as a page.

    An upload is validated and renamed to the format Pillow read, but media is
    same-origin, so a file that slipped through must still be inert: the sandbox
    and `default-src 'none'` stop scripts, and nosniff stops content sniffing.
    """
    response = serve(request, path, document_root=settings.MEDIA_ROOT)  # read late: tests override it
    response["Content-Security-Policy"] = "default-src 'none'; img-src 'self'; sandbox"
    response["X-Content-Type-Options"] = "nosniff"
    return response
