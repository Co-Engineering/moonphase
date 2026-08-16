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
- A readable transcript feed for phones, streaming, that writes back into the same session
- Activity detection and Web Push, so you learn the agent is waiting without a client open
- Share a server or a project with one person, by email, as a viewer or a collaborator
- Electron desktop shell

Not yet: zrok tunnels (previews are reachable from the backend host today), OpenCode,
organization invites and role management UI.

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

See [`docs/architecture.md`](docs/architecture.md) for the full design, and
[`docs/roadmap.md`](docs/roadmap.md) for what lands when.

## Layout

```
apps/api        FastAPI backend — SSH, Docker orchestration, PTY bridge
apps/web        React + Vite + xterm.js frontend (also the future PWA)
apps/desktop    Electron shell around apps/web
infra/images    Container images projects run in
supabase        Migrations, RLS policies, local Supabase config
docs            Architecture and roadmap
```

## Quick start

Requires Docker, Node 20+, pnpm, uv, and the Supabase CLI.

```bash
cp .env.example .env
./scripts/dev.sh
```

That boots Supabase, applies migrations, starts the API on `:8000` and Vite on `:5173`,
then opens the Electron shell. Full instructions and the self-host path are in
[`docs/setup.md`](docs/setup.md).

## License

See [LICENSE](LICENSE).
