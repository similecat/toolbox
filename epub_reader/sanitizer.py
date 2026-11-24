"""EPUB XHTML/HTML/CSS sanitizer and internal asset/chapter link rewriter."""
from __future__ import annotations

import html
import posixpath
import re
from html.parser import HTMLParser
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import quote, unquote


def resolve_relative_archive_path(base_file_archive_path: str, relative_href: str) -> str:
    clean_href = unquote(relative_href.split("#")[0].split("?")[0].strip())
    if not clean_href:
        return base_file_archive_path
    if clean_href.startswith("/"):
        return posixpath.normpath(clean_href.lstrip("/"))
    base_dir = posixpath.dirname(base_file_archive_path)
    joined = clean_href if base_dir in ("", ".") else posixpath.join(base_dir, clean_href)
    normalized = posixpath.normpath(joined)
    if normalized.startswith(".."):
        normalized = re.sub(r"^(\.\./)+", "", normalized)
    return normalized


_EXTERNAL_SCHEME_RE = re.compile(r"^(https?:|mailto:|tel:|data:)", re.IGNORECASE)


def _is_external_scheme(href: str) -> bool:
    return bool(_EXTERNAL_SCHEME_RE.match(href.strip()))


def scope_and_rewrite_css(raw_css: str, css_archive_path: str, book_id: str) -> str:
    cleaned = re.sub(r"@import\s+(?:url\()?['\"]?https?:[^;]+;", "", raw_css, flags=re.IGNORECASE)
    cleaned = re.sub(r"url\(\s*['\"]?\s*javascript:[^)]*\)", "none", cleaned, flags=re.IGNORECASE)

    def _replace_url(match: re.Match[str]) -> str:
        raw_target = match.group(2).strip()
        if not raw_target or _is_external_scheme(raw_target) or raw_target.startswith("#"):
            return match.group(0)
        resolved = resolve_relative_archive_path(css_archive_path, raw_target)
        encoded_book = quote(book_id, safe="")
        encoded_path = quote(resolved, safe="")
        return f'url("/api/epub/books/{encoded_book}/assets?path={encoded_path}")'

    cleaned = re.sub(r"url\(\s*(['\"]?)([^'\")]+)\1\s*\)", _replace_url, cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(
        r"(^|\}|\{)\s*(html|body)(?=[\s,{.#\[:])",
        r"\1 .epub-chapter-scope",
        cleaned,
        flags=re.IGNORECASE,
    )
    return cleaned


_ALLOWED_TAGS = {
    "address", "article", "aside", "footer", "header", "h1", "h2", "h3", "h4", "h5", "h6",
    "hgroup", "main", "nav", "section", "blockquote", "dd", "div", "dl", "dt", "figcaption",
    "figure", "hr", "li", "ol", "p", "pre", "ul", "a", "abbr", "b", "bdi", "bdo", "br",
    "cite", "code", "data", "dfn", "em", "i", "kbd", "mark", "q", "rb", "rp", "rt", "rtc",
    "ruby", "s", "samp", "small", "span", "strong", "sub", "sup", "time", "u", "var", "wbr",
    "caption", "col", "colgroup", "table", "tbody", "td", "tfoot", "th", "thead", "tr",
    "del", "ins", "details", "summary", "img",
    "svg", "g", "path", "rect", "circle", "ellipse", "line", "polyline", "polygon",
    "text", "tspan", "defs", "clippath", "lineargradient", "radialgradient", "stop", "image",
}

_VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

_SUPPRESSED_TAGS = {"script", "style", "head", "title", "noscript", "iframe", "object", "applet"}

_SVG_TAG_CASE = {
    "clippath": "clipPath",
    "lineargradient": "linearGradient",
    "radialgradient": "radialGradient",
}

_SVG_ATTR_CASE = {
    "viewbox": "viewBox",
    "preserveaspectratio": "preserveAspectRatio",
    "textanchor": "textAnchor",
}

_GLOBAL_ATTRS = {"id", "class", "lang", "dir", "title", "epub:type", "role"}

_TAG_ATTRS: Dict[str, set[str]] = {
    "a": {"href", "name", "target", "rel", "data-epub-spine", "data-epub-fragment"},
    "img": {"src", "alt", "width", "height", "loading"},
    "td": {"colspan", "rowspan", "align", "valign"},
    "th": {"colspan", "rowspan", "align", "valign", "scope"},
    "ol": {"start", "type", "reversed"},
    "li": {"value"},
    "svg": {"viewbox", "width", "height", "xmlns", "preserveaspectratio", "fill", "stroke"},
    "g": {"transform", "fill", "stroke", "opacity"},
    "path": {"d", "fill", "stroke", "stroke-width", "transform"},
    "rect": {"x", "y", "width", "height", "rx", "ry", "fill", "stroke"},
    "circle": {"cx", "cy", "r", "fill", "stroke"},
    "ellipse": {"cx", "cy", "rx", "ry", "fill", "stroke"},
    "line": {"x1", "y1", "x2", "y2", "stroke", "stroke-width"},
    "polyline": {"points", "fill", "stroke"},
    "polygon": {"points", "fill", "stroke"},
    "text": {"x", "y", "dx", "dy", "text-anchor", "font-size", "fill"},
    "tspan": {"x", "y", "dx", "dy", "fill"},
    "stop": {"offset", "stop-color", "stop-opacity"},
    "lineargradient": {"id", "x1", "y1", "x2", "y2", "gradientunits", "gradienttransform"},
    "radialgradient": {"id", "cx", "cy", "r", "fx", "fy", "gradientunits", "gradienttransform"},
    "image": {"href", "xlink:href", "x", "y", "width", "height", "preserveaspectratio"},
}


class _ChapterSanitizerParser(HTMLParser):
    def __init__(
        self,
        book_id: str,
        chapter_archive_path: str,
        resolve_spine_by_archive_path: Callable[[str], Optional[int]],
    ):
        super().__init__(convert_charrefs=False)
        self.book_id = book_id
        self.chapter_archive_path = chapter_archive_path
        self.resolve_spine = resolve_spine_by_archive_path
        self.chunks: List[str] = []
        self._suppress_depth = 0
        self._open_stack: List[Tuple[str, Optional[str]]] = []

    def _asset_url_for(self, href: str) -> Optional[str]:
        trimmed = href.strip()
        if not trimmed:
            return None
        if trimmed.lower().startswith("data:image/"):
            return trimmed
        if _is_external_scheme(trimmed):
            return None
        resolved = resolve_relative_archive_path(self.chapter_archive_path, trimmed)
        return f"/api/epub/books/{quote(self.book_id, safe='')}/assets?path={quote(resolved, safe='')}"

    def _transform_tag(
        self, tag_lower: str, raw_attrs: List[Tuple[str, Optional[str]]]
    ) -> Tuple[Optional[str], Dict[str, str], bool]:
        """Return (emitted_tag, filtered_attrs, is_replacement_void_span)."""
        if tag_lower not in _ALLOWED_TAGS:
            return None, {}, False

        attr_map: Dict[str, str] = {}
        for k, v in raw_attrs:
            if k:
                attr_map[k.lower()] = "" if v is None else v

        if tag_lower == "img":
            mapped = self._asset_url_for(attr_map.get("src", ""))
            if not mapped:
                return "span", {}, True
            attr_map["src"] = mapped
            attr_map["loading"] = "lazy"
        elif tag_lower == "image":
            raw_ref = attr_map.get("href") or attr_map.get("xlink:href") or ""
            mapped = self._asset_url_for(raw_ref)
            if not mapped:
                return "g", {}, False
            attr_map["href"] = mapped
            attr_map.pop("xlink:href", None)
        elif tag_lower == "a":
            raw_href = attr_map.get("href", "").strip()
            if raw_href:
                if re.match(r"^(javascript:|vbscript:|data:)", raw_href, re.IGNORECASE):
                    attr_map.pop("href", None)
                elif re.match(r"^(https?:|mailto:)", raw_href, re.IGNORECASE):
                    attr_map["href"] = raw_href
                    attr_map["target"] = "_blank"
                    attr_map["rel"] = "noopener noreferrer"
                elif raw_href.startswith("#"):
                    fragment = unquote(raw_href[1:])
                    same_spine = self.resolve_spine(self.chapter_archive_path)
                    attr_map["href"] = raw_href
                    if same_spine is not None:
                        attr_map["data-epub-spine"] = str(same_spine)
                    attr_map["data-epub-fragment"] = fragment
                else:
                    parts = raw_href.split("#", 1)
                    rel_part = parts[0]
                    raw_frag = parts[1] if len(parts) > 1 else ""
                    target_archive = resolve_relative_archive_path(self.chapter_archive_path, rel_part)
                    target_spine = self.resolve_spine(target_archive)
                    if target_spine is not None:
                        fragment = unquote(raw_frag) if raw_frag else ""
                        frag_suffix = f"-{fragment}" if fragment else ""
                        attr_map["href"] = f"#epub-spine-{target_spine}{frag_suffix}"
                        attr_map["data-epub-spine"] = str(target_spine)
                        if fragment:
                            attr_map["data-epub-fragment"] = fragment
                    else:
                        attr_map.pop("href", None)

        allowed_for_tag = _GLOBAL_ATTRS | _TAG_ATTRS.get(tag_lower, set())
        filtered: Dict[str, str] = {}
        for k_low, val in attr_map.items():
            if k_low in allowed_for_tag:
                out_key = _SVG_ATTR_CASE.get(k_low, k_low)
                filtered[out_key] = val

        out_tag = _SVG_TAG_CASE.get(tag_lower, tag_lower)
        return out_tag, filtered, False

    @staticmethod
    def _format_start_tag(tag: str, attrs: Dict[str, str]) -> str:
        if not attrs:
            return f"<{tag}>"
        parts = [f'{k}="{html.escape(v, quote=True)}"' for k, v in attrs.items()]
        return f"<{tag} {' '.join(parts)}>"

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        tag_lower = tag.lower()
        if tag_lower in _SUPPRESSED_TAGS:
            if tag_lower not in _VOID_TAGS:
                self._suppress_depth += 1
            return
        if self._suppress_depth > 0:
            return

        out_tag, filtered_attrs, replaced_void = self._transform_tag(tag_lower, attrs)
        if out_tag is None:
            if tag_lower not in _VOID_TAGS:
                self._open_stack.append((tag_lower, None))
            return

        if replaced_void:
            self.chunks.append("<span></span>")
            return

        self.chunks.append(self._format_start_tag(out_tag, filtered_attrs))
        if tag_lower not in _VOID_TAGS:
            self._open_stack.append((tag_lower, out_tag))

    def handle_endtag(self, tag: str) -> None:
        tag_lower = tag.lower()
        if tag_lower in _SUPPRESSED_TAGS:
            if tag_lower not in _VOID_TAGS and self._suppress_depth > 0:
                self._suppress_depth -= 1
            return
        if self._suppress_depth > 0 or tag_lower in _VOID_TAGS:
            return

        for idx in range(len(self._open_stack) - 1, -1, -1):
            orig_low, emitted = self._open_stack[idx]
            if orig_low == tag_lower:
                while len(self._open_stack) > idx:
                    _, popped_emitted = self._open_stack.pop()
                    if popped_emitted:
                        self.chunks.append(f"</{popped_emitted}>")
                break

    def handle_startendtag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        tag_lower = tag.lower()
        if tag_lower in _SUPPRESSED_TAGS or self._suppress_depth > 0:
            return
        if tag_lower in _VOID_TAGS:
            self.handle_starttag(tag, attrs)
        else:
            self.handle_starttag(tag, attrs)
            self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if self._suppress_depth == 0 and data:
            self.chunks.append(html.escape(data, quote=False))

    def handle_entityref(self, name: str) -> None:
        if self._suppress_depth == 0:
            self.chunks.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        if self._suppress_depth == 0:
            self.chunks.append(f"&#{name};")

    def close_remaining(self) -> str:
        while self._open_stack:
            _, emitted = self._open_stack.pop()
            if emitted:
                self.chunks.append(f"</{emitted}>")
        return "".join(self.chunks)


def sanitize_and_rewrite_chapter_html(
    raw_xhtml: str,
    book_id: str,
    chapter_archive_path: str,
    resolve_spine_by_archive_path: Callable[[str], Optional[int]],
    read_archive_text: Callable[[str], Optional[str]],
) -> Tuple[str, List[str]]:
    styles: List[str] = []

    for link_match in re.finditer(r"<link\b[^>]*>", raw_xhtml, flags=re.IGNORECASE):
        tag_text = link_match.group(0)
        rel_match = re.search(r"\brel\s*=\s*(['\"])(.*?)\1", tag_text, flags=re.IGNORECASE)
        href_match = re.search(r"\bhref\s*=\s*(['\"])(.*?)\1", tag_text, flags=re.IGNORECASE)
        if rel_match and "stylesheet" in rel_match.group(2).lower() and href_match:
            href = href_match.group(2).strip()
            if href and not _is_external_scheme(href):
                css_path = resolve_relative_archive_path(chapter_archive_path, href)
                css_text = read_archive_text(css_path)
                if css_text:
                    styles.append(scope_and_rewrite_css(css_text, css_path, book_id))

    for style_match in re.finditer(r"<style\b[^>]*>([\s\S]*?)</style>", raw_xhtml, flags=re.IGNORECASE):
        inline_css = style_match.group(1)
        if inline_css and inline_css.strip():
            styles.append(scope_and_rewrite_css(inline_css, chapter_archive_path, book_id))

    body_match = re.search(r"<body\b[^>]*>([\s\S]*?)</body>", raw_xhtml, flags=re.IGNORECASE)
    body_content = body_match.group(1) if body_match else raw_xhtml

    parser = _ChapterSanitizerParser(
        book_id=book_id,
        chapter_archive_path=chapter_archive_path,
        resolve_spine_by_archive_path=resolve_spine_by_archive_path,
    )
    parser.feed(body_content)
    parser.close()
    sanitized_html = parser.close_remaining()
    return sanitized_html, styles


class _PlainTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []
        self._suppress = 0

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        t = tag.lower()
        if t in ("script", "style", "head", "noscript"):
            self._suppress += 1
            return
        if self._suppress > 0:
            return
        if t in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self.parts.append("\n\n## ")
        elif t == "br":
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t in ("script", "style", "head", "noscript"):
            if self._suppress > 0:
                self._suppress -= 1
            return
        if self._suppress > 0:
            return
        if t in ("p", "div", "section", "article", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre"):
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._suppress == 0 and data:
            self.parts.append(data)


def _strip_tags_text(fragment: str) -> str:
    cleaned = re.sub(r"<[^>]+>", " ", fragment)
    return re.sub(r"\s+", " ", html.unescape(cleaned)).strip()


def extract_chapter_plain_text(raw_xhtml: str) -> Tuple[Optional[str], str]:
    title_match = (
        re.search(r"<h1\b[^>]*>([\s\S]*?)</h1>", raw_xhtml, flags=re.IGNORECASE)
        or re.search(r"<h2\b[^>]*>([\s\S]*?)</h2>", raw_xhtml, flags=re.IGNORECASE)
        or re.search(r"<title\b[^>]*>([\s\S]*?)</title>", raw_xhtml, flags=re.IGNORECASE)
    )
    title_guess = _strip_tags_text(title_match.group(1)) if title_match else None
    if not title_guess:
        title_guess = None

    body_match = re.search(r"<body\b[^>]*>([\s\S]*?)</body>", raw_xhtml, flags=re.IGNORECASE)
    source = body_match.group(1) if body_match else raw_xhtml

    extractor = _PlainTextExtractor()
    extractor.feed(source)
    extractor.close()

    raw_text = "".join(extractor.parts)
    normalized = raw_text.replace("\r\n", "\n")
    normalized = re.sub(r"[ \t]+\n", "\n", normalized)
    normalized = re.sub(r"\n{3,}", "\n\n", normalized).strip()
    return title_guess, normalized
