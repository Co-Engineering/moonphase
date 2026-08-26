#!/bin/sh
# Stands in for xclip so the harness's own "read an image off the system
# clipboard" path (checkImage/saveImage, invoked on every paste) finds
# something to read. This container has no X server and no real OS
# clipboard — the only clipboard a browser-attached session actually has is
# the browser's, so the frontend stages a pasted image here (under the
# session's own HOME) before the keystroke that makes the harness look for
# one ever reaches it. See apps/api/moonphase/sessions.py:stage_clipboard_image
# and apps/web/src/components/Terminal.tsx for the other half.
#
# Consumed once per read, the way a real paste is: a successful `-t
# image/png` read deletes the staged file, so an unrelated later paste does
# not pick up a stale image.
set -eu

stage="$HOME/.moonphase-clipboard/image.png"
want_targets=0
want_png=0
prev=""
for arg in "$@"; do
  case "$prev $arg" in
    "-t TARGETS") want_targets=1 ;;
    "-t image/png") want_png=1 ;;
  esac
  prev=$arg
done

[ -f "$stage" ] || exit 1

if [ "$want_targets" = 1 ]; then
  echo "image/png"
  exit 0
fi

if [ "$want_png" = 1 ]; then
  cat "$stage"
  rm -f "$stage"
  exit 0
fi

exit 1
