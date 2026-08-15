# Claude Code

Runs the [Claude Code](https://code.claude.com/docs) CLI inside Home Assistant.

![The console](../docs/console.png)

- **A console** in the sidebar: a conversation with Claude, streamed as it is
  written, with a queue, cancellation, your earlier conversations, and how much of
  the context window is left. It runs against Claude Code's own sessions, so a
  conversation continues in the terminal and back again.
- **Skills**, installed, replaced, downloaded and deleted from the settings sheet,
  and the permissions file editable there too.
- **Self-updating**, if you want it: the add-on checks for a newer CLI and installs
  it, into the persistent volume so the update outlives a restart.
- **A terminal**, for signing in once and for working by hand.
- **An HTTP API** so anything else in the house — another add-on, an automation,
  a script on the LAN — can send a prompt with input files, watch the turn, and
  download the results. Claude Code is a terminal program; this is what makes it
  something a machine can drive.
- **A guard on the plan's allowance**: a turn is refused when a window is already
  spent and frozen where it stands when it runs into the wall, then carries on by
  itself once the window resets. The five-hour and the weekly window have separate
  figures.

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
