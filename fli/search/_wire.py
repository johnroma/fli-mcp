r"""Parsing helpers for Google Flights' FlightsFrontendService wire format.

The Service returns JSONP-flavoured responses of the form::

    )]}'\n\n
    <chunk1_byte_len>\n
    [["wrb.fr", null, "<inner JSON string>"]]
    <chunk2_byte_len>\n
    [["wrb.fr", null, "<inner JSON string>"]]
    ...

`GetShoppingResults` and `GetCalendarGraph` happen to emit a single chunk so
the legacy parsers in this package could get away with `lstrip(")]}'")`.
`GetBookingResults` emits two chunks, so we need a proper multi-chunk reader.

Important quirk: the length headers count UTF-8 **bytes**, not Python string
characters. When the response contains any non-ASCII characters (which it
sometimes does — airport names, airline names) the offsets diverge, so the
reader must operate over the byte representation of the body.

This module centralises that reader and exposes :func:`iter_wrb_chunks` which
yields the decoded inner JSON of each ``wrb.fr`` chunk.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from typing import Any

logger = logging.getLogger(__name__)

_PREFIX = b")]}'"


def _iter_outer_chunks(body: str | bytes) -> Iterator[Any]:
    """Yield each top-level decoded chunk list ("outer") in ``body``.

    This is the framing layer shared by :func:`iter_wrb_chunks` (which walks
    the rows for inner payloads) and :func:`find_response_error` (which walks
    the same rows for an embedded error envelope). Keeping the byte-length
    framing in one place means both views agree on chunk boundaries.

    Robust to single-chunk responses with no length headers (the older
    ``GetShoppingResults`` / ``GetCalendarGraph`` shape) — those are parsed
    by falling back to a single JSON load over the trimmed body.
    """
    if isinstance(body, str):
        raw = body.encode("utf-8")
    else:
        raw = body

    raw = raw.lstrip()
    if raw.startswith(_PREFIX):
        raw = raw[len(_PREFIX) :]
    raw = raw.lstrip()

    if not raw:
        return

    # Fast path: no length headers (legacy single-chunk responses).
    if not (b"0" <= raw[:1] <= b"9"):
        try:
            outer = json.loads(raw.decode("utf-8"))
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
            logger.warning("Failed to decode single-chunk wrb.fr body as JSON", exc_info=True)
            return
        yield outer
        return

    cursor = 0
    while cursor < len(raw):
        # Read the decimal length prefix terminated by \n.
        end = raw.find(b"\n", cursor)
        if end == -1:
            break
        try:
            length = int(raw[cursor:end])
        except ValueError:
            logger.warning(
                "Malformed length header at offset %d; truncating chunk stream",
                cursor,
            )
            break
        # Google's length header counts the leading newline after the header
        # AND the trailing newline that separates this chunk from the next.
        # We've already consumed the leading newline (it terminated the header),
        # so we read `length - 1` bytes which gives JSON + trailing \n.
        cursor = end + 1
        chunk_bytes = max(length - 1, 0)
        payload = raw[cursor : cursor + chunk_bytes]
        cursor += chunk_bytes
        try:
            outer = json.loads(payload.strip().decode("utf-8"))
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
            logger.warning("Discarding malformed wrb.fr chunk", exc_info=True)
            continue
        yield outer


def iter_wrb_chunks(body: str | bytes) -> Iterator[Any]:
    """Yield the inner JSON object of every ``wrb.fr`` chunk in ``body``."""
    for outer in _iter_outer_chunks(body):
        yield from _chunks_from_outer(outer)


def find_response_error(body: str | bytes) -> tuple[int, str] | None:
    """Detect a FlightsFrontendService error envelope in a 200 response.

    Google sometimes answers a shopping/calendar query with HTTP 200 whose
    ``wrb.fr`` row carries an error instead of result data. In that shape the
    inner-payload slot (``row[2]``) is ``null`` and a later element holds::

        [<code>, null, [["type.googleapis.com/...ErrorResponse", ...]]]

    where ``<code>`` is Google's canonical RPC status (e.g. 13 = INTERNAL,
    which their anti-abuse layer returns to throttled IPs). Without this the
    caller can't tell "Google rejected the request" apart from "no flights
    matched" — both collapse to an empty result. Returns ``(code, type_url)``
    for the first such error, else ``None``.
    """
    for outer in _iter_outer_chunks(body):
        if not isinstance(outer, list):
            continue
        for row in outer:
            if not (isinstance(row, list) and row and row[0] == "wrb.fr"):
                continue
            # A normal success row carries its payload as a JSON string in
            # row[2]; only inspect rows where that slot is empty/absent.
            if len(row) > 2 and isinstance(row[2], str) and row[2]:
                continue
            for el in row[1:]:
                if (
                    isinstance(el, list)
                    and len(el) >= 3
                    and isinstance(el[0], int)
                    and isinstance(el[2], list)
                    and el[2]
                    and isinstance(el[2][0], list)
                    and el[2][0]
                    and isinstance(el[2][0][0], str)
                    and "ErrorResponse" in el[2][0][0]
                ):
                    return el[0], el[2][0][0]
    return None


def _chunks_from_outer(outer: Any) -> Iterator[Any]:
    """Walk a top-level chunk list and yield decoded inner-JSON payloads."""
    if not isinstance(outer, list):
        return
    for row in outer:
        if not isinstance(row, list) or len(row) < 3:
            continue
        if row[0] != "wrb.fr":
            continue
        inner = row[2]
        if not isinstance(inner, str) or not inner:
            continue
        try:
            yield json.loads(inner)
        except (ValueError, json.JSONDecodeError):
            logger.warning("Failed to decode wrb.fr inner JSON payload", exc_info=True)
            continue


_RPC_STATUS_NAMES = {
    3: "INVALID_ARGUMENT",
    8: "RESOURCE_EXHAUSTED",
    13: "INTERNAL",
    14: "UNAVAILABLE",
}


def describe_response_error(code: int, type_url: str) -> str:
    """Build a user-facing message for a FlightsFrontendService error envelope."""
    short = type_url.rsplit(".", 1)[-1] if type_url else "ErrorResponse"
    status = _RPC_STATUS_NAMES.get(code)
    status_part = f"{code} ({status})" if status else str(code)
    base = f"Google Flights returned {short} status {status_part} instead of results. "
    # 13/INTERNAL and 8/RESOURCE_EXHAUSTED are what Google's anti-abuse layer
    # serves to throttled IPs after rate-limiting — fingerprint-independent and
    # not fixable by changing the request; the source IP has to cool down.
    if code in (8, 13):
        return base + (
            "This is typically a soft anti-bot throttle on the source IP after "
            "too many requests (it persists while requests keep coming and clears "
            "once they stop). Reduce request volume and wait before retrying."
        )
    return base + "The request was rejected by Google; try again later."


def parse_first_wrb_payload(body: str | bytes) -> Any:
    """Return the inner JSON of the first ``wrb.fr`` chunk, or None."""
    for chunk in iter_wrb_chunks(body):
        return chunk
    return None
