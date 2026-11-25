"""Unit and end-to-end HTTPS tests for the Machine Discovery Service & cross-platform agent."""
from __future__ import annotations

import os
import re
import shutil
import socket
import tempfile
import threading
import unittest

from werkzeug.serving import WSGIRequestHandler, make_server

_TEST_DIR = tempfile.mkdtemp(prefix="toolbox_machines_test_")
os.environ.setdefault("TOOLBOX_DB_PATH", os.path.join(_TEST_DIR, "init_toolbox.db"))

import db as job_db
import app as toolbox_app
from clients import machine_agent
from ssl_utils import ensure_self_signed_cert


def tearDownModule():
    job_db.close_conn()
    shutil.rmtree(_TEST_DIR, ignore_errors=True)


class FakeClock:
    def __init__(self, initial: float = 1_795_500_000.0) -> None:
        self.now = initial

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class MachineDiscoveryTest(unittest.TestCase):
    def setUp(self) -> None:
        job_db.close_conn()
        self.db_file = os.path.join(_TEST_DIR, f"db_{self._testMethodName}.sqlite")
        job_db.DB_PATH = self.db_file
        job_db.init_db()

        self.clock = FakeClock()
        self.store = toolbox_app.app.extensions["machine_store"]
        self._orig_clock = self.store._clock
        self.store._clock = self.clock

        toolbox_app.app.config["TESTING"] = True
        toolbox_app.app.secret_key = job_db.get_setting("secret_key")
        self.client = toolbox_app.app.test_client()
        self.https_base = "https://localhost"
        self.api_key = job_db.get_api_key()
        self.api_headers = {"X-API-Key": self.api_key}

    def tearDown(self) -> None:
        self.store._clock = self._orig_clock
        job_db.close_conn()

    def _register_and_login(self, email: str = "bo.yang@hotmail.com", password: str = "AdminPass123!"):
        resp = self.client.post(
            "/register",
            data={
                "email": email,
                "password": password,
                "confirm_password": password,
            },
            base_url=self.https_base,
        )
        self.assertEqual(resp.status_code, 302)

    def test_auth_and_web_visibility_enforced(self):
        # Unauthenticated webpage & script download redirect to /login
        for path in ("/machines", "/machines/agent.py"):
            resp = self.client.get(path, base_url=self.https_base)
            self.assertEqual(resp.status_code, 302)
            self.assertIn("/login", resp.headers["Location"])

        # Unauthenticated API calls return 401
        self.assertEqual(self.client.get("/api/machines", base_url=self.https_base).status_code, 401)
        self.assertEqual(self.client.post("/api/machines/register", json={}, base_url=self.https_base).status_code, 401)
        self.assertEqual(self.client.post("/api/machines/heartbeat", json={}, base_url=self.https_base).status_code, 401)
        self.assertEqual(self.client.get("/api/machines/agent.py", base_url=self.https_base).status_code, 401)

        # API key unlocks /api/machines/* and /api/machines/agent.py
        ok_list = self.client.get("/api/machines", headers=self.api_headers, base_url=self.https_base)
        self.assertEqual(ok_list.status_code, 200)
        ok_script = self.client.get("/api/machines/agent.py", headers=self.api_headers, base_url=self.https_base)
        self.assertEqual(ok_script.status_code, 200)
        self.assertIn(b"ToolboxDiscoveryClient", ok_script.data)

        # Login unlocks /machines webpage & home card
        self._register_and_login()
        home = self.client.get("/", base_url=self.https_base)
        self.assertEqual(home.status_code, 200)
        self.assertIn(b"/machines", home.data)

        page = self.client.get("/machines", base_url=self.https_base)
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Machine Discovery", page.data)
        # Admin sees pre-populated API key in setup instructions
        self.assertIn(self.api_key.encode("utf-8"), page.data)

        # Non-admin user can view /machines but does NOT see the raw admin API key
        job_db.add_allowed_email("viewer@example.com")
        viewer_client = toolbox_app.app.test_client()
        viewer_client.post(
            "/register",
            data={
                "email": "viewer@example.com",
                "password": "ViewerPassword1!",
                "confirm_password": "ViewerPassword1!",
            },
            base_url=self.https_base,
        )
        viewer_page = viewer_client.get("/machines", base_url=self.https_base)
        self.assertEqual(viewer_page.status_code, 200)
        self.assertNotIn(self.api_key.encode("utf-8"), viewer_page.data)
        self.assertIn(b"&lt;YOUR_TOOLBOX_API_KEY&gt;", viewer_page.data)

    def test_register_ipv4_ipv6_validation_and_csrf_delete(self):
        # Invalid IPv4 / IPv6 / status rejected with 400
        bad_v4 = self.client.post(
            "/api/machines/register",
            headers=self.api_headers,
            json={"machine_id": "m1", "machine_name": "node-1", "ipv4": "999.1.2.3"},
            base_url=self.https_base,
        )
        self.assertEqual(bad_v4.status_code, 400)

        bad_v6 = self.client.post(
            "/api/machines/register",
            headers=self.api_headers,
            json={"machine_id": "m1", "machine_name": "node-1", "ipv6": "not-an-ipv6"},
            base_url=self.https_base,
        )
        self.assertEqual(bad_v6.status_code, 400)

        bad_status = self.client.post(
            "/api/machines/register",
            headers=self.api_headers,
            json={"machine_id": "m1", "machine_name": "node-1", "serving_status": "exploding"},
            base_url=self.https_base,
        )
        self.assertEqual(bad_status.status_code, 400)

        # Valid registration normalizes IPv6 and captures X-Real-IP
        reg = self.client.post(
            "/api/machines/register",
            headers={**self.api_headers, "X-Real-IP": "20.48.100.77"},
            json={
                "machine_id": "node-01",
                "machine_name": "gpu-linux-01",
                "ipv4": "10.24.0.15",
                "ipv6": "2001:0db8:0000:0000:0000:0000:0000:0042%eth0",
                "serving_status": "serving",
                "status_detail": "tts_worker ready",
                "os_info": "Ubuntu 22.04.4 LTS (x86_64)",
                "heartbeat_interval": 60,
            },
            base_url=self.https_base,
        )
        self.assertEqual(reg.status_code, 200)
        m = reg.get_json()["machine"]
        self.assertEqual(m["ipv4"], "10.24.0.15")
        self.assertEqual(m["ipv6"], "2001:db8::42")
        self.assertEqual(m["public_ip"], "20.48.100.77")
        self.assertEqual(m["effective_status"], "serving")
        self.assertEqual(m["health_status"], "healthy")

        # Logged-in browser session requires CSRF token to DELETE
        self._register_and_login()
        page = self.client.get("/machines", base_url=self.https_base)
        csrf_match = re.search(r'const CSRF_TOKEN = "([^"]+)"', page.data.decode("utf-8"))
        self.assertIsNotNone(csrf_match)
        csrf_token = csrf_match.group(1)

        no_csrf_del = self.client.delete("/api/machines/node-01", base_url=self.https_base)
        self.assertEqual(no_csrf_del.status_code, 403)

        ok_del = self.client.delete(
            "/api/machines/node-01",
            headers={"X-CSRF-Token": csrf_token},
            base_url=self.https_base,
        )
        self.assertEqual(ok_del.status_code, 200)
        self.assertEqual(
            self.client.get("/api/machines/node-01", headers=self.api_headers, base_url=self.https_base).status_code,
            404,
        )

    def test_heartbeat_timing_transitions_and_throttled_db_writes(self):
        # Unknown machine heartbeat returns 205 Reset Content so agent re-registers
        unknown_hb = self.client.post(
            "/api/machines/heartbeat",
            headers=self.api_headers,
            json={"machine_id": "win-box-01"},
            base_url=self.https_base,
        )
        self.assertEqual(unknown_hb.status_code, 205)
        self.assertEqual(unknown_hb.data, b"")

        # Register machine with default 60s interval
        t0 = self.clock()
        self.client.post(
            "/api/machines/register",
            headers=self.api_headers,
            json={
                "machine_id": "win-box-01",
                "machine_name": "win11-workstation",
                "ipv4": "192.168.1.50",
                "ipv6": None,
                "serving_status": "serving",
                "heartbeat_interval": 60,
            },
            base_url=self.https_base,
        )

        # At t + 60s: pulse returns 204 No Content; SQLite write is skipped (throttled < 300s)
        self.clock.advance(60)
        hb1 = self.client.post(
            "/api/machines/heartbeat",
            headers=self.api_headers,
            json={"machine_id": "win-box-01"},
            base_url=self.https_base,
        )
        self.assertEqual(hb1.status_code, 204)
        self.assertEqual(hb1.data, b"")

        db_row = job_db._get_conn().execute(
            "SELECT last_heartbeat_at FROM machines WHERE machine_id = 'win-box-01'"
        ).fetchone()
        self.assertEqual(db_row["last_heartbeat_at"], t0)  # Disk write throttled!

        # Live API still reflects in-memory t0 + 60s immediately
        live = self.client.get("/api/machines/win-box-01", headers=self.api_headers, base_url=self.https_base).get_json()["machine"]
        self.assertEqual(live["last_heartbeat_epoch"], int(t0 + 60))
        self.assertEqual(live["health_status"], "healthy")
        self.assertEqual(live["effective_status"], "serving")

        # Boundary 90s since last heartbeat -> still healthy
        self.clock.advance(90)
        m_90 = self.client.get("/api/machines/win-box-01", headers=self.api_headers, base_url=self.https_base).get_json()["machine"]
        self.assertEqual(m_90["seconds_since_heartbeat"], 90)
        self.assertEqual(m_90["health_status"], "healthy")
        self.assertEqual(m_90["effective_status"], "serving")

        # 91s since last heartbeat -> unhealthy
        self.clock.advance(1)
        m_91 = self.client.get("/api/machines/win-box-01", headers=self.api_headers, base_url=self.https_base).get_json()["machine"]
        self.assertEqual(m_91["seconds_since_heartbeat"], 91)
        self.assertEqual(m_91["health_status"], "unhealthy")
        self.assertEqual(m_91["effective_status"], "unhealthy")

        # Boundary 180s -> still unhealthy; 181s -> unresponsive
        self.clock.advance(89)
        m_180 = self.client.get("/api/machines/win-box-01", headers=self.api_headers, base_url=self.https_base).get_json()["machine"]
        self.assertEqual(m_180["health_status"], "unhealthy")

        self.clock.advance(1)
        m_181 = self.client.get("/api/machines/win-box-01", headers=self.api_headers, base_url=self.https_base).get_json()["machine"]
        self.assertEqual(m_181["seconds_since_heartbeat"], 181)
        self.assertEqual(m_181["health_status"], "unresponsive")
        self.assertEqual(m_181["effective_status"], "unresponsive")

        # Advance past 300s DB flush window and send heartbeat -> recovers to healthy AND flushes SQLite
        self.clock.advance(100)  # now t0 + 341s
        hb_recover = self.client.post(
            "/api/machines/heartbeat",
            headers=self.api_headers,
            json={"machine_id": "win-box-01", "serving_status": "busy", "status_detail": "compiling"},
            base_url=self.https_base,
        )
        self.assertEqual(hb_recover.status_code, 204)

        m_rec = self.client.get("/api/machines/win-box-01", headers=self.api_headers, base_url=self.https_base).get_json()["machine"]
        self.assertEqual(m_rec["health_status"], "healthy")
        self.assertEqual(m_rec["serving_status"], "busy")
        self.assertEqual(m_rec["effective_status"], "busy")
        self.assertEqual(m_rec["status_detail"], "compiling")

        db_row_after = job_db._get_conn().execute(
            "SELECT last_heartbeat_at, serving_status FROM machines WHERE machine_id = 'win-box-01'"
        ).fetchone()
        self.assertEqual(db_row_after["last_heartbeat_at"], self.clock())
        self.assertEqual(db_row_after["serving_status"], "busy")

        # Graceful deregister marks offline immediately
        dereg = self.client.post(
            "/api/machines/deregister",
            headers=self.api_headers,
            json={"machine_id": "win-box-01"},
            base_url=self.https_base,
        )
        self.assertEqual(dereg.status_code, 200)
        m_off = self.client.get("/api/machines/win-box-01", headers=self.api_headers, base_url=self.https_base).get_json()["machine"]
        self.assertEqual(m_off["serving_status"], "stopped")
        self.assertEqual(m_off["health_status"], "offline")
        self.assertEqual(m_off["effective_status"], "offline")

    def test_live_https_keepalive_agent_end_to_end(self):
        class _HTTP11Handler(WSGIRequestHandler):
            protocol_version = "HTTP/1.1"

        ssl_dir = os.path.join(_TEST_DIR, "ssl_e2e")
        cert_path, key_path = ensure_self_signed_cert(ssl_dir)

        srv = make_server(
            "127.0.0.1",
            0,
            toolbox_app.app,
            threaded=True,
            request_handler=_HTTP11Handler,
            ssl_context=(cert_path, key_path),
        )
        port = srv.server_port
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()

        try:
            base_url = f"https://127.0.0.1:{port}"

            # 1. Run CLI agent --once with healthy local TCP check (pointing at our open HTTPS port)
            rc = machine_agent.main(
                [
                    "--server",
                    base_url,
                    "--api-key",
                    self.api_key,
                    "--machine-id",
                    "e2e-agent-01",
                    "--name",
                    "e2e-host",
                    "--ipv4",
                    "10.88.1.9",
                    "--ipv6",
                    "fd00:1234::9",
                    "--check-tcp",
                    f"127.0.0.1:{port}",
                    "--once",
                    "--quiet",
                ]
            )
            self.assertEqual(rc, 0)

            listed = self.store.list_machines(query="e2e-host")
            self.assertEqual(len(listed["machines"]), 1)
            entry = listed["machines"][0]
            self.assertEqual(entry["ipv4"], "10.88.1.9")
            self.assertEqual(entry["ipv6"], "fd00:1234::9")
            self.assertEqual(entry["effective_status"], "serving")

            # 2. Verify 204/205 heartbeat semantics against Flask + true TLS Keep-Alive reuse on HTTP/1.1 204
            ka_client = machine_agent.ToolboxDiscoveryClient(
                server_url=base_url,
                api_key=self.api_key,
                verify_ssl=False,
            )
            try:
                c1, _ = ka_client.request_json("POST", "/api/machines/heartbeat", {"machine_id": "e2e-agent-01"})
                c_unknown, _ = ka_client.request_json("POST", "/api/machines/heartbeat", {"machine_id": "unknown-id"})
                self.assertEqual(c1, 204)
                self.assertEqual(c_unknown, 205)
            finally:
                ka_client.close()

            import http.server
            import ssl as _ssl

            class _KeepAlive204Handler(http.server.BaseHTTPRequestHandler):
                protocol_version = "HTTP/1.1"

                def do_POST(self):
                    n = int(self.headers.get("Content-Length", "0"))
                    if n:
                        self.rfile.read(n)
                    self.send_response(204)
                    self.end_headers()

                def log_message(self, *_args):
                    pass

            ka_httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _KeepAlive204Handler)
            ka_ctx = _ssl.SSLContext(_ssl.PROTOCOL_TLS_SERVER)
            ka_ctx.load_cert_chain(certfile=cert_path, keyfile=key_path)
            ka_httpd.socket = ka_ctx.wrap_socket(ka_httpd.socket, server_side=True)
            ka_port = ka_httpd.server_port
            ka_thread = threading.Thread(target=ka_httpd.serve_forever, daemon=True)
            ka_thread.start()
            try:
                tls_ka = machine_agent.ToolboxDiscoveryClient(
                    server_url=f"https://127.0.0.1:{ka_port}",
                    api_key=self.api_key,
                    verify_ssl=False,
                )
                try:
                    s1, _ = tls_ka.request_json("POST", "/api/machines/heartbeat", {"machine_id": "m1"})
                    sock1 = tls_ka._conn.sock if tls_ka._conn else None
                    s2, _ = tls_ka.request_json("POST", "/api/machines/heartbeat", {"machine_id": "m1"})
                    sock2 = tls_ka._conn.sock if tls_ka._conn else None
                    self.assertEqual(s1, 204)
                    self.assertEqual(s2, 204)
                    self.assertIsNotNone(sock1)
                    self.assertIs(sock1, sock2, "TLS Keep-Alive socket must be reused across 204 pulses")
                finally:
                    tls_ka.close()
            finally:
                ka_httpd.shutdown()
                ka_httpd.server_close()

            # 3. Closed TCP port triggers automatic 'degraded' status report
            unused_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            unused_sock.bind(("127.0.0.1", 0))
            closed_port = unused_sock.getsockname()[1]
            unused_sock.close()

            rc_deg = machine_agent.main(
                [
                    "--server",
                    base_url,
                    "--api-key",
                    self.api_key,
                    "--machine-id",
                    "e2e-agent-01",
                    "--name",
                    "e2e-host",
                    "--check-tcp",
                    f"127.0.0.1:{closed_port}",
                    "--once",
                    "--quiet",
                ]
            )
            self.assertEqual(rc_deg, 0)
            updated = self.store.get("e2e-agent-01")
            self.assertIsNotNone(updated)
            self.assertEqual(updated["serving_status"], "degraded")
            self.assertEqual(updated["effective_status"], "degraded")
            self.assertIn("unreachable", updated["status_detail"])
        finally:
            srv.shutdown()
            srv.server_close()

    def test_agent_ip_classification_helpers(self):
        self.assertTrue(machine_agent._is_usable_ipv4("10.0.0.6"))
        self.assertTrue(machine_agent._is_usable_ipv4("192.168.1.20"))
        self.assertFalse(machine_agent._is_usable_ipv4("127.0.0.1"))
        self.assertFalse(machine_agent._is_usable_ipv4("169.254.10.20"))
        self.assertFalse(machine_agent._is_usable_ipv4("0.0.0.0"))

        self.assertEqual(machine_agent._classify_ipv6("::1"), 0)
        self.assertEqual(machine_agent._classify_ipv6("fe80::1%eth0"), 0)
        self.assertEqual(machine_agent._classify_ipv6("fd12:3456:789a::1"), 2)
        self.assertEqual(machine_agent._classify_ipv6("2404:6800:4004:800::200e"), 3)


if __name__ == "__main__":
    unittest.main()
