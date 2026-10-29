"""Deterministic mapping from persisted OpenCode V2 messages to native CI files.

Never consult the wall clock while mapping. Source timestamps are preserved in
data.source.timestamp; derived events use a deterministic +1 microsecond tie
break when necessary for CI's timestamp-based event identities and cursors.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
import zipfile
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .database import load_sessions

MAPPING_VERSION = "opencode-v2-ci-v1"


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def timestamp(ms):
    if not isinstance(ms, (int, float)) or isinstance(ms, bool) or ms <= 0:
        raise ValueError(f"Missing/invalid source timestamp: {ms!r}")
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def iso(dt):
    return dt.isoformat(timespec="microseconds").replace("+00:00", "Z")


def workspace_for(directory):
    slug = re.sub(r"[^a-z0-9]+", "-", directory.lower()).strip("-")
    return slug or "opencode"


def map_session(session, *, include_reasoning=True):
    sid = session["id"]
    parent = session.get("parent_id") or ""
    workspace = workspace_for(session["directory"])
    events = []
    warnings = []
    excluded = set()

    def emit(name, source_id, anchor, payload=None):
        original = timestamp(anchor)
        event_id = "ocie_" + hashlib.sha256(
            canonical([MAPPING_VERSION, sid, source_id, name]).encode()
        ).hexdigest()
        data = {
            **(payload or {}),
            "session_id": sid,
            "parent_id": parent,
            "timestamp": iso(original),
            "event_id": event_id,
            "source": {"host": "opencode", "mapping": MAPPING_VERSION,
                       "id": source_id, "timestamp": iso(original)},
        }
        record = {"event": name, "timestamp": data["timestamp"], "workspace": workspace, "data": data}
        events.append(record)
        return record

    # A fork is not a delegation. Preserve both relationships without conflating them.
    fork = session.get("fork_session_id")
    if fork:
        emit("session:fork", sid + ":fork", session["time_created"], {"parent": fork})
    emit("session:start", sid, session["time_created"], {
        "working_dir": session["directory"],
        "metadata": {"source": "opencode"},
    })

    prompt = None
    responses = []
    turns = 0
    message_count = 0
    last_status = "running"
    for row in session["messages"]:
        mid, kind, data = row["id"], row["type"], row["data"]
        timing = data.get("time") or {}
        created = timing.get("created") or row["time_created"]
        if kind == "user":
            turns += 1
            message_count += 1
            prompt = data.get("text", "")
            responses = []
            files = [{k: f[k] for k in ("name", "mime") if k in f}
                     for f in data.get("files", []) if isinstance(f, dict)]
            emit("prompt:submit", mid, created, {"prompt": prompt, "turn": turns, "files": files})
            emit("execution:start", mid, created, {"reconstructed": True})
            if files:
                warnings.append(f"{mid}: attachment references included; binary/file contents are not exported")
            last_status = "running"
        elif kind == "assistant":
            completed = timing.get("completed")
            if not completed:
                warnings.append(f"{mid}: unfinished assistant message omitted; export again when idle")
                last_status = "running"
                continue
            model = data.get("model") or {}
            message_count += 1
            details = {"provider": model.get("providerID"), "model": model.get("id"), "source_message_seq": row["seq"]}
            emit("provider:request", mid, created, details)
            emit("llm:request", mid, created, {**details, "message_count": message_count, "reconstructed": True})
            text = []
            for index, block in enumerate(data.get("content", [])):
                btype = block.get("type")
                bid = f"{mid}:block:{index}"
                bt = block.get("time") or {}
                start = bt.get("created") or created
                end = bt.get("completed") or completed
                if btype in ("text", "reasoning"):
                    # Reserve the same clock slots even when reasoning is excluded.
                    first = emit("content_block:start", bid, start, {"block_index": index})
                    final = emit("content_block:end", bid, end, {
                        "block_index": index, "block": {"type": "thinking" if btype == "reasoning" else "text",
                                                          "text": block.get("text", "")},
                    })
                    if btype == "reasoning" and not include_reasoning:
                        excluded.update([first["data"]["event_id"], final["data"]["event_id"]])
                    if btype == "text":
                        text.append(block.get("text", ""))
                elif btype == "tool":
                    call_id = block.get("id")
                    if not call_id:
                        raise ValueError(f"{mid}: tool block has no stable call ID")
                    state = block.get("state") or {}
                    # Use message+index for emitter identity; preserve real tool_call_id for graph joins.
                    tool = {"tool_call_id": call_id, "tool_name": block.get("name")}
                    emit("content_block:start", bid, start, {"block_index": index})
                    emit("content_block:end", bid, start, {
                        "block_index": index, "block": {"type": "tool_call", "id": call_id},
                    })
                    emit("tool:pre", bid, bt.get("ran") or start, {**tool, "tool_input": state.get("input", {})})
                    status = state.get("status")
                    if status in ("completed", "error", "failed"):
                        output = state.get("content", [])
                        preview = "\n".join(c.get("text", "") for c in output if isinstance(c, dict) and c.get("type") == "text") if isinstance(output, list) else str(output)
                        failed = status in ("error", "failed")
                        payload = {**tool, "duration_ms": max(0, end - (bt.get("ran") or start)),
                                   "result": {"output": output, "error": state.get("error") if failed else None},
                                   "result_output_preview": preview[:2000], "result_error": state.get("error") if failed else None}
                        if failed:
                            payload["error"] = state.get("error") or preview or status
                        emit("tool:error" if failed else "tool:post", bid, end, payload)
                    else:
                        warnings.append(f"{mid}/{call_id}: tool has no terminal state ({status}); no result fabricated")
                else:
                    warnings.append(f"{mid}/{index}: unmapped content type {btype!r}")
            tokens = data.get("tokens") or {}
            usage = {"input_tokens": tokens.get("input"), "output_tokens": tokens.get("output"),
                     "reasoning_tokens": tokens.get("reasoning"),
                     "cache_read_input_tokens": (tokens.get("cache") or {}).get("read"),
                     "cache_creation_input_tokens": (tokens.get("cache") or {}).get("write")}
            response = "\n".join(text)
            responses.append(response)
            emit("llm:response", mid, completed, {
                **details, "usage": usage, "stop_reason": data.get("finish"),
                "duration_ms": max(0, completed - created), "response": response,
                "cost": data.get("cost"),
            })
        elif kind == "idle":
            outcome = data.get("outcome")
            status = {"succeeded": "success", "cancelled": "cancelled", "failed": "error"}.get(outcome, "unknown")
            emit("orchestrator:complete", mid, created, {"status": status, "turn_count": turns, "orchestrator": "opencode"})
            emit("execution:end", mid, created, {"status": status, "reconstructed": True})
            if prompt is not None:
                emit("prompt:complete", mid, created, {"prompt": prompt, "response": "\n".join(responses), "status": status})
                prompt = None
                responses = []
            last_status = "idle" if status == "success" else status
        elif kind in ("system", "synthetic"):
            emit(f"opencode:{kind}", mid, created, {"text": data.get("text", ""), "description": data.get("description")})
        else:
            # Keep source record identity/type visible; don't silently drop an unknown message.
            warnings.append(f"{mid}: message type {kind!r} exported as opencode:message")
            emit("opencode:message", mid, created, {"message_type": kind, "text": data.get("text", "")})

    # Global source-time order within the session; stable insertion order resolves
    # equal anchors. Keep excluded reasoning in the clock so a privacy toggle
    # never changes the IDs or timestamps of the other events.
    events.sort(key=lambda e: e["data"]["source"]["timestamp"])
    last = None
    for event in events:
        at = datetime.fromisoformat(event["timestamp"].replace("Z", "+00:00"))
        if last and at <= last:
            at = last + timedelta(microseconds=1)
        last = at
        event["timestamp"] = event["data"]["timestamp"] = iso(at)
    events = [e for e in events if e["data"]["event_id"] not in excluded]

    metadata = {
        "format": "context-intelligence", "version": "1.0.0", "session_id": sid,
        "workspace": workspace, "working_dir": session["directory"], "parent_id": parent,
        "started_at": events[0]["timestamp"], "last_event_at": events[-1]["timestamp"],
        "status": last_status, "ended_at": "", "source": "opencode", "mapping": MAPPING_VERSION,
        "source_session_id": sid, "fork_session_id": fork,
    }
    # Idle is a completed RUN, not a closed session: never fabricate session:end.
    return {"events": events, "metadata": metadata, "warnings": warnings}


def private_write(path, text):
    with open(path, "x", encoding="utf-8", opener=lambda p, flags: os.open(p, flags, 0o600)) as f:
        f.write(text)


def export_sessions(database: Path, state: Path, ids: list[str], *, include_children=True, include_reasoning=True):
    sessions = load_sessions(database, ids, include_children)
    mapped = [(s, map_session(s, include_reasoning=include_reasoning)) for s in sessions]
    export_id = uuid.uuid4().hex  # artifact locator ONLY; never part of an event or idempotency key
    root = state / "exports" / export_id
    root.mkdir(parents=True, mode=0o700)
    report = {"id": export_id, "mapping": MAPPING_VERSION, "sessions": [], "warnings": [],
              "include_children": include_children, "include_reasoning": include_reasoning}
    for session, mapped_session in mapped:
        sid = session["id"]
        if not re.fullmatch(r"[A-Za-z0-9_-]+", sid):
            raise ValueError("Unsafe session ID in database")
        relative = Path("projects") / mapped_session["metadata"]["workspace"] / "sessions" / sid / "context-intelligence"
        target = root / relative
        target.mkdir(parents=True, mode=0o700)
        lines = "".join(canonical(e) + "\n" for e in mapped_session["events"])
        meta = canonical(mapped_session["metadata"]) + "\n"
        private_write(target / "events.jsonl", lines)
        private_write(target / "metadata.json", meta)
        report["sessions"].append({"id": sid, "title": session.get("title"), "path": str(relative),
                                   "events": len(mapped_session["events"]),
                                   "event_types": dict(Counter(e["event"] for e in mapped_session["events"])),
                                   "sha256": hashlib.sha256((lines + meta).encode()).hexdigest()})
        report["warnings"].extend(mapped_session["warnings"])
    report["fingerprint"] = hashlib.sha256(canonical({
        s["id"]: s["sha256"] for s in report["sessions"]
    }).encode()).hexdigest()
    private_write(root / "manifest.json", canonical(report) + "\n")
    fd = os.open(root / "export.zip", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f, zipfile.ZipFile(f, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted((root / "projects").rglob("*")):
            if path.is_file():
                archive.write(path, str(path.relative_to(root)))
        archive.write(root / "manifest.json", "manifest.json")
    return report
