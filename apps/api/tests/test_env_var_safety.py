"""An env var key becomes a literal `KEY=value` line in a file that gets
`.`-sourced as bash (profile.py's apply() and sessions.py's launcher script
and is_authenticated() all do this) -- only the value is shell-quoted when
that file is written, so an unrestricted key is a command-injection
primitive. These check both layers: the schema validator that rejects a bad
key at the API boundary, and the defense-in-depth pattern profile.py
re-checks immediately before it ever writes the file.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from moonphase.profile import _ENV_KEY_RE
from moonphase.schemas import ClaudeConfigIn, WorkspaceProfileIn

# The exact shape from the filed issue: a key that, once `.`-sourced, runs an
# arbitrary command regardless of what the (harmless) value is.
INJECTION_KEY = "A; touch /tmp/pwned #"


@pytest.mark.parametrize("schema", [WorkspaceProfileIn, ClaudeConfigIn])
def test_an_injection_shaped_env_key_is_rejected(schema: type) -> None:
    with pytest.raises(ValidationError):
        schema(env_vars={INJECTION_KEY: "x"})


@pytest.mark.parametrize("schema", [WorkspaceProfileIn, ClaudeConfigIn])
def test_a_key_starting_with_a_digit_is_rejected(schema: type) -> None:
    """Not shell-injection-shaped, but still not a valid identifier."""
    with pytest.raises(ValidationError):
        schema(env_vars={"1SECRET": "x"})


@pytest.mark.parametrize("schema", [WorkspaceProfileIn, ClaudeConfigIn])
def test_an_ordinary_env_key_is_accepted(schema: type) -> None:
    value = schema(env_vars={"API_KEY": "sk-test", "_PRIVATE": "1"})
    assert value.env_vars == {"API_KEY": "sk-test", "_PRIVATE": "1"}


def test_profiles_own_last_line_of_defence_matches_the_schema_rule() -> None:
    """The re-check profile.py runs immediately before writing the env file,
    independent of whatever already passed schema validation on the way in."""
    assert _ENV_KEY_RE.match("API_KEY")
    assert _ENV_KEY_RE.match("_PRIVATE")
    assert not _ENV_KEY_RE.match(INJECTION_KEY)
    assert not _ENV_KEY_RE.match("1SECRET")
    assert not _ENV_KEY_RE.match("HAS SPACE")
