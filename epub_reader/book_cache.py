"""Bounded in-memory LRU cache of decrypted ParsedEpubArchive instances."""
from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Tuple

from .parser import ParsedEpubArchive
from .vault_manager import VaultManager


class DecryptedBookCache:
    """Thread-safe LRU cache holding decrypted EPUB archives strictly in RAM."""

    def __init__(self, max_books: int = 3, max_total_bytes: int = 256 * 1024 * 1024):
        self.max_books = max_books
        self.max_total_bytes = max_total_bytes
        self._entries: "OrderedDict[str, Tuple[ParsedEpubArchive, int]]" = OrderedDict()
        self._total_bytes = 0
        self._lock = threading.RLock()

    def put(self, book_id: str, archive: ParsedEpubArchive, byte_size: int) -> None:
        with self._lock:
            self.evict(book_id)
            while len(self._entries) >= self.max_books or (
                len(self._entries) > 0 and self._total_bytes + byte_size > self.max_total_bytes
            ):
                oldest_id, _ = next(iter(self._entries.items()))
                self.evict(oldest_id)
            self._entries[book_id] = (archive, byte_size)
            self._total_bytes += byte_size

    def get_or_load(self, book_id: str, vault: VaultManager) -> ParsedEpubArchive:
        with self._lock:
            if book_id in self._entries:
                self._entries.move_to_end(book_id)
                return self._entries[book_id][0]

        epub_bytes = vault.read_decrypted_epub_bytes(book_id)
        archive = ParsedEpubArchive.from_bytes(epub_bytes)
        self.put(book_id, archive, len(epub_bytes))
        return archive

    def evict(self, book_id: str) -> None:
        with self._lock:
            existing = self._entries.pop(book_id, None)
            if existing:
                archive, byte_size = existing
                archive.dispose()
                self._total_bytes = max(0, self._total_bytes - byte_size)

    def clear_all(self) -> None:
        with self._lock:
            for archive, _ in self._entries.values():
                archive.dispose()
            self._entries.clear()
            self._total_bytes = 0
