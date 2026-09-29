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
import ipaddress
import time
import unicodedata

from django.conf import settings
from django.core.cache import cache

from .errors import Refused

LOGIN_PER_ACCOUNT_AND_IP = 10  # guesses at one account from one address
LOGIN_PER_ACCOUNT = 100  # from anywhere: high, so a stranger can't lock a named judge out
LOGIN_PER_IP = 50  # a venue's shared NAT address needs headroom
LOGIN_WINDOW = 15 * 60
SIGNUP_WINDOW = 60 * 60  # per-address signups: settings.DOGFOOD_RATE_LIMITS_SIGNUP_PER_IP
VOTE_PER_USER = 30
VOTE_PER_IP = 120
VOTE_WINDOW = 10 * 60
COMMENT_PER_USER = 10
COMMENT_WINDOW = 10 * 60


def hit(scope, ident, limit, window):
    if not settings.DOGFOOD_RATE_LIMITS or not ident:
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
    hops = settings.DOGFOOD_PROXY_COUNT
    if hops:
        chain = [p.strip() for p in request.META.get("HTTP_X_FORWARDED_FOR", "").split(",") if p.strip()]
        if len(chain) >= hops:
            return _bucket(chain[-hops])
    return _bucket(request.META.get("REMOTE_ADDR", ""))


def _bucket(ip):
    """IPv6 counts per /64 (one household or host can rotate through the rest); no address is one shared bucket."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return ip or "unknown"
    if addr.version == 6:
        if addr.ipv4_mapped:
            return str(addr.ipv4_mapped)
        return str(ipaddress.ip_network(f"{addr}/64", strict=False))
    return str(addr)


def account_key(raw):
    """The login name as Django's form will read it (NFKC, then our lower-casing), so variants share a counter."""
    return unicodedata.normalize("NFKC", str(raw or "")).strip().lower()
