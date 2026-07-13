"""
Local device registry — persistent list of known devices.

Each device has a permanent ``device_id`` (UUID) and a mutable
``device_name``.  The registry keeps track of which devices are
currently online (connected to the relay) and maintains a local
list of pre-authorized devices (trusted for password-less access).

Data is persisted to ``~/.opendesk/device_registry.json``.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class DeviceEntry:
    """A known device."""

    device_id: str
    device_name: str = ""
    last_seen: float = 0.0
    online: bool = False
    trusted: bool = False  # pre-authorized
    session_id: str = ""  # current session (if online)


_REGISTRY_PATH = Path.home() / ".opendesk" / "device_registry.json"


class DeviceRegistry:
    """Persistent registry of known devices.

    Thread-safe for reads; writes are serialised via a lock.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or _REGISTRY_PATH
        self._devices: dict[str, DeviceEntry] = {}
        self._load()

    # ── read ────────────────────────────────────────────────────────

    def get(self, device_id: str) -> DeviceEntry | None:
        """Look up a device by ID."""
        return self._devices.get(device_id)

    def all(self) -> list[DeviceEntry]:
        """Return all known devices (online first, then by name)."""
        devices = sorted(
            self._devices.values(),
            key=lambda d: (not d.online, d.device_name.lower() or d.device_id),
        )
        return devices

    def online(self) -> list[DeviceEntry]:
        """Return only devices currently marked as online."""
        return [d for d in self._devices.values() if d.online]

    def trusted(self) -> list[DeviceEntry]:
        """Return all pre-authorized devices."""
        return [d for d in self._devices.values() if d.trusted]

    def is_trusted(self, device_id: str) -> bool:
        """Check if a device is pre-authorized."""
        entry = self._devices.get(device_id)
        return entry is not None and entry.trusted

    def find(self, query: str) -> list[DeviceEntry]:
        """Cerca dispositivi per UUID (anche parziale) o nome.

        Parameters
        ----------
        query : str
            UUID completo, prefisso UUID, o parte del nome.

        Returns
        -------
        list[DeviceEntry]
            Dispositivi che matchano (ordinati per rilevanza).
        """
        q = query.lower().strip()
        if not q:
            return []

        results: list[tuple[DeviceEntry, int]] = []

        for d in self._devices.values():
            did = d.device_id.lower()
            dname = d.device_name.lower()

            # Match esatto UUID → priorità massima
            if did == q:
                results.append((d, 0))
            # Match prefisso UUID
            elif did.startswith(q):
                results.append((d, 1))
            # Match nome contiene
            elif q in dname:
                results.append((d, 2))
            # Match nome inizia con
            elif dname.startswith(q):
                results.append((d, 3))

        # Ordina per rilevanza (priorità crescente)
        results.sort(key=lambda x: x[1])
        return [entry for entry, _ in results]

    # ── write ───────────────────────────────────────────────────────

    def upsert(self, device_id: str, *, _save: bool = True, **kwargs: Any) -> DeviceEntry:
        """Add or update a device entry.

        Parameters
        ----------
        device_id : str
            Permanent device ID.
        _save : bool
            If ``False``, skip writing to disk (useful for batch ops).
        **kwargs
            Fields to update (device_name, online, trusted, session_id,
            last_seen).

        Returns
        -------
        DeviceEntry
            The updated entry.
        """
        entry = self._devices.get(device_id)
        if entry is None:
            entry = DeviceEntry(
                device_id=device_id,
                device_name=kwargs.pop("device_name", device_id[:8]),
            )
            self._devices[device_id] = entry

        for key, value in kwargs.items():
            if hasattr(entry, key) and value is not None:
                setattr(entry, key, value)

        if not kwargs.get("last_seen"):
            entry.last_seen = time.time()

        if _save:
            self._save()
        return entry

    def remove(self, device_id: str) -> bool:
        """Remove a device from the registry.

        Returns ``True`` if the entry existed.
        """
        if device_id in self._devices:
            del self._devices[device_id]
            self._save()
            return True
        return False

    def set_trusted(self, device_id: str, trusted: bool) -> None:
        """Mark/unmark a device as pre-authorized."""
        if device_id in self._devices:
            self._devices[device_id].trusted = trusted
            self._save()

    def merge_from_relay(self, devices: list[dict]) -> None:
        """Merge device list received from the relay.

        Devices present in the relay list are marked online; those
        not present but known locally remain offline.
        """
        # Mark all currently known devices as offline
        for entry in self._devices.values():
            entry.online = False

        # Update from relay list (batch: no intermediate saves)
        for d in devices:
            device_id = d.get("device_id", "")
            if not device_id:
                continue
            self.upsert(
                device_id,
                _save=False,
                device_name=d.get("device_name", ""),
                online=True,
                session_id=d.get("session_id", ""),
            )

        self._save()

    # ── persistence ─────────────────────────────────────────────────

    def _load(self) -> None:
        """Load registry from disk."""
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text())
            for item in data.get("devices", []):
                entry = DeviceEntry(**item)
                self._devices[entry.device_id] = entry
            logger.info("Loaded %d known devices from %s", len(self._devices), self._path)
        except Exception as e:
            logger.warning("Failed to load device registry: %s", e)

    def _save(self) -> None:
        """Save registry to disk."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "devices": [
                asdict(entry) for entry in self._devices.values()
            ],
        }
        self._path.write_text(json.dumps(data, indent=2))
