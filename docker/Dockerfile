# Moonphase, as one image: the API and the frontend it serves.
#
# They ship together on purpose. The API serves the built client from
# `apps/web/dist`, which is what makes one address the whole product — you point
# a phone at your host, get the app, and it talks to the API it came from. Same
# origin, so there is no CORS to configure, no second URL to remember, and the
# service worker lands at the root scope where push requires it.

# --- build the client -------------------------------------------------------
FROM node:22-slim AS web

WORKDIR /src
RUN corepack enable

# Manifests first, so a change to application code does not reinstall the world.
# Every workspace member's manifest is needed for pnpm to resolve the lockfile.
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY apps/web/package.json apps/web/
COPY apps/desktop/package.json apps/desktop/
RUN pnpm install --frozen-lockfile --filter @moonphase/web...

COPY apps/web apps/web
RUN pnpm --filter @moonphase/web build


# --- run the API ------------------------------------------------------------
FROM python:3.12-slim AS runtime

# ssh-client is not used by the app — asyncssh speaks the protocol itself — but
# having it present makes diagnosing a server that will not connect enormously
# easier from inside the container.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        openssh-client \
        postgresql-client \
    && rm -rf /var/lib/apt/lists/*

# Never as root. The API opens outbound SSH connections and runs a proxy; there
# is nothing it needs root for.
RUN useradd --create-home --uid 10001 moonphase

WORKDIR /app

# Dependencies before source, for the same caching reason as above.
COPY apps/api/pyproject.toml apps/api/README.md* apps/api/
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir /app/apps/api

COPY apps/api/moonphase apps/api/moonphase
COPY scripts/gen_vapid.py scripts/gen_vapid.py

# The schema travels with the code that expects it.
#
# These used to be bind-mounted from a checkout on the server, which meant
# `docker compose pull` upgraded the API and left the database exactly where it
# was — silently, because a migration that is not on disk is not a migration
# that failed. The first schema change after that was noticed only because the
# feature needing it did nothing.
COPY supabase/migrations /app/migrations
COPY docker/bootstrap.sql /app/bootstrap.sql
COPY docker/migrate.sh /app/migrate.sh

# The API resolves the client relative to its own file: moonphase/main.py ->
# apps/api/moonphase -> apps/api -> apps -> apps/web/dist. Keeping the source
# layout inside the image is what makes that hold.
COPY --from=web /src/apps/web/dist apps/web/dist

# Which build this is.
#
# An instance could not previously say: the version was a string in the source
# that nobody moved, so "am I up to date" had no answer and neither did "what is
# running on that server". Baked at build time and passed through by the
# workflow, so the answer comes from the artefact rather than from a promise
# somebody made in a file.
ARG MOONPHASE_COMMIT=unknown
ARG MOONPHASE_RELEASE=edge
ARG MOONPHASE_BUILT_AT=unknown
ENV MOONPHASE_COMMIT=$MOONPHASE_COMMIT \
    MOONPHASE_RELEASE=$MOONPHASE_RELEASE \
    MOONPHASE_BUILT_AT=$MOONPHASE_BUILT_AT

LABEL org.opencontainers.image.revision=$MOONPHASE_COMMIT
LABEL org.opencontainers.image.version=$MOONPHASE_RELEASE

USER moonphase

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MOONPHASE_API_HOST=0.0.0.0 \
    MOONPHASE_API_PORT=8471

EXPOSE 8471

HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=5 \
    CMD curl -fsS http://127.0.0.1:8471/api/health || exit 1

CMD ["uvicorn", "moonphase.main:app", "--host", "0.0.0.0", "--port", "8471", \
     "--app-dir", "/app/apps/api", "--proxy-headers", "--forwarded-allow-ips", "*"]
