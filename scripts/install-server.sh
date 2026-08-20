#!/usr/bin/env sh
#
# Install Moonphase on a server, from your own machine.
#
#   curl -fsSL https://raw.githubusercontent.com/oliversvane/moonphase/main/scripts/install-server.sh | sh
#
# Asks for the address, a username, and a key or password, then does the rest
# over SSH: installs Docker if the machine has none, generates every secret,
# brings the stack up and tells you where to open it.
#
# Nothing needs to be installed on the server first, and nothing but `ssh` on
# this machine. Your password, if you use one, is typed to `ssh` itself and
# never passes through this script — there is nowhere here it could be stored,
# because it is never held.
#
# Safe to run twice. Running it again upgrades an existing install and keeps
# every secret, because regenerating MOONPHASE_SECRET_KEY would make every SSH
# credential it holds unreadable.
set -eu

REPO_RAW="${MOONPHASE_RAW:-https://raw.githubusercontent.com/oliversvane/moonphase/main}"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
info() { printf '\033[34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33m warn\033[0m %s\n' "$*"; }
die() { printf '\033[31merror\033[0m %s\n' "$*" >&2; exit 1; }

command -v ssh >/dev/null 2>&1 || die "ssh is required on this machine."

# --- asking ------------------------------------------------------------------
#
# Read from the terminal rather than stdin. Piped from curl, stdin *is* this
# script, and a `read` would swallow the rest of it — which is the sort of
# failure that looks like the script simply stopping half way.

ask() {
  # ask <prompt> <default> ; answer on stdout, prompt on stderr so it is not
  # captured by the caller.
  #
  # The terminal is required here rather than at the top, so a run with every
  # answer already given — MOONPHASE_HOST and friends — needs no terminal at
  # all and can be automated.
  if [ ! -r /dev/tty ]; then
    die "This has to ask where to install, and there is no terminal to ask on.

  Either run it from one:
    curl -fsSL $REPO_RAW/scripts/install-server.sh -o install-server.sh
    sh install-server.sh

  Or answer in advance:
    MOONPHASE_HOST=203.0.113.10 MOONPHASE_SSH_USER=root \\
      MOONPHASE_SSH_KEY=~/.ssh/id_ed25519 sh install-server.sh"
  fi
  _prompt="$1"
  _default="${2:-}"
  if [ -n "$_default" ]; then
    printf '%s [%s]: ' "$_prompt" "$_default" >&2
  else
    printf '%s: ' "$_prompt" >&2
  fi
  read -r _answer < /dev/tty || _answer=""
  [ -n "$_answer" ] || _answer="$_default"
  printf '%s' "$_answer"
}

confirm() {
  _answer=$(ask "$1 [y/N]" "")
  case "$_answer" in
    [yY] | [yY][eE][sS]) return 0 ;;
    *) return 1 ;;
  esac
}

printf '\n'
bold "Install Moonphase on a server"
printf '\n'
printf '  Any Linux machine you can reach over SSH — a VPS, a box under a desk.\n'
printf '  It needs nothing installed first.\n'
printf '\n'

HOST="${MOONPHASE_HOST:-}"
[ -n "$HOST" ] || HOST=$(ask "Server address (IP or hostname)")
[ -n "$HOST" ] || die "No address given."

SSH_USER="${MOONPHASE_SSH_USER:-}"
[ -n "$SSH_USER" ] || SSH_USER=$(ask "Username" "root")

SSH_PORT="${MOONPHASE_SSH_PORT:-}"
[ -n "$SSH_PORT" ] || SSH_PORT=$(ask "SSH port" "22")

# A key if one is offered, a password otherwise. Both end at the same place;
# the difference is only what ssh is told to try.
# MOONPHASE_SSH_PASSWORD is deliberately not a thing: a password on a command
# line is in the shell history and in `ps` output for every user on the machine.
# Answer the prompt, or use a key.
KEY="${MOONPHASE_SSH_KEY:-}"
if [ -z "$KEY" ] && [ "${MOONPHASE_SSH_KEY+set}" != "set" ]; then
  for candidate in "$HOME/.ssh/id_ed25519" "$HOME/.ssh/id_rsa"; do
    [ -f "$candidate" ] && { KEY="$candidate"; break; }
  done
  printf '\n'
  if [ -n "$KEY" ]; then
    printf '  Found a key at %s\n' "$KEY"
    if ! confirm "  Use it?"; then
      KEY=$(ask "  Path to a key, or blank to use a password" "")
    fi
  else
    KEY=$(ask "  Path to an SSH key, or blank to use a password" "")
  fi
fi

if [ -n "$KEY" ]; then
  KEY=$(printf '%s' "$KEY" | sed "s|^~|$HOME|")
  [ -f "$KEY" ] || die "No key at $KEY"
fi

# --- connecting ---------------------------------------------------------------
#
# One connection, shared. Without this a password would be asked for on every
# command — half a dozen times, which is intolerable and teaches people to pick
# a short one.

CONTROL_DIR="${TMPDIR:-/tmp}/moonphase-ssh-$$"
mkdir -p "$CONTROL_DIR"
chmod 700 "$CONTROL_DIR"
CONTROL="$CONTROL_DIR/control"

cleanup() {
  ssh -o ControlPath="$CONTROL" -O exit "$SSH_USER@$HOST" >/dev/null 2>&1 || true
  rm -rf "$CONTROL_DIR"
}
trap cleanup EXIT INT TERM

SSH_OPTS="-o ControlMaster=auto -o ControlPath=$CONTROL -o ControlPersist=5m"
SSH_OPTS="$SSH_OPTS -o StrictHostKeyChecking=accept-new -o ConnectTimeout=20"
SSH_OPTS="$SSH_OPTS -p $SSH_PORT"
[ -n "$KEY" ] && SSH_OPTS="$SSH_OPTS -i $KEY -o IdentitiesOnly=yes"

# shellcheck disable=SC2086 — SSH_OPTS is a deliberate word list.
remote() { ssh $SSH_OPTS "$SSH_USER@$HOST" "$@"; }
# shellcheck disable=SC2086
remote_tty() { ssh $SSH_OPTS -t "$SSH_USER@$HOST" "$@"; }

printf '\n'
info "Connecting to $SSH_USER@$HOST:$SSH_PORT"
if [ -z "$KEY" ]; then
  printf '  You will be asked for the password once.\n'
fi

if ! remote true; then
  printf '\n'
  die "Could not connect.

  Things worth checking:
    * the address and port are right, and the machine is up
    * $SSH_USER exists on it and may log in
    * your key is the one that machine knows, if you chose one"
fi
info "Connected"

# --- what we are installing onto ---------------------------------------------

UNAME=$(remote "uname -s" 2>/dev/null || echo unknown)
[ "$UNAME" = "Linux" ] || die "Moonphase installs onto Linux; that machine reports '$UNAME'."

# Root, or something that can become it. The installer needs it only to install
# Docker; a machine that already has Docker needs neither.
IS_ROOT=$(remote 'id -u' 2>/dev/null || echo 1)
HAS_SUDO=$(remote 'command -v sudo >/dev/null 2>&1 && echo yes || echo no')
HAS_DOCKER=$(remote 'command -v docker >/dev/null 2>&1 && echo yes || echo no')

if [ "$HAS_DOCKER" = "no" ] && [ "$IS_ROOT" != "0" ] && [ "$HAS_SUDO" = "no" ]; then
  die "That machine has no Docker, and $SSH_USER is not root and has no sudo.
  Install Docker there first, or use an account that can."
fi

# curl and openssl, which the installer needs and a minimal image often lacks.
# Fixing it here is the difference between "everything" and "everything except
# the two commands you have never heard of".
MISSING=$(remote 'for tool in curl openssl git; do
  command -v "$tool" >/dev/null 2>&1 || printf "%s " "$tool"
done')
if [ -n "$(printf '%s' "$MISSING" | tr -d ' ')" ]; then
  info "Installing what the server is missing:$MISSING"
  remote_tty "set -e
    as_root() { if [ \"\$(id -u)\" = 0 ]; then \"\$@\"; else sudo \"\$@\"; fi; }
    if command -v apt-get >/dev/null 2>&1; then
      as_root apt-get update -qq && as_root apt-get install -y -qq $MISSING
    elif command -v dnf >/dev/null 2>&1; then
      as_root dnf install -y -q $MISSING
    elif command -v yum >/dev/null 2>&1; then
      as_root yum install -y -q $MISSING
    elif command -v pacman >/dev/null 2>&1; then
      as_root pacman -Sy --noconfirm --needed $MISSING
    elif command -v apk >/dev/null 2>&1; then
      as_root apk add --no-cache $MISSING
    else
      echo 'Could not work out this distribution package manager.' >&2
      exit 1
    fi" || die "Could not install $MISSING on the server. Install them by hand and run this again."
fi

# --- installing ---------------------------------------------------------------

printf '\n'
if [ "$HAS_DOCKER" = "yes" ]; then
  info "Installing Moonphase (Docker is already there)"
else
  info "Installing Moonphase, and Docker first — this takes a few minutes"
fi
printf '\n'

# Fetched on the server rather than pushed from here, so there is one copy of
# the installer and it is the published one. A TTY, because installing Docker
# needs sudo and sudo may want a password.
if ! remote_tty "curl -fsSL $REPO_RAW/scripts/install.sh -o /tmp/moonphase-install.sh \
    && sh /tmp/moonphase-install.sh; rc=\$?; rm -f /tmp/moonphase-install.sh; exit \$rc"; then
  printf '\n'
  die "The install did not finish. The output above says where it stopped."
fi

# --- where to open it ----------------------------------------------------------

# What the installer actually published. It takes 80 when 80 is free and stays
# on 8471 when something else already has it, so guessing here would be wrong
# on exactly the machines that are already doing something.
# Asked of Compose directly. Parsing `ps --format {{.Publishers}}` looked like
# it would work and does not: the template renders `{0.0.0.0 80 80 tcp}`, with
# no arrow to match on, so a pattern written for the `docker ps` format finds
# nothing and quietly reports the fallback port on every machine.
PUBLISHED=$(remote 'cd moonphase 2>/dev/null && docker compose port proxy 80 2>/dev/null' || true)
case "$PUBLISHED" in
  *:80) URL="http://$HOST" ;;
  *) URL="http://$HOST:8471" ;;
esac

printf '\n'
bold "Moonphase is running."
printf '\n'
printf '  Open %s and follow the setup.\n' "$URL"
printf '  The first account is yours, and the address and HTTPS are set there.\n'
printf '\n'
printf '  If it does not open:\n'
printf '    * your provider may need port 80 opened in its firewall\n'
printf '    * on a cloud VM that is a security group, not the machine\n'
printf '\n'
printf '  Later, from this machine:\n'
printf '    ssh %s@%s "cd moonphase && docker compose logs -f api"\n' "$SSH_USER" "$HOST"
printf '    Run this script again to upgrade — it keeps your data.\n'
printf '\n'
printf '  The app for your own computer and phone:\n'
printf '    https://oliversvane.github.io/moonphase/getting-started/app/\n'
printf '\n'
