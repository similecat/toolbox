"""Flask Blueprint for the Encrypted Active-Recall EPUB Reader."""
from __future__ import annotations

import base64
import datetime
import os
import re
import time
import uuid
from typing import Any, Callable, Dict, Tuple
from urllib.parse import quote, unquote

from flask import Blueprint, Response, current_app, g, jsonify, render_template, request

from .book_cache import DecryptedBookCache
from .crypto_primitives import CryptoAuthError, zero_bytearray
from .markdown_export import build_study_markdown
from .parser import EpubParseError, ParsedEpubArchive
from .quiz_service import CopilotQuizService
from .vault_manager import KeyNotFoundBookError, VaultLockedError, VaultManager

MAX_UPLOAD_BYTES = 150 * 1024 * 1024  # 150 MiB
VALID_HIGHLIGHT_COLORS = {"amber", "emerald", "sky", "rose", "violet"}


class _HttpApiError(Exception):
    def __init__(self, status_code: int, message: str, code: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.code = code


def _utc_iso_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def _encode_content_disposition(filename: str) -> str:
    ascii_fallback = re.sub(r'[^\x20-\x7E]|["\\]', "_", filename)
    encoded = quote(filename, safe="")
    return f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{encoded}"


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


def create_epub_blueprint(
    *,
    project_root: str,
    get_csrf_token: Callable[[], str],
    validate_csrf: Callable[[], bool],
) -> Blueprint:
    bp = Blueprint("epub_reader", __name__)

    default_vault_dir = os.path.join(project_root, "data", "epub_vault")
    quiz_service = CopilotQuizService()

    runtimes: Dict[str, Tuple[VaultManager, DecryptedBookCache]] = {}

    def get_runtime() -> Tuple[VaultManager, DecryptedBookCache]:
        configured_dir = (
            current_app.config.get("EPUB_VAULT_DIR")
            or os.environ.get("EPUB_VAULT_DIR")
            or default_vault_dir
        )
        abs_dir = os.path.abspath(configured_dir)
        if abs_dir not in runtimes:
            scrypt_override = current_app.config.get("EPUB_SCRYPT_PARAMS")
            vm = VaultManager(
                vault_dir=abs_dir,
                scrypt_params=scrypt_override,
            )
            runtimes[abs_dir] = (vm, DecryptedBookCache())
        return runtimes[abs_dir]

    def build_vault_status(vault: VaultManager) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "initialized": vault.is_initialized(),
            "unlocked": vault.is_unlocked(),
            "dpapiSupported": vault.is_dpapi_supported(),
            "dpapiRememberEnabled": vault.has_dpapi_remember_file(),
        }
        if getattr(g, "current_user", None) is not None:
            payload["csrfToken"] = get_csrf_token()
        return payload

    def require_csrf_if_cookie_session() -> None:
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return
        if getattr(g, "is_api_authenticated", False):
            return
        if not validate_csrf():
            raise _HttpApiError(403, "Invalid or missing CSRF token")

    def require_unlocked_vault() -> Tuple[VaultManager, DecryptedBookCache]:
        require_csrf_if_cookie_session()
        vault, cache = get_runtime()
        if not vault.is_unlocked():
            raise VaultLockedError()
        return vault, cache

    def read_json() -> Dict[str, Any]:
        data = request.get_json(silent=True)
        if data is None:
            if not request.data:
                return {}
            raise _HttpApiError(400, "Invalid JSON request body")
        if not isinstance(data, dict):
            raise _HttpApiError(400, "JSON body must be an object")
        return data

    def asset_version() -> str:
        js_path = os.path.join(project_root, "static", "epub_reader", "assets", "epub-reader.js")
        try:
            return str(int(os.path.getmtime(js_path)))
        except OSError:
            return "1"

    @bp.errorhandler(_HttpApiError)
    def _handle_http_err(err: _HttpApiError):
        body: Dict[str, Any] = {"error": err.message}
        if err.code:
            body["code"] = err.code
        return _json_no_store(body, err.status_code)

    @bp.errorhandler(VaultLockedError)
    def _handle_locked(err: VaultLockedError):
        return _json_no_store({"error": str(err), "code": "VAULT_LOCKED"}, 401)

    @bp.errorhandler(CryptoAuthError)
    def _handle_crypto(err: CryptoAuthError):
        return _json_no_store({"error": str(err), "code": "CRYPTO_AUTH_FAILED"}, 401)

    @bp.errorhandler(EpubParseError)
    def _handle_epub_parse(err: EpubParseError):
        return _json_no_store({"error": str(err), "code": "EPUB_PARSE_FAILED"}, 422)

    @bp.errorhandler(KeyNotFoundBookError)
    def _handle_missing_book(err: KeyNotFoundBookError):
        return _json_no_store({"error": str(err)}, 404)

    # ============================================================
    # Webpage Entry Point
    # ============================================================

    @bp.route("/epub-reader")
    def epub_reader_page():
        resp = Response(
            render_template("epub_reader.html", asset_version=asset_version()),
            mimetype="text/html",
        )
        return _set_no_store(resp)

    # ============================================================
    # Vault Status & Lifecycle APIs
    # ============================================================

    @bp.route("/api/epub/vault/status", methods=["GET"])
    def vault_status():
        vault, _ = get_runtime()
        return _json_no_store(build_vault_status(vault))

    @bp.route("/api/epub/vault/init", methods=["POST"])
    def vault_init():
        require_csrf_if_cookie_session()
        body = read_json()
        passphrase = str(body.get("passphrase") or "")
        remember = bool(body.get("rememberOnThisPc", False))
        if len(passphrase) < 8 or len(passphrase) > 256:
            raise _HttpApiError(400, "passphrase: Passphrase must be between 8 and 256 characters")
        vault, _ = get_runtime()
        try:
            vault.initialize(passphrase, remember)
        except ValueError as exc:
            raise _HttpApiError(400, str(exc)) from exc
        return _json_no_store(build_vault_status(vault), 201)

    @bp.route("/api/epub/vault/unlock", methods=["POST"])
    def vault_unlock():
        require_csrf_if_cookie_session()
        body = read_json()
        passphrase = str(body.get("passphrase") or "")
        remember = bool(body.get("rememberOnThisPc", False))
        if len(passphrase) < 8 or len(passphrase) > 256:
            raise _HttpApiError(400, "passphrase: Passphrase must be between 8 and 256 characters")
        vault, _ = get_runtime()
        delay_sec = float(current_app.config.get("EPUB_UNLOCK_FAILURE_DELAY_SEC", 0.25))
        try:
            vault.unlock_with_passphrase(passphrase, remember)
        except CryptoAuthError:
            if delay_sec > 0:
                time.sleep(delay_sec)
            raise
        except ValueError as exc:
            raise _HttpApiError(400, str(exc)) from exc
        return _json_no_store(build_vault_status(vault), 200)

    @bp.route("/api/epub/vault/unlock-dpapi", methods=["POST"])
    def vault_unlock_dpapi():
        require_csrf_if_cookie_session()
        vault, _ = get_runtime()
        try:
            vault.unlock_with_dpapi()
        except ValueError as exc:
            raise _HttpApiError(400, str(exc)) from exc
        return _json_no_store(build_vault_status(vault), 200)

    @bp.route("/api/epub/vault/lock", methods=["POST"])
    def vault_lock():
        vault, cache = require_unlocked_vault()
        cache.clear_all()
        vault.lock()
        return _json_no_store(build_vault_status(vault), 200)

    @bp.route("/api/epub/vault/dpapi", methods=["POST"])
    def vault_dpapi_toggle():
        vault, _ = require_unlocked_vault()
        body = read_json()
        if "enabled" not in body or not isinstance(body["enabled"], bool):
            raise _HttpApiError(400, "enabled: boolean required")
        try:
            vault.set_dpapi_remember(body["enabled"])
        except ValueError as exc:
            raise _HttpApiError(400, str(exc)) from exc
        return _json_no_store(build_vault_status(vault), 200)

    @bp.route("/api/epub/copilot/status", methods=["GET"])
    def copilot_status():
        require_unlocked_vault()
        return _json_no_store(quiz_service.get_status(), 200)

    # ============================================================
    # Books Collection & Import
    # ============================================================

    @bp.route("/api/epub/books", methods=["GET"])
    def books_list():
        vault, _ = require_unlocked_vault()
        return _json_no_store({"books": vault.list_books()}, 200)

    @bp.route("/api/epub/books/import", methods=["POST"])
    def books_import():
        vault, cache = require_unlocked_vault()
        raw_name = request.headers.get("X-Original-Filename", "imported-book.epub")
        original_filename = unquote(raw_name).strip() or "imported-book.epub"
        raw_bytes = bytearray(request.get_data(cache=False))
        if len(raw_bytes) > MAX_UPLOAD_BYTES:
            zero_bytearray(raw_bytes)
            raise _HttpApiError(413, "Payload exceeds maximum size (150 MiB)")
        try:
            parsed = ParsedEpubArchive.from_bytes(bytes(raw_bytes))
            summary = vault.import_book(
                {
                    "title": parsed.title,
                    "author": parsed.author,
                    "language": parsed.language,
                    "description": parsed.description,
                    "originalFilename": original_filename,
                    "epubBytes": bytes(raw_bytes),
                    "chapterCount": len(parsed.spine),
                    "coverMimeType": parsed.cover_mime_type,
                    "coverBase64": parsed.cover_base64,
                }
            )
            cache.put(summary["bookId"], parsed, len(raw_bytes))
            return _json_no_store({"book": summary}, 201)
        finally:
            zero_bytearray(raw_bytes)

    @bp.route("/api/epub/books/import-vault", methods=["POST"])
    def books_import_vault():
        vault, _ = require_unlocked_vault()
        encoded_pass = request.headers.get("X-Vault-Passphrase", "")
        try:
            export_passphrase = (
                base64.b64decode(encoded_pass).decode("utf-8") if encoded_pass else ""
            )
        except Exception as exc:
            raise _HttpApiError(400, "Invalid X-Vault-Passphrase encoding") from exc
        if len(export_passphrase) < 8:
            raise _HttpApiError(400, "Portable vault passphrase must be at least 8 characters")

        archive_bytes = bytearray(request.get_data(cache=False))
        if len(archive_bytes) > MAX_UPLOAD_BYTES + 8 * 1024 * 1024:
            zero_bytearray(archive_bytes)
            raise _HttpApiError(413, "Payload exceeds maximum size")
        try:
            summary = vault.import_portable_vault(bytes(archive_bytes), export_passphrase)
            return _json_no_store({"book": summary}, 201)
        finally:
            zero_bytearray(archive_bytes)

    # ============================================================
    # Single Book Scoped Routes
    # ============================================================

    @bp.route("/api/epub/books/<book_id>", methods=["GET", "DELETE"])
    def book_detail_or_delete(book_id: str):
        vault, cache = require_unlocked_vault()
        if request.method == "DELETE":
            cache.evict(book_id)
            vault.delete_book(book_id)
            return _json_no_store({"deleted": True}, 200)

        book = vault.get_book_summary(book_id)
        archive = cache.get_or_load(book_id, vault)
        state = vault.read_book_state(book_id)
        spine_summary = [
            {
                "spineIndex": s["spineIndex"],
                "id": s["id"],
                "href": s["href"],
                "title": s["title"],
                "charCount": s["charCount"],
            }
            for s in archive.spine
        ]
        return _json_no_store(
            {
                "book": book,
                "toc": archive.toc,
                "spine": spine_summary,
                "state": state,
            },
            200,
        )

    @bp.route("/api/epub/books/<book_id>/assets", methods=["GET"])
    def book_asset(book_id: str):
        vault, cache = require_unlocked_vault()
        asset_path = (request.args.get("path") or "").strip()
        if not asset_path:
            raise _HttpApiError(400, "Missing asset path query parameter")
        archive = cache.get_or_load(book_id, vault)
        asset = archive.get_asset(asset_path)
        if not asset:
            raise _HttpApiError(404, "Book asset not found")
        asset_bytes, mime_type = asset
        resp = Response(asset_bytes, status=200, mimetype=mime_type)
        resp.headers["Content-Type"] = mime_type
        resp.headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'"
        return _set_no_store(resp)

    @bp.route("/api/epub/books/<book_id>/chapters/<int:spine_index>", methods=["GET"])
    def book_chapter(book_id: str, spine_index: int):
        vault, cache = require_unlocked_vault()
        archive = cache.get_or_load(book_id, vault)
        spine_item, html_out, styles = archive.render_chapter(book_id, spine_index)
        return _json_no_store(
            {
                "bookId": book_id,
                "spineIndex": spine_index,
                "title": spine_item["title"],
                "href": spine_item["href"],
                "html": html_out,
                "styles": styles,
                "charCount": spine_item["charCount"],
            },
            200,
        )

    @bp.route("/api/epub/books/<book_id>/progress", methods=["PUT"])
    def book_progress(book_id: str):
        vault, _ = require_unlocked_vault()
        body = read_json()
        try:
            spine_index = int(body["spineIndex"])
            scroll_ratio = float(body["scrollRatio"])
        except (KeyError, TypeError, ValueError) as exc:
            raise _HttpApiError(400, "Invalid progress payload") from exc
        if spine_index < 0 or not (0.0 <= scroll_ratio <= 1.0):
            raise _HttpApiError(400, "Out-of-range spineIndex or scrollRatio")

        completed_ch = body.get("completedChapterIndex")
        if completed_ch is not None:
            completed_ch = int(completed_ch)

        def _mutate(state: Dict[str, Any]) -> Dict[str, Any]:
            completed_set = set(state.get("progress", {}).get("completedChapters", []))
            if completed_ch is not None and completed_ch >= 0:
                completed_set.add(completed_ch)
            next_state = dict(state)
            next_state["progress"] = {
                "spineIndex": spine_index,
                "scrollRatio": scroll_ratio,
                "completedChapters": sorted(completed_set),
                "updatedAt": _utc_iso_now(),
            }
            return next_state

        updated = vault.update_book_state(book_id, _mutate)
        return _json_no_store({"progress": updated["progress"]}, 200)

    # ============================================================
    # Bookmarks
    # ============================================================

    @bp.route("/api/epub/books/<book_id>/bookmarks", methods=["POST"])
    def create_bookmark(book_id: str):
        vault, _ = require_unlocked_vault()
        body = read_json()
        try:
            spine_index = int(body["spineIndex"])
            chapter_title = str(body["chapterTitle"]).strip()
            scroll_ratio = float(body["scrollRatio"])
            snippet = str(body.get("snippet") or "")[:500]
            label = str(body["label"]).strip()
        except (KeyError, TypeError, ValueError) as exc:
            raise _HttpApiError(400, "Invalid bookmark payload") from exc

        if spine_index < 0 or not chapter_title or not label or not (0.0 <= scroll_ratio <= 1.0):
            raise _HttpApiError(400, "Bookmark fields out of bounds")

        created = {
            "id": str(uuid.uuid4()),
            "spineIndex": spine_index,
            "chapterTitle": chapter_title[:300],
            "scrollRatio": scroll_ratio,
            "snippet": snippet,
            "label": label[:200],
            "createdAt": _utc_iso_now(),
        }

        def _mutate(state: Dict[str, Any]) -> Dict[str, Any]:
            next_state = dict(state)
            next_state["bookmarks"] = [created] + list(state.get("bookmarks", []))
            return next_state

        vault.update_book_state(book_id, _mutate)
        return _json_no_store({"bookmark": created}, 201)

    @bp.route("/api/epub/books/<book_id>/bookmarks/<bookmark_id>", methods=["PATCH", "DELETE"])
    def modify_bookmark(book_id: str, bookmark_id: str):
        vault, _ = require_unlocked_vault()
        if request.method == "DELETE":
            def _delete(state: Dict[str, Any]) -> Dict[str, Any]:
                next_state = dict(state)
                next_state["bookmarks"] = [
                    bm for bm in state.get("bookmarks", []) if bm.get("id") != bookmark_id
                ]
                return next_state

            vault.update_book_state(book_id, _delete)
            return _json_no_store({"deleted": True}, 200)

        body = read_json()
        label = str(body.get("label") or "").strip()
        if not label or len(label) > 200:
            raise _HttpApiError(400, "label: must be 1..200 characters")

        found_holder: Dict[str, Any] = {}

        def _patch(state: Dict[str, Any]) -> Dict[str, Any]:
            next_bms = []
            for bm in state.get("bookmarks", []):
                if bm.get("id") == bookmark_id:
                    updated_bm = dict(bm)
                    updated_bm["label"] = label
                    found_holder["bm"] = updated_bm
                    next_bms.append(updated_bm)
                else:
                    next_bms.append(bm)
            next_state = dict(state)
            next_state["bookmarks"] = next_bms
            return next_state

        vault.update_book_state(book_id, _patch)
        if "bm" not in found_holder:
            raise _HttpApiError(404, "Bookmark not found")
        return _json_no_store({"bookmark": found_holder["bm"]}, 200)

    # ============================================================
    # Highlights & Markdown Study Notes (Annotations)
    # ============================================================

    @bp.route("/api/epub/books/<book_id>/annotations", methods=["POST"])
    def create_annotation(book_id: str):
        vault, _ = require_unlocked_vault()
        body = read_json()
        try:
            spine_index = int(body["spineIndex"])
            chapter_title = str(body["chapterTitle"]).strip()
            start_offset = int(body["startOffset"])
            end_offset = int(body["endOffset"])
            selected_text = str(body["selectedText"])
            prefix = str(body.get("prefix") or "")[:200]
            suffix = str(body.get("suffix") or "")[:200]
            color = str(body.get("color") or "amber")
            note_md = str(body.get("noteMarkdown") or "")[:20000]
        except (KeyError, TypeError, ValueError) as exc:
            raise _HttpApiError(400, "Invalid annotation payload") from exc

        if (
            spine_index < 0
            or not chapter_title
            or start_offset < 0
            or end_offset <= start_offset
            or not selected_text
            or len(selected_text) > 10000
            or color not in VALID_HIGHLIGHT_COLORS
        ):
            raise _HttpApiError(400, "Annotation fields out of bounds")

        now = _utc_iso_now()
        created = {
            "id": str(uuid.uuid4()),
            "spineIndex": spine_index,
            "chapterTitle": chapter_title[:300],
            "startOffset": start_offset,
            "endOffset": end_offset,
            "selectedText": selected_text,
            "prefix": prefix,
            "suffix": suffix,
            "color": color,
            "noteMarkdown": note_md,
            "createdAt": now,
            "updatedAt": now,
        }

        def _mutate(state: Dict[str, Any]) -> Dict[str, Any]:
            next_state = dict(state)
            next_state["annotations"] = list(state.get("annotations", [])) + [created]
            return next_state

        vault.update_book_state(book_id, _mutate)
        return _json_no_store({"annotation": created}, 201)

    @bp.route("/api/epub/books/<book_id>/annotations/<annotation_id>", methods=["PATCH", "DELETE"])
    def modify_annotation(book_id: str, annotation_id: str):
        vault, _ = require_unlocked_vault()
        if request.method == "DELETE":
            def _delete(state: Dict[str, Any]) -> Dict[str, Any]:
                next_state = dict(state)
                next_state["annotations"] = [
                    ann for ann in state.get("annotations", []) if ann.get("id") != annotation_id
                ]
                return next_state

            vault.update_book_state(book_id, _delete)
            return _json_no_store({"deleted": True}, 200)

        body = read_json()
        color = body.get("color")
        note_md = body.get("noteMarkdown")
        if color is not None and color not in VALID_HIGHLIGHT_COLORS:
            raise _HttpApiError(400, "Invalid highlight color")
        if note_md is not None and len(str(note_md)) > 20000:
            raise _HttpApiError(400, "noteMarkdown exceeds maximum length")

        found_holder: Dict[str, Any] = {}

        def _patch(state: Dict[str, Any]) -> Dict[str, Any]:
            next_anns = []
            for ann in state.get("annotations", []):
                if ann.get("id") == annotation_id:
                    updated_ann = dict(ann)
                    if color is not None:
                        updated_ann["color"] = color
                    if note_md is not None:
                        updated_ann["noteMarkdown"] = str(note_md)
                    updated_ann["updatedAt"] = _utc_iso_now()
                    found_holder["ann"] = updated_ann
                    next_anns.append(updated_ann)
                else:
                    next_anns.append(ann)
            next_state = dict(state)
            next_state["annotations"] = next_anns
            return next_state

        vault.update_book_state(book_id, _patch)
        if "ann" not in found_holder:
            raise _HttpApiError(404, "Annotation not found")
        return _json_no_store({"annotation": found_holder["ann"]}, 200)

    # ============================================================
    # Post-Chapter Quiz Generation & Submission
    # ============================================================

    @bp.route("/api/epub/books/<book_id>/chapters/<int:spine_index>/quiz/generate", methods=["POST"])
    def generate_quiz(book_id: str, spine_index: int):
        vault, cache = require_unlocked_vault()
        body = read_json()
        mcq_count = int(body.get("mcqCount", 4))
        short_count = int(body.get("shortAnswerCount", 2))
        if not (1 <= mcq_count <= 8) or not (0 <= short_count <= 4):
            raise _HttpApiError(400, "Invalid quiz question counts")

        book_summary = vault.get_book_summary(book_id)
        archive = cache.get_or_load(book_id, vault)
        ch_title, plain_text = archive.get_chapter_plain_text(spine_index)
        current_state = vault.read_book_state(book_id)
        chapter_anns = [
            a for a in current_state.get("annotations", []) if a.get("spineIndex") == spine_index
        ]

        try:
            quiz = quiz_service.generate_chapter_quiz(
                spine_index=spine_index,
                chapter_title=ch_title,
                chapter_plain_text=plain_text,
                language=book_summary["language"],
                chapter_annotations=chapter_anns,
                mcq_count=mcq_count,
                short_answer_count=short_count,
            )
        except ValueError as exc:
            raise _HttpApiError(400, str(exc)) from exc
        except RuntimeError as exc:
            raise _HttpApiError(503, str(exc)) from exc

        def _save_quiz(state: Dict[str, Any]) -> Dict[str, Any]:
            next_state = dict(state)
            next_state["quizzes"] = [quiz] + list(state.get("quizzes", []))
            return next_state

        vault.update_book_state(book_id, _save_quiz)
        return _json_no_store({"quiz": quiz}, 201)

    @bp.route("/api/epub/books/<book_id>/quizzes/<quiz_id>/submit", methods=["POST"])
    def submit_quiz(book_id: str, quiz_id: str):
        vault, _ = require_unlocked_vault()
        body = read_json()
        answers = body.get("answers")
        if not isinstance(answers, dict):
            raise _HttpApiError(400, "answers object is required")

        current_state = vault.read_book_state(book_id)
        target_quiz = next(
            (q for q in current_state.get("quizzes", []) if q.get("id") == quiz_id),
            None,
        )
        if not target_quiz:
            raise _HttpApiError(404, "Quiz record not found")

        try:
            graded = quiz_service.grade_quiz_submission(target_quiz, answers)
        except RuntimeError as exc:
            raise _HttpApiError(503, str(exc)) from exc

        def _update_graded(state: Dict[str, Any]) -> Dict[str, Any]:
            completed_set = set(state.get("progress", {}).get("completedChapters", []))
            completed_set.add(int(graded["spineIndex"]))
            next_progress = dict(state.get("progress", {}))
            next_progress["completedChapters"] = sorted(completed_set)
            next_progress["updatedAt"] = _utc_iso_now()

            next_state = dict(state)
            next_state["progress"] = next_progress
            next_state["quizzes"] = [
                graded if q.get("id") == quiz_id else q for q in state.get("quizzes", [])
            ]
            return next_state

        updated_state = vault.update_book_state(book_id, _update_graded)
        return _json_no_store({"quiz": graded, "progress": updated_state["progress"]}, 200)

    # ============================================================
    # Three-Format Exports (.epub, .md, .vault)
    # ============================================================

    @bp.route("/api/epub/books/<book_id>/export/epub", methods=["GET"])
    def export_epub(book_id: str):
        vault, _ = require_unlocked_vault()
        original_filename = vault.get_book_original_filename(book_id)
        final_name = (
            original_filename
            if original_filename.lower().endswith(".epub")
            else f"{original_filename}.epub"
        )
        epub_bytes = vault.read_decrypted_epub_bytes(book_id)
        resp = Response(epub_bytes, status=200, mimetype="application/epub+zip")
        resp.headers["Content-Disposition"] = _encode_content_disposition(final_name)
        resp.headers["Content-Length"] = str(len(epub_bytes))
        return _set_no_store(resp)

    @bp.route("/api/epub/books/<book_id>/export/markdown", methods=["GET"])
    def export_markdown(book_id: str):
        vault, cache = require_unlocked_vault()
        book = vault.get_book_summary(book_id)
        archive = cache.get_or_load(book_id, vault)
        state = vault.read_book_state(book_id)
        markdown = build_study_markdown(book, archive.spine, state)
        md_bytes = markdown.encode("utf-8")
        safe_slug = re.sub(r'[<>:"/\\|?*\x00-\x1F]', "", book["title"]).strip()[:80] or "study-notes"
        resp = Response(md_bytes, status=200, mimetype="text/markdown; charset=utf-8")
        resp.headers["Content-Type"] = "text/markdown; charset=utf-8"
        resp.headers["Content-Disposition"] = _encode_content_disposition(f"{safe_slug}-notes.md")
        resp.headers["Content-Length"] = str(len(md_bytes))
        return _set_no_store(resp)

    @bp.route("/api/epub/books/<book_id>/export/vault", methods=["POST"])
    def export_vault(book_id: str):
        vault, _ = require_unlocked_vault()
        body = read_json()
        export_passphrase = str(body.get("exportPassphrase") or "")
        if len(export_passphrase) < 8 or len(export_passphrase) > 256:
            raise _HttpApiError(400, "Export passphrase must be between 8 and 256 characters")
        portable_bytes = vault.export_portable_vault(book_id, export_passphrase)
        opaque_name = f"encrypted-book-{book_id[:8]}.vault"
        resp = Response(portable_bytes, status=200, mimetype="application/octet-stream")
        resp.headers["Content-Disposition"] = _encode_content_disposition(opaque_name)
        resp.headers["Content-Length"] = str(len(portable_bytes))
        return _set_no_store(resp)

    return bp
