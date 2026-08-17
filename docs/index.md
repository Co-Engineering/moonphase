# Moonphase

<p class="moonphase-lede">
Your coding agents live on servers you own, not on the laptop you need to close.
</p>

Moonphase is a self-hosted control plane for remote AI coding sessions. You add a server
over SSH, create a project, and Moonphase provisions an isolated Docker container with a
coding harness running inside a persistent `tmux` session.

Attach from your desktop. Detach by shutting the lid. Reattach from your phone on the
train. The session never noticed.

```console
$ cp .env.example .env
$ ./scripts/dev.sh
```

## Why

An agent that can work for an hour is only useful if you can leave it for an hour. On a
laptop you cannot: closing the lid kills the process, the SSH session, and the context it
had built up. So you sit and watch it, which is the thing it was supposed to save you.

Moving the session onto a server you own fixes that, and creates three new problems that
Moonphase is mostly about solving:

- **You stop knowing when it needs you.** So Moonphase watches every session and pushes a
  notification the moment one starts waiting — and lets you answer from the notification.
- **You stop knowing what it did.** So there is a plain-English summary, a diff of the
  branch, and a search across every transcript you own.
- **You lose your undo.** So there are save points: a button that saves where you are and
  a button that puts you back, where going back never destroys anything.

## What you get

<div class="moonphase-cards" markdown>

[<strong>Sessions that survive</strong><span>A detached tmux session per project. Attach, detach and reattach from anywhere; nothing restarts.</span>](concepts/sessions.md)

[<strong>Notifications that mean something</strong><span>Push when an agent starts waiting, answerable in one tap from your phone.</span>](guides/from-your-phone.md)

[<strong>Save points</strong><span>An undo button for people who do not use git. Going back never destroys anything.</span>](guides/save-points.md)

[<strong>Previews that actually work</strong><span>A window whose network is inside the container, so an app calling its own API just works.</span>](guides/previews.md)

[<strong>Real sharing</strong><span>Lend a server or a project. Sessions stay individual — nobody works on your account.</span>](guides/sharing.md)

[<strong>Usage you can act on</strong><span>Tokens and spend counted from the harness's own transcripts, with limits and alerts.</span>](guides/usage-and-limits.md)

</div>

## How it fits together

```text
  phone ──┐
  laptop ──┼──►  ┌──────────────────────┐
  desktop ─┘     │  Moonphase backend   │  always on
                 │  FastAPI + Supabase  │
                 └──────────┬───────────┘
                            │ ssh
              ┌─────────────┼─────────────┐
           srv-a          srv-b         srv-c
         ┌────────┐     ┌────────┐   ┌────────┐
         │ docker │     │ docker │   │ docker │
         │ proj×3 │     │ proj×1 │   │ proj×2 │
         └────────┘     └────────┘   └────────┘
```

The backend is the only component that holds SSH credentials and the only one that talks
to your servers. Clients are thin: they authenticate against GoTrue and stream terminals
over a WebSocket.

[Read the architecture →](concepts/architecture.md){ .md-button }

## Self-hosted means self-hosted

There is no Moonphase service to sign up for. You run the backend, you point it at
machines you control, and your agents run on your own Claude account or API key. Nothing
about your code, your transcripts or your credentials passes through anyone else.

That also means the security decisions are yours, and the
[security model](concepts/security.md) documents exactly where the boundaries are.

## Status

**v0.1** — the full chain works end to end, verified against real infrastructure rather
than mocks. See the [roadmap](roadmap.md) for what has landed and what is next.

---

<div class="moonphase-cards" markdown>

[<strong>Install it →</strong><span>Docker, Node, pnpm, uv and the Supabase CLI. About five minutes.</span>](getting-started/installation.md)

[<strong>Take the tour →</strong><span>Every screen, and what it is for.</span>](getting-started/tour.md)

[<strong>Configuration reference →</strong><span>Every environment variable, and what happens if you get it wrong.</span>](reference/configuration.md)

</div>
