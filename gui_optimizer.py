"""Optimizer tab backend: independent profiles and API actions."""

import json
import threading
from http import HTTPStatus

import json_http_optimizer as optimizer

from config import (
    PROFILE_FILE,
    OPTIMIZER_MAX_PROFILE_NAME_LENGTH as MAX_PROFILE_NAME_LENGTH,
    OPTIMIZER_MAX_SAVED_PROFILES as MAX_SAVED_PROFILES,
)


LOCK = threading.Lock()


def validate_name(name):
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Profile name must not be empty.")
    name = name.strip()
    if len(name) > MAX_PROFILE_NAME_LENGTH or any(ord(char) < 32 for char in name):
        raise ValueError(f"Profile name must be at most {MAX_PROFILE_NAME_LENGTH} characters without control characters.")
    return name


def load_profiles():
    try:
        payload = json.loads(PROFILE_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (UnicodeError, json.JSONDecodeError) as error:
        raise OSError(f"Could not read Optimizer profiles: {error}") from error
    try:
        if not isinstance(payload, dict) or payload.get("version") != 1:
            raise ValueError("Unsupported profile format.")
        profiles = payload.get("profiles")
        if not isinstance(profiles, dict) or len(profiles) > MAX_SAVED_PROFILES:
            raise ValueError("Invalid profiles object.")
        result = {}
        names = set()
        for name, options in profiles.items():
            name = validate_name(name)
            if name.casefold() in names:
                raise ValueError("Duplicate profile names.")
            names.add(name.casefold())
            result[name] = optimizer.validate_options(options)
        return result
    except ValueError as error:
        raise OSError(f"Could not load Optimizer profiles: {error}") from error


def config():
    return {"default": {"base_url": optimizer.BASE_URL, "distance_percent": optimizer.DISTANCE_PERCENT},
            "previous": None, "previous_profile": None, "profiles": load_profiles()}


def update_profile(data, *, delete=False):
    if not isinstance(data, dict):
        raise ValueError("Request body must be a JSON object.")
    name = validate_name(data.get("name"))
    with LOCK:
        profiles = load_profiles()
        existing = next((key for key in profiles if key.casefold() == name.casefold()), None)
        if delete:
            if existing is None:
                raise ValueError("Profile does not exist.")
            del profiles[existing]
            profile = None
        else:
            if existing is not None:
                raise ValueError("A profile with this name already exists.")
            if len(profiles) >= MAX_SAVED_PROFILES:
                raise ValueError(f"At most {MAX_SAVED_PROFILES} profiles can be saved.")
            profile = optimizer.validate_options(data.get("options"))
            profiles[name] = profile
        PROFILE_FILE.parent.mkdir(parents=True, exist_ok=True)
        temporary = PROFILE_FILE.with_suffix(".json.tmp")
        try:
            temporary.write_text(json.dumps({"version": 1, "profiles": profiles},
                                            indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            temporary.replace(PROFILE_FILE)
        finally:
            temporary.unlink(missing_ok=True)
        return {"name": name, "profile": profile, "profiles": profiles,
                "previous_profile": None}


GET_PATHS = frozenset({"/api/optimizer/config"})
POST_PATHS = frozenset({"/api/optimizer/profile/save", "/api/optimizer/profile/delete"})


def handle_get(request) -> None:
    request.send_json(config())


def handle_post(request_path: str, data: object) -> tuple[object, int]:
    if request_path not in POST_PATHS:
        raise ValueError("Unknown Optimizer action.")
    return update_profile(data, delete=request_path.endswith("/delete")), HTTPStatus.OK
