"""Shared pytest fixtures (loads TopDog credentials from .env for integration tests)."""
import os
from pathlib import Path

_TOPDOG_ENV_KEYS = ("TOPDOG_USER", "TOPDOG_PW", "TOPDOG_EMAIL", "TOPDOG_PASSWORD")


def _load_topdog_credentials() -> None:
    """Load only TopDog credential vars from .env — never DATABASE_URL or other settings."""
    env_path = Path(__file__).resolve().parents[1] / ".env"
    if not env_path.is_file():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key in _TOPDOG_ENV_KEYS and key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")


_load_topdog_credentials()

# Normalise alternate credential key names to TOPDOG_USER / TOPDOG_PW.
if not os.getenv("TOPDOG_USER"):
    email = os.getenv("TOPDOG_EMAIL")
    if email:
        os.environ["TOPDOG_USER"] = email
if not os.getenv("TOPDOG_PW"):
    password = os.getenv("TOPDOG_PASSWORD")
    if password:
        os.environ["TOPDOG_PW"] = password
