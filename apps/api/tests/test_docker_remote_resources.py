"""Parsing Docker's own accounting of disk, CPU and memory.

Nothing here needs a real Docker daemon: the thing worth pinning down is that
the JSON/df output Docker actually prints — which mixes decimal (system df)
and binary (stats) unit suffixes for no reason a caller controls — comes out
as the right number of bytes.
"""

from __future__ import annotations

import json

from moonphase import docker_remote
from moonphase.ssh import CommandResult


class _ScriptedSSH:
    """Returns canned output in the order calls are made."""

    def __init__(self, *outputs: str) -> None:
        self._outputs = list(outputs)
        self.commands: list[str] = []

    async def run(self, conn, command, *, timeout: float = 60.0, stdin=None):
        self.commands.append(command)
        stdout = self._outputs.pop(0) if self._outputs else ""
        return CommandResult(exit_status=0, stdout=stdout, stderr="")


def _fake(monkeypatch, *outputs: str) -> _ScriptedSSH:
    fake = _ScriptedSSH(*outputs)
    monkeypatch.setattr(docker_remote.ssh, "run", fake.run)
    return fake


# --- _parse_docker_bytes -----------------------------------------------------


def test_parses_decimal_units() -> None:
    assert docker_remote._parse_docker_bytes("833.7kB") == 833_700
    assert docker_remote._parse_docker_bytes("60.41MB") == 60_410_000
    assert docker_remote._parse_docker_bytes("1GB") == 1_000_000_000


def test_parses_binary_units() -> None:
    assert docker_remote._parse_docker_bytes("92.18MiB") == int(92.18 * 1024**2)
    assert docker_remote._parse_docker_bytes("31.34GiB") == int(31.34 * 1024**3)


def test_parses_bare_bytes_and_zero() -> None:
    assert docker_remote._parse_docker_bytes("0B") == 0
    assert docker_remote._parse_docker_bytes("512") == 512


def test_unrecognised_text_is_zero_not_a_crash() -> None:
    assert docker_remote._parse_docker_bytes("garbage") == 0
    assert docker_remote._parse_docker_bytes("") == 0


# --- volume_create ------------------------------------------------------------


async def test_volume_create_always_labels_moonphase(monkeypatch) -> None:
    fake = _fake(monkeypatch, "")
    await docker_remote.volume_create(None, "mp-test-workspace")
    assert "--label moonphase=1" in fake.commands[0]
    assert "moonphase.project" not in fake.commands[0]


async def test_volume_create_labels_its_project_when_given(monkeypatch) -> None:
    fake = _fake(monkeypatch, "")
    await docker_remote.volume_create(None, "mp-test-workspace", project="mp-test-abcd1234")
    assert "--label moonphase.project=mp-test-abcd1234" in fake.commands[0]


# --- volume_remove -------------------------------------------------------------


async def test_volume_remove_reports_success(monkeypatch) -> None:
    async def fake_run(*a, **k):
        return CommandResult(exit_status=0, stdout="", stderr="")

    monkeypatch.setattr(docker_remote.ssh, "run", fake_run)
    assert await docker_remote.volume_remove(None, "mp-test-workspace") is True


async def test_volume_remove_reports_failure_rather_than_raising(monkeypatch) -> None:
    async def fake_run(*a, **k):
        return CommandResult(exit_status=1, stdout="", stderr="volume is in use")

    monkeypatch.setattr(docker_remote.ssh, "run", fake_run)
    assert await docker_remote.volume_remove(None, "mp-test-workspace") is False


# --- disk_usage -----------------------------------------------------------------


async def test_disk_usage_reads_the_docker_root_filesystem(monkeypatch) -> None:
    fake = _fake(monkeypatch, "/var/lib/docker\n", "132011507712 74184519680\n")
    disk = await docker_remote.disk_usage(None)
    assert disk == docker_remote.DiskUsage(total_bytes=132011507712, used_bytes=74184519680)
    assert "/var/lib/docker" in fake.commands[1]


async def test_disk_usage_falls_back_when_docker_info_is_empty(monkeypatch) -> None:
    _fake(monkeypatch, "\n", "1000 500\n")
    disk = await docker_remote.disk_usage(None)
    assert disk == docker_remote.DiskUsage(total_bytes=1000, used_bytes=500)


# --- volume_usage ----------------------------------------------------------------


async def test_volume_usage_parses_sizes_and_project_labels(monkeypatch) -> None:
    rows = [
        {
            "Name": "mp-demo-abcd1234-workspace",
            "Size": "60.41MB",
            "Labels": "moonphase=1,moonphase.project=mp-demo-abcd1234",
        },
        {"Name": "moonphase_db-data", "Size": "3.554kB", "Labels": ""},
    ]
    _fake(monkeypatch, json.dumps(rows))
    volumes = await docker_remote.volume_usage(None)
    assert volumes[0] == docker_remote.VolumeUsage(
        name="mp-demo-abcd1234-workspace",
        project_label="mp-demo-abcd1234",
        size_bytes=60_410_000,
    )
    assert volumes[1].project_label is None
    assert volumes[1].size_bytes == 3554


async def test_volume_usage_is_empty_on_bad_output(monkeypatch) -> None:
    _fake(monkeypatch, "not json")
    assert await docker_remote.volume_usage(None) == []


# --- container_stats ---------------------------------------------------------------


async def test_container_stats_is_a_noop_with_no_names(monkeypatch) -> None:
    fake = _fake(monkeypatch)
    assert await docker_remote.container_stats(None, []) == {}
    assert fake.commands == []


async def test_container_stats_parses_cpu_and_mem(monkeypatch) -> None:
    line = json.dumps(
        {"Name": "mp-demo-abcd1234", "CPUPerc": "2.60%", "MemUsage": "92.18MiB / 31.34GiB"}
    )
    _fake(monkeypatch, line + "\n")
    stats = await docker_remote.container_stats(None, ["mp-demo-abcd1234"])
    assert stats["mp-demo-abcd1234"].cpu_percent == 2.60
    assert stats["mp-demo-abcd1234"].mem_bytes == int(92.18 * 1024**2)
