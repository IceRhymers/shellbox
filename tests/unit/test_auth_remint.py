"""T-AUTH-REMINT -- one re-mint per auth failure, and the second one is terminal.

Apps OAuth expires in about an hour. A publisher outlives that, so the *first* ``auth_failed``
on a long-running attach is the routine case -- the token aged out -- and treating it as fatal
would kill a healthy session once an hour.

But an auth failure that survives a **freshly minted** token is not an expiry. The credential
chain is broken, and no number of dials will mint a working token out of it. Retrying there is
`R26`: an endless loop that logs like network trouble while the browser shows a blank terminal
and nobody is told that ``databricks auth login`` is the remedy.

So the rule has to distinguish those two, and it does it by counting consecutive failures
across a mint rather than by counting failures:

* ``auth_failed`` -> re-mint, retry once.
* ``auth_failed`` again, with the fresh token -> **terminal**, naming the remedy.
* a dial that reached ``hello`` -> the budget resets, because a token that just worked is proof
  the chain is intact and an expiry an hour from now deserves its own re-mint.

That last clause is the one an implementation drops. Without it a publisher gets exactly one
re-mint per process lifetime, so the second hourly expiry -- hours after the first, with every
dial in between succeeding -- is reported as a broken credential chain.
"""

from __future__ import annotations

import uuid

import anyio
import pytest
from shellbox_mcp.transport import (
    Failure,
    TransportTerminal,
    WSTransport,
    WSTransportConfig,
)
from websockets.datastructures import Headers
from websockets.exceptions import InvalidStatus
from websockets.http11 import Response
from wsfakes import SESSION_ID, FakeConnection, RecordingSleep, ScriptedDial, hello_bytes

EPOCH = str(uuid.uuid4())


def auth_failure() -> InvalidStatus:
    """The measured shape: an HTML login page, which is an auth failure at any status."""
    return InvalidStatus(
        Response(200, "", Headers({"Content-Type": "text/html"}), b"<!doctype html><html>login")
    )


def config(**overrides: object) -> WSTransportConfig:
    base: dict[str, object] = {
        "url": "wss://app.example/publish",
        "session_id": SESSION_ID,
        "epoch": EPOCH,
        "backoff_floor": 0.0,
        "backoff_cap": 0.0,
    }
    base.update(overrides)
    return WSTransportConfig(**base)  # type: ignore[arg-type]


class CountingMint:
    """A token source that returns a different token each call, and counts them.

    Distinct values matter: they are what proves the second dial carried a token the first did
    not, which is the whole content of "re-mint".
    """

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> str:
        self.calls += 1
        return f"token-{self.calls}"


def bearers(dial: ScriptedDial) -> list[str | None]:
    """The ``Authorization`` header each dial carried, in order."""
    out: list[str | None] = []
    for _url, kwargs in dial.calls:
        headers = kwargs.get("additional_headers")
        out.append(None if headers is None else headers["Authorization"])  # type: ignore[index]
    return out


async def drain(transport: WSTransport, take: int = 1) -> int:
    """Pull ``take`` connections out of the loop and report how many arrived."""
    stream = transport.connect_forever()
    got = 0
    try:
        while got < take:
            await stream.__anext__()
            got += 1
    finally:
        await stream.aclose()
    return got


# --------------------------------------------------------------------------------------
# The routine case: a token aged out
# --------------------------------------------------------------------------------------


def test_one_auth_failure_re_mints_and_retries() -> None:
    """T-AUTH-REMINT. The hourly expiry, which must not be fatal."""
    mint = CountingMint()
    dial = ScriptedDial([auth_failure(), FakeConnection([hello_bytes(SESSION_ID, EPOCH)])])
    transport = WSTransport(config(), mint_token=mint, dial=dial, sleep=RecordingSleep())

    assert anyio.run(drain, transport) == 1
    assert mint.calls == 2, "one mint per dial: the retry must not reuse the token that failed"
    assert bearers(dial) == ["Bearer token-1", "Bearer token-2"]


def test_the_token_is_minted_on_every_dial_rather_than_cached() -> None:
    """T-AUTH-REMINT. Not only on the auth path.

    Apps OAuth expires in about an hour and this loop re-dials every 10 to 18 minutes, so a
    cached token would eventually be presented after expiry -- turning a routine reconnect into
    an auth failure that then spends the re-mint budget it should never have needed.
    """
    mint = CountingMint()
    dial = ScriptedDial(
        [
            FakeConnection([hello_bytes(SESSION_ID, EPOCH)]),
            FakeConnection([hello_bytes(SESSION_ID, EPOCH)]),
        ]
    )
    transport = WSTransport(config(), mint_token=mint, dial=dial, sleep=RecordingSleep())

    assert anyio.run(drain, transport, 2) == 2
    assert bearers(dial) == ["Bearer token-1", "Bearer token-2"], "two dials, two fresh tokens"


def test_no_authorization_header_is_sent_when_no_minter_is_injected() -> None:
    """T-AUTH-REMINT. The loopback lane: no credential chain, and none needed.

    Minting is an injected callable so that nothing in this module imports the Databricks SDK.
    The integration lane relies on the absence being clean rather than an empty bearer.
    """
    dial = ScriptedDial([FakeConnection([hello_bytes(SESSION_ID, EPOCH)])])

    anyio.run(drain, WSTransport(config(), dial=dial))

    assert bearers(dial) == [None]


# --------------------------------------------------------------------------------------
# The broken case: a fresh token did not help
# --------------------------------------------------------------------------------------


def test_a_second_consecutive_auth_failure_is_terminal_and_names_the_remedy() -> None:
    """T-AUTH-REMINT. The token was not the problem, so the remedy is a person.

    The message carries the two commands rather than a status code, because the operator
    reading it is looking at a blank terminal and needs the next action, not a diagnosis.
    """
    mint = CountingMint()
    dial = ScriptedDial([auth_failure(), auth_failure(), auth_failure()])
    transport = WSTransport(config(), mint_token=mint, dial=dial, sleep=RecordingSleep())

    with pytest.raises(TransportTerminal) as caught:
        anyio.run(drain, transport)

    assert caught.value.failure is Failure.AUTH_FAILED
    assert "shellbox-mcp doctor" in str(caught.value)
    assert "databricks auth login" in str(caught.value)
    assert len(dial.calls) == 2, "it must stop at the second failure, not keep dialing"


def test_the_re_mint_budget_is_configurable_and_zero_makes_the_first_failure_terminal() -> None:
    """T-AUTH-REMINT. ``max_auth_remints=0`` for a caller that mints nothing to re-mint."""
    dial = ScriptedDial([auth_failure(), auth_failure()])
    transport = WSTransport(
        config(max_auth_remints=0), dial=dial, sleep=RecordingSleep()
    )

    with pytest.raises(TransportTerminal):
        anyio.run(drain, transport)

    assert len(dial.calls) == 1


# --------------------------------------------------------------------------------------
# The clause an implementation drops
# --------------------------------------------------------------------------------------


def test_a_successful_connection_resets_the_budget() -> None:
    """T-AUTH-REMINT. The clause that makes this survive a long-lived publisher.

    Without it, a publisher gets one re-mint for its whole process lifetime. The second hourly
    expiry -- hours later, with every dial in between succeeding -- would then be reported as a
    broken credential chain and would stop the publisher for a condition that is routine.

    The script here is exactly that history: fail, mint, connect, and an hour later fail again.
    The last failure must be re-minted, not fatal.
    """
    mint = CountingMint()
    dial = ScriptedDial(
        [
            auth_failure(),
            FakeConnection([hello_bytes(SESSION_ID, EPOCH)]),
            auth_failure(),
            FakeConnection([hello_bytes(SESSION_ID, EPOCH)]),
        ]
    )
    transport = WSTransport(config(), mint_token=mint, dial=dial, sleep=RecordingSleep())

    assert anyio.run(drain, transport, 2) == 2, (
        "the second expiry must get its own re-mint; a token that worked proved the chain intact"
    )
    assert mint.calls == 4


def test_a_transient_failure_does_not_consume_the_auth_budget() -> None:
    """T-AUTH-REMINT. Only ``auth_failed`` counts against the budget.

    The edge kill is transient and arrives every 10 to 18 minutes forever. If those decremented
    an auth budget, every publisher would eventually die of "authentication failed" having
    never seen an authentication failure.
    """
    mint = CountingMint()
    dial = ScriptedDial(
        [
            OSError("connection refused"),
            OSError("connection refused"),
            OSError("connection refused"),
            auth_failure(),
            FakeConnection([hello_bytes(SESSION_ID, EPOCH)]),
        ]
    )
    transport = WSTransport(config(), mint_token=mint, dial=dial, sleep=RecordingSleep())

    assert anyio.run(drain, transport) == 1
    assert len(dial.calls) == 5


def test_a_terminal_failure_is_raised_rather_than_classified() -> None:
    """T-AUTH-REMINT. ``TransportTerminal`` from inside a dial must not be re-classified.

    A ``hello`` naming another session raises ``TransportTerminal`` inside ``_dial_once``. The
    loop's ``except Exception`` would otherwise catch it, classify it as transient, and retry
    the one failure that must never be retried -- so the re-raise is explicit and this pins it.
    """
    dial = ScriptedDial([FakeConnection([hello_bytes("another-session", EPOCH)])])
    transport = WSTransport(config(), dial=dial, sleep=RecordingSleep())

    with pytest.raises(TransportTerminal) as caught:
        anyio.run(drain, transport)

    assert caught.value.failure is Failure.PROTOCOL


def test_a_malformed_url_is_terminal_before_anything_is_dialed_twice() -> None:
    """T-AUTH-REMINT. ``CONFIG`` stops the loop: retrying a typo cannot fix it."""
    from websockets.exceptions import InvalidURI

    dial = ScriptedDial([InvalidURI("https://app.example", "scheme isn't ws or wss")])
    transport = WSTransport(config(), dial=dial, sleep=RecordingSleep())

    with pytest.raises(TransportTerminal) as caught:
        anyio.run(drain, transport)

    assert caught.value.failure is Failure.CONFIG
    assert len(dial.calls) == 1
