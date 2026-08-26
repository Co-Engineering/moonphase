"""The clipboard shim, run as itself.

It stands in for `xclip` inside a container that has no X server, so the
harness's own "paste an image" path finds something. It ships as a string
baked into an image, which means nothing exercises it until somebody pastes a
screenshot into a real terminal — and a shell script that silently exits 1 is
indistinguishable from having no image staged.

So it is extracted and run here against a real filesystem, with the argument
shapes `xclip` is actually called with.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from moonphase.imagebuild import XCLIP_SHIM


@pytest.fixture
def shim(tmp_path: Path) -> Path:
    script = tmp_path / "xclip"
    script.write_text(XCLIP_SHIM)
    script.chmod(0o755)
    return script


def run(shim: Path, home: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [str(shim), *args],
        capture_output=True,
        env={**os.environ, "HOME": str(home)},
    )


def stage(home: Path, content: bytes = b"\x89PNG-not-really") -> Path:
    directory = home / ".moonphase-clipboard"
    directory.mkdir(parents=True, exist_ok=True)
    image = directory / "image.png"
    image.write_bytes(content)
    return image


def test_it_advertises_png_when_an_image_is_staged(shim: Path, tmp_path: Path) -> None:
    """The first thing a caller asks is what formats are on the clipboard."""
    stage(tmp_path)
    result = run(shim, tmp_path, "-selection", "clipboard", "-t", "TARGETS", "-o")

    assert result.returncode == 0
    assert b"image/png" in result.stdout


def test_it_hands_back_the_image_bytes_unchanged(shim: Path, tmp_path: Path) -> None:
    content = b"\x89PNG\r\n\x1a\n" + bytes(range(256))
    stage(tmp_path, content)

    result = run(shim, tmp_path, "-selection", "clipboard", "-t", "image/png", "-o")

    assert result.returncode == 0
    assert result.stdout == content


def test_the_image_is_consumed_once(shim: Path, tmp_path: Path) -> None:
    """A paste is a one-off. Leaving it staged would attach the same
    screenshot to every later paste that happened to find it."""
    image = stage(tmp_path)

    first = run(shim, tmp_path, "-t", "image/png", "-o")
    assert first.returncode == 0
    assert not image.exists()

    second = run(shim, tmp_path, "-t", "image/png", "-o")
    assert second.returncode != 0
    assert second.stdout == b""


def test_nothing_staged_means_nothing_offered(shim: Path, tmp_path: Path) -> None:
    """Answering "image/png" with no image behind it would make the caller ask
    for bytes that are not there."""
    for args in (("-t", "TARGETS", "-o"), ("-t", "image/png", "-o")):
        result = run(shim, tmp_path, *args)
        assert result.returncode != 0
        assert result.stdout == b""


def test_an_unrelated_invocation_fails_rather_than_answering(
    shim: Path, tmp_path: Path
) -> None:
    """It shadows the real `xclip` on PATH, so a text copy has to fail
    honestly rather than hand back a PNG."""
    stage(tmp_path)

    result = run(shim, tmp_path, "-selection", "clipboard", "-o")
    assert result.returncode != 0
    assert result.stdout == b""

    text = run(shim, tmp_path, "-selection", "clipboard", "-t", "text/plain", "-o")
    assert text.returncode != 0
    assert text.stdout == b""
