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

## v0.2 — make it comfortable ✅

The slice proved it works; this made it something you would actually leave
running.

- [x] **Global sign-in and settings.** One organization-wide profile holds the
      Claude credential, global CLAUDE.md, settings.json, MCP config, env vars
      and git identity, materialised into every container on session start.
- [x] **Claude Code sign-in relay.** The login runs on a PTY in a throwaway
      container; the URL is surfaced in the UI and the code typed back.
- [x] **GitHub.** Device flow or a pasted token, becoming a git credential
      helper and `GH_TOKEN` in every project.
- [x] **Automatic preview ports.** Detected inside the container's network
      namespace and tunnelled on demand, so nothing is declared up front.
- [x] **User-defined environments.** A base image plus setup commands, built
      on the server from a generated recipe.
- [x] **Session activity and web push.** A background monitor watches every
      running project and notifies when the agent stops working or blocks on a
      question — the reason you were able to walk away at all.
- [ ] **Session health reconciliation.** The monitor already detects a stopped
      container; it should write that back to `projects.status` so a rebooted
      server does not leave the UI lying.
- [ ] **WebSocket auth tickets.** Replace the token query parameter with a
      short-lived single-use ticket, so access tokens stop landing in logs.

## v0.3 — zrok previews

- [ ] Self-hosted zrok controller in the compose stack
- [ ] Reserved share per project, so preview URLs survive restarts
- [ ] Detect the dev server port instead of asking for it up front
- [ ] Preview link in the project header, shareable to a phone

## v0.4 — the phone client ✅

- [x] Tail the harness transcript JSONL and serve parsed events with a cursor
- [x] Chat-style read view of the live session, defaulting on narrow screens
- [x] Tap-to-answer prompts, written back with `tmux send-keys`
- [x] Installable PWA with push, served by the same backend
- [ ] Stream events over the existing WebSocket instead of polling every 3s
- [ ] Render diffs for Edit/Write rather than just the file path

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
