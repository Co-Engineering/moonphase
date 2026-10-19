"""Keeping the harness CLIs current inside project containers.

An environment image is built once per server and reused for as long as its
tag exists, so the Claude Code and OpenCode it carries are frozen at whatever
`latest` meant on that day. The harnesses' own self-updaters were the only
thing moving them forward, and they did it badly here: they install into npm's
prefix, which the image points at the shared home volume, every running agent
retried every half hour, and a single interrupted install left every later
attempt failing — each one after downloading the release into its session's
npm cache again, which is how one session's cache reached 2.5GB.

So Moonphase owns the install instead. Each managed package lives in the
container's own layer under INSTALL_ROOT/<slug>/<version>, and a `current`
symlink names the one to run. Sessions and Moonphase's own calls pick it by
`resolve_snippet`, login shells by PATH from PROFILE_PATH; both fall back to
the image's copy in IMAGE_BIN_DIR, which is never touched. The harness's own
updater is switched off through `Harness.managed_env`. The work happens in a
shell script run as root inside the container, started detached so nothing in
the API ever waits on npm. A running agent keeps the binary it started with;
it picks up a newer one when its session is next restarted.
"""

from __future__ import annotations

import shlex

import asyncssh

from . import docker_remote
from . import harness as harness_registry
from .config import get_settings
from .harness import Harness

# In the container layer, not the home volume: a copy there is shared by every
# container that ever mounts the volume and outlives the image it was built
# against, which is the mess this replaces.
INSTALL_ROOT = "/opt/moonphase/harness"
# Where the image's own copy lives — the fallback until the first update has
# finished, or if it never can. Left alone: root's own `npm i -g` owns it.
IMAGE_BIN_DIR = "/usr/local/bin"
SCRIPT_PATH = "/usr/local/sbin/moonphase-harness-update"
STATE_DIR = "/var/lib/moonphase"
# Sourced by login shells: tmux windows and the pane's fallback shell never
# see the launcher's exports or its choice of binary.
PROFILE_PATH = "/etc/profile.d/moonphase-harness.sh"
# Where the harnesses' own updaters used to install to (the image's
# npm_config_prefix), cleaned up by the script.
LEGACY_PREFIX = "/home/dev/.npm-global"
DEV_HOME = "/home/dev"
# A session's npm cache above this is almost entirely failed self-update
# downloads. It is only a cache, so dropping it costs a re-download at most.
NPM_CACHE_LIMIT_KB = 1024 * 1024


def package_slug(package: str) -> str:
    """A directory name for an npm package: `@scope/name` -> `scope-name`."""
    return package.lstrip("@").replace("/", "-")


def managed_packages() -> list[tuple[str, str]]:
    """(npm package, binary) for every harness Moonphase installs itself."""
    out: dict[str, str] = {}
    for h in harness_registry.available():
        if h.managed_package and h.managed_binary:
            out.setdefault(h.managed_package, h.managed_binary)
    return list(out.items())


def managed_env() -> dict[str, str]:
    """What every managed harness wants exported, merged."""
    out: dict[str, str] = {}
    for h in harness_registry.available():
        if h.managed_package and h.managed_binary:
            out.update(h.managed_env())
    return out


def binary_candidates(harness: Harness, command: list[str]) -> list[str]:
    """Absolute paths to try, in order, for the program `command` runs.

    Empty when `command` does not start with the harness's managed binary, so
    a launch command naming something else is never silently swapped out.
    """
    binary = harness.managed_binary
    if not binary or not command or command[0] != binary:
        return []
    candidates = []
    if harness.managed_package:
        slug = package_slug(harness.managed_package)
        candidates.append(f"{INSTALL_ROOT}/{slug}/current/bin/{binary}")
    candidates.append(f"{IMAGE_BIN_DIR}/{binary}")
    return candidates


def resolve_snippet(harness: Harness, command: list[str]) -> str:
    """sh that sets $harness_bin to what `command[0]` should run, or "".

    The first of `binary_candidates` that is executable, else the bare name.
    Empty when `command` does not start with the harness's managed binary.
    """
    candidates = binary_candidates(harness, command)
    if not candidates:
        return ""
    # By its real, versioned path: the update script runs as root without
    # CAP_SYS_PTRACE, so a process's cmdline is all it can see when deciding
    # whether an old version is still running.
    return (
        f"harness_bin={shlex.quote(command[0])}\n"
        f"for candidate in {' '.join(shlex.quote(c) for c in candidates)}; do\n"
        '    if [ -x "$candidate" ]; then\n'
        '        harness_bin=$(readlink -f -- "$candidate" 2>/dev/null) || harness_bin=\n'
        '        [ -n "$harness_bin" ] || harness_bin=$candidate\n'
        "        break\n"
        "    fi\n"
        "done\n"
    )


def managed_command(harness: Harness, command: list[str]) -> list[str]:
    """`command` as an argv that runs the binary a session would.

    For Moonphase's own calls into a container (`claude mcp list`), which go
    through neither the launcher nor a login shell. Unchanged for a command
    that is not the harness's managed binary.
    """
    resolve = resolve_snippet(harness, command)
    if not resolve:
        return list(command)
    exports = "".join(f"export {k}={shlex.quote(v)}\n" for k, v in harness.managed_env().items())
    return ["sh", "-c", f'{exports}{resolve}exec "$harness_bin" "$@"', "sh", *command[1:]]


def update_script() -> str:
    """The POSIX sh script the container runs as root.

    Usage: `moonphase-harness-update <hours> [<channel>] [force]`. It exits at
    once when another run holds the lock, or when the last run finished less
    than <hours> ago unless `force` is given. Every path is overridable from
    the environment so the tests can run it against temporary directories.
    """
    managed = managed_packages()
    bins = "".join(f" {shlex.quote(package_slug(p))} {shlex.quote(b)}" for p, b in managed)
    updates = f"write_profile{bins}\n" + "".join(
        f"update_one {shlex.quote(package_slug(p))} {shlex.quote(p)} {shlex.quote(b)}\n"
        for p, b in managed
    )
    legacy = "".join(f"clean_legacy {shlex.quote(p)} {shlex.quote(b)}\n" for p, b in managed)
    prunes = "".join(f'prune "$ROOT"/{shlex.quote(package_slug(p))}\n' for p, _ in managed)
    profile = "".join(f"export {k}={shlex.quote(v)}\n" for k, v in managed_env().items())
    return (
        _SCRIPT_HEAD.replace("@INSTALL_ROOT@", INSTALL_ROOT)
        .replace("@STATE_DIR@", STATE_DIR)
        .replace("@LEGACY_PREFIX@", LEGACY_PREFIX)
        .replace("@DEV_HOME@", DEV_HOME)
        .replace("@NPM_CACHE_LIMIT_KB@", str(NPM_CACHE_LIMIT_KB))
        .replace("@PROFILE_PATH@", PROFILE_PATH)
        .replace("@PROFILE_EXPORTS@\n", profile)
        + updates
        + legacy
        + "clean_caches\n"
        + prunes
        + _SCRIPT_TAIL
    )


async def trigger(conn: asyncssh.SSHClientConnection, container: str) -> bool:
    """Write the script into the container and start it, without waiting.

    Returns False when updates are switched off. Raises SSHError when the
    container could not be reached; callers decide how much that matters.
    """
    settings = get_settings()
    hours = settings.moonphase_harness_update_hours
    if hours <= 0:
        return False
    # Beside it, then renamed over it: a run still reading the old script
    # keeps the old file rather than finding new text at its old offset.
    staged = f"{SCRIPT_PATH}.new"
    await docker_remote.write_file(
        conn, container, staged, update_script(), user="root", mode="755"
    )
    renamed = await docker_remote.exec_capture(
        conn, container, ["mv", "-f", staged, SCRIPT_PATH], user="root", timeout=30
    )
    renamed.check("Installing the harness update script")
    await docker_remote.exec_detached(
        conn,
        container,
        [SCRIPT_PATH, f"{hours:g}", settings.moonphase_harness_channel],
        user="root",
    )
    return True


_SCRIPT_HEAD = r"""#!/bin/sh
# Generated by Moonphase (moonphase/harness_updates.py). Edits will be overwritten.
set -u
umask 022
# docker exec starts in the image's WORKDIR, the user's repo, and npm would
# take that repo's .npmrc (registry, auth, logs) for this root job.
cd / || exit 1

hours=${1:-6}
[ $# -gt 0 ] && shift
channel=latest
force=
for arg in "$@"; do
    case $arg in
        force) force=1 ;;
        *) channel=$arg ;;
    esac
done

ROOT=${MOONPHASE_HARNESS_ROOT:-@INSTALL_ROOT@}
STATE=${MOONPHASE_STATE_DIR:-@STATE_DIR@}
LEGACY=${MOONPHASE_LEGACY_PREFIX:-@LEGACY_PREFIX@}
DEV_HOME=${MOONPHASE_DEV_HOME:-@DEV_HOME@}
CACHE_LIMIT_KB=${MOONPHASE_NPM_CACHE_LIMIT_KB:-@NPM_CACHE_LIMIT_KB@}
PROC=${MOONPHASE_PROC:-/proc}
PROFILE=${MOONPHASE_PROFILE_FILE:-@PROFILE_PATH@}
# The image puts the dev user's home volume first on PATH; a root job should
# not run whatever happens to be there.
PATH=${MOONPHASE_UPDATE_PATH:-/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin}
export PATH
# The image sets npm's prefix to the home volume and a root exec inherits it,
# so without this an install would write root-owned files into it.
unset npm_config_prefix NPM_CONFIG_PREFIX npm_config_cache NPM_CONFIG_CACHE

mkdir -p "$STATE" "$ROOT" || exit 1
# Run detached, so a reason not to run goes in the log or nowhere. A missing
# flock in particular would otherwise read as "another run holds the lock".
case $channel in
    ''|*[!A-Za-z0-9._^~*+-]*)
        echo "moonphase: refusing channel '$channel'" > "$STATE/harness-update.log"
        exit 2 ;;
esac
if ! command -v flock >/dev/null 2>&1; then
    echo "moonphase: flock is not installed; not updating" > "$STATE/harness-update.log"
    exit 1
fi
exec 9>"$STATE/harness-update.lock"
flock -n 9 || exit 0

now=$(date +%s)
if [ -z "$force" ] && [ -f "$STATE/harness-update.stamp" ]; then
    last=$(cat "$STATE/harness-update.stamp" 2>/dev/null)
    case $last in ''|*[!0-9]*) last=0 ;; esac
    # Up to a minute short, so an ask that lands a moment early (SSH latency,
    # whole-second stamps) is not put off until the next one.
    interval=$(awk -v h="$hours" \
        'BEGIN { i = h * 3600; s = i / 10; if (s > 60) s = 60; printf "%d", i - s }')
    [ $(( now - last )) -lt "${interval:-0}" ] && exit 0
fi
# The start, not the end, so the interval does not also stretch by however
# long each run takes.
# Taken whatever the outcome, so a release that will not install is retried
# on the next interval, not by every session that starts in the meantime.
echo "$now" > "$STATE/harness-update.stamp"

exec >"$STATE/harness-update.log" 2>&1
echo "moonphase: harness update started $(date -u +%Y-%m-%dT%H:%M:%SZ), channel $channel"

work=$(mktemp -d) || exit 1
trap 'rm -rf "$work"' EXIT
trap 'exit 1' HUP INT TERM

overall=ok
status_json=

record() {
    # $1 package, $2 result. Only versions, the checked channel and fixed
    # words reach here, so nothing needs escaping.
    cur=$(readlink "$ROOT/$slug/current" 2>/dev/null || true)
    prev=$(readlink "$ROOT/$slug/previous" 2>/dev/null || true)
    extra=
    [ -n "$fallback" ] && extra=$(printf ',"fallback_from":"%s"' "$fallback")
    entry=$(printf '"%s":{"current":"%s","previous":"%s","channel":"%s","result":"%s"%s}' \
        "$1" "$cur" "$prev" "$used" "$2" "$extra")
    status_json=$status_json${status_json:+,}$entry
    [ "$2" = ok ] || overall=failed
    echo "moonphase: $1: $2 (current ${cur:-none})"
}

verify() {
    [ -x "$1/bin/$2" ] || return 1
    out=$(HOME="$work" timeout 60 "$1/bin/$2" --version 2>&1) || return 1
    echo "moonphase: $1/bin/$2 --version: $out"
}

write_profile() {
    # $@ slug/binary pairs. Tested at login, so a `current` that is missing or
    # broken leaves the image's copy to be found as before.
    mkdir -p "${PROFILE%/*}" || return 0
    tmp=$PROFILE.moonphase.$$
    if {
        cat <<'MOONPHASE_PROFILE'
# Generated by Moonphase (moonphase/harness_updates.py). Edits will be overwritten.
# Moonphase keeps these harnesses current; their own updaters would install a
# second copy into the home volume. Its copy goes first on PATH if it runs.
@PROFILE_EXPORTS@
MOONPHASE_PROFILE
        # Resolved at login, so a `claude` started from a shell runs by its
        # versioned path, which is how prune tells it is still in use.
        while [ $# -ge 2 ]; do
            printf 'd=$(readlink -f "%s" 2>/dev/null) && [ -x "$d/%s" ] && PATH="$d:$PATH"\n' \
                "$ROOT/$1/current/bin" "$2"
            shift 2
        done
        echo 'unset d'
        echo 'export PATH'
    } > "$tmp" && chmod 644 "$tmp" && mv -f "$tmp" "$PROFILE"; then
        return 0
    fi
    rm -f "$tmp"
    echo "moonphase: could not write $PROFILE"
}

resolve() {
    # $1 spec. Prints the version npm resolves it to; npm's errors are kept
    # in $work/view.err as well as logged.
    timeout 120 npm view "$1" version --cache "$cache" \
        --no-update-notifier --loglevel=error 2>"$work/view.err" \
        | sed -n '$p' | sed "s/.* '//; s/'.*//"
    cat "$work/view.err" >&2
}

is_version() {
    case $1 in ''|*[!A-Za-z0-9.+-]*) return 1 ;; esac
}

switch_to() {
    # $1 package dir, $2 version. Renaming a new link over the old one is
    # atomic, so a session starting mid-switch sees one version or the other.
    old=$(readlink "$1/current" 2>/dev/null || true)
    ln -sfn "$2" "$1/.current.$$" && mv -T "$1/.current.$$" "$1/current" || return 1
    if [ -n "$old" ] && [ "$old" != "$2" ]; then
        ln -sfn "$old" "$1/.previous.$$" && mv -T "$1/.previous.$$" "$1/previous"
    fi
    return 0
}

update_one() {
    slug=$1 pkg=$2 bin=$3
    dir=$ROOT/$slug
    used=$channel fallback=
    mkdir -p "$dir" || { record "$pkg" "failed: cannot create $dir"; return; }
    cache=$(mktemp -d "$work/npm.XXXXXX")
    version=$(resolve "$pkg@$channel")
    # One channel names both packages, so a version number or a tag only one
    # of them publishes must not stop the other updating. Only on npm's "no
    # such version": an unreachable registry is no reason to leave a pin.
    if ! is_version "$version" && [ "$channel" != latest ] \
            && grep -q -e E404 -e 'is not in this registry' "$work/view.err"; then
        echo "moonphase: $pkg has no $channel; following latest instead"
        used=latest fallback=$channel
        version=$(resolve "$pkg@latest")
    fi
    if ! is_version "$version"; then
        rm -rf "$cache"
        record "$pkg" "failed: could not resolve $used"
        return
    fi

    # A `.r` sibling is the same version, installed beside a broken copy.
    case $(readlink "$dir/current" 2>/dev/null) in
        "$version"|"$version".r[0-9]*)
            if [ -x "$dir/current/bin/$bin" ]; then
                rm -rf "$cache"
                record "$pkg" ok
                return
            fi ;;
    esac

    # Installed in place rather than staged and moved, in case a postinstall
    # records its own path. `current` never points here until it has run.
    target=$dir/$version
    if ! verify "$target" "$bin"; then
        # Never into a directory already there, working or not: `current`,
        # `previous` or a running session may be using it. prune removes it
        # once nothing is.
        # A dangling `current` or `previous` still names it.
        if [ -e "$target" ] || [ -L "$target" ] \
                || [ "$(readlink "$dir/current" 2>/dev/null)" = "$version" ] \
                || [ "$(readlink "$dir/previous" 2>/dev/null)" = "$version" ]; then
            target=$dir/$version.r$(date +%s)
            if ! mkdir "$target"; then
                rm -rf "$cache"
                record "$pkg" "failed: cannot create $target"
                return
            fi
        fi
        echo "moonphase: installing $pkg@$version into $target"
        if ! timeout 900 npm install -g --prefix "$target" --cache "$cache" \
                --no-audit --no-fund --no-update-notifier --loglevel=error \
                "$pkg@$version"; then
            rm -rf "$target" "$cache"
            record "$pkg" "failed: npm install $version"
            return
        fi
        chmod -R a+rX "$target"
        if ! verify "$target" "$bin"; then
            rm -rf "$target" "$cache"
            record "$pkg" "failed: $version does not run"
            return
        fi
    fi
    rm -rf "$cache"

    if ! switch_to "$dir" "${target##*/}"; then
        record "$pkg" "failed: could not switch to ${target##*/}"
        return
    fi
    record "$pkg" ok
}

clean_legacy() {
    # Exactly what the harnesses' own updaters left in the old prefix: the
    # package, npm's half-renamed copies of it, and its bin link. Nothing else
    # in there is ours.
    pkg=$1 bin=$2
    modules=$LEGACY/lib/node_modules
    case $pkg in
        */*) parent=$modules/${pkg%/*} name=${pkg#*/} ;;
        *) parent=$modules name=$pkg ;;
    esac
    # A session started before Moonphase managed the harness may still be
    # running that copy; it goes on a later run, once nothing is.
    if in_use "$modules/$pkg" "$modules/$pkg" "$LEGACY/bin/$bin"; then
        echo "moonphase: $LEGACY still in use for $pkg; leaving it for now"
        return 0
    fi
    rm -rf -- "$modules/$pkg" "$parent/.$name-"*
    rm -f -- "$LEGACY/bin/$bin"
}

clean_caches() {
    for c in "$DEV_HOME"/sessions/*/.npm/_cacache "$DEV_HOME"/.npm/_cacache; do
        [ -d "$c" ] || continue
        kb=$(du -sk "$c" 2>/dev/null | cut -f1)
        case $kb in ''|*[!0-9]*) continue ;; esac
        if [ "$kb" -gt "$CACHE_LIMIT_KB" ]; then
            rm -rf -- "$c"
            echo "moonphase: removed npm cache $c (${kb}KB)"
        fi
    done
}

in_use() {
    # $1 version dir, $2 the same with symlinks resolved, $3 optionally one
    # exact path (a bin link) that also counts. Root cannot read
    # another user's /proc/<pid>/exe without CAP_SYS_PTRACE, which Docker
    # drops, so the world-readable cmdline is what usually answers: the
    # launcher starts a harness by its resolved, versioned path for this.
    for p in "$PROC"/[0-9]*; do
        exe=$(readlink "$p/exe" 2>/dev/null) && case $exe in "$1"/*|"$2"/*) return 0 ;; esac
        [ -r "$p/cmdline" ] || continue
        if tr '\0' '\n' < "$p/cmdline" 2>/dev/null | {
            while IFS= read -r arg || [ -n "$arg" ]; do
                case $arg in "$1"/*|"$2"/*) exit 0 ;; esac
                [ -n "${3:-}" ] && [ "$arg" = "$3" ] && exit 0
            done
            exit 1
        }; then
            return 0
        fi
    done
    return 1
}

prune() {
    # Keeps whatever current and previous resolve to. Anything else goes
    # unless a process is still running a binary from it — a session that has
    # not been restarted since two releases ago.
    [ -d "$1" ] || return 0
    keep_current=$(cd "$1/current" 2>/dev/null && pwd -P) || keep_current=
    keep_previous=$(cd "$1/previous" 2>/dev/null && pwd -P) || keep_previous=
    for v in "$1"/*; do
        case ${v##*/} in current|previous) continue ;; esac
        [ -d "$v" ] || continue
        real=$(cd "$v" 2>/dev/null && pwd -P) || real=$v
        [ "$real" = "$keep_current" ] && continue
        [ "$real" = "$keep_previous" ] && continue
        if in_use "$v" "$real"; then
            echo "moonphase: keeping $v, still running"
            continue
        fi
        rm -rf -- "$v"
        echo "moonphase: removed $v"
    done
    rm -f -- "$1"/.current.* "$1"/.previous.*
}

"""

_SCRIPT_TAIL = r"""
printf '{"finished_at":"%s","channel":"%s","result":"%s","packages":{%s}}\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$channel" "$overall" "$status_json" \
    > "$STATE/harness-update.json.tmp" \
    && mv -f "$STATE/harness-update.json.tmp" "$STATE/harness-update.json"
echo "moonphase: harness update finished: $overall"
[ "$overall" = ok ]
"""
