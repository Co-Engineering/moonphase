"""First run, and the two gates it controls.

The point of this module is that installing does not end with "now edit a file
on the server". What is worth testing is the part that has teeth: which
hostnames the proxy may obtain a certificate for, and whether a stranger may
create an account.
"""

from __future__ import annotations

from pathlib import Path

from moonphase.authconfig import (
    AuthMethods,
    incomplete,
    normalise_public_url,
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


# --- who administers the instance -------------------------------------------


def test_administering_the_instance_is_not_the_same_as_owning_an_org() -> None:
    """The check that guarded instance settings passed for everybody.

    It asked whether the caller was 'owner' or 'admin' of any organization —
    and a trigger makes every account the owner of a personal one the moment it
    signs up. So any signed-in user could change the instance's domain or
    reopen registration, which went unnoticed only because nothing worse hung
    off it. Managing other people's accounts would have.
    """
    migration = (
        Path(__file__).resolve().parents[3]
        / "supabase/migrations/20260820120000_instance_admins.sql"
    ).read_text()

    assert "create table if not exists public.instance_admins" in migration
    # The old policy is replaced, not left beside the new one.
    assert "drop policy if exists instance_settings_write" in migration
    assert "from public.instance_admins a where a.user_id = auth.uid()" in migration
    # Nothing an ordinary account can write: granting goes through the API,
    # which is where the "not the last one" rule lives.
    assert "grant select on public.instance_admins to authenticated" in migration
    assert "grant insert" not in migration


def test_an_existing_install_keeps_an_administrator() -> None:
    """Nobody would be able to administer an instance that already exists, and
    the fix would be a database client. The earliest account is the one that
    ran the installer."""
    migration = (
        Path(__file__).resolve().parents[3]
        / "supabase/migrations/20260820120000_instance_admins.sql"
    ).read_text()

    assert "insert into public.instance_admins (user_id)" in migration
    assert "order by created_at asc limit 1" in migration


def test_the_first_account_can_still_finish_setup() -> None:
    """A fresh install has no administrators — the backfill found no users —
    so the person setting it up would be refused by the screen they are setting
    it up with. Completing setup is what claims the instance."""
    import inspect

    from moonphase.routers import setup as setup_router

    source = inspect.getsource(setup_router.complete)

    assert "insert into instance_admins" in source
    # Only ever from empty, or a later signup could claim an instance that
    # already has an owner.
    assert "where not exists (select 1 from instance_admins)" in source


def test_removing_an_account_refuses_to_take_its_work_with_it() -> None:
    """Deleting an account cascades through its personal organization, which
    takes its servers and projects with it — and leaves the containers those
    describe running on the machines with nothing pointing at them."""
    import inspect

    from moonphase.routers import people

    source = inspect.getsource(people.remove_person)

    assert "owned_projects" in source
    assert "409" in source
    # And an instance nobody can administer is only recoverable with psql.
    assert "last administrator" in source


def test_accounts_are_made_through_gotrue_rather_than_by_hand() -> None:
    """The columns of `auth.users` belong to GoTrue and change between
    versions. A row written by hand works until it does not, and the failure
    would be an account that exists and cannot sign in."""
    import inspect

    from moonphase.routers import people

    source = inspect.getsource(people.invite_person)

    assert "_gotrue" in source
    assert "insert into auth.users" not in source


# --- what people actually type ----------------------------------------------


def test_a_bare_domain_becomes_an_https_address() -> None:
    """The common case, and it used to be stored as typed.

    `moonphase.example.com` is what the address *is*, so it is what people
    write. It is not a URL, and everything downstream needs one — it becomes
    GOTRUE_SITE_URL, the redirect URI a provider is handed, and the origin the
    client fetches from. Stored bare it produced
    `moonphase.example.com/auth/v1/callback`, which Google refuses.
    """
    assert normalise_public_url("moonphase.example.com") == "https://moonphase.example.com"
    assert normalise_public_url("  moonphase.example.com/  ") == "https://moonphase.example.com"


def test_a_scheme_that_was_typed_is_kept() -> None:
    """Writing `http://` in front of a name is a deliberate thing to say."""
    assert normalise_public_url("http://moonphase.example.com") == "http://moonphase.example.com"
    assert normalise_public_url("https://moonphase.example.com") == "https://moonphase.example.com"
    # And a scheme in capitals is still a scheme.
    assert normalise_public_url("HTTPS://Example.COM") == "https://example.com"


def test_an_address_that_cannot_hold_a_certificate_gets_http() -> None:
    """Guessing https for an IP would hand somebody an address guaranteed not to
    answer: no certificate authority will issue for one, so the name would
    resolve and the handshake would fail."""
    assert normalise_public_url("203.0.113.10") == "http://203.0.113.10"
    assert normalise_public_url("203.0.113.10:8471") == "http://203.0.113.10:8471"
    assert normalise_public_url("localhost:8471") == "http://localhost:8471"


def test_a_path_is_not_part_of_an_origin() -> None:
    """`https://example.com/moonphase` as an origin is not a thing, and would
    produce a redirect URI with the path in the middle of it."""
    assert normalise_public_url("example.com/setup") == "https://example.com"


def test_nothing_typed_stays_nothing() -> None:
    """Blank means "use whatever address this machine answers on", which is a
    different thing from an empty string stored as the domain."""
    assert normalise_public_url("") is None
    assert normalise_public_url("   ") is None
    assert normalise_public_url(None) is None


def test_both_ways_of_setting_the_address_normalise_it() -> None:
    """Setup and the settings screen are separate endpoints, and an address
    corrected later must be treated exactly like one entered at the start."""
    import inspect

    from moonphase.routers import people
    from moonphase.routers import setup as setup_router

    assert "normalise_public_url" in inspect.getsource(setup_router.complete)
    assert "normalise_public_url" in inspect.getsource(people.write_settings)
