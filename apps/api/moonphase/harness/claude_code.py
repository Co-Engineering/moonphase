"""Claude Code harness."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

from .base import (
    AuthStatus,
    Harness,
    HarnessAuthMode,
    HarnessCredential,
    HarnessKind,
    LaunchSpec,
    SessionSpace,
    register,
)

__all__ = ["ClaudeCode", "AuthStatus"]

# Claude Code keeps everything — credentials, settings, history and per-project
# transcripts — under $HOME/.claude, which is why giving each session its own
# HOME is enough to keep two people's accounts apart in one container.
# Transcripts land in <config>/projects/<slug>, where <slug> is the working
# directory with every '/' turned into '-'.
def _claude_home(space: SessionSpace) -> str:
    return f"{space.home}/.claude"

# The OAuth authorization URL `claude setup-token` prints for the user to open.
LOGIN_URL_PATTERN = r"https://claude\.com/\S*oauth\S*"



# Tools whose most useful one-line summary is a particular input field. Falling
# back to the first string keeps an unknown tool readable rather than blank.
_TOOL_SUMMARY_FIELD = {
    "Read": "file_path",
    "Edit": "file_path",
    "Write": "file_path",
    "NotebookEdit": "notebook_path",
    "Bash": "command",
    "Grep": "pattern",
    "Glob": "pattern",
    "Task": "description",
    "WebFetch": "url",
    "WebSearch": "query",
    "Skill": "skill",
}


def _summarise_tool(name: str, tool_input: Any) -> str:
    if not isinstance(tool_input, dict):
        return ""
    field = _TOOL_SUMMARY_FIELD.get(name)
    value = tool_input.get(field) if field else None
    if value is None:
        value = next(
            (v for v in tool_input.values() if isinstance(v, str) and v.strip()), ""
        )
    text = " ".join(str(value).split())
    return text[:160]


def _result_excerpt(content: Any) -> str:
    """A tool result reduced to something that fits on a phone."""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = [
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        text = "\n".join(p for p in parts if p)
    else:
        text = ""
    lines = [line for line in text.splitlines() if line.strip()]
    return " ".join(" ".join(lines[:3]).split())[:200]


def _attach_diff(event: Any, name: str, tool_input: Any) -> None:
    """Give an edit its change, so it can be judged without opening the file.

    "Do you want to make this edit?" is unanswerable from a file path alone,
    and the file path is all a phone would otherwise have.
    """
    from ..transcript import build_diff

    if not isinstance(tool_input, dict):
        return

    if name == "Edit":
        before = tool_input.get("old_string")
        after = tool_input.get("new_string")
        if not isinstance(before, str) or not isinstance(after, str):
            return
    elif name == "Write":
        # A new file is entirely additions; showing it against nothing is the
        # honest rendering.
        before = ""
        after = tool_input.get("content")
        if not isinstance(after, str):
            return
    else:
        return

    lines, added, removed, truncated = build_diff(before, after)
    if not lines:
        return
    event.diff = lines
    event.added = added
    event.removed = removed
    event.truncated = truncated


def _project_slug(workdir: str) -> str:
    return workdir.replace("/", "-")


# --- layering project- and session-level config over the org profile --------
#
# Three scopes (org, project, session) compose into one effective config,
# entirely under the session's own $HOME rather than into the project's git
# checkout — see `profile_file_target` above for why `.mcp.json` at the repo
# root is the wrong place for this. A more specific scope wins a scalar
# setting; CLAUDE.md and MCP servers are additive; permission rules union with
# the stricter decision winning, so a project-wide deny cannot be quietly
# reopened by someone's own session settings.

_PERMISSION_DECISIONS = ("deny", "ask", "allow")  # most restrictive first


def _parse_object(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _merge_settings_layers(layers: list[str | None]) -> str | None:
    merged: dict[str, Any] = {}
    decided: dict[str, str] = {}  # "Tool(pattern)" -> decision

    for raw in layers:
        doc = _parse_object(raw)
        for key, value in doc.items():
            if key != "permissions":
                merged[key] = value
        permissions = doc.get("permissions")
        if not isinstance(permissions, dict):
            continue
        for decision in _PERMISSION_DECISIONS:
            rules = permissions.get(decision)
            if not isinstance(rules, list):
                continue
            for rule in rules:
                if not isinstance(rule, str):
                    continue
                current = decided.get(rule)
                if current is None or (
                    _PERMISSION_DECISIONS.index(decision)
                    < _PERMISSION_DECISIONS.index(current)
                ):
                    decided[rule] = decision

    if decided:
        by_decision: dict[str, list[str]] = {}
        for rule, decision in decided.items():
            by_decision.setdefault(decision, []).append(rule)
        merged["permissions"] = by_decision

    return json.dumps(merged) if merged else None


def _merge_claude_md_layers(layers: list[tuple[str, str | None]]) -> str | None:
    """Concatenate CLAUDE.md layers, broadest first.

    Same order Claude Code itself loads nested CLAUDE.md files in: user, then
    project, then the most specific one.
    """
    parts = [
        f"# {label}\n\n{text.strip()}"
        for label, text in layers
        if text and text.strip()
    ]
    return "\n\n---\n\n".join(parts) if parts else None


def _merge_mcp_layers(layers: list[str | None]) -> str | None:
    servers: dict[str, Any] = {}
    for raw in layers:
        layer_servers = _parse_object(raw).get("mcpServers")
        if isinstance(layer_servers, dict):
            servers.update(layer_servers)
    return json.dumps({"mcpServers": servers}) if servers else None


def _merge_skills_layers(layers: list[dict[str, str]]) -> dict[str, str]:
    merged: dict[str, str] = {}
    for layer in layers:
        merged.update(layer)
    return merged


def _row_skills(row: dict[str, Any] | None) -> dict[str, str]:
    if not row:
        return {}
    from ..profile import parse_json_object

    return {str(k): str(v) for k, v in parse_json_object(row.get("skills_json")).items()}


def _row_has_config(row: dict[str, Any] | None) -> bool:
    if not row:
        return False
    return bool(
        row.get("claude_settings_json")
        or row.get("claude_md")
        or row.get("mcp_json")
        or _row_skills(row)
    )


class ClaudeCode(Harness):
    kind = HarnessKind.CLAUDE_CODE
    display_name = "Claude Code"
    # OAuth first: it uses the user's existing Pro/Max subscription, which is
    # what most people actually want. API key is the fallback for teams.
    supported_auth_modes = (HarnessAuthMode.OAUTH, HarnessAuthMode.API_KEY)

    def launch_spec(
        self, *, resume: bool = False, credential: HarnessCredential | None = None
    ) -> LaunchSpec:
        # Tied to one provider, so the credential says nothing about how to
        # start it.
        del credential
        # Launched through a wrapper (see sessions.py) so the pane survives the
        # harness exiting and drops to a shell instead of killing the window.
        #
        # `--continue` reopens the most recent conversation in the working
        # directory, which is what makes a session survive its container being
        # restarted underneath it. Claude Code falls back to a new conversation
        # when there is nothing to continue, so this is safe on a fresh
        # workspace.
        command = ["claude", "--continue"] if resume else ["claude"]
        return LaunchSpec(command=command, workdir="/workspace", env={})

    def credential_files(
        self, credential: HarnessCredential, space: SessionSpace
    ) -> dict[str, str]:
        if credential.mode is HarnessAuthMode.OAUTH and credential.oauth_blob:
            return {f"{_claude_home(space)}/.credentials.json": credential.oauth_blob}
        return {}

    def credential_env(self, credential: HarnessCredential) -> dict[str, str]:
        if credential.mode is HarnessAuthMode.API_KEY and credential.api_key:
            return {"ANTHROPIC_API_KEY": credential.api_key}
        if credential.oauth_token:
            # What `claude setup-token` produces. Scoped to inference only,
            # which is exactly what a coding session needs.
            return {"CLAUDE_CODE_OAUTH_TOKEN": credential.oauth_token}
        return {}

    def seed_config_files(self, space: SessionSpace) -> dict[str, str]:
        # Skips the theme picker on first attach. Deliberately does NOT set
        # the per-project trust flag: that prompt guards against hostile
        # content in a cloned repo, and answering it is the user's call, not
        # something Moonphase should quietly do for them.
        return {
            f"{space.home}/.claude.json": json.dumps(
                {"hasCompletedOnboarding": True, "theme": "dark"}
            )
        }

    def profile_files(self, profile: Any, space: SessionSpace) -> dict[str, str]:
        """The user's global Claude Code configuration.

        `settings.json` and the global `CLAUDE.md` are exactly the things
        people expect to set once and have everywhere, so they are owned by
        the profile and overwritten on each session start. MCP servers are
        not here: Claude Code reads user-scoped servers from `~/.claude.json`
        (see `profile_file_target`/`merge_into_profile_file`), not from a
        file under `~/.claude`.
        """
        home = _claude_home(space)
        files: dict[str, str] = {}
        if profile.claude_settings_json:
            files[f"{home}/settings.json"] = profile.claude_settings_json
        if profile.claude_md:
            files[f"{home}/CLAUDE.md"] = profile.claude_md
        return files

    def profile_file_target(self, space: SessionSpace) -> str | None:
        return f"{space.home}/.claude.json"

    def merge_into_profile_file(self, existing: str | None, profile: Any) -> str | None:
        """Merge the org's MCP servers into Claude Code's own state file.

        `~/.claude.json` is where Claude Code keeps trust decisions, project
        history and the rest of its own mutable state, and also where it
        looks for user-scoped MCP servers — under a top-level `mcpServers`
        key. Overwriting the whole file the way `profile_files()` does for
        `settings.json` would throw that state away, so only that one key is
        replaced.
        """
        try:
            doc = json.loads(existing) if existing else {}
        except json.JSONDecodeError:
            doc = {}
        if not isinstance(doc, dict):
            doc = {}

        servers = None
        if profile.mcp_json:
            try:
                parsed = json.loads(profile.mcp_json)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                servers = parsed.get("mcpServers")

        if isinstance(servers, dict):
            doc["mcpServers"] = servers
        else:
            doc.pop("mcpServers", None)

        return json.dumps(doc)

    def skills_directory(self, space: SessionSpace) -> str | None:
        return f"{_claude_home(space)}/skills"

    def credentials_merge_target(self, space: SessionSpace) -> str | None:
        return f"{_claude_home(space)}/.credentials.json"

    def merge_into_credentials_file(
        self, existing: str | None, mcp_oauth: dict[str, str]
    ) -> str | None:
        """Merge saved MCP server OAuth tokens into Claude Code's own credentials file.

        This is the same file the account's own OAuth credential lives in
        (`credential_files()`, above) — on OAuth mode that has already
        overwritten it wholesale by the time this runs, and on API-key mode
        it may not exist at all yet either way. Claude Code stores an MCP
        server's token under a top-level `mcpOAuth` key, keyed by
        `"<server-name>|<hash>"` — the hash is Claude Code's own and not
        recomputed here; each entry is replayed exactly as captured.
        """
        try:
            doc = json.loads(existing) if existing else {}
        except json.JSONDecodeError:
            doc = {}
        if not isinstance(doc, dict):
            doc = {}

        existing_oauth = doc.get("mcpOAuth")
        merged_oauth = dict(existing_oauth) if isinstance(existing_oauth, dict) else {}

        for server_name, raw_entry in mcp_oauth.items():
            try:
                parsed = json.loads(raw_entry)
            except json.JSONDecodeError:
                continue
            if not isinstance(parsed, dict):
                continue
            # A server's hash changes if its config does, so drop whatever was
            # there for this name before adding the current entry — otherwise
            # a stale, no-longer-valid hash lingers alongside the live one.
            for key in [k for k in merged_oauth if k.split("|", 1)[0] == server_name]:
                del merged_oauth[key]
            merged_oauth.update(parsed)

        if merged_oauth:
            doc["mcpOAuth"] = merged_oauth
        else:
            doc.pop("mcpOAuth", None)

        return json.dumps(doc)

    def compose_project_layers(
        self,
        profile: Any,
        project_row: dict[str, Any] | None,
        session_row: dict[str, Any] | None,
    ) -> Any:
        # No project- or session-level config exists: leave the org profile
        # exactly as it was rather than round-tripping it through JSON
        # parsing and a "Global preferences" header nobody asked for. Most
        # projects and sessions never set any of this, and that case must be
        # byte-for-byte what it always was.
        if not _row_has_config(project_row) and not _row_has_config(session_row):
            return profile

        project_row = project_row or {}
        session_row = session_row or {}

        settings = _merge_settings_layers(
            [
                profile.claude_settings_json,
                project_row.get("claude_settings_json"),
                session_row.get("claude_settings_json"),
            ]
        )
        claude_md = _merge_claude_md_layers(
            [
                ("Global preferences", profile.claude_md),
                ("Project instructions", project_row.get("claude_md")),
                ("Your session", session_row.get("claude_md")),
            ]
        )
        mcp_json = _merge_mcp_layers(
            [
                profile.mcp_json,
                project_row.get("mcp_json"),
                session_row.get("mcp_json"),
            ]
        )
        skills = _merge_skills_layers(
            [profile.skills, _row_skills(project_row), _row_skills(session_row)]
        )

        return replace(
            profile,
            claude_settings_json=settings,
            claude_md=claude_md,
            mcp_json=mcp_json,
            skills=skills,
        )

    def activity_signals(self) -> Any:
        from ..activity import ActivitySignals

        return ActivitySignals(
            # Blocking questions. Verified against the shipped binary's own
            # strings rather than guessed: permission prompts are phrased
            # "Do you want to ...", and selections offer numbered options with
            # "Enter to confirm".
            prompt_patterns=(
                r"Do you want to\b",
                r"❯\s*1\.",
                r"Enter to confirm",
                r"\(y/n\)",
                r"Press Enter to continue",
            ),
            # Claude Code composes its interrupt hint dynamically, so there is
            # no stable literal to match. Change detection covers this.
            busy_patterns=(),
        )


    def parse_transcript_record(self, record: Any) -> list[Any]:
        """Normalise one Claude Code transcript line.

        Only `user` and `assistant` records carry conversation; the rest are
        bookkeeping (file history, modes, titles) that a reader does not want
        to see. Thinking blocks are emitted but tagged, so the UI can offer
        them without them dominating a small screen.
        """
        from ..transcript import TranscriptEvent

        if not isinstance(record, dict):
            return []

        kind = record.get("type")
        if kind not in ("user", "assistant"):
            return []

        message = record.get("message")
        if not isinstance(message, dict):
            return []

        uuid = str(record.get("uuid") or "")
        at = record.get("timestamp")
        sidechain = bool(record.get("isSidechain"))
        content = message.get("content")
        events: list[TranscriptEvent] = []

        # A plain string is what a typed prompt looks like.
        if isinstance(content, str):
            text = content.strip()
            if text:
                events.append(
                    TranscriptEvent(
                        id=uuid, kind="user", text=text, at=at, sidechain=sidechain
                    )
                )
            return events

        if not isinstance(content, list):
            return []

        for index, block in enumerate(content):
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            # Blocks share the record's uuid, so index keeps ids unique for
            # client-side keying and de-duplication.
            block_id = f"{uuid}:{index}"

            if block_type == "text":
                text = str(block.get("text", "")).strip()
                if text:
                    events.append(
                        TranscriptEvent(
                            id=block_id,
                            kind="user" if kind == "user" else "assistant",
                            text=text,
                            at=at,
                            sidechain=sidechain,
                        )
                    )
            elif block_type == "thinking":
                text = str(block.get("thinking", "")).strip()
                if text:
                    events.append(
                        TranscriptEvent(
                            id=block_id, kind="thinking", text=text, at=at,
                            sidechain=sidechain,
                        )
                    )
            elif block_type == "tool_use":
                name = str(block.get("name", "tool"))
                event = TranscriptEvent(
                    id=block_id,
                    kind="tool",
                    tool=name,
                    text=_summarise_tool(name, block.get("input")),
                    at=at,
                    sidechain=sidechain,
                )
                _attach_diff(event, name, block.get("input"))
                events.append(event)
            elif block_type == "tool_result":
                is_error = bool(block.get("is_error"))
                excerpt = _result_excerpt(block.get("content"))
                # A successful result is usually noise; an error never is.
                if is_error or excerpt:
                    events.append(
                        TranscriptEvent(
                            id=block_id,
                            kind="result",
                            text=excerpt,
                            ok=not is_error,
                            at=at,
                            sidechain=sidechain,
                        )
                    )

        return events

    def auth_probe_script(self, space: SessionSpace) -> str:
        return (
            f'test -s "{_claude_home(space)}/.credentials.json" '
            '|| test -n "$ANTHROPIC_API_KEY" '
            '|| test -n "$CLAUDE_CODE_OAUTH_TOKEN"'
        )

    def auth_status_script(self) -> str:
        # Authoritative, unlike checking for a file: it reports whether the
        # credential actually works and which method is in use.
        return "claude auth status --json 2>/dev/null"

    def login_command(self) -> list[str]:
        # Produces a long-lived token tied to the user's Claude subscription,
        # which is what makes a single global sign-in possible.
        return ["claude", "setup-token"]

    def login_url_pattern(self) -> str:
        return LOGIN_URL_PATTERN

    def transcript_dir(self, space: SessionSpace) -> str:
        return f"{_claude_home(space)}/projects/{_project_slug(space.workdir)}"

    def credential_paths(self) -> list[str]:
        """Files to harvest after an interactive login succeeds.

        The sign-in relay runs in a throwaway container of its own, not in a
        session, so this is the plain default location.
        """
        return [f"{_claude_home(SessionSpace())}/.credentials.json"]

    def version_command(self) -> list[str]:
        return ["claude", "--version"]


register(ClaudeCode())
