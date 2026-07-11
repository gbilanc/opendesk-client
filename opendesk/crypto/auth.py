"""
Password-based authentication with Argon2id hashing.

Provides:
- ``AuthManager`` — stores credentials (in-memory or loaded from config)
- Password verification with Argon2id (memory-hard, resistant to GPU/ASIC)
- Session ID generation (like AnyDesk/TeamViewer numeric ID)
- One-time password support for unattended access
"""

from __future__ import annotations

import json
import logging
import os
import random
import string
import time
from dataclasses import dataclass, field
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, VerifyMismatchError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SESSION_ID_LENGTH = 9  # e.g. "123 456 789"
_SESSION_ID_BLOCKS = 3
_SESSION_ID_BLOCK_SIZE = 3
_OTP_LENGTH = 8
_OTP_VALIDITY_SECONDS = 300  # 5 minutes
_MAX_SESSIONS = 100  # max pending sessions before pruning oldest
_SESSION_MAX_AGE = 86400  # 24 hours — discard sessions older than this

# ---------------------------------------------------------------------------
# Password hasher
# ---------------------------------------------------------------------------

_hasher = PasswordHasher(
    time_cost=3,       # number of iterations
    memory_cost=65536, # 64 MiB
    parallelism=4,     # number of threads
    hash_len=32,
    salt_len=16,
)


def hash_password(password: str) -> str:
    """Hash a password with Argon2id.

    Returns
    -------
    str
        Encoded hash string (includes salt, parameters, and hash).
    """
    return _hasher.hash(password)


def verify_password(password: str, hash_str: str) -> bool:
    """Verify a password against its Argon2id hash.

    Returns
    -------
    bool
        ``True`` if the password matches.
    """
    try:
        return _hasher.verify(hash_str, password)
    except (VerificationError, VerifyMismatchError):
        return False


def needs_rehash(hash_str: str) -> bool:
    """Check if the hash uses outdated parameters.

    Call this after successful verification and re-hash if ``True``.
    """
    return _hasher.check_needs_rehash(hash_str)


# ---------------------------------------------------------------------------
# Session ID
# ---------------------------------------------------------------------------


def generate_session_id() -> str:
    """Generate a human-friendly session ID like "123 456 789".

    Returns
    -------
    str
        A 9-digit number grouped in blocks of 3.
    """
    digits = [str(random.randint(0, 9)) for _ in range(_SESSION_ID_LENGTH)]
    blocks = [
        "".join(digits[i : i + _SESSION_ID_BLOCK_SIZE])
        for i in range(0, _SESSION_ID_LENGTH, _SESSION_ID_BLOCK_SIZE)
    ]
    return " ".join(blocks)


def generate_otp() -> str:
    """Generate a one-time password.

    Returns
    -------
    str
        An 8-character alphanumeric OTP.
    """
    alphabet = string.ascii_uppercase + string.digits
    return "".join(random.choices(alphabet, k=_OTP_LENGTH))


# ---------------------------------------------------------------------------
# Credential store
# ---------------------------------------------------------------------------


@dataclass
class StoredCredential:
    """A stored credential entry."""

    username: str
    password_hash: str
    created_at: float = field(default_factory=time.time)
    last_used_at: float | None = None


@dataclass
class PendingSession:
    """A session waiting for remote acceptance.

    Models the "partner is waiting" pattern used by TeamViewer / AnyDesk.
    """

    session_id: str
    password_hash: str
    created_at: float = field(default_factory=time.time)
    is_one_time: bool = False
    otp: str | None = None
    expires_at: float = field(default_factory=lambda: time.time() + _OTP_VALIDITY_SECONDS)


class AuthManager:
    """Manages credentials and sessions.

    Supports:
    - Persistent credential storage (JSON file)
    - Session ID generation for incoming connections
    - One-time password for unattended access
    - Password change
    """

    def __init__(self, config_path: str | Path | None = None) -> None:
        self._config_path = Path(config_path) if config_path else Path.home() / ".opendesk" / "credentials.json"
        self._credentials: dict[str, StoredCredential] = {}
        self._pending_sessions: dict[str, PendingSession] = {}
        self._load()

    # ── credential management ───────────────────────────────────────

    def set_password(self, username: str, password: str) -> None:
        """Set or update a user's password."""
        h = hash_password(password)
        existing = self._credentials.get(username)
        if existing:
            existing.password_hash = h
            logger.info("Password updated for '%s'", username)
        else:
            self._credentials[username] = StoredCredential(
                username=username, password_hash=h,
            )
            logger.info("Password created for '%s'", username)
        self._save()

    def authenticate(self, username: str, password: str) -> bool:
        """Verify credentials.

        Automatically re-hashes if the parameters have changed.
        """
        cred = self._credentials.get(username)
        if cred is None:
            return False

        if not verify_password(password, cred.password_hash):
            return False

        cred.last_used_at = time.time()

        if needs_rehash(cred.password_hash):
            cred.password_hash = hash_password(password)
            self._save()
            logger.info("Re-hashed password for '%s' (parameter upgrade)", username)

        return True

    def remove_user(self, username: str) -> bool:
        """Remove a user's credentials."""
        if username in self._credentials:
            del self._credentials[username]
            self._save()
            logger.info("Removed user '%s'", username)
            return True
        return False

    def list_users(self) -> list[str]:
        """Return all registered usernames."""
        return list(self._credentials.keys())

    def has_users(self) -> bool:
        """Check if any credentials exist."""
        return len(self._credentials) > 0

    # ── session management ──────────────────────────────────────────

    def create_session(self, password: str, one_time: bool = False) -> PendingSession:
        """Create a pending session (like AnyDesk waiting for connection).

        Parameters
        ----------
        password : str
            The password the remote peer must provide.
        one_time : bool
            If ``True``, the session is valid for a single connection
            and auto-expires.

        Returns
        -------
        PendingSession
        """
        # Clean up expired sessions before creating a new one
        self.cleanup_expired()
        self._enforce_max_sessions()

        session_id = generate_session_id()
        while session_id in self._pending_sessions:
            session_id = generate_session_id()

        otp = generate_otp() if one_time else None

        session = PendingSession(
            session_id=session_id,
            password_hash=hash_password(password),
            is_one_time=one_time,
            otp=otp,
            expires_at=time.time() + _OTP_VALIDITY_SECONDS if one_time else 0,
        )
        self._pending_sessions[session_id] = session
        logger.info(
            "Session %s created (one_time=%s)", session_id, one_time,
        )
        return session

    def verify_session(self, session_id: str, password: str) -> bool:
        """Verify a session password.

        Returns
        -------
        bool
            ``True`` if the session exists, has not expired, and the
            password matches.
        """
        session = self._pending_sessions.get(session_id)
        if session is None:
            return False

        # Check expiration
        if session.is_one_time and time.time() > session.expires_at:
            self._pending_sessions.pop(session_id, None)
            logger.warning("Session %s expired", session_id)
            return False

        # Verify password
        if not verify_password(password, session.password_hash):
            return False

        # One-time: consume immediately
        if session.is_one_time:
            self._pending_sessions.pop(session_id, None)
            logger.info("Session %s consumed (one-time)", session_id)

        return True

    def remove_session(self, session_id: str) -> None:
        """Remove a pending session."""
        self._pending_sessions.pop(session_id, None)

    def cleanup_expired(self) -> int:
        """Remove all expired one-time sessions and old non-OTP sessions.

        Removes:
        - One-time sessions past their expiry time.
        - Non-OTP sessions older than ``_SESSION_MAX_AGE`` (24h).

        Returns
        -------
        int
            Number of sessions removed.
        """
        now = time.time()
        expired = [
            sid for sid, s in self._pending_sessions.items()
            if (s.is_one_time and now > s.expires_at)
            or (not s.is_one_time and now - s.created_at > _SESSION_MAX_AGE)
        ]
        for sid in expired:
            del self._pending_sessions[sid]
        if expired:
            logger.info("Cleaned up %d expired/stale sessions", len(expired))
        return len(expired)

    def _enforce_max_sessions(self) -> int:
        """If we have more than ``_MAX_SESSIONS`` pending, remove the oldest.

        Returns
        -------
        int
            Number of sessions removed.
        """
        if len(self._pending_sessions) <= _MAX_SESSIONS:
            return 0
        # Sort by creation time (oldest first) and remove excess
        sorted_ids = sorted(
            self._pending_sessions.keys(),
            key=lambda sid: self._pending_sessions[sid].created_at,
        )
        excess = len(sorted_ids) - _MAX_SESSIONS
        for sid in sorted_ids[:excess]:
            del self._pending_sessions[sid]
        logger.info("Pruned %d oldest sessions (max %d)", excess, _MAX_SESSIONS)
        return excess

    # ── persistence ─────────────────────────────────────────────────

    def _load(self) -> None:
        """Load credentials from the JSON config file."""
        if not self._config_path.exists():
            return
        try:
            data = json.loads(self._config_path.read_text())
            for username, cred_data in data.get("credentials", {}).items():
                self._credentials[username] = StoredCredential(
                    username=username,
                    password_hash=cred_data["password_hash"],
                    created_at=cred_data.get("created_at", 0),
                    last_used_at=cred_data.get("last_used_at"),
                )
            logger.info("Loaded %d credentials from %s", len(self._credentials), self._config_path)
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning("Failed to load credentials: %s", e)

    def _save(self) -> None:
        """Save credentials to the JSON config file."""
        self._config_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "credentials": {
                u: {
                    "password_hash": c.password_hash,
                    "created_at": c.created_at,
                    "last_used_at": c.last_used_at,
                }
                for u, c in self._credentials.items()
            }
        }
        self._config_path.write_text(json.dumps(data, indent=2))
        logger.debug("Credentials saved to %s", self._config_path)
