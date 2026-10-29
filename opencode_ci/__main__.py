from __future__ import annotations

import argparse
import json
import os
import sqlite3
from pathlib import Path

from .database import list_sessions
from .exporter import export_sessions
from .server import make_server
from .settings import load_settings


def main():
    parser = argparse.ArgumentParser(description="Select, export and optionally upload OpenCode V2 sessions")
    parser.add_argument("--db", type=Path, default=Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))) / "opencode/opencode.db")
    parser.add_argument("--state", type=Path, default=Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))) / "opencode-context-intelligence")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="Open a loopback-only local web app")
    serve.add_argument("--port", type=int, default=8766)
    serve.add_argument("--server-url", default=os.environ.get("AMPLIFIER_CONTEXT_INTELLIGENCE_SERVER_URL"), help="Override the CI base URL saved in private Connection settings")
    serve.add_argument("--auth-resource", default=os.environ.get("AMPLIFIER_CONTEXT_INTELLIGENCE_AUTH_RESOURCE"), help="Override the Entra resource URI saved in private Connection settings")
    serve.add_argument("--auth-mode", choices=("entra", "static"), default="entra", help="Entra via Azure credentials (default), or legacy static bearer auth")
    serve.add_argument("--api-key-env", default="AMPLIFIER_CONTEXT_INTELLIGENCE_API_KEY", help="Environment variable containing the static bearer credential (never the key itself)")
    listing = sub.add_parser("list", help="List session metadata, without reading message contents")
    listing.add_argument("--search", default="")
    listing.add_argument("--include-children", action="store_true")
    export = sub.add_parser("export", help="Export explicitly selected sessions; never upload")
    export.add_argument("--session", action="append", required=True, help="Repeat for multiple sessions")
    children = export.add_mutually_exclusive_group()
    children.add_argument("--include-children", dest="include_children", action="store_true", help="Include child sessions, overriding saved preferences")
    children.add_argument("--no-children", dest="include_children", action="store_false", help="Exclude child sessions, overriding saved preferences")
    reasoning = export.add_mutually_exclusive_group()
    reasoning.add_argument("--include-reasoning", dest="include_reasoning", action="store_true", help="Include reasoning text, overriding saved preferences")
    reasoning.add_argument("--no-reasoning", dest="include_reasoning", action="store_false", help="Exclude reasoning text, overriding saved preferences")
    export.set_defaults(include_children=None, include_reasoning=None)
    args = parser.parse_args()
    args.db = args.db.expanduser().resolve()
    args.state = args.state.expanduser().resolve()
    args.state.mkdir(parents=True, mode=0o700, exist_ok=True)
    # All application outputs, including uploader progress replacements, are private.
    os.umask(0o077)
    try:
        if args.command == "list":
            print(json.dumps(list_sessions(args.db, search=args.search, include_children=args.include_children), indent=2))
        elif args.command == "export":
            settings = load_settings(args.state)
            print(json.dumps(export_sessions(args.db, args.state, args.session,
                                             **{name: settings[name] if getattr(args, name) is None else getattr(args, name)
                                                for name in ("include_children", "include_reasoning")}), indent=2))
        else:
            server, token = make_server(args.db, args.state, port=args.port, server_url=args.server_url,
                                       api_key_env=args.api_key_env, auth_resource=args.auth_resource, auth_mode=args.auth_mode)
            print(f"Open http://127.0.0.1:{server.server_port}/#token={token}", flush=True)
            print("Local-only. OpenCode DB is read-only. Export and upload require separate actions.", flush=True)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                server.server_close()
    except (ValueError, OSError, sqlite3.Error) as exc:
        parser.exit(1, f"Error: {exc}\n")


if __name__ == "__main__":
    main()
