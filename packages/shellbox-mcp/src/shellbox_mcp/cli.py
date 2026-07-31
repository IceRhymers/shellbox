"""Entrypoint for the ``shellbox-mcp`` console script.

Logging MUST be configured to stderr before any other import in this module. stdout is
the MCP JSON-RPC transport (see .omc/plans/phase-2-session-plane.md §6): a stray
``print()`` or a stdout-attached logging handler corrupts the protocol stream. Enforced
by ruff's T20 rule over this package (see packages/shellbox-mcp/pyproject.toml).
"""

import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

# PLACEHOLDER (W4): wire up the `serve` (default) | `enroll` | `bootstrap` | `doctor`
# subcommands, reading SHELLBOX_LOG_LEVEL (see config.py, §5) instead of the hardcoded
# INFO above, and dispatch into server.py's MCP tool definitions. W1 only establishes the
# package skeleton and the stderr-before-any-other-import discipline this module depends
# on; everything below is intentionally unimplemented.


def main() -> None:
    """Console-script entrypoint; dispatches to `serve` by default (implemented in W4)."""
    raise NotImplementedError("shellbox-mcp CLI is implemented in W4")


if __name__ == "__main__":
    main()

