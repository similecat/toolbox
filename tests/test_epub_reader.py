"""Unit and integration tests for the Toolbox Encrypted EPUB Reader."""
from __future__ import annotations

import base64
import io
import os
import shutil
import tempfile
import unittest
import zipfile

_TEST_DIR = tempfile.mkdtemp(prefix="toolbox_epub_test_")
os.environ.setdefault("TOOLBOX_DB_PATH", os.path.join(_TEST_DIR, "init_toolbox.db"))

import db as job_db
import app as toolbox_app
from epub_reader.parser import ParsedEpubArchive
from epub_reader.vault_manager import VaultManager
from werkzeug.security import generate_password_hash


def tearDownModule():
    job_db.close_conn()
    shutil.rmtree(_TEST_DIR, ignore_errors=True)


def _build_sample_epub(title: str = "Secret Cryptography Handbook", author: str = "Ada Lovelace") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("mimetype", "application/epub+zip")
        zf.writestr(
            "META-INF/container.xml",
            """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>""",
        )
        zf.writestr(
            "OEBPS/content.opf",
            f"""<?xml version="1.0" encoding="UTF-8"?>
<package version="3.0" xmlns="http://www.idpf.org/2007/opf" unique-identifier="bookid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="bookid">urn:uuid:11111111-2222-3333-4444-555555555555</dc:identifier>
    <dc:title>{title}</dc:title>
    <dc:creator>{author}</dc:creator>
    <dc:language>en</dc:language>
    <dc:description>Test encrypted EPUB study book.</dc:description>
  </metadata>
  <manifest>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    <item id="css1" href="styles/book.css" media-type="text/css"/>
    <item id="img1" href="images/diagram.svg" media-type="image/svg+xml" properties="cover-image"/>
    <item id="ch1" href="text/ch1.xhtml" media-type="application/xhtml+xml"/>
    <item id="ch2" href="text/ch2.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine>
    <itemref idref="ch1"/>
    <itemref idref="ch2"/>
  </spine>
</package>""",
        )
        zf.writestr(
            "OEBPS/nav.xhtml",
            """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
  <body>
    <nav epub:type="toc">
      <ol>
        <li><a href="text/ch1.xhtml">Chapter 1: Envelope Encryption</a></li>
        <li><a href="text/ch2.xhtml">Chapter 2: Active Recall</a></li>
      </ol>
    </nav>
  </body>
</html>""",
        )
        zf.writestr(
            "OEBPS/styles/book.css",
            "body { line-height: 1.8; background-image: url('../images/diagram.svg'); }",
        )
        zf.writestr(
            "OEBPS/images/diagram.svg",
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><circle cx="5" cy="5" r="4"/></svg>',
        )
        zf.writestr(
            "OEBPS/text/ch1.xhtml",
            """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
  <head>
    <title>Chapter 1: Envelope Encryption</title>
    <link rel="stylesheet" href="../styles/book.css"/>
  </head>
  <body>
    <h1>Chapter 1: Envelope Encryption</h1>
    <p onclick="alert(1)">AES-256-GCM protects both confidentiality and integrity.</p>
    <script>window.__pwned = true;</script>
    <img src="https://evil.example/tracker.png" alt="tracker"/>
    <img src="../images/diagram.svg" onerror="alert(2)" alt="diagram"/>
    <p><a href="ch2.xhtml#sec-a">Continue to Chapter 2</a></p>
  </body>
</html>""",
        )
        zf.writestr(
            "OEBPS/text/ch2.xhtml",
            """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
  <head><title>Chapter 2: Active Recall</title></head>
  <body>
    <h1 id="sec-a">Chapter 2: Active Recall</h1>
    <p>Testing retrieval strengthens memory retention.</p>
  </body>
</html>""",
        )
    return buf.getvalue()


class EpubReaderIntegrationTest(unittest.TestCase):
    def setUp(self):
        job_db.close_conn()
        self.case_dir = tempfile.mkdtemp(dir=_TEST_DIR)
        job_db.DB_PATH = os.path.join(self.case_dir, "toolbox.sqlite")
        job_db.init_db()

        self.vault_dir = os.path.join(self.case_dir, "vault")
        toolbox_app.app.config["TESTING"] = True
        toolbox_app.app.config["EPUB_VAULT_DIR"] = self.vault_dir
        toolbox_app.app.config["EPUB_SCRYPT_PARAMS"] = {"N": 1024, "r": 8, "p": 1, "keyLen": 32}
        toolbox_app.app.config["EPUB_UNLOCK_FAILURE_DELAY_SEC"] = 0.0
        toolbox_app.app.secret_key = job_db.get_setting("secret_key")

        self.client = toolbox_app.app.test_client()
        self.https_base = "https://localhost"
        self.api_headers = {"X-API-Key": job_db.get_api_key()}

    def tearDown(self):
        job_db.close_conn()

    def _login_web_user(self) -> str:
        job_db.create_user("bo.yang@hotmail.com", generate_password_hash("StrongPass123!"), is_admin=True)
        resp = self.client.post(
            "/login",
            data={"email": "bo.yang@hotmail.com", "password": "StrongPass123!"},
            base_url=self.https_base,
        )
        self.assertEqual(resp.status_code, 302)
        status_resp = self.client.get("/api/epub/vault/status", base_url=self.https_base)
        self.assertEqual(status_resp.status_code, 200)
        return status_resp.get_json()["csrfToken"]

    def test_auth_and_csrf_protection(self):
        # Unauthenticated webpage redirects to /login
        r_page = self.client.get("/epub-reader", base_url=self.https_base)
        self.assertEqual(r_page.status_code, 302)
        self.assertIn("/login", r_page.headers["Location"])

        # Unauthenticated API returns 401 without code
        r_api = self.client.get("/api/epub/vault/status", base_url=self.https_base)
        self.assertEqual(r_api.status_code, 401)
        self.assertNotIn("code", r_api.get_json())

        # Logged-in web user can view /epub-reader and index card
        csrf_token = self._login_web_user()
        r_home = self.client.get("/", base_url=self.https_base)
        self.assertEqual(r_home.status_code, 200)
        self.assertIn(b"/epub-reader", r_home.data)

        r_reader = self.client.get("/epub-reader", base_url=self.https_base)
        self.assertEqual(r_reader.status_code, 200)
        self.assertIn(b"epub-reader.js", r_reader.data)
        self.assertIn("no-store", r_reader.headers.get("Cache-Control", ""))

        # Mutating cookie-authenticated call without X-CSRF-Token is rejected with 403
        r_no_csrf = self.client.post(
            "/api/epub/vault/init",
            json={"passphrase": "master-passphrase-123", "rememberOnThisPc": False},
            base_url=self.https_base,
        )
        self.assertEqual(r_no_csrf.status_code, 403)

        # With X-CSRF-Token succeeds!
        r_ok = self.client.post(
            "/api/epub/vault/init",
            json={"passphrase": "master-passphrase-123", "rememberOnThisPc": False},
            headers={"X-CSRF-Token": csrf_token},
            base_url=self.https_base,
        )
        self.assertEqual(r_ok.status_code, 201)
        self.assertTrue(r_ok.get_json()["unlocked"])

    def test_full_encrypted_epub_lifecycle_via_api_key(self):
        # 1. Initialize vault
        r_init = self.client.post(
            "/api/epub/vault/init",
            json={"passphrase": "correct-horse-battery", "rememberOnThisPc": False},
            headers=self.api_headers,
            base_url=self.https_base,
        )
        self.assertEqual(r_init.status_code, 201)
        self.assertIn("no-store", r_init.headers.get("Cache-Control", ""))

        # 2. Import EPUB
        epub_bytes = _build_sample_epub()
        r_imp = self.client.post(
            "/api/epub/books/import",
            data=epub_bytes,
            headers={
                **self.api_headers,
                "Content-Type": "application/octet-stream",
                "X-Original-Filename": "crypto-handbook.epub",
            },
            base_url=self.https_base,
        )
        self.assertEqual(r_imp.status_code, 201)
        book = r_imp.get_json()["book"]
        book_id = book["bookId"]
        self.assertEqual(book["title"], "Secret Cryptography Handbook")
        self.assertEqual(book["chapterCount"], 2)

        # Verify zero plaintext leakage in filenames or vault.meta
        obj_files = os.listdir(os.path.join(self.vault_dir, "objects"))
        self.assertEqual(len(obj_files), 2)
        for fname in obj_files:
            self.assertNotIn("Secret", fname)
            self.assertNotIn("crypto", fname)
        with open(os.path.join(self.vault_dir, "vault.meta"), "rb") as f:
            self.assertNotIn(b"Secret Cryptography", f.read())
        with open(os.path.join(self.vault_dir, "catalog.enc"), "rb") as f:
            self.assertNotIn(b"Secret Cryptography", f.read())

        # 3. Detail, sanitized chapter HTML/CSS, and decrypted asset serving
        r_detail = self.client.get(
            f"/api/epub/books/{book_id}",
            headers=self.api_headers,
            base_url=self.https_base,
        )
        self.assertEqual(r_detail.status_code, 200)
        self.assertEqual(len(r_detail.get_json()["toc"]), 2)

        r_ch = self.client.get(
            f"/api/epub/books/{book_id}/chapters/0",
            headers=self.api_headers,
            base_url=self.https_base,
        )
        self.assertEqual(r_ch.status_code, 200)
        ch_json = r_ch.get_json()
        self.assertNotIn("<script", ch_json["html"])
        self.assertNotIn("onclick", ch_json["html"])
        self.assertNotIn("onerror", ch_json["html"])
        self.assertNotIn("evil.example", ch_json["html"])
        self.assertIn(f"/api/epub/books/{book_id}/assets?path=OEBPS%2Fimages%2Fdiagram.svg", ch_json["html"])
        self.assertIn('data-epub-spine="1"', ch_json["html"])
        self.assertIn('data-epub-fragment="sec-a"', ch_json["html"])
        self.assertIn(".epub-chapter-scope", ch_json["styles"][0])

        r_asset = self.client.get(
            f"/api/epub/books/{book_id}/assets?path=OEBPS/images/diagram.svg",
            headers=self.api_headers,
            base_url=self.https_base,
        )
        self.assertEqual(r_asset.status_code, 200)
        self.assertIn(b"<svg", r_asset.data)
        self.assertIn("default-src 'none'", r_asset.headers.get("Content-Security-Policy", ""))

        # 4. Progress, bookmark, annotation CRUD & Markdown/EPUB/Portable .vault exports
        r_prog = self.client.put(
            f"/api/epub/books/{book_id}/progress",
            json={"spineIndex": 1, "scrollRatio": 0.45, "completedChapterIndex": 0},
            headers=self.api_headers,
            base_url=self.https_base,
        )
        self.assertEqual(r_prog.status_code, 200)
        self.assertEqual(r_prog.get_json()["progress"]["completedChapters"], [0])

        r_bm = self.client.post(
            f"/api/epub/books/{book_id}/bookmarks",
            json={
                "spineIndex": 0,
                "chapterTitle": "Chapter 1",
                "scrollRatio": 0.25,
                "snippet": "AES-256-GCM",
                "label": "Key takeaway",
            },
            headers=self.api_headers,
            base_url=self.https_base,
        )
        self.assertEqual(r_bm.status_code, 201)
        bm_id = r_bm.get_json()["bookmark"]["id"]

        r_bm_patch = self.client.patch(
            f"/api/epub/books/{book_id}/bookmarks/{bm_id}",
            json={"label": "Updated takeaway"},
            headers=self.api_headers,
            base_url=self.https_base,
        )
        self.assertEqual(r_bm_patch.status_code, 200)
        self.assertEqual(r_bm_patch.get_json()["bookmark"]["label"], "Updated takeaway")

        r_ann = self.client.post(
            f"/api/epub/books/{book_id}/annotations",
            json={
                "spineIndex": 0,
                "chapterTitle": "Chapter 1: Envelope Encryption",
                "startOffset": 0,
                "endOffset": 11,
                "selectedText": "AES-256-GCM",
                "prefix": "",
                "suffix": " protects",
                "color": "emerald",
                "noteMarkdown": "Uses **96-bit IV** and 128-bit tag.",
            },
            headers=self.api_headers,
            base_url=self.https_base,
        )
        self.assertEqual(r_ann.status_code, 201)

        r_md = self.client.get(
            f"/api/epub/books/{book_id}/export/markdown",
            headers=self.api_headers,
            base_url=self.https_base,
        )
        self.assertEqual(r_md.status_code, 200)
        self.assertIn(b"Uses **96-bit IV**", r_md.data)
        self.assertIn(b"Updated takeaway", r_md.data)

        r_epub = self.client.get(
            f"/api/epub/books/{book_id}/export/epub",
            headers=self.api_headers,
            base_url=self.https_base,
        )
        self.assertEqual(r_epub.status_code, 200)
        self.assertEqual(r_epub.data, epub_bytes)

        r_pv = self.client.post(
            f"/api/epub/books/{book_id}/export/vault",
            json={"exportPassphrase": "transfer-secret-passphrase"},
            headers=self.api_headers,
            base_url=self.https_base,
        )
        self.assertEqual(r_pv.status_code, 200)
        portable_bytes = r_pv.data

        # 5. Lock vault, test wrong passphrase, unlock, delete book, and re-import portable .vault
        self.client.post("/api/epub/vault/lock", headers=self.api_headers, base_url=self.https_base)
        r_locked = self.client.get("/api/epub/books", headers=self.api_headers, base_url=self.https_base)
        self.assertEqual(r_locked.status_code, 401)
        self.assertEqual(r_locked.get_json()["code"], "VAULT_LOCKED")

        r_bad_unlock = self.client.post(
            "/api/epub/vault/unlock",
            json={"passphrase": "wrong-passphrase-attempt"},
            headers=self.api_headers,
            base_url=self.https_base,
        )
        self.assertEqual(r_bad_unlock.status_code, 401)

        r_unlock = self.client.post(
            "/api/epub/vault/unlock",
            json={"passphrase": "correct-horse-battery"},
            headers=self.api_headers,
            base_url=self.https_base,
        )
        self.assertEqual(r_unlock.status_code, 200)

        r_del = self.client.delete(
            f"/api/epub/books/{book_id}",
            headers=self.api_headers,
            base_url=self.https_base,
        )
        self.assertEqual(r_del.status_code, 200)
        self.assertEqual(
            self.client.get("/api/epub/books", headers=self.api_headers, base_url=self.https_base).get_json()["books"],
            [],
        )

        # Import portable .vault backup and verify annotations survived!
        b64_pass = base64.b64encode(b"transfer-secret-passphrase").decode("ascii")
        r_imp_pv = self.client.post(
            "/api/epub/books/import-vault",
            data=portable_bytes,
            headers={
                **self.api_headers,
                "Content-Type": "application/octet-stream",
                "X-Vault-Passphrase": b64_pass,
            },
            base_url=self.https_base,
        )
        self.assertEqual(r_imp_pv.status_code, 201)
        restored_id = r_imp_pv.get_json()["book"]["bookId"]
        r_restored = self.client.get(
            f"/api/epub/books/{restored_id}",
            headers=self.api_headers,
            base_url=self.https_base,
        )
        self.assertEqual(len(r_restored.get_json()["state"]["annotations"]), 1)

    def test_uninitialized_vault_default_state(self):
        target_dir = os.path.join(self.case_dir, "empty_runtime_vault")
        vm = VaultManager(vault_dir=target_dir)
        self.assertFalse(vm.is_initialized())
        self.assertFalse(vm.is_unlocked())
        self.assertFalse(vm.has_dpapi_remember_file())


if __name__ == "__main__":
    unittest.main()
