"""Keeping a server's disk from filling with what can be thrown away.

Nothing here touches a project's work: only caches and temporary files that
whatever made them makes again when it needs them. Each of them grew without
bound before this, and together they filled a 256 GB worker:

* `/tmp` in every project container. It is in the container's own layer, which
  lives as long as the container — months — and nothing ever emptied it.
  Claude Code keeps its background tasks' output and its scratchpads there,
  and an agent pointing a dev server's log at a scratchpad left one 17 GB file
  behind. Files untouched for a week go, and any file over a gigabyte that has
  not been written to for a day.
* Go's build cache, per session HOME. Go trims only entries unused for five
  days, so a session building every day keeps all of it (36 GB in one).
  Trimmed to a cap, least recently used first: Go touches an entry each time
  it uses one.
* Next.js's cache (`.next/cache`) under a project or a session's HOME, which
  has no limit at all in development (27 GB in one). Removed whole once over
  the cap; Next rebuilds it.
* On the server: Docker's build cache from environment builds, and
  environment images no container uses any more — every recipe change and
  every environment edit builds a new one, and the old ones stayed.

Run by the monitor, once a day per server.
"""

from __future__ import annotations

import logging

import asyncssh

from . import docker_remote, ssh

log = logging.getLogger(__name__)

# A cache directory may grow to this before it is trimmed.
CACHE_CAP_KB = 5 * 1024 * 1024
# How long a file in /tmp may sit untouched.
TMP_DAYS = 7
# A file this big in /tmp goes once it has not been written to for a day.
TMP_BIG = "+1G"
# Docker build cache younger than this is kept: an environment being
# rebuilt again soon builds from it.
BUILD_CACHE_KEEP = "168h"
# An image built within this long may be one a project is about to be
# created from, its container not yet made.
IMAGE_GRACE_SECONDS = 24 * 3600

# Run as root inside each project container. tmux keeps its socket in
# /tmp/tmux-<uid>; sockets are not regular files, and the directory is never
# empty while a server is up, so neither find below can remove it.
CONTAINER_SCRIPT = f"""
CAP_KB={CACHE_CAP_KB}
find /tmp -xdev -mindepth 1 -type f \\
    \\( -mtime +{TMP_DAYS} -o \\( -size {TMP_BIG} -mmin +1440 \\) \\) -delete 2>/dev/null
find /tmp -xdev -mindepth 1 -depth -type d -empty -mtime +{TMP_DAYS} ! -name 'tmux-*' \\
    -delete 2>/dev/null

trim() {{
    d="$1"; [ -d "$d" ] || return 0
    size=$(du -xsk "$d" | cut -f1)
    [ "$size" -gt "$CAP_KB" ] || return 0
    find "$d" -type f -printf '%T@ %k %p\\n' | sort -n |
        awk -v over=$((size - CAP_KB)) \\
            '{{ if (freed >= over) exit; print substr($0, index($0, $3)); freed += $2 }}' |
        while IFS= read -r f; do rm -f -- "$f"; done
    echo "trimmed $d"
}}
for d in /home/dev/.cache/go-build /home/dev/sessions/*/.cache/go-build; do trim "$d"; done

find /home/dev /workspace -xdev -maxdepth 6 -type d -path '*/.next/cache' 2>/dev/null |
    while IFS= read -r d; do
        if [ "$(du -xsk "$d" | cut -f1)" -gt "$CAP_KB" ]; then
            rm -rf -- "$d" && echo "removed $d"
        fi
    done
true
"""

# Run on the server. Only Moonphase's own environment images, and only those
# neither a container (running or stopped) uses nor built in the last day.
SERVER_SCRIPT = f"""
docker builder prune -af --filter until={BUILD_CACHE_KEEP} >/dev/null 2>&1
used=" $(docker ps -a --format '{{{{.Image}}}}' | sort -u | tr '\\n' ' ') "
now=$(date +%s)
docker images --format '{{{{.Repository}}}}:{{{{.Tag}}}}' | grep '^moonphase/env-' |
    while IFS= read -r img; do
        case $used in *" $img "*) continue ;; esac
        created=$(docker image inspect -f '{{{{.Created}}}}' "$img" 2>/dev/null) || continue
        at=$(date -d "$created" +%s 2>/dev/null) || continue
        [ $((now - at)) -gt {IMAGE_GRACE_SECONDS} ] || continue
        docker rmi "$img" >/dev/null 2>&1 && echo "removed $img"
    done
docker image prune -f >/dev/null 2>&1
true
"""


async def clean_container(conn: asyncssh.SSHClientConnection, container: str) -> str:
    result = await docker_remote.exec_capture(
        conn, container, ["sh", "-c", CONTAINER_SCRIPT], user="root", timeout=1800
    )
    return result.stdout.strip()


async def clean_server(conn: asyncssh.SSHClientConnection) -> str:
    result = await ssh.run(conn, SERVER_SCRIPT, timeout=1800)
    return result.stdout.strip()
