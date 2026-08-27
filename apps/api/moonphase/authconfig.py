"""Turning "how people sign in" into something GoTrue will read.

GoTrue takes its configuration from the environment at startup and has no
reload, so a checkbox in a setup screen has to become an environment file and a
restart. The API renders the file; the auth container watches it and restarts
itself. Neither step involves anyone opening a shell, which is the whole point.

Secrets are decrypted here and nowhere else on the way out — they go from the
`private` schema into a file that only these two containers can read, and are
never returned to a client.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# Where the API writes and the auth container reads. A volume shared by exactly
# those two.
CONFIG_PATH = "/config/auth.env"


@dataclass
class AuthMethods:
    """What is switched on, and what each needs to work."""

    password_enabled: bool = True
    magic_link_enabled: bool = False
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_sender: str = ""
    smtp_password: str = ""

    google_enabled: bool = False
    google_client_id: str = ""
    google_client_secret: str = ""

    microsoft_enabled: bool = False
    microsoft_client_id: str = ""
    microsoft_client_secret: str = ""
    microsoft_tenant: str = "common"

    # Whether anyone besides the first account may sign up. The setup screen's
    # own switch — the thing GoTrue's own signup-blocking endpoints (`/otp`,
    # and any implicit-signup path, not just `/signup`) need to actually agree
    # with, rather than just the one route Caddy happens to gate per request.
    signup_open: bool = True

    # Where GoTrue thinks it lives. Redirects after an OAuth round trip are
    # built from this, so it has to be the address the browser used — which is
    # the one the setup screen collected.
    public_url: str = ""

    problems: list[str] = field(default_factory=list)


def normalise_public_url(value: str | None) -> str | None:
    """Turn what somebody typed into an address the rest of this can use.

    People type `moonphase.example.com`, because that is what the address *is*.
    Stored as typed it is not a URL, and everything downstream needs one: it
    becomes `GOTRUE_SITE_URL`, the OAuth redirect a provider is handed, and the
    origin the client fetches from. A bare name silently produced
    `moonphase.example.com/auth/v1/callback`, which Google refuses, and a client
    that treated it as a relative path.

    So a missing scheme is filled in — with `https` for a name, because that is
    what happens the moment a certificate is issued, and `http` for an address
    that can never have one. An IP address or `localhost` cannot be issued a
    certificate, so guessing `https` there would hand somebody an address that
    is guaranteed not to answer.

    Whatever scheme was actually typed is kept. Someone writing `http://` in
    front of a name has said something deliberate.
    """
    if value is None:
        return None
    raw = value.strip()
    if not raw:
        return None

    scheme = ""
    rest = raw
    lowered = raw.lower()
    for candidate in ("https://", "http://"):
        if lowered.startswith(candidate):
            scheme = candidate
            rest = raw[len(candidate) :]
            break

    # The host, and a port if one was given. A path is dropped: this is an
    # origin, and `https://example.com/moonphase` as an origin is not a thing.
    host = rest.split("/")[0].strip()
    if not host:
        return None

    if not scheme:
        # Bracketed IPv6, an IPv4 literal, or localhost — none of which can hold
        # a certificate, so https would be a promise nothing can keep.
        bare = host.split(":")[0] if not host.startswith("[") else host
        parts = bare.split(".")
        numeric = len(parts) == 4 and all(part.isdigit() for part in parts)
        scheme = (
            "http://"
            if bare.lower() == "localhost" or numeric or host.startswith("[")
            else "https://"
        )

    return scheme + host.lower()


def has_domain(public_url: str) -> bool:
    """Whether the address is a name rather than a bare IP.

    Google and Microsoft both refuse a redirect URI pointing at an IP address,
    so an OAuth provider configured on one cannot work however complete its
    credentials are. Enforced here as well as in the interface, because the
    interface is a convenience and this is the actual rule.
    """
    host = public_url.strip().lower()
    for prefix in ("https://", "http://"):
        if host.startswith(prefix):
            host = host[len(prefix) :]
            break
    host = host.split("/")[0].split(":")[0].strip()
    if not host or host == "localhost":
        return False
    # An IPv4 literal, or anything bracketed, which is IPv6.
    if host.startswith("["):
        return False
    parts = host.split(".")
    if len(parts) == 4 and all(part.isdigit() for part in parts):
        return False
    return "." in host


def usable(methods: AuthMethods) -> list[str]:
    """Which methods are switched on *and* have what they need.

    A provider enabled without its credentials would render a button that
    cannot work, which is worse than not offering it: the person clicks, gets
    an error from somewhere they have never heard of, and has no idea it was
    misconfigured here.
    """
    out: list[str] = []
    if methods.password_enabled:
        out.append("password")
    if methods.magic_link_enabled and methods.smtp_host and methods.smtp_sender:
        out.append("magic_link")
    domain = has_domain(methods.public_url)
    if (
        methods.google_enabled
        and domain
        and methods.google_client_id
        and methods.google_client_secret
    ):
        out.append("google")
    if (
        methods.microsoft_enabled
        and domain
        and methods.microsoft_client_id
        and methods.microsoft_client_secret
    ):
        out.append("microsoft")
    return out


def incomplete(methods: AuthMethods) -> list[str]:
    """Methods that are on but cannot work, and why."""
    problems: list[str] = []
    domain = has_domain(methods.public_url)
    if methods.magic_link_enabled and not (methods.smtp_host and methods.smtp_sender):
        problems.append("Magic links need an SMTP server and a sender address.")
    if (methods.google_enabled or methods.microsoft_enabled) and not domain:
        problems.append(
            "Google and Microsoft need a custom domain — neither will redirect "
            "to a bare IP address."
        )
    if methods.google_enabled and not (
        methods.google_client_id and methods.google_client_secret
    ):
        problems.append("Google needs a client ID and secret.")
    if methods.microsoft_enabled and not (
        methods.microsoft_client_id and methods.microsoft_client_secret
    ):
        problems.append("Microsoft needs a client ID and secret.")
    if not usable(methods):
        problems.append("No sign-in method is usable — nobody could get in.")
    return problems


def _quote(value: str) -> str:
    """Shell-safe, because this file is sourced by the entrypoint."""
    return "'" + str(value).replace("'", "'\\''") + "'"


def render(methods: AuthMethods) -> str:
    """The environment file the auth container sources.

    Every line is emitted, including the disabled ones, because the container
    keeps whatever it was started with otherwise — a provider turned off would
    stay on until the next full recreate.
    """
    site = methods.public_url or "http://localhost:8471"
    email_on = str(methods.password_enabled or methods.magic_link_enabled).lower()
    tenant = methods.microsoft_tenant or "common"
    azure_url = f"https://login.microsoftonline.com/{tenant}/v2.0"
    lines = [
        "# Generated by Moonphase from the sign-in settings. Do not edit:",
        "# it is rewritten whenever those change, and the auth service",
        "# restarts when it is.",
        "",
        f"API_EXTERNAL_URL={_quote(site)}",
        f"GOTRUE_SITE_URL={_quote(site)}",
        f"GOTRUE_URI_ALLOW_LIST={_quote(site + '/**')}",
        # Caddy's forward_auth in front of /auth/v1/signup is per-request and
        # covers that one route; this is GoTrue's own switch, and the only
        # thing that also closes /otp and any other implicit-signup path.
        f"GOTRUE_DISABLE_SIGNUP={_quote(str(not methods.signup_open).lower())}",
        "",
        f"GOTRUE_EXTERNAL_EMAIL_ENABLED={_quote(email_on)}",
        # Off means GoTrue insists on confirming an address it cannot email
        # from, which locks out password signup on an instance with no SMTP.
        f"GOTRUE_MAILER_AUTOCONFIRM={_quote(str(not methods.magic_link_enabled).lower())}",
        "",
        f"GOTRUE_SMTP_HOST={_quote(methods.smtp_host)}",
        f"GOTRUE_SMTP_PORT={_quote(str(methods.smtp_port or 587))}",
        f"GOTRUE_SMTP_USER={_quote(methods.smtp_user)}",
        f"GOTRUE_SMTP_PASS={_quote(methods.smtp_password)}",
        f"GOTRUE_SMTP_ADMIN_EMAIL={_quote(methods.smtp_sender)}",
        f"GOTRUE_SMTP_SENDER_NAME={_quote('Moonphase')}",
        "",
        f"GOTRUE_EXTERNAL_GOOGLE_ENABLED={_quote(str(methods.google_enabled).lower())}",
        f"GOTRUE_EXTERNAL_GOOGLE_CLIENT_ID={_quote(methods.google_client_id)}",
        f"GOTRUE_EXTERNAL_GOOGLE_SECRET={_quote(methods.google_client_secret)}",
        f"GOTRUE_EXTERNAL_GOOGLE_REDIRECT_URI={_quote(site + '/auth/v1/callback')}",
        "",
        f"GOTRUE_EXTERNAL_AZURE_ENABLED={_quote(str(methods.microsoft_enabled).lower())}",
        f"GOTRUE_EXTERNAL_AZURE_CLIENT_ID={_quote(methods.microsoft_client_id)}",
        f"GOTRUE_EXTERNAL_AZURE_SECRET={_quote(methods.microsoft_client_secret)}",
        f"GOTRUE_EXTERNAL_AZURE_REDIRECT_URI={_quote(site + '/auth/v1/callback')}",
        # Which Microsoft accounts are allowed: one tenant, or `common` for any.
        f"GOTRUE_EXTERNAL_AZURE_URL={_quote(azure_url)}",
        "",
    ]
    return "\n".join(lines)


def redirect_uri(public_url: str) -> str:
    """What to paste into Google's and Microsoft's consoles."""
    return f"{(public_url or '').rstrip('/')}/auth/v1/callback"
