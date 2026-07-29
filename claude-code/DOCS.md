# Claude Code

Runs the Claude Code CLI inside Home Assistant, with a web UI for managing skills
and an HTTP API for sending prompts and collecting the results.

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
| `model` | `opus` | Default for jobs that do not name one. See below. |
| `api_token` | empty | Required before the API accepts traffic from the network. See below. |
| `timeout_minutes` | `90` | A run past this is killed and the job marked failed. |

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
