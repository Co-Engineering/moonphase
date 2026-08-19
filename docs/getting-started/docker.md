# Install with Docker

The fastest way to run Moonphase. Docker is the only thing you need — the
database, sign-in, and every key that encrypts your credentials are created for you.

```console
$ curl -fsSL https://raw.githubusercontent.com/oliversvane/moonphase/main/scripts/install.sh | sh
```

Then open **<http://127.0.0.1:8471>** and create an account. The first one is yours.

For a real deployment, give it the address people will use — it sets up HTTPS
for that name by itself:

```console
$ curl -fsSL https://raw.githubusercontent.com/oliversvane/moonphase/main/scripts/install.sh \
    | sh -s -- https://moonphase.example.com
```

!!! warning "`sh -s --`, not a variable in front of curl"
    `MOONPHASE_PUBLIC_URL=… curl … | sh` looks right and is not: the assignment
    applies to `curl`, not to the shell reading the script, so the setting is
    silently lost and you get a localhost install.

!!! tip "Read it before you pipe it"
    Piping a script from the internet into a shell is a thing worth being suspicious
    of. [The script is here](https://github.com/oliversvane/moonphase/blob/main/scripts/install.sh)
    — it clones the repository, writes an `.env`, and runs `docker compose up`.

    You can do the same by hand:

    ```console
    $ git clone https://github.com/oliversvane/moonphase.git
    $ cd moonphase && ./scripts/install.sh
    ```

## What it starts

Four containers:

| Service | Image | Does |
| ------- | ----- | ---- |
| `db` | `postgres:16-alpine` | Everything is stored here |
| `auth` | `supabase/gotrue` | Sign-in |
| `api` | `ghcr.io/oliversvane/moonphase` | SSH, Docker orchestration, the PTY bridge, and the client |
| `proxy` | `caddy:2-alpine` | Puts all of it on one address |

Deliberately **not** the full Supabase stack. Moonphase talks to Postgres directly with
asyncpg and never uses PostgREST, Realtime or Storage, so running them would be several
hundred megabytes of container doing nothing. What it does depend on — three roles and
the `auth.uid()` helper every policy is written against — is created by
`docker/bootstrap.sql`.

The API image is about 400 MB and contains both the backend and the built client, because
[one address is the whole product](../concepts/architecture.md). It is published for
`linux/amd64` and `linux/arm64`.

### Where the image comes from

The same image is published to two registries:

| Registry | Name |
| -------- | ---- |
| GitHub (default) | `ghcr.io/oliversvane/moonphase` |
| Docker Hub | [`oliversvanecoec/moonphase`](https://hub.docker.com/r/oliversvanecoec/moonphase) |

GitHub's is the default because it does not rate-limit anonymous pulls. Docker Hub allows
100 per six hours per IP address, which is plenty for one machine and not plenty for a
shared address or a CI runner — and it surfaces mid-install as `toomanyrequests`, which
reads like the install is broken.

To use the mirror instead, in `.env`:

```bash
MOONPHASE_IMAGE=oliversvanecoec/moonphase
```

### Pinning a version

`.env` chooses the tag:

```bash
MOONPHASE_VERSION=0.2.1     # or 0.2, or latest, or edge
```

| Tag | What it is |
| --- | ---------- |
| `latest` | The most recent release. The default. |
| `0.2.1`, `0.2` | Pin as tightly or as loosely as you like |
| `edge` | The tip of `main`. Builds and passes tests; not a release. |

Pinning an exact version is the right call for anything you depend on — an upgrade then
happens when you change that line, rather than whenever you happen to pull.

### Building it yourself

Pulling is the default because it takes a minute rather than several, and because
everyone then runs identical bits. To build instead — because you are working on
Moonphase, or want a commit that has not been released:

```console
$ docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build
```

Or let the installer do it:

```console
$ MOONPHASE_BUILD=1 ./scripts/install.sh
```

??? question "Why two compose files instead of one with both keys?"
    With `image:` and `build:` on the same service, whether Compose pulls or builds
    depends on what happens to be in the local image cache. That is the last thing an
    installer should be ambiguous about, so the two modes are two explicit files.

!!! note "Nothing to build on the server"
    The image your *projects* run in is built on the managed server itself, over the SSH
    connection Moonphase already has. This stack does not need it.

## Running it twice is safe

The installer keeps any secret already in `.env` rather than generating a new one. That
matters most for `MOONPHASE_SECRET_KEY`: rotating it would make every stored SSH key and
harness credential unreadable.

Migrations are recorded as they are applied, so a second run reports `already up to date`
and does nothing.

## Making it reachable

The default publishes to `127.0.0.1` only, which is right for trying it out and wrong for
the thing Moonphase is actually for — being reachable from your phone.

Two things to change in `.env`:

```bash
MOONPHASE_PUBLIC_URL=https://moonphase.example.com   # where people reach it
MOONPHASE_BIND=0.0.0.0                               # publish beyond loopback
```

Then `docker compose up -d`.

### HTTPS is automatic

Give the installer an `https://` address and Caddy provisions a Let's Encrypt
certificate for it, listens on 443, and redirects 80. There is nothing to edit,
install or renew, and the certificate lives in a volume so a restart does not ask
for another one.

It needs two things: ports **80 and 443** reachable from the internet, and DNS
pointing at the machine — see [pointing a domain at it](../guides/dns.md) for the
exact fields on Cloudflare, Namecheap, GoDaddy, Route 53 and the rest. The certificate is obtained the first time someone visits,
so installing before the DNS record exists is fine — it simply gets one later.

!!! danger "Do not run it on plain HTTP in public"
    The WebSocket carries a live terminal, and during harness sign-in it carries
    credentials. [Push notifications also require a secure context](../guides/from-your-phone.md),
    so without HTTPS the feature that makes leaving a session running worthwhile does not
    exist at all.

    A Tailscale HTTPS address or a Cloudflare tunnel work too, if you would rather not
    expose 80 and 443.

`MOONPHASE_PUBLIC_URL` has to be the address a **browser** uses. The client is served from
it and signs in against the same origin, so an unreachable value breaks sign-in rather
than degrading it.

## Managing it

```console
$ docker compose logs -f api          # follow the backend
$ docker compose ps                   # what is running
$ docker compose down                 # stop, keeping your data
$ docker compose down -v              # stop and delete everything
```

### Upgrading

```console
$ docker compose pull
$ docker compose up -d
```

Migrations run automatically on start, and are additive and forward-only.

If you build from source, `git pull` first and add the override:

```console
$ git pull
$ docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build
```

### Backing up

Two things, and the second is the one people forget:

```console
$ docker compose exec db pg_dump -U postgres postgres > moonphase.sql
```

…and `.env`, because it holds `MOONPHASE_SECRET_KEY`.

!!! danger "A database backup without the key is not a backup"
    Every SSH key and harness credential in that dump is encrypted with it. Keep the key
    somewhere other than the database it protects.

## Settings worth knowing

Set in `.env`, read by `docker-compose.yml`:

| Variable | Default | Meaning |
| -------- | ------- | ------- |
| `MOONPHASE_PUBLIC_URL` | `http://localhost:8471` | Where browsers reach it |
| `MOONPHASE_BIND` | `127.0.0.1` | Interface the proxy publishes on |
| `MOONPHASE_PORT` | `8471` | Port it publishes on |
| `MOONPHASE_DISABLE_SIGNUP` | `false` | Set `true` once your account exists |
| `MOONPHASE_MAILER_AUTOCONFIRM` | `true` | Set `false` when you have a mail server |
| `MOONPHASE_MONITOR_INTERVAL` | `20` | Seconds between activity checks; `0` disables notifications |

Everything the API itself understands is in the
[configuration reference](../reference/configuration.md).

!!! tip "Close signup once you are in"
    Anyone who can reach the address can create an account by default. Set
    `MOONPHASE_DISABLE_SIGNUP=true` and restart once yours exists.

## If it does not come up

```console
$ docker compose ps
$ docker compose logs migrate api
```

**`migrate` exited 1.** Usually the database was not ready or the auth schema had not
appeared. It is safe to re-run: `docker compose up -d migrate`.

**Sign-in fails with a network error.** `MOONPHASE_PUBLIC_URL` is probably not an address
your browser can reach. It is used for the auth endpoint as well as for serving the app.

**Port already in use.** Change `MOONPHASE_PORT` in `.env` and bring it up again.

More in [troubleshooting](../reference/troubleshooting.md).

## Developing instead

The Docker stack runs a *built* client, so there is no hot reload. To work on Moonphase
itself, use [the development setup](installation.md).
