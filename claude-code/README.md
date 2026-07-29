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

## Before you install

Claude Code is a native binary that **requires the AVX instruction set**. Without
it the binary dies immediately with `Illegal instruction`, and there is no
workaround. Check the machine that hosts Home Assistant:

```bash
grep -m1 -ow avx /proc/cpuinfo || echo "no AVX: Claude Code cannot run here"
```

Low-power chips such as AMD Bobcat (E-350, E-450) and the Intel Atom and Celeron
parts of that era lack AVX no matter how new the machine looks, and they are
common in small Home Assistant boxes. On a virtual machine, AVX may simply be
hidden by a generic guest CPU type, which is fixable — see
[DOCS.md](DOCS.md#hardware-requirements).

Also required: a Claude account on a Pro, Max, Team, or Enterprise plan, and
4 GB+ RAM.

See [DOCS.md](DOCS.md) for installation, options, and the API reference.
