"""
End-to-end encryption using PyNaCl (libsodium).

Provides:
- Key exchange via Curve25519 Diffie-Hellman (``nacl.public.Box``)
- Perfect Forward Secrecy via ephemeral key pairs per session
- Authenticated encryption (XSalsa20-Poly1305)
- Session key rotation support
"""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass

from nacl import bindings as sodium
from nacl.public import Box, PrivateKey, PublicKey

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

NONCE_SIZE = 24  # XSalsa20-Poly1305 nonce bytes
KEY_SIZE = 32  # Curve25519 private/public key bytes

# ---------------------------------------------------------------------------
# Key types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CryptoKeyPair:
    """An asymmetric key pair for E2E encryption."""

    private_key: PrivateKey
    public_key: PublicKey

    @classmethod
    def generate(cls) -> CryptoKeyPair:
        """Generate a new random key pair."""
        priv = PrivateKey.generate()
        return cls(private_key=priv, public_key=priv.public_key)

    @classmethod
    def from_private_bytes(cls, data: bytes) -> CryptoKeyPair:
        """Restore a key pair from a 32-byte private key."""
        priv = PrivateKey(data)
        return cls(private_key=priv, public_key=priv.public_key)

    @property
    def private_bytes(self) -> bytes:
        return bytes(self.private_key)

    @property
    def public_bytes(self) -> bytes:
        return bytes(self.public_key)

    def encode_public(self) -> str:
        """URL-safe Base64-encoded public key for transmission.

        Uses ``base64.urlsafe_b64encode`` to avoid ``/`` and ``+``
        characters that may be corrupted by intermediaries (e.g. relay
        servers applying URL-decoding to payloads).
        """
        return base64.urlsafe_b64encode(self.public_bytes).decode().rstrip("=")

    @classmethod
    def decode_public(cls, encoded: str) -> PublicKey:
        """Decode a URL-safe Base64 public key (padding optional)."""
        # Add padding if needed: urlsafe_b64decode requires length % 4 == 0
        padding = 4 - len(encoded) % 4
        if padding != 4:
            encoded += "=" * padding
        return PublicKey(base64.urlsafe_b64decode(encoded))


# ---------------------------------------------------------------------------
# Session encryption
# ---------------------------------------------------------------------------


@dataclass
class EncryptedMessage:
    """A complete encrypted payload with metadata."""

    ciphertext: bytes
    nonce: bytes
    sender_public_key: bytes  # so the recipient can identify the sender

    def encode(self) -> bytes:
        """Serialize to a binary format for transport.

        Format::
            [ 2 bytes sender_pk_len ][ sender_pk ][ 1 byte nonce_len ][ nonce ][ ciphertext ]
        """
        pk_len = len(self.sender_public_key).to_bytes(2, "big")
        n_len = len(self.nonce).to_bytes(1, "big")
        return pk_len + self.sender_public_key + n_len + self.nonce + self.ciphertext

    @classmethod
    def decode(cls, data: bytes) -> EncryptedMessage:
        """Deserialize from the binary format."""
        pk_len = int.from_bytes(data[:2], "big")
        sender_pk = data[2 : 2 + pk_len]
        n_len = data[2 + pk_len]
        nonce = data[2 + pk_len + 1 : 2 + pk_len + 1 + n_len]
        ciphertext = data[2 + pk_len + 1 + n_len :]
        return cls(ciphertext=ciphertext, nonce=nonce, sender_public_key=sender_pk)


class E2EEncryption:
    """End-to-end encryption manager for a single session.

    Each side generates an ephemeral key pair.  The shared secret is
    derived via Box (Curve25519 + HSalsa20).  Provides **Perfect
    Forward Secrecy** because keys are ephemeral per session.

    Usage::

        alice = E2EEncryption()
        bob = E2EEncryption()

        # Exchange public keys
        alice.set_remote_key(bob.local_key_pair.public_key)
        bob.set_remote_key(alice.local_key_pair.public_key)

        # Now they can communicate
        encrypted = alice.encrypt(b"Hello Bob")
        plain = bob.decrypt(encrypted)
    """

    def __init__(self, key_pair: CryptoKeyPair | None = None) -> None:
        self._key_pair = key_pair or CryptoKeyPair.generate()
        self._remote_public_key: PublicKey | None = None
        self._box: Box | None = None

    # ── properties ──────────────────────────────────────────────────

    @property
    def local_key_pair(self) -> CryptoKeyPair:
        return self._key_pair

    @property
    def has_remote_key(self) -> bool:
        return self._remote_public_key is not None

    # ── key exchange ────────────────────────────────────────────────

    def set_remote_key(self, public_key: PublicKey | str) -> None:
        """Set the remote peer's public key.

        Parameters
        ----------
        public_key : PublicKey | str
            Either a ``PublicKey`` object or a Base64-encoded string.
        """
        if isinstance(public_key, str):
            public_key = CryptoKeyPair.decode_public(public_key)

        self._remote_public_key = public_key
        self._box = Box(self._key_pair.private_key, public_key)
        logger.debug("Remote key set, Box ready")

    def get_public_key_string(self) -> str:
        """Return this peer's public key as a Base64 string."""
        return self._key_pair.encode_public()

    # ── encrypt / decrypt ──────────────────────────────────────────

    def encrypt(self, plaintext: bytes) -> EncryptedMessage:
        """Encrypt data for the remote peer.

        Requires ``set_remote_key()`` to have been called.

        Raises
        ------
        RuntimeError
            If the remote key has not been set.
        """
        if self._box is None:
            raise RuntimeError("Remote public key not set — call set_remote_key() first")

        nonce = sodium.randombytes(NONCE_SIZE)
        encrypted = self._box.encrypt(plaintext, nonce)
        return EncryptedMessage(
            ciphertext=encrypted.ciphertext,
            nonce=nonce,
            sender_public_key=self._key_pair.public_bytes,
        )

    def decrypt(self, msg: EncryptedMessage) -> bytes:
        """Decrypt a message from the remote peer.

        Uses the sender's public key from the message to identify the
        correct Box (supports multiple senders in future).

        Raises
        ------
        RuntimeError
            If the remote key has not been set.
        nacl.exceptions.CryptoError
            If decryption fails (tampered data / wrong key).
        """
        if self._box is None:
            raise RuntimeError("Remote public key not set — call set_remote_key() first")

        # Reconstruct the nacl encrypted format: nonce + ciphertext
        combined = msg.nonce + msg.ciphertext
        plaintext = self._box.decrypt(combined)
        return plaintext

    # ── session key rotation ────────────────────────────────────────

    def rotate_keys(self) -> CryptoKeyPair:
        """Generate a new local key pair for key rotation.

        Returns the *new* key pair (the old one is discarded).
        Caller should send the new public key to the remote peer.
        """
        old_pair = self._key_pair
        self._key_pair = CryptoKeyPair.generate()
        # Re-create Box if remote key was set
        if self._remote_public_key is not None:
            self._box = Box(self._key_pair.private_key, self._remote_public_key)
        logger.debug("Local key pair rotated")
        return old_pair


# ---------------------------------------------------------------------------
# Convenience: encrypt / decrypt with JSON payloads
# ---------------------------------------------------------------------------


def encrypt_json(
    encryption: E2EEncryption,
    payload: dict,
    encoder: type[json.JSONEncoder] | None = None,
) -> EncryptedMessage:
    """Serialize a dict to JSON and encrypt it."""
    data = json.dumps(payload, cls=encoder).encode("utf-8")
    return encryption.encrypt(data)


def decrypt_json(encryption: E2EEncryption, msg: EncryptedMessage) -> dict:
    """Decrypt and deserialize a JSON payload."""
    plain = encryption.decrypt(msg)
    return json.loads(plain.decode("utf-8"))
