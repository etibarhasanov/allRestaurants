"""Environment/config loading."""

from __future__ import annotations

import os
from typing import Dict, Optional

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - dotenv is optional at runtime
    load_dotenv = None


def load_env(dotenv_path: Optional[str] = None) -> Dict[str, str]:
    """Load ``.env`` (if present) and return the process environment."""
    if load_dotenv is not None:
        load_dotenv(dotenv_path=dotenv_path, override=False)
    return dict(os.environ)


DEFAULT_DB_PATH = "data/restaurants.db"
