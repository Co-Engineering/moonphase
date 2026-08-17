# Sharing

Two ways to give someone access, both from the **Share** button on a server or a project.

|                  | `Can view`                             | `Can use`                                        |
| ---------------- | -------------------------------------- | ------------------------------------------------ |
| **on a server**  | see the machine and how it is doing    | also create their own projects on it              |
| **on a project** | watch the feed and terminal, read-only | also type into it, answer prompts, start and stop |

Share by email. If they have not signed up yet the grant waits for them, so *"share it
and tell them to register"* works in that order.

## Sessions stay individual

This is the part people expect to work differently, so it is worth stating plainly:

**Sharing a project never shares the Claude subscription behind it.**

A session belongs to one person. A project can hold several. You drive your own and may
watch anyone's.

Each session gets:

- its own `HOME` inside the container — credentials, settings, history, git identity
- its own **git worktree**, on branch `moonphase/<session>`

So two people work the same repository without their agents overwriting each other,
merging is ordinary git, and commits carry the right author. Nobody's work ever runs on
somebody else's account.

Someone else's session is read-only for you, and the interface says so rather than
swallowing your keystrokes:

> **Read-only** — this is *alice*'s session. It runs on their Claude account, so only
> they can type into it. You can watch it live.
>
> **[Start my own session]**

## A project on a lent server is theirs

If you lend someone a server and they create a project on it, that project is **theirs** —
their organization, their Claude account, their transcript.

You see that it exists and can reclaim the resources. You do not get to read it.

## Shares never grant administration

Only the owner can bootstrap a server, test it, delete it, or decide who else gets in.
There is no share level that lets someone hand out further access.

## Access levels

What the UI calls *Can view* and *Can use* resolves in the database to one of four levels,
computed in SQL rather than in application code:

| Level   | Means                                                            |
| ------- | ---------------------------------------------------------------- |
| `admin` | Everything, including deleting it and managing its shares         |
| `write` | Use it: start, stop, type into it, create projects on it          |
| `read`  | Watch it                                                         |
| `host`  | You own the machine a project runs on, but not the project        |

`host` is deliberately not on the same scale as the others. Owning the metal gets you the
right to reclaim resources and nothing else.

Every one of these is enforced by row-level security policies, not by the API. A bug in a
route handler cannot widen them — see [the security model](../concepts/security.md).
