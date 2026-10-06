"""
Tests for the cryptography module.

Covers E2E encryption roundtrip, key exchange, and password hashing.
"""

from __future__ import annotations

import queue

from opendesk.crypto.auth import generate_session_id, hash_password, verify_password
from opendesk.crypto.e2ee import CryptoKeyPair, E2EEncryption, EncryptedMessage
from opendesk.network.protocol import Message, MessageType
from opendesk.network.relay_client import RelayRole, _RelaySession

# ── E2E encryption ────────────────────────────────────────────────────────


class TestE2EEncryption:
    def test_key_pair_generation(self) -> None:
        """A generated key pair should have 32-byte keys."""
        kp = CryptoKeyPair.generate()
        assert len(kp.private_bytes) == 32
        assert len(kp.public_bytes) == 32

    def test_public_key_encoding(self) -> None:
        """Base64 encode/decode of public key."""
        kp = CryptoKeyPair.generate()
        encoded = kp.encode_public()
        assert isinstance(encoded, str)
        assert len(encoded) > 40  # base64 of 32 bytes

        decoded = CryptoKeyPair.decode_public(encoded)
        assert bytes(decoded) == kp.public_bytes

    def test_encrypt_decrypt_roundtrip(self) -> None:
        """Alice encrypts → Bob decrypts successfully."""
        alice = E2EEncryption()
        bob = E2EEncryption()

        alice.set_remote_key(bob.local_key_pair.public_key)
        bob.set_remote_key(alice.local_key_pair.public_key)

        plaintext = b"Hello, this is a secret message!"
        encrypted = alice.encrypt(plaintext)
        decrypted = bob.decrypt(encrypted)

        assert decrypted == plaintext

    def test_encrypted_message_serialisation(self) -> None:
        """EncryptedMessage encode/decode roundtrip."""
        alice = E2EEncryption()
        bob = E2EEncryption()
        alice.set_remote_key(bob.local_key_pair.public_key)
        bob.set_remote_key(alice.local_key_pair.public_key)

        original = alice.encrypt(b"serialise me")
        data = original.encode()
        restored = EncryptedMessage.decode(data)

        assert restored.ciphertext == original.ciphertext
        assert restored.nonce == original.nonce
        assert restored.sender_public_key == original.sender_public_key

    def test_wrong_key_fails(self) -> None:
        """Decrypting with a different key should raise an error."""
        alice = E2EEncryption()
        bob = E2EEncryption()
        eve = E2EEncryption()

        alice.set_remote_key(bob.local_key_pair.public_key)
        bob.set_remote_key(alice.local_key_pair.public_key)

        encrypted = alice.encrypt(b"secret")
        # Eve tries to decrypt with her own key
        eve.set_remote_key(alice.local_key_pair.public_key)

        import nacl.exceptions

        try:
            eve.decrypt(encrypted)
            assert False, "Should have raised CryptoError"
        except nacl.exceptions.CryptoError:
            pass  # expected

    def test_relay_envelope_requires_authenticated_key_exchange(self) -> None:
        """Peer payloads are unreadable until both password proofs validate."""
        alice = _RelaySession(
            "relay", 1, "session", "shared-secret", RelayRole.HOST, queue.Queue()
        )
        bob = _RelaySession(
            "relay", 1, "session", "shared-secret", RelayRole.CLIENT, queue.Queue()
        )

        assert bob._accept_remote_key(
            alice._key_exchange_message(MessageType.KEY_EXCHANGE).payload
        )
        assert alice._accept_remote_key(
            bob._key_exchange_message(MessageType.KEY_EXCHANGE_ACK).payload
        )

        encrypted = alice._encrypt_peer_message(Message.chat_message("private"))
        assert encrypted.encrypted is True
        assert encrypted.payload["_e2ee"] != b"private"
        assert bob._decrypt_peer_message(encrypted).payload == {"text": "private"}

    def test_relay_envelope_rejects_invalid_key_proof(self) -> None:
        """A relay cannot substitute a public key without the session password."""
        session = _RelaySession(
            "relay", 1, "session", "shared-secret", RelayRole.CLIENT, queue.Queue()
        )
        msg = Message.key_exchange(E2EEncryption().get_public_key_string())
        msg.payload["proof"] = "forged"
        assert not session._accept_remote_key(msg.payload)

    def test_trusted_device_per_device_secret_roundtrip(self) -> None:
        """Trusted (password-less) peers establish E2E via the per-device secret."""
        host = _RelaySession(
            "relay",
            1,
            "session",
            "host-password",
            RelayRole.HOST,
            queue.Queue(),
            device_id="host-1",
            trusted_devices={"client-1": "device-secret"},
        )
        client = _RelaySession(
            "relay",
            1,
            "session",
            "",
            RelayRole.CLIENT,
            queue.Queue(),
            device_id="client-1",
        )
        # The host sets this when authenticating a trusted device.
        host._proof_secret = "device-secret"

        assert client._accept_remote_key(
            host._key_exchange_message(MessageType.KEY_EXCHANGE).payload
        )
        assert host._accept_remote_key(
            client._key_exchange_message(MessageType.KEY_EXCHANGE_ACK).payload
        )

        encrypted = host._encrypt_peer_message(Message.chat_message("private"))
        assert encrypted.encrypted is True
        assert client._decrypt_peer_message(encrypted).payload == {"text": "private"}

    def test_trusted_device_rejects_changed_secret(self) -> None:
        """A pinned per-device secret cannot be silently replaced."""
        client = _RelaySession(
            "relay",
            1,
            "session",
            "",
            RelayRole.CLIENT,
            queue.Queue(),
            device_id="client-1",
            known_device_secrets={"host-1": "pinned"},
        )
        host = _RelaySession(
            "relay",
            1,
            "session",
            "host-password",
            RelayRole.HOST,
            queue.Queue(),
            device_id="host-1",
        )
        host._proof_secret = "rotated"
        assert not client._accept_remote_key(
            host._key_exchange_message(MessageType.KEY_EXCHANGE).payload
        )

    def test_passwordless_accepts_legacy_unverified_key(self) -> None:
        """A password-less peer accepts a legacy host key that has no proof."""
        client = _RelaySession(
            "relay", 1, "session", "", RelayRole.CLIENT, queue.Queue(), device_id="client-1"
        )
        legacy = Message.key_exchange(E2EEncryption().get_public_key_string())
        assert client._accept_remote_key(legacy.payload)

    def test_key_rotation(self) -> None:
        """After key rotation, old ciphertexts should not decrypt."""
        alice = E2EEncryption()
        bob = E2EEncryption()
        alice.set_remote_key(bob.local_key_pair.public_key)
        bob.set_remote_key(alice.local_key_pair.public_key)

        encrypted = alice.encrypt(b"before rotation")

        # Rotate keys and exchange new ones
        alice.rotate_keys()
        bob.set_remote_key(alice.local_key_pair.public_key)

        # Old ciphertext should fail
        import nacl.exceptions

        try:
            bob.decrypt(encrypted)
            assert False, "Old key should not work after rotation"
        except nacl.exceptions.CryptoError:
            pass


# ── Password hashing ──────────────────────────────────────────────────────


class TestPasswordHashing:
    def test_hash_and_verify(self) -> None:
        h = hash_password("MySecurePass123!")
        assert verify_password("MySecurePass123!", h)
        assert not verify_password("WrongPassword", h)

    def test_hash_format(self) -> None:
        h = hash_password("test")
        assert h.startswith("$argon2id$"), f"Unexpected format: {h[:20]}"

    def test_session_id_format(self) -> None:
        sid = generate_session_id()
        assert len(sid) == 11  # "123 456 789"
        assert sid.count(" ") == 2
        parts = sid.split()
        assert len(parts) == 3
        assert all(len(p) == 3 for p in parts)
        assert all(p.isdigit() for p in parts)
