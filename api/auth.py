"""
api/auth.py — Bearer and WebSocket ticket verification for the bot HTTP/WS module.

Two public functions:
    verify_bearer(request_headers, config) -> bool
    verify_ws_ticket(token: str, config) -> bool

Neither raises on auth failure — both return False. The HTTP handler (Step 4)
maps False to 401. Secrets are accessed via .get_secret_value() at the compare
site only; they are never logged.
"""

from __future__ import annotations

import hmac
import threading
from typing import Any

import jwt
import structlog

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Single-use nonce store for WS tickets
# ---------------------------------------------------------------------------
# Maps sub (nonce string) -> exp timestamp (float, Unix seconds).
# Bounded to _MAX_CONSUMED entries; evicted in exp_ts ascending order (FIFO
# by expiry, not by insertion time — guarantees that the longest-lived nonces
# are evicted last, which is correct for a short-lived ticket system).
# ---------------------------------------------------------------------------

_MAX_CONSUMED: int = 10_000
_consumed_subs: dict[str, float] = {}
_consumed_subs_lock: threading.Lock = threading.Lock()


def _register_sub(sub: str, exp_ts: float) -> bool:
    """
    Attempt to register *sub* as consumed.  Returns True if registration
    succeeded (sub was not previously seen), False if it was a replay.

    Evicts oldest-expiry entries when the store is at capacity before
    inserting, so the store never grows beyond _MAX_CONSUMED + 1.
    """
    with _consumed_subs_lock:
        if sub in _consumed_subs:
            logger.debug("ws_ticket_replay_detected", sub_truncated=sub[:8])
            return False

        # Evict by exp_ts ascending when at capacity.
        if len(_consumed_subs) >= _MAX_CONSUMED:
            oldest_sub = min(_consumed_subs, key=_consumed_subs.__getitem__)
            del _consumed_subs[oldest_sub]
            logger.debug("ws_ticket_nonce_evicted", store_size=len(_consumed_subs))

        _consumed_subs[sub] = exp_ts
        return True


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_api(config: Any) -> Any | None:
    """Return config.api if present, else None."""
    return getattr(config, "http_api", None)


def _get_bot_api_token(config: Any) -> str | None:
    """
    Defensive getter for Bearer secret.  Returns the raw token string, or
    None if the attribute chain is broken (missing field, not a SecretStr).
    Never logs the value.
    """
    api = _get_api(config)
    if api is None:
        return None
    secret_field = getattr(api, "bot_api_token", None)
    if secret_field is None:
        return None
    get_secret = getattr(secret_field, "get_secret_value", None)
    if not callable(get_secret):
        return None
    try:
        return get_secret()
    except Exception:  # noqa: BLE001
        return None


def _get_ws_ticket_secret(config: Any) -> str | None:
    """
    Defensive getter for WS ticket HS256 secret.  Returns the raw secret
    string, or None if the attribute chain is broken.  Never logs the value.
    """
    api = _get_api(config)
    if api is None:
        return None
    secret_field = getattr(api, "ws_ticket_secret", None)
    if secret_field is None:
        return None
    get_secret = getattr(secret_field, "get_secret_value", None)
    if not callable(get_secret):
        return None
    try:
        return get_secret()
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def verify_bearer(request_headers: Any, config: Any) -> bool:
    """
    Validate an ``Authorization: Bearer <token>`` header against
    ``config.api.bot_api_token``.

    Parameters
    ----------
    request_headers:
        A mapping that supports ``.get("Authorization")`` — compatible with
        aiohttp's ``CIMultiDictProxy``, plain ``dict``, or any other
        case-sensitive header mapping that the HTTP handler passes in.
    config:
        The application config object.  Must expose
        ``config.api.bot_api_token`` as a ``pydantic.SecretStr`` (or any
        object with ``.get_secret_value() -> str``).

    Returns
    -------
    bool
        True if the header is present and the token matches; False in all
        other cases (missing header, missing config field, wrong token).
    """
    expected = _get_bot_api_token(config)
    if expected is None:
        logger.info("bearer_verify_config_missing")
        return False

    auth_header: str | None = None
    try:
        auth_header = request_headers.get("Authorization") or request_headers.get(
            "authorization"
        )
    except Exception:  # noqa: BLE001
        logger.debug("bearer_verify_header_read_error")
        return False

    if not auth_header:
        logger.debug("bearer_verify_no_header")
        return False

    parts = auth_header.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        logger.debug("bearer_verify_malformed_header")
        return False

    provided = parts[1]

    try:
        # hmac.compare_digest requires both operands to be the same type.
        match = hmac.compare_digest(
            provided.encode("utf-8"), expected.encode("utf-8")
        )
    except Exception:  # noqa: BLE001
        logger.debug("bearer_verify_compare_error")
        return False

    if match:
        logger.info("bearer_verify_ok")
    else:
        logger.info("bearer_verify_failed")

    return match


def verify_ws_ticket(token: str, config: Any) -> bool:
    """
    Validate a short-lived HS256 JWT used as a WebSocket upgrade ticket.

    The token must carry:
    - ``sub``  — nonce string (single-use; replays are rejected)
    - ``exp``  — expiry (validated automatically by PyJWT)
    - ``iat``  — issued-at (present; not verified beyond presence)

    The secret is taken from ``config.api.ws_ticket_secret``.

    Parameters
    ----------
    token:
        The raw JWT string received from the WebSocket upgrade request.
    config:
        The application config object.  Must expose
        ``config.api.ws_ticket_secret`` as a ``pydantic.SecretStr``.

    Returns
    -------
    bool
        True if the token is valid, unexpired, and not previously consumed.
        False in all other cases.
    """
    secret = _get_ws_ticket_secret(config)
    if secret is None:
        logger.info("ws_ticket_verify_config_missing")
        return False

    try:
        payload: dict[str, Any] = jwt.decode(
            token,
            secret,
            algorithms=["HS256"],
            options={"verify_exp": True},
        )
    except jwt.ExpiredSignatureError:
        logger.info("ws_ticket_expired")
        return False
    except jwt.InvalidTokenError:
        # Covers DecodeError, InvalidSignatureError, etc.
        logger.info("ws_ticket_invalid")
        return False
    except Exception:  # noqa: BLE001
        logger.debug("ws_ticket_decode_unexpected_error")
        return False

    sub = payload.get("sub")
    if not sub or not isinstance(sub, str):
        logger.info("ws_ticket_missing_sub")
        return False

    # iat presence check (not signature-verified separately; PyJWT already
    # verified the signature above).
    if "iat" not in payload:
        logger.info("ws_ticket_missing_iat")
        return False

    exp_ts: float = float(payload.get("exp", 0.0))

    # Single-use enforcement.
    if not _register_sub(sub, exp_ts):
        logger.info("ws_ticket_already_consumed")
        return False

    logger.info("ws_ticket_verify_ok")
    return True
