# Claude Code

Runs the [Claude Code](https://code.claude.com/docs) CLI inside Home Assistant.

- **A web UI** in the sidebar to install, replace, download and delete skills, run
  a prompt, and collect whatever files the run produced.
- **A terminal**, for signing in once and for working by hand.
- **An HTTP API** so another add-on such as n8n can send a prompt with input
  files, poll the job, and download the results.

Everything is stored in the add-on's own `/data` volume, which survives restarts
and add-on updates. No Home Assistant folder is mapped, and no other add-on needs
filesystem access.

Requires a Claude account on a Pro, Max, Team, or Enterprise plan, and 4 GB+ RAM.

See [DOCS.md](DOCS.md) for installation, options, and the API reference.
