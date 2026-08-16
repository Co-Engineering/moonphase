# Setup

## Development

Requires Docker, Node 20+, pnpm, uv, and the Supabase CLI.

```bash
cp .env.example .env

# Generate the key that encrypts SSH and harness credentials at rest.
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# → paste into MOONPHASE_SECRET_KEY

supabase start          # prints ANON_KEY and JWT secret
# → paste ANON_KEY into SUPABASE_ANON_KEY and VITE_SUPABASE_ANON_KEY

./scripts/dev.sh
```

`dev.sh` starts Supabase, applies migrations, builds the runtime image if
missing, then runs the API on `:8787`, Vite on `:5273` and the Electron shell.
Ctrl-C stops everything it started.

Ports are 8787 and 5273 rather than the more usual 8000 and 5173 because those
collide with other local Supabase and Vite projects often enough to be
annoying.

## Adding your first server

Any machine you can reach over SSH. It needs Docker, or a user with
passwordless sudo so Moonphase can install it.

1. **Add server** in the sidebar.
2. Pick an authentication mode:
   - *Password (once)* — Moonphase logs in, installs its own ed25519 key,
     verifies key-only login works, then destroys the password. Recommended.
   - *Moonphase-managed key* — you are shown a public key to install yourself.
     Nothing of yours is ever sent to the backend.
   - *Paste my private key* — fastest, but Moonphase then holds a credential
     that probably opens more than this one machine.
3. Wait for **online**. The card shows the Docker version and the pinned host
   key fingerprint.

## Creating a project

**New project**, pick the server, name it, optionally give a repository URL.

For authentication, either paste an Anthropic API key (stored encrypted), or
leave it blank and sign in to your Claude subscription from inside the terminal
on first attach.

On first attach Claude Code asks whether you trust the workspace folder. That
prompt is deliberately not pre-answered — it guards against hostile content in
a cloned repository, and it is your call, not Moonphase's.

## Verifying an install

```bash
cd apps/api && .venv/bin/python -m pytest tests/ -v
```

- `test_rls.py` — tenancy isolation and secret confinement (needs Supabase)
- `test_end_to_end.py` — SSH → Docker → tmux → PTY against a throwaway sshd
  container (needs Docker)

For a full-stack check against the running API:

```bash
docker build -t moonphase/fake-server:latest infra/testing/fake-server/
apps/api/.venv/bin/python scripts/smoke.py
```

That signs up a real GoTrue user, bootstraps a server, provisions a project,
attaches a WebSocket terminal, and asserts a second user can see none of it.

## Self-hosting

The pieces you need running:

| Component        | Purpose                                    |
| ---------------- | ------------------------------------------ |
| Supabase         | Postgres + GoTrue auth                     |
| Moonphase API    | SSH, Docker orchestration, PTY bridge      |
| Static frontend  | The web/PWA client                         |
| zrok controller  | Preview tunnels (v0.3)                     |

Notes for a real deployment:

- **`MOONPHASE_SECRET_KEY` is not recoverable.** Lose it and every stored SSH
  key and harness credential becomes unreadable; you re-onboard every server.
  Back it up somewhere other than the database.
- **Terminate TLS in front of the API.** The WebSocket carries a live terminal
  and, during harness login, credentials.
- **Give the API its own Postgres role.** It currently connects as `postgres`
  for convenience. It needs `SET ROLE authenticated`, `service_role`, and
  access to the `private` schema — nothing more.
- **The API must be reachable from your phone**, which is the entire point.
  Put it behind a reverse proxy with a real certificate, or on a VPN.
- Managed servers only need inbound SSH, and only from the backend.

## Troubleshooting

**"Invalid token" on every request.** Supabase now signs with ES256 via JWKS.
Check `SUPABASE_URL` points at the auth service the client is using — the API
fetches `/auth/v1/.well-known/jwks.json` from it.

**CORS errors in the browser.** `MOONPHASE_CORS_ORIGINS` must list the exact
origin the frontend is served from, and the API must be restarted after
changing it.

**Electron shows a blank window / connection refused.** Vite must bind IPv4;
`server.host` is pinned to `127.0.0.1` for this reason, since `localhost`
resolves to `::1` on some systems while Electron loads `127.0.0.1`.

**Electron fails to start with "failed to install correctly."** Its postinstall
can silently produce an incomplete `dist/` on very new Node versions. Extract
the cached zip by hand:

```bash
cd node_modules/.pnpm/electron@*/node_modules/electron
rm -rf dist && mkdir dist
unzip -q ~/.cache/electron/*/electron-v*-linux-x64.zip -d dist
printf 'electron' > path.txt
```

**Server stuck in `error` after a Docker install.** Group membership only
applies to new sessions. Press **Test** to reconnect and re-probe.
