# Environments

What a project's container is made of.

An environment is a base image plus a list of setup commands. Moonphase builds it **on the
server**, so nothing has to be pushed to a registry and the build happens where the layers
will be used.

**Settings → Environments.**

## Defining one

| Field       | Example                                             |
| ----------- | --------------------------------------------------- |
| Name        | `Python + Node`                                     |
| Base image  | `debian:bookworm-slim`                              |
| Setup       | `apt-get update && apt-get install -y python3 nodejs npm` |

Moonphase adds what it needs on top of whatever you choose — the harness, `tmux`, `git`,
and the plumbing that makes sessions work — so your setup script only has to describe
your project's own dependencies.

## Choosing one

Pick it when you create a project. Existing projects keep the environment they were
created with; changing it means recreating the container, which is deliberate — an
environment change that silently destroyed a running session's state would be a bad
surprise.

## Built-in environments

Ship with Moonphase and cannot be edited or deleted, only used as a starting point. They
are marked **built in** in the list.

## Why not a Dockerfile

You can express most of one, and the fields map onto exactly the two lines of a Dockerfile
that anyone actually changes. Full Dockerfile support would mean shipping a build context,
which means deciding whose machine it comes from — and the answer is not obviously the
laptop that happens to be open.

If you need more than this, build an image yourself and give its name as the base.

## Where the work lives

A project has two named volumes: one for the workspace and one for `HOME`. Rebuilding an
environment replaces the image, not the volumes, so your code and your session state
survive.

Inside `HOME`, each session gets its own directory — see [sessions](../concepts/sessions.md)
for why that matters.
