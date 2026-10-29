"""Confined local artifact deletion. Never modify sessions or upload receipts."""

import fcntl
import json
import os
import re
import shutil
from pathlib import Path


class ExportBusy(ValueError):
    pass


def delete_export(state: Path, export_id):
    if not isinstance(export_id, str) or not re.fullmatch(r"[a-f0-9]{32}", export_id):
        raise ValueError("Invalid export ID")
    base = state / "exports"
    root = base / export_id
    if base.is_symlink() or root.is_symlink():
        raise ValueError("Refusing to delete a symlinked export")
    if not root.exists():
        return {"status": "already_deleted", "id": export_id}
    if not root.is_dir() or root.resolve().parent != base.resolve():
        raise ValueError("Invalid export directory")
    manifest = root / "manifest.json"
    if manifest.is_symlink() or not manifest.is_file():
        raise ValueError("Not a recognized local export")
    report = json.loads(manifest.read_text(encoding="utf-8"))
    if not isinstance(report, dict) or report.get("id") != export_id:
        raise ValueError("Export manifest does not match its directory")
    # Match the uploader's inter-process lock. Another app instance must not
    # lose files it is currently using for delivery.
    with open(state / "upload.lock", "a", opener=lambda p, flags: os.open(p, flags, 0o600)) as file:
        try:
            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ExportBusy("An upload is using local exports. Try deleting after it finishes") from None
        shutil.rmtree(root)  # Only this validated artifact, never the exports/state roots.
    return {"status": "deleted", "id": export_id}
