#!/bin/sh
# Align the in-container docker group with the gid that owns the mounted host
# socket, so the `deploy` user can drive Docker the way a real server's user
# would. sshd re-initialises supplementary groups from /etc/group at login, so
# `docker run --group-add` alone would not reach the SSH session.
set -e

SOCKET=/var/run/docker.sock

if [ -S "$SOCKET" ]; then
  gid=$(stat -c '%g' "$SOCKET")
  if ! getent group "$gid" >/dev/null 2>&1; then
    groupadd -g "$gid" hostdocker
  fi
  group_name=$(getent group "$gid" | cut -d: -f1)
  usermod -aG "$group_name" deploy
  echo "[fake-server] deploy added to group $group_name (gid $gid)"
else
  echo "[fake-server] warning: $SOCKET not mounted; docker probes will fail"
fi

exec /usr/sbin/sshd -D -e
