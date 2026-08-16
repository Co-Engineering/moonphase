"""Runtime configuration, loaded from the environment / repo-root .env."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# apps/api/moonphase/config.py -> repo root
REPO_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- secrets -----------------------------------------------------------
    moonphase_secret_key: str = Field(
        ...,
        description="Fernet key encrypting SSH and harness credentials at rest.",
    )

    # --- database ----------------------------------------------------------
    database_url: str = "postgresql+asyncpg://postgres:postgres@127.0.0.1:54722/postgres"

    # --- supabase ----------------------------------------------------------
    supabase_url: str = "http://127.0.0.1:54721"
    supabase_anon_key: str = ""
    supabase_jwt_secret: str = ""

    # --- api ---------------------------------------------------------------
    moonphase_api_host: str = "0.0.0.0"
    moonphase_api_port: int = 8471
    moonphase_cors_origins: str = "http://localhost:5173"

    # --- containers --------------------------------------------------------
    moonphase_runtime_image: str = "moonphase/runtime-claude:latest"
    # Resolved per project from its chosen environment. The catalogue lives in
    # moonphase/environments.py; this only says where the images are published.
    moonphase_runtime_image_template: str = "moonphase/runtime-claude:{environment}"

    # --- previews ----------------------------------------------------------
    # Interface the per-port preview listeners bind to. Loopback is right when
    # the backend and browser are the same machine; set 0.0.0.0 when the
    # backend is remote and you want previews reachable from your phone.
    moonphase_preview_bind: str = "127.0.0.1"
    # Host clients should dial to reach those listeners. Defaults to the bind
    # address, but must be the externally routable name behind a proxy.
    moonphase_preview_host: str = "127.0.0.1"

    # --- github -------------------------------------------------------------
    # OAuth app client id enabling the device flow. Without it, GitHub can
    # still be connected by pasting a personal access token. Device flow needs
    # no client secret, which is why it suits a self-hosted deployment.
    moonphase_github_client_id: str = ""

    # --- ssh ---------------------------------------------------------------
    moonphase_ssh_connect_timeout: int = 15
    moonphase_ssh_keepalive_interval: int = 30
    moonphase_ssh_trust_on_first_use: bool = True

    @field_validator("moonphase_secret_key")
    @classmethod
    def _non_empty_secret(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError(
                "MOONPHASE_SECRET_KEY is required. Generate one with:\n"
                '  python -c "from cryptography.fernet import Fernet; '
                'print(Fernet.generate_key().decode())"'
            )
        return v.strip()

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.moonphase_cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
