"""
Challenge-response authentication for secure password verification.

Replaces plaintext password transmission over the wire with an
HMAC-based challenge-response mechanism:

1. Host generates a random nonce and sends it with the auth request.
2. Client computes HMAC-SHA256(nonce, password) and sends it back.
3. Host computes the expected HMAC with its stored password and compares.

This ensures the password is never transmitted in plaintext, even
over an unencrypted relay connection.  The nonce is single-use,
preventing replay attacks.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Union


def generate_nonce() -> str:
    """Generate a cryptographically secure random nonce.

    Returns
    -------
    str
        Hex-encoded 32-byte nonce (64 hex chars).
    """
    return secrets.token_hex(32)


def compute_response(nonce: str, password: str) -> str:
    """Compute the HMAC-SHA256 response for a challenge.

    Parameters
    ----------
    nonce : str
        The challenge nonce received from the host (hex-encoded).
    password : str
        The shared secret password.

    Returns
    -------
    str
        Hex-encoded HMAC-SHA256 digest.
    """
    key = password.encode("utf-8")
    msg = nonce.encode("utf-8")
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


def verify_response(nonce: str, password: str, response: str) -> bool:
    """Verify a client's HMAC response against the expected value.

    Uses ``hmac.compare_digest`` for timing-safe comparison.

    Parameters
    ----------
    nonce : str
        The challenge nonce that was sent to the client.
    password : str
        The stored password for the session.
    response : str
        The HMAC digest received from the client.

    Returns
    -------
    bool
        ``True`` if the response is valid.
    """
    expected = compute_response(nonce, password)
    return hmac.compare_digest(expected, response)
