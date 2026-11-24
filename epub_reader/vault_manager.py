"""Encrypted EPUB Vault Manager compatible with WorkNotes vault/ layout."""
from __future__ import annotations

import datetime
import json
import os
import re
import shutil
import threading
import uuid
from typing import Any, Callable, Dict, List, Optional

from .crypto_primitives import (
    DEFAULT_SCRYPT_PARAMS,
    CryptoAuthError,
    decrypt_aes256_gcm,
    derive_key_scrypt,
    encrypt_aes256_gcm,
    pack_portable_vault,
    sha256_hex,
    unpack_portable_vault,
    zero_bytearray,
)
from .dpapi import WindowsDpapiProvider


def _utc_iso_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def _atomic_write_file(file_path: str, content: bytes | str) -> None:
    os.makedirs(os.path.dirname(file_path), exist_ok=True)
    tmp_path = f"{file_path}.tmp-{os.urandom(8).hex()}"
    data = content.encode("utf-8") if isinstance(content, str) else bytes(content)
    with open(tmp_path, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp_path, file_path)


def _secure_overwrite_and_delete(file_path: str) -> None:
    if not os.path.exists(file_path):
        return
    try:
        size = os.path.getsize(file_path)
        if size > 0:
            zeros = b"\x00" * min(size, 64 * 1024)
            with open(file_path, "r+b") as fh:
                written = 0
                while written < size:
                    chunk_len = min(len(zeros), size - written)
                    fh.seek(written)
                    fh.write(zeros[:chunk_len])
                    written += chunk_len
                fh.flush()
                os.fsync(fh.fileno())
    except OSError:
        pass
    try:
        os.remove(file_path)
    except OSError:
        pass


class VaultLockedError(Exception):
    """Raised when an operation requires an unlocked vault."""

    def __init__(self, message: str = "Vault is currently locked"):
        super().__init__(message)


class KeyNotFoundBookError(KeyError):
    """Raised when a book ID does not exist in the unlocked catalog."""


class VaultManager:
    """Thread-safe envelope-encrypted EPUB library vault."""

    _OBJ_ID_RE = re.compile(r"^[0-9a-f]{32}$")

    def __init__(
        self,
        vault_dir: str,
        dpapi: Optional[WindowsDpapiProvider] = None,
        scrypt_params: Optional[Dict[str, int]] = None,
    ):
        self.vault_dir = os.path.abspath(vault_dir)
        self.dpapi = dpapi or WindowsDpapiProvider()
        self.scrypt_params = dict(scrypt_params or DEFAULT_SCRYPT_PARAMS)
        self._mvk: Optional[bytearray] = None
        self._vault_id: Optional[str] = None
        self._catalog: Optional[Dict[str, Any]] = None
        self._lock = threading.RLock()

    @property
    def meta_path(self) -> str:
        return os.path.join(self.vault_dir, "vault.meta")

    @property
    def dpapi_path(self) -> str:
        return os.path.join(self.vault_dir, "dpapi.bin")

    @property
    def catalog_path(self) -> str:
        return os.path.join(self.vault_dir, "catalog.enc")

    @property
    def objects_dir(self) -> str:
        return os.path.join(self.vault_dir, "objects")

    def _object_path(self, obj_id: str) -> str:
        if not self._OBJ_ID_RE.match(obj_id or ""):
            raise ValueError("Invalid object identifier")
        return os.path.join(self.objects_dir, f"{obj_id}.bin")

    def is_initialized(self) -> bool:
        return os.path.isfile(self.meta_path) and os.path.isfile(self.catalog_path)

    def is_unlocked(self) -> bool:
        with self._lock:
            return self._mvk is not None and self._catalog is not None and self._vault_id is not None

    def is_dpapi_supported(self) -> bool:
        return self.dpapi.is_supported()

    def has_dpapi_remember_file(self) -> bool:
        return self.dpapi.is_supported() and os.path.isfile(self.dpapi_path)

    @staticmethod
    def _dpapi_entropy(vault_id: str) -> bytes:
        return f"epub-reader:dpapi:v1:{vault_id}".encode("utf-8")

    def _sweep_temp_files(self) -> None:
        for directory in (self.vault_dir, self.objects_dir):
            if not os.path.isdir(directory):
                continue
            for name in os.listdir(directory):
                if ".tmp-" in name:
                    try:
                        os.remove(os.path.join(directory, name))
                    except OSError:
                        pass

    def initialize(self, passphrase: str, remember_on_this_pc: bool = False) -> None:
        with self._lock:
            if self.is_initialized():
                raise ValueError("Vault is already initialized")
            os.makedirs(self.objects_dir, exist_ok=True)

            vault_id = str(uuid.uuid4())
            salt = os.urandom(32)
            mvk = bytearray(os.urandom(32))
            kek = bytearray(derive_key_scrypt(passphrase, salt, self.scrypt_params))
            try:
                wrapped_mvk = encrypt_aes256_gcm(
                    bytes(mvk), bytes(kek), f"epub-reader:mvk:v1:{vault_id}"
                )
                meta = {
                    "version": 1,
                    "vaultId": vault_id,
                    "kdf": {
                        "algorithm": "scrypt",
                        "saltHex": salt.hex(),
                        "N": self.scrypt_params["N"],
                        "r": self.scrypt_params["r"],
                        "p": self.scrypt_params["p"],
                        "keyLen": 32,
                    },
                    "wrappedMvkHex": wrapped_mvk.hex(),
                }
                empty_catalog: Dict[str, Any] = {"version": 1, "books": []}
                encrypted_catalog = encrypt_aes256_gcm(
                    json.dumps(empty_catalog).encode("utf-8"),
                    bytes(mvk),
                    f"epub-reader:catalog:v1:{vault_id}",
                )

                _atomic_write_file(self.meta_path, json.dumps(meta, indent=2))
                _atomic_write_file(self.catalog_path, encrypted_catalog)

                zero_bytearray(self._mvk)
                self._mvk = bytearray(mvk)
                self._vault_id = vault_id
                self._catalog = empty_catalog

                if remember_on_this_pc and self.dpapi.is_supported():
                    protected_blob = self.dpapi.protect(bytes(mvk), self._dpapi_entropy(vault_id))
                    _atomic_write_file(self.dpapi_path, protected_blob)
            finally:
                zero_bytearray(kek)
                zero_bytearray(mvk)

    def unlock_with_passphrase(self, passphrase: str, remember_on_this_pc: bool = False) -> None:
        with self._lock:
            if not self.is_initialized():
                raise ValueError("Vault is not initialized")
            self._sweep_temp_files()
            with open(self.meta_path, "r", encoding="utf-8") as fh:
                meta = json.load(fh)

            kdf = meta["kdf"]
            salt = bytes.fromhex(kdf["saltHex"])
            kek = bytearray(
                derive_key_scrypt(
                    passphrase,
                    salt,
                    {
                        "N": int(kdf["N"]),
                        "r": int(kdf["r"]),
                        "p": int(kdf["p"]),
                        "keyLen": int(kdf.get("keyLen", 32)),
                    },
                )
            )
            try:
                mvk = bytearray(
                    decrypt_aes256_gcm(
                        bytes.fromhex(meta["wrappedMvkHex"]),
                        bytes(kek),
                        f"epub-reader:mvk:v1:{meta['vaultId']}",
                    )
                )
                try:
                    self._load_catalog_with_mvk(mvk, meta["vaultId"])
                    if remember_on_this_pc and self.dpapi.is_supported():
                        protected_blob = self.dpapi.protect(
                            bytes(mvk), self._dpapi_entropy(meta["vaultId"])
                        )
                        _atomic_write_file(self.dpapi_path, protected_blob)
                except Exception:
                    zero_bytearray(mvk)
                    raise
            finally:
                zero_bytearray(kek)

    def unlock_with_dpapi(self) -> None:
        with self._lock:
            if not self.is_initialized():
                raise ValueError("Vault is not initialized")
            if not self.has_dpapi_remember_file():
                raise CryptoAuthError("DPAPI auto-unlock is not enabled on this machine")
            self._sweep_temp_files()
            with open(self.meta_path, "r", encoding="utf-8") as fh:
                meta = json.load(fh)
            with open(self.dpapi_path, "rb") as fh:
                dpapi_blob = fh.read()
            try:
                mvk_bytes = self.dpapi.unprotect(dpapi_blob, self._dpapi_entropy(meta["vaultId"]))
            except Exception as exc:
                _secure_overwrite_and_delete(self.dpapi_path)
                raise CryptoAuthError(
                    "Windows DPAPI key unwrap failed; please unlock with your passphrase"
                ) from exc

            mvk = bytearray(mvk_bytes)
            try:
                self._load_catalog_with_mvk(mvk, meta["vaultId"])
            except Exception:
                zero_bytearray(mvk)
                raise

    def _load_catalog_with_mvk(self, mvk: bytearray, vault_id: str) -> None:
        with open(self.catalog_path, "rb") as fh:
            encrypted_catalog = fh.read()
        catalog_bytes = bytearray(
            decrypt_aes256_gcm(
                encrypted_catalog,
                bytes(mvk),
                f"epub-reader:catalog:v1:{vault_id}",
            )
        )
        try:
            parsed = json.loads(catalog_bytes.decode("utf-8"))
        finally:
            zero_bytearray(catalog_bytes)

        if not isinstance(parsed, dict) or parsed.get("version") != 1 or not isinstance(parsed.get("books"), list):
            raise CryptoAuthError("Corrupted catalog structure")

        zero_bytearray(self._mvk)
        self._mvk = mvk
        self._vault_id = vault_id
        self._catalog = parsed

    def set_dpapi_remember(self, enabled: bool) -> None:
        with self._lock:
            mvk, vault_id, _ = self._assert_unlocked()
            if not enabled:
                _secure_overwrite_and_delete(self.dpapi_path)
                return
            if not self.dpapi.is_supported():
                raise ValueError("Windows DPAPI is not supported on this platform")
            protected_blob = self.dpapi.protect(bytes(mvk), self._dpapi_entropy(vault_id))
            _atomic_write_file(self.dpapi_path, protected_blob)

    def lock(self) -> None:
        with self._lock:
            zero_bytearray(self._mvk)
            self._mvk = None
            self._vault_id = None
            self._catalog = None

    def _assert_unlocked(self):
        if self._mvk is None or self._vault_id is None or self._catalog is None:
            raise VaultLockedError()
        return self._mvk, self._vault_id, self._catalog

    def _persist_catalog(self) -> None:
        mvk, vault_id, catalog = self._assert_unlocked()
        plaintext = bytearray(json.dumps(catalog, ensure_ascii=False).encode("utf-8"))
        try:
            encrypted = encrypt_aes256_gcm(
                bytes(plaintext), bytes(mvk), f"epub-reader:catalog:v1:{vault_id}"
            )
            _atomic_write_file(self.catalog_path, encrypted)
        finally:
            zero_bytearray(plaintext)

    @staticmethod
    def _to_summary(entry: Dict[str, Any]) -> Dict[str, Any]:
        summary: Dict[str, Any] = {
            "bookId": entry["bookId"],
            "title": entry["title"],
            "author": entry["author"],
            "language": entry["language"],
            "chapterCount": entry["chapterCount"],
            "epubByteLength": entry["epubByteLength"],
            "progress": entry["progress"],
            "importedAt": entry["importedAt"],
        }
        if entry.get("description"):
            summary["description"] = entry["description"]
        if entry.get("coverMimeType") and entry.get("coverBase64"):
            summary["coverDataUrl"] = f"data:{entry['coverMimeType']};base64,{entry['coverBase64']}"
        if entry.get("lastReadAt"):
            summary["lastReadAt"] = entry["lastReadAt"]
        return summary

    def list_books(self) -> List[Dict[str, Any]]:
        with self._lock:
            _, _, catalog = self._assert_unlocked()
            sorted_entries = sorted(
                catalog["books"],
                key=lambda b: b.get("lastReadAt") or b.get("importedAt") or "",
                reverse=True,
            )
            return [self._to_summary(entry) for entry in sorted_entries]

    def _find_entry(self, book_id: str) -> Dict[str, Any]:
        _, _, catalog = self._assert_unlocked()
        for entry in catalog["books"]:
            if entry["bookId"] == book_id:
                return entry
        raise KeyNotFoundBookError(f"Book not found: {book_id}")

    def get_book_summary(self, book_id: str) -> Dict[str, Any]:
        with self._lock:
            return self._to_summary(self._find_entry(book_id))

    def get_book_original_filename(self, book_id: str) -> str:
        with self._lock:
            return self._find_entry(book_id)["originalFilename"]

    def import_book(self, data: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            _, _, catalog = self._assert_unlocked()
            book_id = str(uuid.uuid4())
            book_dek = bytearray(os.urandom(32))
            epub_obj_id = os.urandom(16).hex()
            state_obj_id = os.urandom(16).hex()
            now = _utc_iso_now()
            epub_bytes: bytes = bytes(data["epubBytes"])
            epub_sha256 = sha256_hex(epub_bytes)

            initial_state = data.get("initialState")
            if initial_state:
                state = dict(initial_state)
                state["version"] = 1
                state["bookId"] = book_id
            else:
                state = {
                    "version": 1,
                    "bookId": book_id,
                    "progress": {
                        "spineIndex": 0,
                        "scrollRatio": 0,
                        "completedChapters": [],
                        "updatedAt": now,
                    },
                    "bookmarks": [],
                    "annotations": [],
                    "quizzes": [],
                }

            try:
                encrypted_epub = encrypt_aes256_gcm(
                    epub_bytes, bytes(book_dek), f"epub-reader:book-epub:v1:{book_id}"
                )
                state_plain = bytearray(json.dumps(state, ensure_ascii=False).encode("utf-8"))
                try:
                    encrypted_state = encrypt_aes256_gcm(
                        bytes(state_plain), bytes(book_dek), f"epub-reader:book-state:v1:{book_id}"
                    )
                finally:
                    zero_bytearray(state_plain)

                _atomic_write_file(self._object_path(epub_obj_id), encrypted_epub)
                _atomic_write_file(self._object_path(state_obj_id), encrypted_state)

                entry: Dict[str, Any] = {
                    "bookId": book_id,
                    "bookDekHex": bytes(book_dek).hex(),
                    "epubObjId": epub_obj_id,
                    "stateObjId": state_obj_id,
                    "title": data["title"],
                    "author": data["author"],
                    "language": data["language"],
                    "originalFilename": data["originalFilename"],
                    "epubSha256": epub_sha256,
                    "epubByteLength": len(epub_bytes),
                    "chapterCount": data["chapterCount"],
                    "progress": state["progress"],
                    "importedAt": now,
                    "lastReadAt": now,
                }
                if data.get("description"):
                    entry["description"] = data["description"]
                if data.get("coverMimeType"):
                    entry["coverMimeType"] = data["coverMimeType"]
                if data.get("coverBase64"):
                    entry["coverBase64"] = data["coverBase64"]

                catalog["books"].append(entry)
                self._persist_catalog()
                return self._to_summary(entry)
            finally:
                zero_bytearray(book_dek)

    def read_decrypted_epub_bytes(self, book_id: str) -> bytes:
        with self._lock:
            entry = self._find_entry(book_id)
            book_dek = bytearray.fromhex(entry["bookDekHex"])
            obj_path = self._object_path(entry["epubObjId"])
            expected_sha = entry["epubSha256"]

        try:
            with open(obj_path, "rb") as fh:
                encrypted = fh.read()
            decrypted = decrypt_aes256_gcm(
                encrypted, bytes(book_dek), f"epub-reader:book-epub:v1:{book_id}"
            )
            if sha256_hex(decrypted) != expected_sha:
                raise CryptoAuthError("Decrypted EPUB failed SHA-256 integrity check")
            return decrypted
        finally:
            zero_bytearray(book_dek)

    def read_book_state(self, book_id: str) -> Dict[str, Any]:
        with self._lock:
            entry = self._find_entry(book_id)
            book_dek = bytearray.fromhex(entry["bookDekHex"])
            obj_path = self._object_path(entry["stateObjId"])

        try:
            with open(obj_path, "rb") as fh:
                encrypted = fh.read()
            decrypted = bytearray(
                decrypt_aes256_gcm(
                    encrypted, bytes(book_dek), f"epub-reader:book-state:v1:{book_id}"
                )
            )
            try:
                return json.loads(decrypted.decode("utf-8"))
            finally:
                zero_bytearray(decrypted)
        finally:
            zero_bytearray(book_dek)

    def update_book_state(
        self,
        book_id: str,
        mutator: Callable[[Dict[str, Any]], Dict[str, Any]],
    ) -> Dict[str, Any]:
        with self._lock:
            entry = self._find_entry(book_id)
            current = self.read_book_state(book_id)
            next_state = mutator(current)
            book_dek = bytearray.fromhex(entry["bookDekHex"])
            try:
                plain = bytearray(json.dumps(next_state, ensure_ascii=False).encode("utf-8"))
                try:
                    encrypted = encrypt_aes256_gcm(
                        bytes(plain), bytes(book_dek), f"epub-reader:book-state:v1:{book_id}"
                    )
                    _atomic_write_file(self._object_path(entry["stateObjId"]), encrypted)
                finally:
                    zero_bytearray(plain)

                entry["progress"] = next_state["progress"]
                entry["lastReadAt"] = _utc_iso_now()
                self._persist_catalog()
                return next_state
            finally:
                zero_bytearray(book_dek)

    def delete_book(self, book_id: str) -> None:
        with self._lock:
            _, _, catalog = self._assert_unlocked()
            idx = next(
                (i for i, b in enumerate(catalog["books"]) if b["bookId"] == book_id),
                -1,
            )
            if idx == -1:
                raise KeyNotFoundBookError(f"Book not found: {book_id}")
            removed = catalog["books"].pop(idx)
            self._persist_catalog()
            _secure_overwrite_and_delete(self._object_path(removed["epubObjId"]))
            _secure_overwrite_and_delete(self._object_path(removed["stateObjId"]))

    def export_portable_vault(self, book_id: str, export_passphrase: str) -> bytes:
        with self._lock:
            entry = dict(self._find_entry(book_id))
        epub_bytes = self.read_decrypted_epub_bytes(book_id)
        state = self.read_book_state(book_id)
        metadata: Dict[str, Any] = {
            "title": entry["title"],
            "author": entry["author"],
            "language": entry["language"],
            "originalFilename": entry["originalFilename"],
            "epubSha256": entry["epubSha256"],
            "chapterCount": entry["chapterCount"],
        }
        if entry.get("description"):
            metadata["description"] = entry["description"]
        if entry.get("coverMimeType"):
            metadata["coverMimeType"] = entry["coverMimeType"]
        if entry.get("coverBase64"):
            metadata["coverBase64"] = entry["coverBase64"]

        return pack_portable_vault(
            {"version": 1, "metadata": metadata, "epubBytes": epub_bytes, "state": state},
            export_passphrase,
            self.scrypt_params,
        )

    def import_portable_vault(self, archive: bytes, export_passphrase: str) -> Dict[str, Any]:
        unpacked = unpack_portable_vault(archive, export_passphrase)
        meta = unpacked["metadata"]
        rest_state = dict(unpacked["state"])
        rest_state.pop("bookId", None)
        return self.import_book(
            {
                "title": meta["title"],
                "author": meta["author"],
                "language": meta["language"],
                "description": meta.get("description"),
                "originalFilename": meta["originalFilename"],
                "epubBytes": unpacked["epubBytes"],
                "chapterCount": meta["chapterCount"],
                "coverMimeType": meta.get("coverMimeType"),
                "coverBase64": meta.get("coverBase64"),
                "initialState": rest_state,
            }
        )
