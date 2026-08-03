"""
Unattended access mode.

Allows the remote computer to be accessed without a user accepting
each connection (like AnyDesk's "unattended access").
Configuration is persisted and optionally locked with a master password.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from opendesk.crypto.auth import generate_session_id, hash_password, verify_password

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path.home() / ".opendesk" / "unattended.json"


@dataclass
class UnattendedConfig:
    """Configuration for unattended access."""

    enabled: bool = False
    fixed_password: str = ""  # hashed
    auto_accept: bool = True
    preserve_session_id: bool = True  # keep the same ID across restarts
    session_id: str = ""
    allowed_peers: list[str] = field(default_factory=list)  # allowlist of peer IDs
    require_master_password: bool = False
    master_password_hash: str = ""


class UnattendedAccess:
    """Manager for unattended (non-presidiata) access configuration.

    Allows the computer to be accessed:
    - With a fixed password (no per-session acceptance needed)
    - From a specific list of trusted peers
    - With optional master password lock

    Usage::

        ua = UnattendedAccess()
        ua.enable(password="myfixedpass")
        ...
        if ua.is_allowed(peer_id="abc", password="myfixedpass"):
            # grant access
    """

    def __init__(self, config_path: str | Path | None = None) -> None:
        self._config_path = Path(config_path) if config_path else _CONFIG_PATH
        self._config = UnattendedConfig()
        self._load()

    # ── properties ──────────────────────────────────────────────────

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    @property
    def session_id(self) -> str:
        return self._config.session_id

    @property
    def config(self) -> UnattendedConfig:
        return self._config

    # ── lifecycle ───────────────────────────────────────────────────

    def enable(self, password: str, auto_accept: bool = True) -> None:
        """Enable unattended access with a fixed password.

        Parameters
        ----------
        password : str
            The password that remote peers must provide.
        auto_accept : bool
            If ``True``, connections are accepted automatically
            without local confirmation.
        """
        self._config.enabled = True
        self._config.fixed_password = hash_password(password)
        self._config.auto_accept = auto_accept
        self._config.preserve_session_id = True
        if not self._config.session_id:
            self._config.session_id = generate_session_id()
        self._save()
        logger.info("Unattended access enabled (session: %s)", self._config.session_id)

    def disable(self) -> None:
        """Disable unattended access."""
        self._config.enabled = False
        self._config.fixed_password = ""
        self._config.allowed_peers.clear()
        self._save()
        logger.info("Unattended access disabled")

    def set_master_password(self, password: str) -> None:
        """Set a master password to lock the unattended config."""
        self._config.master_password_hash = hash_password(password)
        self._config.require_master_password = True
        self._save()
        logger.info("Master password set")

    def verify_master_password(self, password: str) -> bool:
        """Verify the master password."""
        if not self._config.require_master_password:
            return True
        return verify_password(password, self._config.master_password_hash)

    # ── access control ──────────────────────────────────────────────

    def is_allowed(self, peer_id: str, password: str) -> bool:
        """Check if a peer is allowed to connect.

        Parameters
        ----------
        peer_id : str
            The remote peer's identifier.
        password : str
            The password provided by the peer.

        Returns
        -------
        bool
            ``True`` if access should be granted.
        """
        if not self._config.enabled:
            return False

        # Check allowlist
        if self._config.allowed_peers:
            if peer_id not in self._config.allowed_peers:
                logger.warning("Peer %s not in allowlist", peer_id)
                return False

        # Check password
        if self._config.fixed_password:
            return verify_password(password, self._config.fixed_password)

        return True

    def add_allowed_peer(self, peer_id: str) -> None:
        """Add a peer to the allowlist."""
        if peer_id not in self._config.allowed_peers:
            self._config.allowed_peers.append(peer_id)
            self._save()
            logger.info("Peer added to allowlist: %s", peer_id)

    def remove_allowed_peer(self, peer_id: str) -> None:
        """Remove a peer from the allowlist."""
        if peer_id in self._config.allowed_peers:
            self._config.allowed_peers.remove(peer_id)
            self._save()
            logger.info("Peer removed from allowlist: %s", peer_id)

    def rotate_session_id(self) -> str:
        """Generate a new session ID (invalidates the old one)."""
        self._config.session_id = generate_session_id()
        self._save()
        logger.info("Session ID rotated: %s", self._config.session_id)
        return self._config.session_id

    # ── persistence ─────────────────────────────────────────────────

    def _load(self) -> None:
        if not self._config_path.exists():
            return
        try:
            data = json.loads(self._config_path.read_text())
            self._config = UnattendedConfig(
                enabled=data.get("enabled", False),
                fixed_password=data.get("fixed_password", ""),
                auto_accept=data.get("auto_accept", True),
                preserve_session_id=data.get("preserve_session_id", True),
                session_id=data.get("session_id", ""),
                allowed_peers=data.get("allowed_peers", []),
                require_master_password=data.get("require_master_password", False),
                master_password_hash=data.get("master_password_hash", ""),
            )
            logger.debug("Unattended config loaded")
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning("Failed to load unattended config: %s", e)

    def _save(self) -> None:
        self._config_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "enabled": self._config.enabled,
            "fixed_password": self._config.fixed_password,
            "auto_accept": self._config.auto_accept,
            "preserve_session_id": self._config.preserve_session_id,
            "session_id": self._config.session_id,
            "allowed_peers": self._config.allowed_peers,
            "require_master_password": self._config.require_master_password,
            "master_password_hash": self._config.master_password_hash,
        }
        self._config_path.write_text(json.dumps(data, indent=2))
        logger.debug("Unattended config saved")
