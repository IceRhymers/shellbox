"""The named-key allowlist. An allowlist, because ``send-keys`` accepts far more than keys."""

from __future__ import annotations

import pytest
from shellbox_mcp.errors import InvalidKey
from shellbox_mcp.keys import ALLOWED_KEYS, validate_key, validate_keys


@pytest.mark.parametrize(
    "key",
    [
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
        "F1",
        "F12",
        "C-c",
        "C-d",
        "M-x",
        "C-Space",
    ],
)
def test_allowed_keys(key: str) -> None:
    assert validate_key(key) == key


@pytest.mark.parametrize(
    ("key", "why"),
    [
        ("enter", "case-sensitive: tmux's own key names are"),
        ("C-C", "modifiers are lower-case"),
        ("F13", "does not exist in the allowlist"),
        ("F0", "no such key"),
        ("C-M-x", "compound modifiers are outside the allowlist"),
        ("Any", "tmux's wildcard key"),
        ("", "empty"),
        ("Enter Enter", "two keys in one argument"),
        (";", "a literal, and one send-keys silently swallows -- text goes via the buffer"),
        ("-n", "would parse as a flag"),
        ("kill-session", "send-keys' key parser accepts constructs that are not keys"),
    ],
)
def test_rejected_keys(key: str, why: str) -> None:
    with pytest.raises(InvalidKey) as excinfo:
        validate_key(key)
    assert excinfo.value.code == "invalid_key"


def test_validate_keys_preserves_order() -> None:
    """Order matters: ``send-keys`` delivers its arguments in order and callers rely on it."""
    assert validate_keys(["C-c", "Enter", "Up"]) == ["C-c", "Enter", "Up"]


def test_validate_keys_empty_is_empty() -> None:
    assert validate_keys(None) == []
    assert validate_keys([]) == []


def test_one_bad_key_rejects_the_whole_sequence() -> None:
    with pytest.raises(InvalidKey):
        validate_keys(["Enter", "NotAKey"])


def test_allowlist_covers_the_documented_set() -> None:
    """§8's list, asserted as a set so a quiet removal fails."""
    expected = (
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
        }
        | {f"F{n}" for n in range(1, 13)}
        | {f"C-{c}" for c in "abcdefghijklmnopqrstuvwxyz"}
        | {f"M-{c}" for c in "abcdefghijklmnopqrstuvwxyz"}
    )
    assert ALLOWED_KEYS == expected
