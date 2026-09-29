import json
from pathlib import Path

import pytest
from django.core.management import call_command
from django.test import Client

FIXTURES_PATH = Path(__file__).resolve().parent.parent / "fixtures.json"

# Mirrors .dogfood.toml [auth]; the seed command must create these.
ORGANIZER = "org_7f2a"
JUDGE_A = "jdg_a_91bc"  # fixture judge jdg_01
JUDGE_B = "jdg_b_44de"  # fixture judge jdg_02
PARTICIPANT = "prt_2e88"  # priya1@example.org, team tm_01


@pytest.fixture
def fixture_data():
    return json.loads(FIXTURES_PATH.read_text())


@pytest.fixture
def seeded(db):
    call_command("seed", fixtures=str(FIXTURES_PATH), sessions=True, warm=False, verbosity=0)


def client_as(session_key=None):
    """A test client that sends the checker's cookie, or nothing."""
    client = Client()
    if session_key:
        client.cookies["session"] = session_key
    return client


@pytest.fixture(autouse=True)
def _isolated_cache(settings):
    settings.CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "tests"}}
    settings.DOGFOOD_BOOTSTRAP_DRAWS = 60
    from django.core.cache import cache

    cache.clear()
