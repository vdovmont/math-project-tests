"""Optimizer tab backend: independent profiles and API actions."""

import json
import shutil
import threading
import uuid
from datetime import datetime
from http import HTTPStatus
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import json_http_optimizer as optimizer
from settings_storage import UrlStorageError, load_saved_urls

from config import (
    PROFILE_FILE,
    OPTIMIZER_MAX_PROFILE_NAME_LENGTH as MAX_PROFILE_NAME_LENGTH,
    OPTIMIZER_MAX_SAVED_PROFILES as MAX_SAVED_PROFILES,
    OPTIMIZER_GROUPS_FOLDER,
    OPTIMIZER_GROUP_SELECTION_FILE,
    OPTIMIZER_MAX_GROUP_NAME_LENGTH,
    MAX_VIEW_FILE_BYTES,
    MAX_UPLOAD_FILE_BYTES,
    MAX_UPLOAD_FILES,
)


LOCK = threading.Lock()
GROUP_LOCK = threading.Lock()


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
            result[name] = validate_profile_options(options)
        return result
    except ValueError as error:
        raise OSError(f"Could not load Optimizer profiles: {error}") from error


def validate_profile_options(options):
    profile = optimizer.validate_options(options)
    saved_url_name = options.get("saved_url_name") if isinstance(options, dict) else None
    profile["saved_url_name"] = (
        saved_url_name if isinstance(saved_url_name, str) and saved_url_name else None
    )
    return profile


def config():
    urls_error = None
    try:
        saved_urls = load_saved_urls()
    except UrlStorageError as error:
        saved_urls = {}
        urls_error = str(error)
    return {
        "default": {
            "base_url": optimizer.BASE_URL,
            "distance_percent": optimizer.DISTANCE_PERCENT,
            "saved_url_name": None,
        },
        "previous": None,
        "previous_profile": None,
        "profiles": load_profiles(),
        "saved_urls": saved_urls,
        "urls_error": urls_error,
    }


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
            profile = validate_profile_options(data.get("options"))
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


def validate_group_name(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Group name must not be empty.")
    name = value.strip()
    if len(name) > OPTIMIZER_MAX_GROUP_NAME_LENGTH:
        raise ValueError(
            f"Group name must not exceed {OPTIMIZER_MAX_GROUP_NAME_LENGTH} characters."
        )
    if any(ord(character) < 32 for character in name):
        raise ValueError("Group name must not contain control characters.")
    return name


def group_folder(group_id: str) -> Path:
    if not isinstance(group_id, str) or not group_id:
        raise ValueError("Select an Optimizer group.")
    folder = (OPTIMIZER_GROUPS_FOLDER / group_id).resolve()
    root = OPTIMIZER_GROUPS_FOLDER.resolve()
    if folder.parent != root or not folder.is_dir():
        raise ValueError("Optimizer group does not exist.")
    return folder


def read_group_manifest(folder: Path) -> dict[str, str]:
    try:
        payload = json.loads((folder / "group.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise OSError(f"Could not read Optimizer group '{folder.name}': {error}") from error
    if (
        not isinstance(payload, dict)
        or payload.get("version") != 1
        or payload.get("id") != folder.name
    ):
        raise OSError(f"Optimizer group '{folder.name}' has invalid metadata.")
    return {
        "id": folder.name,
        "name": validate_group_name(payload.get("name")),
        "created_at": str(payload.get("created_at", "")),
    }


def load_group_selection() -> str | None:
    try:
        payload = json.loads(OPTIMIZER_GROUP_SELECTION_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise OSError(f"Could not read Optimizer group selection: {error}") from error
    if not isinstance(payload, dict) or payload.get("version") != 1:
        return None
    selected = payload.get("selected_group_id")
    return selected if isinstance(selected, str) else None


def write_group_selection(group_id: str | None) -> None:
    OPTIMIZER_GROUP_SELECTION_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = OPTIMIZER_GROUP_SELECTION_FILE.with_suffix(".json.tmp")
    try:
        temporary.write_text(
            json.dumps(
                {"version": 1, "selected_group_id": group_id}, indent=2
            ) + "\n",
            encoding="utf-8",
        )
        temporary.replace(OPTIMIZER_GROUP_SELECTION_FILE)
    finally:
        temporary.unlink(missing_ok=True)


def optimizer_groups() -> dict[str, object]:
    OPTIMIZER_GROUPS_FOLDER.mkdir(parents=True, exist_ok=True)
    groups = []
    for folder in OPTIMIZER_GROUPS_FOLDER.iterdir():
        if folder.is_dir() and not folder.is_symlink() and (folder / "group.json").is_file():
            manifest = read_group_manifest(folder)
            initial = sorted(
                (
                    {"name": file.name, "path": file.name, "size": file.stat().st_size}
                    for file in (folder / "initial").glob("*-input.json")
                    if file.is_file()
                ),
                key=lambda item: item["name"].casefold(),
            )
            merged_file = folder / "merged" / "merged-input.json"
            merged = (
                {"name": merged_file.name, "path": merged_file.name, "size": merged_file.stat().st_size}
                if merged_file.is_file() else None
            )
            outputs = sorted(
                (
                    {
                        "name": file.name,
                        "path": file.relative_to(folder / "outputs").as_posix(),
                        "size": file.stat().st_size,
                    }
                    for file in (folder / "outputs").rglob("*.json")
                    if file.is_file()
                ),
                key=lambda item: item["path"].casefold(),
            )
            groups.append({**manifest, "initial": initial, "merged": merged, "outputs": outputs})
    groups.sort(key=lambda item: (item["name"].casefold(), item["id"]))
    selected = load_group_selection()
    ids = {str(group["id"]) for group in groups}
    if selected not in ids:
        selected = str(groups[0]["id"]) if groups else None
        write_group_selection(selected)
    return {"groups": groups, "selected_group_id": selected}


def create_group(data: object) -> dict[str, object]:
    if not isinstance(data, dict):
        raise ValueError("Request body must be a JSON object.")
    name = validate_group_name(data.get("name"))
    with GROUP_LOCK:
        current = optimizer_groups()
        if any(str(group["name"]).casefold() == name.casefold() for group in current["groups"]):
            raise ValueError(f"An Optimizer group named '{name}' already exists.")
        group_id = uuid.uuid4().hex
        folder = OPTIMIZER_GROUPS_FOLDER / group_id
        folder.mkdir(parents=True)
        for child in ("initial", "merged", "outputs"):
            (folder / child).mkdir()
        (folder / "group.json").write_text(
            json.dumps(
                {"version": 1, "id": group_id, "name": name,
                 "created_at": datetime.now().isoformat(timespec="seconds")},
                indent=2,
            ) + "\n",
            encoding="utf-8",
        )
        if current["selected_group_id"] is None:
            write_group_selection(group_id)
    return optimizer_groups()


def select_group(data: object) -> dict[str, object]:
    if not isinstance(data, dict):
        raise ValueError("Request body must be a JSON object.")
    folder = group_folder(data.get("group_id"))
    write_group_selection(folder.name)
    return {"selected_group_id": folder.name}


def add_initial_files(data: object) -> dict[str, object]:
    if not isinstance(data, dict) or not isinstance(data.get("files"), list):
        raise ValueError("Select one or more input JSON files.")
    files = data["files"]
    if not files or len(files) > MAX_UPLOAD_FILES:
        raise ValueError(f"Select between 1 and {MAX_UPLOAD_FILES} files.")
    folder = group_folder(data.get("group_id")) / "initial"
    prepared: list[tuple[Path, str]] = []
    names: set[str] = set()
    for item in files:
        if not isinstance(item, dict):
            raise ValueError("Invalid uploaded file.")
        name, content = item.get("name"), item.get("content")
        if not isinstance(name, str) or Path(name).name != name or not name.endswith("-input.json"):
            raise ValueError("Every file must have a safe name ending in '-input.json'.")
        if not isinstance(content, str) or len(content.encode("utf-8")) > MAX_UPLOAD_FILE_BYTES:
            raise ValueError(f"'{name}' is missing content or exceeds the size limit.")
        try:
            json.loads(content)
        except json.JSONDecodeError as error:
            raise ValueError(f"'{name}' is not valid JSON: {error.msg}.") from error
        if name.casefold() in names or (folder / name).exists():
            raise ValueError(f"Initial file '{name}' already exists in this group.")
        names.add(name.casefold())
        prepared.append((folder / name, content))
    for path, content in prepared:
        path.write_text(content, encoding="utf-8")
    return optimizer_groups()


def delete_group(data: object) -> dict[str, object]:
    if not isinstance(data, dict):
        raise ValueError("Request body must be a JSON object.")
    with GROUP_LOCK:
        folder = group_folder(data.get("group_id"))
        before = optimizer_groups()
        deleted_index = next(
            index for index, group in enumerate(before["groups"])
            if group["id"] == folder.name
        )
        shutil.rmtree(folder)
        remaining_ids = [
            str(group["id"]) for group in before["groups"] if group["id"] != folder.name
        ]
        if before["selected_group_id"] == folder.name:
            replacement = (
                remaining_ids[min(deleted_index, len(remaining_ids) - 1)]
                if remaining_ids else None
            )
            write_group_selection(replacement)
        current = optimizer_groups()
    return current


def category_root(folder: Path, category: object) -> Path:
    roots = {"initial": folder / "initial", "merged": folder / "merged", "outputs": folder / "outputs"}
    if category not in roots:
        raise ValueError("Invalid Optimizer file category.")
    return roots[category]


def safe_category_file(group_id: object, category: object, relative_path: object) -> Path:
    if not isinstance(relative_path, str) or not relative_path:
        raise ValueError("Select an Optimizer JSON file.")
    root = category_root(group_folder(group_id), category).resolve()
    file = (root / relative_path).resolve()
    if not file.is_relative_to(root) or not file.is_file() or file.suffix.lower() != ".json":
        raise ValueError("Optimizer JSON file does not exist.")
    return file


def delete_files(data: object) -> dict[str, object]:
    if not isinstance(data, dict) or not isinstance(data.get("files"), list) or not data["files"]:
        raise ValueError("Select one or more files to delete.")
    files = []
    for item in data["files"]:
        if not isinstance(item, dict):
            raise ValueError("Invalid file selection.")
        files.append(safe_category_file(
            data.get("group_id"), item.get("category"), item.get("path")
        ))
    for file in files:
        file.unlink()
    return optimizer_groups()


GET_PATHS = frozenset({"/api/optimizer/config", "/api/optimizer/groups", "/api/optimizer/file"})
POST_PATHS = frozenset({
    "/api/optimizer/profile/save", "/api/optimizer/profile/delete",
    "/api/optimizer/group/create", "/api/optimizer/group/select",
    "/api/optimizer/group/delete", "/api/optimizer/files/add",
    "/api/optimizer/files/delete",
})


def handle_get(request) -> None:
    parsed = urlparse(request.path)
    if parsed.path == "/api/optimizer/config":
        request.send_json(config())
    elif parsed.path == "/api/optimizer/groups":
        request.send_json(optimizer_groups())
    else:
        try:
            query = parse_qs(parsed.query)
            file = safe_category_file(
                query.get("group", [None])[0], query.get("category", [None])[0],
                query.get("path", [None])[0],
            )
            if file.stat().st_size > MAX_VIEW_FILE_BYTES:
                raise ValueError("File is too large to display.")
            content = file.read_text(encoding="utf-8")
            request.send_json({"name": file.name, "size": file.stat().st_size,
                               "content": json.dumps(json.loads(content), indent=2,
                                                     ensure_ascii=False)})
        except (ValueError, UnicodeError) as error:
            request.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)


def handle_post(request_path: str, data: object) -> tuple[object, int]:
    if request_path not in POST_PATHS:
        raise ValueError("Unknown Optimizer action.")
    if request_path.startswith("/api/optimizer/profile/"):
        return update_profile(data, delete=request_path.endswith("/delete")), HTTPStatus.OK
    actions = {
        "/api/optimizer/group/create": create_group,
        "/api/optimizer/group/select": select_group,
        "/api/optimizer/group/delete": delete_group,
        "/api/optimizer/files/add": add_initial_files,
        "/api/optimizer/files/delete": delete_files,
    }
    return actions[request_path](data), HTTPStatus.OK
