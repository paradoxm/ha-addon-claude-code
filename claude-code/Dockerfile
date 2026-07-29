# Pinned rather than :latest so a base image bump cannot change the build under us.
# Since Supervisor 2026.04.0 no BUILD_FROM argument is provided, so the base image
# must be named here directly. This tag is a multi-arch manifest (amd64, arm64).
FROM ghcr.io/home-assistant/base:3.24-2026.06.1

ENV LANG=C.UTF-8

# bash, curl, libgcc, libstdc++ and ripgrep are Claude Code's documented runtime
# requirements on musl. nginx fronts the ingress UI, ttyd serves the terminal,
# python3 runs the API and any scripts a skill ships.
RUN apk add --no-cache \
        bash \
        curl \
        git \
        jq \
        nginx \
        python3 \
        ripgrep \
        libgcc \
        libstdc++ \
        ttyd

# Anthropic's signed apk repository. The key checksum is the one published in the
# Claude Code install docs; the build fails if the downloaded key does not match.
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

LABEL \
    io.hass.name="Claude Code" \
    io.hass.description="Claude Code CLI with a skills web UI and an HTTP job API" \
    io.hass.type="addon" \
    io.hass.version="1.1.0"

CMD [ "/run.sh" ]
