"""Windows Crypt32 DPAPI wrapper (win32 only; graceful no-op on Linux/macOS)."""
from __future__ import annotations

import ctypes
import sys

CRYPTPROTECT_UI_FORBIDDEN = 0x1


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", ctypes.c_uint32),
        ("pbData", ctypes.c_void_p),
    ]


class WindowsDpapiProvider:
    """Wrap/unwrap secrets under the current Windows user account via Crypt32.dll."""

    def is_supported(self) -> bool:
        return sys.platform == "win32"

    def protect(self, plaintext: bytes, entropy: bytes) -> bytes:
        if not self.is_supported():
            raise RuntimeError("Windows DPAPI is only supported on win32")

        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        crypt32.CryptProtectData.argtypes = [
            ctypes.POINTER(_DataBlob),
            ctypes.c_wchar_p,
            ctypes.POINTER(_DataBlob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(_DataBlob),
        ]
        crypt32.CryptProtectData.restype = ctypes.c_int
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p

        in_buf = ctypes.create_string_buffer(bytes(plaintext), len(plaintext))
        ent_buf = ctypes.create_string_buffer(bytes(entropy), len(entropy))
        in_blob = _DataBlob(len(plaintext), ctypes.cast(in_buf, ctypes.c_void_p))
        ent_blob = _DataBlob(len(entropy), ctypes.cast(ent_buf, ctypes.c_void_p))
        out_blob = _DataBlob(0, None)

        ok = crypt32.CryptProtectData(
            ctypes.byref(in_blob),
            None,
            ctypes.byref(ent_blob),
            None,
            None,
            CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(out_blob),
        )
        if not ok or not out_blob.pbData or out_blob.cbData <= 0:
            raise RuntimeError("CryptProtectData failed")

        try:
            return ctypes.string_at(out_blob.pbData, out_blob.cbData)
        finally:
            kernel32.LocalFree(out_blob.pbData)

    def unprotect(self, ciphertext: bytes, entropy: bytes) -> bytes:
        if not self.is_supported():
            raise RuntimeError("Windows DPAPI is only supported on win32")

        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        crypt32.CryptUnprotectData.argtypes = [
            ctypes.POINTER(_DataBlob),
            ctypes.c_void_p,
            ctypes.POINTER(_DataBlob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(_DataBlob),
        ]
        crypt32.CryptUnprotectData.restype = ctypes.c_int
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p

        in_buf = ctypes.create_string_buffer(bytes(ciphertext), len(ciphertext))
        ent_buf = ctypes.create_string_buffer(bytes(entropy), len(entropy))
        in_blob = _DataBlob(len(ciphertext), ctypes.cast(in_buf, ctypes.c_void_p))
        ent_blob = _DataBlob(len(entropy), ctypes.cast(ent_buf, ctypes.c_void_p))
        out_blob = _DataBlob(0, None)

        ok = crypt32.CryptUnprotectData(
            ctypes.byref(in_blob),
            None,
            ctypes.byref(ent_blob),
            None,
            None,
            CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(out_blob),
        )
        if not ok or not out_blob.pbData or out_blob.cbData <= 0:
            raise RuntimeError("CryptUnprotectData failed")

        try:
            raw = ctypes.string_at(out_blob.pbData, out_blob.cbData)
            ctypes.memset(out_blob.pbData, 0, out_blob.cbData)
            return raw
        finally:
            kernel32.LocalFree(out_blob.pbData)
