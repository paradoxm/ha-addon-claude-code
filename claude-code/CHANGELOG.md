# Changelog

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
