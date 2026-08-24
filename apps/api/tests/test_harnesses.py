"""The harness seam, exercised through every harness registered in it.

Written against the registry rather than one class, so a fourth harness is
covered the day it is added and the rules that hold for all of them are stated
once. The facts specific to each — OpenCode's auth file, the model clai has to
be told — were checked against the real binaries in the runtime image, not
inferred from documentation.
"""

from __future__ import annotations

import pytest

from moonphase import harness as registry
from moonphase.harness import (
    Harness,
    HarnessAuthMode,
    HarnessCredential,
    HarnessKind,
    SessionSpace,
)

ALL = registry.available()


def _key(value: str = "sk-ant-example") -> HarnessCredential:
    return HarnessCredential(mode=HarnessAuthMode.API_KEY, api_key=value)


# --- what must be true of any harness ---------------------------------------


def test_every_kind_has_an_implementation() -> None:
    """The enum and the registry are two places to say the same thing, and a
    value with no class behind it is a project that cannot be created."""
    for kind in HarnessKind:
        assert isinstance(registry.get(kind), Harness), kind


@pytest.mark.parametrize("h", ALL, ids=lambda h: str(h.kind))
def test_a_harness_says_what_it_is(h: Harness) -> None:
    assert h.display_name
    assert h.supported_auth_modes, "a harness nobody can authenticate is unusable"


@pytest.mark.parametrize("h", ALL, ids=lambda h: str(h.kind))
def test_launching_needs_no_credential(h: Harness) -> None:
    """A session can start before a credential is attached — it will fail to
    authenticate, which is a better failure than the launcher raising."""
    spec = h.launch_spec()
    assert spec.command, "something has to be run"
    assert spec.workdir


@pytest.mark.parametrize("h", ALL, ids=lambda h: str(h.kind))
def test_an_api_key_reaches_the_agent_somehow(h: Harness) -> None:
    """Every harness here supports a key; each takes it by file, by environment
    or both, and taking it by neither means a session that cannot work."""
    if HarnessAuthMode.API_KEY not in h.supported_auth_modes:
        pytest.skip(f"{h.kind} does not take an API key")
    credential = _key()
    space = SessionSpace()
    delivered = h.credential_files(credential, space) or h.credential_env(credential)
    assert delivered, f"{h.kind} accepts a key and does nothing with it"


@pytest.mark.parametrize("h", ALL, ids=lambda h: str(h.kind))
def test_the_probe_and_the_paths_agree_on_home(h: Harness) -> None:
    """Two people in one container are kept apart by HOME alone, so anything
    naming a path has to be built from the session's own space rather than
    hard-coded."""
    space = SessionSpace(home="/home/dev/sessions/alice", workdir="/home/dev/sessions/alice/work")
    assert space.home in h.transcript_dir(space) or space.workdir in h.transcript_dir(space)
    probe = h.auth_probe_script(space)
    # Either it looks at a file under this session's home, or at the
    # environment — never at another session's directory.
    assert "/home/dev/sessions/bob" not in probe


# --- the two new ones ---------------------------------------------------------


def test_opencode_writes_the_auth_file_it_actually_reads() -> None:
    """Verified against the binary: `opencode auth list` reports
    "Anthropic api, 1 credentials" for exactly this shape, at exactly this
    path. Writing the environment alone leaves its own commands reporting the
    session as signed out while it works."""
    files = registry.get("opencode").credential_files(_key(), SessionSpace())
    (path,) = files
    assert path.endswith(".local/share/opencode/auth.json")
    assert '"type": "api"' in files[path]
    assert '"anthropic"' in files[path]


def test_opencode_picks_the_provider_from_the_key() -> None:
    opencode = registry.get("opencode")
    anthropic = opencode.credential_env(_key("sk-ant-x"))
    openai = opencode.credential_env(_key("sk-proj-x"))
    assert "ANTHROPIC_API_KEY" in anthropic
    assert "OPENAI_API_KEY" in openai


def test_opencode_resumes_its_previous_session() -> None:
    """What makes a session survive its container restarting under it."""
    assert registry.get("opencode").launch_spec(resume=True).command == [
        "opencode",
        "--continue",
    ]


def test_pydantic_runs_the_coder_agent_not_the_chat() -> None:
    """`clai` on its own is a chat window that cannot touch the repository it is
    sitting in. The `-a` is what makes this a coding harness."""
    command = registry.get("pydantic_ai").launch_spec(credential=_key()).command
    assert "-a" in command
    assert "pydantic_ai_harness.coder:coder_agent" in command


def test_pydantic_is_told_which_model_to_use() -> None:
    """There is no CLAI_MODEL — checked against `clai --help`. The model is an
    argument, and clai defaults to OpenAI, so an Anthropic key with no `-m`
    fails asking for an OpenAI key."""
    pydantic = registry.get("pydantic_ai")

    anthropic = pydantic.launch_spec(credential=_key("sk-ant-x")).command
    assert "-m" in anthropic
    assert anthropic[anthropic.index("-m") + 1].startswith("anthropic:")

    openai = pydantic.launch_spec(credential=_key("sk-proj-x")).command
    assert openai[openai.index("-m") + 1].startswith("openai:")


def test_pydantic_does_not_pretend_to_resume() -> None:
    """It has no flag for it. A flag that did nothing would be worse than the
    session honestly coming back fresh."""
    pydantic = registry.get("pydantic_ai")
    assert (
        pydantic.launch_spec(resume=True, credential=_key()).command
        == pydantic.launch_spec(resume=False, credential=_key()).command
    )


def test_the_runtime_image_installs_every_harness() -> None:
    """A harness with no binary in the image is a project that launches nothing.

    The recipe version has to move with it, or servers keep the image they
    already built — which would have neither new agent in it.
    """
    from moonphase import imagebuild

    recipe = imagebuild.recipe_for("debian:bookworm-slim", None)

    assert "@anthropic-ai/claude-code" in recipe
    assert "opencode-ai" in recipe
    assert "pydantic-ai-harness" in recipe
    # clai is the executable; installing the harness package as a tool fails
    # with "No executables are provided by package".
    assert "uv tool install clai" in recipe
    # The tool's virtualenv has to be readable by `dev`, or the shim is on PATH
    # and cannot be executed.
    assert "UV_TOOL_DIR" in recipe
    assert int(imagebuild.RECIPE_VERSION) >= 4
