# Moonphase

**Your coding agents live on servers you own, not on the laptop you need to close.**

Moonphase is a self-hosted control plane for remote AI coding sessions. Add a server over
SSH, create a project, and Moonphase provisions an isolated Docker container with a coding
harness running inside a persistent `tmux` session. Attach from your desktop, detach by
shutting the lid, reattach from your phone. The session never noticed.

This image is the control plane: the API and the web client it serves. The containers your
*projects* run in are built on your own servers from a recipe, so there is nothing to pull
for those.

## Quick start

```bash
curl -fsSL https://raw.githubusercontent.com/Co-Engineering/moonphase/main/scripts/install.sh | sh
```

That writes an `.env`, generates every secret, and brings up four containers: Postgres,
GoTrue for sign-in, this image, and Caddy putting all of it on one address.

## Running it yourself

It needs Postgres and GoTrue alongside it — see
[`docker-compose.yml`](https://github.com/Co-Engineering/moonphase/blob/main/docker-compose.yml).

```yaml
services:
  api:
    image: oliversvanecoec/moonphase:latest   # or ghcr.io/co-engineering/moonphase:latest
    environment:
      DATABASE_URL: postgresql+asyncpg://postgres:...@db:5432/postgres
      SUPABASE_URL: https://moonphase.example.com
      SUPABASE_ANON_KEY: ...
      SUPABASE_JWT_SECRET: ...
      MOONPHASE_SECRET_KEY: ...   # encrypts SSH keys at rest; not recoverable
```

Every variable is documented in the
[configuration reference](https://co-engineering.github.io/moonphase/reference/configuration/).

## Tags

| Tag | What |
| --- | --- |
| `latest` | The most recent release |
| `0.2.1`, `0.2` | Pin as tightly or as loosely as you like |
| `edge` | The tip of `main` — builds, but is not a release |

Built for `linux/amd64` and `linux/arm64`.

The same image is on GitHub's registry as `ghcr.io/co-engineering/moonphase`, which is what
the compose file defaults to — it has no anonymous pull limit.

## Links

- [Documentation](https://co-engineering.github.io/moonphase/)
- [Source](https://github.com/Co-Engineering/moonphase)
