"""Flask Blueprint for the Machine Discovery Web UI and REST API."""
from __future__ import annotations

import os
from typing import Any, Callable, Dict, Optional

from flask import Blueprint, Response, g, jsonify, render_template, request

import db as job_db
from .store import MachineStore, MachineValidationError


def _set_no_store(resp: Response) -> Response:
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


def _json_no_store(data: Any, status_code: int = 200) -> Response:
    resp = jsonify(data)
    resp.status_code = status_code
    return _set_no_store(resp)


def _read_json_object() -> Dict[str, Any]:
    data = request.get_json(silent=True)
    if data is None:
        if not request.data:
            return {}
        raise MachineValidationError("Invalid JSON request body")
    if not isinstance(data, dict):
        raise MachineValidationError("JSON request body must be an object")
    return data


def _extract_client_ip() -> Optional[str]:
    real_ip = request.headers.get("X-Real-IP")
    if real_ip:
        return real_ip.strip()
    return request.remote_addr


def create_machine_discovery_blueprint(
    *,
    project_root: str,
    validate_csrf: Callable[[], bool],
    store: Optional[MachineStore] = None,
) -> Blueprint:
    bp = Blueprint("machine_discovery", __name__)
    machine_store = store or MachineStore()
    agent_script_path = os.path.join(project_root, "clients", "machine_agent.py")

    @bp.record_once
    def _attach_store(state) -> None:  # type: ignore[misc]
        state.app.extensions["machine_store"] = machine_store

    def _require_csrf_for_browser_mutation() -> Optional[Response]:
        if getattr(g, "is_api_authenticated", False):
            return None
        if not validate_csrf():
            return _json_no_store({"error": "Invalid or missing CSRF token"}, 403)
        return None

    @bp.errorhandler(MachineValidationError)
    def _handle_validation_error(err: MachineValidationError) -> Response:
        return _json_no_store({"error": str(err)}, 400)

    # ============================================================
    # Web UI & Client Script Download Routes
    # ============================================================

    @bp.route("/machines", methods=["GET"])
    def machines_page():
        user = getattr(g, "current_user", None)
        admin_api_key = job_db.get_api_key() if (user and user.get("is_admin")) else None
        base_url = request.url_root.rstrip("/")
        return render_template(
            "machines.html",
            admin_api_key=admin_api_key,
            server_base_url=base_url,
        )

    @bp.route("/machines/agent.py", methods=["GET"])
    @bp.route("/api/machines/agent.py", methods=["GET"])
    def download_machine_agent():
        if not os.path.isfile(agent_script_path):
            return _json_no_store({"error": "Agent script not found on server"}, 404)
        with open(agent_script_path, "rb") as f:
            script_bytes = f.read()
        resp = Response(
            script_bytes,
            status=200,
            mimetype="text/x-python; charset=utf-8",
        )
        resp.headers["Content-Disposition"] = 'attachment; filename="machine_agent.py"'
        return _set_no_store(resp)

    # ============================================================
    # Machine Discovery REST APIs (/api/machines/*)
    # ============================================================

    @bp.route("/api/machines", methods=["GET"])
    def api_list_machines():
        status_filter = request.args.get("status")
        health_filter = request.args.get("health")
        query = request.args.get("q")
        result = machine_store.list_machines(
            status_filter=status_filter,
            health_filter=health_filter,
            query=query,
        )
        return _json_no_store(result, 200)

    @bp.route("/api/machines/<machine_id>", methods=["GET"])
    def api_get_machine(machine_id: str):
        view = machine_store.get(machine_id)
        if view is None:
            return _json_no_store({"error": "Machine not found"}, 404)
        return _json_no_store({"machine": view}, 200)

    @bp.route("/api/machines/register", methods=["POST"])
    def api_register_machine():
        csrf_err = _require_csrf_for_browser_mutation()
        if csrf_err is not None:
            return csrf_err

        payload = _read_json_object()
        view = machine_store.register(payload, observed_ip=_extract_client_ip())
        return _json_no_store(
            {
                "ok": True,
                "machine": view,
                "heartbeat_interval": view["heartbeat_interval"],
            },
            200,
        )

    @bp.route("/api/machines/heartbeat", methods=["POST"])
    def api_heartbeat_machine():
        csrf_err = _require_csrf_for_browser_mutation()
        if csrf_err is not None:
            return csrf_err

        payload = _read_json_object()
        known = machine_store.heartbeat(payload, observed_ip=_extract_client_ip())
        if not known:
            # 205 Reset Content instructs the client agent to immediately send full /register
            return _set_no_store(Response(status=205))
        return _set_no_store(Response(status=204))

    @bp.route("/api/machines/deregister", methods=["POST"])
    def api_deregister_machine():
        csrf_err = _require_csrf_for_browser_mutation()
        if csrf_err is not None:
            return csrf_err

        payload = _read_json_object()
        found = machine_store.deregister(payload)
        return _json_no_store({"ok": True, "found": found}, 200)

    @bp.route("/api/machines/<machine_id>", methods=["DELETE"])
    def api_delete_machine(machine_id: str):
        csrf_err = _require_csrf_for_browser_mutation()
        if csrf_err is not None:
            return csrf_err

        deleted = machine_store.delete(machine_id)
        if not deleted:
            return _json_no_store({"error": "Machine not found"}, 404)
        return _json_no_store({"ok": True, "deleted": machine_id}, 200)

    return bp
