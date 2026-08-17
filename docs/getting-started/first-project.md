# Your first project

A project is a container on one of your servers, with its own volumes, its own network,
and a coding harness running inside a `tmux` session that outlives every client.

## Create it

**+ New project** in the sidebar. You need:

- **A server** — any of yours that is online
- **A name** — becomes the container name and the project slug
- **A repository URL** *(optional)* — cloned on first start
- **An environment** *(optional)* — the base image and setup commands, see
  [environments](../guides/environments.md)

Moonphase pulls or builds the image, creates the volumes, starts the container and
launches the harness.

## Connect your Claude account

Do this once, in **Settings → Accounts**, and every project uses it.

=== "Claude subscription"

    Press **Connect**. Moonphase runs the sign-in on a PTY in a throwaway container,
    surfaces the URL for you to open, and types the code back.

    Your subscription is yours: every session runs on the account of the person who owns
    it, and sharing a project never shares the account behind it.

=== "API key"

    Paste an Anthropic API key. It is encrypted at rest with `MOONPHASE_SECRET_KEY`.

    With an API key the [usage screen](../guides/usage-and-limits.md) leads with spend
    rather than with how much of a window you have used, because that is the limit you
    actually have.

## First attach

Open the project and press **Enter** on a session. Nothing connects until you do — a
session you are not looking at costs nothing.

On first attach Claude Code asks whether you trust the workspace folder.

!!! note "That prompt is not pre-answered, on purpose"
    It guards against hostile content in a cloned repository. Answering it for you would
    remove the one check that exists at exactly the moment it matters, so it is your
    call, not Moonphase's.

## The three views

Once a session is running you get three ways to look at it:

| View         | For                                                                   |
| ------------ | --------------------------------------------------------------------- |
| **Feed**     | A readable transcript, with a plain-English summary on top. Works on a phone. |
| **Terminal** | The real PTY, in xterm.js. The only way to do anything unusual.        |
| **Changes**  | Save points, and a diff of everything this session has done.           |

The feed and the terminal drive the same `tmux` session, so anything you type in one
shows up live in the other.

## Now leave

That is the point. Close the lid.

Turn on notifications in **Settings → Notifications**, and you will be told the moment
the agent needs an answer — and be able to give it from the notification, without opening
a terminal.

[Working from your phone →](../guides/from-your-phone.md){ .md-button .md-button--primary }
[Take the tour →](tour.md){ .md-button }
