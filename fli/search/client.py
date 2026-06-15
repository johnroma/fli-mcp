"""HTTP client implementation with impersonation, rate limiting and retry functionality.

This module provides a robust HTTP client that handles:

- User agent impersonation (to mimic a browser)
- Rate limiting (10 requests per second, *globally* across threads)
- Automatic retries with exponential backoff
- Thread-safe session management (one ``curl_cffi`` session per worker thread)
- Error handling

Threading model
---------------

``curl_cffi.requests.Session`` wraps a libcurl handle which is not safe
to share across threads. We keep one session per worker thread using
``threading.local``; the rate-limit budget is shared globally via
:class:`~fli.search._concurrency.TokenBucketRateLimiter` so concurrent
callers cooperate cleanly under Google's 10 req/sec ceiling.
"""

from __future__ import annotations

import os
import threading
from typing import TYPE_CHECKING, Any

from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from fli.search._concurrency import TokenBucketRateLimiter
from fli.search.exceptions import (
    SearchClientError,
    SearchConnectionError,
    SearchHTTPError,
    SearchTimeoutError,
)

# ``curl_cffi`` adds ~100ms to import time on first load — we only need
# it once an HTTP request actually fires, so import lazily on first use.
# ``TYPE_CHECKING`` makes the annotation visible to static checkers
# without paying the import cost at runtime.
if TYPE_CHECKING:
    from curl_cffi import requests as _curl_requests

    Response = _curl_requests.Response
    Session = _curl_requests.Session
else:
    Response = "Any"
    Session = "Any"

# Module-level singleton client + lock guarding its lazy initialisation.
# ``get_client()`` uses double-checked locking so concurrent first callers
# can't each construct an independent ``Client`` (each with its own
# ``TokenBucketRateLimiter`` — that would silently double the global
# request budget).
client: Client | None = None
_client_lock = threading.Lock()

# Google's published ceiling.
DEFAULT_CALLS_PER_SECOND = 10

# Request timeout in seconds.  Override with the FLI_TIMEOUT env var.
DEFAULT_TIMEOUT: float = 60.0
_env_timeout = os.environ.get("FLI_TIMEOUT")
if _env_timeout is not None:
    try:
        REQUEST_TIMEOUT: float = float(_env_timeout)
    except ValueError:
        msg = f"FLI_TIMEOUT must be a number of seconds, got: {_env_timeout!r}"
        raise ValueError(msg) from None
    if REQUEST_TIMEOUT <= 0:
        raise ValueError(f"FLI_TIMEOUT must be a positive number, got: {_env_timeout!r}")
else:
    REQUEST_TIMEOUT = DEFAULT_TIMEOUT


def _is_retryable(exc: BaseException) -> bool:
    """Decide whether a failed request is worth retrying.

    Retrying a rate-limit / client error is actively harmful: a 429 means
    Google is already shedding our load, so firing the same request two
    more times only deepens the per-IP cooldown (and triples the request
    count behind every user-visible search). We therefore retry *only*
    transient failures — timeouts, connection drops, and 5xx server errors
    — and surface 4xx (including 429) immediately.
    """
    if isinstance(exc, SearchHTTPError):
        status = exc.status_code
        return status is not None and status >= 500
    return isinstance(exc, (SearchTimeoutError, SearchConnectionError))


class Client:
    """HTTP client with built-in rate limiting, retry and user agent impersonation functionality.

    Sessions are kept per-thread because ``curl_cffi.requests.Session`` is
    not thread-safe — concurrent ``post``/``get`` calls from different
    threads each get their own libcurl handle. The shared
    :class:`TokenBucketRateLimiter` enforces the global 10 req/sec budget
    across all of them.
    """

    DEFAULT_HEADERS = {
        "content-type": "application/x-www-form-urlencoded;charset=UTF-8",
    }

    def __init__(
        self,
        calls_per_second: int = DEFAULT_CALLS_PER_SECOND,
    ):
        """Initialise the shared rate limiter and per-thread session storage."""
        self._sessions = threading.local()
        self._rate_limiter = TokenBucketRateLimiter(calls=calls_per_second, period=1.0)

    def _session(self) -> Session:
        """Return this thread's ``Session``, creating it on first use."""
        session = getattr(self._sessions, "session", None)
        if session is None:
            # Deferred import: ``curl_cffi`` is heavy (~100ms cold) and
            # not needed for CLI flows that never hit the network, so
            # only pull it in on the first real request.
            from curl_cffi import requests as _requests

            session = _requests.Session()
            session.headers.update(self.DEFAULT_HEADERS)
            self._sessions.session = session
        return session

    def __del__(self):
        """Best-effort cleanup of the main-thread session (others die with their thread)."""
        session = getattr(self._sessions, "session", None) if hasattr(self, "_sessions") else None
        if session is not None:
            try:
                session.close()
            except Exception:  # noqa: BLE001 — destruction-time best effort
                pass

    # ------------------------------------------------------------------
    # Request entry points
    # ------------------------------------------------------------------

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(),
        retry=retry_if_exception(_is_retryable),
        reraise=True,
    )
    def get(self, url: str, **kwargs: Any) -> Response:
        """Make a rate-limited GET request with automatic retries."""
        self._rate_limiter.acquire()
        kwargs.setdefault("timeout", REQUEST_TIMEOUT)
        try:
            response = self._session().get(url, **kwargs)
            response.raise_for_status()
            return response
        except Exception as e:
            raise _wrap_request_error("GET", url, e) from e

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(),
        retry=retry_if_exception(_is_retryable),
        reraise=True,
    )
    def post(self, url: str, **kwargs: Any) -> Response:
        """Make a rate-limited POST request with automatic retries."""
        self._rate_limiter.acquire()
        kwargs.setdefault("timeout", REQUEST_TIMEOUT)
        try:
            response = self._session().post(url, **kwargs)
            response.raise_for_status()
            return response
        except Exception as e:
            raise _wrap_request_error("POST", url, e) from e


def _wrap_request_error(method: str, url: str, exc: BaseException) -> SearchClientError:
    """Map curl-cffi / network errors into our typed ``SearchClientError`` family.

    The CLI surfaces these as short user-facing messages and writes the
    underlying traceback to a log file, so the message here should read
    well on its own.
    """
    # If a typed error somehow escapes the request body (e.g. a future
    # change in ``_session()``), keep its original type instead of
    # downgrading it to the generic fallback below.
    if isinstance(exc, SearchClientError):
        return exc

    # Imported lazily — ``curl_cffi.requests.exceptions`` triggers the
    # full curl-cffi load, which we otherwise defer until first request.
    from curl_cffi.requests import exceptions as curl_exc

    host = _host_from_url(url)
    if isinstance(exc, curl_exc.Timeout):
        return SearchTimeoutError(
            f"Timed out talking to Google Flights ({host}). "
            "The service may be slow or unreachable from your network — "
            "check your connection and try again."
        )
    if isinstance(exc, curl_exc.ConnectionError):
        return SearchConnectionError(
            f"Could not reach Google Flights ({host}). "
            "Check your internet connection or DNS and try again."
        )
    if isinstance(exc, curl_exc.HTTPError):
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
        return SearchHTTPError(
            _describe_http_error(status, response),
            status_code=status,
        )
    # Anything else (including bare CurlError) — keep a clean message but
    # preserve the original via ``__cause__`` so logs still show details.
    return SearchClientError(
        f"{method} request to Google Flights ({host}) failed: {exc.__class__.__name__}"
    )


def _visible_text(body: str, limit: int = 240) -> str:
    """Strip HTML tags/scripts and collapse whitespace for a readable snippet."""
    import re

    # Drop <script>/<style> blocks wholesale, then remaining tags.
    body = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", body)
    body = re.sub(r"(?s)<[^>]+>", " ", body)
    body = re.sub(r"\s+", " ", body).strip()
    return body[:limit]


def _describe_http_error(status: int | None, response: object) -> str:
    """Build a message that forwards Google's actual error, not a generic 429.

    Google's anti-bot system answers with an HTML CAPTCHA interstitial
    ("Our systems have detected unusual traffic …") under an HTTP 429.
    Collapsing that to "rate-limited, or blocked" hides the real cause and
    the recovery path, so we detect it explicitly and otherwise forward a
    readable snippet of whatever body Google returned.
    """
    suffix = f" (HTTP {status})" if status else ""
    body = ""
    try:
        body = getattr(response, "text", "") or ""
    except Exception:  # noqa: BLE001 — never let error formatting fail the path
        body = ""

    low = body.lower()
    if "unusual traffic" in low or "captcha" in low or "/sorry/" in low:
        return (
            f"Google Flights blocked the request as automated traffic{suffix}: it "
            "served a CAPTCHA 'unusual traffic from your computer network' page. "
            "The block expires shortly after requests stop - reduce request volume "
            "and parallelism, then wait before retrying."
        )

    snippet = _visible_text(body)
    if snippet:
        return f"Google Flights returned an error response{suffix}: {snippet}"
    return (
        f"Google Flights returned an error response{suffix}. "
        "The request may be malformed, rate-limited, or blocked."
    )


def _host_from_url(url: str) -> str:
    """Best-effort host extraction for error messages."""
    try:
        from urllib.parse import urlparse

        return urlparse(url).hostname or url
    except Exception:  # noqa: BLE001 — never let logging fail the request path
        return url


def get_client() -> Client:
    """Get or create a shared HTTP client instance.

    Returns:
        Singleton instance of the HTTP client

    """
    # Double-checked locking: the fast path is a single read (no lock
    # taken once the client is initialised). Only the first concurrent
    # callers serialise through ``_client_lock`` to ensure exactly one
    # ``Client`` (and therefore one ``TokenBucketRateLimiter``) ever
    # exists per process.
    global client
    if client is None:
        with _client_lock:
            if client is None:
                client = Client()
    return client
