# Reviewing changes

The feed says what the agent did and the terminal says what it is doing. Neither answers
the question you have after leaving it alone for an hour, which is *what is different
now* — and the honest answer to that is a diff.

## The Changes view

Open a session and pick **Changes**.

```text
moonphase/oliver-test   vs main   +2761  −0   27 files
```

You get the branch, the point it forked from, the totals, and every file it touched.
Click a file to open its patch.

## Uncommitted work counts

The diff is taken against **where the branch left the base**, and it includes the working
tree — not just what happens to be committed.

That matters more than it sounds. An agent that has written twenty files and committed
none has still changed twenty files, and a review screen that showed nothing until it
committed would be worse than not having one.

Files git has never seen are listed as `NEW`. They have no diff to show, because there is
nothing to compare them against.

!!! tip "Save first, then review"
    Pressing [**Save this version**](save-points.md) turns untracked files into tracked
    ones, which is what gives them diffs. A brand-new project goes from *27 files, no
    diffs* to a reviewable `+2761`.

## Against the base, not against now

Moonphase compares against the merge base — where your branch and `main` last agreed —
rather than against `main` as it is today.

Work someone else landed while your agent was running is not your session's doing, and
attributing it would make every review noisy in exactly the situation where several
people share a project.

## Large changes

The patch is capped. A runaway refactor cannot push megabytes down to a phone, and when
the cap is hit the view says so rather than quietly showing you a fraction:

> The patch was cut short — this is a large change. Open the session to see all of it.

The file list and the counts are always complete; only the patch body is bounded.

## Not a git repository

A scratch project with no repository is a normal state, not an error. The view says so
and stops, rather than showing an empty diff that looks like nothing happened.
