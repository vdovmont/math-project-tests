"""Optimizer tab backend: independent profiles and API actions."""

import json
import shutil
import threading
import time
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


class OptimizerRunState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.running = False
        self.logs: list[str] = []
        self.started_monotonic: float | None = None
        self.elapsed_time_seconds = 0.0
        self.exit_code: int | None = None
        self.result_file: dict[str, str] | None = None
        self.active_query: int | None = None
        self.run_id = 0
        self.progress_active = False

    def append_log(self, line: str, *, progress: bool = False) -> None:
        with self.lock:
            if progress and self.progress_active and self.logs:
                self.logs[-1] = line
            else:
                self.logs.append(line)
            self.progress_active = progress

    def start(self, data: object) -> dict[str, object]:
        if not isinstance(data, dict):
            raise ValueError("Request body must be a JSON object.")
        options = optimizer.validate_options(data)
        folder = group_folder(data.get("group_id"))
        merged_file = folder / "merged" / "merged-input.json"
        if not merged_file.is_file():
            raise ValueError("Merge the selected group before starting optimization.")
        try:
            merged_json = json.loads(merged_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ValueError(f"Could not read merged input: {error}") from error
        if not isinstance(merged_json, dict):
            raise ValueError("Merged input must contain a JSON object.")
        merged_distance = merged_json.get("options", {}).get(
            "optimize_jobs_between_depots_distance"
        ) if isinstance(merged_json.get("options"), dict) else None
        if merged_distance != options["distance_percent"]:
            raise ValueError("Distance changed after the merge. Merge the group again.")
        with self.lock:
            if self.running:
                raise RuntimeError("An Optimizer run is already in progress.")
            previous_options, previous_profile = save_previous_profile(data)
            self.running = True
            self.logs = []
            self.started_monotonic = time.monotonic()
            self.elapsed_time_seconds = 0.0
            self.exit_code = None
            self.result_file = None
            self.active_query = None
            self.progress_active = False
            self.run_id += 1
        threading.Thread(
            target=self._run,
            args=(folder, merged_json, str(options["base_url"])),
            daemon=True,
        ).start()
        result = self.snapshot()
        result["previous_options"] = previous_options
        result["previous_profile"] = previous_profile
        return result

    def _run(self, folder: Path, merged_json: dict[str, object], base_url: str) -> None:
        exit_code = 1
        try:
            self.append_log(f"Sending merged input to {base_url}...")

            def on_query(query: int, start_url: str) -> None:
                with self.lock:
                    self.active_query = query
                self.append_log(f"GET {start_url}")
                self.append_log(f"Query number: {query}")

            _, output = optimizer.run_optimization(
                merged_json,
                base_url,
                on_query=on_query,
                on_progress=lambda line: self.append_log(line, progress=True),
            )
            outputs = folder / "outputs"
            outputs.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            output_file = outputs / f"optimization-{timestamp}-output.json"
            output_file.write_text(
                json.dumps(output, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            self.append_log(f"Saved result: {output_file.name}")
            with self.lock:
                self.result_file = {
                    "group_id": folder.name,
                    "category": "outputs",
                    "path": output_file.name,
                }
            exit_code = 0
        except Exception as error:
            self.append_log(f"Optimization failed: {error}")
        finally:
            with self.lock:
                if self.started_monotonic is not None:
                    self.elapsed_time_seconds = time.monotonic() - self.started_monotonic
                self.running = False
                self.exit_code = exit_code
                self.active_query = None
                self.progress_active = False

    def snapshot(self) -> dict[str, object]:
        with self.lock:
            elapsed = self.elapsed_time_seconds
            if self.running and self.started_monotonic is not None:
                elapsed = time.monotonic() - self.started_monotonic
            return {
                "running": self.running,
                "logs": list(self.logs),
                "elapsed_time_seconds": elapsed,
                "exit_code": self.exit_code,
                "result_file": self.result_file,
                "active_query": self.active_query,
                "run_id": self.run_id,
            }


RUN_STATE = OptimizerRunState()


def validate_name(name):
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Profile name must not be empty.")
    name = name.strip()
    if len(name) > MAX_PROFILE_NAME_LENGTH or any(ord(char) < 32 for char in name):
        raise ValueError(f"Profile name must be at most {MAX_PROFILE_NAME_LENGTH} characters without control characters.")
    return name


def load_profile_store():
    try:
        payload = json.loads(PROFILE_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, None, {}
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise OSError(f"Could not read Optimizer profiles: {error}") from error
    try:
        if not isinstance(payload, dict) or payload.get("version") != 1:
            raise ValueError("Unsupported profile format.")
        raw_previous = payload.get("previous")
        previous = (
            None if raw_previous is None else validate_profile_options(raw_previous)
        )
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
        raw_previous_profile = payload.get("previous_profile")
        previous_profile = None
        if isinstance(raw_previous_profile, str) and previous is not None:
            previous_profile = next((
                name for name, profile in result.items()
                if name.casefold() == raw_previous_profile.casefold()
                and profile == previous
            ), None)
        elif raw_previous_profile is not None:
            raise ValueError("Invalid previous profile name.")
        return previous, previous_profile, result
    except ValueError as error:
        raise OSError(f"Could not load Optimizer profiles: {error}") from error


def load_profiles():
    return load_profile_store()[2]


def write_profile_store(previous, previous_profile, profiles):
    PROFILE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = PROFILE_FILE.with_suffix(".json.tmp")
    try:
        temporary.write_text(json.dumps({
            "version": 1,
            "previous": previous,
            "previous_profile": previous_profile,
            "profiles": profiles,
        }, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        temporary.replace(PROFILE_FILE)
    finally:
        temporary.unlink(missing_ok=True)


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
    previous, previous_profile, profiles = load_profile_store()
    return {
        "default": {
            "base_url": optimizer.BASE_URL,
            "distance_percent": optimizer.DISTANCE_PERCENT,
            "saved_url_name": None,
        },
        "previous": previous,
        "previous_profile": previous_profile,
        "profiles": profiles,
        "saved_urls": saved_urls,
        "urls_error": urls_error,
    }


def update_profile(data, *, delete=False):
    if not isinstance(data, dict):
        raise ValueError("Request body must be a JSON object.")
    name = validate_name(data.get("name"))
    with LOCK:
        previous, previous_profile, profiles = load_profile_store()
        existing = next((key for key in profiles if key.casefold() == name.casefold()), None)
        if delete:
            if existing is None:
                raise ValueError("Profile does not exist.")
            del profiles[existing]
            if previous_profile == existing:
                previous_profile = None
            profile = None
        else:
            if existing is not None:
                raise ValueError("A profile with this name already exists.")
            if len(profiles) >= MAX_SAVED_PROFILES:
                raise ValueError(f"At most {MAX_SAVED_PROFILES} profiles can be saved.")
            profile = validate_profile_options(data.get("options"))
            profiles[name] = profile
        write_profile_store(previous, previous_profile, profiles)
        return {"name": name, "profile": profile, "profiles": profiles,
                "previous_profile": previous_profile}


def save_previous_profile(data: dict[str, object]) -> tuple[dict[str, object], str | None]:
    previous = validate_profile_options(data)
    requested_name = data.get("profile_name")
    with LOCK:
        _, _, profiles = load_profile_store()
        previous_profile = None
        if isinstance(requested_name, str):
            previous_profile = next((
                name for name, profile in profiles.items()
                if name.casefold() == requested_name.casefold() and profile == previous
            ), None)
        write_profile_store(previous, previous_profile, profiles)
    return previous, previous_profile


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
            merge_report_file = folder / "merged" / "merge-report.txt"
            merge_report = (
                {"name": merge_report_file.name, "path": merge_report_file.name,
                 "size": merge_report_file.stat().st_size}
                if merge_report_file.is_file() else None
            )
            merged_distance_percent = None
            if merged:
                try:
                    merged_payload = json.loads(merged_file.read_text(encoding="utf-8"))
                    merged_distance_percent = merged_payload.get("options", {}).get(
                        "optimize_jobs_between_depots_distance"
                    )
                except (OSError, UnicodeError, json.JSONDecodeError, AttributeError):
                    pass
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
            groups.append({
                **manifest,
                "initial": initial,
                "merged": merged,
                "merge_report": merge_report,
                "merged_distance_percent": merged_distance_percent,
                "outputs": outputs,
            })
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
    merged_folder = folder.parent / "merged"
    (merged_folder / "merged-input.json").unlink(missing_ok=True)
    (merged_folder / "merge-report.txt").unlink(missing_ok=True)
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
    if (
        not file.is_relative_to(root)
        or not file.is_file()
        or file.suffix.lower() not in {".json", ".txt"}
    ):
        raise ValueError("Optimizer result file does not exist.")
    return file


def merge_group(data: object) -> dict[str, object]:
    if not isinstance(data, dict):
        raise ValueError("Request body must be a JSON object.")
    distance = optimizer.validate_options({
        "base_url": data.get("base_url"),
        "distance_percent": data.get("distance_percent"),
    })["distance_percent"]
    with GROUP_LOCK:
        folder = group_folder(data.get("group_id"))
        initial_files = sorted(
            (file for file in (folder / "initial").glob("*-input.json") if file.is_file()),
            key=lambda file: file.name.casefold(),
        )
        merged_folder = folder / "merged"
        merged_file = merged_folder / "merged-input.json"
        report_file = merged_folder / "merge-report.txt"
        merged_file.unlink(missing_ok=True)
        documents = []
        try:
            for file in initial_files:
                document = json.loads(file.read_text(encoding="utf-8"))
                if not isinstance(document, dict):
                    raise ValueError(f"'{file.name}' must contain a JSON object.")
                documents.append((file.name, document))
            merged, report, warning_count, error_count = optimizer.merge_inputs(
                documents, distance
            )
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
            merged = None
            warning_count = 0
            error_count = 1
            report = f"Optimizer input merge report\n\nMerge failed\n\n{error}\n"

        report_file.write_text(report, encoding="utf-8")
        if merged is not None:
            temporary = merged_file.with_suffix(".json.tmp")
            try:
                temporary.write_text(
                    json.dumps(merged, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
                temporary.replace(merged_file)
            finally:
                temporary.unlink(missing_ok=True)
        return {
            **optimizer_groups(),
            "merge_status": "done" if merged is not None else "failed",
            "warning_count": warning_count,
            "error_count": error_count,
            "report": report,
            "report_file": {
                "group_id": folder.name,
                "category": "merged",
                "path": report_file.name,
            },
        }


def delete_files(data: object) -> dict[str, object]:
    if not isinstance(data, dict) or not isinstance(data.get("files"), list) or not data["files"]:
        raise ValueError("Select one or more files to delete.")
    files = []
    deleted_initial = False
    for item in data["files"]:
        if not isinstance(item, dict):
            raise ValueError("Invalid file selection.")
        files.append(safe_category_file(
            data.get("group_id"), item.get("category"), item.get("path")
        ))
        deleted_initial = deleted_initial or item.get("category") == "initial"
    for file in files:
        file.unlink()
    if deleted_initial:
        merged_folder = group_folder(data.get("group_id")) / "merged"
        (merged_folder / "merged-input.json").unlink(missing_ok=True)
        (merged_folder / "merge-report.txt").unlink(missing_ok=True)
    return optimizer_groups()


def optimizer_map(group_id: object, output_path: object) -> dict[str, object]:
    folder = group_folder(group_id)
    if not isinstance(output_path, str) or not output_path:
        raise ValueError("Select an Optimizer output JSON.")
    merged_file = folder / "merged" / "merged-input.json"
    output_file = safe_category_file(folder.name, "outputs", output_path)
    if output_file.suffix.lower() != ".json":
        raise ValueError("Optimizer map output must be a JSON file.")
    if not merged_file.is_file():
        raise ValueError("The selected group has no merged input JSON.")
    try:
        merged = json.loads(merged_file.read_text(encoding="utf-8"))
        output = json.loads(output_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"Could not read Optimizer map data: {error}") from error
    manifest = read_group_manifest(folder)
    return {
        **optimizer.build_assignment_map(merged, output),
        "group_id": folder.name,
        "group_name": manifest["name"],
        "output_path": output_path,
    }


GET_PATHS = frozenset({
    "/api/optimizer/config", "/api/optimizer/groups", "/api/optimizer/file",
    "/api/optimizer/status", "/api/optimizer/map",
})
POST_PATHS = frozenset({
    "/api/optimizer/profile/save", "/api/optimizer/profile/delete",
    "/api/optimizer/group/create", "/api/optimizer/group/select",
    "/api/optimizer/group/delete", "/api/optimizer/files/add",
    "/api/optimizer/files/delete", "/api/optimizer/merge", "/api/optimizer/start",
})


def handle_get(request) -> None:
    parsed = urlparse(request.path)
    if parsed.path == "/api/optimizer/config":
        request.send_json(config())
    elif parsed.path == "/api/optimizer/groups":
        request.send_json(optimizer_groups())
    elif parsed.path == "/api/optimizer/status":
        request.send_json(RUN_STATE.snapshot())
    elif parsed.path == "/api/optimizer/map":
        try:
            query = parse_qs(parsed.query)
            request.send_json(optimizer_map(
                query.get("group", [None])[0], query.get("output", [None])[0]
            ))
        except (ValueError, UnicodeError) as error:
            request.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
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
            if file.suffix.lower() == ".json":
                content = json.dumps(json.loads(content), indent=2, ensure_ascii=False)
            request.send_json({"name": file.name, "size": file.stat().st_size,
                               "content": content})
        except (ValueError, UnicodeError) as error:
            request.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)


def handle_post(request_path: str, data: object) -> tuple[object, int]:
    if request_path not in POST_PATHS:
        raise ValueError("Unknown Optimizer action.")
    if request_path == "/api/optimizer/start":
        return RUN_STATE.start(data), HTTPStatus.ACCEPTED
    if request_path.startswith("/api/optimizer/profile/"):
        return update_profile(data, delete=request_path.endswith("/delete")), HTTPStatus.OK
    with RUN_STATE.lock:
        if RUN_STATE.running:
            raise RuntimeError(
                "Wait for the current Optimizer run to finish before changing its group."
            )
    actions = {
        "/api/optimizer/group/create": create_group,
        "/api/optimizer/group/select": select_group,
        "/api/optimizer/group/delete": delete_group,
        "/api/optimizer/files/add": add_initial_files,
        "/api/optimizer/files/delete": delete_files,
        "/api/optimizer/merge": merge_group,
    }
    return actions[request_path](data), HTTPStatus.OK
