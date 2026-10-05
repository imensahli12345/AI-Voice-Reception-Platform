"""Local development settings (docker compose)."""
from pathlib import Path
import environ

PROJECT_ROOT = Path(__file__).resolve().parents[3]
environ.Env.read_env(PROJECT_ROOT / ".env.local", overwrite=False)


from .base import *  # noqa: F403
from .base import env

DEBUG = env.bool("DEBUG", default=True)
ALLOWED_HOSTS = env.list("ALLOWED_HOSTS", default=["localhost", "127.0.0.1"])

# Plain HTTP locally.
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False
REFRESH_COOKIE_SECURE = env.bool("REFRESH_COOKIE_SECURE", default=False)
