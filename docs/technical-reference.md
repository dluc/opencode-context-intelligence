# Technical reference

For setup and everyday use, start with the [README](../README.md). This page
covers implementation details, advanced configuration, and upload limitations.

A standalone, local web app for your end-of-day workflow:

1. Read the OpenCode V2 SQLite database **read-only**, including committed WAL data.
2. Browse/search/filter sessions and select the ones you want.
3. Export native Context Intelligence `events.jsonl` + `metadata.json` files.
4. Download the ZIP, or **separately confirm** an incremental upload using CI's
   standard `/events` API and `aci-event-v1` idempotency keys.

No plugin, no OpenCode config edits, no always-on telemetry. OpenCode need not be
running. Python 3.11+ is required; Azure authentication uses `azure-identity`.

## Run

From the project root:

```sh
./start.sh
```

Requires [uv](https://docs.astral.sh/uv/). The launcher prepares dependencies in
uv's cache, works from any working directory, and forwards server options:

```sh
./start.sh --port 8767
./start.sh --help
```

Connection details still come from Settings; the launcher contains no personal
defaults. For advanced CLI commands or global `--db` / `--state` options, use the
installed `opencode-ci` command below.

Open the launch URL printed in your terminal, **including its `#token=…` fragment**.
The app listens only on `127.0.0.1:8766`. On a remote machine, forward that port:

```sh
ssh -L 8766:127.0.0.1:8766 your-host
```

Alternatively install the reusable command:

```sh
uv tool install .
opencode-ci serve
```

Defaults honor `XDG_DATA_HOME`:

- Database: `~/.local/share/opencode/opencode.db`
- Local exports/receipts: `~/.local/share/opencode-context-intelligence/`

Override them **before** the subcommand:

```sh
python3 -m opencode_ci --db /path/to/opencode.db --state /private/exports serve --port 8766
```

The UI shows root sessions by default. Exports include their child sessions
(recursively) and reasoning text by default in the web UI, CLI, and export API.
You can uncheck either option in Settings and click **Save** to change your saved
export preferences. It does **not** export unselected sessions merely
because they share a project. Selection survives filtering: check the selection
count before exporting. A selected child's parent is not implicitly exported.

## Connection settings and Azure authentication

Click **Settings** to open the normally hidden connection editor.

Settings replaces the sessions/exports view while open; closing it restores the
session list, exports, and current selection.

Each local export has a **Delete** action with confirmation. It removes that
artifact's ZIP and native files only. OpenCode sessions, other exports, upload
receipts, settings, and remote CI data are kept. Deletion is blocked while an
export/upload/download operation is using local archives. Keeping upload receipts
preserves duplicate suppression when you later export the same sessions again.

Connection fields:

- **url**: your Context Intelligence **base** URL (for example `https://ci.example.com`).
- **auth_resource**: the server's Entra resource URI (for example `api://your-application-id`).

The source/destination summary and the child-session/reasoning export checkboxes
also live inside **Settings**, keeping the main session-selection view uncluttered.
Both export options remain enabled by default.

Click **Save** (below all the fields and checkboxes) to persist connection details
and both export preferences together. The editor stays open. Nothing is auto-saved.
**Close**, the **Settings** toggle, or **Escape** hides it and discards unsaved edits.
Exports use the saved preferences, not any unsaved checkbox changes. The editor
starts closed on each page load. Settings persist to
`~/.local/share/opencode-context-intelligence/settings.json` (or your chosen
`--state` directory), with permissions `0600`. The file is outside the source
repository by default. There are **no deployment-specific defaults in the code**;
a fresh installation starts blank. Saving performs no authentication or upload.

**Test connection** checks the currently entered URL and resource without saving
them. It sends only authenticated `GET /status` (reachability) and `GET /whoami`
(identity verification), never `/events`, and never reads the OpenCode database.
Azure may be contacted to acquire a token. Results appear inside Settings; an
unexposed health/identity route is reported separately from rejected credentials.
These read-only checks cannot prove permission to upload. Testing does not change
saved settings, export preferences, or upload receipts.
Both probes use the bearer token because gateways may protect the health route.
A `401` refreshes the Entra token once before retrying. If token acquisition fails,
an unauthenticated health check is used only to diagnose reachability. Overall
success requires both an accepted health request and a verified identity; an
authenticated health-route rejection is reported explicitly, not hidden.

Run `az login` on the machine running the app before uploading. Like Amplifier,
the app uses `DefaultAzureCredential`: Azure's credential chain includes your
Azure CLI session, environment/service credentials and managed identity. It
requests `<auth_resource>/.default` and sends:

```http
Authorization: Bearer <Entra access token>
Content-Type: application/json
```

Tokens are kept **in memory only**, cached until five minutes before expiry, and
checked/refreshed on each upload attempt. A `401` causes one cache invalidation
and retry; a persistent `401` or `403` fails rather than falling back to another
auth mode. A configured connection is not proof of successful authentication;
authentication happens only when you test the connection or confirm an upload. To pick up an Azure
account switch, save settings again (even unchanged) to reset the app's cache.

For temporary overrides, use `--server-url` / `--auth-resource` or the environment
variables `AMPLIFIER_CONTEXT_INTELLIGENCE_SERVER_URL` /
`AMPLIFIER_CONTEXT_INTELLIGENCE_AUTH_RESOURCE`. These overrides are not written to
disk until you explicitly save through the panel. Without connection details the
app stays export-only. HTTPS is required except for loopback test endpoints. A
base URL can include a reverse-proxy path prefix; do not append `/events` yourself.

Legacy static bearer authentication remains available through
`serve --auth-mode static --server-url https://ci.example.com`. Set the key securely
in `AMPLIFIER_CONTEXT_INTELLIGENCE_API_KEY`, or use `--api-key-env MY_CI_KEY` to name
another environment variable. The app never writes that key to settings or sends
it to the browser. Amplifier settings/keys files are not read automatically.

**Export does not upload.** Each export has a separate “Upload this export” action
with an explicit destination/count confirmation. Progress runs in the background
so the browser stays responsive. Closing the page does not stop an upload; keep
the app process alive until it reports the outcome.

### Idempotency

- Original `ses_…` session IDs and tool call IDs are retained.
- Every derived event has a stable `data.event_id`, hashed from mapping version,
  session ID, original message/block identity and event name. No random event IDs.
- Event times come **only** from stored source times, never the current clock.
  CI uses `(session, event name, timestamp, tool call)` to identify many graph
  records. For synthesized boundaries and equal timestamps, a deterministic
  **+1 microsecond** tie-break preserves distinct events and strict ordering.
  The original, unadjusted timestamp remains in `data.source.timestamp`.
- Unchanged input/options produce byte-identical native event/metadata files.
  ZIP/export-job names are unique local artifact locators, not CI identities.
- A local `uploads.db` records event identity digests and acceptance receipts
  **per destination**. A fully unchanged selection is skipped on subsequent pushes,
  even across app restarts or differently named export ZIPs. If a previously sent
  event changes, the app refuses to send it under the same identity. Receipts are
  per **event**, so overlapping selections and newly appended runs skip all old
  accepted events, not just identical ZIP files.
- New events are posted in source-time order, with parents before children, to
  `/events`. Each POST uses the official SHA-256 `aci-event-v1` formula. The app
  never uses `replay=true`: the server's deduplication remains enabled. Full replay
  is deliberately avoided because some CI graph enrichers use in-memory counters.
- A receipt is committed after **each** successful HTTP acceptance. Partial
  failures retry only unconfirmed events. Reserved event identities also persist
  after failure so changed input cannot silently follow partial acceptance.
- Newly introduced historical events before an accepted session checkpoint are
  refused rather than posted out of order. Choose reasoning inclusion before the
  first push; toggling it later is a backfill, not an append-only continuation.
- Network timeouts or a crash between `202` and local receipt commit are
  **ambiguous**. Retries use identical payloads/keys, but the server's normal
  `/events` deduplication is an in-memory, finite-window cache, not a distributed
  exactly-once protocol. The app refuses unconfirmed retries older than six days;
  server restarts/cache loss can still require manual reconciliation. It does not
  claim an unconditional exactly-once graph-ingestion guarantee.
- Incremental primary events retain their IDs/times. Higher-order graph assembly
  also depends on the CI server retaining/restoring its per-session run/iteration
  state; server-side cursor loss is not something a client can repair by blindly
  replaying old events. Validate this against your deployed server before relying
  on incremental uploads of long-lived sessions across server restarts.
- Receipts mean **accepted by the ingestion API**, not necessarily fully processed
  into Neo4j. `202` can precede asynchronous processing or a dead-letter error.
  Check your CI server's `/status` and queue diagnostics for final ingestion health.
- Keep the receipt DB and original exports. Rebuilding an erased server needs a
  deliberate replay with the official uploader, bypassing this app's local receipt
  skip. Do not blindly replay a partially received stream into an active server.

## Format and fidelity

Export layout:

```text
exports/<artifact-id>/
  manifest.json
  export.zip
  projects/<working-directory-slug>/sessions/<original-session-id>/context-intelligence/
    events.jsonl
    metadata.json
```

Native records use `{event, timestamp, workspace, data}`. Metadata declares
`format: context-intelligence`, session/parent IDs and the recorded working directory.
Mapping notes and per-session SHA-256 digests are shown in the UI and manifest.

| OpenCode record | CI representation |
| --- | --- |
| Session creation | `session:start`; parent relationship retained |
| Fork relationship | `session:fork`, distinguished from delegation |
| User message | `prompt:submit`, reconstructed `execution:start` |
| Completed assistant message | `provider:request`, `llm:request`, `llm:response` |
| Text / reasoning (included by default) | `content_block:start` / `content_block:end` |
| Tool call + terminal result/error | `tool:pre`, `tool:post` / `tool:error` |
| Persisted idle marker | `orchestrator:complete`, reconstructed `execution:end`, `prompt:complete` |
| System / synthetic message | `opencode:system` / `opencode:synthetic` |
| Unknown message type | Custom event with type/text and an explicit mapping note |

This is **retrospective reconstruction**, not complete live telemetry. Request
bodies, exact token streaming, permission interactions and other events absent
from the saved records cannot be recovered. Request events are marked reconstructed.
No events are invented for delegation or skill loads merely from tool names.
An idle marker ends a **run**, not the whole resumable session: `session:end` is
not fabricated. Unfinished assistant messages are omitted with a note; export them
again after they finish. Binary attachments are not embedded. Native provider
payloads/encrypted reasoning signatures are not copied. Available reasoning text is included by default.

Read-only browsing/export is tested against the OpenCode **2.2.0 V2** schema. Other
schemas fail explicitly rather than guessing at V1 tables. The database is read in
a transaction so sessions/messages in one export are a consistent snapshot.

## CLI export (no web UI)

```sh
python3 -m opencode_ci list --search my-project
python3 -m opencode_ci export --session ses_abc --session ses_def
```

The CLI uses your saved child-session and reasoning preferences (both enabled on
a fresh installation). Override them with `--include-children` / `--no-children`
or `--include-reasoning` / `--no-reasoning` for a single export.
This command never uploads.
You can later upload the `projects/` directory through the official CLI yourself.

## Privacy and security

Exports may contain **sensitive prompts, tool inputs and tool outputs**. They are
not automatically secret-redacted. Review the files before uploading. The default
app state is private to your OS user (directories `0700`, files `0600`). The app
does not query OpenCode credential/account tables or embed their values.

The web server is loopback-only, checks the Host header, requires a random local
token for all data routes, protects POSTs against cross-site requests, and uses a
restrictive content security policy. Transcript/metadata strings use DOM
`textContent`, never raw HTML. Credentials never enter the browser or process argv;
redirects are refused rather than forwarding bearer credentials to another host.
Avoid sharing the launch URL or exposing the port through a public proxy. This is
a single-user local utility, not a hosted multi-tenant application.

### Publishing the repository

Keep connection settings, tokens, exports, upload receipts, screenshots and real
session databases **outside the repository**. `.gitignore` excludes common local
settings, environment, database and archive files as defense in depth; it cannot
remove a secret that was already committed. Before publishing, review staged
files and any existing Git history. Do not copy real deployment URLs, application
IDs, personal paths or transcripts into examples, fixtures, logs or screenshots.
All included examples and test transcripts are synthetic. Public package defaults
are intentionally blank; provision personal defaults in your private state file.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

Tests use synthetic databases/transcripts only: read-only/WAL behavior, selection,
parent traversal, deterministic exports, append-only stability, privacy defaults,
upload receipts/retries/conflicts, and HTTP auth/CSRF. A loopback mock CI server
checks the actual wire payload/keys and repeated pushes. No production endpoint is
contacted by these tests. The upload lock uses POSIX `flock` (Linux/macOS).

## References

- [Context Intelligence server](https://github.com/microsoft/amplifier-context-intelligence)
- [Official uploader](https://github.com/microsoft/amplifier-bundle-context-intelligence/tree/main/modules/tool-context-intelligence-upload)

Server/uploader contracts reviewed at `2d308825f300cf51555390b7f4747720ad459e78`
and `2a8d7bd1e022971c395ec38ed797bf75fcf7aff7`.
