#!/usr/bin/env bash
# Interactive shell handed to ttyd. Keeps HOME on the persistent volume so that
# `claude` finds the login and the skills symlink.
export HOME=/data/home
export TERM=xterm-256color
cd /share/claude 2>/dev/null || cd "${HOME}"
exec bash
