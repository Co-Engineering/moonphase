# Save points

An undo button for people who do not use git.

## The problem

What frightens someone who cannot read a diff is not that the agent will fail — it is
that it will succeed at the wrong thing and leave them with no way back.

They are right to be frightened. The agent commits when it feels like it, and everything
else lives in a worktree nobody has a handle on.

## Using them

In a session, open **Changes**.

- **Save this version** — name it something you will recognise, like *"login works"*. If
  you do not want to think of a name, it uses the date and time.
- **Go back to this** — puts every file back to how it was at that point.

The panel also tells you whether there is work you have not saved:

> **3 files** changed since your last save point.

## Going back never destroys anything

This is the rule that makes the feature safe to hand to someone who cannot inspect what
it did.

Restoring does three things in order:

1. Saves whatever is on disk right now as its own point, labelled *Before going back*
2. Puts the files back to the point you chose
3. Records that as a point too, labelled *Went back to: …*

So the undo has an undo. Every state your project has been in stays reachable, and the
confirmation dialog can promise you that and mean it.

```text
Went back to: Moonphase test point     ← you are here
Before going back            automatic
Moonphase test point
```

## Installed packages are left alone

Going back removes files that were created since the point — but not anything your
`.gitignore` covers. `node_modules`, a virtualenv, a build directory and your `.env` all
survive.

Without that, an undo would cost twenty minutes of reinstalling, and nobody would use it
twice.

## Underneath

`git commit` and `git restore`, in the session's own worktree, committed as the session's
own git identity. The words commit, branch, stash and reset never appear in the
interface, and neither does a hash.

Save points are marked with a trailer so the list stays a record of *your* decisions
rather than a changelog of everything the agent committed on its own.

If you do know git, nothing is hidden from you: it is an ordinary branch —
`moonphase/<session>` — with ordinary commits, and you can `git log` it from the
terminal.

!!! note "Only in your own sessions"
    Save points write files and commit as the session's owner, so they are only offered
    in sessions that are yours. Watching someone else's project does not make their files
    yours to move.
