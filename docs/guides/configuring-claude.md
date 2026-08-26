# Configuring Claude Code

Everything here is set once, per organization, and materialised into every session's
`HOME` when it starts.

**Settings → Claude.**

## Permissions and behaviour

Claude Code's `settings.json`, as a form.

Permission rules are rows — a decision, a tool, and an optional pattern — which serialise
to the `Tool(pattern)` strings the file expects:

| Decision | Tool   | Pattern             | Becomes                    |
| -------- | ------ | ------------------- | -------------------------- |
| Allow    | `Bash` | `npm run test:*`    | `Bash(npm run test:*)`     |
| Deny     | `Write`| `./secrets/**`      | `Write(./secrets/**)`      |
| Ask      | `WebFetch` |                 | `WebFetch`                 |

A rule with no pattern covers the whole tool; a pattern narrows it.

The default mode — what happens when Claude wants to do something you have no rule for —
is a dropdown:

- **Ask each time** (the default)
- **Accept file edits automatically**
- **Plan mode** — read-only until you approve
- **Bypass all permission prompts**

!!! warning "Bypass means bypass"
    Every tool runs without asking, including commands that delete files or reach the
    network. Reasonable in a throwaway container — and this is one — but it is your
    account and your repository inside it.

## MCP servers

The `mcpServers` key of `~/.claude.json`, as a card per server. Pick the transport and
you are shown only the fields it needs:

=== "Local process"

    A command Claude runs and talks to over stdio.

    - **Command** — `npx`
    - **Arguments** — `-y @modelcontextprotocol/server-filesystem /home/dev/sessions`
    - **Environment** — key/value pairs

    Arguments are split respecting quotes, so a path with a space in it survives.

=== "HTTP / SSE"

    A remote server Claude calls over the network.

    - **URL** — `https://example.com/mcp`
    - **Headers** — key/value pairs

There are one-click templates for the common ones, because nobody should have to remember
`@modelcontextprotocol/server-filesystem` from memory.

!!! tip "Secrets belong in Workspace"
    Values you put in an MCP server's environment are written into the container as
    written. Anything secret belongs in **Settings → Workspace → Environment variables**,
    which is encrypted at rest.

## Two things that make the forms safe

**Unknown keys survive.** `hooks`, `statusLine`, a per-server `timeout` — anything the
form cannot show is preserved exactly, and the form says so rather than presenting itself
as the whole file:

> Also in this file and kept as-is: `hooks`, `statusLine`. Use *Edit as JSON* to change
> them.

An editor that silently drops what it does not understand is worse than a textarea.

**The JSON is still there.** Every editor has an **Edit as JSON** tab showing the exact
file, and it will not save until it parses. Structure is a convenience, not a cage.

## Global CLAUDE.md

Written to `~/.claude/CLAUDE.md`, so it applies to every project rather than needing a
copy in each repository. Preferences that are about *you* rather than about a codebase
belong here.

## When it takes effect

On session start. An already-running session keeps the configuration it launched with —
press **Restart harness** to pick up changes.
