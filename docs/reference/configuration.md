# Configuration

Everything is read from the environment. `cp .env.example .env` gives you a working set of
development defaults; only `MOONPHASE_SECRET_KEY` has no default, because it has to be
generated.

## Secrets

### `MOONPHASE_SECRET_KEY`

**Required. Not recoverable.**

Fernet key encrypting SSH private keys and harness credentials at rest.

```console
$ python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

!!! danger
    Lose this and every stored SSH key and harness credential becomes unreadable. You
    re-onboard every server by hand. Back it up somewhere other than the database it
    protects.

## Database and authentication

| Variable              | Default                                                            | Notes |
| --------------------- | ------------------------------------------------------------------ | ----- |
| `DATABASE_URL`        | `postgresql+asyncpg://postgres:postgres@127.0.0.1:54722/postgres`   | Must be the asyncpg driver |
| `SUPABASE_URL`        | `http://127.0.0.1:54721`                                           | The API fetches JWKS from here |
| `SUPABASE_ANON_KEY`   | —                                                                  | Printed by `supabase start` |
| `SUPABASE_JWT_SECRET` | `super-secret-jwt-token-…`                                         | Change it for anything real |
| `MOONPHASE_AUTH_URL`  | —                                                                  | Where GoTrue answers, for the admin calls that create and delete accounts |

`SUPABASE_URL` must point at the auth service the *client* is using. A mismatch produces
"Invalid token" on every request, because the API cannot verify signatures it fetched keys
for from somewhere else.

`MOONPHASE_AUTH_URL` is separate because it is the address the **API** uses, not
the browser. In the Docker stack it is `http://auth:9999` — two containers on one
network have no reason to go out through the proxy and back to reach each other.
Left empty it falls back to `SUPABASE_URL` plus `/auth/v1`, which is right when
the API is run directly.

## API

| Variable                 | Default                                | Notes |
| ------------------------ | -------------------------------------- | ----- |
| `MOONPHASE_API_HOST`     | `0.0.0.0`                              | |
| `MOONPHASE_API_PORT`     | `8471`                                 | |
| `MOONPHASE_CORS_ORIGINS` | `http://localhost:8472,app://.`        | Exact origins, comma-separated |

`MOONPHASE_CORS_ORIGINS` must list the exact origin the frontend is served from, and the
API must be restarted after changing it. A built frontend served by the API itself is
same-origin and needs no entry.

## Containers

| Variable                   | Default                            | Notes |
| -------------------------- | ---------------------------------- | ----- |
| `MOONPHASE_RUNTIME_IMAGE`  | `moonphase/runtime-claude:latest`  | Built by `infra/images/claude` |
| `MOONPHASE_RUNTIME_IMAGE_TEMPLATE` | `moonphase/runtime-claude:{environment}` | Where per-environment images are published. The catalogue itself lives in `environments.py` |

## SSH

| Variable                            | Default | Notes |
| ----------------------------------- | ------- | ----- |
| `MOONPHASE_SSH_CONNECT_TIMEOUT`     | `15`    | Seconds |
| `MOONPHASE_SSH_KEEPALIVE_INTERVAL`  | `30`    | Seconds |
| `MOONPHASE_SSH_TRUST_ON_FIRST_USE`  | `true`  | See below |

With trust-on-first-use enabled, the first host key Moonphase sees for a server is pinned.
A later mismatch is a hard failure rather than a prompt — the point of pinning is that
nobody gets to click through it.

Set it to `false` to require the fingerprint to be supplied when the server is added.

## Previews

| Variable                  | Default       | Notes |
| ------------------------- | ------------- | ----- |
| `MOONPHASE_PREVIEW_BIND`  | `127.0.0.1`   | Interface the per-port listeners bind to |
| `MOONPHASE_PREVIEW_HOST`  | `127.0.0.1`   | Host clients dial to reach them |

Loopback suits a backend on the same machine as your browser.

!!! warning "Widening the bind"
    Setting `MOONPHASE_PREVIEW_BIND=0.0.0.0` makes preview listeners reachable from
    anywhere that can route to the backend. Shared ports have no authentication — that is
    what makes them shareable — so this is a real exposure decision and not a convenience
    toggle.

    The SOCKS proxy used by desktop previews is deliberately **not** governed by this
    setting and always binds to loopback.

## Notifications

| Variable                        | Default                 | Notes |
| ------------------------------- | ----------------------- | ----- |
| `MOONPHASE_MONITOR_INTERVAL`    | `20`                    | Seconds. `0` disables the monitor |
| `MOONPHASE_VAPID_PUBLIC_KEY`    | —                       | |
| `MOONPHASE_VAPID_PRIVATE_KEY`   | —                       | |
| `MOONPHASE_VAPID_SUBJECT`       | `mailto:you@example.com`| Must be an address you control |

Generate the keypair:

```console
$ apps/api/.venv/bin/python scripts/gen_vapid.py >> .env
```

Disabling the monitor also disables activity dots and budget alerts, since all three are
computed by the same sweep.

## GitHub

| Variable                      | Default | Notes |
| ----------------------------- | ------- | ----- |
| `MOONPHASE_GITHUB_CLIENT_ID`  | —       | Enables device-flow sign-in |

The client id of a GitHub OAuth app. No client secret is needed. Leave it blank to connect
with a personal access token instead.

## Frontend

Public by design, and only needed for `pnpm dev` where Vite serves the app on its own
port.

| Variable                  | Default                   |
| ------------------------- | ------------------------- |
| `VITE_SUPABASE_URL`       | `http://127.0.0.1:54721`  |
| `VITE_SUPABASE_ANON_KEY`  | —                         |
| `VITE_API_URL`            | `http://127.0.0.1:8471`   |

A **built** frontend is served by the API and discovers all of this from `GET /api/config`
at runtime. That is what lets a phone connect by typing one address, and why these are not
baked into the bundle.
