"""Read OpenCode's materialized V2 session records without modifying its DB."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def connect(path: Path):
    # Do NOT use immutable=1: a live OpenCode database can have committed WAL data.
    db = sqlite3.connect(path.expanduser().resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA query_only=ON")
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"session_v2", "session_message"} <= tables:
            raise ValueError("Unsupported database: expected OpenCode V2 session_v2/session_message tables")
        db.execute("BEGIN")  # consistent WAL snapshot across all SELECTs in this connection
        yield db
    finally:
        db.close()


def list_sessions(path: Path, *, search="", since=0, include_children=False, limit=200, offset=0):
    with connect(path) as db:
        where = ["s.time_updated >= ?"]
        args: list = [since]
        if not include_children:
            where.append("s.parent_id IS NULL")
        if search:
            where.append("(s.title LIKE ? ESCAPE '\\' OR s.directory LIKE ? ESCAPE '\\' OR s.id LIKE ? ESCAPE '\\')")
            pattern = "%" + search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            args.extend([pattern] * 3)
        clause = " AND ".join(where)
        total = db.execute(f"SELECT count(*) FROM session_v2 s WHERE {clause}", args).fetchone()[0]
        rows = db.execute(
            f"""SELECT s.id,s.title,s.directory,s.parent_id,s.fork_session_id,s.time_created,
                s.time_updated,s.time_idle,s.idle_outcome,s.time_archived,
                (SELECT type FROM session_message m WHERE m.session_id=s.id ORDER BY seq DESC LIMIT 1) AS last_message_type,
                (SELECT count(*) FROM session_message m WHERE m.session_id=s.id) AS messages,
                (SELECT count(*) FROM session_v2 c WHERE c.parent_id=s.id) AS children
                FROM session_v2 s WHERE {clause}
                ORDER BY s.time_updated DESC,s.id LIMIT ? OFFSET ?""",
            [*args, min(max(limit, 1), 500), max(offset, 0)],
        )
        return {"sessions": [dict(r) for r in rows], "total": total}


def load_sessions(path: Path, session_ids: list[str], include_children: bool):
    if not session_ids or len(session_ids) > 500:
        raise ValueError("Select between 1 and 500 sessions")
    with connect(path) as db:
        selected = {}
        queue = list(dict.fromkeys(session_ids))
        while queue:
            sid = queue.pop(0)
            if sid in selected:
                continue
            row = db.execute("SELECT * FROM session_v2 WHERE id=?", (sid,)).fetchone()
            if row is None:
                raise ValueError(f"Session not found: {sid}")
            session = dict(row)
            session["messages"] = [
                {**dict(m), "data": json.loads(m["data"])}
                for m in db.execute(
                    "SELECT id,type,seq,time_created,time_updated,data FROM session_message WHERE session_id=? ORDER BY seq,id",
                    (sid,),
                )
            ]
            selected[sid] = session
            if include_children:
                queue.extend(r[0] for r in db.execute(
                    "SELECT id FROM session_v2 WHERE parent_id=? ORDER BY time_created,id", (sid,)
                ))
        # Parents before children; refuse cycles rather than silently misrepresent lineage.
        result = []
        pending = dict(selected)
        while pending:
            ready = [s for s in pending.values() if s.get("parent_id") not in pending]
            if not ready:
                raise ValueError("Cycle in session parent relationships")
            for session in sorted(ready, key=lambda s: (s["time_created"], s["id"])):
                result.append(session)
                del pending[session["id"]]
        return result
