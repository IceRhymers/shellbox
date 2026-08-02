"""T-AUTH-CONTENT -- probe constraint 6: validate response CONTENT, never status alone.

The measurement this file exists for: **an unauthenticated request to the Databricks Apps edge
returned HTTP 200 with an HTML login body** (`probe/FINDINGS.md`). A status-only check reads
that as success. The publisher then streams a pty into a login page and reports that it is
connected, which is `R26` -- an endless reconnect loop that logs like network trouble while the
browser shows a blank terminal.

So the rule ``classify_failure`` implements is that the body decides before the status does,
and the expensive mistake has a direction: calling an auth failure transient loops forever,
while calling a transient failure auth-related costs one re-mint and one retry. The
classification is biased accordingly, and these tests pin that bias.

WARNING: The HTML detection is deliberately NOT a search for words like "sign in" or
"password". That is a guess about one login page's copy, and it would break silently the day
Databricks rewords it -- silently because the failure mode is a *missed* auth failure, which
presents as the reconnect loop above rather than as a test going red.
"""

from __future__ import annotations

import pytest
from shellbox_mcp.transport import Failure, _looks_like_html, _NoRedirect, classify_failure
from shellbox_transport.codec import CodecError
from websockets.datastructures import Headers
from websockets.exceptions import (
    ConnectionClosedError,
    InvalidMessage,
    InvalidStatus,
    InvalidURI,
)
from websockets.http11 import Response

_LOGIN_PAGE = b"<!doctype html><html><body>Sign in to Databricks</body></html>"


def rejected(status: int, *, content_type: str | None = None, body: bytes = b"") -> InvalidStatus:
    """The shape ``websockets`` raises when a server refuses the upgrade."""
    headers = Headers() if content_type is None else Headers({"Content-Type": content_type})
    return InvalidStatus(Response(status, "", headers, body))


# --------------------------------------------------------------------------------------
# The measured case, and the reason this file is not a status table
# --------------------------------------------------------------------------------------


def test_a_200_carrying_an_html_login_page_is_an_auth_failure() -> None:
    """T-AUTH-CONTENT. THE measured case (probe constraint 6).

    A 200 is the success status. Reading it as success here is the whole failure, because the
    body is a login page and the socket is not the App.
    """
    got = classify_failure(rejected(200, content_type="text/html; charset=utf-8", body=_LOGIN_PAGE))

    assert got is Failure.AUTH_FAILED


def test_an_html_body_decides_at_every_status_outside_the_gateway_four() -> None:
    """T-AUTH-CONTENT. Not a 200 special case: a login page is a login page.

    Pinned across a spread of statuses so that an edge answering 418 with the same page is
    classified by what it *sent*, not by what it labelled it. The four gateway codes are the
    documented exception and the next test covers them.
    """
    for status in (200, 201, 302, 403, 418, 451):
        assert classify_failure(rejected(status, content_type="text/html", body=_LOGIN_PAGE)) is (
            Failure.AUTH_FAILED
        ), f"an HTML login page under {status} is still an auth failure"


def test_a_gateway_status_outranks_an_html_body() -> None:
    """T-AUTH-CONTENT. The precedence, which runs the opposite way to the rest of this file.

    A 502 or a 503 from a proxy almost always carries an HTML error page, and that page is a
    proxy's, not a login form. Letting the body decide there would classify the single most
    ordinary transient failure on this path as an auth failure, spend a token re-mint on it,
    and -- if it recurred once more -- declare a passing outage terminal.

    So the four gateway codes are checked first and win. Everywhere else the body wins, which
    is what constraint 6 asks for: those four are the only statuses that carry a reliable
    "try again" meaning of their own.
    """
    for status in (500, 502, 503, 504):
        assert classify_failure(
            rejected(status, content_type="text/html", body=b"<html>bad gateway")
        ) is Failure.TRANSIENT, f"{status} means try again, whatever body the proxy attached"


# --------------------------------------------------------------------------------------
# The status rules, for bodies that are not HTML
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_the_four_gateway_statuses_are_transient(status: int) -> None:
    """T-AUTH-CONTENT. The retryable set is the four ``websockets`` itself considers retryable.

    Kept to four rather than "5xx" on purpose: a 501 or a 505 is the server saying it will
    never do this, which retrying cannot change.
    """
    assert classify_failure(rejected(status)) is Failure.TRANSIENT


@pytest.mark.parametrize("status", [300, 301, 302, 303, 307, 308])
def test_a_redirect_is_an_auth_failure_and_is_never_followed(status: int) -> None:
    """T-AUTH-CONTENT. The probe measured a 302 on an unauthenticated upgrade.

    A redirect on a WebSocket upgrade is the login flow, not routing.
    """
    assert classify_failure(rejected(status)) is Failure.AUTH_FAILED


@pytest.mark.parametrize("status", [200, 401, 403])
def test_a_200_401_or_403_is_an_auth_failure_even_with_no_body(status: int) -> None:
    """T-AUTH-CONTENT. A 200 on an *upgrade* is never success -- the success status is 101.

    A server that answers 200 answered as though this were an ordinary HTTP request, which
    means something that is not the App terminated it.
    """
    assert classify_failure(rejected(status)) is Failure.AUTH_FAILED


def test_an_unrecognised_status_is_a_protocol_failure_and_is_still_retried() -> None:
    """T-AUTH-CONTENT. A 404 on ``$SHELLBOX_APP_URL`` is a wrong path, not network trouble.

    The classification is what a log line names, and naming this ``transient`` would send an
    operator hunting a network problem that does not exist.

    It is still retried, and that is deliberate. ``connect_forever`` stops only on ``CONFIG``
    and on a second ``auth_failed``, and an Apps redeploy can answer 404 at the edge for a few
    seconds -- so giving up here would kill every publisher on every deploy.
    """
    assert classify_failure(rejected(404)) is Failure.PROTOCOL
    assert classify_failure(rejected(405)) is Failure.PROTOCOL


# --------------------------------------------------------------------------------------
# Non-HTTP failures
# --------------------------------------------------------------------------------------


def test_a_malformed_url_is_terminal_configuration_and_not_a_retry() -> None:
    """T-AUTH-CONTENT. ``InvalidURI`` can only mean a bad ``$SHELLBOX_APP_URL``.

    It cannot mean a chased redirect, because ``_NoRedirect`` refuses to follow one -- see the
    test below. Retrying a typo forever is the loop this classification exists to stop.
    """
    got = classify_failure(InvalidURI("https://app.example/x", "scheme isn't ws or wss"))

    assert got is Failure.CONFIG


@pytest.mark.parametrize(
    "exc",
    [
        ConnectionClosedError(None, None),
        InvalidMessage("did not receive a valid HTTP response"),
        OSError("connection refused"),
        TimeoutError(),
        EOFError(),
        CodecError("hello arrived truncated"),
    ],
    ids=["abrupt-close", "bad-handshake", "refused", "timeout", "eof", "bad-frame"],
)
def test_network_and_decode_failures_are_transient(exc: BaseException) -> None:
    """T-AUTH-CONTENT. The socket-level failures, including the MEASURED teardown signature.

    ``ConnectionClosedError(None, None)`` renders as "no close frame received or sent", which
    is the exact text the probe recorded against the real edge every 10 to 18 minutes. It is
    the single most common input to this function in production and it must never be terminal.
    """
    assert classify_failure(exc) is Failure.TRANSIENT


# --------------------------------------------------------------------------------------
# The HTML detector itself, including what it deliberately does NOT match
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("content_type", "body"),
    [
        ("text/html", b""),
        ("text/html; charset=utf-8", b""),
        ("TEXT/HTML", b""),
        (None, b"<!doctype html><html>"),
        (None, b"<!DOCTYPE HTML PUBLIC>"),
        (None, b"   \n\t<html lang='en'>"),
        (None, b"<HTML>"),
    ],
    ids=["ct", "ct-charset", "ct-upper", "doctype", "doctype-upper", "leading-space", "html-upper"],
)
def test_a_web_page_is_recognised_by_its_type_or_its_first_bytes(
    content_type: str | None, body: bytes
) -> None:
    """T-AUTH-CONTENT. Two signals and no third: the declared type, and the opening markers."""
    assert _looks_like_html(content_type, body)


@pytest.mark.parametrize(
    ("content_type", "body"),
    [
        (None, b""),
        ("application/octet-stream", b"\x00\xff\x01\xfe"),
        ("application/json", b'{"error":"nope"}'),
        (None, b"Please sign in with your password to continue"),
        (None, b"<!doctype html>" + b"x" * 600),
    ],
    ids=["empty", "binary", "json", "prose-about-signing-in", "marker-still-in-first-512"],
)
def test_the_detector_reads_markers_and_not_the_page_copy(
    content_type: str | None, body: bytes
) -> None:
    """T-AUTH-CONTENT. The non-behavior, asserted because it is a decision.

    ``Please sign in with your password`` is prose that a *terminal* can legitimately emit --
    it is the kind of string an SSH banner or a login prompt inside the pane contains. Matching
    on it would classify a working session's own output as an auth failure.

    The last case is the inverse check: a marker within the first 512 bytes still matches
    however long the document is, so the window is a bound on the scan and not on the match.
    """
    expected = body.lstrip().lower().startswith((b"<!doctype html", b"<html"))
    assert _looks_like_html(content_type, body) is expected


def test_the_client_refuses_to_follow_a_redirect() -> None:
    """T-AUTH-CONTENT. The structural half: the 302 must SURVIVE to reach ``classify_failure``.

    MEASURED against ``websockets`` 15.0.1: the default ``connect`` follows a 302, and a
    Databricks login redirect points at an ``https://`` URL -- so the follow fails with
    ``InvalidURI: scheme isn't ws or wss``. That is an auth failure arriving under a name that
    says nothing about auth, and ``classify_failure`` would have to guess.

    It also means this client never sends its bearer token to whatever host a ``Location``
    header names, which is the security half of the same override.
    """
    original = rejected(302)

    assert _NoRedirect.process_redirect(object.__new__(_NoRedirect), original) is original
