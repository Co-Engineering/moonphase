#!/usr/bin/env sh
#
# Apply an update when the API asks for one.
#
# The API runs in a container and the update is `docker compose pull && up -d`
# on the host, which needs the host's Docker daemon. Giving the API that would
# hand the internet-facing component root-equivalent control of the machine, so
# it does not get it: this does, it does nothing else, and it is opt-in.
#
# No port, no HTTP, no token. It watches a file in a volume it shares with the
# API, which is the same shape as the auth container watching its generated
# config — a pattern already in this stack, and one where the only way to ask
# for anything is to be a container that was given the volume.
#
# What it can be asked to do is exactly one thing. There is no command in the
# request file and nothing is read out of it: its contents are a nonce, and the
# only question asked of it is whether it changed.
set -eu

REQUESTS=/updates
REQUEST="$REQUESTS/request"
STATUS="$REQUESTS/status"
PROJECT="${MOONPHASE_PROJECT_DIR:-/project}"
POLL_SECONDS="${MOONPHASE_UPDATE_POLL:-3}"

say() { printf '%s updater: %s\n' "$(date -u +%H:%M:%S)" "$*"; }

status() {
  # Written atomically: the API may read this at any moment, and half a status
  # file is a worse answer than a stale one.
  printf '%s\n' "$1" > "$STATUS.tmp" && mv "$STATUS.tmp" "$STATUS"
}

mkdir -p "$REQUESTS"

command -v docker >/dev/null 2>&1 || {
  say "no docker CLI in this image"
  status "failed|no docker CLI in the updater image"
  exit 1
}

if [ ! -f "$PROJECT/docker-compose.yml" ]; then
  say "no compose project mounted at $PROJECT"
  status "failed|the updater has no compose project mounted at $PROJECT"
  exit 1
fi

say "watching for update requests"

# Whatever is there at startup is already dealt with. Otherwise every restart of
# this container — including the one an update itself causes — would re-run the
# update that started it, forever.
seen=""
[ -f "$REQUEST" ] && seen="$(cat "$REQUEST" 2>/dev/null || true)"

# A status left saying "running" means we were stopped mid-update. That used to
# be the expected ending — `up -d` recreated this container along with the
# rest — and the assumption behind it was wrong: Compose was killed partway
# through its own run, so the services it had not reached yet were left
# created and never started. An instance came back with no database.
#
# Services are now brought up without this one (see below), so being restarted
# mid-update is no longer normal. If it happens anyway, the update is of
# unknown outcome rather than done.
if [ -f "$STATUS" ] && [ "${STATUS_KEEP:-}" != "1" ]; then
  case "$(cat "$STATUS" 2>/dev/null || true)" in
    running*) status "failed|the updater stopped part way through; run docker compose up -d" ;;
  esac
fi

while true; do
  sleep "$POLL_SECONDS"

  [ -f "$REQUEST" ] || continue
  current="$(cat "$REQUEST" 2>/dev/null || true)"
  [ -n "$current" ] || continue
  [ "$current" != "$seen" ] || continue
  seen="$current"

  say "update requested"

  # Where the project actually lives, on the host.
  #
  # Compose is being run from inside a container, and the daemon it talks to
  # resolves bind mounts against the *host's* filesystem. A relative source
  # like `./docker/Caddyfile` became `/project/docker/Caddyfile` — this
  # container's view — which does not exist out there, so Docker created it as
  # an empty directory and the proxy died trying to mount a directory onto a
  # file. The auth container went the same way and the instance was
  # unreachable.
  #
  # Asked of the daemon rather than configured, so an install that predates
  # this needs no new setting.
  host_dir=$(docker inspect "$(cat /etc/hostname)" \
    --format '{{range .Mounts}}{{if eq .Destination "'"$PROJECT"'"}}{{.Source}}{{end}}{{end}}' \
    2>/dev/null || true)
  if [ -z "$host_dir" ]; then
    say "could not find the project's path on the host"
    status "failed|the updater could not work out where the project lives on the host"
    continue
  fi

  # Everything runs in a throwaway container that holds the project at the
  # same path the host does, so every relative path means the same thing in
  # both places. It is also not part of the compose project, so nothing the
  # update does can stop it half way through.
  compose() {
    docker run --rm \
      -v /var/run/docker.sock:/var/run/docker.sock \
      -v "$host_dir:$host_dir" \
      -w "$host_dir" \
      docker:28-cli docker compose "$@" 2>&1
  }

  status "running|pulling images"
  if ! output=$(compose pull); then
    say "pull failed"
    status "failed|$(printf '%s' "$output" | tail -3 | tr '\n' ' ')"
    continue
  fi

  status "running|restarting services"

  # Everything except this container.
  #
  # A plain `docker compose up -d` includes the updater, so Compose stopped the
  # container running the very command it was executing. The process died with
  # it, part way down the list: the API was replaced, the database was recreated
  # and never started, and the instance came back answering 500 with no `db` to
  # resolve. The status file said "running" forever.
  #
  # Asking Compose for the service list keeps this correct whichever overlay
  # files are in play, rather than naming services here and going stale.
  services=$(compose config --services 2>/dev/null | grep -vx updater | tr '\n' ' ')
  if [ -z "$(printf '%s' "$services" | tr -d ' ')" ]; then
    say "could not list services"
    status "failed|could not read the compose project's service list"
    continue
  fi

  # shellcheck disable=SC2086 - a deliberate word list.
  if ! output=$(compose up -d $services); then
    say "up failed"
    status "failed|$(printf '%s' "$output" | tail -3 | tr '\n' ' ')"
    continue
  fi

  # `up -d` returning says the containers were created and started, not that
  # they stayed up. An update that leaves a service crash-looping reported
  # "ok|updated" and looked like a success from the settings screen while the
  # instance was unreachable — which is exactly how one went unnoticed.
  sleep 10
  broken=$(compose ps --format '{{.Service}} {{.State}}' 2>/dev/null \
    | grep -vE ' (running|exited)$' | awk '{print $1}' | tr '\n' ' ')
  if [ -n "$(printf '%s' "$broken" | tr -d ' ')" ]; then
    say "some services did not settle: $broken"
    status "failed|these did not come up: $broken"
    continue
  fi

  say "update applied"
  # This container is deliberately left on its old image: updating it would
  # mean stopping it mid-command, which is the fault this avoids. It picks the
  # new one up at the next `docker compose up -d` run by hand.
  status "ok|updated"
done
