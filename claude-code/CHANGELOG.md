# Changelog

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
