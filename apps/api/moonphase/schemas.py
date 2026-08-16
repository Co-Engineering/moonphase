"""Request and response models for the HTTP API."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

SshAuthMode = Literal["password_bootstrap", "managed_key", "provided_key"]
HarnessKindStr = Literal["claude_code", "opencode"]
HarnessAuthModeStr = Literal["oauth", "api_key"]


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --- organizations ----------------------------------------------------------


class OrganizationOut(ORMModel):
    id: UUID
    name: str
    slug: str
    is_personal: bool
    role: str | None = None
    created_at: datetime


# --- servers ----------------------------------------------------------------


class ServerCreate(BaseModel):
    org_id: UUID | None = Field(
        default=None,
        description="Defaults to the caller's personal organization.",
    )
    name: str = Field(min_length=1, max_length=64)
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(default=22, ge=1, le=65535)
    ssh_user: str = Field(min_length=1, max_length=64)
    auth_mode: SshAuthMode

    # password_bootstrap
    password: str | None = None
    # provided_key
    private_key: str | None = None
    passphrase: str | None = None

    auto_install_docker: bool = True

    @field_validator("host")
    @classmethod
    def _clean_host(cls, v: str) -> str:
        v = v.strip()
        if not v or " " in v:
            raise ValueError("Host must be a hostname or IP address with no spaces.")
        return v

    def validate_credentials(self) -> None:
        """Cross-field checks the auth mode implies."""
        if self.auth_mode == "password_bootstrap" and not self.password:
            raise ValueError("A password is required for password bootstrap.")
        if self.auth_mode == "provided_key" and not self.private_key:
            raise ValueError("A private key is required for this auth mode.")


class ServerOut(ORMModel):
    id: UUID
    org_id: UUID
    name: str
    host: str
    port: int
    ssh_user: str
    ssh_auth_mode: SshAuthMode
    status: str
    status_detail: str | None
    host_key_fingerprint: str | None
    docker_version: str | None
    managed_public_key: str | None
    last_seen_at: datetime | None
    created_at: datetime
    project_count: int = 0


class ServerBootstrapOut(BaseModel):
    server: ServerOut
    status: str
    detail: str | None = None
    # Present when the user must install a key manually.
    public_key_to_install: str | None = None


# --- projects ---------------------------------------------------------------


class ProjectCreate(BaseModel):
    server_id: UUID
    name: str = Field(min_length=1, max_length=64)
    harness: HarnessKindStr = "claude_code"
    # Base distribution for the container. Validated against the catalogue.
    environment: str = "debian"
    repo_url: str | None = None
    cpus: str | None = None
    memory: str | None = None

    @field_validator("repo_url")
    @classmethod
    def _clean_repo(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not v:
            return None
        if not (v.startswith(("https://", "http://", "git@", "ssh://"))):
            raise ValueError("Repository URL must be an http(s), ssh:// or git@ URL.")
        return v


class ProjectOut(ORMModel):
    id: UUID
    org_id: UUID
    server_id: UUID
    server_name: str | None = None
    name: str
    slug: str
    harness: HarnessKindStr
    environment: str = "debian"
    repo_url: str | None
    container_name: str | None
    status: str
    status_detail: str | None
    preview_port: int | None
    preview_url: str | None
    created_at: datetime
    # What the agent is doing right now, so the sidebar means something.
    activity: str = "unknown"
    activity_detail: str | None = None
    activity_at: datetime | None = None


class PushSubscriptionIn(BaseModel):
    endpoint: str = Field(min_length=8, max_length=2048)
    p256dh: str = Field(min_length=8, max_length=512)
    auth: str = Field(min_length=8, max_length=512)
    user_agent: str | None = None


class PushStatusOut(BaseModel):
    configured: bool
    public_key: str | None = None
    subscribed: bool = False


class ActivityOut(BaseModel):
    state: str = "unknown"
    detail: str | None = None
    changed_at: datetime | None = None


class SessionOut(ORMModel):
    id: UUID
    project_id: UUID
    tmux_session: str
    harness: HarnessKindStr
    state: str
    started_at: datetime | None
    last_attached_at: datetime | None
    transcript_path: str | None
    activity: str = "unknown"
    activity_detail: str | None = None
    # Devices currently viewing this session. Live from tmux, not stored:
    # a stale count would be worse than none.
    attached_clients: int = 0
    # True when it exists in tmux right now.
    alive: bool = False


class SessionStartIn(BaseModel):
    restart: bool = False
    # Which tmux session. Defaults to the project's first.
    session: str | None = None


class SessionCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=48)


class TranscriptEventOut(BaseModel):
    id: str
    kind: str
    text: str = ""
    at: datetime | None = None
    tool: str | None = None
    ok: bool | None = None
    sidechain: bool = False


class PromptOptionOut(BaseModel):
    key: str
    label: str


class PromptOut(BaseModel):
    question: str
    options: list[PromptOptionOut] = Field(default_factory=list)


class FeedOut(BaseModel):
    """One poll's worth of everything the phone client needs."""

    events: list[TranscriptEventOut] = Field(default_factory=list)
    cursor: str = ""
    # False until the harness has written a transcript.
    available: bool = True
    activity: str = "unknown"
    # Present only while the agent is blocked on a question.
    prompt: PromptOut | None = None


class AnswerIn(BaseModel):
    """A tapped option, or typed text, for a waiting prompt."""

    key: str = Field(min_length=1, max_length=64)


class SendKeysIn(BaseModel):
    keys: str = Field(min_length=1, max_length=8192)
    enter: bool = True


class HarnessCredentialIn(BaseModel):
    org_id: UUID | None = None
    project_id: UUID | None = None
    harness: HarnessKindStr = "claude_code"
    auth_mode: HarnessAuthModeStr
    label: str | None = None
    api_key: str | None = None
    oauth_blob: str | None = None

    def validate_material(self) -> None:
        if self.auth_mode == "api_key" and not self.api_key:
            raise ValueError("An API key is required for api_key mode.")
        if self.auth_mode == "oauth" and not self.oauth_blob:
            raise ValueError("OAuth credentials are required for oauth mode.")


class HarnessCredentialOut(BaseModel):
    id: UUID
    org_id: UUID
    project_id: UUID | None
    harness: HarnessKindStr
    auth_mode: HarnessAuthModeStr
    label: str | None
    created_at: datetime


class HarnessInfoOut(BaseModel):
    kind: str
    display_name: str
    supported_auth_modes: list[str]
    # Implemented by this build of Moonphase.
    available: bool = True
    # Signed in, so projects using it will actually work.
    configured: bool = False
    login_supported: bool = False


class EnvironmentOut(BaseModel):
    key: str
    display_name: str
    description: str
    base_image: str
    setup_script: str | None = None
    # Ships with Moonphase, so it cannot be deleted (only shadowed).
    builtin: bool = True
    # Projects currently using it, so the UI can warn before deleting.
    project_count: int = 0


class EnvironmentIn(BaseModel):
    org_id: UUID | None = None
    key: str = Field(min_length=2, max_length=40)
    display_name: str = Field(min_length=1, max_length=64)
    description: str | None = None
    base_image: str = Field(min_length=1, max_length=200)
    setup_script: str | None = None

    @field_validator("key")
    @classmethod
    def _slug(cls, v: str) -> str:
        v = v.strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,38}[a-z0-9]", v):
            raise ValueError(
                "Key must be lowercase letters, digits and hyphens, and start "
                "and end with a letter or digit."
            )
        return v

    @field_validator("base_image")
    @classmethod
    def _image(cls, v: str) -> str:
        v = v.strip()
        if " " in v:
            raise ValueError("Base image must not contain spaces.")
        return v


# --- workspace profile ------------------------------------------------------


class WorkspaceProfileIn(BaseModel):
    org_id: UUID | None = None
    claude_settings_json: str | None = None
    claude_md: str | None = None
    mcp_json: str | None = None
    env_vars: dict[str, str] = Field(default_factory=dict)
    git_user_name: str | None = None
    git_user_email: str | None = None

    @field_validator("claude_settings_json", "mcp_json")
    @classmethod
    def _valid_json(cls, v: str | None) -> str | None:
        """Reject malformed JSON here rather than letting the harness choke."""
        if v is None or not v.strip():
            return None
        import json

        try:
            json.loads(v)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Must be valid JSON: {exc}") from exc
        return v


class WorkspaceProfileOut(BaseModel):
    org_id: UUID
    claude_settings_json: str | None
    claude_md: str | None
    mcp_json: str | None
    env_vars: dict[str, str]
    git_user_name: str | None
    git_user_email: str | None
    # Connection state, never the credentials themselves.
    harness_connected: bool = False
    harness_auth_mode: str | None = None
    github_connected: bool = False
    github_account: str | None = None
    github_scopes: str | None = None


# --- harness sign-in --------------------------------------------------------


class HarnessLoginStart(BaseModel):
    org_id: UUID | None = None
    harness: HarnessKindStr = "claude_code"
    # Which server runs the throwaway login container. Defaults to any online one.
    server_id: UUID | None = None


class HarnessLoginOut(BaseModel):
    session_id: str
    state: str
    url: str | None = None
    detail: str | None = None
    # Live terminal contents. Shown while verifying and on failure, so a flow
    # that stalls is diagnosable instead of opaque.
    pane: str | None = None


class HarnessLoginCode(BaseModel):
    session_id: str
    code: str = Field(min_length=1, max_length=4096)


class HarnessApiKeyIn(BaseModel):
    org_id: UUID | None = None
    harness: HarnessKindStr = "claude_code"
    api_key: str = Field(min_length=8, max_length=512)


# --- github -----------------------------------------------------------------


class GitHubDeviceStart(BaseModel):
    org_id: UUID | None = None


class GitHubDeviceOut(BaseModel):
    session_id: str
    state: str
    user_code: str | None = None
    verification_uri: str | None = None
    interval: int = 5
    detail: str | None = None
    account: str | None = None


class GitHubTokenIn(BaseModel):
    org_id: UUID | None = None
    token: str = Field(min_length=8, max_length=512)


# --- previews ---------------------------------------------------------------


class DetectedPortOut(BaseModel):
    port: int
    bind: str
    process: str | None = None
    loopback_only: bool = False
    shared: bool = False
    url: str | None = None


class PortShareIn(BaseModel):
    port: int = Field(ge=1, le=65535)


class HealthOut(BaseModel):
    status: str
    version: str
    database: str
