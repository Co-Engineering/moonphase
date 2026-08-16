"""Harness registry.

Importing a harness module registers it, so `harness.get("claude_code")` works
anywhere without the caller knowing which module defines it.
"""

from .base import (
    AuthStatus,
    Harness,
    HarnessAuthMode,
    HarnessCredential,
    HarnessKind,
    LaunchSpec,
    SessionSpace,
    available,
    get,
    register,
)
from .claude_code import ClaudeCode  # noqa: F401  (import registers the harness)

__all__ = [
    "AuthStatus",
    "ClaudeCode",
    "Harness",
    "HarnessAuthMode",
    "HarnessCredential",
    "HarnessKind",
    "LaunchSpec",
    "SessionSpace",
    "available",
    "get",
    "register",
]
