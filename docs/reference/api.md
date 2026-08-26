# HTTP API

The backend is a plain FastAPI application. Everything the clients do goes through
these routes.

The desktop app has exactly one capability the web client lacks, and it is a
browser capability rather than an API one: it can set a window's proxy, which is
what makes [previews](../guides/previews.md) work. Every route below is open to
both.

## Interactive reference

The API serves its own OpenAPI schema, which is always correct for the version you are
running:

| Path            | What                        |
| --------------- | --------------------------- |
| `/docs`         | Swagger UI                  |
| `/redoc`        | ReDoc                       |
| `/openapi.json` | The schema itself           |

The tables below are a map, not a substitute.

## Authentication

Every route except `/api/health` and `/api/config` requires a GoTrue access token:

```bash
curl -H "Authorization: Bearer $TOKEN" https://moonphase.example/api/servers
```

The token's claims are pushed into Postgres for the duration of the request, and
row-level security decides what you can see. A route that forgets to filter returns an
empty set rather than someone else's data — see [the security model](../concepts/security.md).

## Discovery

| Method | Path           | Purpose |
| ------ | -------------- | ------- |
| `GET`  | `/api/health`  | Liveness |
| `GET`  | `/api/config`  | Auth URL, anon key, VAPID public key, and whether signup is open — public by design |

`/api/config` is what lets a phone connect by typing one address: the client fetches its
own configuration at runtime rather than having it baked into the bundle.

## Servers

| Method | Path | Purpose |
| ------ | ---- | ------- |
| `GET`, `POST` | `/api/servers` | List, add |
| `GET`, `PATCH`, `DELETE` | `/api/servers/{server_id}` | Read, rename, remove. `PATCH` takes the name only — the address and key are what it authenticated against |
| `POST` | `/api/servers/{server_id}/bootstrap` | Install a key, probe or install Docker |
| `POST` | `/api/servers/{server_id}/test` | Reconnect and re-probe |
| `GET`, `POST` | `/api/servers/{server_id}/shares` | List, grant |
| `PATCH`, `DELETE` | `/api/servers/{server_id}/shares/{share_id}` | Change role, revoke |

## Projects

| Method | Path | Purpose |
| ------ | ---- | ------- |
| `GET`, `POST` | `/api/projects` | List, create |
| `GET`, `PATCH`, `DELETE` | `/api/projects/{project_id}` | Read, rename, remove. `PATCH` takes the display name only; the container keeps the name it was created with |
| `POST` | `/api/projects/{project_id}/start` | Start the container |
| `POST` | `/api/projects/{project_id}/stop` | Stop it |
| `GET` | `/api/projects/{project_id}/logs` | Container logs |
| `GET`, `POST` | `/api/projects/{project_id}/shares` | List, grant |
| `PATCH`, `DELETE` | `/api/projects/{project_id}/shares/{share_id}` | Change role, revoke |

## Sessions

| Method | Path | Purpose |
| ------ | ---- | ------- |
| `GET` | `/api/sessions` | Every session you can see, across all projects |
| `GET`, `POST` | `/api/projects/{project_id}/sessions` | List, create |
| `POST` | `/api/projects/{project_id}/sessions/start` | Start the harness |
| `DELETE` | `/api/projects/{project_id}/sessions/{name}` | Remove |
| `PATCH` | `/api/projects/{project_id}/sessions/{name}/rename` | Display name only — see [sessions](../concepts/sessions.md) |
| `POST` | `/api/projects/{project_id}/sessions/keys` | Type into a session |
| `GET` | `/api/projects/{project_id}/sessions/snapshot` | Plain-text pane capture |
| `POST` | `/api/projects/{project_id}/sessions/{name}/detach-clients` | Drop stale tmux clients |
| `POST` | `/api/projects/{project_id}/sessions/ticket` | Mint a WebSocket ticket (below) |

The live terminal is a WebSocket, not one of these. A browser cannot set
headers on the handshake, so proof of identity travels in the query string —
as a short-lived, single-use ticket rather than the bearer token itself,
since query parameters land in proxy access logs. Mint one first with
`POST …/sessions/ticket`, scoped to the project it names:

```text
ws(s)://<host>/ws/projects/{project_id}/terminal?session=<name>&ticket=<ticket>
```

`token=<jwt>` still works as a fallback for a client that has not switched to
tickets, and is what the feed and preview sockets below still use:

```text
ws(s)://<host>/ws/projects/{project_id}/feed?session=<name>&token=<jwt>
```

## Reading a session

| Method | Path | Purpose |
| ------ | ---- | ------- |
| `GET` | `/api/projects/{project_id}/feed` | Paged transcript |
| `POST` | `/api/projects/{project_id}/feed/answer` | Answer the current prompt |
| `GET` | `/api/projects/{project_id}/sessions/{name}/summary` | Counted plain-English digest |
| `GET` | `/api/projects/{project_id}/sessions/{name}/changes` | Branch diff, committed or not |
| `GET` | `/api/attention` | Every question waiting on you, options parsed |
| `POST` | `/api/projects/{project_id}/sessions/{name}/answer` | Answer a named session |
| `GET` | `/api/search?q=` | Search every transcript you own |

## Save points

| Method | Path | Purpose |
| ------ | ---- | ------- |
| `GET`, `POST` | `/api/projects/{project_id}/sessions/{name}/checkpoints` | List, save |
| `POST` | `…/checkpoints/{checkpoint}/restore` | Go back to one |

Only in sessions you own — they commit as the session's git identity.

## Previews

| Method | Path | Purpose |
| ------ | ---- | ------- |
| `GET` | `/api/projects/{project_id}/ports` | What the container is listening on |
| `POST`, `DELETE` | `/api/projects/{project_id}/preview` | Which ports are worth opening; close a public link |
| `POST`, `DELETE` | `/api/projects/{project_id}/ports/{port}/share` | Public link on, off |

The proxy itself is a WebSocket, one per connection the app accepts:

```text
ws(s)://<host>/ws/projects/{project_id}/preview/socks?token=<jwt>
```

It carries a SOCKS5 conversation that terminates inside the container. The app
listens on its own loopback and pipes each connection here, which is what lets a
preview work against an instance running somewhere else — see
[previews](../guides/previews.md).

## Usage

| Method | Path | Purpose |
| ------ | ---- | ------- |
| `GET` | `/api/usage?hours=` | Windows, models, projects, series |
| `GET`, `PUT` | `/api/usage/limits` | Plan allowance and alert threshold |
| `GET`, `PUT` | `/api/usage/prices` | Model rates |
| `DELETE` | `/api/usage/prices/{model}` | Remove an override |

## Settings

| Method | Path | Purpose |
| ------ | ---- | ------- |
| `GET`, `PUT` | `/api/profile` | Claude settings, MCP, CLAUDE.md, env vars, git identity |
| `POST` | `/api/profile/harness/api-key` | Store an API key |
| `POST` | `/api/profile/harness/login/start` | Begin subscription sign-in |
| `GET` | `/api/profile/harness/login/{session_id}` | Poll it |
| `POST` | `/api/profile/harness/login/code` | Submit the code |
| `DELETE` | `/api/profile/harness` | Disconnect |
| `POST` | `/api/profile/github/device/start` | Begin GitHub device flow |
| `GET` | `/api/profile/github/device/{session_id}` | Poll it |
| `POST` | `/api/profile/github/token` | Store a personal access token |
| `DELETE` | `/api/profile/github` | Disconnect |
| `GET`, `PUT` | `/api/environments` | List, create or update |
| `DELETE` | `/api/environments/{key}` | Remove |
| `GET` | `/api/harnesses` | Which harnesses are available and configured |
| `GET` | `/api/organizations` | Your organizations |

## The instance

Administering the instance rather than your own account. Every route here refuses
anyone who is not in `instance_admins` — which is not the same as owning an
organization, since every account owns one. See
[people and access](../guides/people.md).

| Method | Path | Purpose |
| ------ | ---- | ------- |
| `GET` | `/api/instance/me` | Whether the caller administers this instance |
| `GET` | `/api/instance/people` | Every account here |
| `POST` | `/api/instance/people` | Create one; returns a password, shown once |
| `DELETE` | `/api/instance/people/{user_id}` | Remove one. Refused while it owns projects |
| `PUT` | `/api/instance/people/{user_id}/admin` | Grant or revoke administration |
| `GET`, `PUT` | `/api/instance/settings` | The domain, and whether signup is open |
| `GET` | `/api/setup` | Whether setup is needed — unauthenticated |
| `POST` | `/api/setup` | Complete setup. The first caller claims the instance |
| `GET`, `PUT` | `/api/setup/methods` | Which ways of signing in are offered |

Two of those are asked by the proxy rather than by a client, and answer with a
status rather than a body: `/api/setup/signup-allowed` gates registration per
request, and `/api/setup/tls-allowed` decides which hostnames may be issued a
certificate.

## Notifications

| Method | Path | Purpose |
| ------ | ---- | ------- |
| `GET` | `/api/notifications` | Whether push is configured, and whether you are subscribed |
| `POST` | `/api/notifications/subscribe` | Register a device |
| `POST` | `/api/notifications/unsubscribe` | Remove one |
| `POST` | `/api/notifications/test` | Send yourself one |
