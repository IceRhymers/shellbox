"""Read the ``export const`` declarations out of a JavaScript module, without a JS runtime.

ADR-23 keeps every JavaScript toolchain out of this repo, so the browser half's constants are
compared to the Python half's by PARSING rather than by executing. That buys one specific thing
and no more: a deadline, a wire field name or a message kind cannot move on one side alone. It
buys nothing at all about the two implementations' logic, and `test_client_parity.py` says so
where a reader will see it.

The parser is deliberately narrow. It reads exactly the form both browser modules are written
in -- one declaration per line, a number or a single-quoted-free string literal or a reference
to another constant in the same file -- and it reports what it could not read rather than
skipping it silently. A parser that quietly returned ``{}`` would make every test built on it
pass while asserting nothing, which is the vacuity this repo checks for by name.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

__all__ = ["MIN_CONSTANTS", "js_constants"]

# A floor under how many declarations a real module yields. Both files declare well over this,
# and the number exists so that a regex broken by an edit to the JavaScript's formatting fails
# here instead of turning every comparison into a no-op over an empty dict.
MIN_CONSTANTS = 10

_DECLARATION = re.compile(r"^export const ([A-Z][A-Z0-9_]*) = (.+);$", re.MULTILINE)


def js_constants(path: Path, known: dict[str, object] | None = None) -> dict[str, object]:
    """Every ``export const NAME = <literal>;`` in ``path``, as Python values.

    Numbers arrive as ``float`` and strings as ``str``.

    A declaration whose value NAMES another constant resolves to that constant's value, looked
    up first among the ones already read from this file and then among ``known`` -- which is how
    a caller supplies what the module IMPORTS. The one form in use is
    ``CODE_TERMINAL_GONE = CLOSED_TERMINAL_GONE`` in `static/protocol.js`, whose right-hand side
    comes from `static/codec.js`. Resolving it is what makes the alias comparable instead of
    unreadable, and keeping it an alias in the JavaScript is what stops the browser's two files
    from drifting apart on a value they must share.

    Raises ``ValueError`` on a declaration it cannot read, and on a file yielding fewer than
    `MIN_CONSTANTS`. Both are failures of this parser rather than of the module it read, and
    both must be loud: a silent skip is indistinguishable from agreement.
    """
    source = path.read_text(encoding="utf-8")
    imported = {} if known is None else known
    found: dict[str, object] = {}
    for name, literal in _DECLARATION.findall(source):
        raw = literal.strip()
        if raw in found:
            found[name] = found[raw]
            continue
        if raw in imported:
            found[name] = imported[raw]
            continue
        try:
            # JSON covers both forms the modules use -- a double-quoted string and a decimal
            # number -- and it rejects everything else, which is the behaviour wanted here.
            found[name] = json.loads(raw)
        except ValueError as exc:
            raise ValueError(
                f"{path.name} declares {name} as {raw!r}, which this parser cannot read. "
                "Keep browser constants to a plain string, a plain number, or the name of a "
                "constant declared above -- see tests/unit/jsconst.py."
            ) from exc

    if len(found) < MIN_CONSTANTS:
        raise ValueError(
            f"{path.name} yielded only {len(found)} constants, under the floor of "
            f"{MIN_CONSTANTS}. The declaration form probably changed, and every parity "
            "assertion built on this would now be comparing an empty dict."
        )
    return found
