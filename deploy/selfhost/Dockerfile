# syntax=docker/dockerfile:1
# SANA GTM application image: the API, the platform worker (which runs every crawler and task
# kind) and the one-shot "migrate" service all use it. Build context: the repository root.
#
#   docker compose -f deploy/selfhost/compose.yaml build          (the sanagtm CLI does this)
#
# INSTALL_BROWSER=1 adds Playwright + Chromium for the AI scraper's browser fallback (~1 GB).
FROM python:3.12-slim-bookworm

ARG INSTALL_BROWSER=0
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright \
    PYTHONPATH=/app

# PostgreSQL 17 client tools (pg_dump / pg_restore for backups, restores and migration) from the
# PostgreSQL project's own apt repository: Debian bookworm ships only version 15.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl gnupg; \
    install -d /usr/share/postgresql-common/pgdg; \
    curl -fsSL -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc https://www.postgresql.org/media/keys/ACCC4CF8.asc; \
    echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" \
        > /etc/apt/sources.list.d/pgdg.list; \
    apt-get update; \
    apt-get install -y --no-install-recommends postgresql-client-17; \
    apt-get purge -y gnupg; apt-get autoremove -y; rm -rf /var/lib/apt/lists/*; \
    /usr/lib/postgresql/17/bin/pg_dump --version

RUN groupadd --system --gid 10001 sanagtm \
 && useradd --system --uid 10001 --gid sanagtm --home-dir /app --shell /usr/sbin/nologin sanagtm

WORKDIR /app
COPY deploy/selfhost/requirements.txt deploy/selfhost/requirements-browser.txt deploy/selfhost/requirements-test.txt /tmp/req/
RUN pip install -r /tmp/req/requirements.txt -r /tmp/req/requirements-test.txt
RUN if [ "$INSTALL_BROWSER" = "1" ]; then \
        pip install -r /tmp/req/requirements-browser.txt && python -m playwright install --with-deps chromium; \
    fi

# Application code (read-only for the runtime user). Not copied: sheets/ and store/ (the
# crawler's Google Sheets / SQLite paths, which the platform refuses to load), the web app
# (its own image), Windows tooling, docs.
COPY adapters ./adapters
COPY config ./config
COPY crawler ./crawler
COPY exporters ./exporters
COPY models ./models
COPY utils ./utils
COPY main.py ./
COPY cloud ./cloud
COPY deploy/ops ./deploy/ops
RUN rm -rf /app/cloud/web && python -m compileall -q /app/cloud /app/adapters /app/crawler > /dev/null

USER sanagtm
CMD ["python", "-m", "cloud.ops.selfhost", "info"]
