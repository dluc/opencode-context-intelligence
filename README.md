# OpenCode → Context Intelligence

Pick the OpenCode conversations you want to analyse, export them, and send them
to your [Context Intelligence](https://github.com/microsoft/amplifier-context-intelligence)
server — from a browser. Download a copy instead if you are not ready to upload.

You run it yourself, as your own user, on the machine where your OpenCode sessions
live. It reads those sessions without changing them. Nothing uploads until you
choose an export and confirm.

## When you'd use it

- **You are done for the day and want to collect your work.** Find today's
  sessions, select the ones that matter, and export them together.
- **You want a copy before sending anything.** Download the export, look through
  it, and upload it later — or keep it local.
- **You come back to a conversation tomorrow.** Export it again when there is
  more to include. The app remembers what it has already uploaded.

## What you get

- **A session picker.** Search by title or project and filter by date.
- **Child sessions and reasoning included.** Both are on by default; change
  them in Settings if you want a smaller export.
- **Separate export and upload steps.** Creating an export never sends it
  anywhere. Each upload needs your confirmation.
- **Your existing Azure login.** No fixed API key to copy into the app when
  your server uses Microsoft Entra.
- **A connection test.** Check the server and your login without sending any
  conversations.
- **Local cleanup.** Delete export copies when you no longer need them, without
  deleting the original OpenCode sessions or anything on the server.

## Getting started

You need [uv](https://docs.astral.sh/uv/), Python 3.11 or newer, and OpenCode V2
sessions on the machine. Run it as **you**, not root. The app is designed for Linux
and macOS, and has been tested on Linux with OpenCode 2.2.0. You only need a
Context Intelligence server if you want to upload; local exports work without one.

```bash
git clone https://github.com/dluc/opencode-context-intelligence.git
cd opencode-context-intelligence
./start.sh
```

The launcher sets up its dependencies and prints a browser link. Open the **whole
link**, including the part after `#` — that is how the local app recognises you.
It runs at `127.0.0.1:8766` by default, so it is only reachable on your own machine.

Keep the terminal open while you use the app. **Ctrl+C** stops it; run
`./start.sh` again next time. Your saved settings, exports, and upload history
stay where they are. OpenCode itself does not need to be running.

If the port is already in use:

```bash
./start.sh --port 8767
```

## Everyday use

### Exporting sessions

Find the sessions you want, tick their boxes, and click **Export selected
sessions**. The resulting copy appears under **Local exports**.

From there, choose **Download ZIP** to keep a copy, or **Upload this export** to
send it to your configured server. The upload asks you to confirm the destination
before it starts.

Exports can include prompts, reasoning, file contents, and tool results.
**They are not automatically scrubbed of secrets.** Choose carefully and review
the files before uploading anything sensitive.

### Connecting to your server

If your server uses your Azure login, sign in on the machine running the app:

```bash
az login
```

Open **Settings** and enter the two values supplied by whoever runs your server:

- **url** — the server's base address, such as `https://ci.example.com`.
- **auth_resource** — the Azure application it uses, usually something like
  `api://your-application-id`.

**Test connection** checks the values you have entered without saving them or
sending session data. It reports server access and authentication separately.
A successful test does not, by itself, prove that you have permission to upload.

**Save** remembers the connection details and both export checkboxes together.
Nothing is saved automatically. **Close** leaves Settings and discards unsaved
changes. Settings stays out of the way while you browse sessions.

### Cleaning up local exports

Click **Delete** on a local export and confirm. It removes that copy's files
only — your OpenCode conversations and uploaded data are untouched.

The app also keeps its upload history, so deleting a local copy does not make it
forget what it has already sent.

## Where your files live

By default, the app keeps its files in:

```text
~/.local/share/opencode-context-intelligence/
```

| Item | Location |
| --- | --- |
| Export copies and ZIPs | `exports/` |
| Saved settings | `settings.json` |
| Upload history | `uploads.db` |

**Settings shows the actual export folder.** If your system uses a different
data directory, that is the path to look at.

These are your personal files, not part of the application checkout. Keep them
out of anything you publish or share. The code ships with no private server
addresses, Azure application IDs, or credentials.

## FAQ

### What does “Nothing new to upload” mean?

Everything in that export was uploaded before. The app skipped it rather than
sending it again. It is not an error.

### Can I retry after a failed upload?

Yes. The app remembers each event the server received and skips those on retry.
Keep the upload-history file if you want it to keep remembering.

There is one awkward case: a connection can drop after the server receives an
event but before the app hears back. The app retries with the same event identity,
but it cannot promise duplicate-free recovery through every server restart or
network failure. If it asks you to check an uncertain upload, do that rather than
deleting the history and starting over. The [technical reference](docs/technical-reference.md#idempotency)
explains the limits.

### Does “Upload finished” mean the server has finished processing it?

It means the server has received the events. Context Intelligence may still be
processing them. Check your server if the imported sessions do not appear yet.

### Can I export a session that is still running?

Yes, but unfinished assistant messages are left out. Export it again once that
work has finished. The export can only include what OpenCode saved; it cannot
recreate missing conversation details.

### Can I use it from another computer?

Run the app where the OpenCode sessions live, then forward its port over SSH:

```bash
ssh -L 8766:127.0.0.1:8766 your-host
```

Open the printed browser link on your own computer. Do not share that link or
expose the app directly to the internet — this is a personal tool, not a shared
web service.

## Working on the code

The [technical reference](docs/technical-reference.md) covers command-line use,
configuration options, the export format, authentication, and upload guarantees.

Run the tests with:

```bash
uv run --no-project --with . python -B -m unittest discover -s tests -v
```

Tests use made-up conversations and local test servers, not your sessions or
your real Context Intelligence endpoint.

## License

[MIT](LICENSE).
