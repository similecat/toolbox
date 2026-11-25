"""Flask Blueprint for Worker Service Discovery Dashboard, REST APIs, and Admission Gate."""
from __future__ import annotations

import functools
import hashlib
import os
from typing import Any, Callable, Dict, Optional

from flask import Blueprint, Response, current_app, g, jsonify, render_template, request

import db as job_db
from .store import ServiceAdmissionError, ServiceStore, ServiceValidationError


def _set_no_store(resp: Response) -> Response:
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


def _json_no_store(data: Any, status_code: int = 200, extra_headers: Optional[Dict[str, str]] = None) -> Response:
    resp = jsonify(data)
    resp.status_code = status_code
    if extra_headers:
        for k, v in extra_headers.items():
            resp.headers[k] = v
    return _set_no_store(resp)


def _read_json_object() -> Dict[str, Any]:
    data = request.get_json(silent=True)
    if data is None:
        if not request.data:
            return {}
        raise ServiceValidationError("Invalid JSON request body")
    if not isinstance(data, dict):
        raise ServiceValidationError("JSON request body must be an object")
    return data


def _extract_client_ip() -> Optional[str]:
    real_ip = request.headers.get("X-Real-IP")
    if real_ip:
        return real_ip.strip()
    return request.remote_addr


def get_service_store() -> ServiceStore:
    store = current_app.extensions.get("service_store")
    if store is None:
        store = ServiceStore()
        current_app.extensions["service_store"] = store
    return store


def verify_request_worker(expected_service_type: Optional[str] = None) -> Dict[str, Any]:
    """Validate X-Worker-Id and X-Worker-Lease headers against the live ServiceStore."""
    worker_id = request.headers.get("X-Worker-Id")
    lease_token = request.headers.get("X-Worker-Lease")
    store = get_service_store()
    worker_view = store.verify_worker_admission(
        worker_id,
        lease_token,
        expected_service_type=expected_service_type,
    )
    g.current_worker = worker_view
    return worker_view


def require_registered_worker(expected_service_type: Optional[str] = None) -> Callable[..., Any]:
    """Decorator making Service Discovery registration mandatory on worker endpoints."""

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                verify_request_worker(expected_service_type=expected_service_type)
            except ServiceAdmissionError as err:
                return _json_no_store(
                    {
                        "error": err.message,
                        "code": err.code,
                        "register_endpoint": "/api/services/register",
                    },
                    err.status_code,
                )
            return fn(*args, **kwargs)

        return wrapper

    return decorator


def create_service_discovery_blueprint(
    *,
    project_root: str,
    validate_csrf: Callable[[], bool],
    store: Optional[ServiceStore] = None,
) -> Blueprint:
    bp = Blueprint("service_discovery", __name__)
    service_store = store or ServiceStore()
    sdk_script_path = os.path.join(project_root, "clients", "worker_sdk.py")

    @bp.record_once
    def _attach_store(state) -> None:  # type: ignore[misc]
        state.app.extensions["service_store"] = service_store

    def _require_csrf_for_browser_mutation() -> Optional[Response]:
        if getattr(g, "is_api_authenticated", False):
            return None
        if not validate_csrf():
            return _json_no_store({"error": "Invalid or missing CSRF token"}, 403)
        return None

    def _require_worker_or_admin_mutation() -> Optional[Response]:
        if getattr(g, "is_api_authenticated", False):
            return None
        user = getattr(g, "current_user", None)
        if not user or not user.get("is_admin"):
            return _json_no_store(
                {"error": "Forbidden: Worker API key or admin privileges required"},
                403,
            )
        if not validate_csrf():
            return _json_no_store({"error": "Invalid or missing CSRF token"}, 403)
        return None

    @bp.errorhandler(ServiceValidationError)
    def _handle_validation_error(err: ServiceValidationError) -> Response:
        return _json_no_store({"error": str(err)}, 400)

    @bp.errorhandler(ServiceAdmissionError)
    def _handle_admission_error(err: ServiceAdmissionError) -> Response:
        return _json_no_store(
            {
                "error": err.message,
                "code": err.code,
                "register_endpoint": "/api/services/register",
            },
            err.status_code,
        )

    # ============================================================
    # Web Dashboard & Downloadable Worker SDK Routes
    # ============================================================

    @bp.route("/services", methods=["GET"])
    def services_page():
        user = getattr(g, "current_user", None)
        admin_api_key = job_db.get_api_key() if (user and user.get("is_admin")) else None
        base_url = request.url_root.rstrip("/")
        return render_template(
            "services.html",
            admin_api_key=admin_api_key,
            server_base_url=base_url,
        )

    @bp.route("/services/worker_sdk.py", methods=["GET"])
    @bp.route("/api/services/worker_sdk.py", methods=["GET"])
    def download_worker_sdk():
        if not os.path.isfile(sdk_script_path):
            return _json_no_store({"error": "Worker SDK script not found on server"}, 404)
        with open(sdk_script_path, "rb") as f:
            script_bytes = f.read()
        resp = Response(
            script_bytes,
            status=200,
            mimetype="text/x-python; charset=utf-8",
        )
        resp.headers["Content-Disposition"] = 'attachment; filename="worker_sdk.py"'
        return _set_no_store(resp)

    # ============================================================
    # Service Discovery REST APIs (/api/services/*)
    # ============================================================

    @bp.route("/api/services", methods=["GET"])
    def api_list_services():
        service_type = request.args.get("service_type")
        status_filter = request.args.get("status")
        health_filter = request.args.get("health")
        capability = request.args.get("capability")
        machine_id = request.args.get("machine_id")
        query = request.args.get("q")
        healthy_only = request.args.get("healthy_only", "").strip().lower() in ("1", "true", "yes")

        result = service_store.list_services(
            service_type=service_type,
            status_filter=status_filter,
            health_filter=health_filter,
            capability=capability,
            machine_id=machine_id,
            query=query,
            healthy_only=healthy_only,
        )

        q_hash = hashlib.sha1(request.query_string).hexdigest()[:8]
        scoped_etag = f"{result['etag'][:-1]}-{q_hash}\""
        result["etag"] = scoped_etag

        inm = (request.headers.get("If-None-Match") or "").strip()
        headers = {
            "ETag": scoped_etag,
            "X-Registry-Revision": str(result["registry_revision"]),
        }
        if inm and inm == scoped_etag:
            resp = Response(status=304)
            for k, v in headers.items():
                resp.headers[k] = v
            return _set_no_store(resp)

        return _json_no_store(result, 200, extra_headers=headers)

    @bp.route("/api/services/discover/<service_type>", methods=["GET"])
    def api_discover_peers(service_type: str):
        capability = request.args.get("capability")
        exclude_worker_id = request.args.get("exclude_worker_id") or request.headers.get("X-Worker-Id")

        result = service_store.discover_service_peers(
            service_type,
            capability=capability,
            exclude_worker_id=exclude_worker_id,
        )
        q_sig = f"{service_type}|{capability or ''}|{exclude_worker_id or ''}".encode("utf-8")
        q_hash = hashlib.sha1(q_sig).hexdigest()[:8]
        scoped_etag = f"{result['etag'][:-1]}-{q_hash}\""
        result["etag"] = scoped_etag

        inm = (request.headers.get("If-None-Match") or "").strip()
        headers = {
            "ETag": scoped_etag,
            "X-Registry-Revision": str(result["registry_revision"]),
        }
        if inm and inm == scoped_etag:
            resp = Response(status=304)
            for k, v in headers.items():
                resp.headers[k] = v
            return _set_no_store(resp)

        return _json_no_store(result, 200, extra_headers=headers)

    @bp.route("/api/services/<worker_id>", methods=["GET"])
    def api_get_worker(worker_id: str):
        raw_hours = request.args.get("hours", "24")
        try:
            hours = int(raw_hours)
        except ValueError:
            hours = 24
        detail = service_store.get_worker_detail(worker_id, hours=hours)
        if detail is None:
            return _json_no_store({"error": "Worker service not found"}, 404)
        return _json_no_store(detail, 200)

    @bp.route("/api/services/register", methods=["POST"])
    def api_register_service():
        auth_err = _require_worker_or_admin_mutation()
        if auth_err is not None:
            return auth_err

        payload = _read_json_object()
        result = service_store.register(payload, observed_ip=_extract_client_ip())
        headers = {
            "X-Registry-Revision": str(result["registry_revision"]),
            "X-Peer-Token": result["peer_auth_token"],
        }
        return _json_no_store(result, 200, extra_headers=headers)

    @bp.route("/api/services/heartbeat", methods=["POST"])
    def api_heartbeat_service():
        auth_err = _require_worker_or_admin_mutation()
        if auth_err is not None:
            return auth_err

        payload = _read_json_object()
        hb_res = service_store.heartbeat(payload, observed_ip=_extract_client_ip())
        if hb_res is None:
            # 205 Reset Content instructs the worker runtime to re-run full /register immediately
            return _set_no_store(Response(status=205))

        resp = Response(status=204)
        resp.headers["X-Registry-Revision"] = str(hb_res["registry_revision"])
        resp.headers["X-Peer-Token"] = hb_res["peer_auth_token"]
        resp.headers["X-Peer-Token-Expires"] = str(hb_res["peer_token_expires_at"])
        return _set_no_store(resp)

    @bp.route("/api/services/deregister", methods=["POST"])
    def api_deregister_service():
        auth_err = _require_worker_or_admin_mutation()
        if auth_err is not None:
            return auth_err

        payload = _read_json_object()
        found = service_store.deregister(payload)
        return _json_no_store({"ok": True, "found": found}, 200)

    @bp.route("/api/services/<worker_id>", methods=["DELETE"])
    def api_delete_service(worker_id: str):
        csrf_err = _require_csrf_for_browser_mutation()
        if csrf_err is not None:
            return csrf_err

        deleted = service_store.delete(worker_id)
        if not deleted:
            return _json_no_store({"error": "Worker service not found"}, 404)
        return _json_no_store({"ok": True, "deleted": worker_id}, 200)

    return bp
