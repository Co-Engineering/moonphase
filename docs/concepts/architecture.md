# Architecture

## The problem

You are deep in a coding session on your laptop when you have to leave. Closing
the lid kills the agent mid-task, so you sit back down and wait. The session is
tied to the wrong machine.

Moonphase moves the session onto a server you own and makes every device a
thin, disposable view onto it.

## Shape

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

The backend is the only component that holds SSH credentials and the only one
that talks to managed servers. Clients authenticate against GoTrue and stream
terminals over WebSocket; they hold no secrets and no state worth keeping.

## The session model

A detached `tmux` session per project is the single source of truth; clients attach and
detach freely and the session never notices. Sessions are individual — each has its own
`HOME`, its own credentials and its own git worktree.

[The session model in full →](sessions.md)

## Security model

The backend is the only component that holds SSH credentials. Tenancy is enforced by
row-level security in Postgres rather than by application code, and secrets live in a
schema the `authenticated` role cannot reach at all.

[The security model in full →](security.md)

## Previews: why forwarding a port is not enough

A project runs a frontend on 5173 and an API on 8000. Forward each to a free
local port and the frontend loads — and then fails, because the page runs in a
browser on the *client* machine, so its `fetch('http://localhost:8000')` gets
the client's port 8000. That is not the API and may well be something else.

Renumbering cannot fix it. The address is the application's choice, and it asks
for the one it was written with. Preserving the numbers only works while they
happen to be free, breaks for a second project, and is impossible below 1024.
Rewriting the app's URLs is not an option either: the agent writes that code
and cannot be relied on to write it a particular way.

So Moonphase stops translating addresses and changes what they mean. Each
project gets a **SOCKS5 proxy** (`socks.py`) whose every connection is
terminated by the same `docker exec socat` relay the tunnels use, and the
desktop shell opens previews in a window routed through it. Inside that window
`localhost:8000` *is* the API, the page's origin *is* `http://localhost:5173`,
and a CORS allowlist written for local development matches because nothing is
being faked. Hardcoded ports, absolute URLs, websockets and a service on port
80 all behave exactly as they would if the browser were on the same machine as
the code.

Two details are load-bearing:

- **`proxyBypassRules: '<-loopback>'`.** Chromium never proxies loopback
  addresses by default, so without it every `localhost` request — which is to
  say all of them — goes straight to the client machine and misses the
  container entirely.
- **Both address families are tried for a name.** socat resolves a name once
  and connects to the first address it gets. Node binds `localhost` to ::1, so
  a request that resolves to 127.0.0.1 first simply fails against Vite.

Two properties of the proxy are deliberate and worth knowing:

- **It binds loopback and cannot be configured otherwise.** The port tunnels
  take their bind address from settings, because exposing one port of one
  container to a phone is what they are for. A SOCKS proxy is a general network
  path *as the container*, with no authentication in front of it — Chromium
  implements no SOCKS5 auth — so following that setting would mean anyone who
  enabled phone previews published an open proxy into their container.
- **Opening one requires control access, not observation.** Whoever holds a
  preview can POST to the app's own API. Someone shared in to watch a session
  can see what the agent is doing; acting on it through a side door is a
  different thing, and view-only has to mean it.

What remains, and is the same trust boundary the tunnels already have: any
local process on the backend host can use an open proxy. On a desktop install
that host is your own machine and grants nothing new. On a shared backend it
means container-level network access for anyone with a local account there.

The limitation is honest and worth stating: a proxy only helps a client whose
proxy we can set. That is the Electron window. A phone or an external browser
still gets a forwarded port, which is enough for a single service and cannot be
enough for an app that calls its own API by address.

## Surviving a reboot

A managed server restarts — maintenance, power, someone typing `reboot`. The
containers come back on their own, because they are started with
`--restart unless-stopped`. Everything *inside* them does not: tmux is gone,
and with it the agent and its conversation.

Two things follow, and Moonphase used to get both wrong by saying nothing. The
record has to match the machine: the monitor is the only thing that looks at
every project regularly, so it reconciles project status from what the
container actually reports. A project claiming to be running while its
container is stopped offers a terminal, a Stop button and a green dot for
something that does not exist.

And a container that came back with nothing running in it is a state of its own
— genuinely running, genuinely empty — worth naming rather than reporting as
either half. Sessions in that state offer **Resume**, which starts the harness
with `--continue` so it reopens the conversation it was having instead of a
blank prompt in the right directory. Restarting without that would be
technically a recovery and practically a loss.

Resuming is asked of the harness rather than assumed: `launch_spec(resume=...)`
is part of the seam, and an agent that cannot resume ignores it and starts
normally.

## Notifications are the product working while you are gone

Everything else is about a session outliving your laptop. This is the part that
lets you stop watching it: a push arrives when an agent starts waiting for you,
so closing the app is safe.

They are real system notifications, not a banner inside a page, and the
mechanism is worth stating because it is the reason they work with the app
closed. The browser subscribes and is handed an endpoint at its own push
service — Google's for Chrome and Android, Apple's for Safari and iOS. Moonphase
signs a message with the VAPID private key and posts it there. That service
delivers it over the connection the operating system already keeps open, wakes
the service worker, and the worker calls `showNotification`. Moonphase holds no
connection to the phone and does not need the app to be running.

Two platform facts shape the interface around it:

- **On iPhone and iPad there is no push at all until the site is installed to
  the Home Screen.** Safari does not expose `PushManager` in a tab, so the
  honest report — "this browser has no push support" — is true and useless.
  Anyone reading it would conclude Moonphase does not work on their phone. The
  settings panel detects this case specifically and gives the taps instead.
- **A secure context is required**, and a phone pointed at a plain-http address
  on a home network is the likeliest way to end up without one. Browsers say
  almost nothing about it, so the connect screen warns before you connect
  rather than after nothing happens.

A notification about a question uses `requireInteraction`, so it stays until it
is answered rather than fading while the phone is face down; an announcement
that a run finished does not. The icon badge is derived from what is still in
the notification shade rather than a counter kept in the worker, because a
service worker is stopped and restarted at the browser's discretion and any
total it held would be wrong by morning.

## The harness seam

`harness/base.py` defines what Moonphase needs to know about a coding agent:
how to launch it, how to give it credentials, how to tell whether it has any,
and where it writes transcripts. Everything above that line — containers, tmux,
the PTY bridge, the UI — is harness-agnostic.

Adding OpenCode is a subclass plus an enum value that already exists in the
database. It is not a change to the session machinery.

One deliberate restraint: `seed_config_files()` skips cosmetic first-run
wizards (Claude Code's theme picker) but does **not** pre-answer the workspace
trust prompt. That prompt guards against hostile content in a cloned repo, and
answering it is the user's decision, not Moonphase's.

## Request flow: opening a project

1. Client sends `POST /api/projects/{id}/sessions/start` with a GoTrue bearer
   token.
2. `auth.py` verifies it — ES256 against the published JWKS, or HS256 against a
   configured secret, with the algorithm pinned to what the key actually is.
3. `runtime.load_project_context` reads the project through an RLS-scoped
   session (authorization), then decrypts the server's key through a service
   session (capability).
4. `ssh.pool` returns a pooled connection, opening one if needed.
5. `sessions.ensure_session` is idempotent: if tmux is already running, it does
   nothing at all. Recreating it would throw away the conversation the user
   came back for.
6. The client opens `WS /ws/projects/{id}/terminal`, which allocates a PTY on a
   new channel of that same SSH connection and pumps bytes both ways.

## Still missing

* **zrok tunnels.** Previews work today through a per-port listener on the
  backend, which is enough when you can reach it. A real public URL — for a
  webhook, an OAuth callback, or sending a link to someone — still wants zrok.
* **Invites and role management UI.** Orgs, roles and policies exist in the
  database and are enforced; only the screens are missing.
* **WebSocket auth tickets.** The terminal socket takes its token as a query
  parameter, so access tokens land in proxy logs.
* **OpenCode.** A second `Harness` subclass, and an enum value that is already
  in the schema.
