import os
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent.parent
REPO_DIR = SRC_DIR.parent


def env_bool(name, default):
    return os.environ.get(name, str(default)).lower() in ("1", "true", "yes", "on")


# The default key only exists so a fresh clone boots; set DJANGO_SECRET_KEY
# for anything beyond a laptop demo.
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-only-not-secret-change-me")
DEBUG = env_bool("DJANGO_DEBUG", False)
ALLOWED_HOSTS = os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,[::1]").split(",")

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "portal",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "portal.context.portal",
            ],
        },
    },
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": os.environ.get("DJANGO_DB_PATH", str(REPO_DIR / "db.sqlite3")),
    }
}

# The acceptance checker sends "Cookie: session=<key>".
SESSION_COOKIE_NAME = "session"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = False
USE_TZ = True

# Fixture import (manage.py seed).
DOGFOOD_FIXTURES = os.environ.get("DOGFOOD_FIXTURES", str(REPO_DIR / "fixtures.json"))
# Writes the fixed checker sessions from .dogfood.toml and gives the seeded
# accounts DOGFOOD_DEMO_PASSWORD. Demo only.
DOGFOOD_SEED_SESSIONS = env_bool("DOGFOOD_SEED_SESSIONS", False)
DOGFOOD_DEMO_PASSWORD = os.environ.get("DOGFOOD_DEMO_PASSWORD", "dogfood-demo")
# Bootstrap resamples behind the results page's rank bands and P(win).
DOGFOOD_BOOTSTRAP_DRAWS = int(os.environ.get("DOGFOOD_BOOTSTRAP_DRAWS", "400"))

LOGIN_URL = "/login"
LOGIN_REDIRECT_URL = "/events"
LOGOUT_REDIRECT_URL = "/projects"
STATIC_URL = "/static/"
# Uploaded thumbnails and gallery images. Served by Django itself (small
# scale, one container); validated as images on upload, random file names.
MEDIA_URL = "/media/"
MEDIA_ROOT = os.environ.get("DJANGO_MEDIA_ROOT", str(REPO_DIR / "media"))
DATA_UPLOAD_MAX_MEMORY_SIZE = 16 * 1024 * 1024
DATA_UPLOAD_MAX_NUMBER_FILES = 10
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 10}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
]

# Results analysis is cached on disk so every web worker shares it; keys are
# fingerprints of the data, so a new score or weight is a new key.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.filebased.FileBasedCache",
        "LOCATION": os.environ.get("DJANGO_CACHE_DIR", str(REPO_DIR / ".cache")),
        "TIMEOUT": 7 * 24 * 3600,
    }
}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": "INFO"},
}
