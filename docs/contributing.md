# Contributing

## Getting set up

Follow [installation](getting-started/installation.md), then:

```console
$ ./scripts/dev.sh
```

## The layout

```text
apps/api        FastAPI backend — SSH, Docker orchestration, PTY bridge
apps/web        React + Vite + xterm.js frontend, and the PWA
apps/desktop    Electron shell around apps/web
infra/images    Container images projects run in
supabase        Migrations, RLS policies, local Supabase config
docs            This site
```

## Running the checks

Everything must pass before a change is worth reviewing.

=== "Backend"

    ```console
    $ cd apps/api
    $ .venv/bin/ruff check moonphase/ tests/
    $ .venv/bin/python -m pytest tests/ -q
    ```

=== "Frontend"

    ```console
    $ cd apps/web
    $ npx tsc --noEmit
    $ npx eslint src --max-warnings=0
    $ npx vitest run
    $ npx vite build
    ```

=== "Docs"

    ```console
    $ pip install -r docs/requirements.txt
    $ mkdocs serve
    ```

    CI builds with `--strict`, so a broken internal link fails the build.

## Testing against real things

Moonphase talks to SSH, Docker and `tmux`, and mocks of those agree with your assumptions
by construction. The suite therefore runs against real infrastructure:

- `test_rls.py` — tenancy isolation against a real Postgres with real policies
- `test_end_to_end.py` — SSH → Docker → tmux → PTY against a throwaway `sshd` container
- `scripts/smoke.py` — the whole stack, including that a second user sees none of it

Several of the most annoying bugs in this codebase were things a mock would have been
happy with: `tmux list-clients -a` is not a valid flag, `git rev-parse --abbrev-ref
origin/HEAD` prints to stdout while failing, and `grep` omits the filename when given
exactly one file. Prefer a test that would have caught those.

## Migrations

```console
$ supabase migration new some_name
$ supabase db push --local
```

Additive and forward-only. A migration that destroys data nobody asked to remove is not
one that should land.

Row-level security is written in the migration alongside the table, not bolted on later.
A table without policies is unreachable by the `authenticated` role, which is the right
default.

## Style

The codebase has a strong preference for comments that explain **why**, especially where
the reason is a bug that already happened once. `# -uall lists untracked files rather than
collapsing a new directory into one line` is worth keeping; `# get the files` is not.

Match the density and idiom of the file you are editing.

## Reporting a security issue

Open a private security advisory on the repository rather than a public issue.
