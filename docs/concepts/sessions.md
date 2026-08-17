# Sessions

The load-bearing idea. Everything else in Moonphase follows from getting this right, so
it is worth being precise.

Inside each project container, one **detached tmux session** owns the harness
process. Nothing about that session belongs to a client:

```
container
└── tmux server (detached, survives everything)
    └── session "moonphase"
        └── launch-moonphase.sh      ← sources credentials, sets job control
            └── claude               ← owns the terminal foreground group
```

Attaching runs `docker exec -it … tmux attach` on a fresh SSH channel with a
PTY. Detaching closes that channel. The tmux server — and the harness — never
notice. Closing a laptop, losing a train tunnel, and quitting the app are all
the same event, and none of them interrupt work.

Two details in that diagram are load-bearing and were found by testing rather
than reasoning:

* **`set -m` in the launcher.** A non-interactive shell does not put its child
  in a new process group, so the wrapper and the harness would share the
  terminal's foreground group. Ctrl-C would then kill the wrapper, close the
  pane, and destroy the session — precisely the thing the design promises not
  to do. Job control gives the harness its own group.
* **The wrapper does not `exec`.** When the harness exits, the wrapper prints a
  message and drops to a shell, keeping the pane and therefore the session
  alive. A crashed agent leaves something to reattach to.

## Two clients, one session

The desktop attaches to the real PTY. The phone renders a readable feed parsed
from the harness's own JSONL transcript, and writes back with `tmux send-keys`.

Both surfaces therefore drive the *same* tmux session. There is no second
protocol to keep in sync and no risk of the two views disagreeing: a permission
prompt answered on a phone appears in the desktop terminal as if typed there.

The phone deliberately does **not** attach a terminal, for two reasons. An
80-column TUI on a 390-pixel screen is unusable. And tmux sizes a window to its
most recent client, so a phone attaching would squeeze the desktop down to
phone width — the feed observes without ever becoming a client.

That second point has a sharp edge worth knowing about. `docker exec` does not
kill the process it started when its client disconnects, so a closed terminal
leaves `tmux attach` running inside the container forever. Those phantom
clients accumulate and keep constraining the window size. The attach wrapper
therefore announces its tty before tmux takes the screen, and the bridge
consumes that line and detaches itself explicitly on the way out.

### Sessions are navigable, and connect only when opened

Sessions used to be a tab strip inside a project, which was wrong in two ways.
A session is the thing you are actually looking at, so it belongs where
everything else you navigate to lives — the sidebar, where several projects'
sessions can be visible at once, which tabs could never show. And opening a
project attached a terminal and a feed immediately, spending an SSH channel and
a tmux client before anyone had asked to look at anything.

Now a project opens to a list, nothing is connected, and entering a session is
what attaches. Listing costs one database query for every session the caller
can see (`GET /api/sessions`) and touches no server at all — a machine that is
asleep should not make the sidebar slow or empty. The attached-device count
does need tmux, so it is opt-in via `?live=true` and asked for only when you
are looking at a session and the number means something.

**Several at once is an operating system problem.** People run more than one
agent and want to see them side by side, so a session can be opened in a window
of its own rather than into panes and splitters we would have to build. A
tiling window manager arranges those across as many monitors as there are, and
a plain one lets you drag them where you like. The same URL works as a browser
popup, and renders the same components — the window is an entry point, not a
second implementation.

### Sessions are individual

Sharing a project shares the code and the machine. It must not share the coding
subscription behind them — that is a licensing question before it is a billing
one, and "whose account is this running on" should never be ambiguous.

So a session belongs to exactly one person, a project may hold several, and the
rule is simply: **you drive your own sessions and may watch anyone's.** Typing
into someone else's is refused, because their harness is authenticated as them.

Isolation is by `HOME`. Each session gets `/home/dev/sessions/<name>/`, which is
enough to separate a harness's credentials, settings, history and transcripts
*and* `~/.gitconfig` in one move — without depending on any particular tool
honouring any particular override variable. (Claude Code has no
`CLAUDE_CONFIG_DIR` in the version we ship against; `HOME` works for every
harness that will ever exist.) `git config` runs with `GIT_CONFIG_GLOBAL`
pointed there rather than `--global`, which would resolve to the container's
shared home and let the last session to start decide who everybody commits as.

Each session also gets a **git worktree** at `<home>/work`, on a branch named
`moonphase/<session>`. `/workspace` stays the repository. Two agents editing one
checkout would overwrite each other mid-thought and the damage would be
invisible until something failed to build; with worktrees, sharing work is a
merge, which is a problem git already solved. Closing a session removes its
checkout and keeps its branch, because that branch may hold the only copy of
something.

A session's home and workdir are fixed when it is created and recorded on the
row. Moving a running session would point it at a directory its harness has
never seen and orphan its real state, so only a restart — which recreates it
anyway — adopts a new layout. That is also the upgrade path for sessions made
before sessions had owners.

A viewer attached to a terminal gets `tmux attach -r -f ignore-size`. The
`-r` is the read-only part; `ignore-size` is there because a viewer who cannot
type could otherwise still squeeze the window for whoever is driving. The
guarantee that actually holds is server-side, though: the PTY bridge drops
inbound keystrokes for a non-writable client rather than trusting tmux to.

### One connection is not enough

asyncssh multiplexes channels over a single TCP connection, and sshd allows ten
concurrent channels on one (`MaxSessions 10`). Everything Moonphase does against
a server therefore competed for those ten: an attached terminal, a feed
following a transcript, the activity monitor, port detection — and **one channel
for every TCP connection a preview tunnel carries**, which is six or more for a
single page load.

Past ten, `create_process` fails with `ChannelOpenError("open failed")`, the
terminal stops working, and nothing on screen suggests the cause was something
else being busy.

Rather than rewrite someone's sshd config, the pool holds several connections
per server and hands them out round-robin, growing up to a ceiling when they
all fill. Both paths that open channels recover from a refusal by moving to
another connection: `pool.create_process` for long-lived ones, and `ssh.run`
via `pool.another()`, because it is handed a connection rather than a target
and cannot otherwise ask for a different one.

## Surviving a reboot

A managed server restarts — maintenance, power, someone typing `reboot`. The
containers come back on their own, because they are started with
`--restart unless-stopped`. Everything *inside* them does not: tmux is gone,
and with it the agent and its conversation.

Two things follow, and Moonphase used to get both wrong by saying nothing. The
record has to match the machine: the monitor is the only thing that looks at
every project regularly, so it reconciles project status from what the
container actually reports. A project claiming to be running while its
container is stopped offers a terminal, a Stop button and a green dot for
something that does not exist.

And a container that came back with nothing running in it is a state of its own
— genuinely running, genuinely empty — worth naming rather than reporting as
either half. Sessions in that state offer **Resume**, which starts the harness
with `--continue` so it reopens the conversation it was having instead of a
blank prompt in the right directory. Restarting without that would be
technically a recovery and practically a loss.

Resuming is asked of the harness rather than assumed: `launch_spec(resume=...)`
is part of the seam, and an agent that cannot resume ignores it and starts
normally.

