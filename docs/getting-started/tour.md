# A tour

Every screen, and what it is for. If you would rather read about one thing properly, the
guides go deeper.

## The home screen

Deliberately almost empty. Its whole job is to answer one question — *is anything waiting
for me?* — and most of the time the answer is no.

When something **is** waiting, the question appears with its options already parsed, so
you can answer it here rather than opening a project and finding a cursor.

<div class="moonphase-cards" markdown>

[<strong>Answering from anywhere</strong><span>How the prompt is parsed, and why only your own sessions appear.</span>](../guides/from-your-phone.md)

</div>

Underneath is a one-line usage strip: how full your current limit window is, and when it
resets. It disappears when nothing is running.

## The sidebar

Servers, then the projects on them, then the sessions in each project.

The coloured dot is the session's activity, checked continuously by the backend rather
than inferred from the last thing you saw:

| Colour | Meaning                                            |
| ------ | -------------------------------------------------- |
| Blue   | Working                                            |
| Amber  | Waiting for you                                    |
| Grey   | Idle — running, but not doing anything             |
| Dim    | Stopped                                            |

A session's name is greyed until you enter it. Entering is what connects it.

Click a **project** rather than a session and you get its session list, with
**New session** to add another and **close** to remove one. Several sessions in
one project is the ordinary way to work: each gets its own home, its own git
worktree and its own branch, so one can refactor while another chases a bug.
Closing one keeps its branch.

## Inside a session

### Feed

The transcript, rendered for reading. At the top, a summary counted from the transcript
itself:

> **Claude made 3 new files and changed 1 file.**
>
> *"Both servers are up and wired together correctly. Open this in your browser…"*

Counted, not generated — a summary written by a model could be wrong in ways its reader
has no way to check.

### Terminal

The real thing, over a WebSocket PTY bridge. Resizes, reconnects, and survives your
client crashing because the `tmux` session is the source of truth, not the connection.

If you are watching someone else's session it is read-only and says so.

### Changes

Two things that answer "what state is my project in":

- **Save points** — where you have saved, whether there is unsaved work, and a button to
  go back. [Guide →](../guides/save-points.md)
- **The diff** — every file this session has touched since it branched, committed or not.
  [Guide →](../guides/reviewing-changes.md)

### Your app

Whatever the container is listening on, named rather than numbered. Moonphase makes an
HTTP request to each port and uses the page's own `<title>`:

```text
Your app       port 5173    Get a public link
Todo           port 5174    Get a public link
Data service   port 8000    Get a public link
```

**Open** launches a window whose network is inside the container, so an app that calls
its own API at `http://localhost:8000` works without being written any particular way.
[Guide →](../guides/previews.md)

## Search

`⌘K` (or `Ctrl-K`) searches every transcript you own, across every project and machine.

Only your own sessions, and only what was actually *said* — matches inside tool calls are
dropped, because they read as false positives. [Guide →](../guides/search.md)

## Usage

Tokens and spend, counted from the harness's own transcripts. Which number leads depends
on how you pay: a subscription gets its limit window, an API key gets the bill.

[Guide →](../guides/usage-and-limits.md)

## Settings

| Tab               | What is in it                                                     |
| ----------------- | ----------------------------------------------------------------- |
| **Accounts**      | Connect Claude and GitHub once; every project uses it              |
| **Claude**        | Permissions, MCP servers and a global `CLAUDE.md` — as forms       |
| **Workspace**     | Git identity and environment variables                            |
| **Environments**  | Base images and setup commands, built on the server               |
| **Instance**      | The domain, who may sign in, and who has an account               |

**Instance** appears only for administrators of the instance — see
[people and access](../guides/people.md).

## Sharing

The **Share** button on a server or a project. Give someone access by email — the grant
waits for them if they have not signed up yet.

Sharing a project never shares the account behind it. [Guide →](../guides/sharing.md)
