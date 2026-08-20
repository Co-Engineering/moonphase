"""First run, and the two gates it controls.

The point of this module is that installing does not end with "now edit a file
on the server". What is worth testing is the part that has teeth: which
hostnames the proxy may obtain a certificate for, and whether a stranger may
create an account.
"""

from __future__ import annotations

from moonphase.authconfig import (
    AuthMethods,
    incomplete,
    redirect_uri,
    render,
    usable,
)
from moonphase.routers.setup import host_of


def test_a_url_reduces_to_the_hostname_a_certificate_is_for() -> None:
    assert host_of("https://moonphase.example.com") == "moonphase.example.com"
    assert host_of("http://moonphase.example.com/") == "moonphase.example.com"
    assert host_of("moonphase.example.com") == "moonphase.example.com"


def test_a_port_is_not_part_of_the_hostname() -> None:
    """Caddy asks about the name, never the port it arrived on."""
    assert host_of("https://moonphase.example.com:8471") == "moonphase.example.com"


def test_case_does_not_decide_whether_a_certificate_is_issued() -> None:
    assert host_of("HTTPS://Moonphase.Example.COM") == "moonphase.example.com"


def test_a_path_is_ignored() -> None:
    assert host_of("https://moonphase.example.com/setup?x=1") == "moonphase.example.com"


def test_nothing_reduces_to_nothing() -> None:
    # Which the endpoint answers 400 to, rather than approving.
    assert host_of("") == ""
    assert host_of("   ") == ""


# --- how people sign in --------------------------------------------------------


def test_password_alone_needs_nothing_configured() -> None:
    assert usable(AuthMethods()) == ["password"]
    assert incomplete(AuthMethods()) == []


def test_a_provider_without_credentials_is_not_offered() -> None:
    """The failure it prevents: a button that sends someone to Google's error
    page, with nothing to suggest it was misconfigured here."""
    half = AuthMethods(
        public_url="https://moonphase.example.com",
        google_enabled=True,
        google_client_id="cid",
    )

    assert "google" not in usable(half)
    assert any("client ID and secret" in problem for problem in incomplete(half))


def test_a_provider_with_both_halves_is_offered() -> None:
    whole = AuthMethods(
        public_url="https://moonphase.example.com",
        google_enabled=True,
        google_client_id="cid",
        google_client_secret="s",
    )

    assert usable(whole) == ["password", "google"]
    assert incomplete(whole) == []


def test_magic_links_need_somewhere_to_send_from() -> None:
    assert "magic_link" not in usable(AuthMethods(magic_link_enabled=True))
    assert any("SMTP" in problem for problem in incomplete(AuthMethods(magic_link_enabled=True)))

    working = AuthMethods(
        magic_link_enabled=True, smtp_host="smtp.example.com", smtp_sender="a@example.com"
    )
    assert "magic_link" in usable(working)


def test_turning_everything_off_is_reported_rather_than_allowed() -> None:
    """It would lock everyone out, including whoever is doing it."""
    problems = incomplete(AuthMethods(password_enabled=False))

    assert any("nobody could get in" in problem.lower() for problem in problems)


def test_the_rendered_config_turns_disabled_providers_off_explicitly() -> None:
    """A container keeps whatever it started with, so omitting the line would
    leave a provider on until the next full recreate."""
    rendered = render(AuthMethods())

    assert "GOTRUE_EXTERNAL_GOOGLE_ENABLED='false'" in rendered
    assert "GOTRUE_EXTERNAL_AZURE_ENABLED='false'" in rendered


def test_the_rendered_config_is_shell_safe() -> None:
    """It is sourced by the entrypoint, so a quote in a secret must not end the
    value — or worse, start a command."""
    rendered = render(AuthMethods(google_client_secret="it's; rm -rf /"))

    assert "rm -rf /" in rendered
    assert "\n rm -rf" not in rendered
    assert "'it'\\''s; rm -rf /'" in rendered


def test_autoconfirm_follows_whether_mail_can_be_sent() -> None:
    """Confirmation on an instance with no SMTP locks out password signup,
    because GoTrue insists on confirming an address it cannot email."""
    assert "GOTRUE_MAILER_AUTOCONFIRM='true'" in render(AuthMethods())
    assert "GOTRUE_MAILER_AUTOCONFIRM='false'" in render(
        AuthMethods(magic_link_enabled=True, smtp_host="h", smtp_sender="s@e.com")
    )


def test_the_redirect_uri_is_what_the_provider_must_be_given() -> None:
    assert redirect_uri("https://moonphase.example.com") == (
        "https://moonphase.example.com/auth/v1/callback"
    )
    # A trailing slash would make it not match what was pasted into Google.
    assert redirect_uri("https://moonphase.example.com/") == (
        "https://moonphase.example.com/auth/v1/callback"
    )


def test_the_first_account_is_possible_even_with_signup_closed() -> None:
    """The gate allows signup whenever there are no users, which is what lets
    the default be closed — otherwise nobody could ever make the first one, and
    an abandoned setup would leave the instance open to anyone who found it."""
    import inspect

    from moonphase.routers import setup as setup_router

    source = inspect.getsource(setup_router.signup_allowed)

    assert 'found["users"] == 0' in source
    assert "signup_open" in source


def test_an_ip_address_is_not_a_domain() -> None:
    """Google and Microsoft both refuse to redirect to one, so a provider
    configured against an IP cannot work however complete its credentials."""
    from moonphase.authconfig import has_domain

    assert has_domain("https://moonphase.example.com") is True
    assert has_domain("moonphase.example.com") is True

    assert has_domain("http://203.0.113.10") is False
    assert has_domain("203.0.113.10:8471") is False
    assert has_domain("http://localhost:8471") is False
    assert has_domain("") is False


def test_oauth_is_refused_without_a_domain() -> None:
    on_an_ip = AuthMethods(
        public_url="http://203.0.113.10",
        google_enabled=True,
        google_client_id="cid",
        google_client_secret="secret",
    )

    assert "google" not in usable(on_an_ip)
    assert any("bare IP" in problem for problem in incomplete(on_an_ip))


def test_the_same_credentials_work_once_there_is_a_domain() -> None:
    on_a_domain = AuthMethods(
        public_url="https://moonphase.example.com",
        google_enabled=True,
        google_client_id="cid",
        google_client_secret="secret",
    )

    assert "google" in usable(on_a_domain)
    assert incomplete(on_a_domain) == []


def test_a_refused_signup_says_why_in_a_shape_the_client_can_read() -> None:
    """Caddy copies a non-2xx answer from this gate straight to the browser,
    and the browser is the Supabase library, which calls `.json()` on whatever
    arrives.

    An empty 403 therefore reached the person trying to sign up as "Failed to
    execute 'json' on 'Response': Unexpected end of JSON input" — a parser
    complaining, where a sentence explaining that the instance is closed should
    have been. GoTrue's own error shape is what the library knows how to read.
    """
    import inspect

    from moonphase.routers import setup as setup_router

    source = inspect.getsource(setup_router.signup_allowed)

    assert "JSONResponse" in source
    assert '"msg"' in source, "the Supabase client reads the message from `msg`"
    assert "not accepting new accounts" in source


def test_the_sign_in_page_can_find_out_before_offering_to_sign_you_up() -> None:
    """The link was drawn whatever the setting said, so the only way to
    discover that an instance was closed was to fill the form in and fail.

    That page holds no token — it is the one page nobody has signed in to — so
    the answer has to come from somewhere unauthenticated.
    """
    import inspect

    from moonphase.routers import meta

    source = inspect.getsource(meta.instance_config)

    assert "signup_open" in source
    # Same rule as the proxy's gate: an instance with no accounts is open,
    # whatever the stored setting says, or the first person could never sign up.
    assert "users == 0" in source
