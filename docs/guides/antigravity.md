# Antigravity

Antigravity is Google's coding agent, and `agy` is the command-line version of it. In
Moonphase it is a harness beside Claude Code: a project can run it instead, and
nothing else about the project changes. `agy` is a terminal program like Claude
Code, so it runs in the same `tmux` session, over the same PTY bridge, with the
same attach and detach, its own `HOME` per session and its own git worktree. A
laptop closing is no more of an event for it than for Claude.

A project's harness is chosen once, in **New project → Harness**, and stays. Only
harnesses you have connected are offered there, so Antigravity appears once it is
connected below.

What this page says `agy` does was checked against version 1.4.3, the release
current when it was written, rather than taken from Google's documentation, which
disagrees with the program in a few places. Where it relies on Google's word
instead, it says so.

## Connect it

**Settings → Accounts → Antigravity.** Once, and every Antigravity project uses it.

=== "Sign in with Google"

    1. Press **Sign in with Google**. Moonphase starts `agy` in a throwaway
       container on one of your servers, picks Google sign-in on its first screen
       — the choice you made by pressing the button — and shows you the link it
       prints.
    2. Open the link and sign in to Google.
    3. Google sends you to a page on `antigravity.google` that shows a code, with
       a button to copy it. Copy it.
    4. Paste it back into Moonphase.

    Nothing has to listen on your machine for this to work: the code travels by
    your clipboard, which is why it works when the agent is on a server and the
    browser is on your phone. A code only works for the attempt that printed its
    link, so if one is refused, start again for a fresh link rather than pasting
    the same code twice.

    The sign-in is encrypted at rest with `MOONPHASE_SECRET_KEY` and written into
    each session's own `HOME` when it starts; `agy` renews it from there by itself.
    What you can use is whatever your Google account's plan allows — Google
    describes the tiers on its [plans page](https://antigravity.google/docs/plans).

    Google's FAQ suggests trying an `@gmail.com` address if a Google Workspace
    account will not sign in.

=== "Gemini API key"

    Press **Use a Gemini API key instead** and paste a key from
    [Google AI Studio](https://aistudio.google.com/apikey). It is encrypted at rest
    with `MOONPHASE_SECRET_KEY`.

    A key is billed through the Gemini API, not through a Google AI plan, and does
    not draw on a plan's allowance. Moonphase hands it to `agy` as `GEMINI_API_KEY`
    and sets `"modelProvider": "gemini"` in `agy`'s own settings file, because the
    key on its own is ignored — `agy` goes on asking for a Google sign-in until
    that setting is there too. Moving a session back to a Google account takes the
    setting away again.

More than one account works exactly as it does for Claude — **Add another
account**, then choose one per project or per session; see
[more than one account](../getting-started/first-project.md#more-than-one-account).
An Antigravity project only offers your Antigravity accounts. Switching restarts
`agy` on the other account and asks it to continue the same conversation.

## The first start in each session

Every session has its own `HOME` (see
[sessions](../concepts/sessions.md#sessions-are-individual)), so the first time a
session starts, `agy` behaves as if it had just been installed and asks three things
before it shows a prompt:

1. **A colour scheme.** Purely cosmetic, but `agy` records it together with the next
   screen as one step, so it cannot be skipped on its own.
2. **Terms of Service & Data Use.** Google's terms, and a checkbox agreeing to let
   Google collect and use your interactions data — **ticked by default**.
3. **Do you trust the contents of this project?** Asked once per folder, and every
   session's worktree is a folder of its own, so once per session. It guards against
   hostile content in a cloned repository, the same check as Claude Code's.

By default Moonphase answers none of them. They are answered with the arrow keys and
Enter, and Enter takes whatever is highlighted — so anything typed from the feed or
the home screen would pick the highlighted answer for you, and a "no" typed at the
trust question would trust the folder. Moonphase offers no buttons and no text box
for them, refuses to type into them, and points you at the **Terminal** instead.
After that the session's `HOME` remembers all three, so restarting the harness,
resuming after a reboot or switching accounts goes straight to the prompt.

### Letting Moonphase answer them

Adding your first Google account in **Settings → Accounts** asks whether Moonphase
may answer two of them for you in every new session. Both start off, each is its own
choice, and both stay on the Antigravity card to change later:

- **Accept Google's terms for me** marks `agy`'s onboarding as done
  (`~/.gemini/antigravity-cli/cache/onboarding.json`), which skips the terms screen
  and the colour picker with it. You agree to Google's terms in every session, and
  the screen's data-use checkbox is never shown to you, so that is not a choice you
  make.
- **Trust each session's folder** adds the session's worktree to
  `trustedWorkspaces` in `agy`'s `settings.json`. Turn it on only if you trust the
  repositories your projects clone.

Both are written only when the file is not there yet, which means a session's first
start. A session that has already asked keeps the answer it was given, and turning a
choice off later does not take an answer back.

## Auto Mode

`agy`'s **Auto Mode** (`--mode auto`, `/config`, or **Shift+Tab**) approves file
edits and safe commands without asking, and runs commands in its **terminal
sandbox**. On Linux that sandbox is built on unprivileged user namespaces, and
without it `agy` falls back from Auto Mode to asking about every command.

Two things decide whether the sandbox can start:

- **The server's kernel.** Ubuntu, from 23.10 on, only lets programs an AppArmor
  profile names create user namespaces
  (`kernel.apparmor_restrict_unprivileged_userns = 1`); `agy` then says
  `Terminal sandbox unavailable: AppArmor restricts unprivileged user namespaces`.
  That is a setting of the server's kernel, not of any container, so the server's
  page shows an **Antigravity Auto Mode** card to its owner whenever it is on.
  **Allow Auto Mode on this server** lifts the restriction for the whole machine —
  the default on Debian, Fedora and most other distributions — and writes
  `/etc/sysctl.d/60-moonphase-userns.conf` so a reboot keeps it. **Restore the
  restriction** puts it back. Both need passwordless sudo, the same as installing
  Docker.
- **The project's container.** Docker's default seccomp filter blocks creating user
  namespaces in an ordinary container, so the sandbox only starts in a project with
  **Docker access** turned on, which runs under Sysbox. Turn that on for an
  Antigravity project that should use Auto Mode.

`agy` checks once, when it starts, so a session picks either change up on its next
**Restart harness**.

## What it shares with Claude Code's configuration

Moonphase's configuration screens reach Antigravity too, at every scope — **Settings → Agent settings** for the
organization, a project's or a session's **⋯ menu → Configure** below that — layered
the same way, org then project then session:

| In Moonphase | What `agy` gets |
| ------------ | --------------- |
| Instructions — the **Instructions** tab in Settings, **Instructions** in an Antigravity project's or session's **Configure** | `~/.gemini/AGENTS.md` — every layer that sets some, concatenated, as for Claude Code |
| MCP servers | `~/.gemini/config/mcp_config.json` |
| Skills | `~/.gemini/config/skills/<name>/SKILL.md`, unchanged — `agy` reads the same folders and frontmatter |
| Environment variables | The session's environment, as for every harness |
| Permissions and behaviour | Nothing. It is Claude Code's `settings.json`, and `agy`'s settings are a different file with different keys, so **Configure** does not offer it for an Antigravity session |

`~` there is the session's own `HOME`, never the repository, so none of it shows up
in `git status`. `agy` still reads a repository's own `AGENTS.md` or `GEMINI.md`
as it normally would. It does not read `CLAUDE.md` at all — the name appears
nowhere in the program — so a `CLAUDE.md` committed to a repository reaches
Claude Code sessions only.

**MCP servers belong to Moonphase.** `mcp_config.json` is written on every session
start, so it always matches what Moonphase has, and a server removed in Moonphase
is gone from `agy` too. The other side of that: a server added with `agy mcp add`
inside a session works until the session next starts, and is then replaced. Add
it in Moonphase to keep it.

Two things on the MCP screens do not carry over. The **Connect** button for a
server that signs in with OAuth relays `claude mcp login`, and what it produces is
Claude Code's credential, so it signs `agy` in to nothing. And `agy`'s own
documentation says it does not support the older HTTP+SSE transport, so a server
added as SSE may not connect from an Antigravity session.

## The feed

Antigravity sessions have a [feed](from-your-phone.md#the-feed-not-the-terminal),
the same as Claude Code's: your messages, its replies and its thinking, every tool
call summarised in a line, edits with their diff, and the results that came back.
`agy` writes each conversation's steps to
`~/.gemini/antigravity-cli/brain/<conversation>/.system_generated/logs/transcript_full.jsonl`,
and Moonphase follows the conversation `agy` itself records as the current one for
the session's worktree, rather than whichever file changed last — which could be a
sub-agent's.

Two things behave differently from Claude Code, because `agy` writes a step only
once it has finished:

- **A step waiting on your permission appears after you answer.** The question
  itself is read from the screen, as Claude Code's is, so it still reaches the home
  screen, your notifications and the feed's answer buttons straight away. A reply
  interrupted half-way is never written at all.
- **Nothing appears before your first message.** `agy` only records which
  conversation is current once it has had one.

Answering is safe in one way that needed care. In `agy`, pressing an option's
number picks it and confirms it at once, so Moonphase sends only the number. An
Enter after it — what Claude Code needs — would land on whatever came next, and if
that was the next permission prompt, it would approve its default "Yes". The same
goes for a number you type rather than tap while a question is showing. And the
screens moved only with the arrow keys — the first-start questions above, and the
sign-in screen if a session's sign-in has lapsed — are left to the terminal for the
reason given there.

## Resuming

**Resume**, reattaching after the container restarted, and switching accounts all
start `agy --continue`, which reopens the last conversation in that session's
worktree. With none to reopen it quietly starts a new one, so it is safe on a
session that never got as far as a message.

**Duplicating a session starts a fresh conversation.** The duplicate gets the
branch and the exact commit, as always, but `agy` finds the conversation to reopen
by the absolute path of the folder it was in, and a duplicate's worktree is a
different folder.

## Updates

Moonphase keeps `agy` current the same way it keeps Claude Code current: each
project container gets its own install, a new release is installed in the
background and switched to only once it runs, and **Restart harness** waits for the
newest before it starts. The difference is where releases come from. `agy` is a
native program, not an npm package, and Moonphase reads the same release manifest
Google's own installer does, checking every download against the SHA-512 the
manifest gives.

A release is a 64MB download that unpacks to over 200MB, and most containers only
ever run Claude Code, so a container fetches `agy` only once a session in it has
been set up to run Antigravity — which happens just before that session's first
start asks for an update, so the first Antigravity session in a container starts on
the newest release. Until then the copy built into the image is the only one there.

`agy`'s own updater is switched off with `AGY_CLI_DISABLE_AUTO_UPDATE=true`, because
Moonphase manages the version, exactly as it does for Claude Code. The manifest only
ever names the current release, so `latest` is the only version there is to follow:
if `MOONPHASE_HARNESS_CHANNEL` pins something else, Claude Code honours the pin and
`agy` keeps following `latest`. The details are in
[harness updates](../reference/configuration.md#harness-updates).

## What does not work yet

- **Plan limits on the Dashboard.** It shows Claude accounts only. Google documents
  a `/usage` command inside `agy` itself, typed in the terminal, for your quota.
- **Usage and cost.** The Usage screen counts Claude Code's transcripts and nothing
  else, so Antigravity sessions are not on it.
- **Search and summaries.** Both read Claude Code's transcript format, so search
  finds nothing in an Antigravity session and its feed has no summary on top.
- **Signing in with a Google Cloud project.** `agy` offers it — and Application
  Default Credentials — but Moonphase only drives the personal Google sign-in. A
  Gemini API key is the alternative to a Google account for now.
- **Duplicating a session's conversation.** A duplicate starts fresh, as above.

## Google's terms

Google's [Antigravity FAQ](https://antigravity.google/docs/faq) says that wrapping
`agy`'s headless mode, `agy -p`, inside third-party coding agents or applications,
or automating account rotation, is not supported on consumer plans. The same FAQ
also says that using third-party software, tools or services to access Antigravity
breaks its Terms of Service, and recommends a Gemini Enterprise or Google AI Studio
API key for that instead.

What Moonphase does is run Google's own interactive `agy`, unmodified, in a terminal
on your own server, signed in as you. It never uses headless mode, and it never
rotates accounts: a session runs on the account you chose for it until you choose
another. How Google's terms apply to that is for Google to say, not Moonphase. If
you are unsure how they apply to you, use a Gemini API key.
