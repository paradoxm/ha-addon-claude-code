# Claude Code

Runs the Claude Code CLI inside Home Assistant, with a console for talking to it,
a terminal, and an HTTP API for sending prompts and collecting the results.

Everything lives in the add-on's own `/data` volume. No Home Assistant folder is
mapped, and nothing else needs filesystem access.

## Hardware requirements

**Check this before installing.** Claude Code ships as a native binary that
requires the **AVX** instruction set. Without it the binary is killed the moment
it starts, and [Anthropic documents no
workaround](https://code.claude.com/docs/en/troubleshoot-install#illegal-instruction).

Run this on the machine that hosts Home Assistant, or in this add-on's terminal:

```bash
grep -m1 -ow avx /proc/cpuinfo || echo "no AVX: Claude Code cannot run here"
```

Also needed: **x86-64 or ARM64**, and **4 GB+ RAM** — a large skill spawns
subagents, so a 2 GB box will not do.

### What lacking AVX looks like

Running `claude` prints `Illegal instruction (core dumped)`, and the add-on log
reports:

```
ERROR: The claude binary did not run (exit 132) on x86_64.
ERROR: This CPU reports no AVX support, and Claude Code's native binary requires it.
ERROR: Model: AMD E-450 APU with Radeon(tm) HD Graphics
```

Exit 132 is 128 + 4, meaning the process died on SIGILL. Through the job API the
same failure appears as `"exit_code": -4`.

### Which processors are affected

AVX arrived with Intel Sandy Bridge and AMD Bulldozer in 2011, so most desktop
and laptop chips from 2011 onward have it. The exceptions are the low-power
lines: AMD Bobcat (E-350, E-450, C-60 and similar) and the Intel Atom and
Celeron parts of that era never got AVX, whatever year the machine was sold.
Those chips are common in the small, quiet boxes people pick for Home Assistant,
which is exactly where this bites.

On a **virtual machine** the CPU may support AVX while the hypervisor hides it
behind a generic model such as `kvm64` or `qemu64`. That case is fixable: set the
guest CPU type to `host` and reboot the guest. In Proxmox this is
**VM → Hardware → Processors → Type**.

On **bare metal without AVX** there is nothing to configure. The remaining
options are to host Home Assistant on a machine that has AVX, or to run this
add-on's image as a plain Docker container on another machine that does — it does
not depend on the Supervisor, and needs only a `/data` volume holding an
`options.json`.

## Install

1. **Settings → Add-ons → Add-on Store → ⋮ → Repositories**, and add
   `https://github.com/paradoxm/ha-addon-claude-code`.
2. Install *Claude Code*. The image is built on the Home Assistant machine, so the
   first install takes a few minutes.
3. Start it and open **Web UI**.
4. Click **Terminal**, run `claude`, choose your account, and open the link it
   prints — on any machine — then paste the code back. `claude auth status` says
   whose account it is, and the console's header turns to *signed in*.

The sign-in is needed once: it is kept in the add-on's own `/data`, so it outlives
restarts, add-on updates and CLI updates and goes into the backups, and the add-on
renews the token by itself before it can expire. `claude auth logout` in the same
terminal signs out. Requires a Claude account on a Pro, Max, Team, or Enterprise
plan.

## The console

The Web UI is a conversation with Claude Code. It is not a separate chat bolted on
the side: every message is a headless CLI run against Claude Code's own session, so
a conversation started here can be continued in the terminal with `claude
--resume`, and one started in the terminal shows up here.

- **Enter** sends, **Shift+Enter** starts a new line.
- The reply streams in as it is written. **Cancel** on the in-flight reply stops it,
  which is what `Esc` does in the terminal.
- Sending while Claude is still answering **queues** the message. Queued messages
  are listed under the composer in the order they will run, and each resolves the
  conversation to continue at the moment it starts — so a queued message joins the
  conversation rather than forking a new one.
- **Model**, **effort** and **permission mode** sit in the conversation's header.
  They default to the add-on's options and apply to the next message only.
- **New chat** starts a fresh conversation — the equivalent of `/clear`, which has
  no headless form: a conversation with nothing to resume simply is a new one.
- The **magnifier** opens the conversation list with a filter box. Opening one is
  `/resume`. A conversation can be renamed there, or by clicking the name in the
  header; either runs `/rename`, so the name is written into Claude Code's own
  transcript and every client sees it. The **bin** removes one for good, and asks first: it
  becomes a red tick, and only the second press deletes. A conversation with a
  turn in flight is refused — that transcript is open in the CLI.
- Under the transcript is **how much of the context window is left**. Past 50% a
  **Compact** button appears and runs `/compact`.
- Beside it, a count of **unread notices**: what the CLI reported alongside the
  conversation rather than as part of it — a refused request, an unknown command, a
  tool call that failed, an API error. They are one click away and never join the
  message flow, the ones you have not seen are marked, and once they have all been
  read the count is not shown at all.
- Claude Code records a slash command, its output, injected reminders and its own
  "No response requested." as ordinary messages too. Those are filtered out
  entirely, since none of them is anything either of you said.

`/compact` summarises the conversation, but it cannot shrink the fixed overhead —
the system prompt, tool definitions and skill metadata — which is most of the cost
of a short conversation. On a two-turn conversation the figure barely moves. That is
why the button only appears once the conversation itself is the bulk of the window.

### Settings

**Settings** opens seven tabs, on **Usage**:

- **Usage** — both windows of the plan's allowance, what is used, when each comes back, and
  the figure that window stops at, marked on its own bar. The two figures are set apart:
  the five-hour window may be left high while the week is held to something stricter.
  Green, amber past 70 per cent, red past 90 — and red for the window that is holding work
  up, whatever figure that is at.
- **Skills** — everything below.
- **Permissions** — `~/.claude/settings.json` in a text box, validated as JSON
  before it is saved. `USE_BUILTIN_RIPGREP=0` is enforced by the add-on and shown
  as such: Alpine needs the distro `ripgrep`, so that one key is put back whatever
  the file says.
- **MCP** — every configured server with a switch, grouped by where it applies:
  *everywhere* (user scope) or *this folder only* (the local and project scopes of
  `/data/chat`). Switching one off runs `claude mcp remove` and keeps the definition
  in `/data/mcp-off.json`; switching it back on runs `claude mcp add-json` with what
  was kept, so nothing has to be typed again. The list shows the name, the transport
  and the command or address — never an `env`, a header or a URL's query string,
  which is where keys live. Servers provided by an installed plugin are not listed:
  they belong to the plugin. Add a server in the terminal with `claude mcp add`.
- **CLAUDE.md** — `~/.claude/CLAUDE.md`, the instructions read before every
  conversation here. A `CLAUDE.md` in a project folder adds to it rather than
  replacing it.
- **Config** — `~/.claude.json`, Claude Code's own file: the account, the MCP
  servers, and a record of every folder it has been run in. Validated as JSON, saved
  verbatim, and refused while a job is running, because the CLI writes it too. It
  holds credentials, and a bad edit can break the sign-in — which `claude` in the
  terminal can put back.
- **Updates** — the CLI's versions and the install, described below.

### On a phone

The same page. Below 860 pixels the readings and the five actions collapse into one
drawer behind a single button, the conversation's model, effort and mode fold behind
a chip that reads `opus · medium · manual`, and the token count appears — in short
form — only once the window is more than half used. What is left is the transcript
and the box you type into, which is what a phone is for.

The Home Assistant app shows this add-on's UI in a webview, so the phone gets the web
page. There is no separate app to install, and nothing here needs one.

## Managing skills

**Settings → Skills** lists every installed skill with its description, file count,
size and last change, and lets you:

- **install or replace** one by dropping a `.tar.gz` on the upload area;
- **download** one as a `.tar.gz`, so a copy exists outside the add-on;
- **delete** one, which takes two clicks: the first arms the button, the second
  removes the skill.

The archive must contain `SKILL.md` at its top level. A single wrapping folder is
unwrapped automatically, so both of these work:

```
skill.tar.gz → SKILL.md, references/, scripts/
skill.tar.gz → my-skill/SKILL.md, my-skill/references/
```

**The name is not asked for.** It comes from `name:` in the archive's `SKILL.md`,
and failing that from the wrapping folder — the skill already carries its name, so
typing a second one only invites a mismatch between what the UI lists and what
Claude Code loads. Uploading a name that already exists replaces it.

### How the CLI finds them

Skills land in `/data/home/.claude/skills/<name>/`. `HOME` is `/data/home`, so
that is `~/.claude/skills` — where Claude Code looks for personal skills. Nothing
needs configuring, and each job is a fresh CLI process, so a newly uploaded skill
is picked up by the next run.

One detail this relies on: Claude Code only watches a skills directory that
existed when the process started, so `run.sh` creates it before anything else.

### Will an add-on update wipe them?

No. `/data` is a persistent volume that Home Assistant keeps across restarts and
add-on updates, and it is included in Home Assistant backups.

Uninstalling the add-on does remove the volume. If a skill exists only here, use
**download** to keep a copy.

## Configuration

| Option | Default | Notes |
| --- | --- | --- |
| `model` | `opus` | Default for jobs that do not name one. See below. |
| `effort` | `medium` | How long Claude thinks before answering: `low`, `medium`, `high`, `xhigh`, `max`. `medium` is the CLI's own default. |
| `permission_mode` | `manual` | What the agent may do unasked: `manual` asks, `plan` only plans, `acceptEdits` writes files, `auto` and `dontAsk` run tools freely. `bypassPermissions` is deliberately not offered. |
| `api_token` | empty | Required before the API accepts traffic from the network. See below. |
| `timeout_minutes` | `90` | A run past this is killed and the job marked failed. |
| `auto_update` | `true` | Install a newer CLI by itself. See below. |
| `update_channel` | `latest` | `latest` takes every release; `stable` runs about a week behind and skips releases with major regressions. |

`model`, `effort` and `permission_mode` are **defaults**, not limits: the console
and the API both override them per message. A value outside the documented set is
refused with `400` rather than passed to the CLI to fail there.

A mode your plan does not include fails when it is used, and the failure is
reported on that turn. The add-on cannot grey it out in advance: what the CLI
reports at the start of a session is the mode in force and the protocol features
available, not the account's entitlements.

### Choosing a model

The option is a list of the four aliases the CLI accepts:

| Alias | Tier |
| --- | --- |
| `opus` | most capable |
| `sonnet` | cheaper and faster |
| `haiku` | cheapest and fastest |
| `fable` | most expensive |

Aliases rather than pinned ids on purpose: each always points at the newest model
of its tier, so the list cannot go stale.

**This is only the default.** Any job can override it, which is the point when a
task is simple enough that the cheapest tier will do:

```json
{"prompt": "Rename these files consistently.", "model": "haiku", "start": true}
```

The per-job field is a free string, so a full name such as `claude-sonnet-5` works
there too when a specific version matters. In the web UI the model box offers the
aliases as a dropdown and still accepts a typed full name; leaving it empty uses
the add-on's default.

### Keeping the CLI up to date

**Settings → Updates** is where this lives: the installed version, what the channel
currently offers, where the binary actually resolved to, what the last install did,
and the two buttons — *check now*, which is the only thing here that reaches the
network, and *Update CLI*. A version waiting to be installed is announced in the
masthead, so it is not something you have to go looking for. With `auto_update` on,
the add-on also checks at start and once a day and installs the newest release on
the chosen channel.

Both paths run `claude install <channel>`, which writes the binary to
`~/.local/bin/claude` — inside `/data`, so **an update outlives a restart**. That
directory comes first on `PATH`, ahead of the copy baked into the image, which
stays as a fallback for a fresh install that has not downloaded anything yet. Check
which one is live with `GET /version`, whose `binary` field gives the resolved
path. An update installed into the image layer instead would silently disappear on
the next restart, which is why the ordering matters.

An install takes roughly half a minute. It runs in the background and its progress
is written to `/data/update.json`, so:

- closing or reloading the page loses nothing — the banner and the button are
  rendered from the server's state, not from the tab that started it;
- if the add-on restarts mid-install, the run is reported as `interrupted` rather
  than being left as `running` forever;
- the result of the last update stays visible until the next one.

Updating is refused while a job is running, so the binary is never swapped out from
under a run in progress. The automatic check simply tries again on its next pass.

### Reaching the API from another add-on

With `api_token` empty the API binds to localhost only. The web UI still works —
nginx reaches it internally and supplies the token, so the browser never holds
one — but nothing outside the container can connect.

To let another add-on call it:

1. **Invent the token yourself** and put it in the `api_token` option. Nothing
   issues it: it is not a Home Assistant access token and not a Claude API key,
   just a shared secret between the caller and this add-on. Generate one with
   `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`, or use any
   long random string.
2. Save the configuration and restart the add-on.
3. Map port `7682` in the add-on's **Network** section.
4. Call `http://<home-assistant-ip>:7682/` with
   `Authorization: Bearer <the same value>`.

Wherever `$TOKEN` appears below, it is that value.

A note on addressing by hostname instead: add-ons installed from a repository get
`{hashed-repo-id}_claude_code`, not `local_claude_code`, so the name is not
predictable. The published port is the reliable route.

## API

Every route except `GET /ping` needs `Authorization: Bearer <api_token>` once a
token is set. Requests must carry a `Content-Length`; chunked bodies are rejected
with `411` rather than silently arriving empty.

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/ping` | Liveness only, no token, no detail — for a watchdog |
| `GET` | `/health` | Version, sign-in state, skill count, queue depth, update state |
| `GET` | `/version` | Installed and available versions, and the resolved binary path |
| `GET` | `/update` | Progress or result of the most recent update |
| `POST` | `/update?target=` | Start an update. `target` is `latest`, `stable` or a version; defaults to `update_channel`. Returns `202` at once |
| `GET` | `/settings` | `~/.claude/settings.json`, its path, and the keys the add-on enforces |
| `PUT` | `/settings` | Replace it. The body is the raw file; invalid JSON is refused with `400` |
| `GET` | `/files` | The files this add-on will edit: `config` and `memory` |
| `GET` | `/files/<key>` | One of them, as text |
| `PUT` | `/files/<key>` | Replace it. The body is the raw file; `config` is checked as JSON and refused while a job runs |
| `GET` | `/mcp` | Configured MCP servers: name, scope, transport, command, and whether it is on |
| `POST` | `/mcp/<name>` | `{"enabled": true\|false}` — moves the server between the CLI's config and the add-on's store |
| `GET` | `/skills` | Installed skills with metadata |
| `POST` | `/skills?name=<name>` | Install or replace; body is the `.tar.gz`. `name` is optional — without it the name comes from `SKILL.md` |
| `GET` | `/skills/<name>/archive` | Download the skill as `.tar.gz` |
| `DELETE` | `/skills/<name>` | Remove a skill |
| `GET` | `/chat` | The conversation: its id and name, the transcript, the queue, a failed turn, notices, context left |
| `POST` | `/chat` | Send a message. Same body as a job, started at once |
| `GET` | `/chat/sessions` | Every conversation on disk, and which one is current |
| `POST` | `/chat/new` | Forget the current conversation, so the next message starts one |
| `POST` | `/chat/resume` | `{"session": …}` — continue that conversation |
| `POST` | `/chat/rename` | `{"session": …, "title": …}` — runs `/rename` |
| `DELETE` | `/chat/sessions/<id>` | Delete a conversation — its transcript, which is the only place it lives |
| `POST` | `/chat/compact` | Runs `/compact` on the current conversation |
| `POST` | `/jobs` | `{"prompt": …, "model": …, "start": true}` |
| `PUT` | `/jobs/<id>/files/<name>` | Upload an input file; body is the raw file |
| `POST` | `/jobs/<id>/start` | Queue a job created without `start` |
| `POST` | `/jobs/<id>/cancel` | Stop a run, or drop it from the queue |
| `POST` | `/jobs/<id>/pause` | Freeze the run and its subagents. Spends nothing, loses nothing, and the timeout stops counting |
| `POST` | `/jobs/<id>/resume` | Let it carry on |
| `GET` | `/usage` | How much of the plan's five-hour and weekly allowance is gone, when each resets, and whether that is `enough` to work on — each window against its own figure, `session_threshold` and `week_threshold` |
| `GET` | `/jobs/<id>` | Status, result or error, and produced files. While it runs, also `partial` and `activity` |
| `GET` | `/jobs/<id>/files/<path>` | Download a produced file |
| `DELETE` | `/jobs/<id>` | Delete the job and its files |
| `GET` | `/jobs` | All jobs, newest first |
| `PUT` | `/state/<key>` | Keep a JSON object of your own under a name you choose |
| `GET` | `/state/<key>` | Read it back exactly as it was left |
| `DELETE` | `/state/<key>` | Forget it |
| `GET` | `/state` | The keys in use |

`status` moves through `created` → `queued` → `running` → `done` or `failed`.
Poll `GET /jobs/<id>`; there is no callback. While a job runs, that answer also
carries `partial` — the reply as far as it has been written — and `activity`, the last
few tool calls with the file or command each was pointed at. That is what a caller
shows someone who is waiting: a long run is often quiet for minutes at a time.

A job may carry `effort`, `permission_mode`, `resume` and `source` alongside `prompt`
and `model`. `{job_dir}` and `{job_id}` in a prompt are replaced with the job's own
directory and id when it is created, which is how a prompt names the folder its
uploads are in. `source` says who sent the job: the console leaves it alone, and
anything else — a script driving the API, say — names itself, which keeps its
turns and its conversation out of the console's window. A chat job with a `source` of
its own and no `resume` starts a new conversation rather than continuing the one the
console is showing; the id comes back as `session_id` when the turn finishes, and
passing it as `resume` on the next turn is what makes a bot's exchange one
conversation. The chat routes are the same machinery with `chat: true` set, which is what
makes a message from the console and a job from another add-on queue behind one another instead
of running two CLI processes at once.

### The plan's allowance

With **Act on the limit** on — it is, by default — the add-on watches the account's own
usage figures and acts on them itself, because it owns the process and nothing else can
stop the CLI the moment a wall appears:

- a turn is **refused** with `429` when a window is already past its own figure, and the
  answer says which window, how full, and when it resets;
- a turn that runs into the wall is **frozen** where it stands — `SIGSTOP` to the whole
  process group, so every subagent stops with it. Nothing more is spent, nothing is lost,
  and the run's own timeout does not count while it waits. `GET /jobs/<id>` then carries
  `paused: true`, `paused_reason: "limits"` and `resumes_at`;
- once the window resets it **carries on by itself**, `SIGCONT`, from exactly where it
  stopped;
- `POST /jobs/<id>/resume` overrules the guard, and it stays overruled for that turn.

Each window is judged against its own figure — **Stop the five-hour window at** and
**Stop the weekly window at** — because the two are not the same question. The five-hour
window refills as the day goes on, so spending it to the brim costs an hour of waiting; the
week is what a fortnight of work has to fit inside, and a lower figure there keeps something
in reserve. Work stops when either is past its own figure, whichever that is. Each window in
`GET /usage` carries the `threshold` it is judged against, `thresholds` reports the pair, and
`worst` is the window with the least room left to its own figure — not the fuller one.

How often it looks depends on how close the wall is: a quarter of an hour away from it, two
minutes when nearly there. A reading that cannot be had never stops work — the endpoint is
undocumented, and its silence must not become a way to lose runs. Asked too often it
answers `429`, which is taken at its word: a quarter of an hour of silence, longer if it
asks for longer, and *refresh* cannot shorten it. The **Usage** tab in
Settings shows both windows, when each comes back, and reads them again on request.

The reading uses the sign-in from the terminal, and that access token lives about eight
hours. The CLI renews it while it runs; the add-on keeps it warm the rest of the time — a
token with less than two hours left is renewed, so a renewal happens every six hours or so
whether or not anybody is looking, and the sign-in never dies of silence. Whether it is time
is looked at no more often than `usage_check_seconds`, the same setting that paces
everything else: one number governs how often this add-on touches anything of Anthropic's. A
token that ran out anyway is also renewed before a reading, and once more if
the endpoint refuses one that looked good. Never while a turn is running: the CLI renews its
own then, and both at once would spend the same refresh token twice. When the sign-in really
has run out, the tab says so and the add-on stops asking for half an hour.

`/state` is a small place for a caller's own notes — a key you name, a JSON object you
own, up to half a megabyte, kept in `/data` and backed up with everything else. The
add-on never looks inside. It exists because a caller that drives long runs has state
of its own — who it is talking to, which job it is watching, what it has already sent —
and wherever it runs may not keep that honestly. An automation platform that hands each
run a copy of its stored data and writes the copy back when the run ends will undo a note
made by another run that started later — quietly, and only sometimes. Here a read returns
what is on disk and a write replaces it, both at once.

While a turn is in flight, `GET /chat` returns the words so far as `partial` on the
running entry in `pending`, and cuts the transcript back to the last thing the user
said. Claude Code writes its reply into the transcript as the turn runs, so serving
both would be the same words twice.

### Prompt only

```bash
curl -s -X POST http://homeassistant.local:7682/jobs \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"prompt": "Summarise what the installed skills do.", "start": true}'
```

### With input files

Upload the files before starting, then poll. The job directory is the CLI's
working directory, so the prompt can use relative paths.

```bash
ID=$(curl -s -X POST http://homeassistant.local:7682/jobs \
      -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
      -d '{"prompt": "Read the files in in/ and write the result into result/."}' \
     | jq -r .id)

curl -s -X PUT --data-binary @form.docx \
  -H "Authorization: Bearer $TOKEN" \
  "http://homeassistant.local:7682/jobs/$ID/files/form.docx"

curl -s -X POST -H "Authorization: Bearer $TOKEN" \
  "http://homeassistant.local:7682/jobs/$ID/start"

curl -s -H "Authorization: Bearer $TOKEN" \
  "http://homeassistant.local:7682/jobs/$ID" | jq '{status, error, files}'
```

Every file the run produced is listed in `files`, excluding the uploads and the
add-on's own bookkeeping.

## Security

**Treat an installed skill and a job input as code you are choosing to run.** A
job runs as root in this container with `Bash` pre-approved and unrestricted
outbound network, and `/data` holds both the Claude account's OAuth credentials
(`/data/home/.claude/.credentials.json`) and your `api_token`
(`/data/options.json`). A malicious skill, or a prompt-injecting document handed in
as a job input, can read those and send them anywhere. `--allowedTools` and
`--permission-mode` shape what the agent does by default; they are **not a
security boundary**.

The terminal is an unauthenticated root shell, reachable only through Home
Assistant ingress, which is restricted to the Supervisor's address. The API is off
the network entirely until `api_token` is set, and the token must be at least 16
characters from `A-Z a-z 0-9 . _ ~ -` — other characters are rejected at startup
rather than silently mangled.

**Ingress is not admin-only.** Home Assistant deliberately lets non-admin users
open an add-on's ingress UI, because an add-on can set `panel_admin: false`;
`panel_admin: true` only hides the sidebar entry. So *any* authenticated Home
Assistant user — including a dashboard-only or guest account — can reach this
add-on's terminal and job API. Treat access to this add-on as equivalent to
handing out administrator rights, and do not run it on an instance where you give
accounts to people you would not trust with the box.

Backups include `/data`, so the account credentials end up inside them in the
clear unless the backup itself is password-protected. **Skills are backed up whole**
— every file you uploaded, so if a skill carries reference material, that material
is in the backup too. The single exclusion is `home/.local`, the CLI's own ~230 MB
copy, which is re-downloaded after a restore.

## Things worth knowing

- **Claude Code wants 4 GB+ RAM**, and a large skill spawns subagents on top of
  that. Check **Settings → System → Hardware** first.
- **One CLI process at a time**, job or update. A job queued during an install
  waits for it, and an update is refused while a job runs. These runs are long and
  memory-hungry, and this shares a box with Home Assistant.
- **The job API exists because the run is long.** Driving the Claude API directly
  would mean reimplementing agentic tool use and skill discovery, and paying
  metered API credits instead of using the CLI's subscription sign-in. A single
  non-streaming Messages request also tops out around ten minutes — streaming
  lifts that, so it is the smaller of the two reasons.
- **`claude.log` holds the CLI's stderr** for each job and appears in the `files`
  listing alongside the outputs, so `GET /jobs/<id>/files/claude.log` fetches it.
  It is the first place to look when a job fails without a message. The console
  shows a failed turn's reason inline and deliberately has no file browser: files
  belong to the API jobs that produce them, and the terminal is a better tool for
  looking around `/data` than a list in a chat window would be.
- **Old jobs are pruned.** The newest 50 finished jobs are kept; older ones are
  deleted with their uploads and outputs when a new job is created.
- **Turn the add-on's Watchdog option on.** It defaults to off. With it on, Home
  Assistant restarts the add-on when the UI stops answering, and the container's
  own health check catches the case where one of the three processes has died
  while the others keep the container looking healthy.
- **`--bare` is deliberately not used.** Bare mode skips skill discovery and
  ignores the sign-in, requiring `ANTHROPIC_API_KEY` instead.
- **Alpine needs the distro ripgrep**, so `run.sh` writes `USE_BUILTIN_RIPGREP=0`
  into `~/.claude/settings.json` on first start. Later edits are left alone.
- **Failures come back readable.** The CLI reports problems in its stdout JSON
  rather than on stderr, so an unauthenticated run surfaces as
  `Not logged in · Please run /login` instead of an empty error.
- **The base image is pinned** in the `Dockerfile`. Since Supervisor 2026.04.0 no
  `BUILD_FROM` argument is provided, so there is no `build.yaml`.
