#!/usr/bin/env bash
# Interactive shell handed to ttyd. Keeps HOME on the persistent volume so that
# `claude` finds the login and the installed skills.
export HOME=/data/home
export TERM=xterm-256color

# Same order as run.sh: an update installed into ~/.local/bin takes precedence
# over the packaged copy, so the terminal runs the version the add-on reports.
export PATH="${HOME}/.local/bin:${PATH}"

cd "${HOME}"
exec bash
