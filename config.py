from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _get_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


PORT: int = int(os.getenv("PORT", "9000"))
ADMIN_PASSWORD: str = os.getenv("ADMIN_PASSWORD", "")
SECRET_KEY: str = os.getenv("SECRET_KEY", "change-me-in-production")
DATABASE_PATH: Path = Path(os.getenv("DATABASE_PATH", "data/deploy-hook.db"))
SESSION_HTTPS_ONLY: bool = _get_bool("SESSION_HTTPS_ONLY", False)
