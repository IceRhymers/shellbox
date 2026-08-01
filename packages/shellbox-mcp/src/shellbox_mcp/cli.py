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

# 🔴 `serve` is imported INSIDE the serve path, not here. It pulls in the MCP SDK and
# pydantic, and `doctor` is precisely the command you run when the server will not
# start -- so a module-level import makes the diagnostic unavailable in exactly the
# situation it exists for. Measured in a real sandbox, where a corrupt cached wheel
# left pydantic uninstalled and `shellbox-mcp doctor` died on an unrelated traceback.

logger = logging.getLogger(__name__)

USAGE = """\
usage: shellbox-mcp [serve | doctor | bootstrap [options]]

  serve      (default) run the stdio MCP server. All configuration comes from the
             environment, so `{"command": "shellbox-mcp", "args": []}` is a complete
             registration.

  doctor     report why this host is or is not working, and exit non-zero if it is
             not. Writes to stderr.

  bootstrap  record what only an outside caller can know, and optionally reset the
             sandbox's baked credential. Run it from outside over ssh:

               databricks sandbox ssh <id> -- shellbox-mcp bootstrap --sandbox-id <id>

             --sandbox-id ID   record which sandbox this is. A sandbox CANNOT learn
                               this itself, so it must be injected. PER-BOOT.
             --gateway-host H  record the sandbox's gateway host.
             --reset-pat       remove the baked creator PAT from every config the SDK
                               reads. Requires --sandbox-id. PER-BOOT.
             --register-codex  add shellbox to ~/.codex/config.toml, preserving the
                               platform's model config. PER-BOOT.

Deferred: enroll -- see the message it prints.
"""

# Named here rather than treated as unknown so the error says "not implemented yet" instead
# of "unknown command", which is the difference between a reader checking the plan and a
# reader checking their spelling.
_DEFERRED = {
    # `identity.py` has landed and `serve` uses it, and `enroll.py` runs E1-E7 automatically
    # on a background thread at every start. What a standalone `enroll` command would add is
    # a way to run it on demand -- useful for diagnosis, not needed for correctness.
    "enroll": "not needed: `serve` runs enrollment automatically. Use `doctor` to inspect it",
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
        raise SystemExit(f"shellbox-mcp {command}: {_DEFERRED[command]}")
    if command == "doctor":
        _doctor(args[1:])
        return
    if command == "bootstrap":
        _bootstrap(args[1:])
        return
    if command != "serve":
        sys.stderr.write(USAGE)
        raise SystemExit(f"shellbox-mcp: unknown command {command!r}")
    if args[1:]:
        sys.stderr.write(USAGE)
        raise SystemExit(f"shellbox-mcp serve: unexpected arguments {args[1:]!r}")

    # The level is re-resolved here only to log the fallback: the handler above is already
    # configured, and this is the call that warns about an unrecognised value.
    log_level_from_env()
    from shellbox_mcp.server import serve

    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        # A misconfigured process must fail here, not answer every tool call with the same
        # error: an operator who typo'd a limit believes they moved a correctness boundary.
        raise SystemExit(f"shellbox-mcp: {exc}") from exc
    serve(settings)


def _doctor(args: list[str]) -> None:
    """Run every check and exit non-zero if any FAILED.

    Output goes to **stderr**, like everything else this package writes: `doctor` is most
    useful on a host that is misbehaving, which is exactly when something may be reading
    this process's stdout as a JSON-RPC stream.
    """
    if args:
        sys.stderr.write(USAGE)
        raise SystemExit(f"shellbox-mcp doctor: unexpected arguments {args!r}")

    from shellbox_mcp.doctor import render, run_checks

    report = run_checks()
    sys.stderr.write(render(report))
    if report.failed:
        raise SystemExit(1)


def _bootstrap(args: list[str]) -> None:
    """Record what only an outside caller knows, and optionally reset the baked PAT.

    ⚠️ **Every operation here is per-boot.** `/etc/lakebox/setup-home-directory.sh` re-points
    the templated `$HOME` paths at every start, and the identity cache is the only thing that
    persists. Running this once per sandbox is not enough; it must run once per boot.
    """
    import argparse

    from shellbox_mcp import boot_templated, identity

    parser = argparse.ArgumentParser(prog="shellbox-mcp bootstrap", add_help=False)
    parser.add_argument("--sandbox-id")
    parser.add_argument("--gateway-host")
    parser.add_argument("--reset-pat", action="store_true")
    parser.add_argument("--register-codex", action="store_true")
    try:
        options = parser.parse_args(args)
    except SystemExit as exc:
        sys.stderr.write(USAGE)
        raise SystemExit("shellbox-mcp bootstrap: bad arguments") from exc

    # ADR-8: an operator must not be able to reset the credential without also saying WHICH
    # sandbox they just did it to. `doctor` reports "sandbox_id is NULL" as "never
    # bootstrapped" -- so a reset-only run produces a host that has lost its credential and
    # cannot be identified in the inventory, which is the worst of both.
    if options.reset_pat and not options.sandbox_id:
        raise SystemExit(
            "shellbox-mcp bootstrap: --reset-pat requires --sandbox-id.\n"
            "A sandbox cannot learn its own id, so resetting without recording it leaves a "
            "host that has lost its credential AND cannot be named in the inventory."
        )
    if not any((options.sandbox_id, options.reset_pat, options.register_codex)):
        sys.stderr.write(USAGE)
        raise SystemExit("shellbox-mcp bootstrap: nothing to do; pass at least one option")

    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        raise SystemExit(f"shellbox-mcp: {exc}") from exc
    settings.ensure_state_dir()

    if options.sandbox_id or options.gateway_host:
        host = identity.resolve_host_id(
            settings.state_dir,
            explicit=settings.host_id_override,
            sandbox_id=options.sandbox_id,
            gateway_host=options.gateway_host,
        )
        logger.info(
            "recorded sandbox_id=%s gateway_host=%s on host %s",
            host.sandbox_id,
            host.gateway_host,
            host.host_id,
        )

    if options.register_codex:
        codex = "~/.codex/config.toml"
        result = boot_templated.replace_templated(codex, boot_templated.codex_mcp_registration())
        logger.info(
            "%s Codex registration at %s (was %s)",
            "wrote" if result.changed else "left unchanged (already registered):",
            codex,
            result.before.value,
        )

    if options.reset_pat:
        try:
            outcome = boot_templated.reset_pat()
        except boot_templated.ResetIncomplete as exc:
            # Never a warning: a reset that reports success while a credential survives is
            # the failure mode this whole path exists to prevent.
            raise SystemExit(f"shellbox-mcp bootstrap: {exc}") from exc
        logger.info(
            "PAT reset across %s; %s",
            ", ".join(str(p) for p in outcome.paths_checked),
            "changed" if outcome.changed else "already credential-less",
        )
        if outcome.overrides:
            logger.info("handled overrides: %s", outcome.overrides)


if __name__ == "__main__":
    main()
