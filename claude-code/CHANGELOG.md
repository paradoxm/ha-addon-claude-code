# Changelog

## 1.20.0

- **`PATCH /state/<key>` changes part of a note and leaves the rest alone.** A caller
  keeping a record per conversation under one key had to read the whole note, change its
  own part and write all of it back — and between its read and its write another of its own
  runs may have written, so that change went back to what it was minutes ago. It is what
  cost one bot a pair's afternoon twice over. The body is laid over what is stored, as deep
  as it goes: what is not mentioned is left alone, `null` takes a field out, a list or a
  number replaces what was there. The read and the write happen under one lock, so two
  callers changing two different records in the same instant both keep their change, and
  the answer carries the note as it now stands.

## 1.19.0

- **A turn says which conversation it is in from its first seconds.** `session_id` used to
  be written into the job record from the CLI's final report, so a caller driving the API
  could not know it until the turn ended — and a turn that ended without a report, a
  timeout or an add-on restart, never said at all. Its conversation could not be carried
  on, and the work in it was lost. The id is taken from the stream the CLI writes as it
  goes, where it appears before any work is done.
- **A caller can name the conversation it opens.** A new `title` on `POST /jobs`, applied
  while the first turn is still running. Every conversation a skill opens is otherwise
  listed under the skill's own opening lines — the same ones for all of them — so a long
  run could not be told from another until it came back. The name is written where
  `/rename` writes it and costs no turn, so it does not queue behind the run it is naming.
  Ignored by a turn that resumes a conversation, which is named already.

## 1.18.2

- **`limit_threshold` is gone.** The single figure both windows used to share was kept for
  one version so that an add-on already running would not lose the number it was set to.
  Both windows have had their own figure since 1.18.0, so the old one only sat in the
  configuration page inviting the question of whether it still did anything. It does not:
  **clear it and save before updating**, because Home Assistant checks the saved options
  against the schema and an option the schema no longer knows will hold the add-on back.
  Neither window's default has changed — 90 per cent when a figure is unset.

## 1.18.1

- **The Watchdog reads the container's own health now.** Home Assistant calls the
  `watchdog:` URL obsolete and takes its answer from the image's `HEALTHCHECK`, which
  was already there — so that check picked up the half it was missing: its first
  request goes through nginx instead of straight at the API, which is what the
  Supervisor used to probe from outside. nginx, the API and the terminal, every thirty
  seconds. `startup`, `boot` and `ingress_port` are gone from the options as well; each
  only repeated Home Assistant's own default.
- A `cd` in the terminal script that could fail silently now exits instead.

## 1.18.0

- **A figure for each window.** *Stop work at* was one number for both, which meant being
  strict about the wrong one: the five-hour window refills as the day goes on, so spending
  it to the brim costs an hour of waiting, while the week is what a fortnight of work has to
  fit inside. **Stop the five-hour window at** (`session_threshold`) and **Stop the weekly
  window at** (`week_threshold`) are set separately, and work stops when either is past its
  own figure. `limit_threshold` is still read when neither is set, so an add-on that has
  been running keeps the number it was given.
- Which window bites first is now the one with the **least room left to its own figure**,
  not the fuller one — a week at 70% of 75 stops work before a session at 80% of 95. The
  watcher's ladder counts that same room, so it looks closely when a window is close to
  *its* wall rather than close to a hundred. `GET /usage` carries the figure inside each
  window and the pair in `thresholds`; the Usage tab marks each bar at its own.
- **A turn can be stopped the moment it is reported running.** «running» is written when the
  worker takes the job and the CLI appears a beat later — a beat that got longer when the
  spawn began waiting on the credentials lock. A freeze in that window was refused as "only
  a running turn can be frozen", and a cancel left the CLI to run on; both now wait out the
  spawn. The tests were failing about one run in two on this, always somewhere else.
- The README now shows the console, and says what the HTTP API is for and how the allowance
  is guarded.

## 1.17.7

- **The words of a long turn come back one message to a line.** A turn that ran for two
  hours reported its progress every couple of minutes, and `partial` handed all of it back
  as a single 2400-character line — so a caller showing what is happening could not tell the
  newest sentence from the first, and showed a placeholder for two hours instead. A finished
  message and the one still being written are now separate lines, in order, subagents left
  out as before.

## 1.17.6

- **Every time in the console is on Home Assistant's clock.** The add-on stamps its records
  in UTC, and the page deleted the `+00:00` along with the microseconds — so each message,
  job and skill read five hours early here, with nothing to say it was another country's
  time. Messages, the history list, the update story and the skills tab now show the hour
  this house keeps; the *Last read* line in the Usage tab too. The «UTC» labels beside the
  update times are gone, because they would now be lies.

## 1.17.5

- **Keeping the sign-in warm keeps to the pace of the setting.** It was hung on the watcher's
  half-minute tick, which is there to notice a turn starting and ending and touches nothing
  outside the container — but nothing riding on that tick should run at that pace. Whether the
  token is near its end is now looked at no more often than `usage_check_seconds`, so one
  number still governs every rhythm this add-on keeps. The renewal itself was, and remains, a
  request every six hours or so.

## 1.17.4

- **The sign-in is kept alive, not just repaired.** A token with less than two hours left is
  renewed on the watcher's own tick, so a renewal happens every six hours or so whether or
  not anybody is looking at the Usage tab. Before, the renewal waited for somebody to look —
  which meant the sign-in still died every night and was resurrected on the next reading.
  Now it never runs out, and the first turn after a quiet week does not wait on a renewal it
  could have had for nothing. It costs a file and a clock reading on each tick. A renewal
  that fails is not tried again for half an hour, and one is never attempted while a turn is
  running.

## 1.17.3

- **The sign-in behind the allowance renews itself.** An access token lives about eight
  hours; the CLI renews it from the refresh token beside it, but only while the CLI runs —
  so an add-on left alone overnight woke with a spent token and read `HTTP 401` from then
  on, with the guard blind behind it. The renewal now happens here too: before a reading if
  the token has run out, and once more if the endpoint refuses one that looked good. Only
  ever while no turn is running, because the CLI renews its own and two renewals spend the
  same refresh token twice. The rest of the credentials file — every MCP server keeps its
  tokens there — is written back untouched, and readable by nobody else.
- **A sign-in that really has run out says so**, instead of `HTTP 401`, and no request is
  spent on a token already known to be dead.

## 1.17.2

- **When the usage endpoint says «asked too often», the add-on believes it.** A `429` now
  buys a quarter of an hour of silence — longer if it sends a `Retry-After` — and *refresh*
  cannot shorten it, since pressing that button is how the wall gets hit. The Usage tab says
  when it will try again instead of sending anybody to the terminal to sign in for nothing.
  Nothing is held back meanwhile: a reading that cannot be had never stops work.

## 1.17.1

- **Usage is the first tab, and the one Settings opens on.** What is left of the allowance
  is what gets looked at; skills change once a month.
- **Rename and delete are icon buttons of one size and shape** — a pencil and a bin, the
  height of a line of text, dim until the row is wanted. Delete still asks: the bin becomes
  a red tick, and only the second press does it. Before, *DELETE* was a full-height block
  of uppercase beside a title nobody could read past it.
- **A failing interface test says so instead of hanging.** One left its page open when it
  threw, and a jsdom window holds the runner's process open — so a plain failure looked like
  a suite that never finished. The runner is told to exit when the tests are done.

## 1.17.0

- **A conversation can be deleted.** *delete* beside each one in the history list, and it
  asks first: the button becomes «✓ sure?» and only the second press does it — there is no
  undo, and the transcript is the whole conversation. Refused while a turn of that
  conversation is running, since that is the file the CLI has open. `DELETE
  /chat/sessions/<id>`.

## 1.16.3

- **A refusal reads like a sentence, not a timestamp.** «…it resets at
  `2026-08-13T13:20:00.124379+00:00`» went to whoever tried to send a message; it now says
  «it resets 13 Aug, 18:20», on the clock this machine is set to. `GET /usage` reports that
  `timezone` too, so a caller showing these times keeps no copy of where the house is.
- **A message that was made and never started can be thrown away.** Refusing to delete one
  left it in the console's waiting list for good — nothing prunes a job that never ran, and
  *remove* answered 409. Only a queued or running job is refused now.
- **A long waiting message cannot take the page with it.** The queue showed the whole
  prompt; a skill's, at dozens of lines, pushed the composer off the screen and stopped the
  page scrolling. Three lines each, and a ceiling on the block.

## 1.16.2

- **Times are on Home Assistant's clock, and read like times.** The reset times in the
  Usage tab were formatted by whatever the browser guessed and printed as `13.08, 18:20`;
  they now say `13 Aug, 18:19` in the timezone Home Assistant is set to — handed to every
  add-on in `TZ`, and asked of the Supervisor if that is missing. `GET /health` reports it,
  so anything else driving the add-on can use the same clock.

## 1.16.1

- **How often the allowance is read is a setting now** — `usage_check_seconds`, 180 by
  default — and one number governs every reading: the answer is cached for that long, the
  watcher looks no more often even when it is close to the figure, and the console asks no
  more often either. A page nobody is looking at asks nothing at all. Every reading is a
  request to Anthropic, and this is the only place that decides how many there are.
- **Why nothing is running, said on the page you are already looking at**: a red line above
  the conversation whenever the allowance is spent and the add-on is acting on it — which
  window, how full, what it stops at, when it comes back. It says nothing when the add-on
  is only reporting, because then nothing is held.
- **The two new options are optional, so the configuration page can be saved again.** An
  add-on that has been running for a while keeps the options it was saved with, and a new
  *required* option that is not in that file yet makes Home Assistant refuse the whole
  form — including the edit that would have added it. Unset now means what the code
  already did: stop at 90 per cent, and act on it.
- Green, amber past 70 per cent, red past 90 in the **Usage** tab — and red as well for the
  window that is holding work up, whatever figure that happens to be at, with a line saying
  so. The figure the add-on stops at is marked on the bar itself, so how much room is left
  reads at a glance rather than out of a sentence underneath.
- **Tests, after a review that deleted code to see whether they would notice.** Three would
  not have: the watcher could stop calling the guard, the freeze could stop sending signals,
  and the guard's own thaw could go back to marking the turn as overruled — all with a green
  suite. Now a spy watches the call, the kernel is asked whether the process really stopped
  (`T` in `/proc`), and a run's grandchild is checked to have stopped with it, which is the
  whole reason the signal goes to the process group.

## 1.16.0

- **The add-on now acts on the plan's allowance, instead of only reporting it.** A turn is
  refused with `429` when the window is already spent, and a turn that runs into the wall
  is frozen where it stands — `SIGSTOP` to the whole process group, so the subagents stop
  with it — then carries on by itself, from the same place, once the window resets. It
  happens whether or not anybody is watching, which is the point: the caller that was
  supposed to watch is exactly what failed. `GET /jobs/<id>` carries `paused_reason` and
  `resumes_at` while a turn waits, `POST /jobs/<id>/resume` overrules the guard for that
  turn, and **Act on the limit** turns the whole thing off.
- **How often it looks depends on how close the wall is** — a quarter of an hour of room
  away, two minutes when nearly there. A reading costs a request to Anthropic, and asking
  every minute through a half-hour run is both wasteful and rude.
- **A Usage tab in Settings**: both windows with what is used and when each comes back, the
  figure work stops at, and a button to read it again. The same numbers the guard acts on.
  A frozen turn now reads as *paused* in the console, with *let it go* beside it — before,
  it looked like an ordinary working turn and the only button offered was the one that
  throws the work away.
- **A window that will not be back for hours is not worth freezing for.** Past six hours —
  a weekly window resetting in six days, say — the turn is left to run into the wall
  instead, because a frozen process holds the one CLI, and with it every job behind it.
- **Fixed, all found while reviewing the above:** the pause flag outlived the process it
  belonged to, which defeated the *next* turn's timeout and stopped the guard from ever
  freezing again; a frozen turn stayed frozen for good if the reading went quiet while it
  waited (it is let go once its window was due back); a turn refused at the wall left a
  job behind that the console showed as queued for ever and nothing could delete; the
  guard and the turn wrote the same record from two threads, so one silently undid the
  other; and an answer from the usage endpoint in a shape it has never used would have
  stopped every job from starting rather than only the reading.

## 1.15.1

- **`limit_threshold` goes down to 10 per cent, and has a name in the UI.** The floor was
  50, which refused any value a person might want for a test — or for keeping most of the
  week for something else — and the option showed as a bare key.

## 1.15.0

- **How full is too full is now a setting.** `limit_threshold`, 90 per cent by default,
  and `GET /usage` reports it alongside `enough` — the answer to the only question a
  caller has: may work start, or carry on? The add-on still does not act on it itself;
  what it does is stop every caller from carrying its own copy of the number. Lower it if
  a long run keeps hitting the wall; raise it to use more of the window.

## 1.14.0

- **Somewhere for a caller to keep its own notes.** `PUT`, `GET` and `DELETE`
  `/state/<key>` store a JSON object under a name of your choosing, and `GET /state`
  lists the keys in use. Half a megabyte each, in `/data`, backed up with everything
  else; the add-on never looks inside. It is here because a caller running half-hour
  jobs has state of its own — which job it is watching, what it has already delivered —
  and its own platform may hand every execution a stale copy of that state and write it
  back at the end. This one reads from disk and replaces on write.

## 1.13.1

- **The context readout was reading the wrong number.** It took the size of the window
  in use from the turn's totals, which add up every request the turn made — so a long
  turn reported more tokens than the window holds, and the console said `1,118,758 of
  1,000,000 used, 0% free` while the CLI itself said 37%. What fills the window is the
  last request, and that is what is read now; a subagent's requests are left out of it,
  since they read their own context rather than this conversation's.

## 1.13.0

- **How much of the plan's allowance is left.** `GET /usage` reports the five-hour
  window and the week — a percentage each and when each resets — read with the
  account's own credentials, the same figures its other clients draw. Undocumented, so
  it is best-effort by design: no sign-in, an expired token or a changed answer all
  report `available: false` rather than becoming a new way for a run to fail. A caller
  can now hold a long run back before a limit stops it halfway.
- **A turn can be frozen and let go again**: `POST /jobs/<id>/pause` and `/resume`
  signal the whole process group, so the subagents freeze with it. Nothing is lost and
  nothing more is spent, and the run's own timeout stops counting while it is stopped.
  A frozen turn can still be cancelled — it is let go first, or the stop would sit
  unanswered.
- **`?refresh` works.** A flag with no value was being dropped before it was read, so
  *check now* in the updates tab quietly returned the cached answer. It re-reads now.

## 1.12.1

- **Another caller no longer lands in the middle of your conversation.** A job sent
  with a `source` of its own and no `resume` used to fall back to whatever
  conversation this window was showing, so a bot's first message joined it. It gets a
  conversation of its own now, and only the console's turns continue the console's.

## 1.12.0

Everything here is what a bot driving the add-on over the API turned out to need.

- **A running job says what it is doing.** `GET /jobs/<id>` now carries `partial` —
  the reply so far — and `activity`, the last few tool calls with what each was
  pointed at. A half-hour run can go minutes without producing a word, and a caller
  polling it had nothing to show for the wait.
- **A prompt can name the job's own directory** with `{job_dir}` and `{job_id}`,
  which the add-on fills in when the job is created. A prompt is written before the
  job exists, so "the uploads are in there, put the archive next to them" had nothing
  to name.
- **A job can say where it came from**: `source`. The console leaves it alone; another
  caller names itself, and the console then leaves that caller's conversation alone —
  its turns no longer appear in this window, and it no longer decides which
  conversation the window is showing.
- **A downloaded file keeps its name**, whatever alphabet it is in. The
  `Content-Disposition` header carries the RFC 5987 form alongside the ASCII one; a
  file called `Вариант 1 — Рука.docx` used to arrive as a row of underscores.

## 1.11.0

- **An MCP tab.** Every configured server with a switch, grouped by where it
  applies — everywhere, or only in the folder the conversation runs in. Switching one
  off lifts it out of Claude Code's configuration and keeps its definition here,
  secrets included, so switching it back on restores it exactly as it was rather than
  asking for the command again. The list never returns an `env`, a header or a URL's
  query string: what a server is, not what it authenticates with.
- **A CLAUDE.md tab**, for the instructions Claude Code reads before every
  conversation in this add-on.
- **A Config tab** showing `~/.claude.json` with validation and formatting. Saving is
  refused while Claude is working, since the CLI writes that file itself.
- **Warnings go away once read.** The count is of unread ones and the button is not
  there at all when there are none; inside, the ones you had not seen are marked in
  amber.
- The message box no longer spills out of a short window or pushes its placeholder
  off the side of a narrow one, and it has lost the drag handle: the shell decides
  its height.

## 1.10.0

- **The phone gets a drawer instead of debris.** The readings and the actions
  collapse into one card hung from the button that opens it, with an amber segment
  joining the two: five readings as an instrument list, four actions as touch-sized
  rows. The magnifier is a named row there, since an icon on its own says nothing in
  a list.
- **The conversation's three controls fold behind a chip** reading
  `opus · medium · manual`, which is worth more than three rows of selects on a
  screen that narrow. Tapping it opens them.
- **The token count stays out of the way on a phone**, appearing in short form only
  once the window is more than half used — the point at which compact turns up
  beside it.
- **Scrolling up to re-read something is no longer undone.** The view followed the
  text for as long as a turn ran, whatever the reader was doing. Now it follows only
  while you are at the end, and a strip under the transcript — never over it —
  offers the way back.

## 1.9.2

- **A file dropped on the upload area is checked before it is sent.** The `accept`
  attribute only filters the file picker, so anything dragged in went up in full and
  came back as a 400 from the server.
- The prompt is handed to the CLI with one call instead of three, and the version
  cache, the plugin report and the session id a transcript is read by are typed as
  what they actually are — found by putting mypy on the file.

## 1.9.1

- **The queue reading in the header works.** It shared a key with the chat's queue
  block in the script's element table, so the number was written into the wrong
  element and the reading stayed at "—" forever. Found by putting a linter on the
  UI, not by looking.
- The whole group of readings and buttons is what collapses on a narrow screen now,
  and the panel hangs from the button that opens it rather than from the bottom of
  the masthead, which left it floating mid-page.
- Nothing the UI script declares lands on `window` any more.

## 1.9.0

- **"No response requested." is gone from the chat.** The CLI answers a slash
  command with a message it wrote itself, marked `<synthetic>`, and renaming a
  conversation left one of those behind every time. Those are filtered now; the API
  errors among them — the one synthetic message worth reading — are kept as notices.
- **Updates moved into Settings**, on a tab of their own: the installed version,
  what the channel offers, which binary is actually running, what the last install
  did and what the CLI printed while doing it, plus *check now* and *Update CLI*. An
  install in progress is reported there and in the masthead. A version waiting to be
  installed is announced in the masthead, which is what the old amber button did.
- **The layout works on a phone.** Below 860 pixels the five actions collapse behind
  one button instead of wrapping into a wall, the readout reflows, and the
  conversation's name and controls take a line each. The rename field no longer
  reaches across the controls beside it and covers them on a narrow window.
- Development: the UI has tests. The real `index.html`, `style.css` and `app.js` are
  loaded into a DOM and driven the way a person drives them — Enter sends,
  Shift+Enter does not, deleting a skill takes two clicks, a warning shows a count
  and not a message. No framework and no build step were added to do it.

## 1.8.1

- **`/rename` no longer writes three lines of machinery into your chat.** Claude
  Code records a slash command, its caveat and its output as ordinary user
  messages, so renaming a conversation put `<command-name>/rename</command-name>`
  and friends in the window. Every tag the CLI wraps its own bookkeeping in is now
  filtered out, taken from what real transcripts contain rather than guessed at —
  injected reminders, the `!` bash mode and a subagent's notifications included.
- **Warnings have a place of their own.** A refused request, an unknown command or
  a tool call that failed is reported by the CLI beside the conversation rather
  than in it, and the add-on now shows them the same way: a count in the bar under
  the transcript, the detail behind one click. Nothing joins the message flow.
- **New chat is no longer undone by the turn that was still running.** Starting a
  fresh conversation while Claude was answering was reversed the moment that turn
  finished, because it pointed the chat back at the conversation it belonged to.

## 1.8.0

- **The reply no longer appears twice while it is being written.** Claude Code adds
  its answer to the transcript as the turn runs, so the same words arrived both from
  the transcript and from the live stream. The transcript is now cut back to the last
  thing you said while a turn is in flight, and the live text is served on its own.
- **The conversation's name is in the header**, and clicking it renames the
  conversation with `/rename` — the same command the history list uses, so the new
  name goes into Claude Code's own transcript.
- Timestamps carry microseconds. With second precision, two turns created in the
  same second sorted arbitrarily, which was enough for the chat to show the wrong
  turn's error and for pruning to delete the wrong jobs.
- **The documentation describes the add-on as it now is.** It still called the UI a
  page for managing skills, and its API reference was missing the conversation, the
  settings file and cancellation entirely. The `effort` and `permission_mode`
  options are documented, and so is the reason a mode your plan lacks cannot be
  greyed out before it is used.

## 1.7.0

- **The reply appears as it is written.** Runs now use
  `--output-format stream-json --include-partial-messages`, and the CLI's output is
  written straight to a file as it arrives, so the text can be read while the turn
  is still going. Thinking and tool-call deltas are skipped; a block caret marks
  where the words are landing. Verified against real CLI output: the parser pulls
  the reply out of the `text_delta` events, and the final `result` record still
  yields the session id, the usage and the context window exactly as before.
- **Cancel a turn.** A cancel button sits beside the working indicator, and Esc
  does the same thing when no sheet is open — the terminal's own gesture. It sends SIGTERM to
  the whole process group, which is what the CLI documents for an
  abort — it ends the turn, stops any Bash it started, and runs its SessionEnd
  hooks — then SIGKILL if it does not go within ten seconds. Whatever had already
  been said is kept. A message still waiting in the queue is dropped without
  starting. Until now a wrong or runaway run held both the chat and the API until
  the ninety-minute timeout.
- **Only the transcript scrolls.** The masthead and the composer stay put instead
  of the whole page moving. On a very short viewport the page scrolls as before,
  since a fixed shell would leave no room to read.

## 1.6.0

- **Your message no longer appears twice while Claude is answering.** The
  transcript comes from Claude Code, which records the message the moment the turn
  starts, and the add-on was drawing it a second time from the job. The running
  turn now contributes only Claude's side: a working indicator where the reply will
  appear.
- **Messages queue instead of being blocked.** Sending stays available while Claude
  is busy, exactly as it is in the terminal. Anything waiting sits in a dashed
  block below the transcript, marked as not yet seen, and is picked up in order —
  and can be removed while it waits.
  This needed a fix underneath: the conversation to continue is now resolved when a
  turn *runs* rather than when it is queued. A second message used to capture a
  session id that did not exist yet and would have started a separate conversation.
- **The skill name field is gone.** The name comes from `name:` in the archive's
  SKILL.md, which is what Claude Code itself goes by, so the two can no longer
  disagree. A wrapping folder name is the fallback, and `?name=` still overrides it
  for API callers.
- **Deleting a skill asks first**: one click arms the button and it shows a tick,
  the second click deletes.

## 1.5.0

- **Settings sheet with tabs.** Skills and the archive upload moved out of the side
  rail into it, so the conversation now has the page to itself.
- **Permissions tab**: an editor for Claude Code's own user settings, where
  permission rules live. It validates as you type — Save is out of reach while the
  JSON is broken rather than accepting it and failing at the server — checks that
  `permissions.allow`/`deny`/`ask` are lists of strings, and leaves every other key
  alone, since the schema is Claude Code's and not this add-on's. `format`
  reindents, `reload` discards.
  One thing it enforces: `USE_BUILTIN_RIPGREP=0` is reapplied on every save. The
  bundled ripgrep does not run on musl, so a hand-edit that dropped it would break
  search with no obvious cause.

- **Compact.** A button appears beside the context reading once the conversation is
  using more than half the window, and runs `/compact`. Verified that the command
  is accepted in headless mode; note that it summarises the *conversation*, so it
  cannot shrink the fixed overhead — system prompt, tool definitions, skill
  metadata — and on a short conversation the figure barely moves. That is why the
  button only appears past 50%.
- **Rename a conversation** from the history list. It calls `/rename`, so the new
  title is written into Claude Code's own transcript and is what every client shows
  from then on, rather than a label kept only here.
- Slash commands no longer appear in the transcript as though you had said them,
  and a command in flight reads as a note rather than as a message.
- Answering an earlier question: the modes a plan allows **cannot** be detected in
  advance. The CLI's session-init event reports the current mode and its protocol
  capabilities, but nothing about entitlements, so the selector offers all five and
  an unavailable one fails at use.

## 1.4.3

- The default permission mode is `manual`, the CLI's own default. It approves
  nothing by itself, which costs less here than it sounds: the add-on already
  pre-approves Bash, Read, Write, Edit, Glob and Grep with `--allowedTools`, and
  those apply whatever the mode. So ordinary work and a skill's own scripts still
  run unattended; what changes is that anything outside that list — a web request,
  an MCP tool — is refused rather than waited on. `auto`, which also permits those
  through a safety classifier, is one click away in the chat.

## 1.4.2

- The model control is a selector like effort and mode beside it. As a text input
  with a placeholder it read as unset even though a default was in force. The API
  still accepts a full model name, which an alias cannot express.
- A selector whose configured default is missing from its own list no longer
  silently shows the first entry instead — the value is added rather than dropped,
  and `default` from an older config is normalised to `manual` before it is sent.

## 1.4.1

- The history button is drawn as an SVG instead of the ⌕ character, which is
  missing from most monospace fonts and fell back to something small and
  misaligned. Its height is matched to the neighbouring buttons explicitly, since
  an icon button has no text to set it.

- The review-everything mode is listed as `manual`, which is what the CLI, the
  editor extensions and the apps all call it. `default` is its config value and the
  CLI accepts either; the API still takes `default` so an existing caller keeps
  working, but only one of the two names is offered, since a list showing both for
  one mode is worse than either alone.

## 1.4.0

The console becomes a conversation, built on Claude Code's own session handling
rather than a private store of my own.

- **Chat.** Your messages and Claude's replies in one transcript, Enter sends and
  Shift+Enter breaks the line. The transcript is read from Claude Code's own
  session file, so a conversation continued from the terminal appears here too.
- **New chat** starts a fresh conversation. `/clear` has no headless equivalent;
  a new conversation is simply one started without `--resume`.
- **History**, behind the magnifier, with a filter box that narrows as you type.
  Titles are the ones Claude generates for each session, and a title you set
  yourself takes precedence. Picking one resumes it.
- **Context left**, in percent, under the transcript. Both numbers come from the
  CLI's own report — `usage` for the prompt size and `modelUsage.contextWindow`
  for the limit — so no model's window is hardcoded.
- **Effort** and **permission mode** selectors beside the model. The lists are
  served by the add-on, so they cannot drift from what it accepts. Effort is
  `low` to `max`, default `medium` as in the CLI. Modes are `default`, `plan`,
  `acceptEdits`, `auto` and `dontAsk`; `bypassPermissions` is deliberately absent,
  since this container holds the account credentials.
- **Plugins follow the CLI.** After the CLI moves to a new version, marketplaces
  and every installed plugin are updated too — a plugin is built against a CLI
  version, and leaving them behind is how a working setup quietly rots.
- The version check now runs once a day rather than twice, since releases do not
  land more often than that.
- A failed message stays on screen with its reason. It never enters Claude Code's
  transcript, so without this both the message and the failure vanished.

## 1.3.4

- Fixes the empty banner that could not be closed. Making the banner closable gave
  it `display: flex`, and an author rule that sets `display` overrides the
  browser's own `[hidden] { display: none }` — author styles beat the user-agent
  sheet regardless of specificity. So the script hid it while CSS kept drawing an
  empty strip. `[hidden]` is now authoritative for every element.

## 1.3.3

- The update banner no longer sits there permanently with nothing to close it. A
  check that changed nothing is not news, so it gets no banner at all; the time of
  the last check moved to a tooltip on the `updates` readout.
- The banner that does appear — an installed update, a failure, an interrupted run
  — has a dismiss button, and dismissing is remembered per run, so it stays closed
  across reloads without hiding the next outcome.

## 1.3.2

- Skills are backed up whole, reference material included. Backup excludes are
  matched right-anchored against the full path, so the previous bare `jobs` and
  `.local` patterns would also have matched folders of those names *inside* a
  skill and dropped them from the backup without saying so. The jobs exclusion is
  gone — that scratch is tens of megabytes, not worth risking skill data — and the
  remaining one is `home/.local`, which can only match the CLI's own copy.

## 1.3.1

- Tell the browser to revalidate the web UI. The files carry no content hash, so a
  cached copy survived an add-on update and showed the previous interface while the
  API was already the new one — which looked exactly like the update not having
  applied. `Cache-Control: no-cache` plus the existing ETag makes each check a 304.
- The startup log no longer reports "is current" when a newer CLI exists and
  `auto_update` is off. It now names the available version and says the option is
  off.

## 1.3.0

Interface rework, plus the results of a review that ran the code rather than
reading it. Several of these were found by more than one reviewer independently.

**Interface**

- The prompt console is now the wide centre column and skills moved to a side
  rail: writing prompts is what this page is for.
- Skills are a collapsed accordion built on native `<details>`, so a skill whose
  description runs to a thousand characters no longer owns the page. One opens at
  a time, and prose is set in the serif rather than monospace.
- The file picker is reachable from the keyboard again — `hidden` had removed it
  from the tab order, leaving no keyboard path to it at all.
- Label contrast raised to 4.6:1 (was 3.09:1) and control borders to a
  perceivable ratio; `prefers-reduced-motion` now also covers the running-job
  pill, the longest-lived animation on the page.
- Banners announce themselves to screen readers, and **replace** says which skill
  it is about to overwrite.

**Correctness**

- A successful health poll no longer wipes an unrelated error message.
- One failed poll no longer abandons a job that is still running; polling is
  self-chaining, so a slow reply cannot repaint a finished job as running.
- A run in progress is picked back up when the page is reloaded.
- The queue readout counts the running job instead of reading zero throughout it.
- A non-JSON response is now an error rather than a plausible, invented readout.

**Robustness**

- One lock now covers the CLI, so an update cannot start beside a queued job and a
  job cannot start mid-install. Two mechanisms previously guarded one resource and
  neither covered the other direction.
- That lock can no longer leak: a failed state write or a thread that cannot start
  used to leave it held, which silently killed updates until a restart.
- State files are written atomically. A concurrent reader saw partial JSON in 40%
  of reads under contention, and a power cut left the file corrupt for good.
- A job directory without a readable `job.json` no longer breaks the whole job
  listing, and finished jobs beyond the newest 50 are pruned.
- A timeout now kills the CLI's whole process group; subagents used to survive and
  keep burning CPU beside Home Assistant.
- Two concurrent starts of the same job no longer run it twice.
- The first version probe after a boot works: the cache treated host uptime under
  60s as "recently checked" and reported the CLI as unavailable.
- Deleting a running job is refused rather than deleting the process's own
  working directory.

**Security**

- Download filenames are sanitised before reaching `Content-Disposition`; a job
  could create a file whose name injected response headers on the Home Assistant
  origin. The name check also no longer accepts a trailing newline.
- Skill archives are rejected if they expand beyond 512 MB or 20,000 entries. Only
  the compressed size was capped, and zero-fill gzips at about 1000:1.
- A negative `Content-Length` no longer bypasses the upload cap.
- `api_token` is validated at startup: characters that break the nginx templating
  used to either corrupt the token silently or stop the add-on from starting.
- The token comparison is constant-time, and the ingress restriction falls back to
  Home Assistant's internal subnet instead of failing open — that port serves a
  root terminal.
- The prompt is passed on stdin. `-p` is a boolean flag and the prompt is a
  positional, so a prompt beginning with `--` was parsed as an option.
- `SUPERVISOR_TOKEN` is no longer handed to the CLI's environment.
- Errors answered before the request body is read now close the connection, rather
  than leaving those bytes to be parsed as the next request.
- `nosniff` and a content-security policy on the UI; `/health` requires the token
  and a token-free `/ping` replaces it for liveness checks.
- DOCS.md said the container "reaches only its own `/data`" as reassurance. That
  is where the account credentials live, so the claim is now an explicit warning.

## 1.2.0

- Keeps the CLI up to date: checks at start and every twelve hours, and installs
  the newest release on the chosen channel. Turn it off with `auto_update`, and
  pick `latest` or `stable` with `update_channel`.
- **Update CLI** button in the web UI, which names the available version when
  there is one, next to a readout of the installed version.
- Updates are installed into `~/.local/bin`, which lives on the `/data` volume, and
  that directory now comes first on `PATH`. An update therefore survives a restart;
  installing over the packaged copy in the image layer would not have, since that
  layer is replaced. The packaged copy remains as the fallback.
- Update progress is kept in `/data/update.json` rather than in the browser, so
  reloading the page still shows a run in progress and the previous result. A run
  cut short by a restart is reported as `interrupted` instead of staying `running`.
- Updating is refused while a job is running, so the binary is not swapped out
  underneath it.
- The terminal no longer starts in `/share/claude`, a leftover from before the
  add-on stopped mapping any Home Assistant folder.

## 1.1.2

- Name the AVX requirement when the binary dies with `Illegal instruction`.
  Claude Code's native binary needs AVX, which pre-2013 processors lack and which
  a hypervisor may not pass through to the guest; the add-on now says so, prints
  the CPU model, and points at the guest CPU type, instead of leaving a bare
  SIGILL for the reader to decode.

## 1.1.1

- Report why the `claude` binary failed instead of logging a bare
  `version unavailable`. Startup discarded its stderr, which turned any failure
  into an unexplained line with nothing to act on; the log now carries the exit
  code, the machine architecture, and the binary's own output.

## 1.1.0

- The `model` option is now a dropdown of the four aliases the CLI accepts:
  `opus`, `sonnet`, `haiku`, `fable`. Aliases rather than pinned ids, so the list
  tracks the newest model of each tier instead of going stale.
- The web UI's model box offers the same aliases while still accepting a typed
  full name such as `claude-sonnet-5`, and shows the configured default when left
  empty.
- Clarified that `api_token` is a value you invent, not one issued by Home
  Assistant or Anthropic.

## 1.0.0

- First release.
- Claude Code installed from Anthropic's signed apk repository, with the published
  signing-key checksum verified at build time.
- Web UI behind Home Assistant ingress for installing, replacing, downloading and
  deleting skills, and for running a prompt.
- Web terminal (ttyd) for the one-time sign-in and for manual work.
- HTTP API for skills and jobs, with file upload and download, so another add-on
  can drive it. Off the network until `api_token` is set.
- Skills stored in `/data/home/.claude/skills`, which is `~/.claude/skills` for
  the CLI, so no configuration is needed for discovery.
- Runs one job at a time, and reconciles jobs left `running` by a restart.
