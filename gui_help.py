"""Editable help pages shared by the Tester and Optimizer GUI sections."""

import base64
import binascii
import html
import json
import re
import secrets
import threading
import time
import uuid
from http import HTTPStatus
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from config import (
    HELP_EDITOR_PASSWORD,
    HELP_EDITOR_USERNAME,
    HELP_IMAGES_FOLDER,
    HELP_MAX_IMAGE_BYTES,
    HELP_SESSION_SECONDS,
    OPTIMIZER_HELP_FILE,
    TESTER_HELP_FILE,
)


HELP_FILES = {
    "tester": TESTER_HELP_FILE,
    "optimizer": OPTIMIZER_HELP_FILE,
}
MAX_HELP_TITLE_LENGTH = 120
MAX_HELP_CONTENT_LENGTH = 250_000
LOCK = threading.Lock()
SESSIONS: dict[str, float] = {}

IMAGE_TYPES = {
    "image/png": (".png", b"\x89PNG\r\n\x1a\n"),
    "image/jpeg": (".jpg", b"\xff\xd8\xff"),
    "image/webp": (".webp", b"RIFF"),
    "image/gif": (".gif", b"GIF8"),
}
HELP_FONT_SIZES = {8, 9, 10, 11, 12, 14, 16, 18, 20, 22, 24, 26, 28, 32, 36, 48, 72}
ALLOWED_TAGS = {
    "p", "div", "br", "strong", "b", "u", "ol", "ul", "li", "h2", "h3", "h4",
    "font", "span", "img",
}


class HelpHtmlSanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in ALLOWED_TAGS:
            return
        safe_attrs: list[tuple[str, str]] = []
        values = dict(attrs)
        if tag == "font":
            if values.get("size") in {"2", "3", "4", "5", "6"}:
                safe_attrs.append(("size", values["size"]))
            else:
                style = values.get("style", "")
                match = re.fullmatch(
                    r"\s*font-size\s*:\s*(\d+)px\s*;?\s*", style, re.IGNORECASE
                )
                if match and int(match.group(1)) in HELP_FONT_SIZES:
                    safe_attrs.append(("style", f"font-size: {int(match.group(1))}px"))
        elif tag == "span":
            style = values.get("style", "")
            match = re.fullmatch(r"\s*font-size\s*:\s*(\d+)px\s*;?\s*", style, re.IGNORECASE)
            if match and int(match.group(1)) in HELP_FONT_SIZES:
                safe_attrs.append(("style", f"font-size: {int(match.group(1))}px"))
        elif tag == "img":
            source = values.get("src", "")
            if not source.startswith("/api/help/image?"):
                return
            safe_attrs.extend((("src", source), ("alt", values.get("alt", "Image")[:200])))
        rendered = "".join(
            f' {name}="{html.escape(value, quote=True)}"' for name, value in safe_attrs
        )
        self.parts.append(f"<{tag}{rendered}>")

    def handle_endtag(self, tag: str) -> None:
        if tag in ALLOWED_TAGS and tag not in {"br", "img"}:
            self.parts.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        self.parts.append(html.escape(data))


def sanitize_help_html(content: str) -> str:
    sanitizer = HelpHtmlSanitizer()
    sanitizer.feed(content)
    sanitizer.close()
    return "".join(sanitizer.parts)


class HelpImageCollector(HTMLParser):
    def __init__(self, section: str) -> None:
        super().__init__(convert_charrefs=True)
        self.section = section
        self.names: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "img":
            return
        source = dict(attrs).get("src")
        if not isinstance(source, str):
            return
        parsed = urlparse(source)
        query = parse_qs(parsed.query)
        name = query.get("name", [""])[0]
        image_section = query.get("section", [""])[0]
        if (
            parsed.path == "/api/help/image"
            and image_section == self.section
            and name
            and Path(name).name == name
        ):
            self.names.add(name)


def referenced_help_images(section: object, payload: dict[str, object]) -> set[str]:
    collector = HelpImageCollector(str(section))
    for item in payload["items"]:
        if item.get("format") == "html":
            collector.feed(item["content"])
    collector.close()
    return collector.names


def remove_unreferenced_help_images(section: object, payload: dict[str, object]) -> None:
    folder = HELP_IMAGES_FOLDER / str(section)
    if not folder.is_dir():
        return
    referenced = referenced_help_images(section, payload)
    managed_suffixes = {suffix for suffix, _ in IMAGE_TYPES.values()}
    for path in folder.iterdir():
        if path.is_file() and path.suffix.lower() in managed_suffixes and path.name not in referenced:
            path.unlink()


def help_file(section: object) -> Path:
    if section not in HELP_FILES:
        raise ValueError("Invalid help section.")
    return HELP_FILES[str(section)]


def load_help(section: object) -> dict[str, object]:
    path = help_file(section)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"version": 1, "items": []}
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise OSError(f"Could not read {section} help: {error}") from error
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise OSError(f"The {section} help file has an unsupported format.")
    items = payload.get("items")
    if not isinstance(items, list):
        raise OSError(f"The {section} help file has an invalid item list.")
    validated = []
    for item in items:
        if not isinstance(item, dict):
            raise OSError(f"The {section} help file contains an invalid item.")
        item_id, title, content = item.get("id"), item.get("title"), item.get("content")
        if not all(isinstance(value, str) for value in (item_id, title, content)):
            raise OSError(f"The {section} help file contains an invalid item.")
        content_format = item.get("format", "text")
        if content_format not in {"text", "html"}:
            raise OSError(f"The {section} help file contains an invalid content format.")
        if content_format == "html":
            content = sanitize_help_html(content)
        validated.append({"id": item_id, "title": title, "content": content, "format": content_format})
    return {"version": 1, "items": validated}


def write_help(section: object, payload: dict[str, object]) -> None:
    path = help_file(section)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def require_editor(data: object) -> None:
    if not isinstance(data, dict):
        raise ValueError("Request body must be a JSON object.")
    token = data.get("editor_token")
    now = time.monotonic()
    with LOCK:
        expired = [key for key, deadline in SESSIONS.items() if deadline <= now]
        for key in expired:
            SESSIONS.pop(key, None)
        if not isinstance(token, str) or SESSIONS.get(token, 0) <= now:
            raise ValueError("Editor login has expired. Log in again.")
        SESSIONS[token] = now + HELP_SESSION_SECONDS


def login(data: object) -> dict[str, object]:
    if not isinstance(data, dict):
        raise ValueError("Request body must be a JSON object.")
    username, password = data.get("username"), data.get("password")
    valid = (
        isinstance(username, str)
        and isinstance(password, str)
        and secrets.compare_digest(username, HELP_EDITOR_USERNAME)
        and secrets.compare_digest(password, HELP_EDITOR_PASSWORD)
    )
    if not valid:
        raise ValueError("Incorrect help editor username or password.")
    token = secrets.token_urlsafe(32)
    with LOCK:
        SESSIONS[token] = time.monotonic() + HELP_SESSION_SECONDS
    return {"editor_token": token}


def logout(data: object) -> dict[str, object]:
    if isinstance(data, dict) and isinstance(data.get("editor_token"), str):
        with LOCK:
            SESSIONS.pop(data["editor_token"], None)
    return {"logged_out": True}


def session_status(data: object) -> dict[str, object]:
    require_editor(data)
    return {"editor": True}


def save_item(data: object) -> dict[str, object]:
    require_editor(data)
    section = data.get("section")
    title, content = data.get("title"), data.get("content")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("Help title must not be empty.")
    title = title.strip()
    if len(title) > MAX_HELP_TITLE_LENGTH:
        raise ValueError(f"Help title must not exceed {MAX_HELP_TITLE_LENGTH} characters.")
    if not isinstance(content, str) or len(content) > MAX_HELP_CONTENT_LENGTH:
        raise ValueError(f"Help content must not exceed {MAX_HELP_CONTENT_LENGTH} characters.")
    content = sanitize_help_html(content)
    with LOCK:
        payload = load_help(section)
        item_id = data.get("id")
        if item_id is None:
            item_id = uuid.uuid4().hex
            payload["items"].append({"id": item_id, "title": title, "content": content, "format": "html"})
        else:
            item = next((item for item in payload["items"] if item["id"] == item_id), None)
            if item is None:
                raise ValueError("Help item no longer exists.")
            item.update(title=title, content=content, format="html")
        write_help(section, payload)
        remove_unreferenced_help_images(section, payload)
    return {**payload, "selected_id": item_id}


def upload_image(data: object) -> dict[str, object]:
    require_editor(data)
    section, mime_type, encoded = data.get("section"), data.get("type"), data.get("content")
    help_file(section)
    if mime_type not in IMAGE_TYPES or not isinstance(encoded, str):
        raise ValueError("Select a PNG, JPEG, WebP, or GIF image.")
    try:
        image = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as error:
        raise ValueError("The uploaded image is invalid.") from error
    if not image or len(image) > HELP_MAX_IMAGE_BYTES:
        raise ValueError(f"Images must be no larger than {HELP_MAX_IMAGE_BYTES // (1024 * 1024)} MB.")
    suffix, signature = IMAGE_TYPES[mime_type]
    if not image.startswith(signature) or (mime_type == "image/webp" and image[8:12] != b"WEBP"):
        raise ValueError("The uploaded file does not match its image type.")
    folder = HELP_IMAGES_FOLDER / str(section)
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{uuid.uuid4().hex}{suffix}"
    (folder / name).write_bytes(image)
    return {"url": f"/api/help/image?section={section}&name={name}"}


def delete_item(data: object) -> dict[str, object]:
    require_editor(data)
    section, item_id = data.get("section"), data.get("id")
    with LOCK:
        payload = load_help(section)
        remaining = [item for item in payload["items"] if item["id"] != item_id]
        if len(remaining) == len(payload["items"]):
            raise ValueError("Help item no longer exists.")
        payload["items"] = remaining
        write_help(section, payload)
        remove_unreferenced_help_images(section, payload)
    return payload


def reorder_items(data: object) -> dict[str, object]:
    require_editor(data)
    section, ordered_ids = data.get("section"), data.get("ordered_ids")
    if not isinstance(ordered_ids, list) or not all(
        isinstance(item_id, str) for item_id in ordered_ids
    ):
        raise ValueError("Invalid help item order.")
    with LOCK:
        payload = load_help(section)
        items_by_id = {item["id"]: item for item in payload["items"]}
        if len(ordered_ids) != len(items_by_id) or set(ordered_ids) != set(items_by_id):
            raise ValueError("Help contents changed. Refresh the page and try again.")
        payload["items"] = [items_by_id[item_id] for item_id in ordered_ids]
        write_help(section, payload)
    selected_id = data.get("selected_id")
    return {**payload, "selected_id": selected_id if selected_id in items_by_id else None}


GET_PATHS = frozenset({"/api/help", "/api/help/image"})
POST_PATHS = frozenset({
    "/api/help/login", "/api/help/logout", "/api/help/session", "/api/help/save",
    "/api/help/delete", "/api/help/reorder", "/api/help/image/upload",
})


def handle_get(request) -> None:
    parsed = urlparse(request.path)
    query = parse_qs(parsed.query)
    section = query.get("section", [None])[0]
    try:
        if parsed.path == "/api/help/image":
            name = query.get("name", [""])[0]
            path = HELP_IMAGES_FOLDER / str(section) / name
            if help_file(section) and (not name or Path(name).name != name or not path.is_file()):
                request.send_json({"error": "Image not found."}, HTTPStatus.NOT_FOUND)
                return
            mime_type = next((kind for kind, (suffix, _) in IMAGE_TYPES.items() if path.suffix == suffix), None)
            if mime_type is None:
                raise ValueError("Invalid image type.")
            request.send_bytes(path.read_bytes(), mime_type)
        else:
            request.send_json(load_help(section))
    except ValueError as error:
        request.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)


def handle_post(request_path: str, data: object) -> tuple[object, int]:
    actions = {
        "/api/help/login": login,
        "/api/help/logout": logout,
        "/api/help/session": session_status,
        "/api/help/save": save_item,
        "/api/help/delete": delete_item,
        "/api/help/reorder": reorder_items,
        "/api/help/image/upload": upload_image,
    }
    if request_path not in actions:
        raise ValueError("Unknown help action.")
    return actions[request_path](data), HTTPStatus.OK
