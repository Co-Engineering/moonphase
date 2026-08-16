"""Claude Code harness."""

from __future__ import annotations

import json
from typing import Any

from .base import (
    AuthStatus,
    Harness,
    HarnessAuthMode,
    HarnessCredential,
    HarnessKind,
    LaunchSpec,
    register,
)

__all__ = ["ClaudeCode", "AuthStatus"]

# Claude Code stores per-project transcripts under ~/.claude/projects/<slug>,
# where <slug> is the working directory with every '/' turned into '-'.
CLAUDE_HOME = "/home/dev/.claude"

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


def _project_slug(workdir: str) -> str:
    return workdir.replace("/", "-")


class ClaudeCode(Harness):
    kind = HarnessKind.CLAUDE_CODE
    display_name = "Claude Code"
    # OAuth first: it uses the user's existing Pro/Max subscription, which is
    # what most people actually want. API key is the fallback for teams.
    supported_auth_modes = (HarnessAuthMode.OAUTH, HarnessAuthMode.API_KEY)

    def launch_spec(self) -> LaunchSpec:
        # Launched through a wrapper (see sessions.py) so the pane survives the
        # harness exiting and drops to a shell instead of killing the window.
        return LaunchSpec(command=["claude"], workdir="/workspace", env={})

    def credential_files(self, credential: HarnessCredential) -> dict[str, str]:
        if credential.mode is HarnessAuthMode.OAUTH and credential.oauth_blob:
            return {f"{CLAUDE_HOME}/.credentials.json": credential.oauth_blob}
        return {}

    def credential_env(self, credential: HarnessCredential) -> dict[str, str]:
        if credential.mode is HarnessAuthMode.API_KEY and credential.api_key:
            return {"ANTHROPIC_API_KEY": credential.api_key}
        if credential.oauth_token:
            # What `claude setup-token` produces. Scoped to inference only,
            # which is exactly what a coding session needs.
            return {"CLAUDE_CODE_OAUTH_TOKEN": credential.oauth_token}
        return {}

    def seed_config_files(self) -> dict[str, str]:
        # Skips the theme picker on first attach. Deliberately does NOT set
        # the per-project trust flag: that prompt guards against hostile
        # content in a cloned repo, and answering it is the user's call, not
        # something Moonphase should quietly do for them.
        return {
            "/home/dev/.claude.json": json.dumps(
                {"hasCompletedOnboarding": True, "theme": "dark"}
            )
        }

    def profile_files(self, profile: Any) -> dict[str, str]:
        """The user's global Claude Code configuration.

        `settings.json` and the global `CLAUDE.md` are exactly the things
        people expect to set once and have everywhere, so they are owned by
        the profile and overwritten on each session start.
        """
        files: dict[str, str] = {}
        if profile.claude_settings_json:
            files[f"{CLAUDE_HOME}/settings.json"] = profile.claude_settings_json
        if profile.claude_md:
            files[f"{CLAUDE_HOME}/CLAUDE.md"] = profile.claude_md
        if profile.mcp_json:
            files[f"{CLAUDE_HOME}/.mcp.json"] = profile.mcp_json
        return files

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
                events.append(
                    TranscriptEvent(
                        id=block_id,
                        kind="tool",
                        tool=name,
                        text=_summarise_tool(name, block.get("input")),
                        at=at,
                        sidechain=sidechain,
                    )
                )
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

    def auth_probe_script(self) -> str:
        return (
            f'test -s "{CLAUDE_HOME}/.credentials.json" '
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

    def transcript_dir(self, workdir: str = "/workspace") -> str:
        return f"{CLAUDE_HOME}/projects/{_project_slug(workdir)}"

    def credential_paths(self) -> list[str]:
        """Files to harvest after an interactive login succeeds."""
        return [f"{CLAUDE_HOME}/.credentials.json"]

    def version_command(self) -> list[str]:
        return ["claude", "--version"]


register(ClaudeCode())
