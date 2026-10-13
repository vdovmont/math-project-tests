"""Persistence shared by the Tester and Optimizer settings interfaces."""

from __future__ import annotations

import json
import threading
from urllib.parse import urlparse

from config import MAX_PROFILE_NAME_LENGTH, MAX_SAVED_PROFILES, SAVED_URLS_FILE


URLS_LOCK = threading.Lock()


class UrlStorageError(Exception):
    pass


def validate_name(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("URL name must be text.")
    name = value.strip()
    if not name:
        raise ValueError("URL name must not be empty.")
    if len(name) > MAX_PROFILE_NAME_LENGTH:
        raise ValueError(
            f"URL name must not exceed {MAX_PROFILE_NAME_LENGTH} characters."
        )
    if any(ord(character) < 32 for character in name):
        raise ValueError("URL name must not contain control characters.")
    return name


def validate_base_url(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Base URL must not be empty.")
    url = value.strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Base URL must be a valid HTTP or HTTPS URL.")
    return url


def load_saved_urls() -> dict[str, str]:
    try:
        with SAVED_URLS_FILE.open("r", encoding="utf-8") as file:
            payload = json.load(file)
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise UrlStorageError(f"Could not read saved URLs: {error}") from error
    try:
        if not isinstance(payload, dict) or payload.get("version") != 1:
            raise ValueError("saved URL file has an unsupported format")
        raw_urls = payload.get("urls")
        if not isinstance(raw_urls, dict) or len(raw_urls) > MAX_SAVED_PROFILES:
            raise ValueError("saved URL file has an invalid URLs object")
        urls: dict[str, str] = {}
        normalized_names: set[str] = set()
        for raw_name, raw_url in raw_urls.items():
            name = validate_name(raw_name)
            if name.casefold() in normalized_names:
                raise ValueError(f"URL name '{name}' duplicates another saved name")
            normalized_names.add(name.casefold())
            urls[name] = validate_base_url(raw_url)
        return dict(sorted(urls.items(), key=lambda item: item[0].casefold()))
    except ValueError as error:
        raise UrlStorageError(f"Could not load saved URLs: {error}") from error


def write_saved_urls(urls: dict[str, str]) -> None:
    temporary_file = SAVED_URLS_FILE.with_suffix(SAVED_URLS_FILE.suffix + ".tmp")
    try:
        with temporary_file.open("w", encoding="utf-8", newline="") as file:
            json.dump({"version": 1, "urls": urls}, file, indent=2, ensure_ascii=False)
            file.write("\n")
        temporary_file.replace(SAVED_URLS_FILE)
    except OSError:
        temporary_file.unlink(missing_ok=True)
        raise


def save_named_url(data: object) -> dict[str, object]:
    if not isinstance(data, dict):
        raise ValueError("Request body must be a JSON object.")
    name = validate_name(data.get("name"))
    url = validate_base_url(data.get("url"))
    with URLS_LOCK:
        urls = load_saved_urls()
        if any(existing.casefold() == name.casefold() for existing in urls):
            raise ValueError(f"A URL named '{name}' already exists.")
        if len(urls) >= MAX_SAVED_PROFILES:
            raise ValueError(f"At most {MAX_SAVED_PROFILES} URLs can be saved.")
        urls[name] = url
        urls = dict(sorted(urls.items(), key=lambda item: item[0].casefold()))
        write_saved_urls(urls)
    return {"name": name, "url": url, "urls": urls}


def delete_named_url(data: object) -> dict[str, object]:
    if not isinstance(data, dict):
        raise ValueError("Request body must be a JSON object.")
    name = validate_name(data.get("name"))
    with URLS_LOCK:
        urls = load_saved_urls()
        matching_name = next(
            (existing for existing in urls if existing.casefold() == name.casefold()),
            None,
        )
        if matching_name is None:
            raise ValueError(f"URL '{name}' does not exist.")
        del urls[matching_name]
        write_saved_urls(urls)
    return {"deleted": matching_name, "urls": urls}
