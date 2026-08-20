#!/usr/bin/env python3
"""Generate the Moonphase logo, everywhere it is needed.

A flat crescent: two arcs, one colour, no gradient and no detail that a 16px
sidebar glyph would turn to mush. Every size comes from the same geometry, so
the favicon and the app icon cannot drift apart.

The crescent is the region inside one circle and outside another, drawn as its
actual boundary — the long way round the disc, then back along the bite. The
obvious construction, two circles with `fill-rule="evenodd"`, does not work: the
bite reaches past the disc, so the overrun fills too and the result is a ring
with a lens through it rather than a moon.

Thickness was chosen at 16px rather than at 512. A slimmer crescent is prettier
large and loses its horns small, and small is where this mostly lives.

Run it from anywhere:

    python3 scripts/logo.py

Needs `rsvg-convert` (librsvg) for the PNGs. Without it the SVGs are still
written and the PNGs are reported as skipped.
"""

from __future__ import annotations

import math
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# --- the shape ---------------------------------------------------------------

R = 100.0  # disc radius, in the units the path is written in
BITE_R = 84.0  # radius of the circle taken out of it
BITE_DX = 46.0  # how far to the right that circle sits

# Ink and ground. The accent is the one the app already uses for anything live.
ACCENT = "#7aa2f7"
GROUND = "#12141d"


def crescent_path() -> str:
    """The lit part, as one closed path."""
    # Where the two circles cross — the horns.
    xi = (R * R - BITE_R * BITE_R + BITE_DX * BITE_DX) / (2 * BITE_DX)
    yi = math.sqrt(R * R - xi * xi)
    return (
        f"M {xi:.2f},{-yi:.2f} "
        f"A {R:.0f},{R:.0f} 0 1 0 {xi:.2f},{yi:.2f} "
        f"A {BITE_R:.0f},{BITE_R:.0f} 0 0 1 {xi:.2f},{-yi:.2f} Z"
    )


def bounds() -> tuple[float, float, float, float]:
    """(x0, y0, width, height) of the crescent, computed rather than eyeballed.

    The leftmost point is the disc's, the rightmost is the horns', and the arc
    goes over the top and bottom of the disc — so the height is the diameter.
    """
    xi = (R * R - BITE_R * BITE_R + BITE_DX * BITE_DX) / (2 * BITE_DX)
    return -R, -R, xi + R, 2 * R


# --- the files ---------------------------------------------------------------


def mark_svg() -> str:
    """The bare crescent, in `currentColor`, centred in a square.

    Square because every slot it goes in is one, and `currentColor` because it
    appears on the sidebar, in the docs header and on a sign-in card, each with
    a different background and none of them wanting a second file.
    """
    x0, _, w, h = bounds()
    pad = 0.08 * h
    side = h + 2 * pad
    # Centre the shape's own box, not the disc's: the crescent's weight is left
    # of the disc's centre, and centring the disc leaves it visibly adrift.
    cx = x0 + w / 2
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="{-side / 2:.1f} {-side / 2:.1f} {side:.1f} {side:.1f}" '
        f'role="img" aria-label="Moonphase">'
        f'<path transform="translate({-cx:.2f} 0)" '
        f'd="{crescent_path()}" fill="currentColor"/>'
        f"</svg>\n"
    )


def icon_svg() -> str:
    """The app icon: the mark on its own rounded ground.

    A rounded square rather than a bare glyph because this is what a launcher,
    a dock and a home screen show, and every one of them puts it on a surface
    whose colour is not ours to choose.
    """
    x0, _, w, h = bounds()
    cx = x0 + w / 2
    # 0.62 of the tile, which is roughly what platform icon grids expect and
    # leaves room for the mask a maskable icon may be given.
    scale = (512 * 0.62) / h
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" '
        'role="img" aria-label="Moonphase">'
        f'<rect width="512" height="512" rx="112" fill="{GROUND}"/>'
        f'<g transform="translate(256 256) scale({scale:.4f}) '
        f'translate({-cx:.2f} 0)">'
        f'<path d="{crescent_path()}" fill="{ACCENT}"/></g>'
        "</svg>\n"
    )


def docs_logo_svg() -> str:
    """The mark for the documentation header, in white.

    A static file cannot inherit a colour, and Material puts its logo on a
    coloured bar in both themes — white is the one that reads on both.
    """
    return mark_svg().replace("currentColor", "#ffffff")


TARGETS_SVG = {
    "apps/web/public/icon.svg": icon_svg,
    "apps/web/public/mark.svg": mark_svg,
    "docs/assets/logo.svg": docs_logo_svg,
    # The favicon keeps its own ground: a bare crescent on a light tab strip is
    # a white shape on white.
    "docs/assets/favicon.svg": icon_svg,
}

# Rasterised from icon.svg, because a PNG is what several of these want:
# `apple-touch-icon` ignores SVG entirely, and electron-builder wants a 512
# square it can resize itself.
TARGETS_PNG = {
    "apps/web/public/icon-192.png": 192,
    "apps/web/public/icon-512.png": 512,
    "apps/web/public/icon.png": 512,
    "apps/web/public/apple-touch-icon.png": 180,
    "apps/desktop/packaging/icon.png": 512,
}


def main() -> int:
    for relative, render in TARGETS_SVG.items():
        path = ROOT / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render())
        print(f"  {relative}")

    rsvg = shutil.which("rsvg-convert")
    if rsvg is None:
        print("\nrsvg-convert not found — PNGs skipped.", file=sys.stderr)
        print("  Debian/Ubuntu: sudo apt install librsvg2-bin", file=sys.stderr)
        print("  Arch:          sudo pacman -S librsvg", file=sys.stderr)
        print("  macOS:         brew install librsvg", file=sys.stderr)
        return 1

    source = ROOT / "apps/web/public/icon.svg"
    for relative, size in TARGETS_PNG.items():
        path = ROOT / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [rsvg, "-w", str(size), "-h", str(size), str(source), "-o", str(path)],
            check=True,
        )
        print(f"  {relative} ({size}px)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
