# SecondHandler: FastAPI dashboard + headed Chromium (Xvfb/noVNC for the one-time login) + Claude Code CLI
FROM mcr.microsoft.com/playwright/python:v1.63.0-noble

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    DISPLAY=:99 \
    DATA_DIR=/data \
    PATH=/opt/venv/bin:/home/pwuser/.local/bin:$PATH \
    CLAUDE_BIN=/home/pwuser/.local/bin/claude \
    DISABLE_AUTOUPDATER=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends xvfb x11vnc novnc websockify supervisor curl ca-certificates \
      tesseract-ocr tesseract-ocr-deu \
 && rm -rf /var/lib/apt/lists/*

# wireproxy: userspace WireGuard with a local proxy, for the anonymous requests' VPN exits (app/platforms/vpn.py).
# No root, no tun device, no NET_ADMIN. Pinned with the release's sha256 per architecture.
ARG WIREPROXY_VERSION=1.1.3
ARG TARGETARCH
RUN arch="${TARGETARCH:-amd64}" \
 && case "$arch" in \
      amd64) sum=e88c1d090740373fc606c1bafd81d9a5eadc642cce5667616e20e9d7a444f51c ;; \
      arm64) sum=370e00bd2167960d1ecd1c3c1439715bbaa94a0a110a2040468670c9af6021b6 ;; \
      *) echo "no wireproxy checksum for $arch" >&2; exit 1 ;; \
    esac \
 && curl -fsSL -o /tmp/wireproxy.tar.gz "https://github.com/windtf/wireproxy/releases/download/v${WIREPROXY_VERSION}/wireproxy_linux_${arch}.tar.gz" \
 && echo "$sum  /tmp/wireproxy.tar.gz" | sha256sum -c - \
 && tar -xzf /tmp/wireproxy.tar.gz -C /usr/local/bin wireproxy \
 && chown root:root /usr/local/bin/wireproxy && chmod 755 /usr/local/bin/wireproxy \
 && rm /tmp/wireproxy.tar.gz \
 && wireproxy --version

COPY --from=ghcr.io/astral-sh/uv:0.12.23 /uv /usr/local/bin/uv

# Claude Code CLI, version pinned on purpose: `claude -p` must keep reading CLAUDE_CODE_OAUTH_TOKEN
# (the docs announce --bare, which ignores it, as a future default). Check before bumping.
ARG CLAUDE_CODE_VERSION=2.1.286
USER pwuser
RUN curl -fsSL https://claude.ai/install.sh | bash -s ${CLAUDE_CODE_VERSION} && claude --version
USER root

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv export --frozen --no-dev --no-hashes -o /tmp/requirements.txt \
 && uv venv /opt/venv \
 && uv pip install --python /opt/venv/bin/python -r /tmp/requirements.txt

# Google Chrome (stable) for the app's browser, driven through patchright: the bundled Chromium-for-Testing and
# Playwright's automation traces are what bot protections (Vinted's DataDome) block at login. amd64 only (Google
# ships no arm64 Linux Chrome); elsewhere the bundled Chromium stays in use (app/willhaben/session.py).
RUN if [ "$(dpkg --print-architecture)" = "amd64" ]; then /opt/venv/bin/patchright install --with-deps chrome \
      && google-chrome --version; fi

COPY app ./app
COPY docker/supervisord.conf /etc/supervisor/conf.d/willhaben.conf
COPY docker/entrypoint.sh docker/start-vnc.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/entrypoint.sh /usr/local/bin/start-vnc.sh

EXPOSE 8000 6080
VOLUME ["/data"]
HEALTHCHECK --interval=60s --timeout=5s --start-period=60s CMD curl -fs http://127.0.0.1:8000/healthz || exit 1
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["/usr/bin/supervisord", "-n", "-c", "/etc/supervisor/supervisord.conf"]
