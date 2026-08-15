# Pinned rather than :latest so a base image bump cannot change the build under us.
# Since Supervisor 2026.04.0 no BUILD_FROM argument is provided, so the base image
# must be named here directly. This tag is a multi-arch manifest (amd64, arm64).
FROM ghcr.io/home-assistant/base:3.24-2026.06.1

# bash, curl, jq, libstdc++ and LANG=C.UTF-8 all come from the base image already.
# ripgrep and libgcc are Claude Code's remaining runtime requirements on musl;
# nginx fronts the ingress UI, ttyd serves the terminal, python3 runs the API and
# any scripts a skill ships.
RUN apk add --no-cache \
        git \
        libgcc \
        nginx \
        python3 \
        ripgrep \
        ttyd

# Anthropic's signed apk repository. The key checksum is the one published in the
# Claude Code install docs; the build fails if the downloaded key does not match.
# Note this tracks the `stable` channel while update_channel defaults to `latest`,
# so a fresh install takes one update hop. That is deliberate: the packaged copy is
# only the offline fallback.
RUN wget -O /etc/apk/keys/claude-code.rsa.pub \
        https://downloads.claude.ai/keys/claude-code.rsa.pub \
    && echo "395759c1f7449ef4cdef305a42e820f3c766d6090d142634ebdb049f113168b6  /etc/apk/keys/claude-code.rsa.pub" \
        | sha256sum -c - \
    && echo "https://downloads.claude.ai/claude-code/apk/stable" >> /etc/apk/repositories \
    && apk add --no-cache claude-code

COPY nginx.conf /etc/nginx/nginx.conf.template
COPY www /www
COPY api.py run.sh shell.sh /
RUN chmod a+x /run.sh /shell.sh

# Supervisor maps an unhealthy container to a restart when the add-on's Watchdog
# option is on, and this is the only probe that also covers ttyd. /ping is the
# token-free liveness route; the API's other routes require the token.
HEALTHCHECK --interval=30s --timeout=5s --start-period=45s --retries=3 \
    CMD curl -fsS -o /dev/null http://127.0.0.1:7682/ping \
        && curl -fsS -o /dev/null http://127.0.0.1:7681/terminal/

# No LABEL block on purpose: Supervisor passes io.hass.name/description/url/arch
# and io.hass.version from config.yaml as build-time --label arguments, which win
# over anything set here. A duplicate version in this file could only ever drift
# out of date.

CMD [ "/run.sh" ]
