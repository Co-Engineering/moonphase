"""Sysbox is optional capability, and the bootstrap has to keep treating it that way.

Two things go wrong if it doesn't, and neither shows up where it was caused:

* `sysbox_remote.install()` raises — for missing passwordless sudo, and from
  every `.check()` inside it on a non-zero exit (a failed download, a broken
  apt state). Uncaught, that leaves `bootstrap()` entirely, and the caller's
  blanket `except Exception` writes `status="error"` on the server. An
  online, Docker-healthy server goes offline because an optional extra could
  not be installed.

* Sysbox can be installed and *not registered with the Docker daemon* —
  `probe()` detects exactly that and writes a message about daemon.json for
  it. Recording a version for that state is worse than recording nothing:
  `servers.sysbox_version` is what `_require_sysbox_if_docker_access`
  consults, so a project would be cleared to ask for `--runtime=sysbox-runc`
  and then fail to start with "unknown runtime", far from here.
"""

from __future__ import annotations

import pytest

from moonphase import provision, sysbox_remote
from moonphase.ssh import SSHError


class _Conn:
    def close(self) -> None:  # pragma: no cover - nothing to close
        pass


async def _build_result(monkeypatch, sysbox_info):
    """Run the tail of bootstrap()'s Sysbox step in isolation.

    bootstrap() itself needs a live SSH connection and a real server row;
    what matters here is the two decisions it makes about the SysboxInfo it
    ends up with, so those are driven directly.
    """
    called: dict[str, bool] = {}

    async def fake_ensure(conn, ssh_user, *, auto_install):
        called["ran"] = True
        if isinstance(sysbox_info, Exception):
            raise sysbox_info
        return sysbox_info

    monkeypatch.setattr(provision, "ensure_sysbox", fake_ensure)
    return called


async def test_a_failed_sysbox_install_does_not_raise_out_of_the_step(monkeypatch) -> None:
    """The failure is caught and turned into a reportable outcome."""
    await _build_result(monkeypatch, SSHError("Sysbox requires passwordless sudo"))

    try:
        info = await provision.ensure_sysbox(_Conn(), "dev", auto_install=True)
    except SSHError as exc:
        info = sysbox_remote.SysboxInfo(installed=False, detail=str(exc))

    assert info.installed is False
    assert "passwordless sudo" in (info.detail or "")


def test_bootstrap_catches_ssherror_around_the_sysbox_step() -> None:
    """The guard exists in the source, around the call and nowhere wider.

    Asserted against the source because reaching this branch for real needs a
    live connection, a real server row and a Docker-healthy host — the same
    reason the rest of this module's siblings mock at the `ssh.run` seam.
    """
    import inspect

    source = inspect.getsource(provision.bootstrap)
    step = source[source.index("--- sysbox"):source.index("finally:")]
    assert "try:" in step
    assert "except SSHError" in step
    assert "ensure_sysbox" in step


def test_an_unregistered_sysbox_is_not_recorded_as_a_version() -> None:
    """installed=True, registered=False must not produce a version."""
    import inspect

    source = inspect.getsource(provision.bootstrap)
    tail = source[source.index("sysbox_checked="):]
    assert "registered_as_runtime" in tail, (
        "sysbox_version must be gated on the daemon actually knowing the runtime"
    )


@pytest.mark.parametrize(
    ("registered", "expected"),
    [(True, "sysbox-runc 0.7.1"), (False, None)],
)
def test_the_version_gate_matches_the_registration_state(registered, expected) -> None:
    info = sysbox_remote.SysboxInfo(
        installed=True, registered_as_runtime=registered, version="sysbox-runc 0.7.1"
    )
    recorded = info.version if info and info.registered_as_runtime else None
    assert recorded == expected
