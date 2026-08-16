"""Request and response models for the HTTP API."""

from __future__ import annotations

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
    repo_url: str | None = None
    # Optional per-project harness credential. Omitted means "inherit the
    # organization default".
    harness_auth_mode: HarnessAuthModeStr | None = None
    api_key: str | None = None
    preview_port: int | None = Field(default=None, ge=1, le=65535)
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
    repo_url: str | None
    container_name: str | None
    status: str
    status_detail: str | None
    preview_port: int | None
    preview_url: str | None
    created_at: datetime


class SessionOut(ORMModel):
    id: UUID
    project_id: UUID
    tmux_session: str
    harness: HarnessKindStr
    state: str
    started_at: datetime | None
    last_attached_at: datetime | None
    transcript_path: str | None


class SessionStartIn(BaseModel):
    restart: bool = False


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
    available: bool = True


class HealthOut(BaseModel):
    status: str
    version: str
    database: str
