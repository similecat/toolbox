"""Unit and live HTTPS Peer-to-Peer tests for Worker Service Discovery & Mesh."""
from __future__ import annotations

import math
import os
import re
import shutil
import socket
import struct
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import wave
from typing import Any, Dict

from werkzeug.serving import WSGIRequestHandler, make_server

_TEST_DIR = tempfile.mkdtemp(prefix="toolbox_services_test_")
os.environ.setdefault("TOOLBOX_DB_PATH", os.path.join(_TEST_DIR, "init_toolbox.db"))

import db as job_db  # noqa: E402
import app as toolbox_app  # noqa: E402
from clients import worker_sdk  # noqa: E402
from ssl_utils import ensure_self_signed_cert  # noqa: E402

_TTS_DEMO_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "VibeVoice", "demo")
)
if _TTS_DEMO_DIR not in sys.path:
    sys.path.insert(0, _TTS_DEMO_DIR)
import tts_worker  # noqa: E402


def tearDownModule():
    job_db.close_conn()
    shutil.rmtree(_TEST_DIR, ignore_errors=True)


class FakeClock:
    def __init__(self, initial: float = 1_796_000_000.0) -> None:
        self.now = initial

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _write_test_wav(path: str, duration_sec: float = 0.25, sample_rate: int = 24000) -> str:
    n_frames = max(1, int(sample_rate * duration_sec))
    frames = bytearray()
    for i in range(n_frames):
        sample = int(8000.0 * math.sin(2.0 * math.pi * 440.0 * (i / float(sample_rate))))
        frames.extend(struct.pack("<h", sample))
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(bytes(frames))
    return path


class ServiceDiscoveryUnitTest(unittest.TestCase):
    def setUp(self) -> None:
        job_db.close_conn()
        self.db_file = os.path.join(_TEST_DIR, f"db_{self._testMethodName}.sqlite")
        job_db.DB_PATH = self.db_file
        job_db.init_db()

        self.clock = FakeClock()
        self.store = toolbox_app.app.extensions["service_store"]
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

    def _register_and_login(
        self,
        client: Any = None,
        email: str = "bo.yang@hotmail.com",
        password: str = "AdminPass123!",
    ) -> None:
        c = client or self.client
        resp = c.post(
            "/register",
            data={
                "email": email,
                "password": password,
                "confirm_password": password,
            },
            base_url=self.https_base,
        )
        self.assertEqual(resp.status_code, 302)

    def test_auth_dashboard_and_viewer_permissions(self) -> None:
        # Unauthenticated webpage & SDK script download redirect to /login
        for path in ("/services", "/services/worker_sdk.py"):
            resp = self.client.get(path, base_url=self.https_base)
            self.assertEqual(resp.status_code, 302)
            self.assertIn("/login", resp.headers["Location"])

        # Unauthenticated API calls return 401
        self.assertEqual(self.client.get("/api/services", base_url=self.https_base).status_code, 401)
        self.assertEqual(self.client.get("/api/services/discover/tts", base_url=self.https_base).status_code, 401)
        self.assertEqual(self.client.post("/api/services/register", json={}, base_url=self.https_base).status_code, 401)
        self.assertEqual(self.client.post("/api/services/heartbeat", json={}, base_url=self.https_base).status_code, 401)
        self.assertEqual(self.client.get("/api/services/worker_sdk.py", base_url=self.https_base).status_code, 401)

        # Shared API key unlocks /api/services/* and SDK download
        ok_list = self.client.get("/api/services", headers=self.api_headers, base_url=self.https_base)
        self.assertEqual(ok_list.status_code, 200)
        ok_sdk = self.client.get("/api/services/worker_sdk.py", headers=self.api_headers, base_url=self.https_base)
        self.assertEqual(ok_sdk.status_code, 200)
        self.assertIn(b"ToolboxWorkerRuntime", ok_sdk.data)

        # Register a sample worker so dashboard & viewer API have live data
        reg = self.client.post(
            "/api/services/register",
            json={
                "worker_id": "tts-node-01",
                "worker_name": "GPU TTS Node 01",
                "service_type": "tts",
                "service_version": "1.2.0",
                "host_name": "gpu-box-01",
                "os_info": "Linux 6.8 x86_64",
                "endpoints": [{"scheme": "http", "host": "10.20.30.40", "port": 9410}],
                "capabilities": {"languages": ["en", "zh"], "operations": ["synthesize"]},
                "max_concurrency": 2,
                "active_tasks": 1,
            },
            headers=self.api_headers,
            base_url=self.https_base,
        )
        self.assertEqual(reg.status_code, 200)

        # Login as admin unlocks /services dashboard & home page link
        self._register_and_login()
        home = self.client.get("/", base_url=self.https_base)
        self.assertEqual(home.status_code, 200)
        self.assertIn(b"/services", home.data)

        page = self.client.get("/services", base_url=self.https_base)
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Worker &amp; Service Discovery", page.data)
        self.assertIn(self.api_key.encode("utf-8"), page.data)
        csrf_match = re.search(r'const CSRF_TOKEN = "([^"]+)";', page.get_data(as_text=True))
        self.assertIsNotNone(csrf_match)
        admin_csrf = csrf_match.group(1)

        # Non-admin logged-in user CAN view /services and GET /api/services*,
        # does NOT see raw API key, and CANNOT call /api/services/register to leak peer_verify_secret
        job_db.add_allowed_email("viewer@example.com")
        viewer_client = toolbox_app.app.test_client()
        self._register_and_login(client=viewer_client, email="viewer@example.com", password="ViewerPass123!")

        viewer_page = viewer_client.get("/services", base_url=self.https_base)
        self.assertEqual(viewer_page.status_code, 200)
        self.assertNotIn(self.api_key.encode("utf-8"), viewer_page.data)
        v_csrf = re.search(r'const CSRF_TOKEN = "([^"]+)";', viewer_page.get_data(as_text=True)).group(1)

        viewer_api = viewer_client.get("/api/services", base_url=self.https_base)
        self.assertEqual(viewer_api.status_code, 200)
        self.assertEqual(viewer_api.get_json()["summary"]["total_workers"], 1)

        viewer_detail = viewer_client.get("/api/services/tts-node-01", base_url=self.https_base)
        self.assertEqual(viewer_detail.status_code, 200)
        self.assertEqual(viewer_detail.get_json()["worker"]["worker_name"], "GPU TTS Node 01")

        viewer_reg = viewer_client.post(
            "/api/services/register",
            json={"worker_id": "rogue-01", "service_type": "tts"},
            headers={"X-CSRF-Token": v_csrf},
            base_url=self.https_base,
        )
        self.assertEqual(viewer_reg.status_code, 403)

        # Browser DELETE requires valid CSRF token
        del_no_csrf = self.client.delete("/api/services/tts-node-01", base_url=self.https_base)
        self.assertEqual(del_no_csrf.status_code, 403)
        del_ok = self.client.delete(
            "/api/services/tts-node-01",
            headers={"X-CSRF-Token": admin_csrf},
            base_url=self.https_base,
        )
        self.assertEqual(del_ok.status_code, 200)

    def test_mandatory_worker_registration_gate_and_lease_lifecycle(self) -> None:
        # Submit a TTS job as an API client
        sub = self.client.post(
            "/api/tts/submit",
            json={"text": "Mandatory discovery test", "language": "en"},
            headers=self.api_headers,
            base_url=self.https_base,
        )
        self.assertEqual(sub.status_code, 201)
        job_id = sub.get_json()["job_id"]

        # 1. Worker endpoints reject unregistered workers with 428 Precondition Required
        r_poll = self.client.get("/api/tts/worker/jobs", headers=self.api_headers, base_url=self.https_base)
        self.assertEqual(r_poll.status_code, 428)
        self.assertEqual(r_poll.get_json()["code"], "worker_registration_required")

        r_status = self.client.post(
            f"/api/tts/job/{job_id}/status",
            json={"status": "processing"},
            headers=self.api_headers,
            base_url=self.https_base,
        )
        self.assertEqual(r_status.status_code, 428)

        # 2. Registering as a DIFFERENT service type ('ocr') is rejected on 'tts' endpoints with 403
        ocr_reg = self.client.post(
            "/api/services/register",
            json={
                "worker_id": "ocr-worker-01",
                "worker_name": "OCR Worker 01",
                "service_type": "ocr",
                "heartbeat_interval": 30,
            },
            headers=self.api_headers,
            base_url=self.https_base,
        ).get_json()
        wrong_svc = self.client.get(
            "/api/tts/worker/jobs",
            headers={
                "X-API-Key": self.api_key,
                "X-Worker-Id": "ocr-worker-01",
                "X-Worker-Lease": ocr_reg["worker_lease_token"],
            },
            base_url=self.https_base,
        )
        self.assertEqual(wrong_svc.status_code, 403)
        self.assertEqual(wrong_svc.get_json()["code"], "worker_service_type_mismatch")

        # 3. Forged lease token is rejected with 403
        tts_reg = self.client.post(
            "/api/services/register",
            json={
                "worker_id": "tts-worker-01",
                "worker_name": "TTS Worker 01",
                "service_type": "tts",
                "heartbeat_interval": 30,
                "endpoints": [{"protocol": "http", "host": "10.0.0.15", "port": 9411}],
            },
            headers=self.api_headers,
            base_url=self.https_base,
        ).get_json()

        forged = self.client.get(
            "/api/tts/worker/jobs",
            headers={
                "X-API-Key": self.api_key,
                "X-Worker-Id": "tts-worker-01",
                "X-Worker-Lease": "wlease1~0000000000000000000000000000000000000000000000000000000000000000",
            },
            base_url=self.https_base,
        )
        self.assertEqual(forged.status_code, 403)
        self.assertEqual(forged.get_json()["code"], "invalid_worker_lease")

        # 4. Valid registered TTS worker succeeds
        tts_headers = {
            "X-API-Key": self.api_key,
            "X-Worker-Id": "tts-worker-01",
            "X-Worker-Lease": tts_reg["worker_lease_token"],
        }
        ok_poll = self.client.get("/api/tts/worker/jobs", headers=tts_headers, base_url=self.https_base)
        self.assertEqual(ok_poll.status_code, 200)
        self.assertEqual(len(ok_poll.get_json()["jobs"]), 1)

        # 5. Advance clock past 1.5x heartbeat_interval (46s > 45s) -> lease expires!
        initial_list = self.client.get("/api/services", headers=self.api_headers, base_url=self.https_base)
        initial_etag = initial_list.headers["ETag"]

        self.clock.advance(46.0)

        # ETag MUST change when worker transitions from online -> unhealthy even with zero writes!
        stale_list = self.client.get(
            "/api/services",
            headers={**self.api_headers, "If-None-Match": initial_etag},
            base_url=self.https_base,
        )
        self.assertEqual(stale_list.status_code, 200)
        self.assertNotEqual(stale_list.headers["ETag"], initial_etag)

        # Peer discovery excludes unhealthy worker
        disc = self.client.get("/api/services/discover/tts", headers=self.api_headers, base_url=self.https_base)
        self.assertEqual(disc.get_json()["peers"], [])

        # Worker admission gate rejects stale worker with 428 until heartbeat resumes
        expired_poll = self.client.get("/api/tts/worker/jobs", headers=tts_headers, base_url=self.https_base)
        self.assertEqual(expired_poll.status_code, 428)
        self.assertEqual(expired_poll.get_json()["code"], "worker_lease_expired")

        # 6. Sending a heartbeat revives the worker immediately
        hb = self.client.post(
            "/api/services/heartbeat",
            json={"worker_id": "tts-worker-01", "worker_status": "serving"},
            headers=tts_headers,
            base_url=self.https_base,
        )
        self.assertEqual(hb.status_code, 204)
        revived_poll = self.client.get("/api/tts/worker/jobs", headers=tts_headers, base_url=self.https_base)
        self.assertEqual(revived_poll.status_code, 200)

    def test_etag_caching_telemetry_and_5min_history_rollups(self) -> None:
        reg = self.client.post(
            "/api/services/register",
            json={
                "worker_id": "asr-worker-01",
                "worker_name": "Whisper ASR 01",
                "service_type": "asr",
                "service_version": "2.1.0",
                "heartbeat_interval": 30,
                "max_concurrency": 4,
                "endpoints": [{"protocol": "http", "host": "10.10.1.5", "port": 9500}],
                "capabilities": {
                    "languages": ["en", "zh", "ja"],
                    "operations": ["transcribe"],
                    "gpu": "RTX-4090",
                },
            },
            headers=self.api_headers,
            base_url=self.https_base,
        ).get_json()
        lease = reg["worker_lease_token"]
        w_headers = {
            "X-API-Key": self.api_key,
            "X-Worker-Id": "asr-worker-01",
            "X-Worker-Lease": lease,
        }

        # Verify ETag 304 Not Modified when state is unchanged
        r1 = self.client.get("/api/services", headers=self.api_headers, base_url=self.https_base)
        self.assertEqual(r1.status_code, 200)
        etag1 = r1.headers["ETag"]
        r304 = self.client.get(
            "/api/services",
            headers={**self.api_headers, "If-None-Match": etag1},
            base_url=self.https_base,
        )
        self.assertEqual(r304.status_code, 304)

        # Bucket 1: +10 requests, +6 P2P, +1 error
        self.clock.advance(20.0)
        self.client.post(
            "/api/services/heartbeat",
            json={
                "worker_id": "asr-worker-01",
                "active_tasks": 2,
                "queue_depth": 3,
                "stats": {
                    "total_requests": 10,
                    "succeeded_requests": 9,
                    "failed_requests": 1,
                    "p2p_requests": 6,
                    "avg_latency_ms": 42.5,
                    "p95_latency_ms": 88.0,
                    "last_latency_ms": 39.0,
                    "cpu_percent": 34.2,
                    "memory_mb": 1420.5,
                    "gpu_memory_mb": 4096.0,
                    "custom": {"audio_minutes_transcribed": 12.4},
                },
            },
            headers=w_headers,
            base_url=self.https_base,
        )

        # Advance 310s into Bucket 2: cumulative 25 requests (+15 delta), 16 P2P (+10 delta)
        self.clock.advance(310.0)
        self.client.post(
            "/api/services/heartbeat",
            json={
                "worker_id": "asr-worker-01",
                "active_tasks": 1,
                "queue_depth": 0,
                "stats": {
                    "total_requests": 25,
                    "succeeded_requests": 24,
                    "failed_requests": 1,
                    "p2p_requests": 16,
                    "avg_latency_ms": 38.0,
                    "p95_latency_ms": 75.0,
                    "last_latency_ms": 31.0,
                    "cpu_percent": 22.0,
                    "memory_mb": 1450.0,
                    "gpu_memory_mb": 4096.0,
                    "custom": {"audio_minutes_transcribed": 31.8},
                },
            },
            headers=w_headers,
            base_url=self.https_base,
        )

        # Capability filter test on peer discovery
        disc_match = self.client.get(
            "/api/services/discover/asr?capability=languages:ja",
            headers=self.api_headers,
            base_url=self.https_base,
        ).get_json()
        self.assertEqual(len(disc_match["peers"]), 1)

        disc_miss = self.client.get(
            "/api/services/discover/asr?capability=languages:fr",
            headers=self.api_headers,
            base_url=self.https_base,
        ).get_json()
        self.assertEqual(len(disc_miss["peers"]), 0)

        # Inspect worker detail & 5-minute rollup buckets
        detail = self.client.get(
            "/api/services/asr-worker-01",
            headers=self.api_headers,
            base_url=self.https_base,
        ).get_json()
        worker = detail["worker"]
        self.assertEqual(worker["stats"]["total_requests"], 25)
        self.assertEqual(worker["stats"]["p2p_requests"], 16)
        self.assertEqual(worker["success_rate_pct"], 96.0)
        self.assertEqual(worker["stats"]["custom"]["audio_minutes_transcribed"], 31.8)

        history = detail["history"]
        self.assertGreaterEqual(len(history), 2)
        total_hist_reqs = sum(b["requests_delta"] for b in history)
        total_hist_p2p = sum(b["p2p_delta"] for b in history)
        self.assertEqual(total_hist_reqs, 25)
        self.assertEqual(total_hist_p2p, 16)


class LiveHTTPSPeerMeshTest(unittest.TestCase):
    """End-to-end tests with real HTTPS central server + multi-worker direct P2P sockets."""

    @classmethod
    def setUpClass(cls) -> None:
        job_db.close_conn()
        cls.live_db = os.path.join(_TEST_DIR, "live_p2p_mesh.sqlite")
        job_db.DB_PATH = cls.live_db
        job_db.init_db()

        toolbox_app.app.config["TESTING"] = False
        toolbox_app.app.secret_key = job_db.get_setting("secret_key")
        cls.api_key = job_db.get_api_key()

        ssl_dir = os.path.join(_TEST_DIR, "ssl")
        cert_path, key_path = ensure_self_signed_cert(ssl_dir)

        cls.central_request_count = 0
        cls.count_lock = threading.Lock()

        class _CountingQuietWSGIHandler(WSGIRequestHandler):
            def log_request(self, code="-", size="-"):
                with cls.count_lock:
                    cls.central_request_count += 1

            def log_message(self, format, *args):
                pass

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        _, cls.port = sock.getsockname()
        sock.close()

        cls.httpd = make_server(
            "127.0.0.1",
            cls.port,
            toolbox_app.app,
            threaded=True,
            request_handler=_CountingQuietWSGIHandler,
            ssl_context=(cert_path, key_path),
        )
        cls.server_thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.server_thread.start()
        cls.server_url = f"https://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()
        cls.httpd.server_close()
        job_db.close_conn()

    def test_direct_p2p_calls_bypass_central_server_and_failover(self) -> None:
        # Start two 'math-engine' provider workers + one 'orchestrator' caller worker
        provider_b = worker_sdk.ToolboxWorkerRuntime(
            server_url=self.server_url,
            api_key=self.api_key,
            service_type="math-engine",
            worker_id="math-b",
            worker_name="Math Engine B",
            listen_host="127.0.0.1",
            listen_port=0,
            advertise_host="127.0.0.1",
            heartbeat_interval=60,
            quiet=True,
        )

        @provider_b.operation("sum_squares")
        def _b_sum_sq(payload: Dict[str, Any], ctx: worker_sdk.PeerCallContext) -> Dict[str, Any]:
            nums = payload.get("nums", [])
            return {
                "handled_by": "math-b",
                "caller": ctx.caller_worker_id,
                "caller_service": ctx.caller_service_type,
                "result": sum(x * x for x in nums),
            }

        provider_c = worker_sdk.ToolboxWorkerRuntime(
            server_url=self.server_url,
            api_key=self.api_key,
            service_type="math-engine",
            worker_id="math-c",
            worker_name="Math Engine C",
            listen_host="127.0.0.1",
            listen_port=0,
            advertise_host="127.0.0.1",
            heartbeat_interval=60,
            quiet=True,
        )

        @provider_c.operation("sum_squares")
        def _c_sum_sq(payload: Dict[str, Any], ctx: worker_sdk.PeerCallContext) -> Dict[str, Any]:
            nums = payload.get("nums", [])
            return {
                "handled_by": "math-c",
                "caller": ctx.caller_worker_id,
                "caller_service": ctx.caller_service_type,
                "result": sum(x * x for x in nums),
            }

        caller_a = worker_sdk.ToolboxWorkerRuntime(
            server_url=self.server_url,
            api_key=self.api_key,
            service_type="orchestrator",
            worker_id="orch-a",
            worker_name="Orchestrator A",
            listen_host="127.0.0.1",
            listen_port=0,
            advertise_host="127.0.0.1",
            heartbeat_interval=60,
            quiet=True,
        )

        with provider_b, provider_c, caller_a:
            # 1. Warm caller_a's peer discovery cache once
            peers = caller_a.discover_peers("math-engine")
            self.assertEqual(len(peers), 2)

            # 2. Snapshot central server request counter, then run 10 direct P2P RPCs!
            with self.count_lock:
                before_central_reqs = self.central_request_count

            for i in range(10):
                out = caller_a.call_peer("math-engine", "sum_squares", {"nums": [2, 3, i]})
                self.assertEqual(out["result"], 4 + 9 + i * i)
                self.assertEqual(out["caller"], "orch-a")
                self.assertEqual(out["caller_service"], "orchestrator")
                self.assertIn(out["handled_by"], ("math-b", "math-c"))

            with self.count_lock:
                after_central_reqs = self.central_request_count

            # ZERO central server requests occurred during all 10 worker-to-worker calls!
            self.assertEqual(after_central_reqs, before_central_reqs)

            # 3. Verify unauthenticated P2P call is rejected with 401 by peer worker directly
            p2p_url = f"http://127.0.0.1:{provider_c.bound_port}/peer/v1/invoke/sum_squares"
            unauth_req = urllib.request.Request(
                p2p_url,
                data=b'{"nums":[1,2]}',
                method="POST",
                headers={"Content-Type": "application/json", "X-Peer-Token": "v1~9999999999~bad~svc~deadbeef"},
            )
            with self.assertRaises(urllib.error.HTTPError) as ctx_err:
                urllib.request.urlopen(unauth_req, timeout=5)
            self.assertEqual(ctx_err.exception.code, 401)
            ctx_err.exception.close()

            # 4. Abruptly kill provider_b's P2P listener WITHOUT deregistering -> caller_a fails over to math-c!
            provider_b._shutdown_p2p_server()
            failover_results = [
                caller_a.call_peer("math-engine", "sum_squares", {"nums": [5, 6]})
                for _ in range(4)
            ]
            for res in failover_results:
                self.assertEqual(res["handled_by"], "math-c")
                self.assertEqual(res["result"], 61)

    def test_tts_worker_queue_and_direct_p2p_binary_wav(self) -> None:
        # Inject deterministic lightweight WAV synthesis hooks into tts_worker (no GPU needed)
        model_state = {"loaded": False}

        def _fake_load_model(**kwargs: Any) -> bool:
            model_state["loaded"] = True
            return True

        def _fake_unload_model() -> None:
            model_state["loaded"] = False

        def _fake_is_loaded() -> bool:
            return model_state["loaded"]

        def _fake_generate_audio(text: str, language: str, speaker_name: str, output_path: str) -> str:
            return _write_test_wav(output_path, duration_sec=0.2)

        orig_hooks = (
            tts_worker.load_model,
            tts_worker.unload_model,
            tts_worker.is_model_loaded,
            tts_worker.generate_audio,
            tts_worker._API_KEY,
            tts_worker._VERIFY_SSL,
        )
        tts_worker.load_model = _fake_load_model
        tts_worker.unload_model = _fake_unload_model
        tts_worker.is_model_loaded = _fake_is_loaded
        tts_worker.generate_audio = _fake_generate_audio
        tts_worker._API_KEY = self.api_key
        tts_worker._VERIFY_SSL = False

        try:
            tts_rt = tts_worker.create_tts_worker_runtime(
                base_url=self.server_url,
                api_key=self.api_key,
                verify_tls=False,
                worker_id="tts-live-01",
                worker_name="Live VibeVoice Worker",
                heartbeat_interval=60,
                p2p_host="127.0.0.1",
                p2p_port=0,
                p2p_advertise_host="127.0.0.1",
            )
            tts_rt.quiet = True
            video_dubber_rt = worker_sdk.ToolboxWorkerRuntime(
                server_url=self.server_url,
                api_key=self.api_key,
                service_type="video-dubber",
                worker_id="dubber-live-01",
                worker_name="Video Dubber Worker",
                listen_host="127.0.0.1",
                listen_port=0,
                advertise_host="127.0.0.1",
                heartbeat_interval=60,
                quiet=True,
            )

            with tts_rt, video_dubber_rt:
                # Part A: Video-dubber worker calls TTS worker DIRECTLY via P2P for voices & WAV bytes!
                voices_info = video_dubber_rt.call_peer("tts", "list_voices", {})
                self.assertEqual(voices_info["worker_id"], "tts-live-01")
                self.assertEqual(voices_info["caller_worker_id"], "dubber-live-01")
                self.assertIn("en", voices_info["supported_languages"])

                # Warm cache then verify binary WAV synthesis happens with 0 central server hops
                video_dubber_rt.discover_peers("tts")
                with self.count_lock:
                    before_hops = self.central_request_count

                wav_resp = video_dubber_rt.call_peer_raw(
                    "tts",
                    "synthesize",
                    {"text": "Hello directly from peer worker!", "language": "en"},
                )
                with self.count_lock:
                    after_hops = self.central_request_count

                self.assertEqual(after_hops, before_hops)
                self.assertEqual(wav_resp.status_code, 200)
                self.assertIn("audio/wav", wav_resp.content_type)
                self.assertTrue(wav_resp.body.startswith(b"RIFF"))
                self.assertIn(b"WAVE", wav_resp.body[:16])
                self.assertEqual(wav_resp.headers.get("x-speaker-name"), "Alice")

                # Part B: Submit a central TTS job and process it via registered tts_worker
                import requests

                sub = requests.post(
                    f"{self.server_url}/api/tts/submit",
                    json={"text": "Queued job for central web user", "language": "zh"},
                    headers={"X-API-Key": self.api_key},
                    verify=False,
                    timeout=10,
                )
                self.assertEqual(sub.status_code, 201)
                pending = tts_worker.get_pending_jobs(self.server_url, runtime=tts_rt)
                self.assertEqual(len(pending), 1)
                ok = tts_worker.process_job(self.server_url, pending[0], runtime=tts_rt)
                self.assertTrue(ok)

                # Flush latest telemetry to central server and inspect statistical data
                tts_rt.flush_heartbeat()
                detail = requests.get(
                    f"{self.server_url}/api/services/tts-live-01",
                    headers={"X-API-Key": self.api_key},
                    verify=False,
                    timeout=10,
                ).json()["worker"]

                self.assertGreaterEqual(detail["stats"]["total_requests"], 3)
                self.assertEqual(detail["stats"]["p2p_requests"], 2)
                self.assertGreaterEqual(detail["stats"]["custom"]["p2p_syntheses_completed"], 1)
                self.assertGreaterEqual(detail["stats"]["custom"]["queue_jobs_completed"], 1)
                self.assertGreater(detail["stats"]["custom"]["synthesized_audio_sec_total"], 0.0)
        finally:
            (
                tts_worker.load_model,
                tts_worker.unload_model,
                tts_worker.is_model_loaded,
                tts_worker.generate_audio,
                tts_worker._API_KEY,
                tts_worker._VERIFY_SSL,
            ) = orig_hooks


if __name__ == "__main__":
    unittest.main()
