# Roadmap

Ordered so that each step is useful on its own and none of them require
rewriting what came before.

## v0.1 — vertical slice ✅

The riskiest chain, working end to end: add server → SSH bootstrap → Docker
container → tmux + Claude Code → live terminal in Electron.

- [x] Org/membership/role schema with RLS from the first migration
- [x] Three SSH onboarding modes, converging on a Moonphase-generated key
- [x] Host key pinning with hard failure on mismatch
- [x] Docker probe and optional install
- [x] Per-project container with workspace and home volumes, optional git clone
- [x] tmux session that survives detach, Ctrl-C and client crashes
- [x] WebSocket PTY bridge with reconnect and resize
- [x] Harness abstraction with Claude Code as the first implementation
- [x] Electron shell over a responsive web UI

## v0.2 — make it comfortable

The slice proves it works; this makes it something you would actually leave
running.

- [ ] **Claude Code OAuth relay.** Surface the login URL and code in the UI so
      subscription users never touch an API key. The credential tables already
      model `oauth` mode and store the blob.
- [ ] **Web push notifications.** "Claude is waiting for you" is the single
      highest-value feature after the terminal itself — it is the whole reason
      you walked away.
- [ ] **Repo credentials.** A per-project deploy key so `git clone` and `git
      push` work for private repositories.
- [ ] **Session health reconciliation.** A periodic sweep that reconciles
      recorded state against what is actually running, so a rebooted server
      does not leave the UI lying.
- [ ] **WebSocket auth tickets.** Replace the token query parameter with a
      short-lived single-use ticket, so access tokens stop landing in logs.

## v0.3 — zrok previews

- [ ] Self-hosted zrok controller in the compose stack
- [ ] Reserved share per project, so preview URLs survive restarts
- [ ] Detect the dev server port instead of asking for it up front
- [ ] Preview link in the project header, shareable to a phone

## v0.4 — the phone client

- [ ] Tail the harness transcript JSONL and stream parsed events to clients
- [ ] Chat-style read view of the live session
- [ ] Tap-to-answer permission prompts, written back with `tmux send-keys`
- [ ] Installable PWA with push, served by the same backend

## v0.5 — teams

The schema is already here; this is screens and flows.

- [ ] Invite by email, accept flow
- [ ] Role management UI (owner / admin / member / viewer)
- [ ] Enforce `viewer` as read-only at the terminal layer, not just in queries
- [ ] Per-server sharing within an org
- [ ] Audit log of who attached to what

## v0.6 — more harnesses

- [ ] OpenCode as a second `Harness` subclass
- [ ] Multiple harnesses in one workspace as separate tmux sessions
- [ ] Per-harness runtime images

## Known issues

- **Container images are large** (~1.5 GB). A slimmer base and a shared layer
  cache per server would help.
- **No resource limits by default.** `cpus` and `memory` are plumbed through
  the API but not exposed in the UI, so one runaway build can starve a box.
- **`git clone` failures leave the container running** with an empty workspace.
  Recoverable by hand; should be a retry action.
- **The Docker install path needs passwordless sudo** and is the only place
  Moonphase wants elevated rights. Servers with Docker preinstalled never hit
  it.
