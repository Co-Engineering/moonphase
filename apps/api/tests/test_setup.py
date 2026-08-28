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


def test_closed_signup_is_carried_all_the_way_to_gotrue_itself() -> None:
    """Caddy's forward_auth only gates /auth/v1/signup, per request. GoTrue's
    own switch is what also closes /otp and any other implicit-signup path —
    and unlike the per-request check, it needs the file rewritten and the
    container restarted before it takes effect at all."""
    assert "GOTRUE_DISABLE_SIGNUP='false'" in render(AuthMethods(signup_open=True))
    assert "GOTRUE_DISABLE_SIGNUP='true'" in render(AuthMethods(signup_open=False))


def test_signup_open_defaults_to_open_in_code_same_as_the_column() -> None:
    """`instance_settings.signup_open` defaults open too (closed-by-default is
    the setup screen's own choice, made explicit at setup time) — mismatched
    defaults here would make an unconfigured field render as the wrong side."""
    assert AuthMethods().signup_open is True


def test_the_row_that_feeds_render_carries_signup_open() -> None:
    """`_methods_from` is the only place a database row becomes what `render`
    reads, so a column missing from that translation renders as if it were
    never there — this used to be true of signup_open specifically."""
    from moonphase.routers.setup import _methods_from

    assert _methods_from({"signup_open": False}).signup_open is False
    assert _methods_from({"signup_open": True}).signup_open is True
    # Absent, as a row from before this column mattered here would be: open,
    # matching the column's own default rather than silently closing signup.
    assert _methods_from({}).signup_open is True


def test_finishing_setup_publishes_the_gate_it_just_set() -> None:
    """Otherwise an administrator closes signup during setup, the screen says
    saved, and GoTrue answers requests with whatever it booted with until
    someone unrelated later saves the sign-in methods screen."""
    import inspect

    from moonphase.routers import setup as setup_router

    source = inspect.getsource(setup_router.complete)
    assert "publish_auth_config" in source


def test_changing_instance_settings_publishes_the_gate_too() -> None:
    """The settings screen is the only place signup_open changes after first
    run, and is exactly where this was missing."""
    import inspect

    from moonphase.routers import people

    source = inspect.getsource(people.write_settings)
    assert "publish_auth_config" in source


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


async def test_needs_setup_survives_auth_users_emptying_out(monkeypatch) -> None:
    """`auth.users` is not sticky: it can read zero for reasons that have
    nothing to do with whether this instance was ever set up (an account
    later removed, a direct DB change). `complete()` already refuses to
    re-claim an instance that has an administrator, but that guard means
    nothing if this unauthenticated screen reopens itself whenever the count
    happens to be zero. setup_completed_at is set once and never cleared, so
    it has to be what decides — not the count."""
    from moonphase.routers import setup as setup_router

    async def completed_but_userless() -> dict:
        return {
            "users": 0,
            "public_url": "https://moonphase.example.com",
            "signup_open": False,
            "completed_at": "2026-01-01T00:00:00+00:00",
        }

    monkeypatch.setattr(setup_router, "_state", completed_but_userless)

    result = await setup_router.state()

    assert result.needs_setup is False


async def test_needs_setup_is_true_before_setup_has_ever_completed(monkeypatch) -> None:
    from moonphase.routers import setup as setup_router

    async def fresh_install() -> dict:
        return {
            "users": 0,
            "public_url": None,
            "signup_open": True,
            "completed_at": None,
        }

    monkeypatch.setattr(setup_router, "_state", fresh_install)

    result = await setup_router.state()

    assert result.needs_setup is True


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


def test_auth_methods_write_requires_instance_administration_too() -> None:
    """`auth_methods_write` was the sibling of `instance_settings_write` and
    had the exact same bug — 'owner'/'admin' of any organization, which every
    account is of its own personal one — but the earlier fix was never
    ported to it. Any signed-in user could repoint the instance's SMTP relay
    or OAuth client secrets, or turn password auth off instance-wide.
    """
    migration = (
        Path(__file__).resolve().parents[3]
        / "supabase/migrations/20260827190000_fix_auth_methods_write_policy.sql"
    ).read_text()

    assert "drop policy if exists auth_methods_write" in migration
    assert "from public.instance_admins a where a.user_id = auth.uid()" in migration
    # The org-role check this replaces must not still be present anywhere in
    # the new policy body — a stray `or` would silently keep the old hole.
    assert "org_members" not in migration


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


# --- cloning a repository at project creation --------------------------------


def test_the_clone_carries_a_credential() -> None:
    """It had none, and a private repository therefore failed with "could not
    read Username for 'https://github.com': No such device or address" — git
    asking for a password down a pipe with no terminal on the other end.

    Sessions have had a credential helper all along. This runs before any
    session exists, so it needed its own.
    """
    import inspect

    from moonphase.routers import projects

    source = inspect.getsource(projects._clone)

    assert "credential.helper=store" in source
    # Written over stdin, so the token stays out of the host's process list.
    assert "write_file" in source
    # And `-c`, so none of it lands in the clone's own .git/config, which lives
    # in a volume that outlives this and is readable by whatever runs next.
    # Asserted on the argv rather than on the phrase, which the docstring uses
    # to explain the choice and which therefore says nothing about the code.
    assert '"-c"' in source
    assert '"config"' not in source


def test_the_clone_never_waits_for_a_prompt() -> None:
    """Blocking on a password nothing can type is how the original failure
    reported itself as a device error rather than as a missing credential."""
    import inspect

    from moonphase.routers import projects

    source = inspect.getsource(projects._clone)
    assert "GIT_TERMINAL_PROMPT" in source


def test_the_clone_credential_is_removed_afterwards() -> None:
    """Including when the clone failed. A token left behind in the container is
    the thing the arrangement exists to avoid."""
    import inspect

    from moonphase.routers import projects

    source = inspect.getsource(projects._clone)
    assert "finally:" in source
    assert "rm" in source and "_CLONE_CREDENTIALS" in source


def test_a_private_repo_without_github_says_so() -> None:
    """"git clone failed" plus git's own wording explains nothing to somebody
    who has simply not connected GitHub yet."""
    import inspect

    from moonphase.routers import projects

    source = inspect.getsource(projects._clone)
    assert "Settings → Accounts" in source


# --- renaming ------------------------------------------------------------------


def _update_statement(source: str) -> str:
    """The SQL an endpoint runs, pulled out of its source.

    Asserting against a whole function catches its comments, and a comment
    explaining why a column is left alone would fail a check written to catch
    that column being touched — which is the opposite of what it is for.
    """
    import re

    match = re.search(r'"(update [^"]+)"', source)
    assert match, "no update statement found"
    return " ".join(match.group(1).split())

def test_renaming_changes_the_name_and_nothing_else() -> None:
    """A project's slug, container and volumes were derived from its name when
    it was created, and are what the running container is actually called.
    Renaming those would mean recreating it, which is a great deal to do to
    somebody fixing a typo."""
    import inspect

    from moonphase.routers import projects

    # The statement, not the whole function: the docstring names those columns
    # to explain why it leaves them alone, which says nothing about the code.
    statement = _update_statement(inspect.getsource(projects.rename_project))

    assert statement.startswith("update projects set name")
    for derived in ("slug", "container_name", "workspace_volume", "home_volume"):
        assert derived not in statement, f"renaming must not touch {derived}"


def test_a_server_rename_leaves_how_to_reach_it_alone() -> None:
    """The address, the login and the key are what Moonphase authenticated
    against. Changing one without re-bootstrapping would leave a record that no
    longer describes the machine it points at."""
    import inspect

    from moonphase.routers import servers

    statement = _update_statement(inspect.getsource(servers.rename_server))

    assert statement.startswith("update servers set name")
    for derived in ("host", "ssh_user", "port", "host_key_fingerprint"):
        assert derived not in statement


def test_a_name_too_long_is_refused_rather_than_cut_down() -> None:
    """The database caps it at 64. Silently truncating would rename the thing to
    something nobody typed."""
    import inspect

    from moonphase.routers import projects, servers

    for source in (
        inspect.getsource(projects.rename_project),
        inspect.getsource(servers.rename_server),
    ):
        assert "64 characters at most" in source
        assert "[:80]" not in source and "[:120]" not in source


def test_renaming_goes_through_the_caller_own_session() -> None:
    """So the row-level policies decide whether it is theirs to rename, rather
    than the route deciding and hoping it agrees."""
    import inspect

    from moonphase.routers import projects, servers

    assert "user_session" in inspect.getsource(projects.rename_project)
    assert "user_session" in inspect.getsource(servers.rename_server)


def _derive_url(env_text: str, host: str = "203.0.113.10") -> tuple[str, str]:
    """Run the installer's own URL derivation against a given .env.

    The logic lives in `scripts/install-server.sh` between two markers and
    depends on nothing but `$ENV_FILE` and `$HOST`, so it can be run here as
    itself rather than described. Asserting on the text of a shell script only
    proves the words are present, which is not the same as the script working.
    """
    import subprocess

    script = (
        Path(__file__).resolve().parents[3] / "scripts/install-server.sh"
    ).read_text()

    body = script.split("# >>> url-derivation", 1)[1].split("# <<< url-derivation", 1)[0]

    program = f'ENV_FILE="$1"\nHOST="$2"\n{body}\nprintf "%s %s" "$REACHABLE" "$URL"\n'
    out = subprocess.run(
        ["sh", "-c", program, "sh", env_text, host],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split(" ")
    return out[0], out[1]


def test_the_installer_sends_you_to_a_port_that_is_published() -> None:
    """It told people to open `http://<host>:8471` after a first install, and
    8471 is bound to the server's loopback — so the address it printed could
    not answer, on the one run that has to work.

    It asked the Docker daemon which port was published. On a machine that had
    no Docker, the install had just added this user to the `docker` group, and
    a group does not reach a session that already exists — every command shares
    one multiplexed connection opened before that. Docker refused, the error
    went to /dev/null, and an empty answer fell through to the fallback.
    """
    reachable, url = _derive_url(
        "COMPOSE_FILE=docker-compose.yml:docker-compose.public.yml\n"
        "MOONPHASE_BIND=127.0.0.1\n"
        "MOONPHASE_PORT=8471\n"
    )

    # 80 and 443 are published on every interface by the public overlay; the
    # plain port stays on loopback beside them.
    assert (reachable, url) == ("yes", "http://203.0.113.10")


def test_a_loopback_only_install_is_not_advertised_as_a_url() -> None:
    """Without the public overlay the only published port is on the server's
    loopback, so there is no address to hand out at all. Printing one anyway
    was the same bug wearing the other branch: a confident URL that times out.
    """
    reachable, url = _derive_url(
        "COMPOSE_FILE=docker-compose.yml\nMOONPHASE_BIND=127.0.0.1\nMOONPHASE_PORT=8471\n"
    )

    assert reachable == "no"
    # Pointed at the loopback it is actually on — reached through the tunnel
    # the installer prints — rather than at the host, which cannot serve it.
    assert url == "http://127.0.0.1:8471"


def test_a_deliberately_exposed_plain_port_is_advertised() -> None:
    """Someone who bound it to every interface on purpose does have a working
    address, and refusing to print it would be wrong in the other direction."""
    reachable, url = _derive_url(
        "COMPOSE_FILE=docker-compose.yml\nMOONPHASE_BIND=0.0.0.0\nMOONPHASE_PORT=9000\n"
    )

    assert (reachable, url) == ("yes", "http://203.0.113.10:9000")


def test_the_port_is_read_without_needing_docker() -> None:
    """The whole point of reading .env: it answers the same whether or not this
    session can reach the Docker daemon. An unreadable .env must not silently
    become a confident wrong URL."""
    reachable, url = _derive_url("")

    # Nothing known, so nothing claimed: no overlay recorded and no bind means
    # loopback, which is the assumption that does not strand anybody.
    assert reachable == "no"
    assert url == "http://127.0.0.1:8471"


def test_the_auth_volume_is_writable_by_the_user_that_writes_it() -> None:
    """Google and Microsoft sign-in could not work on any install.

    The API writes the generated GoTrue configuration to /config, a named
    volume. Docker creates a missing mount point as root, the API runs as uid
    10001, and the write is wrapped in `except OSError` — so it failed with a
    permission error, was logged as a warning, and the settings screen reported
    success. GoTrue never received the file and answered "provider is not
    enabled" however correctly the credentials had been entered.

    An empty named volume is initialised from the image, ownership included, so
    the image has to carry the directories.
    """
    dockerfile = (
        Path(__file__).resolve().parents[3] / "docker/Dockerfile"
    ).read_text()

    # Created and owned before the build drops to that user, so the ownership
    # is the one a fresh volume inherits.
    assert "mkdir -p /config /home/moonphase/.moonphase" in dockerfile
    assert "chown moonphase:moonphase /config /home/moonphase/.moonphase" in dockerfile
    assert dockerfile.index("chown moonphase:moonphase") < dockerfile.index(
        "USER moonphase"
    )


def test_gotrue_uri_allow_list_does_not_default_to_everywhere() -> None:
    """Until the "ways to sign in" screen is saved once, the API's own
    dynamic rewrite of this value (authconfig.render) has never run, and
    GoTrue boots with whatever docker-compose.yml gave it directly — `*`
    there means every redirect_to on a magic-link or OAuth callback is
    accepted, on a fresh install, before anyone has configured anything.
    """
    import yaml

    compose = yaml.safe_load(
        (Path(__file__).resolve().parents[3] / "docker-compose.yml").read_text()
    )

    allow_list = compose["services"]["auth"]["environment"]["GOTRUE_URI_ALLOW_LIST"]
    assert allow_list.strip() != "*"
    assert not allow_list.strip().endswith(":-*}")
    # Scoped to the same fallback address the rest of the stack already uses
    # when MOONPHASE_PUBLIC_URL is unset, not to everywhere.
    assert "MOONPHASE_PUBLIC_URL" in allow_list


def test_an_existing_install_has_its_volume_repaired() -> None:
    """The image change only reaches a volume that does not exist yet: a volume
    keeps the ownership it was created with. Neither the API (uid 10001) nor
    the auth container (uid 1000) runs as root, so neither can correct it, and
    the repair has to be something that does."""
    import yaml

    compose = yaml.safe_load(
        (Path(__file__).resolve().parents[3] / "docker-compose.yml").read_text()
    )

    # Parsed rather than matched as text, so reformatting the file cannot make
    # this pass or fail for the wrong reason.
    repair = compose["services"]["prepare-volumes"]
    assert repair["user"] == "root", "nothing else in the stack can chown it"

    command = " ".join(repair["entrypoint"])
    # -R, because a root-owned auth.env inside a chowned directory is still
    # root-owned, and opening it for writing still fails.
    assert "chown -R moonphase:moonphase" in command
    assert "/config" in command and "/home/moonphase/.moonphase" in command

    # It must hold both volumes, or it repairs nothing.
    mounted = {entry.split(":")[0] for entry in repair["volumes"]}
    assert {"auth-config", "api-state"} <= mounted

    # And finish before the API starts, or the first write races the repair.
    assert (
        compose["services"]["api"]["depends_on"]["prepare-volumes"]["condition"]
        == "service_completed_successfully"
    )


def test_a_failed_handoff_is_reported_rather_than_swallowed() -> None:
    """What made this invisible for so long.

    The settings are written to the database before they are handed to the auth
    container, so a failed handoff still looked like a successful save. The
    screen has to say that what it is showing is not what is in force.
    """
    from moonphase.routers import setup as setup_router

    original = setup_router._handoff_error
    try:
        setup_router._handoff_error = None
        # A path that cannot be created, standing in for a volume this user
        # cannot write to.
        import moonphase.authconfig as authconfig

        was = authconfig.CONFIG_PATH
        try:
            authconfig.CONFIG_PATH = "/proc/moonphase-cannot-exist/auth.env"
            setup_router._write_config("GOTRUE_EXTERNAL_AZURE_ENABLED='true'")
        finally:
            authconfig.CONFIG_PATH = was

        assert setup_router._handoff_error is not None
        # Says it is not in force, rather than only that something failed.
        assert "not in force" in setup_router._handoff_error
    finally:
        setup_router._handoff_error = original


def test_a_successful_handoff_clears_an_earlier_failure() -> None:
    """Otherwise the screen keeps warning about a problem that has been fixed,
    which is how people learn to ignore the warnings that matter."""
    import tempfile

    import moonphase.authconfig as authconfig
    from moonphase.routers import setup as setup_router

    original = setup_router._handoff_error
    was = authconfig.CONFIG_PATH
    try:
        setup_router._handoff_error = "something earlier went wrong"
        with tempfile.TemporaryDirectory() as tmp:
            authconfig.CONFIG_PATH = str(Path(tmp) / "auth.env")
            setup_router._write_config("GOTRUE_EXTERNAL_AZURE_ENABLED='true'")
        assert setup_router._handoff_error is None
    finally:
        authconfig.CONFIG_PATH = was
        setup_router._handoff_error = original


def test_the_auth_configuration_is_published_on_startup() -> None:
    """Otherwise the generated file exists only as a side effect of somebody
    visiting the settings screen.

    That is what made the volume repair incomplete: fixing the ownership let
    the API write the file, but nothing asked it to until the next save. An
    instance restored from a backup that did not include the volume had the
    same gap, silently serving whatever the auth container started with.

    The lifespan is actually run here, with what it starts stubbed out. An
    earlier version of this test looked for the call in the function's source,
    which passes just as happily when the name appears in a comment.
    """
    import asyncio
    from unittest.mock import AsyncMock, MagicMock, patch

    from moonphase import main

    published = AsyncMock(return_value=["password"])

    async def enter_startup() -> None:
        with (
            patch.object(main.preflight, "run", AsyncMock()),
            patch.object(main.monitor, "start", MagicMock()),
            patch.object(main.setup, "publish_auth_config", published),
        ):
            started = main.lifespan(MagicMock())
            await started.__aenter__()

    asyncio.run(enter_startup())

    published.assert_awaited_once()


def test_a_failure_to_publish_does_not_stop_the_api_starting() -> None:
    """Being unable to hand the configuration over is a reason to say so on the
    settings screen, not a reason to refuse to serve. An instance that will not
    start cannot be used to fix the reason it will not start."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock, patch

    from moonphase import main

    async def enter_startup() -> None:
        with (
            patch.object(main.preflight, "run", AsyncMock()),
            patch.object(main.monitor, "start", MagicMock()),
            patch.object(
                main.setup,
                "publish_auth_config",
                AsyncMock(side_effect=OSError("read-only file system")),
            ),
        ):
            started = main.lifespan(MagicMock())
            # Getting through this at all is the assertion.
            await started.__aenter__()

    asyncio.run(enter_startup())


def test_saving_the_screen_does_not_erase_a_secret_you_did_not_retype() -> None:
    """The settings form loads every secret as an empty string — it has to, the
    API never sends a secret to a client — so a save that treated blank as
    "clear this" erased whichever secrets were not retyped.

    Someone who set up Microsoft sign-in and later touched anything else on
    that screen was left with a client id, no secret, and "Unsupported
    provider: missing OAuth secret" from the auth service.
    """
    statements = _statements_from_set_auth_secrets(
        {"microsoft_client_secret": "", "google_client_secret": None}
    )

    # Nothing to write: both were blank, so both are left alone.
    assert statements == []


def test_a_secret_that_was_typed_is_written() -> None:
    """The other half — leaving blanks alone must not mean ignoring input."""
    statements = _statements_from_set_auth_secrets(
        {"microsoft_client_secret": "s3cret", "google_client_secret": ""}
    )

    assert len(statements) == 1
    sql = statements[0]
    assert "microsoft_client_secret" in sql
    # And only that one.
    assert "google_client_secret" not in sql


def _statements_from_set_auth_secrets(secrets: dict[str, str | None]) -> list[str]:
    """Run the real function against a connection that records, rather than
    asserting on its source."""
    import asyncio

    from moonphase import queries

    recorded: list[str] = []

    class Recorder:
        async def execute(self, statement, params=None):  # noqa: ANN001
            recorded.append(str(statement))
            return None

    asyncio.run(
        queries.set_auth_secrets_privileged(Recorder(), secrets=secrets)  # type: ignore[arg-type]
    )
    return recorded


def test_the_update_volume_is_repaired_too() -> None:
    """Turning the updater on added a third volume with the same fault.

    The API asks for an update by writing a file into /updates, and it does not
    run as root — so a mount point Docker created as root made "Update" fail
    with "could not reach the updater" on an instance configured correctly in
    every other way. Unlike the auth configuration this one is at least loud,
    but it is the same bug and needs the same repair.

    /updates only exists when the update overlay is in play, so the base
    command guards on the directory rather than assuming it.
    """
    import yaml

    root = Path(__file__).resolve().parents[3]
    base = yaml.safe_load((root / "docker-compose.yml").read_text())
    overlay = yaml.safe_load((root / "docker-compose.update.yml").read_text())

    command = " ".join(base["services"]["prepare-volumes"]["entrypoint"])
    assert "[ -d /updates ]" in command, "must tolerate the overlay being absent"
    assert "chown -R moonphase:moonphase /updates" in command

    # And the overlay has to hand it the volume, or there is nothing to fix.
    mounted = {
        entry.split(":")[0]
        for entry in overlay["services"]["prepare-volumes"]["volumes"]
    }
    assert "update-requests" in mounted

    # The same volume the API writes its request into.
    assert "update-requests:/updates" in overlay["services"]["api"]["volumes"]
