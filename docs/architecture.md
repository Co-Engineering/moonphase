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

This is the load-bearing idea, so it is worth being precise.

Inside each project container, one **detached tmux session** owns the harness
process. Nothing about that session belongs to a client:

```
container
└── tmux server (detached, survives everything)
    └── session "moonphase"
        └── launch-moonphase.sh      ← sources credentials, sets job control
            └── claude               ← owns the terminal foreground group
```

Attaching runs `docker exec -it … tmux attach` on a fresh SSH channel with a
PTY. Detaching closes that channel. The tmux server — and the harness — never
notice. Closing a laptop, losing a train tunnel, and quitting the app are all
the same event, and none of them interrupt work.

Two details in that diagram are load-bearing and were found by testing rather
than reasoning:

* **`set -m` in the launcher.** A non-interactive shell does not put its child
  in a new process group, so the wrapper and the harness would share the
  terminal's foreground group. Ctrl-C would then kill the wrapper, close the
  pane, and destroy the session — precisely the thing the design promises not
  to do. Job control gives the harness its own group.
* **The wrapper does not `exec`.** When the harness exits, the wrapper prints a
  message and drops to a shell, keeping the pane and therefore the session
  alive. A crashed agent leaves something to reattach to.

## Two clients, one session

The desktop attaches to the real PTY. The phone renders a readable feed parsed
from the harness's own JSONL transcript, and writes back with `tmux send-keys`.

Both surfaces therefore drive the *same* tmux session. There is no second
protocol to keep in sync and no risk of the two views disagreeing: a permission
prompt answered on a phone appears in the desktop terminal as if typed there.

The phone deliberately does **not** attach a terminal, for two reasons. An
80-column TUI on a 390-pixel screen is unusable. And tmux sizes a window to its
most recent client, so a phone attaching would squeeze the desktop down to
phone width — the feed observes without ever becoming a client.

That second point has a sharp edge worth knowing about. `docker exec` does not
kill the process it started when its client disconnects, so a closed terminal
leaves `tmux attach` running inside the container forever. Those phantom
clients accumulate and keep constraining the window size. The attach wrapper
therefore announces its tty before tmux takes the screen, and the bridge
consumes that line and detaches itself explicitly on the way out.

## Security model

**Tenancy is enforced by Postgres, not by the API.** The backend connects with a
privileged role but every request-scoped query runs through
`db.user_session()`, which does `SET LOCAL ROLE authenticated` plus
`SET LOCAL request.jwt.claims`. RLS policies then apply. A route handler that
forgets to filter returns an empty set rather than another tenant's servers.
`db.service_session()` opts out explicitly and is used in exactly two places:
reading the `private` schema, and background work with no caller.

**Secrets never reach a client.** SSH keys and harness credentials live in the
`private` schema, which is not exposed to PostgREST and has RLS enabled with no
policy granting `authenticated` anything. They are Fernet-encrypted with a key
from the environment, so a database dump on its own is inert.

**Moonphase holds only credentials it made.** All three onboarding modes
converge on a per-server ed25519 key that Moonphase generated. The password
bootstrap flow uses the user's password exactly once, verifies key-only login
works, then destroys it. Deleting a server revokes that key and nothing else.

**Host keys are pinned** on first successful connect. A later mismatch raises
`HostKeyMismatch` and refuses to connect, because the alternative is handing an
SSH private key to whoever now answers on that address. Getting this right
required not passing `known_hosts=None` to asyncssh — that disables validation
entirely and never calls the pinning callback.

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
