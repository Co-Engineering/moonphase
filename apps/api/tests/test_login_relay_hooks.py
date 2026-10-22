"""The sign-in relay's per-harness hooks, driven end to end without Docker.

test_login_relay.py runs the relay against a real container and a scripted
shell program, which needs a Docker daemon and only ever exercises the
defaults. This stands a fake server in for the SSH connection instead: it
reads the docker and tmux commands login.py actually sends and plays a
terminal program's screens back, so the whole path — start, the screens before
the URL, the URL, the code, the harvest — runs in a unit test, with every key
the relay presses written down.

The Antigravity screens are not made up. The picker and the sign-in screen are
`tmux capture-pane -p` of the shipped agy 1.4.3 binary in a pane the size the
relay opens (200x50); the URL rows are copied from that capture character for
character, one-space indent and all. The rejection's wording is agy's own
(seen when a bad code was typed into it); its layout follows the other two.

Claude Code takes the defaults of every hook, and those defaults are meant to
be exactly what the relay did before the hooks existed. The old code is
reproduced here, verbatim, so that claim is checked rather than assumed.
"""

from __future__ import annotations

import asyncio
import json
import re
import shlex
import time
from types import SimpleNamespace

import pytest

from moonphase import login
from moonphase.harness import (
    Harness,
    HarnessAuthMode,
    HarnessCredential,
    HarnessKind,
    SessionSpace,
)
from moonphase.harness import get as get_harness
from moonphase.harness.base import LaunchSpec
from moonphase.harness.claude_code import LOGIN_URL_PATTERN

# --- a server, as far as login.py can tell -----------------------------------

ROWS = 50


def _pane(*rows: str) -> str:
    """A capture as tmux prints one: every row of the pane, blank ones too."""
    return "\n".join([*rows, *[""] * (ROWS - len(rows))]) + "\n"


class Tui:
    """A terminal program: what is on screen, and what a key does to it."""

    def screen(self) -> str:
        raise NotImplementedError

    def key(self, name: str) -> None:
        del name

    def type(self, text: str) -> None:
        del text


class Clock:
    """Virtual time. Each look at the pane is one poll interval later.

    The relay's deadlines are in seconds, and a test that waited them out for
    real would take minutes; ticking per capture instead keeps the relay's
    arithmetic exactly as it runs, with nothing to sleep through.
    """

    def __init__(self) -> None:
        self.now = time.monotonic()

    def monotonic(self) -> float:
        return self.now


class FakeServer:
    """Stands in for an SSH connection to a server with Docker on it.

    Only `run` is needed: login.py reaches the server through `ssh.run` and
    `docker_remote.exec_capture`, both of which come down to
    `conn.run(command)`. Each command is parsed back into the argv it was
    built from, so quoting mistakes would show up here as they would there.
    """

    def __init__(self, tui: Tui, clock: Clock, *, refuse_keys: int = 0) -> None:
        self.tui = tui
        self.clock = clock
        self.files: dict[str, str] = {}
        self.containers: list[str] = []
        self.removed: list[str] = []
        self.started_with: str | None = None
        self.captures = 0
        # (key, which capture it followed) for every key the relay pressed.
        self.keys: list[tuple[str, int]] = []
        self.typed: list[str] = []
        self.refuse_keys = refuse_keys

    async def run(self, command: str, check: bool = False, input: str | None = None):
        del check
        argv = shlex.split(command)
        if argv[:2] == ["docker", "ps"]:
            return self._ok("")
        if argv[:2] == ["docker", "run"]:
            self.containers.append(argv[argv.index("--name") + 1])
            return self._ok("cid\n")
        if argv[:3] == ["docker", "rm", "-f"]:
            self.removed.append(argv[3])
            return self._ok("")
        if argv[:2] == ["docker", "exec"]:
            return self._exec(argv[2:], input)
        raise AssertionError(f"unexpected command: {command}")

    def _exec(self, argv: list[str], stdin: str | None):
        while argv[0].startswith("-"):
            argv = argv[1:] if argv[0] == "-i" else argv[2:]
        cmd = argv[1:]  # argv[0] is the container

        if cmd[:2] == ["sh", "-c"]:
            words = shlex.split(cmd[2])
            if stdin is not None and "cat" in words and ">" in words:
                self.files[words[-1]] = stdin
                return self._ok("")
            if words[0] == "cat":
                contents = self.files.get(words[1])
                return self._ok(contents) if contents is not None else self._fail()
            # An auth status script: nothing to report.
            return self._ok("")

        if cmd[0] == "tmux":
            if cmd[1] == "new-session":
                self.started_with = cmd[-1]
                return self._ok("")
            if cmd[1] == "capture-pane":
                self.captures += 1
                self.clock.now += login.URL_POLL_SECONDS or 1.5
                return self._ok(self.tui.screen())
            if cmd[1] == "send-keys":
                if "-l" in cmd:
                    self.typed.append(cmd[-1])
                    self.tui.type(cmd[-1])
                    return self._ok("")
                if self.refuse_keys:
                    self.refuse_keys -= 1
                    return self._fail("no server running")
                self.keys.append((cmd[-1], self.captures))
                self.tui.key(cmd[-1])
                return self._ok("")
        raise AssertionError(f"unexpected exec: {argv}")

    @staticmethod
    def _ok(stdout: str):
        return SimpleNamespace(exit_status=0, stdout=stdout, stderr="")

    @staticmethod
    def _fail(stderr: str = ""):
        return SimpleNamespace(exit_status=1, stdout="", stderr=stderr)

    def pressed(self) -> list[str]:
        return [key for key, _ in self.keys]


@pytest.fixture
def clock(monkeypatch) -> Clock:
    clock = Clock()
    # Only login.py's view of the time; the event loop keeps the real one.
    monkeypatch.setattr(login, "time", SimpleNamespace(monotonic=clock.monotonic))
    monkeypatch.setattr(login, "URL_POLL_SECONDS", 0)
    return clock


async def _start(server: FakeServer, harness: Harness) -> login.LoginSession:
    """Start a sign-in and wait for its background preparation to finish."""
    before = set(login._tasks)
    session = await login.start(
        server,  # type: ignore[arg-type]
        org_id="org",
        server_id="server",
        harness=harness,
        image="moonphase/runtime:test",
    )
    pending = login._tasks - before
    await asyncio.wait_for(asyncio.gather(*pending), timeout=10)
    login.forget(session.id)
    return session


async def _verify(
    server: FakeServer, session: login.LoginSession, harness: Harness, *, polls: int = 10
) -> tuple[login.LoginSession, int]:
    """Poll a submitted code as the client does; returns how many polls it took."""
    for n in range(1, polls + 1):
        session = await login.advance(server, session, harness)  # type: ignore[arg-type]
        if session.state != "verifying":
            return session, n
    return session, polls


# --- generic behaviour, with a harness of our own -----------------------------

URL = "https://auth.example.com/authorize?client_id=moonphase-test&state=abc123"


class Steps(Tui):
    """A program that is a list of screens, each moved on from by one key.

    `blank` captures of nothing come first, as a program that has not drawn
    yet; a key that arrives then is lost, which is what agy 1.4.3 does with an
    Enter sent before its first frame. `deaf` screens ignore every key, so a
    test can see whether a key is ever sent twice.
    """

    def __init__(
        self,
        screens: list[tuple[str, str | None]],
        *,
        blank: int = 0,
        holds: dict[int, int] | None = None,
        deaf: bool = False,
    ) -> None:
        self.screens = screens  # (text, the key that moves on from it)
        self.blank = blank
        # How many captures a screen stays up before it is even drawn (its
        # index -> captures), for a program slow to move on after a key.
        self.holds = dict(holds or {})
        self.deaf = deaf
        self.at = 0
        self.lost: list[str] = []

    def screen(self) -> str:
        if self.blank:
            self.blank -= 1
            return _pane()
        if self.holds.get(self.at):
            self.holds[self.at] -= 1
            return _pane()
        return _pane(self.screens[self.at][0])

    def key(self, name: str) -> None:
        if self.blank or self.holds.get(self.at) or self.deaf:
            self.lost.append(name)
            return
        expected = self.screens[self.at][1]
        assert name == expected, f"{name!r} pressed on screen {self.at}"
        self.at += 1


class Fake(Harness):
    """A harness whose sign-in opens on two screens before its URL."""

    kind = HarnessKind.PYDANTIC_AI
    display_name = "Fake Agent"
    supported_auth_modes = (HarnessAuthMode.OAUTH,)

    def __init__(self, *, preamble=(), failures=None, paths=()) -> None:
        self.preamble = tuple(preamble)
        self.failures = failures
        self.paths = list(paths)

    def launch_spec(self, *, resume=False, credential=None) -> LaunchSpec:
        return LaunchSpec(command=["fake"])

    def credential_files(self, credential, space) -> dict[str, str]:
        return {}

    def credential_env(self, credential) -> dict[str, str]:
        return {}

    def auth_probe_script(self, space) -> str:
        return "true"

    def login_command(self) -> list[str]:
        return ["fake", "login"]

    def login_preamble(self):
        return self.preamble

    def login_failure_patterns(self):
        return super().login_failure_patterns() if self.failures is None else self.failures

    def credential_paths(self) -> list[str]:
        return self.paths

    def transcript_dir(self, space) -> str:
        return "/home/dev/.fake"

    def version_command(self) -> list[str]:
        return ["true"]


TWO_SCREENS = [
    ("Choose a theme:\n> dark\n  light", "Enter"),
    ("Sign in with?\n  1. Password\n  2. Browser", "2"),
    # Indented, as Claude Code's own prompt is: the default rejoin glues a
    # word at column 0 onto the end of the URL.
    (f"Open this link:\n{URL}\n\n Paste the code:", None),
]
TWO_STEPS = ((r"Choose a theme:", "Enter"), (r"Sign in with\?", "2"))


@pytest.mark.asyncio
async def test_each_screen_before_the_url_gets_its_key_once_and_in_order(clock) -> None:
    """The point of the preamble: a sign-in that opens on a picker rather
    than its URL used to sit there until the minute ran out."""
    server = FakeServer(Steps(TWO_SCREENS), clock)

    session = await _start(server, Fake(preamble=TWO_STEPS))

    assert session.state == "awaiting_code", session.detail
    assert session.url == URL
    assert server.pressed() == ["Enter", "2"]
    assert server.started_with is not None and server.started_with.startswith("fake login;")


@pytest.mark.asyncio
async def test_a_key_waits_until_its_screen_has_been_up_for_a_whole_poll(clock) -> None:
    """agy drops an Enter that arrives before it has drawn anything, so a key
    sent early is not late, it is gone — and is never sent again. Each key
    therefore goes only once its screen has been seen twice running: the
    program has been showing it, and so reading keys, for a poll already."""
    tui = Steps(TWO_SCREENS, blank=3)
    server = FakeServer(tui, clock)

    session = await _start(server, Fake(preamble=TWO_STEPS))

    assert session.state == "awaiting_code"
    assert tui.lost == []
    # Three blank captures, the theme screen on the 4th and 5th, Enter after
    # the 5th; the next screen on the 6th and 7th, "2" after the 7th.
    assert server.keys == [("Enter", 5), ("2", 7)]


@pytest.mark.asyncio
async def test_a_key_that_goes_nowhere_is_not_sent_again(clock) -> None:
    """A screen that is slow to change is far likelier than a lost key, and a
    repeat would land on whatever came next. What a stuck sign-in gets is the
    error, with the terminal shown, once the wait runs out."""
    tui = Steps(TWO_SCREENS, deaf=True)
    server = FakeServer(tui, clock)

    session = await _start(server, Fake(preamble=TWO_STEPS))

    assert server.pressed() == ["Enter"]
    assert session.state == "error"
    assert session.detail is not None
    assert session.detail.startswith("Fake Agent did not show a sign-in link in time.")
    assert "terminal output below" in session.detail
    assert "Choose a theme:" in session.pane
    assert server.removed == server.containers


@pytest.mark.asyncio
async def test_getting_past_a_screen_restarts_the_wait_for_the_url(clock) -> None:
    """The minute is for a harness that has stopped moving, not a budget for
    the whole sign-in: a picker that took 45 seconds to come up must not
    leave the URL fifteen."""
    # The picker on the 31st capture (45 s in), the URL 30 captures after the
    # Enter: 90 s from the start, well past a single minute.
    tui = Steps(TWO_SCREENS[1:], blank=30, holds={1: 30})
    server = FakeServer(tui, clock)
    started = clock.now

    session = await _start(server, Fake(preamble=TWO_STEPS[1:]))

    assert session.state == "awaiting_code", session.detail
    assert session.url == URL
    assert clock.now - started > login.URL_WAIT_SECONDS


@pytest.mark.asyncio
async def test_with_nothing_on_screen_the_wait_still_ends_after_a_minute(clock) -> None:
    server = FakeServer(Steps([("", None)], blank=10_000), clock)
    started = clock.now

    session = await _start(server, Fake(preamble=TWO_STEPS))

    assert session.state == "error"
    assert server.keys == []
    assert login.URL_WAIT_SECONDS <= clock.now - started <= login.URL_WAIT_SECONDS + 1.5


@pytest.mark.asyncio
async def test_a_key_tmux_refused_is_tried_again_while_its_screen_is_up(clock) -> None:
    """Refused means not delivered, so it has not been sent yet."""
    server = FakeServer(Steps(TWO_SCREENS), clock, refuse_keys=1)

    session = await _start(server, Fake(preamble=TWO_STEPS))

    assert session.state == "awaiting_code"
    assert server.pressed() == ["Enter", "2"]


def test_a_rejection_is_whatever_the_harness_says_one_looks_like() -> None:
    """Regexes, matched without regard to case — and only the harness's own:
    a harness that names its own patterns does not also get Claude's."""
    harness = Fake(failures=(r"code (?:was )?not accepted",))

    assert login._rejected("Error: Code was NOT accepted.", harness)
    assert not login._rejected("Your session has expired.", harness)
    assert login._rejected("Your session has EXPIRED.", Fake())


@pytest.mark.asyncio
async def test_the_first_credentials_file_with_something_in_it_is_harvested(clock) -> None:
    server = FakeServer(Steps([("", None)]), clock)
    harness = Fake(paths=["/home/dev/.fake/empty", "/home/dev/.fake/token"])
    server.files["/home/dev/.fake/empty"] = "   \n"
    server.files["/home/dev/.fake/token"] = '{"token": "t"}'
    session = login.LoginSession(
        id="x", org_id="o", harness_kind="pydantic_ai", server_id="s",
        container="mp-login-x", state="verifying", verifying_since=clock.now,
    )

    session = await login.advance(server, session, harness)  # type: ignore[arg-type]

    assert session.state == "complete"
    assert session.oauth_blob == '{"token": "t"}'


# --- Antigravity, as agy 1.4.3 draws it ---------------------------------------

LOGO = (
    "",
    "     ▄▀▀▄",
    "    ▀▀▀▀▀▀",
    "   ▀▀▀▀▀▀▀▀",
    "  ▄▀▀    ▀▀▄",
    " ▄▀▀      ▀▀▄",
    "",
)

AGY_PICKER = _pane(
    *LOGO,
    " Welcome to the Antigravity CLI. You are currently not signed in.",
    "",
    " Select login method:",
    " > 1. Google OAuth",
    "   2. Use a Google Cloud project",
    "",
    "   ↑/↓ Navigate · enter Select",
)

# The four rows agy wrapped the URL into, as captured: 199 columns each but the
# last, every one of them — not only the continuations — indented by a space.
AGY_URL_ROWS = (
    (
        " https://accounts.google.com/o/oauth2/auth?access_type=offline&client_id"
        "=1071006060591-tmhssin2h21lcre235vtolojh4g403ep.apps.googleusercontent.c"
        "om&code_challenge=vuTyqnpSTQ0y58zFO4wLSeYF6LErpBkBWk9DW"
    ),
    (
        " UhrlOk&code_challenge_method=S256&prompt=consent&redirect_uri=https%3A%"
        "2F%2Fantigravity.google%2Foauth-callback&response_type=code&scope=https%"
        "3A%2F%2Fwww.googleapis.com%2Fauth%2Fcloud-platform+http"
    ),
    (
        " s%3A%2F%2Fwww.googleapis.com%2Fauth%2Fuserinfo.email+https%3A%2F%2Fwww."
        "googleapis.com%2Fauth%2Fuserinfo.profile+https%3A%2F%2Fwww.googleapis.co"
        "m%2Fauth%2Fcclog+https%3A%2F%2Fwww.googleapis.com%2Faut"
    ),
    (
        " h%2Fexperimentsandconfigs+https%3A%2F%2Fwww.googleapis.com%2Fauth%2Faic"
        "ode+openid&state=ndKH4feD525C1r8M9bnpFw"
    ),
)

AGY_RULE = " " + "─" * 198

AGY_SIGN_IN = _pane(
    *LOGO,
    " Your browser should open automatically. If not:",
    "",
    *AGY_URL_ROWS,
    "",
    " Copy and paste the URL or click on the link below:",
    AGY_RULE,
    " → Click here to authenticate",
    AGY_RULE,
    "",
    " If you aren't automatically redirected, paste the authorization code below:",
    "",
    " authorization code...",
    "",
    "",
    "  shift+up/down Navigate",
)

# The same URL in one piece, as agy hands it to xdg-open.
EXPECTED_URL = (
    "https://accounts.google.com/o/oauth2/auth?access_type=offline"
    "&client_id=1071006060591-tmhssin2h21lcre235vtolojh4g403ep.apps.googleusercontent.com"
    "&code_challenge=vuTyqnpSTQ0y58zFO4wLSeYF6LErpBkBWk9DWUhrlOk"
    "&code_challenge_method=S256"
    "&prompt=consent"
    "&redirect_uri=https%3A%2F%2Fantigravity.google%2Foauth-callback"
    "&response_type=code"
    "&scope=https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fcloud-platform"
    "+https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fuserinfo.email"
    "+https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fuserinfo.profile"
    "+https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fcclog"
    "+https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fexperimentsandconfigs"
    "+https%3A%2F%2Fwww.googleapis.com%2Fauth%2Faicode"
    "+openid"
    "&state=ndKH4feD525C1r8M9bnpFw"
)

AGY_REJECTED = _pane(
    *LOGO,
    ' Got an error: token exchange failed: oauth2: "invalid_grant" "Malformed auth code."',
    "",
    " Press any key to go back.",
)

# First start after signing in, which the relay never answers: it has what it
# came for by then.
AGY_SIGNED_IN = _pane(
    " Welcome to Antigravity CLI!",
    "",
    " Choose your color scheme:",
    " > terminal",
)

GOOD_CODE = "4/0AVGzR1A-good-code"
TOKEN_PATH = "/home/dev/.gemini/antigravity-cli/antigravity-oauth-token"
TOKEN = json.dumps(
    {
        "token": {
            "access_token": "ya29.FAKE",
            "token_type": "Bearer",
            "refresh_token": "1//FAKE",
            "expiry": "2026-10-20T21:00:00Z",
        },
        "auth_method": "consumer",
    }
)


class Agy(Tui):
    """agy's sign-in, screen by screen, as the shipped 1.4.3 binary behaves.

    Checked against it: keys before the first frame are lost; Enter on the
    picker starts the Google sign-in; an Enter on the empty code field does
    nothing; a bad code is an error, and any key then goes back to the picker.
    A good code is exchanged — a poll's worth of waiting here — and the token
    written to the file agy uses when there is no keyring.
    """

    def __init__(self, server: FakeServer, *, draws_after: int = 1) -> None:
        self.server = server
        self.state = "starting"
        self.draws_after = draws_after
        self.field = ""

    def screen(self) -> str:
        if self.state == "starting":
            self.draws_after -= 1
            if self.draws_after < 0:
                self.state = "picker"
            return _pane() if self.state == "starting" else AGY_PICKER
        if self.state == "exchanging":
            # Still talking to Google: the screen has not changed yet.
            self.state = "exchanged"
            return AGY_SIGN_IN
        if self.state == "exchanged":
            self.server.files[TOKEN_PATH] = TOKEN
            self.state = "signed_in"
        return {
            "picker": AGY_PICKER,
            "sign_in": AGY_SIGN_IN,
            "rejected": AGY_REJECTED,
            "signed_in": AGY_SIGNED_IN,
        }[self.state]

    def key(self, name: str) -> None:
        if self.state == "picker" and name == "Enter":
            self.state = "sign_in"
        elif self.state == "sign_in" and name == "Enter" and self.field:
            self.state = "exchanging" if self.field == GOOD_CODE else "rejected"
            self.field = ""
        elif self.state == "rejected":
            self.state = "picker"

    def type(self, text: str) -> None:
        if self.state == "sign_in":
            self.field += text


def _agy(clock: Clock) -> FakeServer:
    server = FakeServer(Tui(), clock)
    server.tui = Agy(server)
    return server


@pytest.mark.asyncio
async def test_an_antigravity_sign_in_gets_past_its_picker_to_the_whole_url(clock) -> None:
    """agy has no sign-in subcommand: the TUI signs in when it finds no
    credential, and it opens on a picker of methods. Moonphase's sign-in
    button is the choice of the first one, which is already highlighted, so
    one Enter — once — is all the relay may press. And the URL has to come
    out whole: agy indents each row it wraps, and a rejoin that kept those
    spaces, or stopped at the first, is a link that does not work."""
    server = _agy(clock)
    harness = get_harness("antigravity")

    session = await _start(server, harness)

    assert session.state == "awaiting_code", f"{session.detail}\n{session.pane}"
    assert session.url == EXPECTED_URL
    assert server.pressed() == ["Enter"]
    # Pressed while the picker was on screen, not before agy drew it.
    assert server.tui.state == "sign_in"
    assert server.started_with is not None and server.started_with.startswith("agy;")


def test_rejoining_agys_rows_plainly_would_have_cut_the_url_at_its_first_wrap() -> None:
    """Why agy reads its own URL: the default rejoin stops at the indent."""
    harness = get_harness("antigravity")

    plain = Harness.login_url(harness, AGY_SIGN_IN)

    assert plain is not None and EXPECTED_URL.startswith(plain)
    assert len(plain) < len(EXPECTED_URL)
    assert harness.login_url(AGY_SIGN_IN) == EXPECTED_URL


@pytest.mark.asyncio
async def test_a_good_code_harvests_agys_token_file_as_the_account(clock) -> None:
    server = _agy(clock)
    harness = get_harness("antigravity")
    session = await _start(server, harness)

    session = await login.submit_code(server, session, f"  {GOOD_CODE}\n")  # type: ignore[arg-type]
    assert session.state == "verifying"
    assert server.typed == [GOOD_CODE]
    assert server.pressed() == ["Enter", "Enter"]

    session, polls = await _verify(server, session, harness)

    assert session.state == "complete", f"{session.detail}\n{session.pane}"
    assert session.oauth_blob == TOKEN
    assert session.oauth_token is None
    # The exchange takes a moment; the first poll found nothing yet and the
    # relay kept looking rather than calling it a failure.
    assert polls == 2
    assert server.removed == server.containers

    # And what was harvested is what a session is started with.
    files = harness.credential_files(
        HarnessCredential(mode=HarnessAuthMode.OAUTH, oauth_blob=session.oauth_blob),
        SessionSpace(home="/home/dev/sessions/s"),
    )
    assert files == {"/home/dev/sessions/s/.gemini/antigravity-cli/antigravity-oauth-token": TOKEN}


@pytest.mark.asyncio
async def test_a_code_agy_rejects_ends_the_sign_in_on_the_next_look(clock) -> None:
    """Claude Code's phrases ("invalid code", "expired", "authentication
    failed") appear nowhere in agy's rejection, so before agy said what its
    own looks like this would have polled for three minutes."""
    server = _agy(clock)
    harness = get_harness("antigravity")
    session = await _start(server, harness)

    await login.submit_code(server, session, "not-a-real-code")  # type: ignore[arg-type]
    session, polls = await _verify(server, session, harness)

    assert session.state == "error"
    assert polls == 1
    assert session.detail == "Antigravity rejected that code. Start again."
    assert '"invalid_grant"' in session.pane
    assert session.oauth_blob is None and session.oauth_token is None
    assert server.removed == server.containers


def test_agys_sign_in_screen_is_not_mistaken_for_a_rejection() -> None:
    """Matched against the whole pane while a good code is being exchanged."""
    harness = get_harness("antigravity")
    assert not login._rejected(AGY_SIGN_IN, harness)
    assert not login._rejected(AGY_PICKER, harness)
    assert login._rejected(AGY_REJECTED, harness)


# --- Claude Code, exactly as before ------------------------------------------


def _url_before(pane: str, pattern: str | None) -> str | None:
    """login._prepare's URL scrape before the hooks, verbatim."""
    compiled = re.compile(pattern or r"https://\S+")
    match = compiled.search(pane.replace("\n", ""))
    return match.group(0).rstrip(")]},.") if match else None


def _rejected_before(pane: str) -> bool:
    """login.advance's rejection check before the hooks, verbatim."""
    lowered = pane.lower()
    return any(word in lowered for word in ("invalid code", "expired", "authentication failed"))


SETUP_TOKEN_URL = (
    "https://claude.com/cai/oauth/authorize?code=true"
    "&client_id=9d1c250a-e61b-44d9-88ed-5944d1962f5e&response_type=code"
    "&redirect_uri=https%3A%2F%2Fplatform.claude.com%2Foauth%2Fcode%2Fcallback"
    "&scope=user%3Ainference&code_challenge=Zx9-abcdefghijklmnopqrstuvwxyz0123456789ABCDE"
    "&code_challenge_method=S256&state=Q1w2E3r4T5y6U7i8O9p0"
)

CLAUDE_PANES = [
    "",
    "Welcome to Claude Code\n",
    # On one row, under its banner.
    f" Browser didn't open? Use the url below to sign in:\n\n{SETUP_TOKEN_URL}\n\n"
    " Paste code here if prompted >\n",
    # Hard-wrapped at 200 columns, as the relay's pane draws it.
    " Browser didn't open? Use the url below to sign in:\n\n"
    + "\n".join(SETUP_TOKEN_URL[i : i + 200] for i in range(0, len(SETUP_TOKEN_URL), 200))
    + "\n\n Paste code here if prompted >\n",
    # In brackets, and followed by a full stop.
    f"Open ({SETUP_TOKEN_URL}).\n",
    # A different site entirely.
    "See https://docs.anthropic.com/setup for help.\n",
    # test_login_relay.py's scripted flow, whose prompt sits at column 0.
    "https://claude.com/cai/oauth/authorize?code=xyz&client_id=test&scope=user\n"
    "\nPaste code here if prompted > ",
]

CLAUDE_OUTCOMES = [
    "Login successful!\n",
    "Invalid code supplied\n",
    "OAuth error: Invalid Code\n",
    "The authorization code has EXPIRED. Try again.\n",
    "Authentication failed: 400\n",
    "Your long-lived token: sk-ant-oat01-abc\n",
]


@pytest.mark.parametrize("pane", CLAUDE_PANES)
def test_claude_codes_sign_in_link_is_read_as_it_always_was(pane: str) -> None:
    claude = get_harness("claude_code")
    assert claude.login_url(pane) == _url_before(pane, LOGIN_URL_PATTERN)


@pytest.mark.parametrize("pane", CLAUDE_OUTCOMES + CLAUDE_PANES)
def test_claude_codes_rejections_are_recognised_as_they_always_were(pane: str) -> None:
    assert login._rejected(pane, get_harness("claude_code")) == _rejected_before(pane)


def test_claude_code_has_no_screens_to_get_past() -> None:
    assert get_harness("claude_code").login_preamble() == ()


class SetupToken(Tui):
    """`claude setup-token`, reduced to the shape the relay sees."""

    def __init__(self) -> None:
        self.code = ""
        self.done = False

    def screen(self) -> str:
        if self.done:
            return _pane("Your OAuth token:", "", "sk-ant-oat01-FAKEfake0123456789abcdefghijklmnop")
        return _pane(" Browser didn't open? Use the url below to sign in:", "", SETUP_TOKEN_URL)

    def type(self, text: str) -> None:
        self.code += text

    def key(self, name: str) -> None:
        if name == "Enter" and self.code:
            self.done = True


@pytest.mark.asyncio
async def test_a_claude_code_sign_in_presses_nothing_until_the_code_is_pasted(clock) -> None:
    server = FakeServer(SetupToken(), clock)
    claude = get_harness("claude_code")

    session = await _start(server, claude)

    assert session.state == "awaiting_code", session.detail
    assert session.url == SETUP_TOKEN_URL
    assert server.keys == []
    # Found on the first look, as before: no preamble, so no second sighting.
    assert server.captures == 1
    assert server.started_with is not None
    assert server.started_with.startswith("claude setup-token;")

    await login.submit_code(server, session, "code#state")  # type: ignore[arg-type]
    session, _ = await _verify(server, session, claude)

    assert session.state == "complete"
    assert session.oauth_token == "sk-ant-oat01-FAKEfake0123456789abcdefghijklmnop"
    assert server.pressed() == ["Enter"]
