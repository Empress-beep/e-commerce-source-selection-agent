from __future__ import annotations

from typing import Any


def run_self_check() -> None:
    """Add fixed-input assertions here after the signer is recovered."""


def build_sign(
    sign_input: str,
    payload: dict[str, Any],
    *,
    bootstrap_state: dict[str, Any] | None = None,
) -> str:
    """Rebuild the target sign here after fixed-input validation."""
    _ = (sign_input, payload, bootstrap_state)
    return ""
