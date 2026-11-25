"""
TTS Worker Service for VibeVoice (Service Discovery + P2P Mesh Enabled).

Mandatory Service Discovery behavior:
  1. Registers with the central Web Toolbox server via POST /api/services/register
     before polling or updating any TTS jobs.
  2. Sends live 30s heartbeats, status transitions, and statistical telemetry.
  3. Attaches mandatory X-Worker-Id and X-Worker-Lease headers on all worker API calls.
  4. Exposes direct Peer-to-Peer (P2P) RPC operations (`synthesize`, `list_voices`)
     so other registered workers can synthesize speech directly without central server hops.

Usage:
    python tts_worker.py
    python tts_worker.py --base_url https://localhost:5000 --interval 5
    python tts_worker.py --model_path microsoft/VibeVoice-1.5b --device cuda --p2p_port 9410
"""
from __future__ import annotations

import argparse
import base64
import datetime
import os
import sys
import tempfile
import threading
import time
import traceback
import uuid
import wave
from typing import Any, Dict, List, Optional, Tuple

import requests
import urllib3

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_CLIENTS_DIR = os.path.abspath(os.path.join(_THIS_DIR, "..", "..", "clients"))
for _p in (_THIS_DIR, _REPO_CLIENTS_DIR):
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

from worker_sdk import (  # noqa: E402
    DEFAULT_API_KEY,
    PeerBinaryResponse,
    PeerCallContext,
    ToolboxWorkerRuntime,
)

# Lazy-loaded inference hooks (populated on first synthesis or injectable in tests)
initialize = None
generate_audio = None
load_model = None
unload_model = None
is_model_loaded = None

_API_KEY = os.environ.get("TOOLBOX_API_KEY", DEFAULT_API_KEY)
_VERIFY_SSL = False
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ============================================================
# Default speaker mapping by language
# ============================================================
SPEAKER_MAP = {
    "en": "Alice",       # English speaker
    "english": "Alice",
    "zh": "Xinran",      # Chinese speaker
    "zh-cn": "Xinran",   # Chinese speaker
    "chinese": "Xinran",
}

_MODEL_LOCK = threading.RLock()
_STATS_LOCK = threading.Lock()
_LAST_MODEL_ACTIVITY = 0.0
_TTS_STATS: Dict[str, Any] = {
    "queue_jobs_completed": 0,
    "queue_jobs_failed": 0,
    "p2p_syntheses_completed": 0,
    "synthesized_chars_total": 0,
    "synthesized_audio_bytes_total": 0,
    "synthesized_audio_sec_total": 0.0,
    "model_loaded": False,
}


def _ensure_inference_backend() -> None:
    """Import inference_service lazily so help/SDK/test harnesses work without GPU deps."""
    global initialize, generate_audio, load_model, unload_model, is_model_loaded
    if all(fn is not None for fn in (generate_audio, load_model, unload_model, is_model_loaded)):
        return
    import inference_service as _inf

    if initialize is None:
        initialize = _inf.initialize
    if generate_audio is None:
        generate_audio = _inf.generate_audio
    if load_model is None:
        load_model = _inf.load_model
    if unload_model is None:
        unload_model = _inf.unload_model
    if is_model_loaded is None:
        is_model_loaded = _inf.is_model_loaded


def _safe_is_model_loaded() -> bool:
    if is_model_loaded is None:
        return False
    try:
        return bool(is_model_loaded())
    except Exception:
        return False


def _measure_wav_duration_sec(wav_path: str) -> float:
    try:
        with wave.open(wav_path, "rb") as wf:
            frames = wf.getnframes()
            rate = wf.getframerate()
            if rate > 0:
                return round(frames / float(rate), 3)
    except Exception:
        pass
    return 0.0


def _record_tts_synthesis(
    runtime: Optional[ToolboxWorkerRuntime],
    *,
    chars: int,
    audio_bytes: int,
    duration_sec: float,
    via_p2p: bool,
) -> None:
    with _STATS_LOCK:
        if via_p2p:
            _TTS_STATS["p2p_syntheses_completed"] += 1
        else:
            _TTS_STATS["queue_jobs_completed"] += 1
        _TTS_STATS["synthesized_chars_total"] += max(0, int(chars))
        _TTS_STATS["synthesized_audio_bytes_total"] += max(0, int(audio_bytes))
        _TTS_STATS["synthesized_audio_sec_total"] = round(
            _TTS_STATS["synthesized_audio_sec_total"] + max(0.0, float(duration_sec)), 2
        )
        _TTS_STATS["model_loaded"] = _safe_is_model_loaded()
        snapshot = dict(_TTS_STATS)
    if runtime is not None:
        runtime.telemetry.set_custom_metrics(snapshot)


def _record_tts_failure(runtime: Optional[ToolboxWorkerRuntime]) -> None:
    with _STATS_LOCK:
        _TTS_STATS["queue_jobs_failed"] += 1
        _TTS_STATS["model_loaded"] = _safe_is_model_loaded()
        snapshot = dict(_TTS_STATS)
    if runtime is not None:
        runtime.telemetry.set_custom_metrics(snapshot)


def _sync_model_loaded_metric(runtime: Optional[ToolboxWorkerRuntime]) -> None:
    with _STATS_LOCK:
        _TTS_STATS["model_loaded"] = _safe_is_model_loaded()
        snapshot = dict(_TTS_STATS)
    if runtime is not None:
        runtime.telemetry.set_custom_metrics(snapshot)


def _request_headers(runtime: Optional[ToolboxWorkerRuntime]) -> Dict[str, str]:
    if runtime is not None:
        return runtime.worker_headers()
    return {"X-API-Key": _API_KEY} if _API_KEY else {}


def _worker_http_request(
    method: str,
    url: str,
    runtime: Optional[ToolboxWorkerRuntime],
    **kwargs: Any,
) -> requests.Response:
    """Perform an authenticated central-server call with automatic re-registration on 428/403."""
    headers = dict(kwargs.pop("headers", None) or {})
    headers.update(_request_headers(runtime))
    resp = requests.request(method, url, headers=headers, verify=_VERIFY_SSL, **kwargs)
    if resp.status_code in (428, 403) and runtime is not None:
        try:
            err_code = (resp.json() or {}).get("code", "")
        except Exception:
            err_code = ""
        if resp.status_code == 428 or err_code in ("worker_not_registered", "invalid_worker_lease"):
            if runtime.force_reregister():
                retry_headers = dict(headers)
                retry_headers.update(runtime.worker_headers())
                resp = requests.request(method, url, headers=retry_headers, verify=_VERIFY_SSL, **kwargs)
    return resp


def _ensure_model_ready_locked(
    runtime: Optional[ToolboxWorkerRuntime],
    *,
    model_path: Optional[str],
    device: Optional[str],
    cfg_scale: float,
    seed: Optional[int],
) -> bool:
    global _LAST_MODEL_ACTIVITY
    _ensure_inference_backend()
    if not is_model_loaded():
        if runtime is not None:
            runtime.set_status("starting", "Loading VibeVoice model into memory")
        print("[WORKER] No model loaded, loading now...")
        load_start = time.time()
        ok = load_model(
            model_path=model_path,
            device=device,
            cfg_scale=cfg_scale,
            seed=seed,
        )
        if not ok:
            if runtime is not None:
                runtime.set_status("degraded", "Failed to load VibeVoice model")
            return False
        print(f"[WORKER] Model loaded in {time.time() - load_start:.2f} seconds")
        if runtime is not None:
            runtime.set_status("serving", "Model loaded and serving")
    _LAST_MODEL_ACTIVITY = time.time()
    _sync_model_loaded_metric(runtime)
    return True


def synthesize_speech_locked(
    *,
    text: str,
    language: str = "en",
    speaker_name: Optional[str] = None,
    output_path: str,
    runtime: Optional[ToolboxWorkerRuntime] = None,
    model_cfg: Optional[Dict[str, Any]] = None,
) -> Tuple[str, float]:
    """Thread-safe model load + synthesis shared by central queue jobs and P2P RPCs."""
    global _LAST_MODEL_ACTIVITY
    cfg = model_cfg or {}
    lang_norm = (language or "en").strip().lower()
    chosen_speaker = (speaker_name or "").strip() or SPEAKER_MAP.get(lang_norm, SPEAKER_MAP["en"])

    with _MODEL_LOCK:
        if not _ensure_model_ready_locked(
            runtime,
            model_path=cfg.get("model_path"),
            device=cfg.get("device"),
            cfg_scale=float(cfg.get("cfg_scale", 1.3)),
            seed=cfg.get("seed"),
        ):
            raise RuntimeError("Failed to load TTS model")

        _LAST_MODEL_ACTIVITY = time.time()
        audio_path = generate_audio(
            text=text,
            language=language,
            speaker_name=chosen_speaker,
            output_path=output_path,
        )
        _LAST_MODEL_ACTIVITY = time.time()

    if not audio_path or not os.path.exists(audio_path):
        raise RuntimeError("Audio file was not generated")

    duration_sec = _measure_wav_duration_sec(audio_path)
    return audio_path, duration_sec


# ============================================================
# Central Server Queue Helpers
# ============================================================


def get_pending_jobs(base_url: str, runtime: Optional[ToolboxWorkerRuntime] = None) -> list:
    """Query the toolbox worker API for pending TTS jobs."""
    try:
        endpoint = "/api/tts/worker/jobs?status=pending" if runtime is not None else "/api/tts/jobs?status=pending"
        url = f"{base_url.rstrip('/')}{endpoint}"
        response = _worker_http_request("GET", url, runtime, timeout=10)
        response.raise_for_status()
        data = response.json()
        return data.get("jobs", [])
    except requests.RequestException as e:
        print(f"[ERROR] Failed to query pending jobs: {e}")
        return []
    except Exception as e:
        print(f"[ERROR] Unexpected error querying jobs: {e}")
        return []


def get_job_details(
    base_url: str,
    job_id: str,
    runtime: Optional[ToolboxWorkerRuntime] = None,
) -> Optional[dict]:
    """Get full details of a specific job."""
    try:
        url = f"{base_url.rstrip('/')}/api/tts/job/{job_id}"
        response = _worker_http_request("GET", url, runtime, timeout=10)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        print(f"[ERROR] Failed to get job {job_id}: {e}")
        return None
    except Exception as e:
        print(f"[ERROR] Unexpected error getting job: {e}")
        return None


def update_job_status(
    base_url: str,
    job_id: str,
    status: str,
    error: Optional[str] = None,
    runtime: Optional[ToolboxWorkerRuntime] = None,
) -> bool:
    """Update the status of a job (requires registered TTS worker lease)."""
    try:
        url = f"{base_url.rstrip('/')}/api/tts/job/{job_id}/status"
        payload: Dict[str, Any] = {"status": status}
        if error:
            payload["error"] = error
        response = _worker_http_request("POST", url, runtime, json=payload, timeout=10)
        response.raise_for_status()
        return True
    except requests.RequestException as e:
        print(f"[ERROR] Failed to update job {job_id} status: {e}")
        return False
    except Exception as e:
        print(f"[ERROR] Unexpected error updating status: {e}")
        return False


def upload_audio_result(
    base_url: str,
    job_id: str,
    audio_path: str,
    runtime: Optional[ToolboxWorkerRuntime] = None,
) -> bool:
    """Upload the generated audio file for a job (requires registered TTS worker lease)."""
    try:
        url = f"{base_url.rstrip('/')}/api/tts/job/{job_id}/result"
        with open(audio_path, "rb") as f:
            files = {"audio": (os.path.basename(audio_path), f, "audio/wav")}
            response = _worker_http_request("POST", url, runtime, files=files, timeout=60)
            response.raise_for_status()
        return True
    except requests.RequestException as e:
        print(f"[ERROR] Failed to upload audio for job {job_id}: {e}")
        return False
    except Exception as e:
        print(f"[ERROR] Unexpected error uploading audio: {e}")
        return False


def process_job(
    base_url: str,
    job: dict,
    *,
    runtime: Optional[ToolboxWorkerRuntime] = None,
    model_cfg: Optional[Dict[str, Any]] = None,
) -> bool:
    """Process a single queued TTS job and record service telemetry."""
    job_id = job["id"]
    print(f"\n{'=' * 60}")
    print(f"[WORKER] Processing job: {job_id}")
    print(f"[WORKER] Created: {job.get('created_at', '')}")
    print(f"{'=' * 60}")

    task_cm = runtime.track_task(source="queue", operation="tts_job") if runtime is not None else _nullcontext()
    try:
        with task_cm:
            job_details = get_job_details(base_url, job_id, runtime=runtime)
            if not job_details:
                raise RuntimeError(f"Could not retrieve job details for {job_id}")

            text = job_details.get("text", "")
            language = job_details.get("language", "en")
            if not text:
                update_job_status(base_url, job_id, "failed", error="Empty text", runtime=runtime)
                raise ValueError(f"Job {job_id} has empty text")

            print(f"[WORKER] Language: {language}")
            print(f"[WORKER] Text length: {len(text)} characters")
            print(f"[WORKER] Text preview: {text[:100]}...")

            if not update_job_status(base_url, job_id, "processing", runtime=runtime):
                raise RuntimeError(f"Could not mark job {job_id} as processing")

            speaker_name = SPEAKER_MAP.get((language or "en").lower(), SPEAKER_MAP["en"])
            print(f"[WORKER] Using speaker: {speaker_name}")

            temp_dir = os.path.join(_THIS_DIR, "temp_audio")
            os.makedirs(temp_dir, exist_ok=True)
            temp_output_path = os.path.join(temp_dir, f"{job_id}.wav")

            print("[WORKER] Generating audio...")
            start_time = time.time()
            try:
                audio_path, duration_sec = synthesize_speech_locked(
                    text=text,
                    language=language,
                    speaker_name=speaker_name,
                    output_path=temp_output_path,
                    runtime=runtime,
                    model_cfg=model_cfg,
                )
                generation_time = time.time() - start_time
                print(f"[WORKER] Audio generation completed in {generation_time:.2f} seconds")

                audio_size = os.path.getsize(audio_path)
                print("[WORKER] Uploading audio result...")
                if not upload_audio_result(base_url, job_id, audio_path, runtime=runtime):
                    update_job_status(base_url, job_id, "failed", error="Upload failed", runtime=runtime)
                    raise RuntimeError("Failed to upload audio result")

                _record_tts_synthesis(
                    runtime,
                    chars=len(text),
                    audio_bytes=audio_size,
                    duration_sec=duration_sec,
                    via_p2p=False,
                )
                print(f"[WORKER] Job {job_id} completed successfully!")
                return True
            except Exception as gen_err:
                generation_time = time.time() - start_time
                error_msg = f"{type(gen_err).__name__}: {str(gen_err)}"
                print(f"[ERROR] Audio generation/upload failed after {generation_time:.2f}s: {error_msg}")
                update_job_status(base_url, job_id, "failed", error=error_msg, runtime=runtime)
                raise
            finally:
                if os.path.exists(temp_output_path):
                    try:
                        os.remove(temp_output_path)
                    except OSError:
                        pass
    except Exception:
        _record_tts_failure(runtime)
        return False


class _nullcontext:
    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        return False


# ============================================================
# Direct Peer-to-Peer (P2P) Worker Operations
# ============================================================


def create_tts_worker_runtime(
    *,
    base_url: str,
    api_key: str,
    verify_tls: bool = False,
    worker_id: Optional[str] = None,
    worker_name: Optional[str] = None,
    model_path: Optional[str] = None,
    device: Optional[str] = None,
    cfg_scale: float = 1.3,
    seed: Optional[int] = None,
    heartbeat_interval: int = 30,
    p2p_host: str = "0.0.0.0",
    p2p_port: int = 0,
    p2p_advertise_host: Optional[str] = None,
    p2p_scheme: str = "http",
    p2p_certfile: Optional[str] = None,
    p2p_keyfile: Optional[str] = None,
) -> ToolboxWorkerRuntime:
    """Create and configure a ToolboxWorkerRuntime for the VibeVoice TTS service."""
    model_cfg = {
        "model_path": model_path,
        "device": device,
        "cfg_scale": cfg_scale,
        "seed": seed,
    }

    runtime = ToolboxWorkerRuntime(
        server_url=base_url,
        api_key=api_key,
        service_type="tts",
        service_version="1.2.0",
        worker_id=worker_id,
        worker_name=worker_name,
        max_concurrency=1,
        heartbeat_interval=heartbeat_interval,
        verify_ssl=verify_tls,
        listen_host=p2p_host,
        listen_port=p2p_port,
        advertise_host=p2p_advertise_host,
        p2p_ssl_cert=p2p_certfile,
        p2p_ssl_key=p2p_keyfile,
        capabilities={
            "languages": ["en", "zh", "zh-cn"],
            "speakers": {"en": "Alice", "zh": "Xinran"},
            "audio_formats": ["wav"],
            "model": model_path or "VibeVoice-1.5b",
            "device": device or "auto",
            "streaming": False,
        },
    )
    _sync_model_loaded_metric(runtime)

    def _p2p_list_voices(payload: Any, ctx: PeerCallContext) -> Dict[str, Any]:
        voices_dir = os.path.join(_THIS_DIR, "voices")
        voice_files: List[str] = []
        if os.path.isdir(voices_dir):
            voice_files = sorted(
                os.path.splitext(fn)[0]
                for fn in os.listdir(voices_dir)
                if fn.lower().endswith(".wav")
            )
        return {
            "service_type": "tts",
            "worker_id": runtime.worker_id,
            "default_speakers": {"en": "Alice", "zh": "Xinran"},
            "supported_languages": ["en", "zh", "zh-cn"],
            "available_voices": voice_files or ["Alice", "Xinran"],
            "caller_worker_id": ctx.caller_worker_id,
        }

    def _p2p_synthesize(payload: Any, ctx: PeerCallContext) -> Any:
        if not isinstance(payload, dict):
            raise ValueError("synthesize payload must be a JSON object")
        text = str(payload.get("text") or "").strip()
        if not text:
            raise ValueError("'text' is required and must be non-empty")

        language = str(payload.get("language") or "en").strip()
        speaker_name = str(payload.get("speaker") or payload.get("voice") or "").strip() or None
        response_format = str(payload.get("response_format") or "wav").strip().lower()

        temp_dir = os.path.join(_THIS_DIR, "temp_audio")
        os.makedirs(temp_dir, exist_ok=True)
        temp_wav = os.path.join(temp_dir, f"p2p_{uuid.uuid4().hex[:12]}.wav")

        try:
            audio_path, duration_sec = synthesize_speech_locked(
                text=text,
                language=language,
                speaker_name=speaker_name,
                output_path=temp_wav,
                runtime=runtime,
                model_cfg=model_cfg,
            )
            with open(audio_path, "rb") as wf:
                wav_bytes = wf.read()
        finally:
            if os.path.exists(temp_wav):
                try:
                    os.remove(temp_wav)
                except OSError:
                    pass

        chosen_speaker = speaker_name or SPEAKER_MAP.get(language.lower(), SPEAKER_MAP["en"])
        _record_tts_synthesis(
            runtime,
            chars=len(text),
            audio_bytes=len(wav_bytes),
            duration_sec=duration_sec,
            via_p2p=True,
        )

        if response_format == "json":
            return {
                "worker_id": runtime.worker_id,
                "format": "wav",
                "speaker": chosen_speaker,
                "language": language,
                "duration_sec": duration_sec,
                "audio_base64": base64.b64encode(wav_bytes).decode("ascii"),
            }

        return PeerBinaryResponse(
            data=wav_bytes,
            content_type="audio/wav",
            headers={
                "X-Audio-Duration-Sec": str(duration_sec),
                "X-Speaker-Name": chosen_speaker,
                "X-Language": language,
            },
        )

    runtime.register_operation("list_voices", _p2p_list_voices)
    runtime.register_operation("synthesize", _p2p_synthesize)
    return runtime


def run_worker(
    base_url: str,
    poll_interval: int = 5,
    idle_timeout: int = 60,
    model_path: Optional[str] = None,
    device: Optional[str] = None,
    cfg_scale: float = 1.3,
    seed: Optional[int] = None,
    worker_id: Optional[str] = None,
    worker_name: Optional[str] = None,
    heartbeat_interval: int = 30,
    p2p_host: str = "0.0.0.0",
    p2p_port: int = 0,
    p2p_advertise_host: Optional[str] = None,
    p2p_scheme: str = "http",
    p2p_certfile: Optional[str] = None,
    p2p_keyfile: Optional[str] = None,
) -> None:
    """Register with Service Discovery, serve P2P RPCs, and poll central TTS jobs."""
    global _LAST_MODEL_ACTIVITY
    model_cfg = {
        "model_path": model_path,
        "device": device,
        "cfg_scale": cfg_scale,
        "seed": seed,
    }

    runtime = create_tts_worker_runtime(
        base_url=base_url,
        api_key=_API_KEY,
        verify_tls=_VERIFY_SSL,
        worker_id=worker_id,
        worker_name=worker_name,
        model_path=model_path,
        device=device,
        cfg_scale=cfg_scale,
        seed=seed,
        heartbeat_interval=heartbeat_interval,
        p2p_host=p2p_host,
        p2p_port=p2p_port,
        p2p_advertise_host=p2p_advertise_host,
        p2p_scheme=p2p_scheme,
        p2p_certfile=p2p_certfile,
        p2p_keyfile=p2p_keyfile,
    )

    print("=" * 60)
    print("VibeVoice TTS Worker Service (Service Discovery + P2P Mesh)")
    print("=" * 60)
    print(f"Base URL: {base_url}")
    print(f"Worker ID: {runtime.worker_id}")
    print(f"Poll interval: {poll_interval} seconds | Heartbeat: {heartbeat_interval} seconds")
    print(f"Idle timeout: {idle_timeout} seconds (model unloads when idle)")
    print(f"Started at: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("Press Ctrl+C to stop")
    print("=" * 60)

    with runtime:
        ep_summary = ", ".join(e.get("url") or f"{e.get('protocol', 'http')}://{e['host']}:{e['port']}" for e in runtime.endpoints)
        print(f"[DISCOVERY] Registered worker '{runtime.worker_id}' (P2P endpoints: {ep_summary or 'none'})")

        consecutive_errors = 0
        logged_idle = False

        while True:
            try:
                pending_jobs = get_pending_jobs(base_url, runtime=runtime)
                runtime.telemetry.set_queue_depth(len(pending_jobs))

                if not pending_jobs:
                    consecutive_errors = 0
                    if not logged_idle:
                        logged_idle = True
                        print(f"\n[{datetime.datetime.now().strftime('%H:%M:%S')}] No pending jobs, entering idle mode...")

                    with _MODEL_LOCK:
                        if _safe_is_model_loaded() and _LAST_MODEL_ACTIVITY > 0:
                            idle_elapsed = time.time() - _LAST_MODEL_ACTIVITY
                            if idle_elapsed >= idle_timeout:
                                unload_model()
                                _sync_model_loaded_metric(runtime)
                                runtime.set_status("idle", "Idle (model unloaded)")

                    time.sleep(poll_interval)
                    continue

                logged_idle = False
                consecutive_errors = 0
                print(f"\n[{datetime.datetime.now().strftime('%H:%M:%S')}] Found {len(pending_jobs)} pending job(s)")

                jobs_to_process = list(reversed(pending_jobs))
                for job_to_process in jobs_to_process:
                    runtime.set_status("busy", f"Processing job {job_to_process['id'][:8]}")
                    success = process_job(
                        base_url,
                        job_to_process,
                        runtime=runtime,
                        model_cfg=model_cfg,
                    )
                    if success:
                        print("[WORKER] Job processed successfully")
                    else:
                        print("[WORKER] Job processing failed")

                    pending_jobs = get_pending_jobs(base_url, runtime=runtime)
                    runtime.telemetry.set_queue_depth(len(pending_jobs))
                    if not pending_jobs:
                        runtime.set_status("serving", "Model loaded and serving")
                        break

                time.sleep(poll_interval)

            except KeyboardInterrupt:
                print("\n\n[WORKER] Received shutdown signal, deregistering and stopping...")
                with _MODEL_LOCK:
                    if _safe_is_model_loaded():
                        unload_model()
                break
            except Exception as e:
                consecutive_errors += 1
                error_msg = f"{type(e).__name__}: {str(e)}"
                print(f"\n[WORKER] Unexpected error in main loop ({consecutive_errors} consecutive): {error_msg}")
                traceback.print_exc()
                runtime.set_status("degraded", error_msg[:180])

                backoff_time = min(poll_interval * (2 ** min(consecutive_errors - 1, 3)), 60)
                print(f"[WORKER] Backing off for {backoff_time} seconds...")
                time.sleep(backoff_time)

    print(f"\n[WORKER] Service stopped at {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="VibeVoice TTS Worker Service (Service Discovery Enabled)")
    parser.add_argument(
        "--base_url",
        type=str,
        default=os.environ.get("TOOLBOX_URL", "https://localhost:5000"),
        help="Base URL of the toolbox API (default: https://localhost:5000)",
    )
    parser.add_argument(
        "--api_key",
        type=str,
        default=os.environ.get("TOOLBOX_API_KEY", DEFAULT_API_KEY),
        help="Shared REST API key for Toolbox API authentication",
    )
    parser.add_argument(
        "--verify_ssl",
        action="store_true",
        help="Verify TLS certificate (disabled by default for self-signed certs)",
    )
    parser.add_argument(
        "--worker_id",
        type=str,
        default=os.environ.get("TOOLBOX_WORKER_ID"),
        help="Stable worker instance ID (auto-generated from host + port if omitted)",
    )
    parser.add_argument(
        "--worker_name",
        type=str,
        default=os.environ.get("TOOLBOX_WORKER_NAME"),
        help="Friendly display name in the Worker Dashboard",
    )
    parser.add_argument(
        "--heartbeat_interval",
        type=int,
        default=int(os.environ.get("TOOLBOX_HEARTBEAT_SEC", "30")),
        help="Service Discovery heartbeat interval in seconds (default: 30)",
    )
    parser.add_argument(
        "--p2p_host",
        type=str,
        default="0.0.0.0",
        help="Bind address for direct Peer-to-Peer worker RPC server (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--p2p_port",
        type=int,
        default=int(os.environ.get("TOOLBOX_P2P_PORT", "0")),
        help="Listen port for direct P2P worker RPC server (0 = auto-select ephemeral port)",
    )
    parser.add_argument(
        "--p2p_advertise_host",
        type=str,
        default=os.environ.get("TOOLBOX_P2P_ADVERTISE_HOST"),
        help="Explicit routable IP/hostname advertised to peer workers (auto-detected if omitted)",
    )
    parser.add_argument(
        "--p2p_scheme",
        choices=("http", "https"),
        default="http",
        help="Protocol for P2P worker server (default: http; use https with --p2p_cert/--p2p_key)",
    )
    parser.add_argument("--p2p_cert", type=str, default=None, help="TLS certificate PEM for P2P HTTPS")
    parser.add_argument("--p2p_key", type=str, default=None, help="TLS private key PEM for P2P HTTPS")
    parser.add_argument(
        "--model_path",
        type=str,
        default=None,
        help="Path to the model directory (auto-detects local models/VibeVoice-1.5b if not specified)",
    )
    parser.add_argument(
        "--device",
        type=str,
        default=None,
        help="Device for inference: cuda, mps, cpu (auto-detect if not specified)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=5,
        help="Polling interval in seconds (default: 5)",
    )
    parser.add_argument(
        "--idle_timeout",
        type=int,
        default=60,
        help="Seconds of idle time before unloading the model (default: 60)",
    )
    parser.add_argument(
        "--cfg_scale",
        type=float,
        default=1.3,
        help="CFG scale for generation (default: 1.3)",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Random seed for reproducibility (optional)",
    )
    return parser.parse_args()


def main() -> None:
    global _API_KEY, _VERIFY_SSL
    args = parse_args()
    _API_KEY = args.api_key
    _VERIFY_SSL = args.verify_ssl

    run_worker(
        base_url=args.base_url,
        poll_interval=args.interval,
        idle_timeout=args.idle_timeout,
        model_path=args.model_path,
        device=args.device,
        cfg_scale=args.cfg_scale,
        seed=args.seed,
        worker_id=args.worker_id,
        worker_name=args.worker_name,
        heartbeat_interval=args.heartbeat_interval,
        p2p_host=args.p2p_host,
        p2p_port=args.p2p_port,
        p2p_advertise_host=args.p2p_advertise_host,
        p2p_scheme=args.p2p_scheme,
        p2p_certfile=args.p2p_cert,
        p2p_keyfile=args.p2p_key,
    )


if __name__ == "__main__":
    main()
