"""What the browser sends and what the API requires have to be the same thing.

The paste endpoint required a `session_id` in its body that the handler never
read — the id is a path parameter on the only route that takes it. The client
therefore sent a body without it, and FastAPI refused every request with
"Field required". The connection could be started and never finished, and the
message named a field the person had no way to fill in.

Checked here by building each request the way `apps/web/src/lib/api.ts` builds
it, so a field added on one side and not the other fails before anyone meets
it in a dialog.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from moonphase.schemas import McpOAuthPasteIn, McpOAuthStartIn

API_TS = (
    Path(__file__).resolve().parents[3] / "apps/web/src/lib/api.ts"
).read_text()


def _body_keys(fn: str) -> set[str]:
    """The JSON body `api.ts` sends for one call."""
    start = API_TS.index(f"{fn}: (")
    body = API_TS[start : start + 600]
    match = re.search(r"JSON\.stringify\(\{([^}]*)\}\)", body)
    assert match, f"no JSON body found for {fn}"
    return {k.strip().split(":")[0].strip() for k in match.group(1).split(",") if k.strip()}


def test_the_paste_body_is_what_the_client_sends() -> None:
    sent = _body_keys("pasteMcpOAuth")
    required = {
        name for name, f in McpOAuthPasteIn.model_fields.items() if f.is_required()
    }

    assert required <= sent, f"API requires {required - sent}, which the client never sends"


def test_the_start_body_is_what_the_client_sends() -> None:
    sent = _body_keys("startMcpOAuth")
    required = {
        name for name, f in McpOAuthStartIn.model_fields.items() if f.is_required()
    }

    assert required <= sent, f"API requires {required - sent}, which the client never sends"


def test_a_paste_carrying_only_the_redirect_url_is_accepted() -> None:
    """The exact shape the dialog posts."""
    McpOAuthPasteIn(redirect_url="http://localhost:3118/callback?code=abc&state=def")


def test_an_empty_redirect_url_is_still_refused() -> None:
    """Relaxing the schema must not accept a paste with nothing in it."""
    with pytest.raises(ValidationError):
        McpOAuthPasteIn(redirect_url="")
