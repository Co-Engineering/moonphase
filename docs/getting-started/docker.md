# Install with Docker

The fastest way to run Moonphase. Docker is the only thing you need — the
database, sign-in, and every key that encrypts your credentials are created for you.

```console
$ curl -fsSL https://raw.githubusercontent.com/oliversvane/moonphase/main/scripts/install.sh | sh
```

Then open **<http://127.0.0.1:8471>** and create an account. The first one is yours.

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
| `api` | built from source | SSH, Docker orchestration, the PTY bridge, and the client |
| `proxy` | `caddy:2-alpine` | Puts all of it on one address |

Deliberately **not** the full Supabase stack. Moonphase talks to Postgres directly with
asyncpg and never uses PostgREST, Realtime or Storage, so running them would be several
hundred megabytes of container doing nothing. What it does depend on — three roles and
the `auth.uid()` helper every policy is written against — is created by
`docker/bootstrap.sql`.

The API image is about 400 MB and contains both the backend and the built client, because
[one address is the whole product](../concepts/architecture.md).

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

!!! danger "Put TLS in front of it"
    The WebSocket carries a live terminal, and during harness sign-in it carries
    credentials. [Push notifications also require a secure context](../guides/from-your-phone.md),
    so without HTTPS the feature that makes leaving a session running worthwhile does not
    exist at all.

    A reverse proxy with a real certificate, a Tailscale HTTPS address, or a Cloudflare
    tunnel all work.

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
$ git pull
$ docker compose up -d --build
```

Migrations run automatically on start, and are additive and forward-only.

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
