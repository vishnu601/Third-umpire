"""pytest settings: the real settings with a throwaway secret key, so tests run with demo mode and DEBUG off."""

import os

os.environ.setdefault("DJANGO_SECRET_KEY", "test-only-not-secret")

from .settings import *  # noqa: E402,F401,F403
