"""Parsing `claude mcp list`'s output into a status per configured server.

A stored OAuth credential, or even the config existing at all, says nothing
about whether a server is actually reachable right now — expired tokens,
servers that never needed OAuth in the first place, a typo'd URL, and a
network that changed since the config was written all look identical to
"configured" without ever being checked. `claude mcp list` makes a real
connection attempt per server and reports the result; this only parses what
it prints. Claude Code only — no other harness this project supports has an
equivalent command.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# "name: target (TRANSPORT) - ✓ text" for http/sse, "name: command - ✘ text"
# for stdio (no parenthesised transport). The banner line ("Checking MCP
# server health…"), blank lines, and "No MCP servers configured..." all
# fail to match and are silently skipped — this reads real CLI output, not
# a format Moonphase controls, so an unrecognised line is not an error.
_LINE = re.compile(
    r"^(?P<name>[^:]+):\s+(?P<target>.+?)(?:\s+\((?P<transport>[A-Za-z]+)\))?"
    r"\s+-\s+(?P<symbol>[✓✘])\s+(?P<detail>.*)$"
)


@dataclass
class McpHealth:
    name: str
    ok: bool
    detail: str


def parse(output: str) -> list[McpHealth]:
    out: list[McpHealth] = []
    for line in output.splitlines():
        match = _LINE.match(line.strip())
        if not match:
            continue
        out.append(
            McpHealth(
                name=match.group("name").strip(),
                ok=match.group("symbol") == "✓",
                detail=match.group("detail").strip(),
            )
        )
    return out
