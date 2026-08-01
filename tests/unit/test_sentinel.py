"""The sentinel helper (W13) — a test for the thing the other tests rely on.

`conftest.Sentinel` exists to make one property **structural** instead of re-derived in a
prose comment at each call site: *a sentinel must not be a substring of the command that
produces it*. A pty echoes whatever is pasted, so a needle visible in the command line is
found on the pane whether or not the shell ever ran it — and a test built that way passes
against a session that is listed but dead, or one where `Enter` was never delivered.

It has been got wrong twice for real in this suite, which is why it is now a constructor
invariant with its own tests rather than a convention.
"""

from __future__ import annotations

import re

import pytest
from conftest import Sentinel, counted_lines, sentinel


def test_the_token_never_appears_in_the_command_that_produces_it() -> None:
    """The whole property, for the ordinary case."""
    token = sentinel("LIFE")
    assert token.awaited not in token.typed
    assert token.awaited not in token.echo()


def test_a_violating_pair_cannot_be_constructed() -> None:
    """🔴 The point of the class. A convention can be forgotten; a constructor cannot.

    The message has to explain the consequence, not just the rule — someone hitting this is
    about to "fix" it by loosening the assertion.
    """
    with pytest.raises(AssertionError, match="satisfied by the pty's echo"):
        Sentinel(awaited="KEYS-OK", typed="echo KEYS-OK")


def test_the_shell_resolves_the_typed_form_back_to_the_token() -> None:
    """The split has to be *removable by the shell*, or the sentinel never appears at all.

    Asserted against a real shell rather than by reasoning about quoting: `''` is only an
    empty string because POSIX word-splitting says so, and that is exactly the kind of claim
    worth checking rather than believing.
    """
    import subprocess

    token = sentinel("ROUNDTRIP")
    result = subprocess.run(
        ["sh", "-c", token.echo(newline=False)], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == token.awaited


def test_each_sentinel_is_unique() -> None:
    """A nonce, so a poll cannot be satisfied by output an earlier send left in scrollback.

    Panes are reused and `history-limit` is 20000, so a stale match is a live hazard: a
    fixed token would let a test pass on the *previous* test's output.
    """
    tokens = {sentinel("SAME").awaited for _ in range(50)}
    assert len(tokens) == 50


def test_the_label_is_preserved_so_failures_are_readable() -> None:
    """A failure message full of hex is a failure message nobody reads."""
    assert sentinel("KEYS").awaited.startswith("KEYS-")


def test_echo_controls_its_own_newline() -> None:
    """`send(text=..., keys=["Enter"])` must NOT carry a trailing newline — otherwise the
    shell runs the command before `Enter` is delivered and the ordering the test exists to
    prove is unobservable."""
    token = sentinel("KEYS")
    assert token.echo().endswith("\n")
    assert not token.echo(newline=False).endswith("\n")


# ------------------------------------------------------------------- counted_lines
def test_counted_lines_puts_only_the_last_line_in_the_token() -> None:
    """For scrollback assertions. Awaiting `"200"` against `seq 1 200` matched the echoed
    command — it passed on macOS by luck of timing and failed on Linux with
    `scrollback_lines == 0`. The token is `L200`, which the command text cannot contain."""
    lines = counted_lines(200)
    assert lines.awaited == "L200"
    assert "L200" not in lines.typed
    assert "%s" in lines.typed, "the token must come from a format expansion, not a literal"


def test_counted_lines_really_emits_that_many_lines_ending_in_the_token() -> None:
    """Against a real shell, because the value of this helper is entirely in what `printf`
    and `seq` actually do with it."""
    import subprocess

    lines = counted_lines(20)
    result = subprocess.run(["sh", "-c", lines.typed], capture_output=True, text=True, check=True)
    emitted = result.stdout.split("\n")
    emitted = [line for line in emitted if line]
    assert len(emitted) == 20
    assert emitted[-1] == lines.awaited
    assert emitted[0] == "L1"


def test_no_migrated_site_still_hardcodes_a_bare_sentinel() -> None:
    """A regression guard on the migration itself.

    The old spellings (`echo LIFE''-OK`, `emit_200_lines`) are gone; this fails if one comes
    back, because the next person to add a sentinel by hand will copy whatever they find.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    offenders = []
    for path in (root / "integration").glob("test_*.py"):
        body = path.read_text()
        # The literal two-quote split, written by hand rather than produced by the helper.
        if re.search(r"echo [A-Z]+(''|\"\")-?[A-Z]", body):
            offenders.append(path.name)
    assert not offenders, (
        f"{offenders} hand-roll the sentinel split; use `conftest.sentinel` so the "
        "invariant is enforced rather than remembered"
    )
