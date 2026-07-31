"""Raw-tmux targeting regression -- the OTHER half of §7.1's two-level assertion.

These tests deliberately use the target forms the adapter forbids, straight against tmux.
That is the point: they document *why* ``=<name>:`` is the only form, and they fail loudly if
a future tmux changes any of it. They must never be "fixed" by routing through ``target()``.

The single-sentence version of this criterion is unsatisfiable, which is worth stating so
nobody tries to unify the two levels again: at the **tmux** level ``resize-window -t '=bui'``
returns rc=0 and resizes ``build`` -- precisely why the form is banned -- while at the
**adapter** level a caller-supplied ``=bui`` is ``invalid_name`` and never reaches tmux. The
only way to make one assertion cover both is to weaken it.

Adapter-level assertions live in ``test_tmux_adapter.py``.
"""

from __future__ import annotations

import pytest
from conftest import TmuxServer, requires_tmux

pytestmark = requires_tmux

# The six targeting verbs, with the extra arguments each needs.
TARGETING_VERBS: tuple[tuple[str, list[str]], ...] = (
    ("has-session", []),
    ("kill-session", []),
    ("resize-window", ["-x", "100", "-y", "30"]),
    ("capture-pane", ["-p"]),
    ("send-keys", ["-l", "x"]),
    ("set-option", ["@k", "v"]),
)


@pytest.fixture
def two_sessions(tmux_server: TmuxServer) -> TmuxServer:
    """A server holding exactly ``build`` and ``envtest`` -- the measurement setup."""
    tmux_server.raw("new-session", "-d", "-s", "build", "-x", "80", "-y", "24", "sh")
    tmux_server.raw("new-session", "-d", "-s", "envtest", "-x", "80", "-y", "24", "sh")
    assert sorted(tmux_server.sessions()) == ["build", "envtest"]
    return tmux_server


def test_an_unanchored_prefix_reaches_a_session_the_caller_did_not_name(
    two_sessions: TmuxServer,
) -> None:
    """``-t bui`` resolves to ``build``: exact -> prefix -> fnmatch. R11, and it is a
    security boundary, not a nicety -- under default-open access one agent reaches another's
    session by naming a prefix of it."""
    assert two_sessions.raw("has-session", "-t", "bui").rc == 0
    assert two_sessions.raw("has-session", "-t", "env*").rc == 0, "fnmatch too"
    assert two_sessions.raw("kill-session", "-t", "bui").rc == 0
    assert two_sessions.sessions() == ["envtest"], "`bui` killed `build`"


def test_the_half_anchored_form_is_accepted_by_resize_window(two_sessions: TmuxServer) -> None:
    """🔴 ``resize-window -t '=bui'`` IS accepted (rc=0) and resizes ``build``.

    This is the assertion that makes ``=<name>`` unusable, and the one an earlier revision
    generalised away after measuring only ``has-session`` and ``kill-session``. If a future
    tmux stops doing this, this test fails and the rule can be revisited on evidence.
    """
    result = two_sessions.raw("resize-window", "-t", "=bui", "-x", "100", "-y", "30")
    assert result.rc == 0, f"tmux behaviour changed: {result.stderr!r}"
    sizes = two_sessions.raw(
        "list-sessions", "-F", "#{session_name}\t#{window_width}x#{window_height}"
    ).stdout_raw
    assert "build\t100x30" in sizes, "`=bui` reached `build` anyway"


def test_the_half_anchored_form_is_rejected_by_the_pane_verbs(two_sessions: TmuxServer) -> None:
    """And it is the worst of both worlds: it also REJECTS a session that exists.

    So ``=<name>`` fails to protect the one verb it needs to, and breaks the three it does
    not.
    """
    for verb, extra in (
        ("capture-pane", ["-p"]),
        ("send-keys", ["-l", "x"]),
        ("set-option", ["@k", "v"]),
    ):
        assert two_sessions.raw(verb, "-t", "=build", *extra).rc != 0, verb


@pytest.mark.parametrize(("verb", "extra"), TARGETING_VERBS)
def test_the_anchored_form_rejects_a_nonexistent_session(
    two_sessions: TmuxServer, verb: str, extra: list[str]
) -> None:
    """``=bui:`` is the control: no session ``bui`` exists, so every verb must refuse it.

    ``display-message`` is excluded from this list on purpose -- it returns rc=0 for a
    nonexistent target, which is asserted separately.
    """
    result = two_sessions.raw(verb, "-t", "=bui:", *extra)
    assert result.rc != 0, f"{verb} accepted a target that does not exist"
    assert sorted(two_sessions.sessions()) == ["build", "envtest"]


@pytest.mark.parametrize(("verb", "extra"), TARGETING_VERBS)
def test_the_anchored_form_accepts_the_session_it_names(
    two_sessions: TmuxServer, verb: str, extra: list[str]
) -> None:
    """``=build:`` -- correct for all six, which is what makes it the ONE form."""
    result = two_sessions.raw(verb, "-t", "=build:", *extra)
    assert result.rc == 0, f"{verb} rejected `=build:`: {result.stderr!r}"


def test_new_session_dash_s_takes_a_name_and_anchoring_it_is_a_category_error(
    tmux_server: TmuxServer,
) -> None:
    """``new-session -s '=build'`` succeeds and creates a session NAMED ``=build``.

    After which ``has-session -t '=build'`` returns rc=1 -- the session is unreachable
    through the adapter's own target helper. Which is why ``new_session_name()`` exists and
    returns the bare name.
    """
    created = tmux_server.raw("new-session", "-d", "-s", "=build", "-x", "80", "-y", "24", "sh")
    assert created.rc == 0
    assert tmux_server.sessions() == ["=build"]
    assert tmux_server.raw("has-session", "-t", "=build").rc != 0
    assert tmux_server.raw("has-session", "-t", "=build:").rc != 0
