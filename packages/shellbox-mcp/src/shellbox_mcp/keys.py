"""The named-key allowlist for ``shell_send(keys=...)`` (§8).

``send-keys`` *without* ``-l`` interprets its argument as a key NAME, and tmux's key parser
accepts constructs well outside any user's mental model of "a key" -- including things that
resolve to internal commands. So this is an allowlist, never a denylist: anything not
listed is ``invalid_key``.

Literal text never comes through here. It goes through the buffer path
(``load-buffer``/``paste-buffer``), because ``send-keys -l`` silently swallows a bare ``;``
(H1: rc=0, character never arrives, and ``--`` does not help).
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from shellbox_mcp.errors import InvalidKey

__all__ = ["ALLOWED_KEYS", "validate_key", "validate_keys"]

ALLOWED_KEYS: frozenset[str] = frozenset(
    {
        "Enter",
        "Escape",
        "Tab",
        "Space",
        "BSpace",
        "Up",
        "Down",
        "Left",
        "Right",
        "Home",
        "End",
        "PageUp",
        "PageDown",
        "C-Space",
        *(f"F{n}" for n in range(1, 13)),
        *(f"C-{c}" for c in "abcdefghijklmnopqrstuvwxyz"),
        *(f"M-{c}" for c in "abcdefghijklmnopqrstuvwxyz"),
    }
)

# Kept for error messages only -- membership is decided by ALLOWED_KEYS, so a future
# widening of this pattern cannot widen what is accepted.
_SHAPE_HINT = re.compile(r"^(C|M)-[a-z]$")


def validate_key(key: str) -> str:
    """Return ``key`` if it is in the allowlist, else raise ``invalid_key``."""
    if not isinstance(key, str) or key not in ALLOWED_KEYS:
        hint = ""
        if isinstance(key, str) and _SHAPE_HINT.match(key.lower()) and key not in ALLOWED_KEYS:
            hint = " (modifier keys are lower-case: use 'C-c', not 'C-C')"
        raise InvalidKey(f"key {key!r} is not in the allowlist{hint}")
    return key


def validate_keys(keys: Sequence[str] | None) -> list[str]:
    """Validate a sequence of key names, preserving order.

    Order is preserved because ``send-keys`` delivers its arguments in order and callers
    depend on it (M18: text-then-keys ordering holds).
    """
    if not keys:
        return []
    return [validate_key(k) for k in keys]
