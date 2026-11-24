"""Cryptographic primitives compatible with WorkNotes Encrypted EPUB Reader vault format."""
from __future__ import annotations

import base64
import gzip
import hashlib
import json
import math
import os
from typing import Any, Dict

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class CryptoAuthError(Exception):
    """Raised when decryption, passphrase verification, or integrity checks fail."""

    def __init__(self, message: str = "Authentication or integrity verification failed"):
        super().__init__(message)


DEFAULT_SCRYPT_PARAMS: Dict[str, int] = {
    "N": 32768,
    "r": 8,
    "p": 1,
    "keyLen": 32,
}

IV_BYTES = 12
AUTH_TAG_BYTES = 16
PORTABLE_SALT_BYTES = 32
PORTABLE_HEADER_BYTES = PORTABLE_SALT_BYTES + 3  # salt(32) + N_log2(1) + r(1) + p(1)
PORTABLE_AAD = "epub-reader:portable:v1"


def zero_bytearray(buf: bytearray | memoryview | None) -> None:
    """Zero mutable byte buffer in place."""
    if buf is not None:
        for i in range(len(buf)):
            buf[i] = 0


def sha256_hex(data: bytes | bytearray | memoryview) -> str:
    """Compute lowercase hex SHA-256 digest."""
    return hashlib.sha256(data).hexdigest()


def derive_key_scrypt(
    passphrase: str,
    salt: bytes,
    params: Dict[str, int] | None = None,
) -> bytes:
    """Derive a 32-byte key from a UTF-8 passphrase using scrypt."""
    p = params or DEFAULT_SCRYPT_PARAMS
    return hashlib.scrypt(
        passphrase.encode("utf-8"),
        salt=bytes(salt),
        n=int(p["N"]),
        r=int(p["r"]),
        p=int(p["p"]),
        maxmem=128 * 1024 * 1024,
        dklen=int(p.get("keyLen", 32)),
    )


def encrypt_aes256_gcm(plaintext: bytes, key: bytes, aad: str) -> bytes:
    """Encrypt with AES-256-GCM returning IV(12B) || Ciphertext || AuthTag(16B)."""
    if len(key) != 32:
        raise ValueError("AES-256-GCM requires a 32-byte key")
    iv = os.urandom(IV_BYTES)
    aesgcm = AESGCM(bytes(key))
    ct_and_tag = aesgcm.encrypt(iv, bytes(plaintext), aad.encode("utf-8"))
    return iv + ct_and_tag


def decrypt_aes256_gcm(envelope: bytes, key: bytes, aad: str) -> bytes:
    """Decrypt IV(12B) || Ciphertext || AuthTag(16B) envelope with AES-256-GCM."""
    if len(key) != 32:
        raise ValueError("AES-256-GCM requires a 32-byte key")
    if len(envelope) < IV_BYTES + AUTH_TAG_BYTES:
        raise CryptoAuthError("Encrypted envelope is truncated")
    iv = bytes(envelope[:IV_BYTES])
    ct_and_tag = bytes(envelope[IV_BYTES:])
    try:
        aesgcm = AESGCM(bytes(key))
        return aesgcm.decrypt(iv, ct_and_tag, aad.encode("utf-8"))
    except (InvalidTag, ValueError) as exc:
        raise CryptoAuthError("Decryption failed: invalid key or corrupted ciphertext") from exc


def pack_portable_vault(
    bundle: Dict[str, Any],
    passphrase: str,
    params: Dict[str, int] | None = None,
) -> bytes:
    """Pack EPUB bytes + metadata + study state into an opaque encrypted .vault archive."""
    p = params or DEFAULT_SCRYPT_PARAMS
    n_val = int(p["N"])
    n_log2 = int(round(math.log2(n_val)))
    if (1 << n_log2) != n_val or n_log2 < 10 or n_log2 > 18:
        raise ValueError("Invalid scrypt N parameter for portable vault")

    salt = os.urandom(PORTABLE_SALT_BYTES)
    key = bytearray(derive_key_scrypt(passphrase, salt, p))
    try:
        epub_b64 = base64.b64encode(bundle["epubBytes"]).decode("ascii")
        serialized = json.dumps(
            {
                "version": 1,
                "metadata": bundle["metadata"],
                "epubBase64": epub_b64,
                "state": bundle["state"],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        compressed = gzip.compress(serialized)
        encrypted = encrypt_aes256_gcm(compressed, bytes(key), PORTABLE_AAD)
        header = salt + bytes([n_log2, int(p["r"]), int(p["p"])])
        return header + encrypted
    finally:
        zero_bytearray(key)


def unpack_portable_vault(archive: bytes, passphrase: str) -> Dict[str, Any]:
    """Unpack and verify an encrypted .vault archive."""
    if len(archive) < PORTABLE_HEADER_BYTES + IV_BYTES + AUTH_TAG_BYTES + 2:
        raise CryptoAuthError("Portable vault archive is invalid or truncated")

    salt = archive[:PORTABLE_SALT_BYTES]
    n_log2 = archive[PORTABLE_SALT_BYTES]
    r_val = archive[PORTABLE_SALT_BYTES + 1]
    p_val = archive[PORTABLE_SALT_BYTES + 2]
    if n_log2 < 10 or n_log2 > 18 or r_val < 1 or r_val > 16 or p_val < 1 or p_val > 4:
        raise CryptoAuthError("Unsupported KDF parameters in portable vault archive")

    params = {"N": 1 << n_log2, "r": r_val, "p": p_val, "keyLen": 32}
    key = bytearray(derive_key_scrypt(passphrase, salt, params))
    try:
        decrypted_compressed = decrypt_aes256_gcm(
            archive[PORTABLE_HEADER_BYTES:],
            bytes(key),
            PORTABLE_AAD,
        )
        try:
            decompressed = gzip.decompress(decrypted_compressed)
            parsed = json.loads(decompressed.decode("utf-8"))
        except Exception as exc:
            raise CryptoAuthError("Malformed portable vault payload") from exc

        if (
            not isinstance(parsed, dict)
            or parsed.get("version") != 1
            or not parsed.get("epubBase64")
            or not isinstance(parsed.get("metadata"), dict)
            or not isinstance(parsed.get("state"), dict)
        ):
            raise CryptoAuthError("Malformed portable vault payload")

        epub_bytes = base64.b64decode(parsed["epubBase64"])
        if sha256_hex(epub_bytes) != parsed["metadata"].get("epubSha256"):
            raise CryptoAuthError("Portable vault EPUB SHA-256 digest mismatch")

        return {
            "version": 1,
            "metadata": parsed["metadata"],
            "epubBytes": epub_bytes,
            "state": parsed["state"],
        }
    finally:
        zero_bytearray(key)
