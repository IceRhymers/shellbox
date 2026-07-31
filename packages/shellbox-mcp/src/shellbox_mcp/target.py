"""The ONE safe tmux target form (plan §7.1; spike F2/F3, both lanes).

tmux resolves a target-session **exact -> prefix -> fnmatch**, so with only ``build``
present, ``has-session -t bui`` returns rc=0 and ``kill-session -t bui`` kills ``build``.
Under default-open access that is a cross-agent addressing vulnerability (R11).

The spike measured 7 verbs x 5 forms in both lanes and the matrix collapses to ONE safe
form, ``=<name>:``:

* ``<name>``  — prefix- and fnmatch-matches. Unsafe everywhere.
* ``=<name>`` — the WORST option: ``resize-window -t '=bui'`` still resolves to ``build``
  (rc=0, resizes it), while ``capture-pane``/``send-keys``/``set-option`` reject a valid
  ``=build`` outright.
* ``=<name>:`` — correct for all six targeting verbs, and the only form that rejects the
  ``=bui:`` control (a session that does not exist) everywhere.

``new-session -s`` is the one exception, and it is a category error to anchor it: ``-s``
takes a **name**, not a target. Measured — ``new-session -d -s '=build'`` succeeds and
creates a session literally *named* ``=build``, after which ``has-session -t '=build'``
returns rc=1, i.e. unreachable through this module's own helper.

These are the only two functions this module exposes, and ``tmux.py`` may not construct a
``-t`` value any other way. ``tests/unit/test_target.py`` asserts that mechanically over
``tmux.py``'s AST.
"""

from __future__ import annotations

__all__ = ["new_session_name", "target"]


def target(name: str) -> str:
    """Anchored target for EVERY targeting verb, without exception.

    ``has-session``, ``kill-session``, ``resize-window``, ``capture-pane``, ``send-keys``,
    ``paste-buffer``, ``set-option`` and ``display-message`` all take this and only this.
    """
    return f"={name}:"


def new_session_name(name: str) -> str:
    """The bare session name, for ``new-session -s`` ONLY -- never anchored.

    Identity by construction, so the call site reads as a deliberate choice rather than an
    omission, and so the AST check in ``tests/unit/test_target.py`` can tell "the author
    meant a bare name here" apart from "the author forgot to anchor a target".
    """
    return name
