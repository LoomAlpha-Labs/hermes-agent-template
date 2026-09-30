FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS sqlite-builder

# Keep SQLite independent of Debian's release cadence. The official archive
# checksum is published at https://sqlite.org/download.html. The Python
# verifier is run in both stages so a source, build, copy, or loader mismatch
# fails the image build rather than appearing against a persistent database.
ARG SQLITE_AUTOCONF=3530400
ARG SQLITE_VERSION=3.53.4
ARG SQLITE_SOURCE_ID="2026-07-24 19:02:57 bf7c7f30031888f4e796e429ab3978879485813aaca6f641c7b33e4e09459bcc"
ARG SQLITE_ARCHIVE_SHA3_256=454e45f61c6bd75b7420e7190732dea03ce6639c63ada47bbc592f67fc340338
RUN apt-get update && \
    apt-get install -y --no-install-recommends build-essential ca-certificates curl && \
    curl --fail --show-error --silent --location \
      "https://sqlite.org/2026/sqlite-autoconf-${SQLITE_AUTOCONF}.tar.gz" \
      --output /tmp/sqlite.tar.gz && \
    python -c "import hashlib,pathlib; p=pathlib.Path('/tmp/sqlite.tar.gz'); expected='${SQLITE_ARCHIVE_SHA3_256}'; actual=hashlib.sha3_256(p.read_bytes()).hexdigest(); assert actual == expected, f'SQLite archive SHA3-256 mismatch: {actual}'" && \
    mkdir /tmp/sqlite-src && \
    tar -xzf /tmp/sqlite.tar.gz --strip-components=1 -C /tmp/sqlite-src && \
    cd /tmp/sqlite-src && \
    CFLAGS="-O2 -DSQLITE_ENABLE_FTS5" ./configure --prefix=/opt/sqlite --disable-static --enable-shared && \
    make -j"$(nproc)" && \
    make install

COPY scripts/verify_sqlite_runtime.py /tmp/verify-sqlite-runtime.py
RUN LD_LIBRARY_PATH=/opt/sqlite/lib \
    python /tmp/verify-sqlite-runtime.py \
      --expected-version "${SQLITE_VERSION}" \
      --expected-source-id "${SQLITE_SOURCE_ID}" \
      --expected-library-prefix /opt/sqlite/lib

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ARG SQLITE_VERSION=3.53.4
ARG SQLITE_SOURCE_ID="2026-07-24 19:02:57 bf7c7f30031888f4e796e429ab3978879485813aaca6f641c7b33e4e09459bcc"
COPY --from=sqlite-builder /opt/sqlite/lib/ /opt/sqlite/lib/
COPY scripts/verify_sqlite_runtime.py /usr/local/libexec/hermes/verify-sqlite-runtime.py
RUN printf '%s\n' /opt/sqlite/lib > /etc/ld.so.conf.d/hermes-sqlite.conf && \
    ldconfig && \
    env -i PATH=/usr/local/bin:/usr/bin:/bin HOME=/tmp \
      /usr/local/bin/python /usr/local/libexec/hermes/verify-sqlite-runtime.py \
        --expected-version "${SQLITE_VERSION}" \
        --expected-source-id "${SQLITE_SOURCE_ID}" \
        --expected-library-prefix /opt/sqlite/lib

# Which hermes-agent revision to install. Accepts any git ref the upstream
# repo publishes — a release tag (recommended for reproducibility) or a
# branch name (`main`) for bleeding edge.
#
# To bump: check https://github.com/NousResearch/hermes-agent/releases for the
# newest tag (format `vYYYY.M.D`, optionally with a `.PATCH` suffix, e.g.
# `v2026.5.29.2`) and update the default below. Use `main` only if you accept
# that every rebuild can pull arbitrary new upstream commits.
ARG HERMES_REF=v2026.9.14

# tini = tiny init that we run as PID 1. Without it, hermes's grandchild
# processes (MCP stdio servers, git, bun, browser daemons spawned by tools)
# reparent to PID 1 when their parents exit and pile up as zombies. After
# weeks of uptime that exhausts the kernel's PID table → "fork: cannot
# allocate memory" and the container dies. tini reaps zombies in the
# background and forwards SIGTERM/SIGINT to our entrypoint so Railway's
# stop signal still triggers our graceful shutdown. Standard container init
# (same as Docker's `--init` flag and Kubernetes' pause container).
#
# Node.js is required only at build time to compile the Hermes React dashboard.
# We strip the source + apt lists afterwards to keep the image lean.
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl ca-certificates git gh tini && \
    curl -fsSL https://deb.nodesource.com/setup_22.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    rm -rf /var/lib/apt/lists/*

# Install hermes-agent (provides the `hermes` CLI) and pre-build its React
# dashboard so `hermes dashboard` has nothing to build at runtime.
#
# [all] does not pull in [dev]; messaging platforms, TTS, and
# other heavy backends are lazy-installed by hermes at first use. We pre-install
# the ones this template actually uses so first-message latency is instant.
# Pillow is now a core dependency; keep the backward-compatible vision extra
# listed explicitly. Without image handling hermes cannot downscale an
# oversized image (>5 MB / >8000px), which then bakes into immutable history
# and bricks the session on Anthropic's non-retryable 400. We bake it in.
# When bumping HERMES_REF, re-check hermes-agent's pyproject.toml [all] and
# the extras below against the new release's pyproject.toml.
RUN git clone --depth 1 --branch ${HERMES_REF} https://github.com/NousResearch/hermes-agent.git /opt/hermes-agent && \
    cd /opt/hermes-agent && \
    uv pip install --system --no-cache -e ".[all,messaging,tts-premium,honcho,bedrock,anthropic,edge-tts,hindsight,vision]" && \
    python -c "import run_agent, gateway.run, cron.scheduler; from gateway.session_context import _VAR_MAP; assert 'HERMES_CRON_SESSION' in _VAR_MAP" && \
    uv venv --python /usr/local/bin/python --system-site-packages /tmp/hermes-check && \
    uv pip install --python /tmp/hermes-check/bin/python pytest==9.1.1 pytest-asyncio==1.3.0 && \
    /tmp/hermes-check/bin/python -m pytest -q \
      tests/cron/test_scheduler_cron_session_isolation.py \
      tests/gateway/test_session_hygiene_turnhold_adoption.py \
      tests/agent/test_compression_small_ctx_threshold_floor.py \
      tests/tui_gateway/test_compression_config_hot_reload.py \
      tests/cron/test_cron_script.py \
      tests/hermes_state/test_shared_session_db_registry.py \
      tests/hermes_state/test_dedupe_migration_contention.py \
      tests/gateway/test_session_db_handle_sharing.py && \
    rm -rf /tmp/hermes-check && \
    cd /opt/hermes-agent/web && \
    npm install --silent && \
    npm run build && \
    cd /opt/hermes-agent/ui-tui && \
    npm install --silent --no-fund --no-audit --progress=false && \
    npm run build && \
    rm -rf /opt/hermes-agent/web /opt/hermes-agent/.git /root/.npm

# Why pre-build ui-tui (and why we don't delete it after):
# - The dashboard's embedded Chat tab spawns `node ui-tui/dist/entry.js`
#   on every WebSocket connect to /api/pty.
# - Without HERMES_TUI_DIR, hermes's _make_tui_argv falls through to the
#   npm install + build path (since git-editable installs don't have the
#   bundled tui_dist/ that PyPI wheels include), adding 30-60s to the
#   first chat-open and blocking the asyncio event loop.
# - Pre-building at image time surfaces build failures here rather than
#   at user request time, and makes first-chat-open instant.
# - We keep ui-tui/ entirely (node_modules + dist + src) so HERMES_TUI_DIR
#   can point at it (see below).

COPY requirements.txt /app/requirements.txt
RUN uv pip install --system --no-cache -r /app/requirements.txt

RUN mkdir -p /data/.hermes

COPY server.py /app/server.py
COPY jev_worker.py /app/jev_worker.py
COPY bookmark_worker.py /app/bookmark_worker.py
RUN npm install --global --ignore-scripts @steipete/bird@0.8.0 && bird --version
COPY templates/ /app/templates/
COPY start.sh /app/start.sh
RUN chmod +x /app/start.sh

# Re-run after all apt and Python dependency installation so the final service
# interpreter—not only the early runtime layer—must retain the pinned linkage.
RUN env -i PATH=/usr/local/bin:/usr/bin:/bin HOME=/tmp \
      /usr/local/bin/python /usr/local/libexec/hermes/verify-sqlite-runtime.py \
        --expected-version "${SQLITE_VERSION}" \
        --expected-source-id "${SQLITE_SOURCE_ID}" \
        --expected-library-prefix /opt/sqlite/lib
ENV HOME=/data
ENV HERMES_HOME=/data/.hermes

# Points hermes at our pre-built TUI bundle. hermes's _make_tui_argv checks
# HERMES_TUI_DIR first: if dist/entry.js exists there, it skips the npm
# install/build entirely. This is the official packager path (Nix uses it too)
# and avoids the 30-60s npm bootstrap that git-editable installs would otherwise
# trigger on first /chat connection.
ENV HERMES_TUI_DIR=/opt/hermes-agent/ui-tui

# tini wraps start.sh so it runs as PID 1's child instead of as PID 1 itself.
# `-g` propagates signals to the whole process group so `docker stop` /
# Railway's SIGTERM cleanly terminates the entire tree, not just start.sh.
ENTRYPOINT ["/usr/bin/tini", "-g", "--"]
CMD ["/app/start.sh"]
