"""Fixed-window rate limits kept in the shared cache.

`hit(scope, ident, limit, window)` counts one attempt and raises
Refused(429) once `ident` has made more than `limit` attempts in `scope`
within `window` seconds. Pages and the API already turn Refused into a
response, so a view only has to call it.

The count is read-modify-write, not atomic: two workers racing can each
let one extra attempt through. That is fine for slowing people down, which
is all a rate limit is for. Keys are hashed, so no email or address is
stored in the cache in the clear.
"""

import hashlib
import time

from django.conf import settings
from django.core.cache import cache

from .services import Refused

LOGIN_PER_ACCOUNT = 10  # attempts per account per window
LOGIN_PER_IP = 50  # a venue's shared NAT address needs headroom
LOGIN_WINDOW = 15 * 60
SIGNUP_PER_IP = 30
SIGNUP_WINDOW = 60 * 60


def hit(scope, ident, limit, window):
    if not getattr(settings, "DOGFOOD_RATE_LIMITS", True) or not ident:
        return
    key = "rl:" + hashlib.sha256(f"{scope}:{ident}".encode()).hexdigest()
    now = time.time()
    count, expires = cache.get(key) or (0, 0.0)
    if expires <= now:
        count, expires = 0, now + window
    count += 1
    cache.set(key, (count, expires), timeout=max(1, int(expires - now) + 1))
    if count > limit:
        wait = max(1, round((expires - now) / 60))
        raise Refused(429, "rate_limited", f"too many attempts; try again in about {wait} minute{'s' if wait != 1 else ''}")


def client_ip(request):
    """REMOTE_ADDR, or with DOGFOOD_PROXY_COUNT proxies in front, the address the outermost of ours saw.

    X-Forwarded-For is only trusted for as many hops as we run ourselves; its
    first entries are whatever the client chose to send.
    """
    hops = getattr(settings, "DOGFOOD_PROXY_COUNT", 0)
    if hops:
        chain = [p.strip() for p in request.META.get("HTTP_X_FORWARDED_FOR", "").split(",") if p.strip()]
        if len(chain) >= hops:
            return chain[-hops]
    return request.META.get("REMOTE_ADDR", "")
