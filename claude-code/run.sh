#!/usr/bin/with-contenv bashio
# shellcheck shell=bash
set -e

# Options are read from the file the Supervisor writes rather than through
# bashio::config, which goes over the Supervisor API: a slow or unavailable API
# there yields empty strings, and an empty value would be acted on silently.
OPTIONS=/data/options.json

API_TOKEN="$(jq -r '.api_token // empty' "${OPTIONS}")"

# /data is the add-on's own persistent volume. It survives restarts and add-on
# updates, so the login and the installed skills stay put.
export HOME=/data/home
mkdir -p "${HOME}/.claude/skills" /data/jobs

# On musl the bundled ripgrep does not run, so point Claude Code at the distro
# package. Written only once, so later edits to the file are preserved.
if [ ! -f "${HOME}/.claude/settings.json" ]; then
    cat > "${HOME}/.claude/settings.json" <<'EOF'
{
  "env": {
    "USE_BUILTIN_RIPGREP": "0"
  }
}
EOF
fi

# Ingress traffic arrives from the Supervisor. Its address is resolved rather
# than hardcoded: a wrong literal would answer every ingress request with 403.
# `|| true` is load-bearing: getent exits 2 when the name is unknown, and bashio
# runs with `set -o pipefail`, so without it the add-on would die here silently.
SUPERVISOR_IP="$(getent hosts supervisor 2>/dev/null | awk '{print $1; exit}')" || true
if [ -n "${SUPERVISOR_IP}" ]; then
    ALLOW="allow ${SUPERVISOR_IP}; deny all;"
    bashio::log.info "Ingress restricted to the Supervisor at ${SUPERVISOR_IP}"
else
    # Better an open internal port than a UI that cannot be opened at all.
    ALLOW="# supervisor address could not be resolved; not restricting by IP"
    bashio::log.warning "Could not resolve 'supervisor'; the ingress port is not IP-restricted"
fi

# The web UI talks to the API through nginx, which supplies the token so the
# browser never holds it.
sed -e "s|__API_TOKEN__|${API_TOKEN}|" \
    -e "s|__ALLOW__|${ALLOW}|" \
    /etc/nginx/nginx.conf.template > /etc/nginx/nginx.conf

bashio::log.info "Claude Code $(claude --version 2>/dev/null || echo 'version unavailable')"

if [ ! -f "${HOME}/.claude/.credentials.json" ]; then
    bashio::log.warning "Not logged in yet. Open the Web UI, go to Terminal, and run: claude"
fi

if [ -z "${API_TOKEN}" ]; then
    bashio::log.warning "No api_token set: the API listens on localhost only and is reachable through the web UI, but not from other add-ons."
else
    bashio::log.info "API token set: the API also listens on port 7682 for other add-ons."
fi

python3 /api.py &
ttyd --port 7681 --interface 127.0.0.1 --base-path /terminal --writable /shell.sh &

bashio::log.info "Web UI on ingress"
exec nginx -g 'daemon off;'
