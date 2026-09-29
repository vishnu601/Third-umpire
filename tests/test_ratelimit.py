"""Rate limits on the doors people can hammer: login and signup (and anything else that calls ratelimit.hit)."""

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from portal import ratelimit
from portal.services import Refused

User = get_user_model()


def test_hit_refuses_past_the_limit_and_scopes_are_separate():
    for _ in range(3):
        ratelimit.hit("t", "a", limit=3, window=60)
    with pytest.raises(Refused) as e:
        ratelimit.hit("t", "a", limit=3, window=60)
    assert e.value.status == 429 and e.value.code == "rate_limited"
    ratelimit.hit("t", "b", limit=3, window=60)  # another identity
    ratelimit.hit("u", "a", limit=3, window=60)  # another scope


def test_the_window_expires(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(ratelimit.time, "time", lambda: now[0])
    ratelimit.hit("t", "a", limit=1, window=60)
    with pytest.raises(Refused):
        ratelimit.hit("t", "a", limit=1, window=60)
    now[0] += 61
    ratelimit.hit("t", "a", limit=1, window=60)


def test_limits_can_be_switched_off(settings):
    settings.DOGFOOD_RATE_LIMITS = False
    for _ in range(5):
        ratelimit.hit("t", "a", limit=1, window=60)


def test_password_guessing_on_one_account_is_throttled(db):
    User.objects.create_user("victim@example.org", "victim@example.org", "a-long-test-password")
    c = Client()
    statuses = [c.post("/login", {"username": "victim@example.org", "password": f"guess-{i}"}).status_code
                for i in range(ratelimit.LOGIN_PER_ACCOUNT_AND_IP + 1)]
    assert statuses[:-1] == [200] * ratelimit.LOGIN_PER_ACCOUNT_AND_IP  # form redisplayed with an error
    assert statuses[-1] == 429
    # the right password is refused too while the lock holds, so guessing can't be finished off
    assert c.post("/login", {"username": "victim@example.org", "password": "a-long-test-password"}).status_code == 429


def test_signups_from_one_address_are_throttled(db, settings):
    settings.DOGFOOD_RATE_LIMITS_SIGNUP_PER_IP = 2
    c = Client()
    for i in range(2):
        resp = c.post("/signup", {"name": "N", "email": f"s{i}@example.org", "password": "a-long-test-password"})
        assert resp.status_code == 302
        c.post("/logout")
    resp = c.post("/signup", {"name": "N", "email": "s9@example.org", "password": "a-long-test-password"})
    assert resp.status_code == 429
    assert not User.objects.filter(username="s9@example.org").exists()


def test_client_ip_trusts_forwarded_for_only_when_told(rf, settings):
    req = rf.get("/", HTTP_X_FORWARDED_FOR="6.6.6.6, 1.2.3.4", REMOTE_ADDR="10.0.0.1")
    settings.DOGFOOD_PROXY_COUNT = 0
    assert ratelimit.client_ip(req) == "10.0.0.1"
    settings.DOGFOOD_PROXY_COUNT = 1
    assert ratelimit.client_ip(req) == "1.2.3.4"  # the address our own proxy saw, not a spoofable first entry
