"""Unit and integration tests for HTTPS, authentication, email allow-list, admin config, and shared REST API key."""
import io
import os
import shutil
import ssl
import tempfile
import threading
import unittest
from unittest.mock import patch

import requests
from werkzeug.serving import make_server

# Point DB to a temporary test directory before importing app/db
_TEST_DIR = tempfile.mkdtemp(prefix="toolbox_test_")
os.environ["TOOLBOX_DB_PATH"] = os.path.join(_TEST_DIR, "test_toolbox.db")

import db as job_db
import app as toolbox_app
from ssl_utils import ensure_self_signed_cert


def tearDownModule():
    job_db.close_conn()
    shutil.rmtree(_TEST_DIR, ignore_errors=True)


class ToolboxAuthAndApiTest(unittest.TestCase):
    def setUp(self):
        job_db.close_conn()
        self.db_file = os.path.join(_TEST_DIR, f"db_{self._testMethodName}.sqlite")
        job_db.DB_PATH = self.db_file
        job_db.init_db()

        toolbox_app.app.config["TESTING"] = True
        toolbox_app.app.secret_key = job_db.get_setting("secret_key")
        self.client = toolbox_app.app.test_client()
        self.https_base = "https://localhost"

    def tearDown(self):
        job_db.close_conn()

    def _get_csrf(self, html_bytes: bytes) -> str:
        import re
        m = re.search(r'name="csrf_token"\s+value="([^"]+)"', html_bytes.decode("utf-8"))
        self.assertIsNotNone(m, "CSRF token should be present in admin page")
        return m.group(1)

    def test_default_allowlist_has_bo_yang(self):
        self.assertTrue(job_db.is_email_allowed("bo.yang@hotmail.com"))
        self.assertTrue(job_db.is_email_allowed("BO.YANG@HOTMAIL.COM"))
        self.assertFalse(job_db.is_email_allowed("stranger@example.com"))
        self.assertEqual(job_db.get_api_key(), job_db.DEFAULT_API_KEY)

    def test_webpages_require_login(self):
        for path in ["/", "/github-downloader", "/text-to-voice", "/admin"]:
            resp = self.client.get(path, base_url=self.https_base)
            self.assertEqual(resp.status_code, 302, f"{path} should redirect to /login")
            self.assertIn("/login", resp.headers["Location"])

        resp_post = self.client.post("/tts/submit", data={"text": "hi"}, base_url=self.https_base)
        self.assertEqual(resp_post.status_code, 302)
        self.assertIn("/login", resp_post.headers["Location"])

    def test_whitelist_enforced_on_register_and_password_change(self):
        # Unlisted email cannot register
        resp = self.client.post(
            "/register",
            data={
                "email": "intruder@example.com",
                "password": "password123",
                "confirm_password": "password123",
            },
            base_url=self.https_base,
        )
        self.assertEqual(resp.status_code, 403)
        self.assertIn(b"allow-listed", resp.data)

        # Default allow-listed email CAN register and becomes admin
        resp_ok = self.client.post(
            "/register",
            data={
                "email": "bo.yang@hotmail.com",
                "password": "StrongPassword1!",
                "confirm_password": "StrongPassword1!",
            },
            base_url=self.https_base,
            follow_redirects=False,
        )
        self.assertEqual(resp_ok.status_code, 302)
        user = job_db.get_user_by_email("bo.yang@hotmail.com")
        self.assertIsNotNone(user)
        self.assertEqual(user["is_admin"], 1)

        # Logged-in user can now view protected webpages
        home = self.client.get("/", base_url=self.https_base)
        self.assertEqual(home.status_code, 200)
        self.assertIn(b"bo.yang@hotmail.com", home.data)

        # Allow-listed user can modify password
        pwd_resp = self.client.post(
            "/change-password",
            data={
                "email": "bo.yang@hotmail.com",
                "current_password": "StrongPassword1!",
                "new_password": "NewStrongPassword2!",
                "confirm_password": "NewStrongPassword2!",
            },
            base_url=self.https_base,
        )
        self.assertEqual(pwd_resp.status_code, 200)
        self.assertIn(b"updated successfully", pwd_resp.data)

        # Sign out and verify new password works while old password fails
        self.client.get("/logout", base_url=self.https_base)
        bad_login = self.client.post(
            "/login",
            data={"email": "bo.yang@hotmail.com", "password": "StrongPassword1!"},
            base_url=self.https_base,
        )
        self.assertEqual(bad_login.status_code, 401)

        good_login = self.client.post(
            "/login",
            data={"email": "bo.yang@hotmail.com", "password": "NewStrongPassword2!"},
            base_url=self.https_base,
        )
        self.assertEqual(good_login.status_code, 302)

    def test_admin_can_configure_allowlist_and_non_admin_blocked(self):
        # Register admin (bo.yang@hotmail.com)
        self.client.post(
            "/register",
            data={
                "email": "bo.yang@hotmail.com",
                "password": "AdminPassword1!",
                "confirm_password": "AdminPassword1!",
            },
            base_url=self.https_base,
        )

        admin_page = self.client.get("/admin", base_url=self.https_base)
        self.assertEqual(admin_page.status_code, 200)
        csrf = self._get_csrf(admin_page.data)

        # Admin adds alice@example.com to allow list
        add_resp = self.client.post(
            "/admin/allowlist/add",
            data={"email": "alice@example.com", "csrf_token": csrf},
            base_url=self.https_base,
        )
        self.assertEqual(add_resp.status_code, 200)
        self.assertTrue(job_db.is_email_allowed("alice@example.com"))

        # Switch to a second client for Alice (non-admin)
        alice_client = toolbox_app.app.test_client()
        reg_alice = alice_client.post(
            "/register",
            data={
                "email": "alice@example.com",
                "password": "AlicePassword1!",
                "confirm_password": "AlicePassword1!",
            },
            base_url=self.https_base,
        )
        self.assertEqual(reg_alice.status_code, 302)

        # Alice is NOT admin -> /admin and /api/admin/allowlist return 403
        self.assertEqual(alice_client.get("/admin", base_url=self.https_base).status_code, 403)
        self.assertEqual(alice_client.get("/api/admin/allowlist", base_url=self.https_base).status_code, 403)

        # Admin removes alice@example.com from allow list
        rem_resp = self.client.post(
            "/admin/allowlist/remove",
            data={"email": "alice@example.com", "csrf_token": csrf},
            base_url=self.https_base,
        )
        self.assertEqual(rem_resp.status_code, 200)
        self.assertFalse(job_db.is_email_allowed("alice@example.com"))

        # Removed email can NO LONGER modify password
        pwd_alice = alice_client.post(
            "/change-password",
            data={
                "email": "alice@example.com",
                "current_password": "AlicePassword1!",
                "new_password": "AlicePassword2!",
                "confirm_password": "AlicePassword2!",
            },
            base_url=self.https_base,
        )
        self.assertEqual(pwd_alice.status_code, 403)

    def test_shared_default_api_key_for_all_services(self):
        api_key = job_db.DEFAULT_API_KEY

        # Without API key or login, all REST APIs return 401
        self.assertEqual(self.client.get("/api/tts/jobs", base_url=self.https_base).status_code, 401)
        self.assertEqual(
            self.client.post("/api/tts/submit", json={"text": "hello"}, base_url=self.https_base).status_code,
            401,
        )
        self.assertEqual(
            self.client.post(
                "/api/github/download",
                json={"github_url": "https://github.com/octocat/Hello-World"},
                base_url=self.https_base,
            ).status_code,
            401,
        )

        # Wrong API key returns 401
        self.assertEqual(
            self.client.get(
                "/api/tts/jobs",
                headers={"X-API-Key": "wrong-key"},
                base_url=self.https_base,
            ).status_code,
            401,
        )

        # Valid X-API-Key works for TTS service lifecycle
        sub = self.client.post(
            "/api/tts/submit",
            json={"text": "Hello encrypted world", "language": "en"},
            headers={"X-API-Key": api_key},
            base_url=self.https_base,
        )
        self.assertEqual(sub.status_code, 201)
        job_id = sub.get_json()["job_id"]

        # Authorization: Bearer <key> also works
        jobs_resp = self.client.get(
            "/api/tts/jobs?status=pending",
            headers={"Authorization": f"Bearer {api_key}"},
            base_url=self.https_base,
        )
        self.assertEqual(jobs_resp.status_code, 200)
        self.assertEqual(len(jobs_resp.get_json()["jobs"]), 1)

        # Unregistered worker calls are rejected with 428 Precondition Required
        unreg_resp = self.client.post(
            f"/api/tts/job/{job_id}/status",
            json={"status": "processing"},
            headers={"X-API-Key": api_key},
            base_url=self.https_base,
        )
        self.assertEqual(unreg_resp.status_code, 428)

        # Register TTS worker via Service Discovery, then update status + upload audio + download
        reg_resp = self.client.post(
            "/api/services/register",
            json={
                "worker_id": "tts-test-worker-01",
                "worker_name": "tts-test-worker",
                "service_type": "tts",
                "service_version": "1.2.0",
            },
            headers={"X-API-Key": api_key},
            base_url=self.https_base,
        )
        self.assertEqual(reg_resp.status_code, 200)
        worker_lease = reg_resp.get_json()["worker_lease_token"]
        worker_headers = {
            "X-API-Key": api_key,
            "X-Worker-Id": "tts-test-worker-01",
            "X-Worker-Lease": worker_lease,
        }

        st_resp = self.client.post(
            f"/api/tts/job/{job_id}/status",
            json={"status": "processing"},
            headers=worker_headers,
            base_url=self.https_base,
        )
        self.assertEqual(st_resp.status_code, 200)
        self.assertEqual(st_resp.get_json()["status"], "processing")

        up_resp = self.client.post(
            f"/api/tts/job/{job_id}/result",
            data={"audio": (io.BytesIO(b"FAKE_WAV_DATA"), "test.wav")},
            content_type="multipart/form-data",
            headers=worker_headers,
            base_url=self.https_base,
        )
        self.assertEqual(up_resp.status_code, 200)

        dl_resp = self.client.get(
            f"/api/tts/job/{job_id}/download",
            headers={"X-API-Key": api_key},
            base_url=self.https_base,
        )
        self.assertEqual(dl_resp.status_code, 200)
        self.assertEqual(dl_resp.data, b"FAKE_WAV_DATA")
        dl_resp.close()

        # Nginx X-Accel-Redirect header check when behind Nginx
        accel_resp = self.client.get(
            f"/tts/download/{job_id}",
            headers={"X-API-Key": api_key, "X-Nginx-Proxy": "1"},
            base_url=self.https_base,
        )
        self.assertEqual(accel_resp.status_code, 200)
        self.assertTrue(accel_resp.headers["X-Accel-Redirect"].startswith("/internal-audio/"))

        # Delete job via API key
        del_resp = self.client.delete(
            f"/api/tts/job/{job_id}",
            headers={"X-API-Key": api_key},
            base_url=self.https_base,
        )
        self.assertEqual(del_resp.status_code, 200)

        # Same API key works for GitHub Downloader REST API (mocking outbound GitHub HTTP)
        fake_zip_dir = tempfile.mkdtemp(dir=_TEST_DIR)
        fake_zip = os.path.join(fake_zip_dir, "Hello-World.zip")
        with open(fake_zip, "wb") as f:
            f.write(b"PK\x03\x04FAKEZIP")

        with patch.object(toolbox_app, "download_github_repo", return_value=(fake_zip, fake_zip_dir)):
            gh_resp = self.client.post(
                "/api/github/download",
                json={"github_url": "https://github.com/octocat/Hello-World"},
                headers={"X-API-Key": api_key},
                base_url=self.https_base,
            )
            self.assertEqual(gh_resp.status_code, 200)
            self.assertEqual(gh_resp.data, b"PK\x03\x04FAKEZIP")
            gh_resp.close()

    def test_self_signed_https_live_server_transfer(self):
        ssl_dir = os.path.join(_TEST_DIR, "ssl_live")
        cert_path, key_path = ensure_self_signed_cert(ssl_dir, common_name="localhost")
        self.assertTrue(os.path.exists(cert_path))
        self.assertTrue(os.path.exists(key_path))

        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert_path, key_path)

        server = make_server("127.0.0.1", 0, toolbox_app.app, ssl_context=ctx)
        port = server.server_port
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        try:
            # Verify TLS handshake + webpage login redirect
            r_web = requests.get(f"https://127.0.0.1:{port}/", verify=False, allow_redirects=False, timeout=5)
            self.assertEqual(r_web.status_code, 302)
            self.assertIn("/login", r_web.headers["Location"])

            # Verify TLS encrypted REST API call with default API key
            r_api = requests.get(
                f"https://127.0.0.1:{port}/api/tts/jobs",
                headers={"X-API-Key": job_db.DEFAULT_API_KEY},
                verify=False,
                timeout=5,
            )
            self.assertEqual(r_api.status_code, 200)
            self.assertIn("jobs", r_api.json())
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
