#!/usr/bin/env python3
"""Lightweight cross-platform Machine Discovery Agent for Web Toolbox (Linux & Windows).

Zero third-party dependencies (Python 3.8+ standard library only).
Features:
  - Persistent HTTPS HTTP/1.1 Keep-Alive connection to minimize CPU & network packets
  - Zero-subprocess IPv4 & IPv6 discovery cached across heartbeats
  - Split full registration (/api/machines/register) vs 204 heartbeat pulse (/api/machines/heartbeat)
  - Automatic re-registration on 205 Reset Content or IP/status change
  - Optional local service probes (--check-tcp, --check-http, --check-service, --status-file)
  - Graceful deregistration on SIGINT / SIGTERM
  - Built-in background service installer for Linux (systemd) and Windows (Scheduled Task)

Examples:
  # Run interactively against production:
  python3 machine_agent.py --server https://byang.japaneast.cloudapp.azure.com --api-key <KEY>

  # Monitor local service port 8080 and report degraded if down:
  python3 machine_agent.py --server https://byang.japaneast.cloudapp.azure.com --api-key <KEY> --check-tcp 127.0.0.1:8080

  # Install as background auto-start service (Linux systemd / Windows Scheduled Task):
  sudo python3 machine_agent.py --server https://byang.japaneast.cloudapp.azure.com --api-key <KEY> --install-service
"""
from __future__ import annotations

import argparse
import http.client
import ipaddress
import json
import os
import platform
import random
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_SERVER_URL = "https://byang.japaneast.cloudapp.azure.com"
DEFAULT_INTERVAL_SEC = 60
DEFAULT_IP_REFRESH_SEC = 300
DEFAULT_FULL_SYNC_EVERY_SEC = 900  # Periodic full metadata sync every 15 min
VALID_STATUSES = {"serving", "busy", "draining", "degraded", "maintenance", "stopped"}


# ============================================================
# Persistent Machine Identity & OS Metadata
# ============================================================

def _default_state_dir() -> Path:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("PROGRAMDATA")
        if base:
            return Path(base) / "Toolbox"
        return Path.home() / ".toolbox"
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return Path("/var/lib/toolbox-agent")
    return Path.home() / ".toolbox"


def load_or_create_machine_id(explicit_id: Optional[str] = None) -> str:
    candidate = (explicit_id or os.environ.get("TOOLBOX_MACHINE_ID") or "").strip()
    if candidate:
        return candidate

    id_file = _default_state_dir() / "machine_id"
    try:
        if id_file.is_file():
            saved = id_file.read_text(encoding="utf-8").strip()
            if saved:
                return saved
    except OSError:
        pass

    generated = str(uuid.uuid4())
    try:
        id_file.parent.mkdir(parents=True, exist_ok=True)
        id_file.write_text(generated + "\n", encoding="utf-8")
    except OSError:
        # Read-only filesystem fallback: deterministic UUIDv5 from hostname + MAC
        seed = f"{platform.node()}-{uuid.getnode()}"
        return str(uuid.uuid5(uuid.NAMESPACE_DNS, seed))
    return generated


def get_os_summary() -> str:
    sys_name = platform.system() or "Unknown"
    release = platform.release() or ""
    arch = platform.machine() or ""
    if sys_name == "Linux":
        try:
            os_rel = Path("/etc/os-release")
            if os_rel.is_file():
                for line in os_rel.read_text(encoding="utf-8", errors="ignore").splitlines():
                    if line.startswith("PRETTY_NAME="):
                        pretty = line.split("=", 1)[1].strip().strip('"')
                        return f"{pretty} ({arch})"[:128]
        except OSError:
            pass
    return f"{sys_name} {release} {arch}".strip()[:128]


# ============================================================
# Zero-Subprocess IPv4 & IPv6 Discovery
# ============================================================

def _is_usable_ipv4(addr_str: str) -> bool:
    try:
        ip = ipaddress.IPv4Address(addr_str)
    except ValueError:
        return False
    return not (
        ip.is_loopback
        or ip.is_link_local
        or ip.is_unspecified
        or ip.is_multicast
    )


def _classify_ipv6(addr_str: str) -> int:
    """Return priority score (higher is better; 0 = unusable loopback/link-local)."""
    clean = addr_str.split("%", 1)[0].strip()
    try:
        ip = ipaddress.IPv6Address(clean)
    except ValueError:
        return 0
    if (
        ip.is_loopback
        or ip.is_link_local
        or ip.is_unspecified
        or ip.is_multicast
        or ip.ipv4_mapped is not None
    ):
        return 0
    if ip.teredo is not None or ip.sixtofour is not None:
        return 1
    if ip.is_global:
        return 3
    if ip.is_private:  # ULA fc00::/7
        return 2
    return 1


def discover_primary_ipv4() -> Optional[str]:
    # 1. Non-transmitting UDP route lookup (0 network packets, 0 subprocesses)
    for target in (("8.8.8.8", 80), ("1.1.1.1", 80), ("10.255.255.255", 80)):
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.connect(target)
            ip = sock.getsockname()[0]
            if _is_usable_ipv4(ip):
                return ip
        except OSError:
            pass
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass

    # 2. Fallback to local hostname interface resolution (air-gapped LANs)
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
        for info in infos:
            ip = str(info[4][0])
            if _is_usable_ipv4(ip):
                return ip
    except OSError:
        pass
    return None


def _read_linux_proc_ipv6() -> List[str]:
    results: List[str] = []
    proc_path = Path("/proc/net/if_inet6")
    if not proc_path.is_file():
        return results
    try:
        for line in proc_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            parts = line.split()
            if len(parts) < 6:
                continue
            hex_ip, _ifidx, _plen, scope_hex, _flags, ifname = parts[:6]
            if ifname == "lo" or len(hex_ip) != 32:
                continue
            scope = int(scope_hex, 16)
            if scope in (0x10, 0x20):  # loopback or link-local
                continue
            grouped = ":".join(hex_ip[i : i + 4] for i in range(0, 32, 4))
            try:
                results.append(ipaddress.IPv6Address(grouped).compressed)
            except ValueError:
                continue
    except (OSError, ValueError):
        pass
    return results


def discover_primary_ipv6() -> Optional[str]:
    # 1. Non-transmitting UDPv6 route lookup
    for target in (("2001:4860:4860::8888", 80), ("2606:4700:4700::1111", 80)):
        sock = None
        try:
            sock = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
            sock.connect(target)
            ip = sock.getsockname()[0].split("%", 1)[0]
            if _classify_ipv6(ip) >= 2:
                return ipaddress.IPv6Address(ip).compressed
        except OSError:
            pass
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass

    candidates: List[Tuple[int, str]] = []

    # 2. Linux /proc/net/if_inet6 pure file read
    if os.name != "nt":
        for ip in _read_linux_proc_ipv6():
            score = _classify_ipv6(ip)
            if score > 0:
                candidates.append((score, ip))

    # 3. Cross-platform getaddrinfo(AF_INET6) for Windows / macOS / BSD / Linux
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET6)
        for info in infos:
            raw_ip = str(info[4][0]).split("%", 1)[0]
            score = _classify_ipv6(raw_ip)
            if score > 0:
                candidates.append((score, ipaddress.IPv6Address(raw_ip).compressed))
    except OSError:
        pass

    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


# ============================================================
# Optional Local Service Health Probes
# ============================================================

def _probe_tcp(target: str, timeout: float = 2.0) -> Tuple[bool, str]:
    host, sep, port_str = target.rpartition(":")
    if not sep or not host:
        return False, f"invalid tcp target {target}"
    host = host.strip("[]")
    try:
        port = int(port_str)
    except ValueError:
        return False, f"invalid tcp port {target}"
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, f"tcp:{port} ok"
    except OSError:
        return False, f"tcp:{target} unreachable"


def _probe_http(url: str, timeout: float = 3.0) -> Tuple[bool, str]:
    ctx = None
    if url.lower().startswith("https://"):
        ctx = ssl._create_unverified_context()
    req = urllib.request.Request(url, method="GET", headers={"User-Agent": "ToolboxAgentProbe/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            code = getattr(resp, "status", 200)
            if 200 <= code < 300:
                return True, f"http:{code}"
            return False, f"http:{url} returned {code}"
    except Exception as exc:
        return False, f"http:{url} failed ({type(exc).__name__})"


def _probe_service(service_name: str, timeout: float = 3.0) -> Tuple[bool, str]:
    try:
        if os.name == "nt":
            proc = subprocess.run(
                ["sc.exe", "query", service_name],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            running = proc.returncode == 0 and "RUNNING" in (proc.stdout or "")
            return (True, f"svc:{service_name} running") if running else (False, f"svc:{service_name} not running")
        proc = subprocess.run(
            ["systemctl", "is-active", "--quiet", service_name],
            capture_output=True,
            timeout=timeout,
        )
        return (
            (True, f"svc:{service_name} active")
            if proc.returncode == 0
            else (False, f"svc:{service_name} inactive")
        )
    except Exception as exc:
        return False, f"svc:{service_name} check error ({type(exc).__name__})"


def evaluate_local_serving_status(
    *,
    base_status: str,
    base_detail: str,
    status_file: Optional[str],
    tcp_checks: List[str],
    http_checks: List[str],
    service_checks: List[str],
) -> Tuple[str, str]:
    status = base_status
    details: List[str] = [base_detail] if base_detail else []

    if status_file:
        p = Path(status_file)
        if p.is_file():
            try:
                line = p.read_text(encoding="utf-8", errors="ignore").splitlines()[0].strip()
                if line:
                    raw_st, _, raw_det = line.partition("|")
                    st = raw_st.strip().lower()
                    if st in VALID_STATUSES:
                        status = st
                    if raw_det.strip():
                        details.append(raw_det.strip())
            except (OSError, IndexError):
                pass

    failures: List[str] = []
    ok_notes: List[str] = []

    for tcp_target in tcp_checks:
        ok, msg = _probe_tcp(tcp_target)
        (ok_notes if ok else failures).append(msg)

    for http_url in http_checks:
        ok, msg = _probe_http(http_url)
        (ok_notes if ok else failures).append(msg)

    for svc_name in service_checks:
        ok, msg = _probe_service(svc_name)
        (ok_notes if ok else failures).append(msg)

    if failures:
        status = "degraded"
        details.extend(failures)
    elif ok_notes and not details:
        details.extend(ok_notes)

    return status, "; ".join(details)[:256]


# ============================================================
# Persistent HTTP/1.1 Keep-Alive Discovery Client
# ============================================================

class ToolboxDiscoveryClient:
    """Reuses a single TCP/TLS connection across 60s heartbeats."""

    def __init__(
        self,
        *,
        server_url: str,
        api_key: str,
        verify_ssl: bool = False,
        ca_cert: Optional[str] = None,
        timeout: float = 10.0,
    ) -> None:
        parsed = urllib.parse.urlparse(server_url.rstrip("/"))
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError(f"Invalid --server URL: {server_url!r}")

        self.scheme = parsed.scheme
        self.host = parsed.hostname
        self.port = parsed.port or (443 if self.scheme == "https" else 80)
        self.base_path = parsed.path.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self._conn: Optional[http.client.HTTPConnection] = None

        self._ssl_context: Optional[ssl.SSLContext] = None
        if self.scheme == "https":
            if verify_ssl or ca_cert:
                ctx = ssl.create_default_context(cafile=ca_cert)
            else:
                ctx = ssl._create_unverified_context()
            self._ssl_context = ctx

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    def _ensure_conn(self) -> http.client.HTTPConnection:
        if self._conn is None:
            if self.scheme == "https":
                self._conn = http.client.HTTPSConnection(
                    self.host,
                    self.port,
                    timeout=self.timeout,
                    context=self._ssl_context,
                )
            else:
                self._conn = http.client.HTTPConnection(
                    self.host,
                    self.port,
                    timeout=self.timeout,
                )
        return self._conn

    def request_json(
        self,
        method: str,
        subpath: str,
        payload: Dict[str, Any],
    ) -> Tuple[int, bytes]:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        full_path = f"{self.base_path}{subpath}"
        headers = {
            "Content-Type": "application/json",
            "Content-Length": str(len(body)),
            "Connection": "keep-alive",
            "X-API-Key": self.api_key,
            "User-Agent": "ToolboxMachineAgent/1.0",
        }

        last_exc: Optional[Exception] = None
        for attempt in range(2):
            conn = self._ensure_conn()
            try:
                conn.request(method, full_path, body=body, headers=headers)
                resp = conn.getresponse()
                resp_bytes = resp.read()  # Must drain response to keep socket reusable
                if resp.will_close:
                    self.close()
                return resp.status, resp_bytes
            except (
                http.client.HTTPException,
                ConnectionError,
                TimeoutError,
                ssl.SSLError,
                OSError,
            ) as exc:
                last_exc = exc
                self.close()
                if attempt == 0:
                    continue
                raise
        raise RuntimeError(f"Request failed: {last_exc}")


# ============================================================
# Background Service Installation (Linux systemd & Windows Task)
# ============================================================

LINUX_UNIT_NAME = "toolbox-machine-agent.service"
LINUX_UNIT_PATH = Path("/etc/systemd/system") / LINUX_UNIT_NAME
WINDOWS_TASK_NAME = "ToolboxMachineAgent"


def _build_agent_cli_args(args: argparse.Namespace) -> List[str]:
    cli: List[str] = [
        "--server",
        args.server,
        "--api-key",
        args.api_key,
        "--interval",
        str(args.interval),
        "--status",
        args.status,
    ]
    if args.name:
        cli.extend(["--name", args.name])
    if args.machine_id:
        cli.extend(["--machine-id", args.machine_id])
    if args.ipv4:
        cli.extend(["--ipv4", args.ipv4])
    if args.ipv6:
        cli.extend(["--ipv6", args.ipv6])
    if args.detail:
        cli.extend(["--detail", args.detail])
    if args.status_file:
        cli.extend(["--status-file", args.status_file])
    for t in args.check_tcp:
        cli.extend(["--check-tcp", t])
    for h in args.check_http:
        cli.extend(["--check-http", h])
    for s in args.check_service:
        cli.extend(["--check-service", s])
    if args.verify_ssl:
        cli.append("--verify-ssl")
    if args.ca_cert:
        cli.extend(["--ca-cert", args.ca_cert])
    return cli


def install_background_service(args: argparse.Namespace) -> int:
    src_script = Path(__file__).resolve()

    if os.name == "nt":
        install_dir = _default_state_dir()
        install_dir.mkdir(parents=True, exist_ok=True)
        dest_script = install_dir / "machine_agent.py"
        if src_script != dest_script:
            shutil.copy2(src_script, dest_script)

        py_exe = Path(sys.executable)
        pythonw = py_exe.with_name("pythonw.exe")
        runner = str(pythonw if pythonw.is_file() else py_exe)

        cmd_parts = [runner, str(dest_script)] + _build_agent_cli_args(args)
        tr_cmd = subprocess.list2cmdline(cmd_parts)

        res = subprocess.run(
            [
                "schtasks.exe",
                "/Create",
                "/F",
                "/SC",
                "ONLOGON",
                "/TN",
                WINDOWS_TASK_NAME,
                "/TR",
                tr_cmd,
            ],
            capture_output=True,
            text=True,
        )
        if res.returncode != 0:
            print(f"[ERROR] Failed to register Scheduled Task: {res.stderr.strip() or res.stdout.strip()}", file=sys.stderr)
            return 1
        subprocess.run(["schtasks.exe", "/Run", "/TN", WINDOWS_TASK_NAME], capture_output=True)
        print(f"[OK] Installed and started Windows Scheduled Task '{WINDOWS_TASK_NAME}' -> {dest_script}")
        return 0

    if hasattr(os, "geteuid") and os.geteuid() != 0:
        print("[ERROR] --install-service on Linux requires root (re-run with sudo).", file=sys.stderr)
        return 1

    install_dir = Path("/opt/toolbox-agent")
    install_dir.mkdir(parents=True, exist_ok=True)
    dest_script = install_dir / "machine_agent.py"
    if src_script != dest_script:
        shutil.copy2(src_script, dest_script)
        os.chmod(dest_script, 0o755)

    env_file = install_dir / "agent.env"
    env_file.write_text(f"TOOLBOX_API_KEY={args.api_key}\n", encoding="utf-8")
    os.chmod(env_file, 0o600)

    # Strip --api-key from command line on Linux so it isn't visible in `ps aux`
    safe_args = _build_agent_cli_args(args)
    cleaned_args: List[str] = []
    skip_next = False
    for tok in safe_args:
        if skip_next:
            skip_next = False
            continue
        if tok == "--api-key":
            skip_next = True
            continue
        cleaned_args.append(tok)

    import shlex
    exec_args = " ".join(shlex.quote(x) for x in ([sys.executable, str(dest_script)] + cleaned_args))
    unit_content = f"""[Unit]
Description=Web Toolbox Machine Discovery Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile={env_file}
ExecStart={exec_args}
Nice=19
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
"""
    LINUX_UNIT_PATH.write_text(unit_content, encoding="utf-8")
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "enable", "--now", LINUX_UNIT_NAME], check=True)
    print(f"[OK] Installed and started systemd service '{LINUX_UNIT_NAME}'")
    return 0


def uninstall_background_service() -> int:
    if os.name == "nt":
        res = subprocess.run(
            ["schtasks.exe", "/Delete", "/F", "/TN", WINDOWS_TASK_NAME],
            capture_output=True,
            text=True,
        )
        if res.returncode != 0:
            print(f"[WARN] Scheduled task removal output: {res.stderr.strip() or res.stdout.strip()}", file=sys.stderr)
            return 1
        print(f"[OK] Removed Windows Scheduled Task '{WINDOWS_TASK_NAME}'")
        return 0

    if hasattr(os, "geteuid") and os.geteuid() != 0:
        print("[ERROR] --uninstall-service on Linux requires root (re-run with sudo).", file=sys.stderr)
        return 1

    subprocess.run(["systemctl", "disable", "--now", LINUX_UNIT_NAME], capture_output=True)
    if LINUX_UNIT_PATH.exists():
        LINUX_UNIT_PATH.unlink()
    subprocess.run(["systemctl", "daemon-reload"], capture_output=True)
    print(f"[OK] Uninstalled systemd service '{LINUX_UNIT_NAME}'")
    return 0


# ============================================================
# Main Agent Loop
# ============================================================

def run_agent(args: argparse.Namespace) -> int:
    api_key = (args.api_key or os.environ.get("TOOLBOX_API_KEY") or "").strip()
    if not api_key:
        print(
            "[ERROR] Missing API key. Pass --api-key <KEY> or set TOOLBOX_API_KEY.",
            file=sys.stderr,
        )
        return 2

    machine_id = load_or_create_machine_id(args.machine_id)
    machine_name = (args.name or os.environ.get("TOOLBOX_MACHINE_NAME") or socket.gethostname() or "unknown-host").strip()
    os_info = get_os_summary()
    interval = max(10, min(600, int(args.interval)))

    stop_event = threading.Event()

    def _on_signal(_signum: int, _frame: Any) -> None:
        stop_event.set()

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(getattr(signal, "SIGBREAK"), _on_signal)

    client = ToolboxDiscoveryClient(
        server_url=args.server,
        api_key=api_key,
        verify_ssl=args.verify_ssl,
        ca_cert=args.ca_cert,
    )

    cached_ipv4: Optional[str] = args.ipv4
    cached_ipv6: Optional[str] = args.ipv6
    last_ip_check_at = 0.0
    last_full_register_at = 0.0
    registered = False
    last_sent_status = ""
    last_sent_detail = ""
    consecutive_failures = 0

    print(
        f"[ToolboxAgent] Starting machine_name={machine_name!r} id={machine_id} "
        f"server={args.server} interval={interval}s"
    )

    try:
        while not stop_event.is_set():
            now = time.monotonic()

            # Refresh local interface IPs periodically (zero subprocesses)
            ip_changed = False
            if (now - last_ip_check_at) >= args.ip_refresh_interval or last_ip_check_at == 0.0:
                new_v4 = args.ipv4 or discover_primary_ipv4()
                new_v6 = args.ipv6 or discover_primary_ipv6()
                if last_ip_check_at > 0.0 and (new_v4 != cached_ipv4 or new_v6 != cached_ipv6):
                    ip_changed = True
                cached_ipv4, cached_ipv6 = new_v4, new_v6
                last_ip_check_at = now

            cur_status, cur_detail = evaluate_local_serving_status(
                base_status=args.status,
                base_detail=args.detail or "",
                status_file=args.status_file,
                tcp_checks=args.check_tcp,
                http_checks=args.check_http,
                service_checks=args.check_service,
            )

            need_full_register = (
                not registered
                or ip_changed
                or (now - last_full_register_at) >= DEFAULT_FULL_SYNC_EVERY_SEC
            )

            try:
                if need_full_register:
                    reg_payload = {
                        "machine_id": machine_id,
                        "machine_name": machine_name,
                        "ipv4": cached_ipv4,
                        "ipv6": cached_ipv6,
                        "serving_status": cur_status,
                        "status_detail": cur_detail,
                        "os_info": os_info,
                        "heartbeat_interval": interval,
                    }
                    code, body = client.request_json("POST", "/api/machines/register", reg_payload)
                    if code == 200:
                        registered = True
                        last_full_register_at = now
                        last_sent_status = cur_status
                        last_sent_detail = cur_detail
                        consecutive_failures = 0
                        if not args.quiet:
                            print(
                                f"[ToolboxAgent] Registered {machine_name} "
                                f"(ipv4={cached_ipv4 or '-'}, ipv6={cached_ipv6 or '-'}, status={cur_status})"
                            )
                    else:
                        raise RuntimeError(f"HTTP {code}: {body.decode('utf-8', errors='replace')[:200]}")
                else:
                    hb_payload: Dict[str, Any] = {"machine_id": machine_id}
                    if cur_status != last_sent_status:
                        hb_payload["serving_status"] = cur_status
                    if cur_detail != last_sent_detail:
                        hb_payload["status_detail"] = cur_detail

                    code, body = client.request_json("POST", "/api/machines/heartbeat", hb_payload)
                    if code == 204:
                        last_sent_status = cur_status
                        last_sent_detail = cur_detail
                        consecutive_failures = 0
                    elif code == 205:
                        # Server requested full re-registration
                        registered = False
                        continue
                    else:
                        raise RuntimeError(f"HTTP {code}: {body.decode('utf-8', errors='replace')[:200]}")

            except Exception as exc:
                consecutive_failures += 1
                print(
                    f"[ToolboxAgent] Report error (attempt {consecutive_failures}): {exc}",
                    file=sys.stderr,
                )
                if args.once:
                    return 1
                backoff = min(interval, 5 * (2 ** min(consecutive_failures - 1, 4)))
                jitter = random.uniform(0.0, 2.0)
                stop_event.wait(backoff + jitter)
                continue

            if args.once:
                return 0

            # Sleep interval ± ~8% jitter so fleets don't synchronize pulses
            jitter_bound = min(5.0, interval * 0.08)
            sleep_sec = max(5.0, interval + random.uniform(-jitter_bound, jitter_bound))
            stop_event.wait(sleep_sec)

        # Graceful shutdown deregistration
        if registered and not args.once:
            try:
                client.timeout = 3.0
                client.request_json(
                    "POST",
                    "/api/machines/deregister",
                    {"machine_id": machine_id, "status_detail": "agent stopped"},
                )
                if not args.quiet:
                    print(f"[ToolboxAgent] Deregistered {machine_name} cleanly.")
            except Exception:
                pass
    finally:
        client.close()

    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Lightweight Machine Discovery & Heartbeat Agent for Web Toolbox."
    )
    p.add_argument(
        "--server",
        default=os.environ.get("TOOLBOX_SERVER_URL", DEFAULT_SERVER_URL),
        help=f"Toolbox server base URL (default: {DEFAULT_SERVER_URL})",
    )
    p.add_argument(
        "--api-key",
        default=os.environ.get("TOOLBOX_API_KEY", ""),
        help="Shared Toolbox REST API key (or set TOOLBOX_API_KEY env var)",
    )
    p.add_argument("--name", default=None, help="Machine name override (default: system hostname)")
    p.add_argument("--machine-id", default=None, help="Explicit persistent machine ID override")
    p.add_argument(
        "--status",
        default="serving",
        choices=sorted(VALID_STATUSES),
        help="Base machine serving status (default: serving)",
    )
    p.add_argument("--detail", default=None, help="Optional status detail note")
    p.add_argument(
        "--status-file",
        default=None,
        help="Optional local file containing 'status|detail' updated by local workloads",
    )
    p.add_argument(
        "--check-tcp",
        action="append",
        default=[],
        metavar="HOST:PORT",
        help="Mark status 'degraded' if TCP port is unreachable (repeatable)",
    )
    p.add_argument(
        "--check-http",
        action="append",
        default=[],
        metavar="URL",
        help="Mark status 'degraded' if HTTP(S) URL does not return 2xx (repeatable)",
    )
    p.add_argument(
        "--check-service",
        action="append",
        default=[],
        metavar="SERVICE",
        help="Mark status 'degraded' if systemd/Windows service is not active (repeatable)",
    )
    p.add_argument("--ipv4", default=None, help="Explicit IPv4 address override")
    p.add_argument("--ipv6", default=None, help="Explicit IPv6 address override")
    p.add_argument(
        "--interval",
        type=int,
        default=DEFAULT_INTERVAL_SEC,
        help=f"Heartbeat interval in seconds (default: {DEFAULT_INTERVAL_SEC})",
    )
    p.add_argument(
        "--ip-refresh-interval",
        type=int,
        default=DEFAULT_IP_REFRESH_SEC,
        help=f"How often to re-check local interface IPs in seconds (default: {DEFAULT_IP_REFRESH_SEC})",
    )
    p.add_argument(
        "--verify-ssl",
        action="store_true",
        help="Enforce strict CA verification (off by default for self-signed Toolbox cert)",
    )
    p.add_argument("--ca-cert", default=None, help="Path to custom CA/server PEM certificate")
    p.add_argument("--once", action="store_true", help="Send one registration report and exit")
    p.add_argument("--quiet", action="store_true", help="Suppress routine stdout log lines")
    p.add_argument(
        "--install-service",
        action="store_true",
        help="Install and start as systemd service (Linux) or Scheduled Task (Windows)",
    )
    p.add_argument(
        "--uninstall-service",
        action="store_true",
        help="Remove installed systemd service (Linux) or Scheduled Task (Windows)",
    )
    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if args.uninstall_service:
        return uninstall_background_service()
    if args.install_service:
        if not (args.api_key or os.environ.get("TOOLBOX_API_KEY")):
            print("[ERROR] --api-key is required with --install-service", file=sys.stderr)
            return 2
        return install_background_service(args)

    return run_agent(args)


if __name__ == "__main__":
    sys.exit(main())
