"""
TTS Worker Service for VibeVoice.
Polls the toolbox API for pending TTS jobs, processes them using VibeVoice model,
and uploads the results back.

Usage:
    python tts_worker.py
    python tts_worker.py --base_url http://localhost:5000 --interval 5
    python tts_worker.py --model_path microsoft/VibeVoice-1.5b --device cuda
"""
import argparse
import time
import sys
import os
import requests
import datetime
import traceback
from typing import Optional

# Add the demo directory to path so we can import inference_service
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from inference_service import initialize, generate_audio, load_model, unload_model, is_model_loaded


# ============================================================
# Default speaker mapping by language
# ============================================================
SPEAKER_MAP = {
    "en": "Alice",    # English speaker
    "zh": "Xinran",   # Chinese speaker
    "zh-cn": "Xinran",   # Chinese speaker
}


def get_pending_jobs(base_url: str) -> list:
    """
    Query the toolbox API for pending TTS jobs.
    
    Returns:
        List of job dicts, or empty list on error
    """
    try:
        url = f"{base_url}/api/tts/jobs?status=pending"
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()
        return data.get("jobs", [])
    except requests.RequestException as e:
        print(f"[ERROR] Failed to query pending jobs: {e}")
        return []
    except Exception as e:
        print(f"[ERROR] Unexpected error querying jobs: {e}")
        return []


def get_job_details(base_url: str, job_id: str) -> dict:
    """
    Get full details of a specific job.
    
    Returns:
        Job dict with full text, or None on error
    """
    try:
        url = f"{base_url}/api/tts/job/{job_id}"
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        print(f"[ERROR] Failed to get job {job_id}: {e}")
        return None
    except Exception as e:
        print(f"[ERROR] Unexpected error getting job: {e}")
        return None


def update_job_status(base_url: str, job_id: str, status: str, error: str = None) -> bool:
    """
    Update the status of a job.
    
    Returns:
        True if successful, False otherwise
    """
    try:
        url = f"{base_url}/api/tts/job/{job_id}/status"
        payload = {"status": status}
        if error:
            payload["error"] = error
        response = requests.post(url, json=payload, timeout=10)
        response.raise_for_status()
        return True
    except requests.RequestException as e:
        print(f"[ERROR] Failed to update job {job_id} status: {e}")
        return False
    except Exception as e:
        print(f"[ERROR] Unexpected error updating status: {e}")
        return False


def upload_audio_result(base_url: str, job_id: str, audio_path: str) -> bool:
    """
    Upload the generated audio file for a job.
    
    Returns:
        True if successful, False otherwise
    """
    try:
        url = f"{base_url}/api/tts/job/{job_id}/result"
        with open(audio_path, 'rb') as f:
            files = {"audio": (os.path.basename(audio_path), f, "audio/wav")}
            response = requests.post(url, files=files, timeout=60)
            response.raise_for_status()
        return True
    except requests.RequestException as e:
        print(f"[ERROR] Failed to upload audio for job {job_id}: {e}")
        return False
    except Exception as e:
        print(f"[ERROR] Unexpected error uploading audio: {e}")
        return False


def process_job(base_url: str, job: dict) -> bool:
    """
    Process a single TTS job:
    1. Get full job details
    2. Mark as processing
    3. Generate audio using VibeVoice
    4. Upload result
    
    Returns:
        True if job completed successfully, False otherwise
    """
    job_id = job["id"]
    print(f"\n{'='*60}")
    print(f"[WORKER] Processing job: {job_id}")
    print(f"[WORKER] Created: {job['created_at']}")
    print(f"{'='*60}")
    
    # Get full job details
    job_details = get_job_details(base_url, job_id)
    if not job_details:
        print(f"[ERROR] Could not retrieve job details for {job_id}, skipping")
        return False
    
    text = job_details.get("text", "")
    language = job_details.get("language", "en")
    
    if not text:
        print(f"[ERROR] Job {job_id} has no text, marking as failed")
        update_job_status(base_url, job_id, "failed", error="Empty text")
        return False
    
    print(f"[WORKER] Language: {language}")
    print(f"[WORKER] Text length: {len(text)} characters")
    print(f"[WORKER] Text preview: {text[:100]}...")
    
    # Mark as processing
    if not update_job_status(base_url, job_id, "processing"):
        print(f"[ERROR] Could not mark job {job_id} as processing")
        return False
    
    # Select speaker based on language
    speaker_name = SPEAKER_MAP.get(language, SPEAKER_MAP["en"])
    print(f"[WORKER] Using speaker: {speaker_name}")
    
    # Generate temporary output path
    temp_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "temp_audio")
    os.makedirs(temp_dir, exist_ok=True)
    temp_output_path = os.path.join(temp_dir, f"{job_id}.wav")
    
    # Generate audio
    print(f"[WORKER] Generating audio...")
    start_time = time.time()
    
    try:
        audio_path = generate_audio(
            text=text,
            language=language,
            speaker_name=speaker_name,
            output_path=temp_output_path
        )
        
        generation_time = time.time() - start_time
        print(f"[WORKER] Audio generation completed in {generation_time:.2f} seconds")
        
        if not audio_path or not os.path.exists(audio_path):
            print(f"[ERROR] Audio file was not generated")
            update_job_status(base_url, job_id, "failed", error="Audio generation failed")
            return False
        
        # Upload result
        print(f"[WORKER] Uploading audio result...")
        if upload_audio_result(base_url, job_id, audio_path):
            print(f"[WORKER] Job {job_id} completed successfully!")
            # Clean up temp file
            if os.path.exists(audio_path):
                os.remove(audio_path)
            return True
        else:
            print(f"[ERROR] Failed to upload audio result")
            update_job_status(base_url, job_id, "failed", error="Upload failed")
            return False
            
    except Exception as e:
        generation_time = time.time() - start_time
        error_msg = f"{type(e).__name__}: {str(e)}"
        print(f"[ERROR] Audio generation failed after {generation_time:.2f}s: {error_msg}")
        traceback.print_exc()
        
        update_job_status(base_url, job_id, "failed", error=error_msg)
        
        # Clean up temp file if exists
        if os.path.exists(temp_output_path):
            os.remove(temp_output_path)
        
        return False


def run_worker(base_url: str, poll_interval: int = 5, idle_timeout: int = 60,
               model_path: Optional[str] = None, device: Optional[str] = None,
               cfg_scale: float = 1.3, seed: Optional[int] = None):
    """
    Main worker loop that polls for pending jobs and processes them.
    Model is lazily loaded when jobs are found and unloaded after idle_timeout seconds.

    Args:
        base_url: Base URL of the toolbox API
        poll_interval: Seconds between polls
        idle_timeout: Seconds of idle time before unloading the model
        model_path: Path to model (passed to load_model)
        device: Device for inference (passed to load_model)
        cfg_scale: CFG scale (passed to load_model)
        seed: Random seed (passed to load_model)
    """
    print("=" * 60)
    print("VibeVoice TTS Worker Service")
    print("=" * 60)
    print(f"Base URL: {base_url}")
    print(f"Poll interval: {poll_interval} seconds")
    print(f"Idle timeout: {idle_timeout} seconds (model will unload after this idle period)")
    print(f"Started at: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Press Ctrl+C to stop")
    print("=" * 60)

    consecutive_errors = 0
    idle_start: Optional[float] = None  # When did we start being idle?

    while True:
        try:
            # Get pending jobs
            pending_jobs = get_pending_jobs(base_url)

            if not pending_jobs:
                consecutive_errors = 0

                # Track idle time
                if idle_start is None:
                    idle_start = time.time()
                    print(f"\n[{datetime.datetime.now().strftime('%H:%M:%S')}] No pending jobs, entering idle mode...")

                idle_elapsed = time.time() - idle_start
                if is_model_loaded() and idle_elapsed >= idle_timeout:
                    unload_model()

                time.sleep(poll_interval)
                continue

            # We have jobs — reset idle timer
            idle_start = None

            consecutive_errors = 0
            print(f"\n[{datetime.datetime.now().strftime('%H:%M:%S')}] Found {len(pending_jobs)} pending job(s)")

            # Load model if not already loaded
            if not is_model_loaded():
                print(f"[WORKER] No model loaded, loading now...")
                load_start = time.time()
                if not load_model(
                    model_path=model_path,
                    device=device,
                    cfg_scale=cfg_scale,
                    seed=seed
                ):
                    print("[ERROR] Failed to load model, will retry next poll")
                    time.sleep(poll_interval)
                    continue
                print(f"[WORKER] Model loaded in {time.time() - load_start:.2f} seconds")

            # Process ALL pending jobs while model is loaded (batch processing)
            # The API returns newest first, so reverse for FIFO
            jobs_to_process = list(reversed(pending_jobs))
            for job_to_process in jobs_to_process:
                success = process_job(base_url, job_to_process)

                if success:
                    print(f"[WORKER] Job processed successfully")
                else:
                    print(f"[WORKER] Job processing failed")

                # Re-fetch pending jobs in case new ones arrived
                pending_jobs = get_pending_jobs(base_url)
                if not pending_jobs:
                    break

        except KeyboardInterrupt:
            print("\n\n[WORKER] Received shutdown signal, stopping...")
            # Unload model on shutdown
            if is_model_loaded():
                unload_model()
            break
        except Exception as e:
            consecutive_errors += 1
            error_msg = f"{type(e).__name__}: {str(e)}"
            print(f"\n[WORKER] Unexpected error in main loop ({consecutive_errors} consecutive): {error_msg}")
            traceback.print_exc()

            # Back off on repeated errors
            backoff_time = min(poll_interval * (2 ** min(consecutive_errors - 1, 3)), 60)
            print(f"[WORKER] Backing off for {backoff_time} seconds...")
            time.sleep(backoff_time)
        finally:
            # Always wait before next poll
            time.sleep(poll_interval)

    print(f"\n[WORKER] Service stopped at {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")


def parse_args():
    parser = argparse.ArgumentParser(description="VibeVoice TTS Worker Service")
    parser.add_argument(
        "--base_url",
        type=str,
        default="http://localhost:5000",
        help="Base URL of the toolbox API (default: http://localhost:5000)"
    )
    parser.add_argument(
        "--model_path",
        type=str,
        default=None,
        help="Path to the model directory (auto-detects local models/VibeVoice-1.5b if not specified)"
    )
    parser.add_argument(
        "--device",
        type=str,
        default=None,
        help="Device for inference: cuda, mps, cpu (auto-detect if not specified)"
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=5,
        help="Polling interval in seconds (default: 5)"
    )
    parser.add_argument(
        "--idle_timeout",
        type=int,
        default=60,
        help="Seconds of idle time before unloading the model (default: 60)"
    )
    parser.add_argument(
        "--cfg_scale",
        type=float,
        default=1.3,
        help="CFG scale for generation (default: 1.3)"
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Random seed for reproducibility (optional)"
    )
    return parser.parse_args()


def main():
    args = parse_args()

    # Store model config for lazy loading (no model loaded at startup)
    print("=" * 60)
    print("VibeVoice TTS Worker — Lazy Load Mode")
    print("=" * 60)
    print(f"Model will be loaded on demand when jobs arrive")
    print(f"Model will unload after {args.idle_timeout}s of idle time")
    print(f"Model path: {args.model_path or '(auto-detect)'}")
    print(f"Device: {args.device or '(auto-detect)'}")
    print("=" * 60)

    # Start the worker loop (model loads lazily on first job)
    run_worker(
        base_url=args.base_url,
        poll_interval=args.interval,
        idle_timeout=args.idle_timeout,
        model_path=args.model_path,
        device=args.device,
        cfg_scale=args.cfg_scale,
        seed=args.seed,
    )


if __name__ == "__main__":
    main()
