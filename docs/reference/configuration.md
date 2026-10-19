# Configuration

Everything is read from the environment. `cp .env.example .env` gives you a working set of
development defaults; `MOONPHASE_SECRET_KEY` and `SUPABASE_JWT_SECRET` have no default,
because both have to be generated.

## Secrets

### `MOONPHASE_SECRET_KEY`

**Required. Not recoverable.**

Fernet key encrypting SSH private keys and harness credentials at rest.

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
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
| `SUPABASE_JWT_SECRET` | —                                                                  | Required. `supabase start`'s printed value is public — startup refuses it |
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

## Harness updates

| Variable                          | Default  | Notes |
| --------------------------------- | -------- | ----- |
| `MOONPHASE_HARNESS_UPDATE_HOURS`  | `6`      | Hours between update checks per container, counted from when the last check started. `0` disables updates |
| `MOONPHASE_HARNESS_CHANNEL`       | `latest` | npm dist-tag or exact version Claude Code and OpenCode are kept at. It applies to both, so a package npm says has no such tag or version follows `latest` instead |

An environment image is built once per server and then reused, so the Claude Code and
OpenCode inside it stay at whatever version was current on the day it was built. Moonphase
keeps them current itself instead: each project container gets its own install of each
under `/opt/moonphase/harness/<package>/<version>`, and sessions launch whichever version
the `current` link there points at, falling back to the image's copy until the first update
has finished.

An update is started, in the background, whenever a session starts and by the monitor once
per interval for every running project. It asks npm for the version the channel names,
installs it only if it is new, and switches `current` over only once the new binary has
been run successfully — a failed or broken release leaves the working one in place. The
current and previous versions are kept; older ones are removed once nothing is running them.
The outcome is recorded in `/var/lib/moonphase/harness-update.json` inside the container,
including any package that fell back to `latest` because the channel names a tag or version
it does not have (`"fallback_from"`) — pinning Claude Code to `2.1.290`, say, leaves OpenCode
on `latest` rather than unable to update at all.

Once a version is in place, `/usr/local/bin/claude` and `/usr/local/bin/opencode` point at it
too, so a shell in the container, a terminal window and Moonphase's own `claude mcp` checks
run the same version sessions do rather than the image's.

**A running session keeps the version it started with.** It picks up a new one when it is
restarted.

Claude Code's and OpenCode's own auto-updaters are switched off in Moonphase sessions
(`DISABLE_AUTOUPDATER=1`, `OPENCODE_DISABLE_AUTOUPDATE=1`), and in login shells in the
container through `/etc/profile.d/moonphase-harness.sh`, because Moonphase manages the
version. They installed into the shared home volume, retried every half hour from every
session, and filled each session's npm cache with downloads; every update also removes what
they left behind there, and any session npm cache over 1 GB.

Setting the interval to `0` leaves sessions on the newest version Moonphase has already
installed, or on the image's copy if it never installed one. The harnesses' own updaters
stay off either way, so nothing moves the version until updates are turned back on.

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

```bash
apps/api/.venv/bin/python scripts/gen_vapid.py >> .env
```

Disabling the monitor also disables activity dots and budget alerts, since all three are
computed by the same sweep.

## Resources

| Variable                                  | Default | Notes |
| ------------------------------------------ | ------- | ----- |
| `MOONPHASE_ORPHAN_VOLUME_RETENTION_DAYS`   | `7`     | Days before a volume left behind by a deleted project is actually removed |

Deleting a project keeps its volumes by default — an accidental delete should not also
delete the work. The same background monitor that computes activity also finds volumes
with no project left, and removes them once they have sat unclaimed for this long. Set it
lower to reclaim disk faster, or higher if you want longer to notice and undo a delete.

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
