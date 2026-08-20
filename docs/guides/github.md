# Git and GitHub

An agent that cannot clone your repository or push its work is an agent you have
to copy files out of by hand. Connecting GitHub once fixes that for every
project, now and in the future — you are never asked for it per project.

**Settings → Accounts → GitHub.**

## The two ways

=== "Personal access token"

    Works on every instance, needs nothing configured on the server, and is
    what most people should use.

    1. Open **[github.com/settings/tokens](https://github.com/settings/tokens)**
    2. Generate a new token — classic, or
       [fine-grained](https://github.com/settings/personal-access-tokens)
    3. Give it the access in the table below
    4. Copy it — GitHub shows it once
    5. Paste it into **Settings → Accounts → GitHub → Use a personal access
       token**

    The exact buttons move around, so
    [GitHub's own instructions](https://docs.github.com/en/authentication/keeping-your-account-secure/managing-your-personal-access-tokens)
    are the ones to follow for the clicking. What Moonphase needs is the scopes.

=== "One-click sign-in"

    Available only if whoever runs the instance has registered a GitHub OAuth
    app for it and set `MOONPHASE_GITHUB_CLIENT_ID`. Then the GitHub button runs
    a device flow: it shows a short code, you approve it on github.com, and
    nothing is pasted anywhere.

    If the button is absent, the instance has no OAuth app and the token is your
    route. The screen says so rather than leaving you looking for it.

## Which scopes

| Scope | Why |
| ----- | --- |
| `repo` | Clone, pull and push. Private repositories need it; without it a public repo clones and a push fails |
| `read:org` | Only if the repository belongs to an organization — without it that repo is invisible to the token |
| `workflow` | Only if you want the agent to add or change files under `.github/workflows`. GitHub refuses those pushes otherwise, with a message that does not mention scopes |

The one-click flow asks for all three. For a token, `repo` alone is enough for a
personal repository, and it is the smaller thing to hand over.

!!! tip "Fine-grained tokens work too"
    GitHub's newer fine-grained tokens are fine here — give them **Contents:
    read and write** on the repositories you want, plus **Workflows: read and
    write** if the agent should touch CI. They expire on a date you choose,
    which classic tokens need not, and that is a good reason to prefer them.

!!! danger "Give it an expiry"
    A token with no expiry is a permanent key to your code sitting in a
    database. Moonphase encrypts it at rest and never shows it back to you, but
    the right lifetime for something like this is months, not never. Reconnect
    when it lapses; it takes a minute.

## What the agent gets

Once connected, every session gets the token three ways, so nothing has to be
told about it:

- **A git credential helper**, so `git clone`, `pull` and `push` just work
- **`GH_TOKEN` and `GITHUB_TOKEN`**, so `gh` works without being handed anything
- **URL rewriting**, so `git@github.com:you/thing.git` and `git://` addresses
  become authenticated HTTPS — a repository the agent finds mentioned in a
  README clones without a second credential

Each of those is written into the session's own `HOME`, so two people working in
one project push as themselves and neither can use the other's token.

## Cloning a repository into a project

Paste the URL into **Repository (optional)** when you create the project. SSH,
HTTPS and `git://` forms all work, because they are rewritten to authenticated
HTTPS on the way.

Leave it blank and you get an empty workspace — clone into it later from the
terminal, or let the agent do it.

## Committing as you

Set **Settings → Workspace → Git identity** and every session commits with that
name and email. Without it git uses whatever the container's default is, and the
history ends up attributed to `dev@` some container hostname.

!!! question "Who does a commit belong to when two people share a project?"
    Whoever made it. Each session has its own `HOME` and its own git config, and
    [each has its own worktree and branch](../concepts/sessions.md) — so two
    agents in one project commit separately, to separate branches, as separate
    people.

## When it goes wrong

**`fatal: could not read Username for 'https://github.com'`** — no credential
reached the container. GitHub is not connected, or the session started before it
was. Restarting the harness picks it up.

**`remote: Permission to you/repo.git denied`** — the token is valid and lacks
`repo`, or the repository belongs to an organization and the token lacks
`read:org`.

**`refusing to allow a Personal Access Token to create or update workflow`** —
exactly what it says, and the fix is the `workflow` scope. Nothing else about
the push is wrong.

**A push that hangs** — usually a repository whose SSH URL was not rewritten
because it is not on github.com. Other hosts work over HTTPS with their own
token; the rewriting is GitHub-specific.
