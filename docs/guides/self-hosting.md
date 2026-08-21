# Self-hosting

`scripts/dev.sh` is for development. This is what a real deployment needs.

## The pieces

| Component       | Purpose                                |
| --------------- | -------------------------------------- |
| Supabase        | Postgres and GoTrue authentication     |
| Moonphase API   | SSH, Docker orchestration, PTY bridge  |
| Static frontend | Served by the API itself                |

The frontend is not a separate deployment. Build it and the API serves it:

```bash
pnpm --filter @moonphase/web build
```

That is what lets a phone connect by typing one address — the client discovers Supabase's
URL and keys from `GET /api/config` rather than being built with them baked in.

## Terminate TLS in front of the API

Not optional, for two reasons.

The WebSocket carries a live terminal, and during harness sign-in it carries credentials.
And [push notifications require a secure context](from-your-phone.md), so without HTTPS
the feature that makes the whole thing worthwhile does not exist.

A reverse proxy with a real certificate, a Tailscale HTTPS address, or a Cloudflare tunnel
all work.

Make sure your proxy passes WebSocket upgrades through, and does not buffer them — a
terminal that arrives in one-second batches is unusable.

## Back up the encryption key

`MOONPHASE_SECRET_KEY` encrypts every stored SSH private key and harness credential.

!!! danger "It is not recoverable"
    Lose it and all of them become unreadable. You re-onboard every server by hand.

    Back it up somewhere **other than the database it protects**. A backup of Postgres
    that includes the ciphertext and not the key is not a backup.

## Give the API its own Postgres role

It currently connects as `postgres` for convenience. It needs:

- `SET ROLE authenticated` and `service_role`
- access to the `private` schema

and nothing more. Narrowing this is worth doing before you point it at anything you care
about.

## Reachability

**The API must be reachable from your phone.** That is the entire point of the product,
and it is the constraint that decides your networking.

**Your servers only need inbound SSH**, and only from the backend. Moonphase never asks
you to open a port on a managed server — previews are tunnelled back over the SSH
connection it already holds.

## Previews on a remote backend

Two variables decide where preview listeners live:

| Variable                  | Meaning                                              |
| ------------------------- | ---------------------------------------------------- |
| `MOONPHASE_PREVIEW_BIND`  | Interface the per-port listeners bind to             |
| `MOONPHASE_PREVIEW_HOST`  | Host clients should dial to reach them               |

Loopback suits a backend on the same machine as your browser. If the backend is remote and
you want previews reachable from your phone, you will need to widen the bind — and should
read what that means in [the security model](../concepts/security.md) first.

## The monitor

`MOONPHASE_MONITOR_INTERVAL` is how often the backend checks whether each running agent is
still working. Setting it to `0` disables the monitor, and with it notifications, activity
dots and budget alerts.

Twenty seconds is a reasonable default. It costs one SSH round trip per container per
sweep, not per session — a project with four agents in it is checked in one call.

## Upgrading

```bash
git pull
supabase db push          # or `supabase migration up` against your instance
pnpm --filter @moonphase/web build
```

Then restart the API. Migrations are additive and forward-only; none of them destroy data
you did not ask to remove.

## What is not here yet

- Organization invites and role management in the UI (the schema supports them)
- zrok tunnels for previews — today they are reachable from the backend host

See the [roadmap](../roadmap.md).
