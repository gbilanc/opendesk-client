"""Tests for the per-device secret handling in DeviceRegistry."""

from __future__ import annotations

from pathlib import Path

from opendesk.core.device_registry import DeviceRegistry


def _registry(tmp_path: Path) -> DeviceRegistry:
    return DeviceRegistry(tmp_path / "registry.json")


def test_trust_generates_and_persists_secret(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    reg.upsert("dev-a", device_name="A")
    reg.set_trusted("dev-a", True)

    secret = reg.trusted_secrets()["dev-a"]
    assert len(secret) == 64
    assert DeviceRegistry(tmp_path / "registry.json").trusted_secrets()["dev-a"] == secret


def test_revoke_forgets_and_retrust_rotates(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    reg.upsert("dev-a", device_name="A")
    reg.set_trusted("dev-a", True)
    first = reg.trusted_secrets()["dev-a"]

    reg.set_trusted("dev-a", False)
    assert reg.trusted_secrets() == {}

    reg.set_trusted("dev-a", True)
    assert reg.trusted_secrets()["dev-a"] != first


def test_peer_secret_roundtrip_and_forget(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    reg.set_peer_secret("host-1", "abc")
    assert reg.known_peer_secrets() == {"host-1": "abc"}

    reg.forget_secrets("host-1")
    assert reg.known_peer_secrets() == {}
