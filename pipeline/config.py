"""Pipeline configuration: paths and API keys (read from env vars / .env)."""
from __future__ import annotations

import os
from pathlib import Path

PIPELINE_DIR = Path(__file__).resolve().parent
REPO_ROOT = PIPELINE_DIR.parent
CACHE_DIR = PIPELINE_DIR / "cache"
WEB_DATA_DIR = REPO_ROOT / "web" / "data"
PRICES_DIR = WEB_DATA_DIR / "prices"

try:  # .env is optional; real env vars (e.g. GitHub secrets) take precedence
    from dotenv import load_dotenv

    load_dotenv(REPO_ROOT / ".env", override=False)
except ImportError:
    pass


def tiingo_api_key(required: bool = True) -> str | None:
    """Return TIINGO_API_KEY, raising if it is required and missing."""
    key = os.environ.get("TIINGO_API_KEY", "").strip() or None
    if required and not key:
        raise RuntimeError(
            "TIINGO_API_KEY is not set. Copy .env.example to .env and add your key, "
            "or set it as an environment variable."
        )
    return key


def capitol_api_url() -> str:
    """Base URL of the self-hosted Capitol API (env CAPITOL_API_URL)."""
    return os.environ.get("CAPITOL_API_URL", "").strip() or "http://localhost:3000"
