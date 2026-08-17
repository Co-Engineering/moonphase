# Moonphase

Your coding agents live on servers you own, not on the laptop you need to close.

Moonphase is a self-hosted control plane for remote AI coding sessions. Add a server over
SSH, create a project, and Moonphase provisions an isolated Docker container with a coding
harness running inside a persistent `tmux` session. Attach from your desktop, detach by
shutting the lid, reattach from your phone on the train. The session never noticed.

## Status

**v0.1 — vertical slice.** The full chain works end to end:

```
add server → SSH bootstrap → docker container → tmux + claude → live terminal
```

Implemented:

- Self-hosted Supabase (GoTrue auth + Postgres with RLS from the first migration)
- Organizations, memberships and roles in the schema; personal org auto-created on signup
- Add a server with three SSH trust modes (password bootstrap, Moonphase-managed key,
  bring-your-own key), Docker probe and optional install
- Create a project → dedicated container + named volumes, optional `git clone`
- Claude Code launched in `tmux`, attached over a WebSocket PTY bridge into xterm.js
- Global sign-in and settings: connect Claude Code and GitHub once, every project uses it
- User-definable environments — base image plus setup commands, built on the server
- Automatic port detection and preview tunnels; nothing is ever declared
- Previews that run inside the container's network, so an app calling its own
  API at `http://localhost:8000` works without being written any particular way
- A readable transcript feed for phones, streaming, that writes back into the same session
- Activity detection and Web Push, so you learn the agent is waiting without a client open
- Share a server or a project with one person, by email, as a viewer or a collaborator
- Electron desktop shell

Not yet: zrok tunnels (previews are reachable from the backend host today), OpenCode,
organization invites and role management UI.

## Installing it on your phone

Moonphase serves the built frontend from the API, so one address is the whole
thing. Open it on your phone and add it to your home screen; the first launch
asks for your host only if it cannot work that out from where it was served.

```bash
pnpm --filter @moonphase/web build   # the API serves apps/web/dist when present
```

Then **Settings → Notifications → Enable**, and you get a push whenever one of
your sessions starts waiting for you. Notifications go to the person who owns
the session and nobody else — someone watching a colleague's agent cannot
answer its questions, so waking them would be noise.

**It has to be HTTPS.** Service workers and push are only available in a secure
context, so a phone pointed at `http://192.168.1.x:8471` will not be able to
install the app or receive anything, and browsers say very little about why. A
reverse proxy with a real certificate, a Tailscale HTTPS address, or a
Cloudflare tunnel all work. `localhost` is exempt, which is why it works on the
machine running it.

## Sharing

Two ways to give someone access, both from the **Share** button on a server or a project:

|                   | `Can view`                              | `Can use`                                        |
| ----------------- | --------------------------------------- | ------------------------------------------------ |
| **on a server**   | see the machine and how it is doing     | also create their own projects on it              |
| **on a project**  | watch the feed and terminal, read-only  | also type into it, answer prompts, start and stop |

Share by email. If they have not signed up yet the grant waits for them, so "share it and
tell them to register" works in that order.

A project someone creates on a server you lent them is **theirs** — their organization,
their Claude account, their transcript. You see that it exists and can reclaim the
resources; you do not get to read it. Shares never grant administration: only the owner
can bootstrap, test, delete, or decide who else gets in.

### Sessions are individual

Sharing a project never shares the Claude subscription behind it. A session belongs to
one person, a project can hold several, and **you drive your own and may watch anyone's**.

Each session gets its own `HOME` inside the container — its own credentials, settings,
history and git identity — and its own **git worktree** on branch `moonphase/<session>`.
So two people work the same repository without their agents overwriting each other, and
merging is ordinary git. Commits carry the right author. Nobody's work ever runs on
somebody else's account.

## Architecture

```
  phone ──┐
  laptop ──┼──►  ┌──────────────────────┐
  desktop ─┘     │  Moonphase backend   │ always on
                 │  FastAPI + Supabase  │
                 └──────────┬───────────┘
                            │ ssh (asyncssh)
              ┌─────────────┼─────────────┐
           srv-a          srv-b         srv-c
         ┌────────┐     ┌────────┐   ┌────────┐
         │ docker │     │ docker │   │ docker │
         │ proj×3 │     │ proj×1 │   │ proj×2 │
         └────────┘     └────────┘   └────────┘
```

The backend is the only component that holds SSH credentials and the only one that talks to
your servers. Clients are thin: they authenticate against GoTrue and stream terminals over
WebSocket.

Inside a project container, one long-lived `tmux` session is the single source of truth.
The desktop attaches to its real PTY; the (planned) phone client renders a readable feed
parsed from Claude Code's own session transcript and writes back with `tmux send-keys`, so
both surfaces drive the same session with no protocol drift.

**[Read the documentation →](https://oliversvane.github.io/moonphase/)**

Or locally:

```bash
pip install -r docs/requirements.txt
mkdocs serve
```

## Layout

```
apps/api        FastAPI backend — SSH, Docker orchestration, PTY bridge
apps/web        React + Vite + xterm.js frontend (also the future PWA)
apps/desktop    Electron shell around apps/web
infra/images    Container images projects run in
supabase        Migrations, RLS policies, local Supabase config
docker          Image, compose stack and schema bootstrap for self-hosting
docs            Documentation site (MkDocs Material)
```

## Quick start

Docker is the only requirement.

```bash
curl -fsSL https://raw.githubusercontent.com/oliversvane/moonphase/main/scripts/install.sh | sh
```

That clones the repository, generates every secret it needs, and brings up four
containers: Postgres, GoTrue for sign-in, the API, and Caddy putting all of it on one
address. Open `http://127.0.0.1:8471` and create an account.

To work on Moonphase itself — hot reload, the Electron shell, the test suites — you also
need Node 20+, pnpm, uv and the Supabase CLI:

```bash
cp .env.example .env
./scripts/dev.sh
```

That boots Supabase, applies migrations, starts the API on `:8471` and Vite on `:8472`,
then opens the Electron shell. Full instructions and the self-host path are in the
[documentation](https://oliversvane.github.io/moonphase/getting-started/installation/).

## License

See [LICENSE](LICENSE).
