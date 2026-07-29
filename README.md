# Home Assistant add-on: Claude Code

A Home Assistant add-on repository containing one add-on.

## Requirements

Claude Code is a native binary that **requires the AVX instruction set**, so check
the machine hosting Home Assistant before installing:

```bash
grep -m1 -ow avx /proc/cpuinfo || echo "no AVX: Claude Code cannot run here"
```

No output means the add-on will install and start, but the binary will die with
`Illegal instruction` and there is no workaround. Low-power chips such as AMD
Bobcat (E-350, E-450) and the Intel Atom and Celeron parts of that era lack AVX
regardless of the machine's age. On a virtual machine it may just be hidden by a
generic guest CPU type — see
[claude-code/DOCS.md](claude-code/DOCS.md#hardware-requirements).

Also required: x86-64 or ARM64, 4 GB+ RAM, and a Claude account on a Pro, Max,
Team, or Enterprise plan.

## Installation

**Settings → Add-ons → Add-on Store → ⋮ → Repositories**, then add:

```
https://github.com/paradoxm/ha-addon-claude-code
```

## Add-ons

### [Claude Code](claude-code/)

Runs the Claude Code CLI in Home Assistant: a web UI for managing skills, a
terminal for signing in, and an HTTP API so another add-on can send prompts with
input files and collect the results.

See [claude-code/DOCS.md](claude-code/DOCS.md).
