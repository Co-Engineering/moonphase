"""GitHub authentication.

Two ways in, both landing in the same place — one org-wide token that every
project container gets as a git credential helper and as `GH_TOKEN`:

  * **Device flow** (preferred). The user opens a URL, types a short code, and
    approves. No redirect URI, which matters for a self-hosted app that has no
    stable public origin, and the same interaction pattern as the harness
    sign-in. Needs a GitHub OAuth app's client id in the environment.
  * **Personal access token.** Paste and go. No setup, but the token is as
    broad as whatever the user ticked, and it does not refresh.

Device flow needs no client *secret*, which is the reason it suits a
distributed self-hosted product: there is no shared secret to leak.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

import httpx

log = logging.getLogger(__name__)

DEVICE_CODE_URL = "https://github.com/login/device/code"
ACCESS_TOKEN_URL = "https://github.com/login/oauth/access_token"
USER_URL = "https://api.github.com/user"
REPOS_URL = "https://api.github.com/user/repos"

# Enough for almost anyone; a hard stop so a token on an org with thousands of
# repos can't turn one request into an unbounded fetch loop.
_REPOS_MAX_PAGES = 5

# repo: clone and push private repositories. read:org: resolve org-owned repos.
# workflow: let the agent edit .github/workflows, which it otherwise cannot.
DEFAULT_SCOPES = "repo read:org workflow"


class GitHubError(RuntimeError):
    """Any failure talking to GitHub."""


@dataclass
class DeviceFlow:
    device_code: str
    user_code: str
    verification_uri: str
    interval: int
    expires_at: float
    scopes: str


@dataclass
class GitHubIdentity:
    token: str
    account: str | None
    scopes: str


@dataclass
class GitHubRepo:
    full_name: str
    clone_url: str
    private: bool
    description: str | None
    pushed_at: str | None


@dataclass
class DeviceSession:
    id: str
    org_id: str
    flow: DeviceFlow
    state: str = "awaiting_authorization"  # awaiting_authorization | complete | error
    detail: str | None = None
    identity: GitHubIdentity | None = None
    created_at: float = field(default_factory=time.monotonic)

    @property
    def expired(self) -> bool:
        return time.monotonic() > self.flow.expires_at


_sessions: dict[str, DeviceSession] = {}


def get_session(session_id: str) -> DeviceSession | None:
    return _sessions.get(session_id)


def put_session(session: DeviceSession) -> None:
    _sessions[session.id] = session


def drop_session(session_id: str) -> None:
    _sessions.pop(session_id, None)


async def start_device_flow(client_id: str, scopes: str = DEFAULT_SCOPES) -> DeviceFlow:
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            DEVICE_CODE_URL,
            headers={"Accept": "application/json"},
            data={"client_id": client_id, "scope": scopes},
        )
    if response.status_code >= 300:
        raise GitHubError(f"GitHub refused the device request: {response.text[:200]}")

    payload = response.json()
    if "device_code" not in payload:
        raise GitHubError(
            payload.get("error_description") or f"Unexpected response: {payload}"
        )

    return DeviceFlow(
        device_code=payload["device_code"],
        user_code=payload["user_code"],
        verification_uri=payload.get("verification_uri", "https://github.com/login/device"),
        interval=int(payload.get("interval", 5)),
        expires_at=time.monotonic() + int(payload.get("expires_in", 900)),
        scopes=scopes,
    )


async def poll_device_flow(client_id: str, flow: DeviceFlow) -> GitHubIdentity | None:
    """Check once whether the user has approved.

    Returns None while still pending. Polling is driven by the client asking,
    rather than a background loop, so an abandoned flow costs nothing.
    """
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            ACCESS_TOKEN_URL,
            headers={"Accept": "application/json"},
            data={
                "client_id": client_id,
                "device_code": flow.device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            },
        )
    payload = response.json()

    if "access_token" in payload:
        token = payload["access_token"]
        return GitHubIdentity(
            token=token,
            account=await account_for(token),
            scopes=payload.get("scope") or flow.scopes,
        )

    error = payload.get("error")
    if error in {"authorization_pending", "slow_down"}:
        return None
    if error == "expired_token":
        raise GitHubError("The device code expired. Start again.")
    if error == "access_denied":
        raise GitHubError("Authorization was declined on GitHub.")
    raise GitHubError(payload.get("error_description") or f"GitHub error: {error}")


async def account_for(token: str) -> str | None:
    """The login name behind a token, so the UI can say who is connected."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(
                USER_URL,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                },
            )
        if response.status_code >= 300:
            return None
        return response.json().get("login")
    except httpx.HTTPError:
        return None


def _next_page_url(response: httpx.Response) -> str | None:
    """The `rel="next"` target from a paginated response's `Link` header."""
    for part in response.headers.get("link", "").split(","):
        segment = part.strip()
        if segment.endswith('rel="next"'):
            return segment.split(";", 1)[0].strip("<> ")
    return None


async def list_repos(token: str) -> list[GitHubRepo]:
    """Every repo the token can see, most-recently-pushed first.

    Includes the account's own repos plus anything it collaborates on or has
    through an organization — the same reach the account already has when a
    URL is pasted by hand, just enumerated instead of typed.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }
    url: str | None = (
        f"{REPOS_URL}?sort=pushed&direction=desc&per_page=100"
        "&affiliation=owner,collaborator,organization_member"
    )
    repos: list[GitHubRepo] = []
    async with httpx.AsyncClient(timeout=20) as client:
        for _ in range(_REPOS_MAX_PAGES):
            if url is None:
                break
            response = await client.get(url, headers=headers)
            if response.status_code >= 300:
                raise GitHubError(f"Could not list repositories: {response.text[:200]}")
            for repo in response.json():
                repos.append(
                    GitHubRepo(
                        full_name=repo["full_name"],
                        clone_url=repo["clone_url"],
                        private=repo["private"],
                        description=repo.get("description"),
                        pushed_at=repo.get("pushed_at"),
                    )
                )
            url = _next_page_url(response)
        if url is not None:
            log.warning("GitHub repo list truncated at %d pages", _REPOS_MAX_PAGES)
    return repos


async def verify_token(token: str) -> GitHubIdentity:
    """Validate a pasted token and read back who it belongs to."""
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(
            USER_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
            },
        )
    if response.status_code == 401:
        raise GitHubError("GitHub rejected that token.")
    if response.status_code >= 300:
        raise GitHubError(f"Could not verify the token: {response.text[:200]}")

    # GitHub reports a classic token's grants in this header; fine-grained
    # tokens omit it, which is not an error.
    scopes = response.headers.get("x-oauth-scopes", "")
    return GitHubIdentity(
        token=token, account=response.json().get("login"), scopes=scopes
    )
