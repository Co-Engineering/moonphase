# Search

`⌘K`, or `Ctrl-K`, or **Search** in the sidebar.

After a week of sessions the thing you remember is not which project it was in — it is a
phrase. *"Where did I tell it about the rate limiter."* Scrolling four transcripts to find
that is the work the transcript was supposed to save.

## What it searches

Every transcript in every session you own, across every project and every machine. The
matched phrase is highlighted in place:

```text
USER   fresh-demo · oliver-test   Aug 16, 8:46 PM
Make simple **fastapi** app with a react frontend. It should be a simple todo app…
```

Clicking a result opens the session it came from.

## What it does not search

**Other people's sessions.** A shared project does not make someone else's conversation
yours to read, and this is enforced by which sessions come back from the database rather
than by filtering afterwards.

**Tool calls.** A match inside a `grep` command or a file path is not something the reader
will see in the result, so showing that row would read as a false positive. Only what was
actually said counts.

## Why it searches on Enter

Each keystroke would be a `grep` across every container you own, over SSH. A deliberate
search is both cheaper and closer to how the question actually arrives — you do not
half-remember a phrase one letter at a time.

## Where the searching happens

On your machines, in the files that already exist. Transcripts are the record, they are
already there, and mirroring every message into Postgres would double the storage to
answer a query that runs a few times a day.

Two commands per container: one to find which lines matched, one to fetch those lines.

## Partial results

If one machine does not answer in time you get what the others found, and a note:

> One machine did not answer in time, so this list may be missing hits from it.

An unreachable server should not hide results from the ones that are up.
