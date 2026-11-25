"""In-memory heartbeat cache + throttled SQLite WAL store for Machine Discovery."""
from __future__ import annotations

import datetime
import ipaddress
import os
import re
import threading
import time
from typing import Any, Callable, Dict, List, Optional

import db as job_db

DEFAULT_HEARTBEAT_INTERVAL = 60
MIN_HEARTBEAT_INTERVAL = 10
MAX_HEARTBEAT_INTERVAL = 600
DB_HEARTBEAT_FLUSH_SEC = 300.0  # Flush unchanged heartbeat timestamps at most every 5 min

VALID_SERVING_STATUSES = frozenset(
    {"serving", "busy", "draining", "degraded", "maintenance", "stopped"}
)

_MACHINE_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


class MachineValidationError(ValueError):
    """Raised when incoming machine registration or heartbeat fields are invalid."""


def _epoch_to_iso_utc(epoch_sec: float) -> str:
    return (
        datetime.datetime.fromtimestamp(epoch_sec, tz=datetime.timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def validate_machine_id(raw: Any) -> str:
    val = str(raw or "").strip()
    if not val or not _MACHINE_ID_RE.match(val):
        raise MachineValidationError(
            "Invalid machine_id: must be 1-128 characters matching [A-Za-z0-9._:-]"
        )
    return val


def validate_machine_name(raw: Any) -> str:
    val = str(raw or "").strip()
    if not val:
        raise MachineValidationError("machine_name is required")
    if len(val) > 128:
        raise MachineValidationError("machine_name must be at most 128 characters")
    if any(ord(ch) < 32 for ch in val):
        raise MachineValidationError("machine_name contains invalid control characters")
    return val


def normalize_ipv4(raw: Any) -> Optional[str]:
    if raw is None:
        return None
    val = str(raw).strip()
    if not val:
        return None
    try:
        addr = ipaddress.IPv4Address(val)
    except ValueError as exc:
        raise MachineValidationError(f"Invalid IPv4 address: {val!r}") from exc
    return str(addr)


def normalize_ipv6(raw: Any) -> Optional[str]:
    if raw is None:
        return None
    val = str(raw).strip()
    if not val:
        return None
    zone_stripped = val.split("%", 1)[0]
    try:
        addr = ipaddress.IPv6Address(zone_stripped)
    except ValueError as exc:
        raise MachineValidationError(f"Invalid IPv6 address: {val!r}") from exc
    return addr.compressed


def normalize_observed_ip(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    candidate = raw.split(",", 1)[0].strip()
    if not candidate:
        return None
    zone_stripped = candidate.split("%", 1)[0]
    try:
        addr = ipaddress.ip_address(zone_stripped)
    except ValueError:
        return None
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return str(addr.ipv4_mapped)
    return addr.compressed


def validate_serving_status(raw: Any, *, default: str = "serving") -> str:
    if raw is None or str(raw).strip() == "":
        return default
    val = str(raw).strip().lower()
    if val not in VALID_SERVING_STATUSES:
        allowed = ", ".join(sorted(VALID_SERVING_STATUSES))
        raise MachineValidationError(f"Invalid serving_status {val!r}. Must be one of: {allowed}")
    return val


def validate_heartbeat_interval(raw: Any) -> int:
    if raw is None or str(raw).strip() == "":
        return DEFAULT_HEARTBEAT_INTERVAL
    try:
        val = int(raw)
    except (TypeError, ValueError) as exc:
        raise MachineValidationError("heartbeat_interval must be an integer") from exc
    if val < MIN_HEARTBEAT_INTERVAL or val > MAX_HEARTBEAT_INTERVAL:
        raise MachineValidationError(
            f"heartbeat_interval must be between {MIN_HEARTBEAT_INTERVAL} and {MAX_HEARTBEAT_INTERVAL} seconds"
        )
    return val


def sanitize_short_text(raw: Any, max_len: int = 256) -> str:
    if raw is None:
        return ""
    cleaned = "".join(ch if ord(ch) >= 32 else " " for ch in str(raw)).strip()
    return cleaned[:max_len]


def compute_machine_view(rec: Dict[str, Any], now: float) -> Dict[str, Any]:
    """Derive live health_status and effective_status without mutating stored state."""
    interval = int(rec["heartbeat_interval"] or DEFAULT_HEARTBEAT_INTERVAL)
    unhealthy_after = int(round(interval * 1.5))      # 90s for 60s interval
    unresponsive_after = int(interval * 3)            # 180s for 60s interval

    age_sec = max(0.0, now - float(rec["last_heartbeat_at"]))
    serving_status = rec["serving_status"]

    if serving_status == "stopped":
        health_status = "offline"
        effective_status = "offline"
    elif age_sec > unresponsive_after:
        health_status = "unresponsive"
        effective_status = "unresponsive"
    elif age_sec > unhealthy_after:
        health_status = "unhealthy"
        effective_status = "unhealthy"
    else:
        health_status = "healthy"
        effective_status = serving_status

    return {
        "machine_id": rec["machine_id"],
        "machine_name": rec["machine_name"],
        "ipv4": rec["ipv4"],
        "ipv6": rec["ipv6"],
        "public_ip": rec["public_ip"],
        "serving_status": serving_status,
        "health_status": health_status,
        "effective_status": effective_status,
        "status_detail": rec["status_detail"],
        "os_info": rec["os_info"],
        "heartbeat_interval": interval,
        "unhealthy_threshold_sec": unhealthy_after,
        "unresponsive_threshold_sec": unresponsive_after,
        "first_seen_epoch": int(rec["first_seen_at"]),
        "first_seen_at": _epoch_to_iso_utc(float(rec["first_seen_at"])),
        "last_heartbeat_epoch": int(rec["last_heartbeat_at"]),
        "last_heartbeat_at": _epoch_to_iso_utc(float(rec["last_heartbeat_at"])),
        "seconds_since_heartbeat": int(age_sec),
    }


class MachineStore:
    """Coherent in-memory machine registry backed by SQLite WAL persistence."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.time,
        db_flush_interval_sec: float = DB_HEARTBEAT_FLUSH_SEC,
    ) -> None:
        self._lock = threading.RLock()
        self._clock = clock
        self._db_flush_interval_sec = db_flush_interval_sec
        # Keyed by resolved DB_PATH so test suites switching DB_PATH stay isolated
        self._caches_by_db: Dict[str, Dict[str, Dict[str, Any]]] = {}

    def _get_cache(self) -> Dict[str, Dict[str, Any]]:
        db_key = os.path.abspath(job_db.DB_PATH)
        cache = self._caches_by_db.get(db_key)
        if cache is not None:
            return cache

        conn = job_db._get_conn()
        rows = conn.execute("SELECT * FROM machines").fetchall()
        loaded: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            item = dict(row)
            item["_last_db_flush_at"] = float(item["last_heartbeat_at"])
            loaded[item["machine_id"]] = item
        self._caches_by_db[db_key] = loaded
        return loaded

    def _persist_upsert(self, rec: Dict[str, Any]) -> None:
        conn = job_db._get_conn()
        conn.execute(
            """
            INSERT INTO machines (
                machine_id, machine_name, ipv4, ipv6, public_ip,
                serving_status, status_detail, os_info, heartbeat_interval,
                first_seen_at, last_heartbeat_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(machine_id) DO UPDATE SET
                machine_name = excluded.machine_name,
                ipv4 = excluded.ipv4,
                ipv6 = excluded.ipv6,
                public_ip = excluded.public_ip,
                serving_status = excluded.serving_status,
                status_detail = excluded.status_detail,
                os_info = excluded.os_info,
                heartbeat_interval = excluded.heartbeat_interval,
                last_heartbeat_at = excluded.last_heartbeat_at,
                updated_at = excluded.updated_at
            """,
            (
                rec["machine_id"],
                rec["machine_name"],
                rec["ipv4"],
                rec["ipv6"],
                rec["public_ip"],
                rec["serving_status"],
                rec["status_detail"],
                rec["os_info"],
                rec["heartbeat_interval"],
                rec["first_seen_at"],
                rec["last_heartbeat_at"],
                rec["updated_at"],
            ),
        )
        conn.commit()
        rec["_last_db_flush_at"] = float(rec["last_heartbeat_at"])

    def _persist_heartbeat_only(self, rec: Dict[str, Any]) -> None:
        conn = job_db._get_conn()
        conn.execute(
            "UPDATE machines SET last_heartbeat_at = ?, updated_at = ? WHERE machine_id = ?",
            (rec["last_heartbeat_at"], rec["updated_at"], rec["machine_id"]),
        )
        conn.commit()
        rec["_last_db_flush_at"] = float(rec["last_heartbeat_at"])

    def register(
        self,
        payload: Dict[str, Any],
        *,
        observed_ip: Optional[str] = None,
    ) -> Dict[str, Any]:
        machine_id = validate_machine_id(payload.get("machine_id"))
        machine_name = validate_machine_name(payload.get("machine_name"))
        ipv4 = normalize_ipv4(payload.get("ipv4"))
        ipv6 = normalize_ipv6(payload.get("ipv6"))
        public_ip = normalize_observed_ip(observed_ip)
        serving_status = validate_serving_status(payload.get("serving_status"), default="serving")
        status_detail = sanitize_short_text(payload.get("status_detail"), max_len=256)
        os_info = sanitize_short_text(payload.get("os_info"), max_len=128)
        heartbeat_interval = validate_heartbeat_interval(payload.get("heartbeat_interval"))

        now = self._clock()
        with self._lock:
            cache = self._get_cache()
            existing = cache.get(machine_id)
            first_seen = float(existing["first_seen_at"]) if existing else now
            rec: Dict[str, Any] = {
                "machine_id": machine_id,
                "machine_name": machine_name,
                "ipv4": ipv4,
                "ipv6": ipv6,
                "public_ip": public_ip if public_ip is not None else (existing["public_ip"] if existing else None),
                "serving_status": serving_status,
                "status_detail": status_detail,
                "os_info": os_info or (existing["os_info"] if existing else ""),
                "heartbeat_interval": heartbeat_interval,
                "first_seen_at": first_seen,
                "last_heartbeat_at": now,
                "updated_at": now,
                "_last_db_flush_at": now,
            }
            cache[machine_id] = rec
            self._persist_upsert(rec)
            return compute_machine_view(rec, now)

    def heartbeat(
        self,
        payload: Dict[str, Any],
        *,
        observed_ip: Optional[str] = None,
    ) -> bool:
        """Process a compact heartbeat pulse. Returns False if machine_id is unknown."""
        machine_id = validate_machine_id(payload.get("machine_id"))
        has_status = "serving_status" in payload and payload.get("serving_status") is not None
        new_status = (
            validate_serving_status(payload.get("serving_status"))
            if has_status
            else None
        )
        has_detail = "status_detail" in payload and payload.get("status_detail") is not None
        new_detail = (
            sanitize_short_text(payload.get("status_detail"), max_len=256)
            if has_detail
            else None
        )
        has_ipv4 = "ipv4" in payload
        new_ipv4 = normalize_ipv4(payload.get("ipv4")) if has_ipv4 else None
        has_ipv6 = "ipv6" in payload
        new_ipv6 = normalize_ipv6(payload.get("ipv6")) if has_ipv6 else None
        public_ip = normalize_observed_ip(observed_ip)

        now = self._clock()
        with self._lock:
            cache = self._get_cache()
            rec = cache.get(machine_id)
            if rec is None:
                return False

            metadata_changed = False
            if new_status is not None and new_status != rec["serving_status"]:
                rec["serving_status"] = new_status
                metadata_changed = True
            if new_detail is not None and new_detail != rec["status_detail"]:
                rec["status_detail"] = new_detail
                metadata_changed = True
            if has_ipv4 and new_ipv4 != rec["ipv4"]:
                rec["ipv4"] = new_ipv4
                metadata_changed = True
            if has_ipv6 and new_ipv6 != rec["ipv6"]:
                rec["ipv6"] = new_ipv6
                metadata_changed = True
            if public_ip is not None and public_ip != rec["public_ip"]:
                rec["public_ip"] = public_ip
                metadata_changed = True

            rec["last_heartbeat_at"] = now
            rec["updated_at"] = now

            if metadata_changed:
                self._persist_upsert(rec)
            elif (now - float(rec.get("_last_db_flush_at", 0.0))) >= self._db_flush_interval_sec:
                self._persist_heartbeat_only(rec)

            return True

    def deregister(self, payload: Dict[str, Any]) -> bool:
        """Mark a machine cleanly stopped on graceful agent exit."""
        machine_id = validate_machine_id(payload.get("machine_id"))
        detail = sanitize_short_text(payload.get("status_detail", "agent stopped"), max_len=256)
        now = self._clock()
        with self._lock:
            cache = self._get_cache()
            rec = cache.get(machine_id)
            if rec is None:
                return False
            rec["serving_status"] = "stopped"
            if detail:
                rec["status_detail"] = detail
            rec["last_heartbeat_at"] = now
            rec["updated_at"] = now
            self._persist_upsert(rec)
            return True

    def delete(self, machine_id: str) -> bool:
        mid = validate_machine_id(machine_id)
        with self._lock:
            cache = self._get_cache()
            removed = cache.pop(mid, None)
            conn = job_db._get_conn()
            cur = conn.execute("DELETE FROM machines WHERE machine_id = ?", (mid,))
            conn.commit()
            return (removed is not None) or (cur.rowcount > 0)

    def get(self, machine_id: str) -> Optional[Dict[str, Any]]:
        mid = validate_machine_id(machine_id)
        now = self._clock()
        with self._lock:
            cache = self._get_cache()
            rec = cache.get(mid)
            if rec is None:
                return None
            return compute_machine_view(rec, now)

    def list_machines(
        self,
        *,
        status_filter: Optional[str] = None,
        health_filter: Optional[str] = None,
        query: Optional[str] = None,
    ) -> Dict[str, Any]:
        now = self._clock()
        with self._lock:
            cache = self._get_cache()
            all_views = [compute_machine_view(rec, now) for rec in cache.values()]

        all_views.sort(key=lambda m: (m["machine_name"].lower(), m["machine_id"]))

        summary = {
            "total": len(all_views),
            "serving": sum(1 for m in all_views if m["effective_status"] == "serving"),
            "healthy": sum(1 for m in all_views if m["health_status"] == "healthy"),
            "degraded_or_busy": sum(
                1
                for m in all_views
                if m["health_status"] == "healthy" and m["serving_status"] != "serving"
            ),
            "unhealthy": sum(1 for m in all_views if m["health_status"] == "unhealthy"),
            "unresponsive": sum(1 for m in all_views if m["health_status"] == "unresponsive"),
            "offline": sum(1 for m in all_views if m["health_status"] == "offline"),
        }

        filtered = all_views
        if status_filter:
            sf = status_filter.strip().lower()
            filtered = [m for m in filtered if m["effective_status"] == sf]
        if health_filter:
            hf = health_filter.strip().lower()
            filtered = [m for m in filtered if m["health_status"] == hf]
        if query:
            q = query.strip().lower()
            if q:
                filtered = [
                    m
                    for m in filtered
                    if q in m["machine_name"].lower()
                    or q in (m["ipv4"] or "").lower()
                    or q in (m["ipv6"] or "").lower()
                    or q in (m["public_ip"] or "").lower()
                    or q in (m["os_info"] or "").lower()
                    or q in (m["status_detail"] or "").lower()
                    or q in m["effective_status"].lower()
                ]

        return {
            "server_time": int(now),
            "server_time_iso": _epoch_to_iso_utc(now),
            "summary": summary,
            "machines": filtered,
        }
