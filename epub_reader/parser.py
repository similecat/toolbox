"""In-memory EPUB 2 & EPUB 3 archive parser with zero disk temp extraction."""
from __future__ import annotations

import base64
import html
import io
import posixpath
import re
import xml.etree.ElementTree as ET
import zipfile
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import unquote

from .crypto_primitives import zero_bytearray
from .sanitizer import (
    extract_chapter_plain_text,
    resolve_relative_archive_path,
    sanitize_and_rewrite_chapter_html,
)


class EpubParseError(Exception):
    """Raised when an uploaded buffer is not a valid readable EPUB archive."""


MAX_INLINE_COVER_BYTES = 512 * 1024

MIME_BY_EXT: Dict[str, str] = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".css": "text/css; charset=utf-8",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".otf": "font/otf",
    ".xhtml": "application/xhtml+xml; charset=utf-8",
    ".html": "text/html; charset=utf-8",
}


def _local_name(tag: str) -> str:
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def _attr_local(elem: ET.Element, target_local: str) -> str:
    if target_local in elem.attrib:
        return elem.attrib[target_local]
    for k, v in elem.attrib.items():
        if _local_name(k) == target_local:
            return v
    return ""


def _normalize_archive_key(raw_path: str) -> str:
    cleaned = raw_path.replace("\\", "/").lstrip("/")
    norm = posixpath.normpath(cleaned)
    return "" if norm == "." else norm


def _parse_xml_safe(xml_text: str) -> ET.Element:
    cleaned = xml_text.lstrip("\ufeff")
    try:
        return ET.fromstring(cleaned)
    except ET.ParseError as exc:
        raise EpubParseError(f"Malformed XML inside EPUB archive: {exc}") from exc


class ParsedEpubArchive:
    """Parsed in-memory representation of a decrypted EPUB archive."""

    def __init__(
        self,
        *,
        title: str,
        author: str,
        language: str,
        description: Optional[str],
        opf_path: str,
        spine: List[Dict[str, Any]],
        toc: List[Dict[str, Any]],
        cover_mime_type: Optional[str],
        cover_base64: Optional[str],
        files: Dict[str, bytearray],
        lower_index: Dict[str, str],
        manifest_by_path: Dict[str, Dict[str, Any]],
        spine_by_path: Dict[str, int],
    ):
        self.title = title
        self.author = author
        self.language = language
        self.description = description
        self.opf_path = opf_path
        self.spine = spine
        self.toc = toc
        self.cover_mime_type = cover_mime_type
        self.cover_base64 = cover_base64
        self._files = files
        self._lower_index = lower_index
        self._manifest_by_path = manifest_by_path
        self._spine_by_path = spine_by_path

    @classmethod
    def from_bytes(cls, epub_bytes: bytes) -> "ParsedEpubArchive":
        if not epub_bytes or len(epub_bytes) < 22:
            raise EpubParseError("Invalid or empty EPUB file")

        files: Dict[str, bytearray] = {}
        lower_index: Dict[str, str] = {}
        try:
            with zipfile.ZipFile(io.BytesIO(epub_bytes), "r") as zf:
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    key = _normalize_archive_key(info.filename)
                    if not key:
                        continue
                    files[key] = bytearray(zf.read(info.filename))
                    lower_index[key.lower()] = key
        except zipfile.BadZipFile as exc:
            raise EpubParseError("File is not a valid ZIP/EPUB archive") from exc

        def read_bytes(archive_path: str) -> Optional[bytearray]:
            key = _normalize_archive_key(archive_path)
            if key in files:
                return files[key]
            mapped = lower_index.get(key.lower())
            return files.get(mapped) if mapped else None

        def read_utf8(archive_path: str) -> Optional[str]:
            raw = read_bytes(archive_path)
            if raw is None:
                return None
            return bytes(raw).decode("utf-8", errors="replace")

        # Check META-INF/encryption.xml for DRM (allow font obfuscation)
        encryption_xml = read_utf8("META-INF/encryption.xml")
        if encryption_xml:
            enc_root = _parse_xml_safe(encryption_xml)
            for elem in enc_root.iter():
                if _local_name(elem.tag) == "EncryptionMethod":
                    algo = _attr_local(elem, "Algorithm")
                    is_font_obf = (
                        "idpf.org/2008/embedding" in algo
                        or "adobe.com/pdf/enc#RC" in algo
                    )
                    if not is_font_obf:
                        raise EpubParseError("DRM-encrypted EPUB files are not supported")

        container_xml = read_utf8("META-INF/container.xml")
        if not container_xml:
            raise EpubParseError("Missing META-INF/container.xml in EPUB archive")
        container_root = _parse_xml_safe(container_xml)
        opf_raw_path = ""
        for elem in container_root.iter():
            if _local_name(elem.tag) == "rootfile":
                fp = _attr_local(elem, "full-path").strip()
                if fp:
                    opf_raw_path = fp
                    break
        if not opf_raw_path:
            raise EpubParseError("Missing OPF rootfile path in container.xml")

        opf_path = _normalize_archive_key(opf_raw_path)
        opf_xml = read_utf8(opf_path)
        if not opf_xml:
            raise EpubParseError(f"OPF package file not found at {opf_path}")

        pkg_root = _parse_xml_safe(opf_xml)
        metadata_node: Optional[ET.Element] = None
        manifest_node: Optional[ET.Element] = None
        spine_node: Optional[ET.Element] = None
        for child in pkg_root:
            lname = _local_name(child.tag)
            if lname == "metadata" and metadata_node is None:
                metadata_node = child
            elif lname == "manifest" and manifest_node is None:
                manifest_node = child
            elif lname == "spine" and spine_node is None:
                spine_node = child

        titles: List[str] = []
        creators: List[str] = []
        languages: List[str] = []
        descriptions: List[str] = []
        meta_cover_ref = ""

        if metadata_node is not None:
            for mchild in metadata_node:
                mname = _local_name(mchild.tag).lower()
                text_val = "".join(mchild.itertext()).strip()
                if mname == "title" and text_val:
                    titles.append(text_val)
                elif mname == "creator" and text_val:
                    creators.append(text_val)
                elif mname == "language" and text_val:
                    languages.append(text_val)
                elif mname == "description" and text_val:
                    descriptions.append(text_val)
                elif mname == "meta":
                    if _attr_local(mchild, "name").lower() == "cover":
                        meta_cover_ref = _attr_local(mchild, "content").strip()

        title = titles[0] if titles else "Untitled Book"
        author = ", ".join(creators) if creators else "Unknown Author"
        raw_lang = languages[0] if languages else ""
        description: Optional[str] = None
        if descriptions:
            cleaned_desc = re.sub(r"<[^>]+>", " ", descriptions[0])
            cleaned_desc = re.sub(r"\s+", " ", html.unescape(cleaned_desc)).strip()
            description = cleaned_desc or None

        manifest_by_id: Dict[str, Dict[str, Any]] = {}
        manifest_by_path: Dict[str, Dict[str, Any]] = {}
        if manifest_node is not None:
            for item_el in manifest_node:
                if _local_name(item_el.tag) != "item":
                    continue
                item_id = _attr_local(item_el, "id").strip()
                href = _attr_local(item_el, "href").strip()
                media_type = _attr_local(item_el, "media-type").strip()
                if not item_id or not href:
                    continue
                archive_path = resolve_relative_archive_path(opf_path, href)
                props = [p for p in _attr_local(item_el, "properties").split() if p]
                item_dict = {
                    "id": item_id,
                    "href": href,
                    "archivePath": archive_path,
                    "mediaType": media_type,
                    "properties": props,
                }
                manifest_by_id[item_id] = item_dict
                manifest_by_path[archive_path] = item_dict
                manifest_by_path[archive_path.lower()] = item_dict

        spine: List[Dict[str, Any]] = []
        spine_by_path: Dict[str, int] = {}
        ncx_toc_id = _attr_local(spine_node, "toc").strip() if spine_node is not None else ""
        if spine_node is not None:
            for itemref_el in spine_node:
                if _local_name(itemref_el.tag) != "itemref":
                    continue
                idref = _attr_local(itemref_el, "idref").strip()
                manifest_item = manifest_by_id.get(idref)
                if not manifest_item:
                    continue
                mt = manifest_item["mediaType"].lower()
                ap = manifest_item["archivePath"]
                is_html = (
                    "html" in mt
                    or "xml" in mt
                    or bool(re.search(r"\.(xhtml|html|htm)$", ap, re.IGNORECASE))
                )
                if not is_html:
                    continue
                chapter_xhtml = read_utf8(ap) or ""
                title_guess, plain_text = extract_chapter_plain_text(chapter_xhtml)
                spine_index = len(spine)
                spine.append(
                    {
                        "spineIndex": spine_index,
                        "id": manifest_item["id"],
                        "href": manifest_item["href"],
                        "archivePath": ap,
                        "mediaType": manifest_item["mediaType"],
                        "title": title_guess or f"Chapter {spine_index + 1}",
                        "charCount": len(plain_text),
                    }
                )
                spine_by_path[ap] = spine_index
                spine_by_path[ap.lower()] = spine_index

        if not spine:
            raise EpubParseError("EPUB contains no readable HTML/XHTML spine chapters")

        def resolve_spine(archive_path: str) -> Optional[int]:
            norm = _normalize_archive_key(archive_path)
            if norm in spine_by_path:
                return spine_by_path[norm]
            return spine_by_path.get(norm.lower())

        toc: List[Dict[str, Any]] = []
        nav_item = next(
            (m for m in manifest_by_id.values() if "nav" in m["properties"]),
            None,
        )
        if nav_item:
            nav_xhtml = read_utf8(nav_item["archivePath"])
            if nav_xhtml:
                toc = _parse_epub3_nav_toc(nav_xhtml, nav_item["archivePath"], resolve_spine)

        if not toc:
            ncx_item = manifest_by_id.get(ncx_toc_id) if ncx_toc_id else None
            if not ncx_item:
                ncx_item = next(
                    (
                        m
                        for m in manifest_by_id.values()
                        if m["mediaType"] == "application/x-dtbncx+xml"
                        or m["archivePath"].lower().endswith(".ncx")
                    ),
                    None,
                )
            if ncx_item:
                ncx_xml = read_utf8(ncx_item["archivePath"])
                if ncx_xml:
                    toc = _parse_epub2_ncx_toc(ncx_xml, ncx_item["archivePath"], resolve_spine)

        if not toc:
            toc = [
                {
                    "id": f"toc-{ch['spineIndex']}",
                    "label": ch["title"],
                    "href": ch["href"],
                    "spineIndex": ch["spineIndex"],
                    "children": [],
                }
                for ch in spine
            ]
        else:
            first_title_by_spine: Dict[int, str] = {}

            def visit_toc(items: List[Dict[str, Any]]) -> None:
                for it in items:
                    sp_idx = it["spineIndex"]
                    lbl = it["label"].strip()
                    if sp_idx not in first_title_by_spine and lbl:
                        first_title_by_spine[sp_idx] = lbl
                    visit_toc(it.get("children", []))

            visit_toc(toc)
            for ch in spine:
                if ch["spineIndex"] in first_title_by_spine:
                    ch["title"] = first_title_by_spine[ch["spineIndex"]]

        sample_text = f"{title} {spine[0]['title'] if spine else ''}"
        detected_lang = raw_lang or (
            "zh-CN" if re.search(r"[\u4e00-\u9fff]", sample_text) else "en"
        )

        cover_item = _detect_cover_item(meta_cover_ref, manifest_by_id)
        cover_mime_type: Optional[str] = None
        cover_base64: Optional[str] = None
        if cover_item:
            cover_bytes = read_bytes(cover_item["archivePath"])
            if cover_bytes and 0 < len(cover_bytes) <= MAX_INLINE_COVER_BYTES:
                ext = posixpath.splitext(cover_item["archivePath"])[1].lower()
                cover_mime_type = (
                    cover_item["mediaType"] or MIME_BY_EXT.get(ext) or "image/jpeg"
                )
                cover_base64 = base64.b64encode(bytes(cover_bytes)).decode("ascii")

        return cls(
            title=title,
            author=author,
            language=detected_lang,
            description=description,
            opf_path=opf_path,
            spine=spine,
            toc=toc,
            cover_mime_type=cover_mime_type,
            cover_base64=cover_base64,
            files=files,
            lower_index=lower_index,
            manifest_by_path=manifest_by_path,
            spine_by_path=spine_by_path,
        )

    def _read_bytes(self, archive_path: str) -> Optional[bytearray]:
        key = _normalize_archive_key(archive_path)
        if key in self._files:
            return self._files[key]
        mapped = self._lower_index.get(key.lower())
        return self._files.get(mapped) if mapped else None

    def _read_utf8(self, archive_path: str) -> Optional[str]:
        raw = self._read_bytes(archive_path)
        return bytes(raw).decode("utf-8", errors="replace") if raw is not None else None

    def get_asset(self, archive_path: str) -> Optional[Tuple[bytes, str]]:
        key = _normalize_archive_key(archive_path)
        raw = self._read_bytes(key)
        if raw is None:
            return None
        manifest_item = self._manifest_by_path.get(key) or self._manifest_by_path.get(key.lower())
        ext = posixpath.splitext(key)[1].lower()
        mime_type = (
            (manifest_item["mediaType"] if manifest_item else "")
            or MIME_BY_EXT.get(ext)
            or "application/octet-stream"
        )
        return bytes(raw), mime_type

    def render_chapter(self, book_id: str, spine_index: int) -> Tuple[Dict[str, Any], str, List[str]]:
        if spine_index < 0 or spine_index >= len(self.spine):
            raise EpubParseError(f"Invalid chapter index: {spine_index}")
        spine_item = self.spine[spine_index]
        raw_xhtml = self._read_utf8(spine_item["archivePath"]) or ""

        def resolve_spine_fn(p: str) -> Optional[int]:
            norm = _normalize_archive_key(p)
            if norm in self._spine_by_path:
                return self._spine_by_path[norm]
            return self._spine_by_path.get(norm.lower())

        sanitized_html, styles = sanitize_and_rewrite_chapter_html(
            raw_xhtml=raw_xhtml,
            book_id=book_id,
            chapter_archive_path=spine_item["archivePath"],
            resolve_spine_by_archive_path=resolve_spine_fn,
            read_archive_text=self._read_utf8,
        )
        return spine_item, sanitized_html, styles

    def get_chapter_plain_text(self, spine_index: int) -> Tuple[str, str]:
        if spine_index < 0 or spine_index >= len(self.spine):
            raise EpubParseError(f"Invalid chapter index: {spine_index}")
        spine_item = self.spine[spine_index]
        raw_xhtml = self._read_utf8(spine_item["archivePath"]) or ""
        _, plain_text = extract_chapter_plain_text(raw_xhtml)
        return spine_item["title"], plain_text

    def dispose(self) -> None:
        for buf in self._files.values():
            zero_bytearray(buf)
        self._files.clear()
        self._lower_index.clear()


def _detect_cover_item(
    meta_cover_ref: str,
    manifest_by_id: Dict[str, Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    items = list(manifest_by_id.values())
    for item in items:
        if "cover-image" in item["properties"] and item["mediaType"].startswith("image/"):
            return item
    if meta_cover_ref:
        direct = manifest_by_id.get(meta_cover_ref)
        if direct and direct["mediaType"].startswith("image/"):
            return direct
        for item in items:
            if item["href"] == meta_cover_ref and item["mediaType"].startswith("image/"):
                return item
    for item in items:
        if item["mediaType"].startswith("image/") and re.search(
            r"cover", f"{item['id']} {item['href']}", re.IGNORECASE
        ):
            return item
    return None


def _parse_epub2_ncx_toc(
    ncx_xml: str,
    ncx_archive_path: str,
    resolve_spine: Callable[[str], Optional[int]],
) -> List[Dict[str, Any]]:
    try:
        root = _parse_xml_safe(ncx_xml)
    except EpubParseError:
        return []
    nav_map = next((el for el in root.iter() if _local_name(el.tag) == "navMap"), None)
    if nav_map is None:
        return []

    counter = [0]

    def convert_points(parent_el: ET.Element) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        for pt in parent_el:
            if _local_name(pt.tag) != "navPoint":
                continue
            label = ""
            raw_src = ""
            for sub in pt:
                sname = _local_name(sub.tag)
                if sname == "navLabel":
                    label = "".join(sub.itertext()).strip()
                elif sname == "content":
                    raw_src = _attr_local(sub, "src").strip()
            children = convert_points(pt)

            spine_index: Optional[int] = None
            fragment: Optional[str] = None
            if raw_src:
                parts = raw_src.split("#", 1)
                target_archive = resolve_relative_archive_path(ncx_archive_path, parts[0])
                spine_index = resolve_spine(target_archive)
                if len(parts) > 1 and parts[1]:
                    fragment = unquote(parts[1])
            if spine_index is None and children:
                spine_index = children[0]["spineIndex"]
            if spine_index is not None and label:
                counter[0] += 1
                entry: Dict[str, Any] = {
                    "id": _attr_local(pt, "id") or f"ncx-{counter[0]}",
                    "label": label,
                    "href": raw_src,
                    "spineIndex": spine_index,
                    "children": children,
                }
                if fragment:
                    entry["fragment"] = fragment
                items.append(entry)
        return items

    return convert_points(nav_map)


def _split_top_level_li(ol_inner: str) -> List[str]:
    results: List[str] = []
    ol_depth = 0
    li_depth = 0
    current_li_start = -1
    for match in re.finditer(r"</?(ol|li)\b[^>]*>", ol_inner, flags=re.IGNORECASE):
        full_tag = match.group(0)
        tag_name = match.group(1).lower()
        is_closing = full_tag.startswith("</")
        if full_tag.endswith("/>"):
            continue
        if tag_name == "ol":
            ol_depth += -1 if is_closing else 1
        elif tag_name == "li" and ol_depth == 0:
            if not is_closing:
                if li_depth == 0:
                    current_li_start = match.end()
                li_depth += 1
            elif li_depth > 0:
                li_depth -= 1
                if li_depth == 0 and current_li_start >= 0:
                    results.append(ol_inner[current_li_start:match.start()])
                    current_li_start = -1
    return results


def _parse_epub3_nav_toc(
    nav_xhtml: str,
    nav_archive_path: str,
    resolve_spine: Callable[[str], Optional[int]],
) -> List[Dict[str, Any]]:
    toc_nav_match = re.search(
        r"<nav\b[^>]*\btype\s*=\s*(['\"])[^'\"]*\btoc\b[^'\"]*\1[^>]*>([\s\S]*?)</nav>",
        nav_xhtml,
        flags=re.IGNORECASE,
    ) or re.search(r"<nav\b[^>]*>([\s\S]*?)</nav>", nav_xhtml, flags=re.IGNORECASE)
    if not toc_nav_match:
        return []
    nav_html = toc_nav_match.group(toc_nav_match.lastindex or 1)
    if not nav_html:
        return []

    counter = [0]

    def parse_ol(html_fragment: str) -> List[Dict[str, Any]]:
        ol_match = re.search(r"<ol\b[^>]*>([\s\S]*)</ol>", html_fragment, flags=re.IGNORECASE)
        if not ol_match:
            return []
        li_chunks = _split_top_level_li(ol_match.group(1))
        items: List[Dict[str, Any]] = []
        for li_html in li_chunks:
            sub_ol = re.search(r"<ol\b", li_html, flags=re.IGNORECASE)
            head_part = li_html[:sub_ol.start()] if sub_ol else li_html
            tail_part = li_html[sub_ol.start():] if sub_ol else ""
            children = parse_ol(tail_part) if tail_part else []

            a_match = re.search(r"<a\b([^>]*)>([\s\S]*?)</a>", head_part, flags=re.IGNORECASE)
            span_match = (
                re.search(r"<span\b[^>]*>([\s\S]*?)</span>", head_part, flags=re.IGNORECASE)
                if not a_match
                else None
            )
            raw_label_html = a_match.group(2) if a_match else (span_match.group(1) if span_match else "")
            label = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", raw_label_html))).strip()
            href_match = (
                re.search(r"\bhref\s*=\s*(['\"])(.*?)\1", a_match.group(1), flags=re.IGNORECASE)
                if a_match
                else None
            )
            raw_href = href_match.group(2).strip() if href_match else ""

            spine_index: Optional[int] = None
            fragment: Optional[str] = None
            if raw_href and not re.match(r"^(https?:|mailto:)", raw_href, re.IGNORECASE):
                parts = raw_href.split("#", 1)
                target_archive = resolve_relative_archive_path(nav_archive_path, parts[0])
                spine_index = resolve_spine(target_archive)
                if len(parts) > 1 and parts[1]:
                    fragment = unquote(parts[1])
            if spine_index is None and children:
                spine_index = children[0]["spineIndex"]
            if spine_index is not None and label:
                counter[0] += 1
                entry: Dict[str, Any] = {
                    "id": f"nav-{counter[0]}",
                    "label": label,
                    "href": raw_href,
                    "spineIndex": spine_index,
                    "children": children,
                }
                if fragment:
                    entry["fragment"] = fragment
                items.append(entry)
        return items

    return parse_ol(nav_html)
