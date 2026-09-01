# Roadmap

!!! info "v0.10.4 is released"
    The first tagged release, numbered to match the milestones below rather than
    starting again from 0.1 — the work through v0.5 was done long before there
    was a tag on any of it.

    The map is not exact and is not worth pretending otherwise: **v0.3** is
    still outstanding, and parts of **v0.7** shipped early. A release number
    says what is in the box, not which boxes are ticked.


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
- [x] **WebSocket auth tickets.** Replaced the token query parameter with a
      short-lived single-use ticket — and removed the token fallback
      entirely, plus turned off uvicorn's own access log, so an access
      token genuinely never lands anywhere it could be read back from.

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
- [x] Stream events over a WebSocket instead of polling — `tail -f` inside the
      container, measured at 28–60 ms against a 3000 ms polling floor
- [x] Render diffs for Edit and Write, shown with the question so an approval
      is made on the change rather than on a file name

## v0.5 — knowing what happened ✅

Moving a session onto a server is only comfortable if you can find out what it
did while you were not looking.

- [x] **Sharing.** Servers and projects, by email, as viewer or collaborator,
      with sessions staying individual — nobody's work runs on anyone else's
      account.
- [x] **Answer from anywhere.** Every waiting question, with its options
      already parsed, on the home screen and from the notification.
- [x] **Changes.** A diff of everything a session touched since it branched,
      committed or not, so an agent that wrote twenty files and committed none
      is still reviewable.
- [x] **Search.** A phrase across every transcript you own, on the machines
      where the transcripts already live.
- [x] **Save points.** Commits without the vocabulary, where going back never
      destroys anything and installed packages survive.
- [x] **Plain-English summaries.** Counted from the transcript rather than
      generated, because the reader cannot check a summary against the diff.
- [x] **Usage and spend.** Anchored limit windows with real reset times,
      per-model cost with the cache tiers priced properly, editable rates, plan
      limits and push alerts once per window.
- [x] **Claude Code settings and MCP as forms.** Unknown keys preserved, raw
      JSON always one tab away.

## v0.6 — teams

The schema is already here; this is screens and flows.

- [ ] Invite by email, accept flow
- [ ] Role management UI (owner / admin / member / viewer)
- [ ] Enforce `viewer` as read-only at the terminal layer, not just in queries
- [ ] Per-server sharing within an org
- [ ] Audit log of who attached to what

## v0.7 — more harnesses

Partly done, and out of order: the seam was drawn for a second harness long
before this milestone came up, and using it was the only way to find out whether
it held.

- [x] OpenCode as a second `Harness` subclass
- [x] Pydantic AI as a third, running the `pydantic-ai-harness` coder agent
- [ ] A feed for either. Neither writes the transcript format the feed reads, so
      their sessions show the terminal and nothing else
- [ ] Multiple harnesses in one workspace as separate tmux sessions
- [ ] Per-harness runtime images. One image carries all three today, which costs
      disk on the server and saves a rebuild when a project changes harness

## Not scheduled — signed desktop builds

The desktop app is built for Linux, macOS and Windows and installs with one
command, but nothing is code-signed: macOS quarantines the download and Windows
shows a SmartScreen panel. [The installers deal with
both](getting-started/app.md#about-the-unsigned-builds), which is not the same as
fixing it.

Signing needs an Apple Developer membership and a Windows certificate, each
renewed annually. That is a recurring bill rather than an afternoon, so it waits
for someone to decide the project should carry one.

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
