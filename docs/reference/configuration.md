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
| `MOONPHASE_HARNESS_CHANNEL`       | `latest` | npm dist-tag or exact version Claude Code and OpenCode are kept at. It applies to both, so a package npm says has no such tag or version follows `latest` instead. Antigravity is not on npm and has only `latest` — see below |

An environment image is built once per server and then reused, so the Claude Code,
OpenCode and Antigravity inside it stay at whatever version was current on the day it was
built. Moonphase keeps them current itself instead: each project container gets its own
install of each under `/opt/moonphase/harness/<package>/<version>` (`antigravity` for
`agy`), and sessions launch whichever version the `current` link there points at, falling
back to the image's copy until the first update has finished, or whenever `current` is
missing or does not run.

An update is started, in the background, whenever a session starts, and the monitor asks
every running project's container every quarter of an interval, or every hour if that is
sooner. The container only acts on an ask once the interval has passed since its last
update started, so updates come at most about a quarter of an interval later than the
setting, however a session start shifted them. An update asks npm for the version the
channel names — or, for Antigravity, Google's release manifest for the current one —
installs it only if it is new, and switches `current` over only once the new binary has
been run successfully — a failed or broken release leaves the working one in place. Nothing
`current` or `previous` points at is ever removed: if the installed copy of the current
version stops running, it is reinstalled beside it, as `<version>.r<timestamp>`. Older
versions are removed once nothing is running them. The outcome is recorded in
`/var/lib/moonphase/harness-update.json` inside the container, including any package that
fell back to `latest` because the channel names a tag or version it does not have
(`"fallback_from"`) — pinning Claude Code to `2.1.290`, say, leaves OpenCode and
Antigravity on `latest` rather than unable to update at all.

**Antigravity comes from a release manifest, not npm.** `agy` is a native binary, and
Moonphase reads the same manifest Google's own installer does,
`https://antigravity-cli-auto-updater-974169037036.us-central1.run.app/manifests/linux_<amd64|arm64>.json`,
which gives the version, a tarball and its SHA-512; a download that does not match that
SHA-512 is not installed. The manifest only ever names the current release, so `latest`
is the only channel `agy` has: with `MOONPHASE_HARNESS_CHANNEL` pinned to anything but
the version the manifest names right now, it follows `latest`, and `harness-update.json`
records `"fallback_from"` for it under `antigravity`, as it would for an npm package with
no such tag. The npm packages called `agy` and `antigravity-cli` are
unrelated placeholders, not Google's.

A container only fetches `agy` once one of its sessions has been set up to run
Antigravity (its `HOME` has a `.gemini` directory, which Moonphase writes just before
that session's first start asks for an update), or once Moonphase already keeps a copy
there. Until then `harness-update.json` records `"result": "unused"` for `antigravity`,
which does not make the run a failure, and the image's own copy is the only one — so a
server whose projects only run Claude Code never downloads a release it will not use.

The image's own copies in `/usr/local/bin` are never changed; they stay the fallback, and
root's own `npm` keeps working on them. Login shells in the container — terminal windows,
and the shell a session's pane drops to when the harness exits — put
`/opt/moonphase/harness/<package>/current/bin` first on `PATH` through
`/etc/profile.d/moonphase-harness.sh`, but only if the binary there is executable when the
shell starts, so they otherwise find the image's copy as before. Moonphase's own
`claude mcp list` and `claude mcp login` pick their binary the same way sessions do. Shells
that are not login shells, such as a bare `docker exec`, still find the image's copy.

**A running session keeps the version it started with.** Restarting it (**Restart
harness**, or switching its account) always asks for the newest release first — npm, or
the manifest for `agy` — whatever the interval, and waits for it to install before the
harness starts, so a restart is how a session gets up to date. When the container already
has the newest, that check costs a second or two.

The harnesses' own auto-updaters are switched off in Moonphase sessions and its own
`claude mcp` calls (`DISABLE_AUTOUPDATER=1` for Claude Code,
`OPENCODE_DISABLE_AUTOUPDATE=1` for OpenCode, `AGY_CLI_DISABLE_AUTO_UPDATE=true` for
Antigravity), and in login shells in the container through
`/etc/profile.d/moonphase-harness.sh`, because Moonphase manages the version. Claude
Code's and OpenCode's installed into the shared home volume, retried every half hour from
every session, and filled each session's npm cache with downloads; every update also
removes what they left behind there, and any session npm cache over 1 GB.

`agy`'s switch takes only the literal value `true` — `1`, which the other two take, is
ignored. Without it, `agy` starts a background updater on every run, even on its sign-in
screen, which replaces the binary it was started from. In a Moonphase container that
updater finds it cannot write there and stops, because every copy of `agy` belongs to
root; the variable makes that a second guard rather than the only one. `agy update`,
typed in a session, ignores the variable and fails the same harmless way.

The environment image installs `agy` itself, from the same manifest and checked against
the same SHA-512, as `/usr/local/bin/agy`, and sets `AGY_CLI_DISABLE_AUTO_UPDATE=true` for
everything run in the container. It does not run Google's `install.sh`, which installs
into a home directory and edits shell profiles; the binary needs no setup step. `agy`
needs glibc 2.26 or newer — Debian 10 and Ubuntu 18.04 onwards, so every built-in
environment. An environment image built before Antigravity was added has no `agy`; it is
rebuilt the next time it is used, and a project's container moves onto the new image the
next time it starts, keeping both volumes.

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
