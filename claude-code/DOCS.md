# Claude Code

Runs the Claude Code CLI inside Home Assistant, with a web UI for managing skills
and an HTTP API for sending prompts and collecting the results.

Everything lives in the add-on's own `/data` volume. No Home Assistant folder is
mapped, and nothing else needs filesystem access.

## Install

1. **Settings → Add-ons → Add-on Store → ⋮ → Repositories**, and add
   `https://github.com/paradoxm/ha-addon-claude-code`.
2. Install *Claude Code*. The image is built on the Home Assistant machine, so the
   first install takes a few minutes.
3. Start it and open **Web UI**.
4. Click **Terminal**, run `claude`, and follow the prompts to sign in.

The sign-in is needed once. Requires a Claude account on a Pro, Max, Team, or
Enterprise plan.

## Managing skills

The web UI lists every installed skill with its description, file count, size and
last change, and lets you:

- **install or replace** one by dropping a `.tar.gz` on the upload area;
- **download** one as a `.tar.gz`, so a copy exists outside the add-on;
- **delete** one.

The archive must contain `SKILL.md` at its top level. A single wrapping folder is
unwrapped automatically, so both of these work:

```
skill.tar.gz → SKILL.md, references/, scripts/
skill.tar.gz → my-skill/SKILL.md, my-skill/references/
```

Uploading a name that already exists replaces it.

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
| `model` | `opus` | Default for jobs that do not name one. |
| `api_token` | empty | Required before the API accepts traffic from the network. See below. |
| `timeout_minutes` | `90` | A run past this is killed and the job marked failed. |

### Reaching the API from another add-on

With `api_token` empty the API binds to localhost only. The web UI still works —
nginx reaches it internally and supplies the token, so the browser never holds
one — but nothing outside the container can connect.

To let another add-on such as n8n call it:

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

`GET /health` needs no token. Everything else needs
`Authorization: Bearer <api_token>` once one is set.

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/health` | Version, sign-in state, skill count, queue depth |
| `GET` | `/skills` | Installed skills with metadata |
| `POST` | `/skills?name=<name>` | Install or replace; body is the `.tar.gz` |
| `GET` | `/skills/<name>/archive` | Download the skill as `.tar.gz` |
| `DELETE` | `/skills/<name>` | Remove a skill |
| `POST` | `/jobs` | `{"prompt": …, "model": …, "start": true}` |
| `PUT` | `/jobs/<id>/files/<name>` | Upload an input file; body is the raw file |
| `POST` | `/jobs/<id>/start` | Queue a job created without `start` |
| `GET` | `/jobs/<id>` | Status, result or error, and produced files |
| `GET` | `/jobs/<id>/files/<path>` | Download a produced file |
| `DELETE` | `/jobs/<id>` | Delete the job and its files |
| `GET` | `/jobs` | All jobs, newest first |

`status` moves through `created` → `queued` → `running` → `done` or `failed`.
Poll `GET /jobs/<id>`; there is no callback.

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

## Things worth knowing

- **Claude Code wants 4 GB+ RAM**, and a large skill spawns subagents on top of
  that. Check **Settings → System → Hardware** first.
- **One job at a time.** These runs are long and memory-hungry, and this shares a
  box with Home Assistant.
- **A long run needs no streaming here.** The CLI is a local process, so the job
  API sidesteps the roughly ten-minute ceiling on a single blocking HTTP request
  to the Claude API.
- **`Bash` is allowed** so skills can run their own scripts. That is broad by
  design; the container reaches only its own `/data`.
- **`--bare` is deliberately not used.** Bare mode skips skill discovery and
  ignores the sign-in, requiring `ANTHROPIC_API_KEY` instead.
- **Alpine needs the distro ripgrep**, so `run.sh` writes `USE_BUILTIN_RIPGREP=0`
  into `~/.claude/settings.json` on first start. Later edits are left alone.
- **Failures come back readable.** The CLI reports problems in its stdout JSON
  rather than on stderr, so an unauthenticated run surfaces as
  `Not logged in · Please run /login` instead of an empty error.
- **The base image is pinned** in the `Dockerfile`. Since Supervisor 2026.04.0 no
  `BUILD_FROM` argument is provided, so there is no `build.yaml`.
