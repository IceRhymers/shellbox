"""Entrypoint for the ``shellbox-mcp`` console script.

Logging MUST be configured to stderr before any other import in this module. stdout is
the MCP JSON-RPC transport (see .omc/plans/phase-2-session-plane.md §6): a stray
``print()`` or a stdout-attached logging handler corrupts the protocol stream. Enforced
by ruff's T20 rule over this package (see packages/shellbox-mcp/pyproject.toml).

``serve`` is the default subcommand and every setting comes from the environment (§5), so
``{"command": "shellbox-mcp", "args": []}`` is a complete registration. That is a hard
constraint, not a convenience: ``buzz-acp`` spawns MCP servers with ``args: vec![]`` (§4,
#6), so a design needing a flag could never be used there.
"""

import logging
import os
import sys

# `SHELLBOX_LOG_LEVEL` is read straight from `os.environ` rather than through `config.py`,
# because this must run before `config` (or anything else that might log) is imported, and
# because an unrecognised value must not prevent the process from starting -- stderr is the
# only diagnostic channel, and refusing to start is a worse answer than starting at INFO.
# `config.log_level_from_env` is the same resolution for every other caller;
# `tests/integration/test_cli_entrypoint.py` asserts an unrecognised value warns and still
# serves, and `tests/integration/test_stdout_protocol.py` asserts DEBUG output lands on stderr.
_LEVEL = os.environ.get("SHELLBOX_LOG_LEVEL", "").strip().upper()
logging.basicConfig(
    level=logging.getLevelNamesMapping().get(_LEVEL, logging.INFO),
    stream=sys.stderr,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

from shellbox_mcp.config import ConfigError, Settings, log_level_from_env  # noqa: E402
from shellbox_mcp.server import serve  # noqa: E402

logger = logging.getLogger(__name__)

USAGE = """\
usage: shellbox-mcp [serve]

  serve   (default) run the stdio MCP server; all configuration comes from the
          environment -- see §5 of .omc/plans/phase-2-session-plane.md.

Deferred subcommands: enroll, bootstrap (W7/W8) and doctor (W8).
"""

# Named here rather than treated as unknown so the error says "not implemented yet" instead
# of "unknown command", which is the difference between a reader checking the plan and a
# reader checking their spelling.
_DEFERRED = {
    "enroll": "W7 (identity.py/enroll.py)",
    "bootstrap": "W8 (databrickscfg.py)",
    "doctor": "W8 (doctor)",
}


def main(argv: list[str] | None = None) -> None:
    """Console-script entrypoint. Dispatches to ``serve`` by default.

    Writes diagnostics to **stderr** and never to stdout: by the time an unknown argument
    is reported, a client may already be waiting for a JSON-RPC handshake on stdout, and a
    usage message written there is indistinguishable from a corrupt protocol stream.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in {"-h", "--help", "help"}:
        sys.stderr.write(USAGE)
        return
    command = args[0] if args else "serve"
    if command in _DEFERRED:
        raise SystemExit(f"shellbox-mcp {command}: not implemented yet -- see {_DEFERRED[command]}")
    if command != "serve":
        sys.stderr.write(USAGE)
        raise SystemExit(f"shellbox-mcp: unknown command {command!r}")
    if args[1:]:
        sys.stderr.write(USAGE)
        raise SystemExit(f"shellbox-mcp serve: unexpected arguments {args[1:]!r}")

    # The level is re-resolved here only to log the fallback: the handler above is already
    # configured, and this is the call that warns about an unrecognised value.
    log_level_from_env()
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        # A misconfigured process must fail here, not answer every tool call with the same
        # error: an operator who typo'd a limit believes they moved a correctness boundary.
        raise SystemExit(f"shellbox-mcp: {exc}") from exc
    serve(settings)


if __name__ == "__main__":
    main()
