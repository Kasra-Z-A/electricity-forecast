"""Local configuration. Secrets must never appear in diagnostics."""

import os
from pathlib import Path

from dotenv import load_dotenv


def api_key() -> str:
    load_dotenv(Path(".env"), override=False)
    key = os.environ.get("ENTSOE_API_KEY", "").strip()
    if not key:
        raise ValueError("Set ENTSOE_API_KEY in .env or the environment first.")
    return key
