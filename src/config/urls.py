from django.contrib.auth import views as auth_views
from django.urls import path, re_path
from django.views.generic import RedirectView

from portal import pages, views

urlpatterns = [
    path("", RedirectView.as_view(url="/projects", permanent=False)),
    path("healthz", pages.health),
    re_path(r"^media/(?P<path>.+)$", pages.media, name="media"),
    path("demo/login", pages.demo_login, name="demo-login"),
    # accounts
    path("login", pages.login_view, name="login"),
    path("logout", auth_views.LogoutView.as_view(), name="logout"),
    path("signup", pages.signup, name="signup"),
    # public
    path("projects", views.gallery, name="gallery"),
    path("projects/<str:event_id>/<str:project_id>", pages.project_detail, name="project"),
    path("events", pages.event_list, name="events"),
    path("events/new", pages.event_new, name="event-new"),
    path("events/<str:event_id>", pages.event_detail, name="event"),
    path("events/<str:event_id>/results", pages.results, name="results"),
    # participants
    path("events/<str:event_id>/teams", pages.team_create, name="team-create"),
    path("teams/<str:team_id>", pages.team_detail, name="team"),
    path("teams/<str:team_id>/project", pages.project_edit, name="project-new"),
    path("teams/<str:team_id>/project/<str:project_id>", pages.project_edit, name="project-edit"),
    path("join/<str:token>", pages.join, name="join"),
    # organizers
    path("events/<str:event_id>/manage", pages.event_manage, name="manage"),
    path("events/<str:event_id>/judges", pages.judges, name="judges"),
    path("events/<str:event_id>/judges/<str:judge_id>", pages.judge_detail, name="judge-detail"),
    path("events/<str:event_id>/dashboard", pages.dashboard, name="dashboard"),
    path("events/<str:event_id>/dashboard/live", pages.dashboard_live, name="dashboard-live"),
    path("events/<str:event_id>/audit", pages.audit_log, name="audit"),
    path("events/<str:event_id>/projects/<str:project_id>/reviews", pages.project_reviews, name="project-reviews"),
    # judges
    path("invites/<str:token>", pages.invite_accept, name="invite"),
    path("judge", pages.judge_home, name="judge-home"),
    path("judge/<str:event_id>/<str:project_id>", pages.score, name="score"),
    # api: JSON or CSV, explicit 401/403, never a redirect
    path("api/projects", views.submit_project, name="api-submit-project"),
    path("api/judge/scores", views.judge_scores, name="api-judge-scores"),
    path("api/export.csv", views.export_csv, name="api-export-csv"),
    path("api/events/<str:event_id>/results.csv", views.results_csv, name="api-results-csv"),
    path("api/events/<str:event_id>/progress", views.progress_json, name="api-progress"),
    path("api/events/<str:event_id>/teams.csv", views.teams_csv, name="api-teams-csv"),
    path("api/events/<str:event_id>/submissions.csv", views.submissions_csv, name="api-submissions-csv"),
    path("api/events/<str:event_id>/assignments.csv", views.assignments_csv, name="api-assignments-csv"),
    path("api/events/<str:event_id>/audit.csv", views.audit_csv, name="api-audit-csv"),
]
