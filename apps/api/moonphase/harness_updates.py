"""Keeping the harness CLIs current inside project containers.

An environment image is built once per server and reused for as long as its
tag exists, so the Claude Code, OpenCode and Antigravity it carries are frozen
at whatever `latest` meant on that day. The harnesses' own self-updaters were
the only thing moving them forward, and they did it badly here: they install
into npm's prefix, which the image points at the shared home volume, every
running agent retried every half hour, and a single interrupted install left
every later attempt failing — each one after downloading the release into its
session's npm cache again, which is how one session's cache reached 2.5GB.

So Moonphase owns the install instead. Each managed harness lives in the
container's own layer under INSTALL_ROOT/<slug>/<version>, and a `current`
symlink names the one to run. Sessions and Moonphase's own calls pick it by
`resolve_snippet`, login shells by PATH from PROFILE_PATH; both fall back to
the image's copy in IMAGE_BIN_DIR, which is never touched. The harness's own
updater is switched off through `Harness.managed_env`. The work happens in a
shell script run as root inside the container, started detached so nothing in
the API ever waits on a download. A running agent keeps the binary it started
with; it picks up a newer one when its session is next restarted.

Releases come from one of two places, and only finding and fetching one
differs; the layout, switching, pruning and fallback are shared. An npm
package (`Harness.managed_package`: Claude Code, OpenCode) is resolved with
`npm view` and installed with `npm install -g --prefix`. A native binary
published through a release manifest (`Harness.managed_manifest`: agy) is read
the way Google's own installer reads it — one small JSON file per platform
naming the current version, a tarball and its SHA-512 — downloaded, checked
against that checksum and unpacked into place. Such a manifest only ever names
the newest release, so for these the channel is `latest` or nothing.
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
# What a release manifest's tarball holds: the binary alone, named for the
# product rather than the command — `antigravity`, installed as `agy` — exactly
# as Google's install.sh extracts it. Checked against the 1.4.3 tarballs for
# linux amd64, arm64 and amd64 musl: that one member and nothing else. It is the
# only manifest format there is, so it is a constant here rather than something
# every harness declares.
MANIFEST_MEMBER = "antigravity"


def package_slug(package: str) -> str:
    """A directory name for an npm package: `@scope/name` -> `scope-name`."""
    return package.lstrip("@").replace("/", "-")


def install_slug(harness: Harness) -> str | None:
    """The directory under INSTALL_ROOT a harness is kept in, or None when
    Moonphase does not install it.

    An npm package's is named after the package, as it always was. A harness
    released through a manifest has no package name, so its is named after
    its kind: `antigravity`. The same word keys it in harness-update.json,
    where an npm harness is keyed by its package name.
    """
    if not harness.managed_binary:
        return None
    if harness.managed_package:
        return package_slug(harness.managed_package)
    if harness.managed_manifest:
        return str(harness.kind)
    return None


def managed_packages() -> list[tuple[str, str]]:
    """(npm package, binary) for every harness Moonphase installs from npm."""
    out: dict[str, str] = {}
    for h in harness_registry.available():
        if h.managed_package and h.managed_binary:
            out.setdefault(h.managed_package, h.managed_binary)
    return list(out.items())


def managed_manifests() -> list[tuple[str, str, str, str | None]]:
    """(slug, manifest base URL, binary, marker) for every harness Moonphase
    installs from a release manifest rather than from npm. The marker is
    `Harness.managed_marker`: what tells the script a container uses it."""
    out: dict[str, tuple[str, str, str | None]] = {}
    for h in harness_registry.available():
        if h.managed_manifest and h.managed_binary and not h.managed_package:
            out.setdefault(str(h.kind), (h.managed_manifest, h.managed_binary, h.managed_marker))
    return [(slug, base, binary, marker) for slug, (base, binary, marker) in out.items()]


def managed_env() -> dict[str, str]:
    """What every managed harness wants exported, merged."""
    out: dict[str, str] = {}
    for h in harness_registry.available():
        if install_slug(h):
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
    slug = install_slug(harness)
    if slug:
        candidates.append(f"{INSTALL_ROOT}/{slug}/current/bin/{binary}")
    candidates.append(f"{IMAGE_BIN_DIR}/{binary}")
    return candidates


def wait_snippet() -> str:
    """sh that waits for an update already running in the container.

    A new session starts the update just before it launches, so without this
    a restart meant to pick up a release launched the old copy while the new
    one was still installing. The pause before looking lets the update take
    its lock; when nothing is due it gives the lock back at once.
    """
    lock = f"{STATE_DIR}/harness-update.lock"
    return (
        f"if [ -x {SCRIPT_PATH} ] && command -v flock >/dev/null 2>&1; then\n"
        "    sleep 1\n"
        f"    if [ -e {lock} ] && ! flock -n -s {lock} true 2>/dev/null; then\n"
        "        printf '[moonphase] updating the harness before it starts...\\n'\n"
        f"        flock -s -w 300 {lock} true 2>/dev/null || true\n"
        "    fi\n"
        "fi\n"
    )


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

    The outcome goes to STATE_DIR/harness-update.json, one entry per harness
    under `packages`: an npm harness keyed by its package name, a manifest
    one by its slug (`antigravity`), since it has no package name.
    """
    managed = managed_packages()
    manifests = managed_manifests()
    # npm's first, as they always ran, so a slow 64MB agy download delays only
    # agy's own switch. It does not shorten a restart's wait (`wait_snippet`):
    # that waits for the lock, which is held for the whole run.
    slugs = [(package_slug(p), b) for p, b in managed] + [(s, b) for s, _, b, _ in manifests]
    bins = "".join(f" {shlex.quote(s)} {shlex.quote(b)}" for s, b in slugs)
    updates = (
        f"write_profile{bins}\n"
        + "".join(
            f"update_one {shlex.quote(package_slug(p))} {shlex.quote(p)} {shlex.quote(b)}\n"
            for p, b in managed
        )
        + "".join(
            f"update_manifest {shlex.quote(s)} {shlex.quote(base)} {shlex.quote(b)}"
            f" {shlex.quote(MANIFEST_MEMBER)}"
            + (f" {shlex.quote(marker)}" if marker else "")
            + "\n"
            for s, base, b, marker in manifests
        )
    )
    # Only npm left anything in the old prefix: no harness's own updater ever
    # installed a manifest release there.
    legacy = "".join(f"clean_legacy {shlex.quote(p)} {shlex.quote(b)}\n" for p, b in managed)
    prunes = "".join(f'prune "$ROOT"/{shlex.quote(s)}\n' for s, _ in slugs)
    exports = "".join(f"export {k}={shlex.quote(v)}\n" for k, v in managed_env().items())
    return (
        _SCRIPT_HEAD.replace("@INSTALL_ROOT@", INSTALL_ROOT)
        .replace("@STATE_DIR@", STATE_DIR)
        .replace("@LEGACY_PREFIX@", LEGACY_PREFIX)
        .replace("@DEV_HOME@", DEV_HOME)
        .replace("@NPM_CACHE_LIMIT_KB@", str(NPM_CACHE_LIMIT_KB))
        .replace("@PROFILE_PATH@", PROFILE_PATH)
        .replace("@MANAGED_EXPORTS@\n", exports)
        .replace("@PROFILE_EXPORTS@\n", exports)
        + updates
        + legacy
        + "clean_caches\n"
        + prunes
        + _SCRIPT_TAIL
    )


async def trigger(
    conn: asyncssh.SSHClientConnection, container: str, *, check_now: bool = False
) -> bool:
    """Write the script into the container and start it, without waiting.

    `check_now` skips the once-per-interval gate, so the registry is asked
    whatever the last check found; an up-to-date container still installs
    nothing.

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
        [
            SCRIPT_PATH,
            f"{hours:g}",
            settings.moonphase_harness_channel,
            *(["force"] if check_now else []),
        ],
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
# Every managed harness's own updater switched off for this job too, not only
# for sessions: verify runs each new binary as root, and one that took its
# `--version` for an ordinary start would set its updater off to rewrite the
# very copy just installed — as root, so nothing would stop it. agy 1.4.3 does
# not start its updater for `--version` (checked against the binary), but
# nothing promises the next release will not, and the root-owned directory
# that stops it for a session is no obstacle to root.
@MANAGED_EXPORTS@

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
    # `unused`: a harness nobody in this container runs, left undownloaded.
    case $2 in ok|unused) ;; *) overall=failed ;; esac
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

platform() {
    # The release's platform name, worked out as Google's installer does it:
    # linux_<amd64|arm64>, with _musl on a musl libc. Fails on any other
    # machine, which it publishes nothing for.
    case $(uname -m 2>/dev/null) in
        x86_64|amd64) arch=amd64 ;;
        aarch64|arm64) arch=arm64 ;;
        *) return 1 ;;
    esac
    libc=
    if [ -f /lib/libc.musl-x86_64.so.1 ] || [ -f /lib/libc.musl-aarch64.so.1 ] \
            || ldd /bin/ls 2>&1 | grep -q musl; then
        libc=_musl
    fi
    echo "linux_$arch$libc"
}

fetch() {
    # $1 url, $2 file, $3 seconds an attempt may take, and the window retries
    # may start in: the lock is held throughout, and a stuck download should
    # not keep every later run out for an hour. HTTPS only, redirects
    # included, so neither the manifest nor a redirect can send a download
    # anywhere in the clear.
    curl -fsSL --proto '=https' --proto-redir '=https' --connect-timeout 30 \
        --max-time "$3" --retry 3 --retry-delay 5 --retry-max-time "$3" \
        -o "$2" "$1"
}

manifest_field() {
    # $1 key, $2 manifest. It is pretty-printed JSON, one key to a line; read
    # with the very sed Google's installer uses, so any manifest that installs
    # with it installs here too.
    sed -n 's/.*"'"$1"'"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$2" | head -n 1
}

in_some_home() {
    # $1 a path under a session's HOME. The shared home first, for sessions
    # from before sessions had homes of their own.
    for h in "$DEV_HOME" "$DEV_HOME"/sessions/*; do
        [ -e "$h/$1" ] && return 0
    done
    return 1
}

update_manifest() {
    # $1 slug, $2 manifest base URL, $3 binary, $4 the tarball member that is
    # the binary, $5 optionally a path under a session's HOME that exists once
    # a session there runs it. Recorded under the slug, as there is no package
    # name.
    slug=$1 base=$2 bin=$3 member=$4 marker=${5:-}
    dir=$ROOT/$slug
    used=$channel fallback=
    # Hundreds of megabytes a release, into every container — including the
    # many that will only ever run Claude Code. So not until a session here
    # has been set up to run it (which happens before its launch asks for an
    # update), or Moonphase already keeps a copy here. Until then the image's
    # own copy is what any session would get.
    if [ -n "$marker" ] && [ ! -e "$dir/current" ] && ! in_some_home "$marker"; then
        record "$slug" unused
        return
    fi
    if ! command -v curl >/dev/null 2>&1; then
        record "$slug" "failed: curl is not installed"
        return
    fi
    if ! plat=$(platform); then
        echo "moonphase: $slug publishes nothing for $(uname -m 2>/dev/null)"
        record "$slug" "failed: unsupported architecture"
        return
    fi
    mkdir -p "$dir" || { record "$slug" "failed: cannot create $dir"; return; }
    if ! dl=$(mktemp -d "$work/$slug.XXXXXX"); then
        record "$slug" "failed: cannot create a download directory"
        return
    fi
    if ! fetch "$base/$plat.json" "$dl/manifest.json" 60; then
        rm -rf "$dl"
        record "$slug" "failed: could not fetch the $plat manifest"
        return
    fi
    version=$(manifest_field version "$dl/manifest.json")
    url=$(manifest_field url "$dl/manifest.json")
    sha512=$(manifest_field sha512 "$dl/manifest.json")
    # Nothing from the manifest reaches the log or the record until it has
    # been checked to be what it should be.
    if ! is_version "$version"; then
        rm -rf "$dl"
        record "$slug" "failed: the $plat manifest names no version"
        return
    fi
    case $url in
        https://?*) ;;
        *)
            rm -rf "$dl"
            record "$slug" "failed: the $plat manifest names no https download"
            return ;;
    esac
    case $sha512 in *[!0-9A-Fa-f]*) sha512= ;; esac
    if [ ${#sha512} -ne 128 ]; then
        rm -rf "$dl"
        record "$slug" "failed: the $plat manifest names no SHA-512"
        return
    fi
    # The manifest only ever names the newest release, so the one channel it
    # can honour besides `latest` is that very version. Anything else — a
    # Claude Code version number, an npm tag, an older release — is followed
    # as `latest`, and recorded as such, as for an npm package that has no
    # such tag or version. An unreachable manifest never gets here: that is no
    # reason to leave a pin.
    if [ "$channel" != latest ] && [ "$channel" != "$version" ]; then
        echo "moonphase: $slug offers only $version, not $channel; following latest instead"
        used=latest fallback=$channel
    fi

    # A `.r` sibling is the same version, installed beside a broken copy.
    case $(readlink "$dir/current" 2>/dev/null) in
        "$version"|"$version".r[0-9]*)
            if [ -x "$dir/current/bin/$bin" ]; then
                rm -rf "$dl"
                record "$slug" ok
                return
            fi ;;
    esac

    target=$dir/$version
    if ! verify "$target" "$bin"; then
        # Never into a directory already there, working or not, exactly as for
        # an npm package: `current`, `previous` or a running session may be
        # using it, and a dangling `current` or `previous` still names it.
        if [ -e "$target" ] || [ -L "$target" ] \
                || [ "$(readlink "$dir/current" 2>/dev/null)" = "$version" ] \
                || [ "$(readlink "$dir/previous" 2>/dev/null)" = "$version" ]; then
            target=$dir/$version.r$(date +%s)
        fi
        echo "moonphase: installing $slug $version into $target"
        if ! fetch "$url" "$dl/release.tar.gz" 900; then
            rm -rf "$dl"
            record "$slug" "failed: could not download $version"
            return
        fi
        # From inside the directory, by a fixed name, so no path ever has to
        # survive sha512sum's own line format.
        if ! (cd "$dl" && printf '%s  release.tar.gz\n' "$sha512" | sha512sum -c -); then
            rm -rf "$dl"
            record "$slug" "failed: $version does not match its SHA-512"
            return
        fi
        if ! mkdir "$target"; then
            rm -rf "$dl"
            record "$slug" "failed: cannot create $target"
            return
        fi
        # Only the one member, streamed straight into place rather than
        # unpacked beside the tarball first: it is a 222MB binary, and this is
        # the container's own disk. Anything but a regular file there (a link,
        # a directory) streams nothing, and an empty file is refused.
        if ! mkdir "$target/bin" \
                || ! tar -xzOf "$dl/release.tar.gz" "$member" > "$target/bin/$bin" \
                || ! [ -s "$target/bin/$bin" ] \
                || ! chmod 0755 "$target/bin/$bin"; then
            rm -rf "$target" "$dl"
            record "$slug" "failed: could not unpack $version"
            return
        fi
        rm -rf "$dl"
        chmod -R a+rX "$target"
        if ! verify "$target" "$bin"; then
            rm -rf "$target"
            record "$slug" "failed: $version does not run"
            return
        fi
    fi
    rm -rf "$dl"

    if ! switch_to "$dir" "${target##*/}"; then
        record "$slug" "failed: could not switch to ${target##*/}"
        return
    fi
    record "$slug" ok
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
