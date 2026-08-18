#!/bin/sh
#
# Run GoTrue, and restart it when its configuration changes.
#
# GoTrue reads everything from the environment at startup, so turning on Google
# sign-in would normally mean editing a file on the server and restarting a
# container. That is exactly what the setup screen exists to avoid.
#
# So the API writes a generated environment file into a volume both containers
# share, and this watches it. When it changes, the process exits and Docker
# restarts the container — which re-reads the file on the way up.
#
# Exiting rather than reloading in place is deliberate: GoTrue has no reload,
# and a supervisor that tried to fake one would be a second thing to get wrong.
# `restart: unless-stopped` already does this correctly.
set -eu

CONFIG=/config/auth.env

fingerprint() {
  [ -f "$CONFIG" ] && md5sum "$CONFIG" | cut -d' ' -f1 || echo none
}

if [ -f "$CONFIG" ]; then
  echo "auth: loading generated configuration"
  set -a
  # shellcheck disable=SC1090
  . "$CONFIG"
  set +a
else
  echo "auth: no generated configuration yet, using the environment"
fi

before=$(fingerprint)

auth &
pid=$!

# Poll rather than inotify: the image has no inotify tools, the file changes a
# handful of times in a deployment's life, and five seconds is imperceptible
# next to signing in.
while kill -0 "$pid" 2>/dev/null; do
  sleep 5
  if [ "$(fingerprint)" != "$before" ]; then
    echo "auth: configuration changed, restarting"
    kill "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
    exit 0
  fi
done

wait "$pid"
