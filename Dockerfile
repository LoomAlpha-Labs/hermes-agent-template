FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim AS sqlite-builder

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

FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim

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
ARG HERMES_REF=v0.22.5
ARG HERMES_COMMIT=bf0203a794a07aac3ff764972ab9087baa4e4f4e

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

COPY scripts/stage_hermes_pm_runtime.py /tmp/stage-hermes-pm-runtime.py

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
    test "$(git rev-parse HEAD)" = "${HERMES_COMMIT}" && \
    uv export --frozen --no-dev --no-hashes --no-emit-project \
      --extra all --extra messaging --extra tts-premium \
      --extra bedrock --extra anthropic --extra edge-tts --extra vision \
      --output-file /tmp/hermes-requirements.txt && \
    uv pip install --system --no-cache --no-deps -r /tmp/hermes-requirements.txt && \
    uv pip install --system --no-cache --constraint /tmp/hermes-requirements.txt honcho-ai==2.2.0 hindsight-client==0.6.1 && \
    uv pip install --system --no-cache --no-deps -e . && \
    python scripts/write_install_stamp.py --output install-stamp.json \
      --commit "${HERMES_COMMIT}" --base-version 0.22.5 --distance 0 \
      --source ci --distribution docker --update-mechanism external && \
    python /tmp/stage-hermes-pm-runtime.py && \
    python -m pm.build_env --source /opt/hermes-agent --python /usr/local/bin/python \
      --out /opt/hermes-agent/.venv --no-install-project --sealed --record-selection \
      --extra all --extra messaging --extra tts-premium --extra bedrock \
      --extra anthropic --extra edge-tts --extra vision && \
    uv pip install --python /opt/hermes-agent/.venv/bin/python --no-cache \
      --constraint /tmp/hermes-requirements.txt honcho-ai==2.2.0 hindsight-client==0.6.1 && \
    uv pip install --python /opt/hermes-agent/.venv/bin/python --no-cache --no-deps -e . && \
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
      tests/gateway/test_session_db_handle_sharing.py \
      tests/hermes_state/test_runtime_schema.py \
      tests/hermes_cli/test_browse_schema_drift.py \
      tests/tools/test_delegation_backends.py \
      tests/tools/test_delegation_route_fields.py \
      tests/hermes_cli/test_plugin_host_delegation.py \
      tests/pm/test_runtime_entrypoints.py \
      tests/pm/test_runtime.py && \
    rm -rf /tmp/hermes-check && \
    python scripts/generate_icons.py --source /opt/hermes-agent --out /tmp/hermes-icons && \
    node scripts/build/node-deps.mjs --source /opt/hermes-agent --workspace ui-tui --workspace web && \
    node scripts/build/tui.mjs --source /opt/hermes-agent --out /tmp/hermes-tui && \
    node scripts/build/web.mjs --source /opt/hermes-agent --icons /tmp/hermes-icons --out /tmp/hermes-web && \
    cp -a /tmp/hermes-tui/. /opt/hermes-agent/ui-tui/ && \
    mkdir -p /opt/hermes-agent/hermes_cli/web_dist && \
    cp -a /tmp/hermes-web/. /opt/hermes-agent/hermes_cli/web_dist/ && \
    rm -rf /tmp/hermes-icons /tmp/hermes-tui /tmp/hermes-web /opt/hermes-agent/.git /root/.npm
ENV HERMES_WEB_DIST=/opt/hermes-agent/hermes_cli/web_dist
RUN python -c "from pathlib import Path; from hermes_cli.version_info import get_version_info; from hermes_state_common import SCHEMA_VERSION; v=get_version_info(); assert v.base_version=='0.22.5' and v.commit=='bf0203a794a07aac3ff764972ab9087baa4e4f4e' and SCHEMA_VERSION==32; assert Path('/opt/hermes-agent/ui-tui/dist/entry.js').is_file(); assert Path('/opt/hermes-agent/hermes_cli/web_dist/index.html').is_file(); print('Exact 0.22.5 identity, schema32 and frontend products passed')"

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

# Keep the existing local small.en ASR policy on Python 3.14-compatible wheels.
# The model/cache stays on the persistent volume; no model download during build.
COPY media-requirements.txt /app/media-requirements.txt
RUN uv pip install --system --no-cache --constraint /tmp/hermes-requirements.txt -r /app/media-requirements.txt && \
    uv pip install --python /opt/hermes-agent/.venv/bin/python --no-cache --constraint /tmp/hermes-requirements.txt -r /app/media-requirements.txt && \
    rm /tmp/hermes-requirements.txt && \
    python -c "import io,wave; from faster_whisper.audio import decode_audio; import ctranslate2,yt_dlp; b=io.BytesIO(); w=wave.open(b,'wb'); w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000); w.writeframes(bytes(32000)); w.close(); b.seek(0); assert len(decode_audio(b,sampling_rate=16000))==16000; assert 'int8' in ctranslate2.get_supported_compute_types('cpu'); print('Media decode and CPU int8 passed')"

# Bake the exact core lockfile's helpers into the stamped image store.
# No first-request downloads or mutable runtime installation are needed.
COPY scripts/stage_hermes_helper_tools.py /usr/local/libexec/hermes/stage-helper-tools.py
COPY scripts/verify_hermes_helper_tools.py /usr/local/libexec/hermes/verify-helper-tools.py
RUN python /usr/local/libexec/hermes/stage-helper-tools.py && \
    /opt/hermes-agent/.venv/bin/python /usr/local/libexec/hermes/verify-helper-tools.py

# Installing an enabled plugin can select a new application environment.
# Exercise the real transaction in a disposable home and fail if that selected
# environment loses any template-specific packages or media capabilities.
COPY scripts/verify_hermes_plugin_environment.py /tmp/verify-hermes-plugin-environment.py
RUN mkdir -p /tmp/hermes-plugin-check && \
    env HOME=/tmp/hermes-plugin-check HERMES_HOME=/tmp/hermes-plugin-check/.hermes \
      hermes plugins install 'NousResearch/hermes-official-plugins#advanced-delegation' \
        --ref b3825d097195b5748585b0491fab48c95d105c19 --enable --yes-deps && \
    env HOME=/tmp/hermes-plugin-check HERMES_HOME=/tmp/hermes-plugin-check/.hermes \
      python -c "from hermes_cli.plugins_cmd_git import pinned_revision; from hermes_cli.plugins import discover_plugins; from agent.delegation_registry import get_provider; assert pinned_revision('advanced-delegation') == 'b3825d097195b5748585b0491fab48c95d105c19'; discover_plugins(); assert get_provider('model_map') is not None; print('Pinned plugin manager install and discovery passed')" && \
    env HOME=/tmp/hermes-plugin-check HERMES_HOME=/tmp/hermes-plugin-check/.hermes \
      python /tmp/verify-hermes-plugin-environment.py && \
    rm -rf /tmp/hermes-plugin-check

# Narrow backport on the pinned upstream runtime; fail closed if it no longer applies.
COPY patches/ /tmp/telegram-recovery/
RUN cd /opt/hermes-agent && \
    cp /tmp/telegram-recovery/test_telegram_recovery_backport.py tests/gateway/ && \
    uv venv --python /usr/local/bin/python --system-site-packages /tmp/telegram-check && \
    uv pip install --python /tmp/telegram-check/bin/python pytest==9.1.1 pytest-asyncio==1.3.0 && \
    (HERMES_PYTHON=/tmp/telegram-check/bin/python bash scripts/run_tests.sh -j 1 \
      tests/gateway/test_telegram_recovery_backport.py > /tmp/telegram-before.txt 2>&1; \
      result=$?; cat /tmp/telegram-before.txt; test "$result" -ne 0) && \
    git apply --check /tmp/telegram-recovery/telegram-recovery.patch && \
    git apply /tmp/telegram-recovery/telegram-recovery.patch && \
    HERMES_PYTHON=/tmp/telegram-check/bin/python bash scripts/run_tests.sh -j 2 \
      tests/gateway/test_telegram_recovery_backport.py \
      tests/gateway/test_telegram_network.py \
      tests/gateway/test_telegram_send_reconnect_wait.py \
      tests/gateway/test_telegram_send_path_health.py \
      tests/gateway/test_telegram_polling_health_confirmation.py \
      tests/gateway/test_telegram_network_reconnect.py && \
    rm -rf /tmp/telegram-check /tmp/telegram-recovery

COPY requirements.txt /app/requirements.txt
RUN uv pip install --system --no-cache -r /app/requirements.txt && \
    uv pip install --python /opt/hermes-agent/.venv/bin/python --no-cache -r /app/requirements.txt

RUN mkdir -p /data/.hermes

COPY server.py /app/server.py
COPY runtime_readiness.py /app/runtime_readiness.py
COPY scripts/request_redeploy.py /app/scripts/request_redeploy.py
COPY jev_worker.py /app/jev_worker.py
COPY bookmark_worker.py bookmark_oauth.py x_oauth.py /app/
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
        --expected-library-prefix /opt/sqlite/lib && \
    env -i PATH=/usr/local/bin:/usr/bin:/bin HOME=/tmp \
      /opt/hermes-agent/.venv/bin/python /usr/local/libexec/hermes/verify-sqlite-runtime.py \
        --expected-version "${SQLITE_VERSION}" \
        --expected-source-id "${SQLITE_SOURCE_ID}" \
        --expected-library-prefix /opt/sqlite/lib && \
    cd /app && ADMIN_PASSWORD=acceptance-test-only /opt/hermes-agent/.venv/bin/python \
      -c "import server, uvicorn, run_agent, gateway.run, cron.scheduler; print('Packaged service imports passed')"
# Recheck native helper dispatch after the final application dependencies.
RUN /opt/hermes-agent/.venv/bin/python /usr/local/libexec/hermes/verify-helper-tools.py
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
