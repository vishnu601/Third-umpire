"""Helpers for /api/ views.

API routes answer with JSON and an explicit status. They never redirect:
401 means "log in", 403 means "you are logged in and this is not yours".
"""

import json
from functools import wraps

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

from .services import Refused as ApiError  # one refusal type for pages and API
from .services import require_login as _require_login


def error_response(status, code, message=""):
    return JsonResponse({"error": code, "message": message or code.replace("_", " ")}, status=status)


def api_view(methods):
    """JSON endpoint: method check, JSON-only bodies, ApiError -> response.

    CSRF tokens are skipped because unsafe requests must carry
    Content-Type: application/json, which a cross-site form cannot send
    without a CORS preflight (and we answer no preflights). Session cookies
    are also SameSite=Lax.
    """

    def decorator(view):
        @csrf_exempt
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            if request.method not in methods:
                resp = error_response(405, "method_not_allowed")
                resp["Allow"] = ", ".join(methods)
                return resp
            if request.method not in ("GET", "HEAD", "OPTIONS") and request.content_type != "application/json":
                return error_response(415, "json_required", "send Content-Type: application/json")
            try:
                return view(request, *args, **kwargs)
            except ApiError as e:
                return error_response(e.status, e.code, e.message)

        return wrapper

    return decorator


def require_login(request):
    _require_login(request.user)


def json_body(request):
    try:
        body = json.loads(request.body or b"{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ApiError(400, "invalid_json")
    if not isinstance(body, dict):
        raise ApiError(400, "invalid_json", "body must be a JSON object")
    return body
