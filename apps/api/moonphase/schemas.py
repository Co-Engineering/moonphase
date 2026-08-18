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

# What you grant someone.
ShareRoleStr = Literal["viewer", "collaborator"]
# What they end up with. Computed by the database; see the sharing migration.
#   admin  everything, including deleting it and managing its shares
#   write  use it: start, stop, type into it, create projects on it
#   read   watch it
#   host   you own the machine a project runs on, but not the project
AccessStr = Literal["admin", "write", "read", "host"]


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
    # What this caller may do with it, and whether it reached them through a
    # share rather than their own organization.
    access: AccessStr = "admin"
    shared: bool = False
    share_count: int = 0


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
    # When that was last confirmed; see SessionOut for why it matters.
    checked_at: datetime | None = None
    access: AccessStr = "admin"
    shared: bool = False
    share_count: int = 0


# --- sharing ------------------------------------------------------------------


class ShareIn(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    role: ShareRoleStr = "collaborator"

    @field_validator("email")
    @classmethod
    def _clean_email(cls, v: str) -> str:
        v = v.strip().lower()
        # Deliberately not RFC-complete: the address either matches an account
        # here or it does not, and over-strict validation rejects real ones.
        if v.count("@") != 1 or v.startswith("@") or v.endswith("@") or " " in v:
            raise ValueError("That does not look like an email address.")
        return v


class ShareRoleIn(BaseModel):
    role: ShareRoleStr


class ShareOut(BaseModel):
    id: UUID
    email: str
    role: ShareRoleStr
    # False until the invitee has signed up and the grant has been claimed.
    accepted: bool = False
    created_at: datetime
    # True for the row describing the caller, so the UI can offer "leave"
    # instead of "revoke".
    is_you: bool = False


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
    # When the state last changed, and when it was last confirmed. The second
    # is what says whether the first is news or a guess: a session the monitor
    # cannot reach keeps its old state, and presenting that as current is how
    # an agent that stopped hours ago goes on looking busy.
    activity_at: datetime | None = None
    checked_at: datetime | None = None
    # Who it runs as. A session is one person's: their credentials, their
    # branch, their commits.
    user_id: UUID | None = None
    owner: str | None = None
    is_mine: bool = False
    # Present when listed across projects, so a session can be named somewhere
    # its project is not already on screen.
    project_name: str | None = None
    # The git worktree this session works in, and the branch it is on.
    workdir: str = "/workspace"
    branch: str | None = None
    # Devices currently viewing this session. Live from tmux, not stored:
    # a stale count would be worse than none.
    attached_clients: int = 0
    # True when it exists in tmux right now.
    alive: bool = False


class SessionStartIn(BaseModel):
    restart: bool = False
    # Ask the harness to reopen its previous conversation rather than start a
    # new one. What makes a session survive its container being restarted.
    resume: bool = False
    # Which tmux session. Left out, it resolves to the caller's own — never
    # somebody else's, which would run their subscription on your keystrokes.
    session: str | None = None


class SessionCreateIn(BaseModel):
    # Optional: left out, the name is derived from who is asking, which is now
    # the useful default because names identify people in a shared project.
    name: str | None = Field(default=None, max_length=48)


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
        """Reject malformed JSON here rather than letting the harness choke.

        An object specifically, not merely valid JSON: `settings.json` and
        `.mcp.json` are both objects, and a bare string or list parses fine
        while producing a container where the harness silently ignores the
        file. Failing at the point someone can still fix it is the whole value
        of checking at all.
        """
        if v is None or not v.strip():
            return None
        import json

        try:
            parsed = json.loads(v)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Must be valid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ValueError("Must be a JSON object.")
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


class PreviewServiceOut(BaseModel):
    """One thing listening in the container, and what it appears to be."""

    port: int
    # 'page' serves HTML and is what someone means by "open the app"; 'api'
    # answers JSON; 'unknown' did not respond to an HTTP request at all.
    kind: Literal["page", "api", "unknown"] = "unknown"
    # The page's <title>, which names it far better than a port number does.
    title: str | None = None
    process: str | None = None


class PreviewOut(BaseModel):
    """Where to point a browser so the container's own addresses resolve."""

    proxy_host: str
    proxy_port: int
    # Ordered by what a person most likely meant to open. Not a declaration and
    # not exhaustive: the proxy carries whatever is asked for, including ports
    # that appear after this was built.
    services: list[PreviewServiceOut] = Field(default_factory=list)
    container: str


class PortShareIn(BaseModel):
    port: int = Field(ge=1, le=65535)


class InstanceConfigOut(BaseModel):
    """What a client needs to reach this instance, discovered from its URL."""

    supabase_url: str
    # Public by design: it is shipped to every browser and grants nothing on
    # its own, because every table behind it has row level security.
    supabase_anon_key: str
    # Public half of the push signing pair. Null when push is not configured,
    # which the client should say rather than silently offering notifications.
    vapid_public_key: str | None = None
    version: str


# --- usage --------------------------------------------------------------------


class UsageSliceOut(BaseModel):
    """One model's share of a period."""

    model: str
    tokens: int
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    thinking_tokens: int = 0
    # Null when no rate is known for this model. Distinct from zero: showing
    # $0.00 for an unpriced model is a confident lie about someone's bill.
    cost: float | None = None
    priced: bool = False


class UsageProjectOut(BaseModel):
    project_id: UUID | None = None
    project_name: str
    tokens: int = 0
    cost: float | None = None


class AttentionOut(BaseModel):
    """A question waiting on you, ready to answer without opening anything."""

    project_id: UUID
    project_name: str
    session: str
    activity_at: datetime | None = None
    question: str = ""
    # Null when the pane could not be parsed into buttons; the tail is still
    # there, so the answer is typed rather than tapped.
    prompt: PromptOut | None = None
    tail: str = ""


class ChangedFileOut(BaseModel):
    path: str
    added: int = 0
    removed: int = 0
    # 'untracked' has no diff to show but is still part of what changed.
    status: str = "modified"


class ChangesOut(BaseModel):
    """What a session has done to the code, committed or not."""

    branch: str = ""
    base: str = ""
    added: int = 0
    removed: int = 0
    files: list[ChangedFileOut] = Field(default_factory=list)
    patch: str = ""
    truncated: bool = False
    # Set when the worktree is not a repository, which is a state to render
    # rather than an error to raise.
    detail: str | None = None


class CheckpointOut(BaseModel):
    """One save point, named by whoever made it."""

    id: str
    at: str
    label: str
    # True when the files on disk still match this point.
    current: bool = False
    # Made by Moonphase on the way to somewhere else, not chosen by a person.
    automatic: bool = False


class CheckpointsOut(BaseModel):
    points: list[CheckpointOut] = Field(default_factory=list)
    # Files changed since the newest point, so "you have unsaved work" is a
    # fact rather than a guess.
    unsaved: int = 0
    detail: str | None = None


class SaveCheckpointIn(BaseModel):
    label: str | None = Field(default=None, max_length=120)


class DigestOut(BaseModel):
    """What the agent did, counted."""

    created: list[str] = Field(default_factory=list)
    edited: list[str] = Field(default_factory=list)
    commands: int = 0
    installs: int = 0
    tests: int = 0
    searches: int = 0
    last_said: str = ""
    detail: str | None = None


class SearchHitOut(BaseModel):
    project_id: UUID
    project_name: str
    session: str
    at: str = ""
    role: str = ""
    text: str = ""


class SearchOut(BaseModel):
    query: str
    hits: list[SearchHitOut] = Field(default_factory=list)
    # True when a machine did not answer in time, so the list is incomplete
    # rather than empty.
    partial: bool = False


class UsageWindowOut(BaseModel):
    """One limit period, anchored to when it actually opened."""

    label: str
    hours: int
    # Null when nothing has opened a window: no work, no clock running.
    started_at: datetime | None = None
    resets_at: datetime | None = None
    tokens: int = 0
    cost: float | None = None
    # What the plan allows, if the person has said. Null renders as no bar
    # rather than as a full one or an empty one, both of which would be claims.
    limit_tokens: int | None = None
    percent: float | None = None


class UsageOut(BaseModel):
    """Consumption, framed by how the caller pays for it."""

    # 'oauth' for a subscription, 'api_key' for metered billing. Decides which
    # number leads: how much of the window has gone, or how much it cost.
    billing: str
    hours: int
    tokens: int
    cost: float | None = None
    session_window: UsageWindowOut
    week_window: UsageWindowOut
    models: list[UsageSliceOut] = Field(default_factory=list)
    projects: list[UsageProjectOut] = Field(default_factory=list)
    series: list[dict] = Field(default_factory=list)


class UsageLimitsIn(BaseModel):
    session_tokens: int | None = Field(default=None, gt=0, le=10_000_000_000)
    weekly_tokens: int | None = Field(default=None, gt=0, le=100_000_000_000)
    # Push once per window when usage crosses this share of the allowance.
    alert_percent: int | None = Field(default=None, ge=1, le=100)


class UsageLimitsOut(BaseModel):
    session_tokens: int | None = None
    weekly_tokens: int | None = None
    alert_percent: int | None = None


class ModelPriceIn(BaseModel):
    org_id: UUID | None = None
    model: str = Field(min_length=1, max_length=100)
    input_per_m: float = Field(ge=0, le=10_000)
    output_per_m: float = Field(ge=0, le=10_000)


class ModelPriceOut(BaseModel):
    model: str
    input_per_m: float
    output_per_m: float
    # Ships with Moonphase rather than set here, so the UI can say which
    # numbers are assumptions and which someone actually chose.
    builtin: bool = False

    model_config = ConfigDict(protected_namespaces=())


class SetupStateOut(BaseModel):
    """What the client needs to decide between a setup screen and a sign-in."""

    needs_setup: bool
    signup_open: bool = True


class SetupIn(BaseModel):
    # The address people will use. Decides which hostname the proxy may get a
    # certificate for, so it is load-bearing rather than cosmetic.
    public_url: str | None = Field(default=None, max_length=255)
    signup_open: bool = False


class HealthOut(BaseModel):
    status: str
    version: str
    database: str
