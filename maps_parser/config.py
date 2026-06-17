from __future__ import annotations

import os
from pathlib import Path


def load_env(path: str | Path = ".env") -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(path)


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def require_env(name: str) -> str:
    value = env(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value
