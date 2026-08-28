#!/usr/bin/env sh
#
# Install Moonphase.
#
#   curl -fsSL https://raw.githubusercontent.com/oliversvane/moonphase/main/scripts/install.sh | sh
#
# There is nothing to configure. Docker is installed if missing, every secret is
# generated, and the address and first account are set up in the browser
# afterwards — including HTTPS, which the proxy obtains for whatever domain you
# enter there.
#
# Docker is the only requirement. Everything else — the database, sign-in, the
# keys that encrypt your SSH credentials — is created here.
#
# Pulls the published image by default, which takes about a minute; set
# MOONPHASE_BUILD=1 to build from source instead. If the image cannot be
# pulled, it builds rather than failing.
#
# Safe to run twice. Secrets already in .env are kept, because regenerating
# MOONPHASE_SECRET_KEY would make every stored SSH key unreadable.
set -eu

REPO="${MOONPHASE_REPO:-https://github.com/oliversvane/moonphase.git}"
BRANCH="${MOONPHASE_BRANCH:-main}"
DIR="${MOONPHASE_DIR:-moonphase}"
PORT="${MOONPHASE_PORT:-8471}"
BIND="${MOONPHASE_BIND:-127.0.0.1}"
REPO_RAW_HINT="https://raw.githubusercontent.com/oliversvane/moonphase/main"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
info() { printf '\033[34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33m warn\033[0m %s\n' "$*"; }
die() { printf '\033[31merror\033[0m %s\n' "$*" >&2; exit 1; }

# --- requirements -----------------------------------------------------------

command -v curl >/dev/null 2>&1 || die "curl is required."
command -v openssl >/dev/null 2>&1 || die "openssl is required to generate keys."

as_root() {
  if [ "$(id -u)" = "0" ]; then "$@"; else sudo "$@"; fi
}

# Docker, installed rather than demanded. "Install Docker first" is a second
# command, and this is supposed to be one.
if ! command -v docker >/dev/null 2>&1; then
  if [ "$(id -u)" != "0" ] && ! command -v sudo >/dev/null 2>&1; then
    die "Docker is not installed, and installing it needs root. Run this as root, or install Docker yourself: https://docs.docker.com/get-docker/"
  fi
  info "installing Docker"
  curl -fsSL https://get.docker.com | as_root sh >/dev/null 2>&1 \
    || die "Could not install Docker. Install it yourself and run this again: https://docs.docker.com/get-docker/"
  # So the next login does not need sudo. It does not affect this shell, which
  # is why the invocation is worked out below rather than assumed.
  [ "$(id -u)" = "0" ] || as_root usermod -aG docker "$(id -un)" >/dev/null 2>&1 || true
  as_root systemctl enable --now docker >/dev/null 2>&1 || true
fi

# Which invocation actually works. Immediately after an install the current
# shell is not yet in the docker group, so sudo is the only way until the next
# login — and silently failing at the first `docker compose` would look like the
# install broke.
#
# Why the reason matters, and not just the failure: `sudo docker` fixes a socket
# you are not allowed to open. It does nothing whatsoever for a daemon that is
# not running. Trying it anyway asked for a password that could not have helped,
# three times, before giving up — which is how someone ends up mistyping their
# password at a script that was never going to succeed.
if docker info >/dev/null 2>&1; then
  DOCKER="docker"
  SUDO_NOTE=""
else
  docker_error=$(docker info 2>&1 || true)

  case "$docker_error" in
    *"permission denied"* | *"connect: permission denied"* | *"dial unix"*"permission denied"*)
      # A socket we may not open. sudo is exactly the right answer.
      if [ "$(id -u)" != "0" ] && sudo docker info >/dev/null 2>&1; then
        DOCKER="sudo docker"
        SUDO_NOTE=" (log out and back in to drop the sudo)"
      else
        die "Docker is running but this account cannot use it.

  Add yourself to the docker group, then log out and back in:
    sudo usermod -aG docker \$(id -un)"
      fi
      ;;
    *)
      # The daemon is not there. Say how to start it, on this machine.
      case "$(uname -s)" in
        Darwin)
          die "Docker is installed but not running.

  Start Docker Desktop, wait for the whale in the menu bar, and run this again:
    open -a Docker

  If you meant to install Moonphase onto a *server* rather than onto this Mac,
  use the installer made for that — it does the whole thing over SSH:
    curl -fsSL $REPO_RAW_HINT/scripts/install-server.sh -o install-server.sh
    sh install-server.sh"
          ;;
        *)
          if command -v systemctl >/dev/null 2>&1; then
            die "Docker is installed but its daemon is not running. Start it:
    sudo systemctl enable --now docker"
          else
            die "Docker is installed but its daemon is not running. Start it, then run this again."
          fi
          ;;
      esac
      ;;
  esac
fi

$DOCKER compose version >/dev/null 2>&1 \
  || die "Docker Compose v2 is required (it ships with modern docker-ce and Docker Desktop)."

# --- source -----------------------------------------------------------------
# Running from inside a checkout is the common case for a second run.

if [ -f docker-compose.yml ] && [ -d supabase/migrations ]; then
  info "using the checkout in $(pwd)"
else
  command -v git >/dev/null 2>&1 || die "git is required to fetch Moonphase."
  if [ -d "$DIR/.git" ]; then
    info "updating $DIR"
    git -C "$DIR" pull --ff-only origin "$BRANCH"
  else
    info "cloning into $DIR"
    git clone --branch "$BRANCH" --depth 1 "$REPO" "$DIR"
  fi
  cd "$DIR"
fi

# --- secrets ----------------------------------------------------------------

# base64url without padding, which is what JWT and Fernet both want.
b64url() { openssl base64 -A | tr '+/' '-_' | tr -d '='; }

# A Fernet key is exactly 32 random bytes, urlsafe-base64 encoded, padding kept.
fernet_key() { openssl rand 32 | openssl base64 -A | tr '+/' '-_'; }

# A Supabase-shaped API key: an HS256 JWT carrying only a role. GoTrue does not
# check it — there is no PostgREST here to gate — but supabase-js insists on
# being given one, and a well-formed key keeps this stack compatible with a
# real Supabase should you move to one.
supabase_key() {
  role="$1"
  secret="$2"
  iat=$(date +%s)
  exp=$((iat + 60 * 60 * 24 * 365 * 10))
  header=$(printf '{"alg":"HS256","typ":"JWT"}' | b64url)
  payload=$(printf '{"role":"%s","iss":"moonphase","iat":%s,"exp":%s}' "$role" "$iat" "$exp" | b64url)
  signature=$(printf '%s.%s' "$header" "$payload" \
    | openssl dgst -sha256 -hmac "$secret" -binary | b64url)
  printf '%s.%s.%s' "$header" "$payload" "$signature"
}

# Read a value already in .env, so a second run never rotates a live secret.
existing() {
  [ -f .env ] || return 0
  sed -n "s/^$1=//p" .env | head -1
}

if [ -f .env ]; then
  info "keeping the secrets already in .env"
  # Kept for two reasons: this file holds MOONPHASE_SECRET_KEY, which cannot be
  # regenerated, and a development checkout's .env carries a dozen keys this
  # script knows nothing about. Losing either silently would be the worst kind
  # of failure — one that only shows up much later.
  cp .env .env.bak
else
  info "generating secrets"
fi

SECRET_KEY=$(existing MOONPHASE_SECRET_KEY)
[ -n "${SECRET_KEY:-}" ] || SECRET_KEY=$(fernet_key)

# `supabase start` prints this exact string on every machine — it ships in the
# CLI itself, not generated per-project. Treating it as "already set" would
# carry a publicly known secret straight into a real deployment.
KNOWN_DEFAULT_JWT_SECRET="super-secret-jwt-token-with-at-least-32-characters-long"
JWT_SECRET=$(existing SUPABASE_JWT_SECRET)
if [ "${JWT_SECRET:-}" = "$KNOWN_DEFAULT_JWT_SECRET" ]; then
  warn "SUPABASE_JWT_SECRET in .env is the well-known supabase-start default; generating a real one"
  JWT_SECRET=""
fi
[ -n "${JWT_SECRET:-}" ] || JWT_SECRET=$(openssl rand -hex 32)

PG_PASSWORD=$(existing POSTGRES_PASSWORD)
[ -n "${PG_PASSWORD:-}" ] || PG_PASSWORD=$(openssl rand -hex 24)

ANON_KEY=$(existing SUPABASE_ANON_KEY)
[ -n "${ANON_KEY:-}" ] || ANON_KEY=$(supabase_key anon "$JWT_SECRET")

COMPOSE_FILES="docker-compose.yml"

# Caddy answers the certificate challenge on 80 and serves on 443, so automatic
# HTTPS needs both. A machine already running a web server has them taken, and
# claiming them unconditionally turned "install Moonphase" into "first move your
# other site" — with no error saying so.
COMPOSE_FILES="docker-compose.yml"
PUBLIC_PORTS=1

listening_on() {
  if command -v ss >/dev/null 2>&1; then
    ss -ltn 2>/dev/null | grep -qE ":$1[[:space:]]"
  elif command -v netstat >/dev/null 2>&1; then
    netstat -ltn 2>/dev/null | grep -qE ":$1[[:space:]]"
  else
    return 1
  fi
}

# An install that already took those ports is listening on them itself, and a
# second run must not read its own proxy as somebody else's web server. It did:
# re-running this on a working public install found 80 busy, dropped the
# override, and took the site off 80 — an upgrade that quietly unpublished the
# thing it was upgrading.
#
# So the earlier decision stands. It is only made afresh when there is none.
case "$(existing COMPOSE_FILE)" in
  *docker-compose.public.yml*)
    KEEP_PUBLIC=1
    ;;
  *)
    KEEP_PUBLIC=0
    ;;
esac

if [ "$KEEP_PUBLIC" = "1" ]; then
  info "keeping ports 80 and 443, which this install already has"
else
  for port in 80 443; do
    if listening_on "$port"; then
      PUBLIC_PORTS=0
    fi
  done
fi

if [ "$KEEP_PUBLIC" = "1" ] || [ "$PUBLIC_PORTS" = "1" ]; then
  COMPOSE_FILES="docker-compose.yml:docker-compose.public.yml"
else
  warn "ports 80 and 443 are already in use on this machine"
  warn "installing without them: Moonphase will answer on http://127.0.0.1:${PORT}"
  warn "a custom domain and HTTPS need those ports. Free them, then add"
  warn "  COMPOSE_FILE=docker-compose.yml:docker-compose.public.yml"
  warn "to .env and bring it up again."
fi

GITHUB_CLIENT_ID=$(existing MOONPHASE_GITHUB_CLIENT_ID)

IMAGE=$(existing MOONPHASE_IMAGE)
[ -n "${IMAGE:-}" ] || IMAGE="${MOONPHASE_IMAGE:-ghcr.io/oliversvane/moonphase}"

VERSION=$(existing MOONPHASE_VERSION)
# `latest` is the newest release. It was `edge` for a while because nothing had
# been released and `latest` was a tag that did not exist — pulling it 404s, and
# the installer then builds from source, which is twenty minutes instead of one
# for a reason nothing on screen explains. There are releases now.
[ -n "${VERSION:-}" ] || VERSION="${MOONPHASE_VERSION:-latest}"

VAPID_PUBLIC=$(existing MOONPHASE_VAPID_PUBLIC_KEY)
VAPID_PRIVATE=$(existing MOONPHASE_VAPID_PRIVATE_KEY)
VAPID_SUBJECT=$(existing MOONPHASE_VAPID_SUBJECT)
[ -n "${VAPID_SUBJECT:-}" ] || VAPID_SUBJECT="mailto:admin@example.com"

cat > .env <<ENV
# Written by scripts/install.sh. Safe to edit; re-running keeps these values.
#
# MOONPHASE_SECRET_KEY encrypts every stored SSH key and harness credential.
# It cannot be recovered — lose it and you re-onboard every server by hand.
# Back it up somewhere other than the database it protects.
MOONPHASE_SECRET_KEY=${SECRET_KEY}

POSTGRES_USER=postgres
POSTGRES_PASSWORD=${PG_PASSWORD}
POSTGRES_DB=postgres

SUPABASE_JWT_SECRET=${JWT_SECRET}
SUPABASE_ANON_KEY=${ANON_KEY}

# Where the image comes from. ghcr.io does not rate-limit anonymous pulls;
# oliversvanecoec/moonphase on Docker Hub is the same image, mirrored.
MOONPHASE_IMAGE=${IMAGE}

# Which published image to run: latest, a version like 0.2.1, or edge for the
# tip of main. Pinning an exact version means upgrades happen when you change
# this line rather than whenever you happen to pull.
MOONPHASE_VERSION=${VERSION}

# Which compose files describe this install. Read by docker compose itself, so
# running it later by hand does the same thing this script did — without it, an
# HTTPS deployment would silently come back up on plain HTTP.
COMPOSE_FILE=${COMPOSE_FILES}

# Interface and port the proxy publishes on. 127.0.0.1 keeps it to this
# machine; set 0.0.0.0 once something terminates TLS in front of it.
MOONPHASE_BIND=${BIND}
MOONPHASE_PORT=${PORT}

# How often to check whether each agent is still working, in seconds. 0 turns
# off the monitor, and with it notifications.
MOONPHASE_MONITOR_INTERVAL=20

# Push notifications. Filled in below when they can be generated.
MOONPHASE_VAPID_PUBLIC_KEY=${VAPID_PUBLIC:-}
MOONPHASE_VAPID_PRIVATE_KEY=${VAPID_PRIVATE:-}
MOONPHASE_VAPID_SUBJECT=${VAPID_SUBJECT}

# Client id of a GitHub OAuth app, for one-click sign-in. No secret needed.
MOONPHASE_GITHUB_CLIENT_ID=${GITHUB_CLIENT_ID:-}
ENV

# Anything the previous file had that this script does not manage. A checkout
# used for development keeps DATABASE_URL, the VITE_* variables and half a
# dozen others in here; rewriting the file without them would break `pnpm dev`
# later, a long way from the thing that caused it.
if [ -f .env.bak ]; then
  kept=$(awk -F= '
    /^[A-Z_]+=/ {
      key = $1
      if (key !~ /^(MOONPHASE_SECRET_KEY|POSTGRES_(USER|PASSWORD|DB)|SUPABASE_(JWT_SECRET|ANON_KEY)|MOONPHASE_(PUBLIC_URL|IMAGE|VERSION|BIND|PORT|MONITOR_INTERVAL|GITHUB_CLIENT_ID|VAPID_PUBLIC_KEY|VAPID_PRIVATE_KEY|VAPID_SUBJECT)|COMPOSE_FILE)$/) print
    }' .env.bak)
  if [ -n "$kept" ]; then
    {
      printf '\n# --- kept from your previous .env ---------------------------------------\n'
      printf '%s\n' "$kept"
    } >> .env
    info "kept $(printf '%s\n' "$kept" | wc -l | tr -d ' ') other setting(s) from your previous .env"
  fi
fi

chmod 600 .env

# --- the image --------------------------------------------------------------
# Pulling is the default: it is a minute rather than several, and everyone runs
# identical bits. Building is for working on Moonphase, or for a commit that
# has not been published yet.


build_it() {
  info "building from source (a few minutes the first time)"
  # Recorded in .env so a later `docker compose up -d` builds too rather than
  # trying to pull an image that was never published.
  if ! grep -q "docker-compose.build.yml" .env; then
    sed -i.tmp "s|^COMPOSE_FILE=.*|COMPOSE_FILE=${COMPOSE_FILES}:docker-compose.build.yml|" .env
    rm -f .env.tmp
  fi
  $DOCKER compose build api
}

if [ "${MOONPHASE_BUILD:-0}" = "1" ]; then
  build_it
else
  info "pulling the image"
  # Both streams, not just stderr: compose reports some configuration
  # complaints on stdout, and one scrolling past a working install is exactly
  # what makes people think it went wrong. Only the exit code is used.
  if ! $DOCKER compose pull --quiet api >/dev/null 2>&1; then
    warn "could not pull $IMAGE — building from source instead"
    build_it
  fi
fi

# Push keys need a P-256 keypair in a particular encoding, which is easier to
# generate with the library that will consume it than in shell. The image is
# built by now, so borrow it.
if [ -z "${VAPID_PUBLIC:-}" ]; then
  info "generating push keys"
  # -T and a closed stdin, both deliberately. Piped from curl, this script *is*
  # stdin, and `docker compose run` attaches to it by default — so it swallowed
  # the rest of the file and the install stopped here without an error, in the
  # one path the documentation actually tells people to use.
  if keys=$($DOCKER compose run --rm --no-deps -T --entrypoint python api \
      /app/scripts/gen_vapid.py 2>/dev/null </dev/null); then
    # gen_vapid.py prints VAR=value lines ready for .env.
    printf '%s\n' "$keys" | grep -E '^MOONPHASE_VAPID_(PUBLIC|PRIVATE)_KEY=' > .vapid.tmp || true
    if [ -s .vapid.tmp ]; then
      grep -v -E '^MOONPHASE_VAPID_(PUBLIC|PRIVATE)_KEY=' .env > .env.tmp
      cat .env.tmp .vapid.tmp > .env
      rm -f .env.tmp
      chmod 600 .env
    fi
    rm -f .vapid.tmp
  else
    warn "could not generate push keys; notifications stay off until you do"
  fi
fi

# --- run --------------------------------------------------------------------

info "starting"
$DOCKER compose up -d

info "waiting for it to come up"
attempt=0
until curl -fsS "http://127.0.0.1:${PORT}/api/health" >/dev/null 2>&1; do
  attempt=$((attempt + 1))
  if [ "$attempt" -gt 90 ]; then
    printf '\n'
    warn "it did not answer in time. What the services say:"
    $DOCKER compose ps
    $DOCKER compose logs --tail 40 api migrate
    exit 1
  fi
  sleep 2
done

printf '\n'
bold "Moonphase is running at http://127.0.0.1:${PORT}"
printf '\n'
printf '  Open it and follow the setup. The first account is yours, and\n  the address and HTTPS are set up there — nothing to edit here.\n'
printf '\n'
printf '  Next:\n'
printf '    Add a server        any machine you can reach over SSH\n'
printf '    Connect Claude      Settings -> Accounts\n'
printf '    Your address        set it during setup; HTTPS is automatic\n'
printf '\n'
printf '  Manage it:\n'
printf '    docker compose logs -f api\n'
printf '    docker compose down          stop, keeping your data\n'
printf '    docker compose pull && docker compose up -d              upgrade\n'
printf '\n'
printf '  Documentation: https://oliversvane.github.io/moonphase/\n'
printf '\n'
