# Installation from source

!!! tip "Just want to run it?"
    [Install with Docker](docker.md) — one command, and Docker is the only requirement.

    This page is the development setup: hot reload, the Electron shell, and the test
    suites.

Moonphase runs as three pieces: a Postgres database with authentication (Supabase), the
backend that talks to your servers, and a client. For development, one script starts all
of them.

## Requirements

| Tool             | Why                                                        |
| ---------------- | ---------------------------------------------------------- |
| Docker           | Runs Supabase locally, and builds the project runtime image |
| Node 20+ & pnpm  | The web client and the Electron shell                       |
| [uv][uv]         | Python dependencies for the backend                         |
| Supabase CLI     | Local Postgres, GoTrue and migrations                       |

[uv]: https://docs.astral.sh/uv/

You also need a server to point it at — any machine you can reach over SSH. It does not
need Docker installed; Moonphase can install it, given a user with passwordless sudo.

## Set up

```bash
git clone https://github.com/Co-Engineering/moonphase.git
cd moonphase
cp .env.example .env
```

### Generate the encryption key

Moonphase encrypts SSH private keys and harness credentials before they touch the
database. That key is not derived from anything, so you have to make it:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Paste the result into `MOONPHASE_SECRET_KEY` in `.env`.

!!! danger "This key is not recoverable"
    Lose it and every stored SSH key and harness credential becomes unreadable — you will
    re-onboard every server. Back it up somewhere other than the database it protects.

### Start Supabase

```bash
supabase start
```

It prints an `anon key` and a JWT secret. Paste the anon key into **both**
`SUPABASE_ANON_KEY` and `VITE_SUPABASE_ANON_KEY`.

### Run it

```bash
./scripts/dev.sh
```

That starts Supabase if it is not already up, applies migrations, builds the runtime
image if it is missing, then runs:

| Service       | Address                 |
| ------------- | ----------------------- |
| API           | `http://127.0.0.1:8471` |
| Web (Vite)    | `http://127.0.0.1:8472` |
| Electron      | opens automatically     |

`Ctrl-C` stops everything it started.

??? question "Why 8471 and 8472?"
    Because 8000 and 5173 collide with other local Supabase and Vite projects often
    enough to be genuinely annoying, and a port conflict at first run looks exactly like
    a broken install.

## Notifications (optional, recommended)

Push notifications are what make leaving a session running worthwhile. They need a VAPID
keypair:

```bash
apps/api/.venv/bin/python scripts/gen_vapid.py >> .env
```

Then set `MOONPHASE_VAPID_SUBJECT` to a `mailto:` address you control, and restart the
API.

!!! warning "Push requires HTTPS"
    Service workers and Web Push only work in a secure context. A phone pointed at
    `http://192.168.1.x:8471` cannot install the app or receive anything, and browsers
    say very little about why. `localhost` is exempt, which is why it works on the
    machine running it. See [working from your phone](../guides/from-your-phone.md).

## Verify the install

```bash
cd apps/api && .venv/bin/python -m pytest tests/ -q
```

Two of those tests are the ones that matter:

- `test_rls.py` — tenancy isolation and secret confinement. Needs Supabase running.
- `test_end_to_end.py` — SSH → Docker → tmux → PTY against a throwaway `sshd` container.
  Needs Docker.

For a full-stack check against the running API:

```bash
docker build -t moonphase/fake-server:latest infra/testing/fake-server/
apps/api/.venv/bin/python scripts/smoke.py
```

That signs up a real GoTrue user, bootstraps a server, provisions a project, attaches a
WebSocket terminal, and asserts that a second user can see none of it.

## Next

[Add your first server →](first-server.md){ .md-button .md-button--primary }
[Self-hosting for real →](../guides/self-hosting.md){ .md-button }
