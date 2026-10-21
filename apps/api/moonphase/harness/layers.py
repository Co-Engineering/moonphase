"""Layering project- and session-level config over the org profile.

Three scopes (org, project, session) compose into one effective config. The
pieces here are the ones that mean the same thing to every harness that has
them — instructions, MCP servers and skills — so a harness reusing the profile's
text for its own files layers it the same way Claude Code does. Settings and
permissions are each harness's own and are merged in its own module.
"""

from __future__ import annotations

import json
from typing import Any


def parse_object(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def merge_instruction_layers(layers: list[tuple[str, str | None]]) -> str | None:
    """Concatenate instruction layers (the profile's CLAUDE.md text), broadest
    first.

    Same order Claude Code itself loads nested CLAUDE.md files in: user, then
    project, then the most specific one.
    """
    parts = [
        f"# {label}\n\n{text.strip()}"
        for label, text in layers
        if text and text.strip()
    ]
    return "\n\n---\n\n".join(parts) if parts else None


def merge_mcp_layers(layers: list[str | None]) -> str | None:
    servers: dict[str, Any] = {}
    for raw in layers:
        layer_servers = parse_object(raw).get("mcpServers")
        if isinstance(layer_servers, dict):
            servers.update(layer_servers)
    return json.dumps({"mcpServers": servers}) if servers else None


def merge_skills_layers(layers: list[dict[str, str]]) -> dict[str, str]:
    merged: dict[str, str] = {}
    for layer in layers:
        merged.update(layer)
    return merged


def row_skills(row: dict[str, Any] | None) -> dict[str, str]:
    if not row:
        return {}
    from ..profile import parse_json_object

    return {str(k): str(v) for k, v in parse_json_object(row.get("skills_json")).items()}
