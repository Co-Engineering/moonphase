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
container's own layer under INSTALL_ROOT/<slug>/<version>, a `current` symlink
names the one sessions launch (see `binary_candidates`), and the harness's own
updater is switched off through `Harness.managed_env`. The work happens in a
shell script run as root inside the container, started detached so nothing
in the API ever waits on npm. A running agent keeps the binary it started with;
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
# finished, or if it never can.
IMAGE_BIN_DIR = "/usr/local/bin"
SCRIPT_PATH = "/usr/local/sbin/moonphase-harness-update"
STATE_DIR = "/var/lib/moonphase"
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


def update_script() -> str:
    """The POSIX sh script the container runs as root.

    Usage: `moonphase-harness-update <hours> [<channel>] [force]`. It exits at
    once when another run holds the lock, or when the last run finished less
    than <hours> ago unless `force` is given. Every path is overridable from
    the environment so the tests can run it against temporary directories.
    """
    managed = managed_packages()
    updates = "".join(
        f"update_one {shlex.quote(package_slug(p))} {shlex.quote(p)} {shlex.quote(b)}\n"
        for p, b in managed
    )
    legacy = "".join(f"clean_legacy {shlex.quote(p)} {shlex.quote(b)}\n" for p, b in managed)
    prunes = "".join(f'prune "$ROOT"/{shlex.quote(package_slug(p))}\n' for p, _ in managed)
    return (
        _SCRIPT_HEAD.replace("@INSTALL_ROOT@", INSTALL_ROOT)
        .replace("@STATE_DIR@", STATE_DIR)
        .replace("@LEGACY_PREFIX@", LEGACY_PREFIX)
        .replace("@DEV_HOME@", DEV_HOME)
        .replace("@NPM_CACHE_LIMIT_KB@", str(NPM_CACHE_LIMIT_KB))
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
    await docker_remote.write_file(
        conn, container, SCRIPT_PATH, update_script(), user="root", mode="755"
    )
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

if [ -z "$force" ] && [ -f "$STATE/harness-update.stamp" ]; then
    last=$(cat "$STATE/harness-update.stamp" 2>/dev/null)
    case $last in ''|*[!0-9]*) last=0 ;; esac
    interval=$(awk -v h="$hours" 'BEGIN { printf "%d", h * 3600 }')
    [ $(( $(date +%s) - last )) -lt "${interval:-0}" ] && exit 0
fi

exec >"$STATE/harness-update.log" 2>&1
echo "moonphase: harness update started $(date -u +%Y-%m-%dT%H:%M:%SZ), channel $channel"

work=$(mktemp -d) || exit 1
trap 'rm -rf "$work"' EXIT
trap 'exit 1' HUP INT TERM

overall=ok
status_json=

record() {
    # $1 package, $2 result. Only versions and fixed words reach here, so
    # nothing needs escaping.
    cur=$(readlink "$ROOT/$slug/current" 2>/dev/null || true)
    prev=$(readlink "$ROOT/$slug/previous" 2>/dev/null || true)
    entry=$(printf '"%s":{"current":"%s","previous":"%s","result":"%s"}' \
        "$1" "$cur" "$prev" "$2")
    status_json=$status_json${status_json:+,}$entry
    [ "$2" = ok ] || overall=failed
    echo "moonphase: $1: $2 (current ${cur:-none})"
}

verify() {
    [ -x "$1/bin/$2" ] || return 1
    out=$(HOME="$work" timeout 60 "$1/bin/$2" --version 2>&1) || return 1
    echo "moonphase: $1/bin/$2 --version: $out"
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
    mkdir -p "$dir" || { record "$pkg" "failed: cannot create $dir"; return; }
    cache=$(mktemp -d "$work/npm.XXXXXX")
    version=$(timeout 120 npm view "$pkg@$channel" version --cache "$cache" \
        --no-update-notifier --loglevel=error | sed -n '$p' | sed "s/.* '//; s/'.*//")
    case $version in
        ''|*[!A-Za-z0-9.+-]*)
            rm -rf "$cache"
            record "$pkg" "failed: could not resolve $channel"
            return ;;
    esac

    target=$dir/$version
    if [ "$(readlink "$dir/current" 2>/dev/null)" = "$version" ] && [ -x "$target/bin/$bin" ]
    then
        rm -rf "$cache"
        record "$pkg" ok
        return
    fi

    # Installed in place rather than staged and moved, in case a postinstall
    # records its own path. `current` never points here until it has run.
    if ! verify "$target" "$bin" >/dev/null; then
        echo "moonphase: installing $pkg@$version"
        rm -rf "$target"
        if ! timeout 900 npm install -g --prefix "$target" --cache "$cache" \
                --no-audit --no-fund --no-update-notifier --loglevel=error \
                "$pkg@$version"; then
            rm -rf "$target" "$cache"
            record "$pkg" "failed: npm install $version"
            return
        fi
        chmod -R a+rX "$target"
    fi
    rm -rf "$cache"

    if ! verify "$target" "$bin"; then
        rm -rf "$target"
        record "$pkg" "failed: $version does not run"
        return
    fi
    if ! switch_to "$dir" "$version"; then
        record "$pkg" "failed: could not switch to $version"
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
    for p in "$PROC"/[0-9]*; do
        exe=$(readlink "$p/exe" 2>/dev/null) || continue
        case $exe in "$1"/*) return 0 ;; esac
    done
    return 1
}

prune() {
    # Keeps current and previous. Anything older goes unless a process is
    # still running a binary from it — a session that has not been restarted
    # since two releases ago.
    [ -d "$1" ] || return 0
    keep_current=$(readlink "$1/current" 2>/dev/null || true)
    keep_previous=$(readlink "$1/previous" 2>/dev/null || true)
    for v in "$1"/*; do
        name=${v##*/}
        case $name in current|previous) continue ;; esac
        [ -d "$v" ] || continue
        [ "$name" = "$keep_current" ] && continue
        [ "$name" = "$keep_previous" ] && continue
        if in_use "$v"; then
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
# Stamped whatever the outcome: a release that will not install is retried on
# the next interval, not by every session that starts in the meantime.
date +%s > "$STATE/harness-update.stamp"
echo "moonphase: harness update finished: $overall"
[ "$overall" = ok ]
"""
