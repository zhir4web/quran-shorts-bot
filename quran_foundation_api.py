"""Small, secret-safe Quran Foundation Content API client.

Uses the legacy public API until server-side client credentials are configured.
When configured, it uses Quran Foundation's authenticated Content API and
caches the client-credentials token in memory for the life of the process.
"""
from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import math
import os
import random
import time
from urllib.parse import urljoin, urlparse

import requests


LEGACY_API_BASE = "https://api.quran.com/api/v4/"
API_BASE_BY_ENV = {
    "production": "https://apis.quran.foundation/content/api/v4/",
    "prelive": "https://apis-prelive.quran.foundation/content/api/v4/",
}
AUTH_BASE_BY_ENV = {
    "production": "https://oauth2.quran.foundation",
    "prelive": "https://prelive-oauth2.quran.foundation",
}
REQUEST_TIMEOUT = (10, 30)
MAX_ATTEMPTS = 5
MAX_RETRY_AFTER_SECONDS = 30.0


def _is_retryable_status(status_code):
    """Retry rate limits and server/gateway failures with bounded backoff."""
    return status_code == 429 or 500 <= status_code <= 599


class QuranFoundationAPIError(RuntimeError):
    """A safe API diagnostic that never includes response bodies or secrets."""


class QuranFoundationNotFound(QuranFoundationAPIError):
    """A requested optional chapter recording does not exist."""


_TOKEN = None
_TOKEN_EXPIRES_AT = 0.0
_TOKEN_CLIENT_ID = None
_TOKEN_ENV = None


def _configuration():
    client_id = os.environ.get("QF_CLIENT_ID", "").strip()
    client_secret = os.environ.get("QF_CLIENT_SECRET", "").strip()
    if not client_id and not client_secret:
        return None
    if not client_id or not client_secret:
        raise QuranFoundationAPIError(
            "Set both QF_CLIENT_ID and QF_CLIENT_SECRET to use the authenticated API"
        )
    environment = os.environ.get("QF_ENV", "production").strip().lower()
    if environment not in API_BASE_BY_ENV:
        raise QuranFoundationAPIError("QF_ENV must be production or prelive")
    return client_id, client_secret, environment


def api_url(path):
    """Return an API endpoint for the configured Quran Foundation environment."""
    if not isinstance(path, str) or not path or path.startswith(("/", "\\")):
        raise ValueError("Quran Foundation endpoint must be a relative path")
    if ".." in path.split("/") or urlparse(path).scheme or urlparse(path).netloc:
        raise ValueError("Quran Foundation endpoint path is invalid")
    config = _configuration()
    base = API_BASE_BY_ENV[config[2]] if config else LEGACY_API_BASE
    return urljoin(base, path)


def _retry_delay(response, attempt, now=None):
    """Respect Retry-After when possible, otherwise use bounded backoff."""
    headers = getattr(response, "headers", {}) or {}
    retry_after = headers.get("Retry-After")
    if retry_after is not None:
        try:
            delay = float(retry_after)
        except (TypeError, ValueError):
            try:
                retry_at = parsedate_to_datetime(str(retry_after))
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                current = now or datetime.now(timezone.utc)
                delay = (retry_at - current).total_seconds()
            except (TypeError, ValueError, OverflowError):
                delay = -1.0
        if math.isfinite(delay) and delay >= 0:
            return min(MAX_RETRY_AFTER_SECONDS, delay)
    return min(20.0, 2.0 ** max(0, attempt - 1)) + random.uniform(0.0, 0.5)


def _pause(delay, sleeper):
    sleeper(max(0.0, min(MAX_RETRY_AFTER_SECONDS, float(delay))))


def _request_token(client_id, client_secret, environment, client, sleeper):
    global _TOKEN, _TOKEN_EXPIRES_AT, _TOKEN_CLIENT_ID, _TOKEN_ENV
    now = time.monotonic()
    if (_TOKEN and _TOKEN_CLIENT_ID == client_id and _TOKEN_ENV == environment and
            now < _TOKEN_EXPIRES_AT):
        return _TOKEN

    endpoint = AUTH_BASE_BY_ENV[environment] + "/oauth2/token"
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = client.post(
                endpoint,
                auth=(client_id, client_secret),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                data={"grant_type": "client_credentials", "scope": "content"},
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException:
            if attempt == MAX_ATTEMPTS:
                raise QuranFoundationAPIError(
                    "Quran Foundation authentication is temporarily unavailable"
                ) from None
            _pause(min(20.0, 2.0 ** (attempt - 1)) + random.uniform(0.0, 0.5), sleeper)
            continue

        if _is_retryable_status(response.status_code):
            if attempt == MAX_ATTEMPTS:
                raise QuranFoundationAPIError(
                    f"Quran Foundation authentication is temporarily unavailable (HTTP {response.status_code})"
                )
            _pause(_retry_delay(response, attempt), sleeper)
            continue
        if response.status_code != 200:
            raise QuranFoundationAPIError(
                f"Quran Foundation authentication failed (HTTP {response.status_code}); check the client and environment"
            )
        try:
            payload = response.json()
            token = payload.get("access_token")
            expires_in = float(payload.get("expires_in", 0))
        except (ValueError, TypeError, AttributeError):
            token, expires_in = None, 0
        if not isinstance(token, str) or not token or not math.isfinite(expires_in) or expires_in <= 0:
            raise QuranFoundationAPIError("Quran Foundation returned an invalid authentication response")
        refresh_margin = min(120.0, max(15.0, expires_in * 0.1))
        _TOKEN = token
        _TOKEN_EXPIRES_AT = time.monotonic() + max(1.0, expires_in - refresh_margin)
        _TOKEN_CLIENT_ID = client_id
        _TOKEN_ENV = environment
        return token
    raise QuranFoundationAPIError("Quran Foundation authentication is temporarily unavailable")


def clear_token_cache():
    """Clear the in-memory token; exposed for 401 recovery and unit tests."""
    global _TOKEN, _TOKEN_EXPIRES_AT, _TOKEN_CLIENT_ID, _TOKEN_ENV
    _TOKEN = None
    _TOKEN_EXPIRES_AT = 0.0
    _TOKEN_CLIENT_ID = None
    _TOKEN_ENV = None


def _safe_endpoint(url):
    parsed = urlparse(url)
    return f"{parsed.hostname or 'unknown'}{parsed.path}"


def get_json(url, params=None, session=None, sleep=None):
    """Fetch JSON with bounded transient retries and optional QF OAuth."""
    client = session or requests
    sleeper = sleep or time.sleep
    config = _configuration()
    headers = {"User-Agent": "quran-shorts-bot/3.0"}
    authenticated = config is not None
    if authenticated:
        client_id, client_secret, environment = config
        expected_host = urlparse(API_BASE_BY_ENV[environment]).hostname
        if urlparse(url).hostname != expected_host:
            raise QuranFoundationAPIError("Refusing to send Quran Foundation credentials to an untrusted host")
        headers["x-client-id"] = client_id
    else:
        client_id = client_secret = environment = None

    endpoint = _safe_endpoint(url)
    auth_retried = False
    for attempt in range(1, MAX_ATTEMPTS + 1):
        if authenticated:
            headers["x-auth-token"] = _request_token(
                client_id, client_secret, environment, client, sleeper
            )
        print(f"Quran API request {attempt}/{MAX_ATTEMPTS}: {endpoint}", flush=True)
        try:
            response = client.get(url, params=params, timeout=REQUEST_TIMEOUT, headers=headers)
        except requests.RequestException:
            if attempt == MAX_ATTEMPTS:
                raise QuranFoundationAPIError(
                    "Quran Foundation source is temporarily unavailable (network error)"
                ) from None
            _pause(min(20.0, 2.0 ** (attempt - 1)) + random.uniform(0.0, 0.5), sleeper)
            continue

        if response.status_code == 404 and "/chapter_recitations/" in url:
            raise QuranFoundationNotFound("This chapter reciter has no recording for the selected Surah")
        if response.status_code == 401 and authenticated and not auth_retried:
            clear_token_cache()
            auth_retried = True
            # Retry once with a fresh Client Credentials token, independently
            # from the bounded retry budget for temporary server failures.
            attempt -= 1
            continue
        if _is_retryable_status(response.status_code):
            if attempt == MAX_ATTEMPTS:
                raise QuranFoundationAPIError(
                    f"Quran Foundation source is temporarily unavailable (HTTP {response.status_code})"
                )
            _pause(_retry_delay(response, attempt), sleeper)
            continue
        if response.status_code >= 400:
            raise QuranFoundationAPIError(
                f"Quran Foundation rejected the request (HTTP {response.status_code})"
            )
        try:
            payload = response.json()
        except (ValueError, TypeError):
            if attempt == MAX_ATTEMPTS:
                raise QuranFoundationAPIError("Quran Foundation returned unreadable data") from None
            _pause(min(20.0, 2.0 ** (attempt - 1)) + random.uniform(0.0, 0.5), sleeper)
            continue
        if not isinstance(payload, dict):
            raise QuranFoundationAPIError("Quran Foundation returned an unexpected response")
        print("Quran API response received", flush=True)
        return payload
    raise QuranFoundationAPIError("Quran Foundation source is temporarily unavailable")
