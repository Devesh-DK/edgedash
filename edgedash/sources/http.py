"""
The ONLY place in EdgeDash that performs HTTP requests.

Public API
----------
get_json(url, params, headers, timeout, retries) -> dict | list

    Fetches a JSON endpoint with:
    - A descriptive User-Agent header
    - Configurable timeout (default 10 s)
    - 2 retry attempts with exponential backoff (1 s, 2 s)
    - Raises SourceError with a clear message on any failure

post_json(url, body, params, headers, timeout, retries) -> dict | list

    Same guarantees as get_json but sends a JSON POST body.
    Used by sources whose APIs require POST (e.g. Apify actor runs).

No other module may call requests.get() / requests.post() directly.
"""

from __future__ import annotations

import time
from typing import Any

import requests

_USER_AGENT = (
    "EdgeDash/1.0 (autonomous career intelligence agent; "
    "https://github.com/your-org/edgedash)"
)

_DEFAULT_TIMEOUT: int = 10
_DEFAULT_RETRIES: int = 2
_BACKOFF_BASE: float = 1.0  # seconds; doubled on each retry


class SourceError(RuntimeError):
    """Raised when a source HTTP call fails after all retries."""


def get_json(
    url: str,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = _DEFAULT_TIMEOUT,
    retries: int = _DEFAULT_RETRIES,
) -> dict | list:
    """Fetch a JSON endpoint and return the parsed response.

    Retries up to `retries` times with exponential backoff before raising
    SourceError.

    Args:
        url:      Full URL to request.
        params:   Optional query-string parameters.
        headers:  Optional extra request headers (merged with defaults).
        timeout:  Per-attempt timeout in seconds.
        retries:  Number of retry attempts after the first failure.

    Returns:
        Parsed JSON as a dict or list.

    Raises:
        SourceError: If all attempts fail.
    """
    base_headers = {"User-Agent": _USER_AGENT, "Accept": "application/json"}
    if headers:
        base_headers.update(headers)

    last_error: Exception | None = None

    for attempt in range(retries + 1):
        try:
            response = requests.get(
                url,
                params=params,
                headers=base_headers,
                timeout=timeout,
            )
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            if attempt < retries:
                wait = _BACKOFF_BASE * (2 ** attempt)
                time.sleep(wait)

    raise SourceError(
        f"Failed to fetch {url!r} after {retries + 1} attempt(s). "
        f"Last error: {last_error}"
    ) from last_error


def get_text(
    url: str,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = _DEFAULT_TIMEOUT,
    retries: int = _DEFAULT_RETRIES,
) -> str:
    """Fetch an endpoint and return the response text (for XML/RSS/text).

    Retries up to `retries` times with exponential backoff before raising SourceError.
    """
    base_headers = {"User-Agent": _USER_AGENT, "Accept": "application/xml, text/xml, text/plain, */*"}
    if headers:
        base_headers.update(headers)

    last_error: Exception | None = None

    for attempt in range(retries + 1):
        try:
            response = requests.get(
                url,
                params=params,
                headers=base_headers,
                timeout=timeout,
            )
            response.raise_for_status()
            return response.text
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            if attempt < retries:
                wait = _BACKOFF_BASE * (2 ** attempt)
                time.sleep(wait)

    raise SourceError(
        f"Failed to fetch {url!r} after {retries + 1} attempt(s). "
        f"Last error: {last_error}"
    ) from last_error


def post_json(
    url: str,
    body: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = _DEFAULT_TIMEOUT,
    retries: int = _DEFAULT_RETRIES,
) -> dict | list:
    """POST a JSON body to an endpoint and return the parsed response.

    Same retry/backoff/User-Agent guarantees as get_json.

    Args:
        url:      Full URL to POST to.
        body:     JSON-serialisable dict sent as the request body.
        params:   Optional query-string parameters (e.g. API tokens).
        headers:  Optional extra request headers (merged with defaults).
        timeout:  Per-attempt timeout in seconds.
        retries:  Number of retry attempts after the first failure.

    Returns:
        Parsed JSON as a dict or list.

    Raises:
        SourceError: If all attempts fail.
    """
    base_headers = {
        "User-Agent": _USER_AGENT,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    if headers:
        base_headers.update(headers)

    last_error: Exception | None = None

    for attempt in range(retries + 1):
        try:
            response = requests.post(
                url,
                json=body,
                params=params,
                headers=base_headers,
                timeout=timeout,
            )
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            if attempt < retries:
                wait = _BACKOFF_BASE * (2 ** attempt)
                time.sleep(wait)

    raise SourceError(
        f"Failed to POST {url!r} after {retries + 1} attempt(s). "
        f"Last error: {last_error}"
    ) from last_error
