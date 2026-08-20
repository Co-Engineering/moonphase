#!/usr/bin/env sh
#
# Install the Moonphase desktop app on Linux or macOS.
#
#   curl -fsSL https://raw.githubusercontent.com/oliversvane/moonphase/main/scripts/install-app.sh | sh
#
# This is the app, not the server. It connects to a Moonphase you are already
# running — it asks for the address on first launch — and installing it changes
# nothing on any server.
#
# Nothing is configured here and nothing needs root. On Linux the AppImage lands
# in ~/.local/bin with a launcher entry beside it; on macOS the app goes to
# ~/Applications.
#
# Safe to run twice: it replaces what it installed last time, which is also how
# you update.
set -eu

REPO="${MOONPHASE_REPO:-oliversvane/moonphase}"
CHANNEL="${MOONPHASE_CHANNEL:-edge}"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
info() { printf '\033[34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33m warn\033[0m %s\n' "$*"; }
die() { printf '\033[31merror\033[0m %s\n' "$*" >&2; exit 1; }

command -v curl >/dev/null 2>&1 || die "curl is required."

# --- what are we installing on ----------------------------------------------

os="$(uname -s)"
machine="$(uname -m)"

case "$machine" in
  x86_64 | amd64) arch="x64" ;;
  arm64 | aarch64) arch="arm64" ;;
  *) die "Unsupported architecture: $machine. Builds exist for x86_64 and arm64." ;;
esac

case "$os" in
  Linux | Darwin) ;;
  *) die "This script installs on Linux and macOS. On Windows, see the docs for the PowerShell command." ;;
esac

# --- find the download -------------------------------------------------------
#
# Asked of the API rather than guessed from a version number, because the edge
# build's filename carries a version that moves.

info "Looking up the $CHANNEL build"
release_json="$(curl -fsSL "https://api.github.com/repos/$REPO/releases/tags/$CHANNEL")" \
  || die "Could not reach GitHub to find the $CHANNEL release."

# One asset per platform and architecture. Matched on the extension and the
# architecture in the name, which is what electron-builder puts there.
case "$os" in
  Linux) want="\\.AppImage$" ;;
  Darwin) want="-mac\\.zip$|\\.zip$" ;;
esac

assets="$(printf '%s' "$release_json" \
  | grep -o '"browser_download_url": *"[^"]*"' \
  | sed 's/.*"\(https[^"]*\)"/\1/' \
  | grep -E "$want" || true)"

url="$(printf '%s\n' "$assets" | grep -E "$arch|$(printf '%s' "$arch" | sed 's/x64/x86_64/')" | head -1 || true)"

# electron-builder leaves the architecture out of the name when it built only
# one for that platform, so an arch-less asset is the right answer — but only if
# it names no architecture at all. Taking simply the first match would hand an
# x64 machine the arm64 build whenever that one happened to be listed first,
# which installs cleanly and then refuses to start.
if [ -z "$url" ]; then
  url="$(printf '%s\n' "$assets" | grep -vE 'arm64|aarch64|x64|x86_64|ia32' | head -1 || true)"
fi

[ -n "$url" ] || die "No $os build for $arch in the $CHANNEL release."

tmp="$(mktemp -d)"
# shellcheck disable=SC2064 — expand $tmp now, while it still exists.
trap "rm -rf '$tmp'" EXIT INT TERM

info "Downloading $(basename "$url")"
curl -fL --progress-bar "$url" -o "$tmp/download" || die "Download failed."

# --- install -----------------------------------------------------------------

if [ "$os" = "Linux" ]; then
  bin_dir="${XDG_BIN_HOME:-$HOME/.local/bin}"
  app_dir="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
  icon_dir="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/512x512/apps"
  mkdir -p "$bin_dir" "$app_dir" "$icon_dir"

  install -m 755 "$tmp/download" "$bin_dir/moonphase"

  # The icon lives inside the AppImage. Extracting it is one call and the
  # difference between an entry in the launcher and a nameless grey square, so
  # it is worth doing — but only worth trying: a missing icon is a blemish, not
  # a failed install.
  # Asked for by directory rather than by "*.png": the icon at the top of an
  # AppImage is a symlink into that directory, so matching the name alone
  # extracts a link pointing at something that was not extracted with it.
  #
  # Every step here is optional. A missing icon is a blemish; an install that
  # stops half way because of one is a bug.
  icon="moonphase"
  if (cd "$tmp" && "$bin_dir/moonphase" --appimage-extract "usr/share/icons/*" \
        >/dev/null 2>&1); then
    extracted="$(find "$tmp/squashfs-root" -name "*.png" -type f 2>/dev/null | head -1)"
    if [ -n "$extracted" ] && install -m 644 "$extracted" "$icon_dir/moonphase.png"; then
      command -v gtk-update-icon-cache >/dev/null 2>&1 \
        && gtk-update-icon-cache -f -t "${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor" \
           >/dev/null 2>&1 || true
    else
      warn "Could not extract the app icon; the launcher entry will use a default."
    fi
  fi

  cat > "$app_dir/moonphase.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Moonphase
Comment=Coding agents on servers you own
Exec=$bin_dir/moonphase %U
Icon=$icon
Terminal=false
Categories=Development;
StartupWMClass=Moonphase
EOF
  # Some desktops only notice new entries when the cache is rebuilt.
  command -v update-desktop-database >/dev/null 2>&1 \
    && update-desktop-database "$app_dir" >/dev/null 2>&1 || true

  echo
  bold "Moonphase is installed."
  echo
  echo "  Launch it from your applications menu, or run: moonphase"
  echo

  case ":$PATH:" in
    *":$bin_dir:"*) ;;
    *)
      warn "$bin_dir is not on your PATH, so the 'moonphase' command will not be found."
      echo "       Add it with:"
      echo
      echo "         echo 'export PATH=\"\$PATH:$bin_dir\"' >> ~/.profile"
      echo
      ;;
  esac

  # AppImages mount themselves through FUSE, and say so with a bare
  # "dlopen(): error loading libfuse.so.2" when it is missing. Either version
  # will do — a machine with only libfuse3 runs these perfectly well, and
  # warning there would send people to install something they do not need.
  if ! ldconfig -p 2>/dev/null | grep -qE "libfuse\.so\.2|libfuse3\.so"; then
    warn "AppImages need FUSE, which does not appear to be installed."
    echo "       Debian/Ubuntu:  sudo apt install libfuse2"
    echo "       Fedora:         sudo dnf install fuse"
    echo "       Arch:           sudo pacman -S fuse2"
    echo
  fi
else
  target="$HOME/Applications"
  mkdir -p "$target"

  command -v unzip >/dev/null 2>&1 || die "unzip is required."
  info "Unpacking"
  rm -rf "$tmp/unpacked" && mkdir -p "$tmp/unpacked"
  unzip -q "$tmp/download" -d "$tmp/unpacked" || die "Could not unpack the download."

  bundle="$(find "$tmp/unpacked" -maxdepth 2 -name "*.app" | head -1)"
  [ -n "$bundle" ] || die "No .app found inside the download."

  rm -rf "$target/Moonphase.app"
  cp -R "$bundle" "$target/Moonphase.app"

  # This build is not code-signed, so macOS quarantines it and the first open
  # fails with "Moonphase is damaged and can't be opened" — which is not what
  # has happened. Clearing the flag is the difference between an app that opens
  # and one that does not, and you just chose to run this installer, so it is
  # done here rather than left as a riddle. Said out loud because removing a
  # security marker should never be silent.
  info "This build is not signed by Apple, so macOS would refuse to open it."
  info "Clearing the quarantine flag on $target/Moonphase.app"
  xattr -dr com.apple.quarantine "$target/Moonphase.app" 2>/dev/null || true

  echo
  bold "Moonphase is installed."
  echo
  echo "  Open it from ~/Applications, Spotlight, or run:"
  echo "    open -a Moonphase"
  echo
fi

echo "On first launch it asks for the address of your Moonphase server."
echo "Do not have one yet? https://oliversvane.github.io/moonphase/getting-started/docker/"
echo
