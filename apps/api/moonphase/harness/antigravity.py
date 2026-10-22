"""Antigravity harness.

Google's Antigravity CLI, `agy`: a native Go binary rather than an npm package,
and a terminal TUI like Claude Code, so tmux, the PTY bridge, attach and detach
need nothing new. Every fact below was checked against the shipped 1.4.3 binary
— its own `--help`, its log, the files it writes and the requests it makes —
rather than taken from the website, which disagrees with the binary in several
places.

Everything it keeps lives under `$HOME/.gemini`: `antigravity-cli/` is the
CLI's own state (settings, sign-in, conversations), `config/` is shared with the
desktop app (MCP servers, skills, rules). No variable relocates either, so a
session's own HOME is what keeps two people's accounts apart in one container,
exactly as for Claude Code.

Two ways in, both verified:

* **A Google account.** The sign-in is a copy-and-paste flow: it prints an
  accounts.google.com URL whose redirect is a page on antigravity.google that
  shows a code, and waits for that code to be typed back. No localhost
  callback, so the relay in login.py can drive it. In a container there is no
  D-Bus, so the token goes to a file rather than a keyring, and that file is
  what gets harvested and replayed.
* **A Gemini API key.** `GEMINI_API_KEY` alone does nothing — agy still asks
  for a Google sign-in. It also needs `"modelProvider": "gemini"` in its
  settings file, which is why that file is merged rather than left alone.
"""

from __future__ import annotations

import json
import logging
import re
import shlex
from dataclasses import replace
from typing import Any

from .base import (
    Harness,
    HarnessAuthMode,
    HarnessCredential,
    HarnessKind,
    LaunchSpec,
    SessionSpace,
    register,
)
from .layers import (
    merge_instruction_layers,
    merge_mcp_layers,
    merge_skills_layers,
    parse_object,
    row_skills,
)

log = logging.getLogger(__name__)

__all__ = ["Antigravity"]

# Where Google's own installer fetches releases from: one manifest per
# platform, `{version, url, sha512}`, always naming the current release.
MANIFEST_BASE = "https://antigravity-cli-auto-updater-974169037036.us-central1.run.app/manifests"

# The sign-in, as agy writes it when there is no keyring to put it in. The name
# is assembled at runtime (`<prefix>-oauth-token`), which is why it is not a
# literal in the binary; it was confirmed by tracing which file agy opens.
TOKEN_FILE = "antigravity-oauth-token"

# What a forgotten sign-in is overwritten with: valid JSON that agy reads as no
# sign-in at all (see `merge_into_credentials_file`).
_SIGNED_OUT = "{}\n"

# What its picker calls the personal Google account sign-in, and the redirect
# that marks a URL as agy's own rather than any Google link on screen.
_URL_START = "https://accounts.google.com/o/oauth2/auth?"
_URL_RE = re.compile(r"https://accounts\.google\.com/o/oauth2/auth\?[A-Za-z0-9._~%&=+\-/:]+")
_REDIRECT = "redirect_uri=https%3A%2F%2Fantigravity.google%2Foauth-callback"


def _app_dir(space: SessionSpace) -> str:
    """The CLI's own state: settings, sign-in, conversations, logs."""
    return f"{space.home}/.gemini/antigravity-cli"


def _config_dir(space: SessionSpace) -> str:
    """State shared with the Antigravity desktop app: MCP servers, skills."""
    return f"{space.home}/.gemini/config"


def _token_path(space: SessionSpace) -> str:
    return f"{_app_dir(space)}/{TOKEN_FILE}"


# --- the transcript ------------------------------------------------------------
#
# agy writes one JSON line per finished step to
# brain/<conversation>/.system_generated/logs/transcript_full.jsonl, append-only.
# (`transcript.jsonl` beside it is the same steps with every tool argument
# re-encoded as a JSON string and long fields truncated, so the full one is the
# one to read.) A step waiting on a permission prompt is not written until it is
# answered, and a reply interrupted half-way is never written at all.

# Tools whose most useful one-line summary is a particular argument. Names and
# argument names are the ones agy declares to the model.
_TOOL_SUMMARY_FIELD = {
    "read_file": "Path",
    "write_file": "Path",
    "edit_file": "Path",
    "run_command": "CommandLine",
    "search_web": "query",
    "invoke_subagent": "Prompt",
    "define_subagent": "Name",
    "send_message": "Message",
    "schedule": "Prompt",
    "manage_task": "Action",
    "manage_subagents": "Action",
}

_USER_REQUEST = re.compile(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", re.DOTALL)

# Every tool result opens with these, which say nothing a reader wants.
_RESULT_PREAMBLE = re.compile(r"^(?:Created At|Completed At):.*$", re.MULTILINE)

# How a finished `run_command` result begins. A command that exits non-zero is
# still a step that ran to completion, so agy records it as "DONE" — only its
# own TUI says "failed" — and the code on this line is the one sign of it.
_EXIT_CODE = re.compile(r"Command exited with code (-?\d+)\.")


def _summarise_tool(name: str, args: Any) -> str:
    if not isinstance(args, dict):
        return ""
    value: Any = None
    if name == "ask_question":
        questions = args.get("questions")
        if isinstance(questions, list) and questions and isinstance(questions[0], dict):
            value = questions[0].get("question")
    else:
        field = _TOOL_SUMMARY_FIELD.get(name)
        value = args.get(field) if field else None
    if not isinstance(value, str) or not value.strip():
        value = next((v for v in args.values() if isinstance(v, str) and v.strip()), "")
    return " ".join(str(value).split())[:160]


def _attach_diff(event: Any, name: str, args: Any) -> None:
    """Give an edit its change, so it can be judged without opening the file."""
    from ..transcript import build_diff

    if not isinstance(args, dict):
        return
    if name == "edit_file":
        before, after = args.get("Target"), args.get("Replacement")
    elif name == "write_file":
        # A new file, or an append, is entirely additions.
        before, after = "", args.get("Content")
    else:
        return
    if not isinstance(before, str) or not isinstance(after, str):
        return
    lines, added, removed, truncated = build_diff(before, after)
    if not lines:
        return
    event.diff = lines
    event.added = added
    event.removed = removed
    event.truncated = truncated


def _result_lines(text: str) -> list[str]:
    return [line for line in _RESULT_PREAMBLE.sub("", text).splitlines() if line.strip()]


def _result_excerpt(text: str) -> str:
    return " ".join(" ".join(_result_lines(text)[:3]).split())[:200]


def _command_failed(text: str) -> bool:
    """Whether a result is a command that ran and exited non-zero.

    Only the result's first line is looked at: a file that was read, or a
    command's own output, can contain the same words anywhere below it.
    """
    lines = _result_lines(text)
    found = _EXIT_CODE.fullmatch(lines[0].strip()) if lines else None
    return found is not None and int(found.group(1)) != 0


def _has_project_config(row: dict[str, Any] | None) -> bool:
    """Whether a project or session sets anything this harness reads.

    Claude Code's settings.json is deliberately absent: agy's settings are a
    different file with different keys, so a project's Claude settings say
    nothing about it.
    """
    if not row:
        return False
    return bool(row.get("claude_md") or row.get("mcp_json") or row_skills(row))


def _mcp_servers(mcp_json: str | None) -> dict[str, Any]:
    """The profile's MCP servers, in the shape agy reads.

    The profile holds Claude Code's `{"mcpServers": {...}}`, and agy reads it
    exactly as it is. Checked against the shipped 1.4.3 binary, both by
    `agy mcp list` and by what the agent itself connects to: a local server
    is `command`, `args` and `env`, and is started; a remote one is `url`
    plus `headers`, which go with every request. agy's own `agy mcp add`
    writes the older `serverUrl` instead, but the agent connects to a bare
    `url` just the same, and prefers `url` when an entry has both, so
    nothing is translated. Claude's `"type"` key is accepted. An `"sse"`
    entry is listed as http and its first request is a streamable-HTTP POST;
    whether a server that speaks only the older SSE transport works was not
    checked.
    """
    servers = parse_object(mcp_json).get("mcpServers")
    if not isinstance(servers, dict):
        return {}
    return {str(name): entry for name, entry in servers.items() if isinstance(entry, dict)}


class Antigravity(Harness):
    kind = HarnessKind.ANTIGRAVITY
    display_name = "Antigravity"
    # The Google account first: it is what the CLI itself leads with, and what
    # an individual's plan allowance is attached to. A Gemini API key is the
    # alternative, billed through the Gemini API instead.
    supported_auth_modes = (HarnessAuthMode.OAUTH, HarnessAuthMode.API_KEY)
    # Not on npm — the `agy` and `antigravity-cli` packages there are unrelated
    # placeholders — but released the same way Google's own installer reads,
    # which harness_updates.py follows to keep it current.
    managed_manifest = MANIFEST_BASE
    managed_binary = "agy"
    # Where every session that runs it has its state, written by
    # `profile_files` before its launch asks for an update — so a container
    # downloads agy once a session there runs it, and not before.
    managed_marker = ".gemini"
    # `--continue` finds the conversation by the worktree's absolute path,
    # and a duplicate's is a different one.
    duplicate_resumes = False

    def managed_env(self) -> dict[str, str]:
        # Checked against the shipped 1.4.3 binary: only the exact string
        # "true" switches its updater off; "1" — what the other harnesses
        # take — is ignored. Without it, every start (even on the sign-in
        # screen) looks to update agy, and if the binary's directory is
        # writable it spawns a detached `agy --bg-updater` that outlives the
        # session and replaces the binary in place. Where Moonphase installs
        # agy that directory is root's, and the updater stands down at that
        # check before reaching the network — a second guard, but one that is
        # gone the day agy runs from somewhere writable, such as a copy
        # someone installs into their own home.
        return {"AGY_CLI_DISABLE_AUTO_UPDATE": "true"}

    def launch_spec(
        self, *, resume: bool = False, credential: HarnessCredential | None = None
    ) -> LaunchSpec:
        # Which way it authenticates is decided by its settings file, not its
        # argv (see `merge_into_profile_file`).
        del credential
        # `--continue` reopens the last conversation in this working directory
        # — agy keys that by absolute path — and quietly starts a new one when
        # there is none, so it is safe on a fresh worktree.
        command = ["agy", "--continue"] if resume else ["agy"]
        return LaunchSpec(command=command, workdir="/workspace", env={})

    def credential_files(
        self, credential: HarnessCredential, space: SessionSpace
    ) -> dict[str, str]:
        """The Google sign-in, replayed verbatim where agy reads it.

        It is `{"token": {access_token, refresh_token, expiry, ...},
        "auth_method": "consumer"}`; agy refreshes an expired access token
        from the refresh token on its own, so the blob captured at sign-in
        keeps working when it is written back at every session start.
        """
        if credential.mode is HarnessAuthMode.OAUTH and credential.oauth_blob:
            return {_token_path(space): credential.oauth_blob}
        return {}

    def credential_env(self, credential: HarnessCredential) -> dict[str, str]:
        # GEMINI_API_KEY is the only variable it takes a key from:
        # GOOGLE_API_KEY is refused with a message naming GEMINI_API_KEY.
        if credential.mode is HarnessAuthMode.API_KEY and credential.api_key:
            return {"GEMINI_API_KEY": credential.api_key}
        return {}

    def profile_files(self, profile: Any, space: SessionSpace) -> dict[str, str]:
        """The user's global instructions and MCP servers.

        The instructions go to `~/.gemini/AGENTS.md`, one of the global rule
        files agy loads, under the name it shares with OpenCode — the same
        text people already write for this, rather than a third copy of it.
        Checked against the shipped 1.4.3 binary: the file's text reaches the
        model unchanged, as the `user_global` rule in its system prompt, with
        no frontmatter needed. Only this one of agy's global rule files is
        written: it loads every one it finds (`~/.gemini/GEMINI.md` and the
        two under `config/` as well), each as a rule of its own, so a second
        copy would reach the model twice. It is written on every start, as a
        bare newline when there are no instructions: nothing in profile.apply
        deletes a file, so skipping it would leave instructions someone has
        since cleared in front of the model for good. agy reads a blank file
        as no rule at all and then sends no `user_rules` section. Not an
        empty string: asyncssh sends no end-of-file for empty input, so the
        `cat` behind profile.write_file would wait out its timeout.

        MCP servers go to `~/.gemini/config/mcp_config.json`, which is owned
        by the profile the same way, as Claude Code's user-scoped servers
        are: written on every start, so removing a server in Moonphase
        removes it here. It is always strict JSON — `agy mcp list` refuses a
        file with a comment in it. agy does not rewrite it while it runs.
        """
        servers = _mcp_servers(getattr(profile, "mcp_json", None))
        return {
            f"{space.home}/.gemini/AGENTS.md": getattr(profile, "claude_md", None) or "\n",
            f"{_config_dir(space)}/mcp_config.json": json.dumps({"mcpServers": servers}, indent=2),
        }

    def profile_file_target(self, space: SessionSpace) -> str | None:
        return f"{_app_dir(space)}/settings.json"

    # No `seed_config_files`, deliberately. Checked against the shipped 1.4.3
    # binary: the colour-scheme picker on a new HOME's first start is not
    # skipped by a `colorScheme` in settings.json — with "terminal" or "dark"
    # it still opens, on "terminal". What does skip it is
    # `cache/onboarding.json`, but either of the flags in that file skips the
    # "Terms of Service & Data Use" screen along with it, and that screen is
    # an agreement plus a data-sharing box ticked by default: the user's to
    # answer, not Moonphase's. Folder trust comes straight after, and is left
    # to the user for the same reason (it is never seeded into
    # `trustedWorkspaces`). So a session's first start shows all three. The
    # sign-in relay needs nothing seeded either: agy asks for the sign-in
    # before any of them.

    def merge_into_profile_file(self, existing: str | None, profile: Any) -> str | None:
        """Point agy at the credential this session runs on.

        `settings.json` is agy's own: it records folder trust and whatever is
        changed in `/config`, so only `modelProvider` is touched. Set to
        "gemini" it makes agy use `GEMINI_API_KEY` and skip the Google sign-in
        entirely; left in place on a session that has since moved to a Google
        account, it would go on asking for a key that is no longer there.
        """
        credential = getattr(profile, "harness_credential", None)
        if credential is None:
            return None
        try:
            doc = json.loads(existing) if existing else {}
        except json.JSONDecodeError:
            # Something is there and it is not JSON we understand. agy
            # discards an unreadable settings file and runs on defaults, but
            # it keeps it on disk; overwriting it would lose the trust
            # decisions in it for the sake of one key.
            log.warning("%s is not readable JSON; leaving it untouched", "settings.json")
            return None
        if not isinstance(doc, dict):
            return None

        before = dict(doc)
        if credential.mode is HarnessAuthMode.API_KEY:
            # The only value agy accepts for the key: anything else makes it
            # throw the whole file away.
            doc["modelProvider"] = "gemini"
        elif doc.get("modelProvider") == "gemini":
            doc.pop("modelProvider")

        if doc == before and existing is not None:
            return None
        if not doc and existing is None:
            return None
        return json.dumps(doc, indent=2)

    def skills_directory(self, space: SessionSpace) -> str | None:
        # The same `<name>/SKILL.md` folders as Claude Code's, with the same
        # frontmatter, so the profile's skills are copied over unchanged.
        # Checked against the shipped 1.4.3 binary: a skill written here is
        # listed in the model's system prompt by name, path and description,
        # and offered in the TUI as `/<name>`.
        return f"{_config_dir(space)}/skills"

    def credentials_merge_target(self, space: SessionSpace) -> str | None:
        return _token_path(space)

    def merge_into_credentials_file(
        self,
        existing: str | None,
        mcp_oauth: dict[str, str],
        *,
        keep_account_login: bool = True,
    ) -> str | None:
        """Forget a previous account's sign-in when the session no longer
        runs on one.

        With an API key agy ignores the file anyway, but it would still be
        sitting in the session's HOME — a refresh token for whichever Google
        account the session used to run on. Overwritten rather than deleted,
        since nothing in profile.apply removes files, and with `{}` rather
        than nothing: asyncssh sends no end-of-file for empty input, so an
        empty write would leave the `cat` behind profile.write_file waiting
        out its timeout. Checked against the shipped 1.4.3 binary, agy reads
        `{}` as no sign-in ("unknown auth method"), shows its sign-in screen
        when there is no key, and runs on a key undisturbed. MCP OAuth tokens
        are Claude Code's and have no equivalent here.
        """
        del mcp_oauth
        if keep_account_login or not existing or existing.strip() == _SIGNED_OUT.strip():
            return None
        return _SIGNED_OUT

    def compose_project_layers(
        self,
        profile: Any,
        project_row: dict[str, Any] | None,
        session_row: dict[str, Any] | None,
    ) -> Any:
        """Project and session instructions, MCP servers and skills.

        Layered exactly as for Claude Code, so a project's instructions reach
        whichever agent it runs. Its Claude Code settings do not: they are a
        Claude file and are left out of the profile this harness sees.
        """
        if not _has_project_config(project_row) and not _has_project_config(session_row):
            return profile

        project_row = project_row or {}
        session_row = session_row or {}
        return replace(
            profile,
            claude_md=merge_instruction_layers(
                [
                    ("Global preferences", profile.claude_md),
                    ("Project instructions", project_row.get("claude_md")),
                    ("Your session", session_row.get("claude_md")),
                ]
            ),
            mcp_json=merge_mcp_layers(
                [profile.mcp_json, project_row.get("mcp_json"), session_row.get("mcp_json")]
            ),
            skills=merge_skills_layers(
                [profile.skills, row_skills(project_row), row_skills(session_row)]
            ),
        )

    def activity_signals(self) -> Any:
        from ..activity import ActivitySignals

        return ActivitySignals(
            # Taken from a live pane and the binary's own strings. Every
            # permission prompt opens "Requesting permission for:" over a
            # title question and numbered "Yes, ..." / "No, ..." options. A
            # bare "?" would match the idle footer, "? for shortcuts", and
            # "esc to cancel" also shows while it works.
            prompt_patterns=(
                r"Requesting permission for:",
                r"(?m)^\s*(?:Run this command|Allow access to this (?:URL|file)"
                r"|Allow calling this tool|Allow creation of this file"
                r"|Accept this file edit|Do you want to proceed"
                r"|Allow sandbox bypass for command execution"
                r"|Allow administrator elevation|Allow remote debugging"
                r"|Grant requested permission)\?",
                r"(?m)^\s*>\s*\d{1,2}\.\s+(?:Yes|No)\b",
                r"Question \d+/\d+:",
                # A lapsed sign-in, past its picker: what is typed here is the
                # code from Google's page, and Enter is what submits it.
                r"paste the authorization code",
                # The same sign-in screen's first line. In a pane as short as
                # tmux's default 80x24 that screen scrolls, and the code field
                # matched just above is out of view below the URL.
                r"Your browser should open automatically",
            ),
            # A braille spinner redraws on every frame while it works, so the
            # pane never goes still; change detection covers it.
            busy_patterns=(),
            # Pressing an option's digit picks it and confirms it at once, on
            # a permission prompt and on an ask_question picker alike —
            # checked against the shipped 1.4.3 binary.
            select_submits=True,
            # Screens that ignore digits and letters and move only on arrows
            # and Enter: the first start in a new session HOME (colour scheme,
            # Google's terms and data-use choice, folder trust — the last two
            # the user's own decisions) and a lapsed sign-in's pickers. A
            # message typed into the feed would arrive with an Enter choosing
            # whatever is highlighted, so these are left to the terminal.
            arrow_key_patterns=(
                r"Do you trust the contents of this project\?",
                r"Choose your color scheme:",
                r"Terms of Service & Data Use",
                r"Select login method:",
                r"Select Google Cloud sign-in method:",
            ),
        )

    def parse_transcript_record(self, record: Any, source: str | None = None) -> list[Any]:
        """Normalise one step of an agy transcript.

        Steps carry an index but no id, and the index restarts in every
        conversation, so ids are built from the conversation's directory —
        the first part of `source` — to stay unique when the feed moves on to
        a new conversation.
        """
        from ..transcript import TranscriptEvent

        if not isinstance(record, dict):
            return []
        if record.get("status") == "CLEARED":
            return []

        conversation = (source or "").split("/", 1)[0] or "agy"
        step = record.get("step_index")
        if not isinstance(step, int):
            step = record.get("created_at") or ""
        prefix = f"{conversation}:{step}"
        at = record.get("created_at") if isinstance(record.get("created_at"), str) else None
        kind = record.get("type")
        events: list[TranscriptEvent] = []

        if kind == "USER_INPUT":
            content = record.get("content")
            if not isinstance(content, str):
                return []
            # What was typed sits inside <USER_REQUEST>; the blocks after it
            # (the time, a changed setting) are agy telling the model, not
            # the person speaking.
            match = _USER_REQUEST.search(content)
            text = (match.group(1) if match else content).strip()
            if text:
                events.append(TranscriptEvent(id=f"{prefix}:0", kind="user", text=text, at=at))
            return events

        if kind == "PLANNER_RESPONSE":
            index = 0
            thinking = record.get("thinking")
            if isinstance(thinking, str) and thinking.strip():
                events.append(
                    TranscriptEvent(
                        id=f"{prefix}:{index}", kind="thinking", text=thinking.strip(), at=at
                    )
                )
            index += 1
            content = record.get("content")
            if isinstance(content, str) and content.strip():
                events.append(
                    TranscriptEvent(
                        id=f"{prefix}:{index}", kind="assistant", text=content.strip(), at=at
                    )
                )
            index += 1
            calls = record.get("tool_calls")
            for call in calls if isinstance(calls, list) else []:
                if not isinstance(call, dict):
                    continue
                name = str(call.get("name") or "tool")
                args = call.get("args")
                event = TranscriptEvent(
                    id=f"{prefix}:{index}",
                    kind="tool",
                    tool=name,
                    text=_summarise_tool(name, args),
                    at=at,
                )
                _attach_diff(event, name, args)
                events.append(event)
                index += 1
            return events

        if kind == "GENERIC":
            # A tool's result. It names no tool; it follows the call it
            # answers, which is all the feed needs to place it. "ERROR" is a
            # tool that could not run (a missing file, a declined prompt);
            # a command that ran and failed is "DONE" with its exit code.
            errored = record.get("status") == "ERROR"
            error = record.get("error")
            content = record.get("content")
            failed = errored or (isinstance(content, str) and _command_failed(content))
            if errored and isinstance(error, str) and error.strip():
                excerpt = " ".join(error.split())[:200]
            else:
                excerpt = _result_excerpt(content) if isinstance(content, str) else ""
            if failed or excerpt:
                events.append(
                    TranscriptEvent(
                        id=f"{prefix}:0", kind="result", text=excerpt, ok=not failed, at=at
                    )
                )
            return events

        # SYSTEM_MESSAGE and the rest are agy's own bookkeeping.
        return []

    def auth_probe_script(self, space: SessionSpace) -> str:
        # A sign-in is a token file with a token in it — `access_token` is in
        # every one agy writes — not merely a file that is not empty: a
        # forgotten sign-in leaves `{}` behind (see
        # `merge_into_credentials_file`), which agy treats as signed out.
        token = shlex.quote(_token_path(space))
        return f'grep -qs \'"access_token"\' {token} || test -n "$GEMINI_API_KEY"'

    def login_command(self) -> list[str]:
        # There is no sign-in subcommand: the TUI signs in when it finds no
        # credential, so that is what the relay runs.
        return ["agy"]

    def login_url_pattern(self) -> str:
        return _URL_RE.pattern

    def login_preamble(self) -> tuple[tuple[str, str], ...]:
        # It opens on "Select login method:" with "1. Google OAuth" already
        # highlighted — the choice the person made by pressing Moonphase's
        # sign-in button.
        return ((r"Select login method:", "Enter"),)

    def login_url(self, pane: str) -> str | None:
        """The sign-in URL, rejoined from agy's own wrapping.

        agy breaks a long line itself, at the pane width, and indents every
        continuation by one space — so rejoining on newlines alone leaves
        spaces inside the URL. The URL runs from its first line to the next
        blank one. Checked against the shipped 1.4.3 binary at 200 columns
        (the relay's pane) and at 80, against the URL agy hands to
        `xdg-open`: the rejoined text is exactly that URL.

        A URL is only returned once something is drawn below it — the link
        and the code field, or in a short pane the "(1–20 of 29 lines)"
        scroll marker. A capture of a screen still being drawn has blank
        rows under its last finished line, and at 80 columns the URL's last
        row is a couple of characters of its `state`, so "up to the next
        blank line" alone would hand the relay a URL cut short that still
        passes every other check here — and the relay keeps the first URL it
        is given. The last URL in the pane is the one taken: it is the
        screen showing now.
        """
        lines = pane.splitlines()
        starts = [i for i, line in enumerate(lines) if line.strip().startswith(_URL_START)]
        if not starts:
            return None
        parts: list[str] = []
        end: int | None = None
        for index in range(starts[-1], len(lines)):
            if not lines[index].strip():
                end = index
                break
            parts.append(lines[index].strip())
        if end is None or not any(line.strip() for line in lines[end + 1 :]):
            return None
        match = _URL_RE.fullmatch("".join(parts))
        if match and _REDIRECT in match.group(0) and "&state=" in match.group(0):
            return match.group(0)
        return None

    def login_failure_patterns(self) -> tuple[str, ...]:
        # What a rejected code looks like: 'Got an error: token exchange
        # failed: oauth2: "invalid_grant" "Malformed auth code."'
        return (r"token exchange failed", r"Got an error:", r"invalid_grant")

    def credential_paths(self) -> list[str]:
        return [_token_path(SessionSpace())]

    def transcript_dir(self, space: SessionSpace) -> str:
        """Every conversation's step log is under here, one directory each.

        Nothing directly in it is a `*.jsonl`, which matters: usage, search
        and the summary glob this directory for Claude Code's format, and
        agy's own prompt history (`history.jsonl`, one level up) would be
        misread by all three.
        """
        return f"{_app_dir(space)}/brain"

    def live_transcript_script(self, space: SessionSpace) -> str:
        """The current conversation's transcript, by agy's own record of it.

        `cache/last_conversations.json` maps a working directory to the
        conversation `--continue` would reopen there; agy writes it on a
        conversation's first message. That is the live one, where "newest
        file" could be a sub-agent's conversation or another worktree's.
        Nothing is printed until there is one.
        """
        app = _app_dir(space)
        cache = shlex.quote(f"{app}/cache/last_conversations.json")
        brain = shlex.quote(f"{app}/brain")
        return (
            f"id=$(jq -r --arg w {shlex.quote(space.workdir)} '.[$w] // empty' "
            f"{cache} 2>/dev/null) || exit 0\n"
            "case $id in ''|*[!A-Za-z0-9-]*) exit 0 ;; esac\n"
            f'f={brain}/"$id"/.system_generated/logs/transcript_full.jsonl\n'
            '[ -f "$f" ] && printf \'%s\\n\' "$f"\n'
            "exit 0\n"
        )

    def version_command(self) -> list[str]:
        # Prints the bare version, writes nothing and needs no sign-in.
        return ["agy", "--version"]


register(Antigravity())
