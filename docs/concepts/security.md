# Security model

Moonphase holds SSH keys to machines you own and runs agents with your credentials on
them. The boundaries are worth stating exactly, including the ones that are not where
you might assume.

## Boundaries

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

**Administering the instance is separate from owning anything in it.** Every
account gets a personal organization on signup, with itself as owner — so "owner
of an organization" is true of everyone and says nothing. Changing the instance's
domain, reopening registration and managing accounts are gated on
`instance_admins`, which starts as whoever completed setup and is edited only by
someone already on it. The last one cannot be removed, because an instance nobody
can administer is recoverable only with a database client.

This was not always true. The check used to ask for `owner` or `admin` of any
organization, which every account satisfies, so any signed-in user could change
the domain or reopen the door. It mattered little while nothing worse hung off it
and would have mattered a great deal once account management did.

**The preview proxy listens nowhere.** It is an unauthenticated path *as the
container*, so it is never published: the stream is carried to the desktop app
over the same authenticated WebSocket connection as everything else, and the app
opens the local port itself. It used to bind loopback on the API's machine, which
was safe only because nothing but a browser on that machine could reach it —
true of a development build, false of an installed app.

## Sharing

Organizations answer "my team can use everything we own". They are the wrong
shape for the two things people actually ask for: lending a colleague a
machine, and pulling someone into one running session to look at what the agent
just did. Those are individual grants, and they live in `server_shares` and
`project_shares`.

A grant is keyed on an **email address**, not a user id. You can share with
someone who has not signed up yet; a trigger on `auth.users` claims the row
when they do. Requiring the invitee to register first, then coming back to
finish, is the kind of friction that means the feature goes unused on a
self-hosted install where there is no directory to pick people from.

Two roles are granted — `viewer` and `collaborator` — and they resolve, along
with organization membership, into one of four effective levels:

| level    | what it is                                          |
| -------- | --------------------------------------------------- |
| `admin`  | everything, including deleting it and managing its shares |
| `write`  | use it: start, stop, type into it, create projects on it |
| `read`   | watch it                                             |
| `host`   | you own the machine a project runs on, but not the project |

`host` exists because lending someone a server means their projects appear on
your hardware. Not seeing them at all would make Moonphase useless as a view of
your own machine; seeing them as `read` would hand you a colleague's agent
conversation. So the level is deliberately non-linear: a host sees that a
project exists and can reclaim the resources, and gets nothing that would let
them read or drive it.

The levels are computed by `public.project_access_for()` and
`public.server_access_for()` in SQL. The RLS policies call them and so does the
API, via `runtime.load_project_context(..., require=CAN_CONTROL)`. One
definition, so a route cannot end up more permissive than the row-level rules
it is running under.

Three consequences worth stating:

- **Shares never grant administration.** Only `admin` can create or revoke
  them, so there is no re-sharing and no way to escalate by being shared with.
  A share recipient cannot re-bootstrap, test, or delete a server.
- **A project on a borrowed machine belongs to whoever made it** — their
  organization, their harness credentials, their transcript.
- **A project share discloses the server's name and nothing else.** The
  listing query calls `public.server_label()` instead of joining `servers`,
  so the address, the login and the host key stay with the people who own it.
- **Creating a share discloses whether the target email has an account.** A
  single invite doing that is an accepted, bounded trade-off; being able to
  call it at any rate is not, since it turns that lookup into a way to
  batch-enumerate the instance's users. `routers/shares.py` rate limits share
  creation per caller (`ratelimit.py`) so this stays a per-invite disclosure
  rather than a bulk one.

