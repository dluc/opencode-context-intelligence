"""Private connection and export settings, stored outside the source tree."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from .auth import normalize_resource
from .upload import normalize_url


def validate_settings(value):
    if not isinstance(value, dict) or set(value) - {"url", "auth_resource", "include_children", "include_reasoning"}:
        raise ValueError("Unknown settings field")
    url = value.get("url", "")
    if not isinstance(url, str):
        raise ValueError("url must be a string")
    options = {name: value.get(name, True) for name in ("include_children", "include_reasoning")}
    if any(not isinstance(option, bool) for option in options.values()):
        raise ValueError("Export options must be booleans")
    return {"url": normalize_url(url.strip()) if url.strip() else "",
            "auth_resource": normalize_resource(value.get("auth_resource", "")), **options}


def load_settings(state: Path):
    path = state / "settings.json"
    if not path.exists():
        return validate_settings({})
    try:
        return validate_settings(json.loads(path.read_text(encoding="utf-8")))
    except (ValueError, OSError):
        raise ValueError("Private connection settings could not be read; correct the settings.json file in the application state directory") from None


def save_settings(state: Path, value):
    value = validate_settings(value)
    state.mkdir(parents=True, mode=0o700, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".settings-", dir=state)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(value, file, sort_keys=True, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, state / "settings.json")
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return value
