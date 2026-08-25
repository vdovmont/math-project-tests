#!/usr/bin/env python3
"""Serve a local browser interface for json_http_tester.py."""

from __future__ import annotations

import argparse
import json
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

import json_http_tester as tester


GUI_URL = "http://0.0.0.0:9010"
SCRIPT_FOLDER = Path(__file__).resolve().parent
TESTER_SCRIPT = SCRIPT_FOLDER / "json_http_tester.py"
HTML_FILE = SCRIPT_FOLDER / "json_http_tester_gui.html"
MAX_REQUEST_BODY_BYTES = 64 * 1024
MAX_VIEW_FILE_BYTES = 20 * 1024 * 1024
MAX_UPLOAD_FILE_BYTES = 20 * 1024 * 1024
MAX_UPLOAD_REQUEST_BODY_BYTES = (MAX_UPLOAD_FILE_BYTES * 4) + (1024 * 1024)
DELETE_PREVIEW_TTL_SECONDS = 300
EXPECTED_FINALIZATION_SECONDS = 10.0
DISPLAY_DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"
FILE_ROOTS = {
    "json-data": ("JSON data", tester.JSON_DATA_FOLDER),
    "tests": ("Tests", tester.TESTS_FOLDER),
}
DELETE_PREVIEWS: dict[str, dict[str, object]] = {}
DELETE_PREVIEW_LOCK = threading.Lock()


class RunState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.running = False
        self.logs: list[str] = []
        self.exit_code: int | None = None
        self.started_at: str | None = None
        self.finished_at: str | None = None
        self.overall_status: str | None = None
        self.last_result_path: str | None = None
        self.progress_active = False
        self.progress_console_width = 0
        self.expected_time_seconds: float | None = None
        self.started_monotonic: float | None = None
        self.elapsed_time_seconds: float | None = None
        self.process: subprocess.Popen[bytes] | None = None
        self.active_query: int | None = None
        self.run_folder: Path | None = None
        self.run_options: dict[str, object] | None = None
        self.stopping = False
        self.stopped = False
        self.stop_requested = False
        self.worker_finished = threading.Event()
        self.run_id = 0

    def start(self, options: dict[str, object]) -> None:
        with self.lock:
            if self.running:
                raise RuntimeError("A test run is already in progress.")
            self.expected_time_seconds = calculate_expected_time(
                Path(str(options["json_data_folder"]))
            )
            self.running = True
            self.logs = []
            self.exit_code = None
            self.started_at = datetime.now().strftime(DISPLAY_DATETIME_FORMAT)
            self.started_monotonic = time.monotonic()
            self.elapsed_time_seconds = 0.0
            self.finished_at = None
            self.overall_status = None
            self.last_result_path = None
            self.progress_active = False
            self.progress_console_width = 0
            self.process = None
            self.active_query = None
            self.run_folder = None
            self.run_options = dict(options)
            self.stopping = False
            self.stopped = False
            self.stop_requested = False
            self.worker_finished.clear()
            self.run_id += 1

        worker = threading.Thread(
            target=self._run_tester,
            args=(options,),
            daemon=True,
        )
        worker.start()

    def _append_log(self, line: str) -> None:
        with self.lock:
            replaces_progress = self.progress_active and bool(self.logs)
            if replaces_progress:
                self.logs[-1] = line
                self.progress_active = False
            else:
                self.logs.append(line)
            query_match = re.fullmatch(r"Query number:\s*(\d+)", line)
            if query_match is not None:
                self.active_query = int(query_match.group(1))
            elif line.startswith("Saved result: "):
                self.active_query = None
            folder_prefix = "Created test-run folder: "
            if line.startswith(folder_prefix):
                displayed_folder = Path(line.removeprefix(folder_prefix))
                if not displayed_folder.is_absolute():
                    displayed_folder = Path.cwd() / displayed_folder
                self.run_folder = displayed_folder.resolve()
        if replaces_progress:
            print(f"\r{line.ljust(self.progress_console_width)}", flush=True)
            self.progress_console_width = 0
        else:
            print(line, flush=True)

    def _update_progress(self, line: str) -> None:
        if not line.strip():
            return
        with self.lock:
            if self.progress_active and self.logs:
                self.logs[-1] = line
            else:
                self.logs.append(line)
                self.progress_active = True
        self.progress_console_width = max(self.progress_console_width, len(line))
        print(f"\r{line.ljust(self.progress_console_width)}", end="", flush=True)

    def _capture_process_output(self, stream: object) -> None:
        pending = bytearray()

        def consume(final: bool = False) -> None:
            while True:
                carriage = pending.find(b"\r")
                newline = pending.find(b"\n")
                delimiters = [index for index in (carriage, newline) if index >= 0]
                if not delimiters:
                    break
                index = min(delimiters)
                delimiter = pending[index]
                if delimiter == 13 and index + 1 == len(pending) and not final:
                    break

                text = bytes(pending[:index]).decode("utf-8", errors="replace")
                if delimiter == 13 and index + 1 < len(pending) and pending[index + 1] == 10:
                    del pending[: index + 2]
                    self._append_log(text)
                elif delimiter == 10:
                    del pending[: index + 1]
                    self._append_log(text)
                else:
                    del pending[: index + 1]
                    self._update_progress(text)

        while True:
            chunk = stream.read(1024)  # type: ignore[attr-defined]
            if not chunk:
                break
            pending.extend(chunk)
            consume()
        consume(final=True)
        if pending:
            self._append_log(bytes(pending).decode("utf-8", errors="replace"))

    def _run_tester(self, options: dict[str, object]) -> None:
        command = [
            sys.executable,
            "-u",
            str(TESTER_SCRIPT),
            "--base-url",
            str(options["base_url"]),
            "--json-data-folder",
            str(options["json_data_folder"]),
            "--tests-folder",
            str(options["tests_folder"]),
            "--timeout",
            str(options["timeout"]),
            "--poll-interval",
            str(options["poll_interval"]),
        ]
        display_command = [
            Path(sys.executable).name,
            "-u",
            tester.display_path(TESTER_SCRIPT),
            "--base-url",
            str(options["base_url"]),
            "--json-data-folder",
            tester.display_path(str(options["json_data_folder"])),
            "--tests-folder",
            tester.display_path(str(options["tests_folder"])),
            "--timeout",
            str(options["timeout"]),
            "--poll-interval",
            str(options["poll_interval"]),
        ]
        self._append_log("> " + subprocess.list2cmdline(display_command))

        exit_code = 1
        try:
            process = subprocess.Popen(
                command,
                cwd=Path.cwd(),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=False,
                bufsize=0,
            )
            with self.lock:
                self.process = process
            if process.stdout is None:
                raise RuntimeError("Could not capture tester output.")
            self._capture_process_output(process.stdout)
            exit_code = process.wait()
        except OSError as error:
            self._append_log(f"Could not start tester: {error}")
        except Exception as error:  # Keep the GUI state consistent on worker failure.
            self._append_log(f"Unexpected runner error: {error}")
        finally:
            overall_status = None
            relative_report_path = None
            report_path = None
            report_prefix = "Saved comparison report: "
            with self.lock:
                for line in reversed(self.logs):
                    if line.startswith(report_prefix):
                        report_path = Path(line.removeprefix(report_prefix))
                        break
            with self.lock:
                stop_requested = self.stop_requested
            if report_path is not None and not stop_requested:
                try:
                    overall_status, relative_report_path = read_result_summary(
                        report_path, Path(str(options["tests_folder"]))
                    )
                    self._append_log(f"Overall test result: {overall_status}")
                except (OSError, ValueError) as error:
                    self._append_log(f"Could not read overall test result: {error}")
            if stop_requested:
                self._append_log(f"Tester stopped with exit code {exit_code}.")
            else:
                self._append_log(f"Tester finished with exit code {exit_code}.")
            with self.lock:
                if self.started_monotonic is not None:
                    self.elapsed_time_seconds = (
                        time.monotonic() - self.started_monotonic
                    )
                self.running = False
                self.process = None
                self.active_query = None
                self.stopped = stop_requested
                self.exit_code = exit_code
                self.finished_at = datetime.now().strftime(DISPLAY_DATETIME_FORMAT)
                self.overall_status = overall_status
                if relative_report_path is not None:
                    self.last_result_path = relative_report_path
            self.worker_finished.set()

    def stop(self) -> dict[str, object]:
        with self.lock:
            if not self.running:
                raise RuntimeError("No test run is currently running.")
            if self.stopping:
                raise RuntimeError("The test run is already stopping.")
            if self.active_query is None:
                raise RuntimeError(
                    "The current calculation query is not available yet; try again shortly."
                )
            if self.run_folder is None or self.run_options is None:
                raise RuntimeError("The current test-run folder is not available.")

            query = self.active_query
            process = self.process
            run_folder = self.run_folder
            options = dict(self.run_options)
            tests_root = Path(str(options["tests_folder"]))
            if not tests_root.is_absolute():
                tests_root = Path.cwd() / tests_root
            tests_root = tests_root.resolve()
            if run_folder == tests_root or not path_is_within(run_folder, tests_root):
                raise RuntimeError("Refusing to remove an invalid test-run folder.")
            self.stopping = True

        stop_url = (
            f"{tester.build_url(str(options['base_url']), tester.STOP_REQUEST)}?"
            f"{urlencode({'queue': query})}"
        )
        request = Request(stop_url, headers={"Accept": "application/json"}, method="GET")
        try:
            with urlopen(request, timeout=float(options["timeout"])) as response:
                response.read()
        except HTTPError as error:
            with self.lock:
                self.stopping = False
            raise RuntimeError(
                f"Stop request failed with HTTP {error.code}: {error.reason}"
            ) from error
        except (URLError, TimeoutError, OSError) as error:
            with self.lock:
                self.stopping = False
            raise RuntimeError(f"Stop request failed: {error}") from error

        with self.lock:
            self.stop_requested = True

        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        self.worker_finished.wait(timeout=5)

        try:
            if run_folder.is_dir():
                shutil.rmtree(run_folder)
        except OSError as error:
            with self.lock:
                self.stopping = False
                self.stopped = True
            raise RuntimeError(
                f"The calculation stopped, but its test folder could not be deleted: {error}"
            ) from error
        self._append_log(f"Stopped query {query}.")
        self._append_log(
            f"Deleted test-run folder: {tester.display_path(run_folder)}"
        )
        with self.lock:
            self.stopping = False
            self.stopped = True
            self.overall_status = None
        return self.snapshot(0)

    def snapshot(self, after: int) -> dict[str, object]:
        with self.lock:
            elapsed_time_seconds = self.elapsed_time_seconds
            if self.running and self.started_monotonic is not None:
                elapsed_time_seconds = time.monotonic() - self.started_monotonic
            return {
                "running": self.running,
                "logs": list(self.logs),
                "next": len(self.logs),
                "exit_code": self.exit_code,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "overall_status": self.overall_status,
                "result_path": self.last_result_path,
                "expected_time_seconds": self.expected_time_seconds,
                "elapsed_time_seconds": elapsed_time_seconds,
                "stopping": self.stopping,
                "stopped": self.stopped,
                "active_query": self.active_query,
                "run_id": self.run_id,
            }


RUN_STATE = RunState()


def calculate_expected_time(json_data_folder: Path) -> float:
    """Sum expected calculation times and allow ten seconds for finalization."""
    total = EXPECTED_FINALIZATION_SECONDS
    for output_file in sorted(json_data_folder.glob("*-output.json")):
        try:
            with output_file.open("r", encoding="utf-8") as file:
                metrics = tester.extract_result_metrics(json.load(file))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            raise ValueError(
                f"Could not read expected calculation time from "
                f"'{output_file.name}': {error}"
            ) from error
        total += float(metrics["calculation_time"])
    return total


def validate_uploaded_json_file(
    item: object, label: str, required_suffix: str
) -> tuple[str, str, str]:
    if not isinstance(item, dict):
        raise ValueError(f"Select a {label} JSON file.")
    name = item.get("name")
    content = item.get("content")
    if not isinstance(name, str) or not name:
        raise ValueError(f"The {label} filename is missing.")
    if Path(name).name != name or "/" in name or "\\" in name:
        raise ValueError(f"The {label} filename is invalid.")
    if not name.endswith(required_suffix):
        raise ValueError(
            f"The {label} file must end with '{required_suffix}'."
        )
    if not isinstance(content, str):
        raise ValueError(f"The {label} file content is invalid.")
    if len(content.encode("utf-8")) > MAX_UPLOAD_FILE_BYTES:
        raise ValueError(
            f"The {label} file exceeds the {MAX_UPLOAD_FILE_BYTES // (1024 * 1024)} MB limit."
        )
    try:
        json.loads(content)
    except json.JSONDecodeError as error:
        raise ValueError(
            f"The {label} file is not valid JSON: {error.msg} "
            f"(line {error.lineno}, column {error.colno})."
        ) from error
    return name, content, name.removesuffix(required_suffix)


def add_test_type(data: object) -> dict[str, object]:
    if not isinstance(data, dict):
        raise ValueError("Request body must be a JSON object.")
    input_name, input_content, input_base = validate_uploaded_json_file(
        data.get("input"), "input", "-input.json"
    )
    output_name, output_content, output_base = validate_uploaded_json_file(
        data.get("output"), "output", "-output.json"
    )
    if not input_base or input_base != output_base:
        raise ValueError(
            "Input and output filenames must have the same name before their suffixes."
        )

    _, json_root = get_file_root("json-data")
    json_root.mkdir(parents=True, exist_ok=True)
    uploads = (
        (json_root / input_name, input_content),
        (json_root / output_name, output_content),
    )
    for target, _ in uploads:
        if target.exists():
            raise ValueError(f"File already exists: {tester.display_path(target)}")

    created_files: list[Path] = []
    try:
        for target, content in uploads:
            with target.open("x", encoding="utf-8", newline="") as file:
                file.write(content)
            created_files.append(target)
    except OSError:
        for created_file in created_files:
            try:
                created_file.unlink()
            except OSError:
                pass
        raise

    return {
        "test_type": input_base,
        "files": [tester.display_path(target) for target, _ in uploads],
    }


def validate_options(data: object) -> dict[str, object]:
    if not isinstance(data, dict):
        raise ValueError("Request body must be a JSON object.")

    base_url = data.get("base_url")
    if not isinstance(base_url, str) or not base_url.strip():
        raise ValueError("Base URL must not be empty.")

    parsed_url = urlparse(base_url.strip())
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        raise ValueError("Base URL must be a valid HTTP or HTTPS URL.")

    return {
        "base_url": base_url.strip(),
        "json_data_folder": str(tester.JSON_DATA_FOLDER),
        "tests_folder": str(tester.TESTS_FOLDER),
        "timeout": tester.REQUEST_TIMEOUT_SECONDS,
        "poll_interval": tester.STATE_POLL_INTERVAL_SECONDS,
    }


def get_file_root(root_name: str) -> tuple[str, Path]:
    try:
        label, configured_path = FILE_ROOTS[root_name]
    except KeyError as error:
        raise ValueError("Unknown folder root.") from error

    root = Path(configured_path)
    if not root.is_absolute():
        root = Path.cwd() / root
    return label, root.resolve()


def resolve_file_browser_path(root_name: str, relative_path: str) -> tuple[str, Path, Path]:
    label, root = get_file_root(root_name)
    candidate = (root / relative_path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise ValueError("Path is outside the configured folder.") from error
    return label, root, candidate


def list_folder(root_name: str, relative_path: str) -> dict[str, object]:
    label, root, folder = resolve_file_browser_path(root_name, relative_path)
    if not folder.is_dir():
        raise FileNotFoundError(
            f"Folder does not exist: {tester.display_path(folder)}"
        )

    items = []
    for item in sorted(folder.iterdir(), key=lambda path: (not path.is_dir(), path.name.casefold())):
        resolved_item = item.resolve()
        try:
            resolved_item.relative_to(root)
        except ValueError:
            continue
        stat = item.stat()
        items.append(
            {
                "name": item.name,
                "path": resolved_item.relative_to(root).as_posix(),
                "is_directory": item.is_dir(),
                "size": None if item.is_dir() else stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime).strftime(
                    DISPLAY_DATETIME_FORMAT
                ),
            }
        )

    current_path = folder.relative_to(root).as_posix()
    if current_path == ".":
        current_path = ""
    return {
        "root": root_name,
        "label": label,
        "root_path": tester.display_path(root),
        "path": current_path,
        "items": items,
    }


def read_viewable_file(root_name: str, relative_path: str) -> dict[str, object]:
    label, root, file_path = resolve_file_browser_path(root_name, relative_path)
    if not file_path.is_file():
        raise FileNotFoundError(
            f"File does not exist: {tester.display_path(file_path)}"
        )

    size = file_path.stat().st_size
    if size > MAX_VIEW_FILE_BYTES:
        raise ValueError(
            f"File is too large to display ({size} bytes; limit is "
            f"{MAX_VIEW_FILE_BYTES} bytes)."
        )

    raw_content = file_path.read_bytes()
    content = raw_content.decode("utf-8", errors="replace")
    if file_path.suffix.casefold() == ".json":
        try:
            content = json.dumps(json.loads(content), indent=2, ensure_ascii=False)
        except json.JSONDecodeError:
            pass

    return {
        "root": root_name,
        "label": label,
        "root_path": tester.display_path(root),
        "path": file_path.relative_to(root).as_posix(),
        "size": size,
        "content": content,
    }


def build_json_file_groups() -> dict[str, object]:
    _, json_root = get_file_root("json-data")
    _, tests_root = get_file_root("tests")
    if not json_root.is_dir():
        raise FileNotFoundError(
            f"Folder does not exist: {tester.display_path(json_root)}"
        )
    if not tests_root.is_dir():
        raise FileNotFoundError(
            f"Folder does not exist: {tester.display_path(tests_root)}"
        )

    grouped: dict[str, dict[str, object]] = {}

    def group_for(base_name: str) -> dict[str, object]:
        return grouped.setdefault(
            base_name,
            {"name": base_name, "input": None, "output": None, "tests": []},
        )

    for suffix, item_kind in (("-input.json", "input"), ("-output.json", "output")):
        for discovered_file in json_root.glob(f"*{suffix}"):
            file_path = discovered_file.resolve()
            if not path_is_within(file_path, json_root):
                continue
            base_name = file_path.name.removesuffix(suffix)
            group_for(base_name)[item_kind] = {
                "root": "json-data",
                "path": file_path.relative_to(json_root).as_posix(),
                "label": "Input" if item_kind == "input" else "Expected output",
                "size": file_path.stat().st_size,
            }

    for discovered_file in tests_root.rglob("*-test.json"):
        file_path = discovered_file.resolve()
        if not path_is_within(file_path, tests_root):
            continue
        base_name = file_path.name.removesuffix("-test.json")
        relative_path = file_path.relative_to(tests_root)
        test_item = {
            "root": "tests",
            "path": relative_path.as_posix(),
            "label": f"Test · {relative_path.parent.as_posix()}",
            "sort_name": relative_path.parent.as_posix(),
            "size": file_path.stat().st_size,
        }
        tests = group_for(base_name)["tests"]
        if isinstance(tests, list):
            tests.append(test_item)

    results = []
    for discovered_file in tests_root.rglob("result.txt"):
        file_path = discovered_file.resolve()
        if not path_is_within(file_path, tests_root):
            continue
        relative_path = file_path.relative_to(tests_root)
        results.append(
            {
                "root": "tests",
                "path": relative_path.as_posix(),
                "label": relative_path.parent.as_posix(),
                "run_folder": relative_path.parent.as_posix(),
                "size": file_path.stat().st_size,
            }
        )
    results.sort(key=lambda item: str(item["label"]).casefold(), reverse=True)

    groups = []
    for base_name in sorted(grouped, key=str.casefold):
        group = grouped[base_name]
        tests = group["tests"]
        if isinstance(tests, list):
            tests.sort(
                key=lambda item: str(item.get("sort_name", "")).casefold(),
                reverse=True,
            )
            for item in tests:
                item.pop("sort_name", None)
        groups.append(group)
    return {"results": results, "groups": groups}


def load_json_file(file_path: Path) -> object:
    with file_path.open("r", encoding="utf-8") as file:
        return json.load(file)


def normalize_identifier(value: object) -> str:
    text = str(value).strip()
    try:
        return str(int(text))
    except ValueError:
        return text


def path_is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def read_result_summary(report_path: Path, configured_tests_folder: Path) -> tuple[str, str]:
    tests_root = configured_tests_folder
    if not tests_root.is_absolute():
        tests_root = Path.cwd() / tests_root
    tests_root = tests_root.resolve()

    resolved_report = report_path
    if not resolved_report.is_absolute():
        resolved_report = Path.cwd() / resolved_report
    resolved_report = resolved_report.resolve()
    if not path_is_within(resolved_report, tests_root):
        raise ValueError("Result file is outside TESTS_FOLDER.")

    with resolved_report.open("r", encoding="utf-8") as file:
        first_line = file.readline().strip()
    match = re.fullmatch(r"Overall test results?:\s*(PASSED|FAILED)", first_line)
    if match is None:
        raise ValueError("result.txt has no valid overall status on its first line.")
    return match.group(1), resolved_report.relative_to(tests_root).as_posix()


def deletion_targets_for_test_types(base_names: list[str]) -> list[dict[str, str]]:
    _, json_root = get_file_root("json-data")
    _, tests_root = get_file_root("tests")
    available_groups = {
        str(group["name"]) for group in build_json_file_groups()["groups"]
    }
    unknown_names = sorted(set(base_names) - available_groups)
    if unknown_names:
        raise ValueError(f"Unknown test type(s): {', '.join(unknown_names)}")

    targets: list[dict[str, str]] = []
    selected_names = set(base_names)
    for base_name in selected_names:
        for suffix in ("-input.json", "-output.json"):
            candidate = (json_root / f"{base_name}{suffix}").resolve()
            if candidate.is_file() and path_is_within(candidate, json_root):
                targets.append(
                    {
                        "kind": "file",
                        "root": "json-data",
                        "path": candidate.relative_to(json_root).as_posix(),
                    }
                )

    expected_test_names = {f"{base_name}-test.json" for base_name in selected_names}
    for discovered_file in tests_root.rglob("*-test.json"):
        candidate = discovered_file.resolve()
        if (
            candidate.name in expected_test_names
            and candidate.is_file()
            and path_is_within(candidate, tests_root)
        ):
            targets.append(
                {
                    "kind": "file",
                    "root": "tests",
                    "path": candidate.relative_to(tests_root).as_posix(),
                }
            )
    return targets


def deletion_targets_for_test_results(run_folders: list[str]) -> list[dict[str, str]]:
    _, tests_root = get_file_root("tests")
    targets = []
    for relative_folder in sorted(set(run_folders)):
        if not relative_folder or relative_folder == ".":
            raise ValueError("A test result folder path is empty.")
        candidate = (tests_root / relative_folder).resolve()
        if (
            candidate == tests_root
            or not path_is_within(candidate, tests_root)
            or not candidate.is_dir()
        ):
            raise ValueError(f"Invalid test result folder: {relative_folder}")
        if not (candidate / "result.txt").is_file():
            raise ValueError(
                f"Test result folder has no result.txt: {relative_folder}"
            )
        targets.append(
            {
                "kind": "folder",
                "root": "tests",
                "path": candidate.relative_to(tests_root).as_posix(),
            }
        )
    return targets


def make_deletion_preview(mode: str, items: object) -> dict[str, object]:
    if not isinstance(items, list) or not items or not all(
        isinstance(item, str) and item.strip() for item in items
    ):
        raise ValueError("Select at least one item to delete.")
    selected_items = [str(item).strip() for item in items]

    if mode == "test-types":
        targets = deletion_targets_for_test_types(selected_items)
    elif mode == "test-results":
        targets = deletion_targets_for_test_results(selected_items)
    else:
        raise ValueError("Unknown deletion mode.")
    if not targets:
        raise ValueError("The selected items contain no deletable files.")

    display_paths = []
    for target in targets:
        _, root = get_file_root(target["root"])
        display_paths.append(tester.display_path(root / target["path"]))
        if target["kind"] == "folder":
            folder = (root / target["path"]).resolve()
            for child in sorted(path for path in folder.rglob("*") if path.is_file()):
                display_paths.append(tester.display_path(child))

    token = secrets.token_urlsafe(24)
    with DELETE_PREVIEW_LOCK:
        current_time = time.monotonic()
        expired_tokens = [
            key
            for key, preview in DELETE_PREVIEWS.items()
            if float(preview["expires_at"]) <= current_time
        ]
        for expired_token in expired_tokens:
            DELETE_PREVIEWS.pop(expired_token, None)
        DELETE_PREVIEWS[token] = {
            "expires_at": current_time + DELETE_PREVIEW_TTL_SECONDS,
            "targets": targets,
        }
    return {"token": token, "paths": display_paths}


def execute_deletion(token: object) -> list[str]:
    if not isinstance(token, str) or not token:
        raise ValueError("Deletion confirmation token is missing.")
    with DELETE_PREVIEW_LOCK:
        preview = DELETE_PREVIEWS.pop(token, None)
    if preview is None or float(preview["expires_at"]) <= time.monotonic():
        raise ValueError("Deletion preview has expired; preview the items again.")

    targets = preview["targets"]
    if not isinstance(targets, list):
        raise ValueError("Deletion preview is invalid.")
    deleted = []
    for target in targets:
        if not isinstance(target, dict):
            raise ValueError("Deletion target is invalid.")
        root_name = str(target.get("root", ""))
        target_kind = str(target.get("kind", ""))
        relative_path = str(target.get("path", ""))
        _, root = get_file_root(root_name)
        candidate = (root / relative_path).resolve()
        if candidate == root or not path_is_within(candidate, root):
            raise ValueError("Deletion target is outside its configured folder.")
        if target_kind == "file":
            if candidate.is_file():
                candidate.unlink()
                deleted.append(tester.display_path(candidate))
        elif target_kind == "folder":
            if candidate.is_dir():
                shutil.rmtree(candidate)
                deleted.append(tester.display_path(candidate))
        else:
            raise ValueError("Deletion target type is invalid.")
    return deleted


def get_input_coordinates(input_data: object) -> dict[str, tuple[float, float]]:
    if not isinstance(input_data, dict) or not isinstance(input_data.get("points"), list):
        raise ValueError("Input JSON has no 'points' array.")

    coordinates: dict[str, tuple[float, float]] = {}
    for point in input_data["points"]:
        if not isinstance(point, dict):
            continue
        point_id = point.get("id")
        latitude = point.get("lat")
        longitude = point.get("lon")
        if (
            point_id is None
            or not isinstance(latitude, (int, float))
            or isinstance(latitude, bool)
            or not isinstance(longitude, (int, float))
            or isinstance(longitude, bool)
        ):
            continue
        coordinates[normalize_identifier(point_id)] = (
            float(latitude),
            float(longitude),
        )
    if not coordinates:
        raise ValueError("Input JSON has no usable point coordinates.")
    return coordinates


def extract_map_routes(
    result_data: object,
    coordinates: dict[str, tuple[float, float]],
) -> list[dict[str, object]]:
    if not isinstance(result_data, dict) or not isinstance(
        result_data.get("solutions"), list
    ):
        raise ValueError("Result JSON has no 'solutions' array.")

    map_routes = []
    file_sequence = 1
    for solution_index, solution in enumerate(result_data["solutions"]):
        if not isinstance(solution, dict) or not isinstance(solution.get("routes"), list):
            continue
        for route_index, route in enumerate(solution["routes"]):
            if not isinstance(route, dict) or not isinstance(route.get("deliveries"), list):
                continue

            route_points = []
            missing_points = 0
            for sequence, delivery in enumerate(route["deliveries"]):
                if not isinstance(delivery, dict):
                    continue
                point_order = file_sequence
                file_sequence += 1
                point_id = normalize_identifier(delivery.get("pointId", ""))
                coordinate = coordinates.get(point_id)
                if coordinate is None:
                    missing_points += 1
                    continue
                route_points.append(
                    {
                        "lat": coordinate[0],
                        "lon": coordinate[1],
                        "sequence": sequence,
                        "order": point_order,
                        "point_id": delivery.get("pointId"),
                        "job_id": delivery.get("jobID"),
                        "arrival": delivery.get("arrival"),
                    }
                )

            transport_id = str(route.get("transportID", "unknown"))
            map_routes.append(
                {
                    "id": f"s{solution_index}-r{route_index}",
                    "label": f"Route {route_index + 1} · transport {transport_id}",
                    "transport_id": transport_id,
                    "total_distance": route.get("totalDistance"),
                    "value": route.get("value"),
                    "missing_points": missing_points,
                    "points": route_points,
                }
            )

    return map_routes


def make_route_source(
    file_path: Path,
    source_id: str,
    label: str,
    kind: str,
    coordinates: dict[str, tuple[float, float]],
    display_root: Path,
) -> dict[str, object]:
    source: dict[str, object] = {
        "id": source_id,
        "label": label,
        "kind": kind,
        "file": file_path.relative_to(display_root).as_posix(),
    }
    try:
        source["routes"] = extract_map_routes(load_json_file(file_path), coordinates)
    except (OSError, json.JSONDecodeError, ValueError) as error:
        source["routes"] = []
        source["error"] = str(error)
    return source


def build_route_groups() -> dict[str, object]:
    _, json_root = get_file_root("json-data")
    _, tests_root = get_file_root("tests")
    if not json_root.is_dir():
        raise FileNotFoundError(
            f"Folder does not exist: {tester.display_path(json_root)}"
        )
    if not tests_root.is_dir():
        raise FileNotFoundError(
            f"Folder does not exist: {tester.display_path(tests_root)}"
        )

    test_files_by_name: dict[str, list[Path]] = {}
    for discovered_file in tests_root.rglob("*-test.json"):
        test_file = discovered_file.resolve()
        if not path_is_within(test_file, tests_root):
            continue
        base_name = test_file.name.removesuffix("-test.json")
        test_files_by_name.setdefault(base_name, []).append(test_file)

    groups = []
    for discovered_file in sorted(json_root.glob("*-input.json")):
        input_file = discovered_file.resolve()
        if not path_is_within(input_file, json_root):
            continue
        base_name = input_file.name.removesuffix("-input.json")
        group: dict[str, object] = {
            "name": base_name,
            "input_file": input_file.name,
            "sources": [],
        }
        try:
            coordinates = get_input_coordinates(load_json_file(input_file))
        except (OSError, json.JSONDecodeError, ValueError) as error:
            group["error"] = str(error)
            groups.append(group)
            continue

        sources: list[dict[str, object]] = []
        expected_file = (json_root / f"{base_name}-output.json").resolve()
        if expected_file.is_file() and path_is_within(expected_file, json_root):
            sources.append(
                make_route_source(
                    expected_file,
                    f"{base_name}:output",
                    "Expected output",
                    "output",
                    coordinates,
                    json_root,
                )
            )

        for test_file in sorted(test_files_by_name.get(base_name, []), reverse=True):
            relative_test = test_file.relative_to(tests_root).as_posix()
            sources.append(
                make_route_source(
                    test_file,
                    f"{base_name}:test:{relative_test}",
                    f"Test · {test_file.parent.name}",
                    "test",
                    coordinates,
                    tests_root,
                )
            )

        group["sources"] = sources
        groups.append(group)

    return {"groups": groups}


class GuiRequestHandler(BaseHTTPRequestHandler):
    server_version = "MathCoreTestGUI/1.0"

    def log_message(self, format_string: str, *args: object) -> None:
        # Status polling is frequent; routine access lines only obscure tester logs.
        return

    def send_bytes(self, body: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, data: object, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_bytes(body, "application/json; charset=utf-8", status)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            try:
                body = HTML_FILE.read_bytes()
            except OSError as error:
                self.send_json({"error": str(error)}, HTTPStatus.INTERNAL_SERVER_ERROR)
                return
            self.send_bytes(body, "text/html; charset=utf-8")
            return

        if parsed.path == "/api/config":
            self.send_json(
                {
                    "base_url": tester.BASE_URL,
                }
            )
            return

        if parsed.path in {"/api/files", "/api/file"}:
            query = parse_qs(parsed.query)
            root_name = query.get("root", [""])[0]
            relative_path = query.get("path", [""])[0]
            try:
                if parsed.path == "/api/files":
                    result = list_folder(root_name, relative_path)
                else:
                    result = read_viewable_file(root_name, relative_path)
            except ValueError as error:
                self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
                return
            except FileNotFoundError as error:
                self.send_json({"error": str(error)}, HTTPStatus.NOT_FOUND)
                return
            except OSError as error:
                self.send_json(
                    {"error": str(error)}, HTTPStatus.INTERNAL_SERVER_ERROR
                )
                return
            self.send_json(result)
            return

        if parsed.path == "/api/routes":
            try:
                result = build_route_groups()
            except FileNotFoundError as error:
                self.send_json({"error": str(error)}, HTTPStatus.NOT_FOUND)
                return
            except OSError as error:
                self.send_json(
                    {"error": str(error)}, HTTPStatus.INTERNAL_SERVER_ERROR
                )
                return
            self.send_json(result)
            return

        if parsed.path == "/api/json-files":
            try:
                result = build_json_file_groups()
            except FileNotFoundError as error:
                self.send_json({"error": str(error)}, HTTPStatus.NOT_FOUND)
                return
            except OSError as error:
                self.send_json(
                    {"error": str(error)}, HTTPStatus.INTERNAL_SERVER_ERROR
                )
                return
            self.send_json(result)
            return

        if parsed.path == "/api/status":
            query = parse_qs(parsed.query)
            try:
                after = int(query.get("after", ["0"])[0])
            except ValueError:
                after = 0
            self.send_json(RUN_STATE.snapshot(after))
            return

        self.send_json({"error": "Not found."}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        request_path = urlparse(self.path).path
        if request_path not in {
            "/api/start",
            "/api/stop",
            "/api/add-test-type",
            "/api/delete-preview",
            "/api/delete",
        }:
            self.send_json({"error": "Not found."}, HTTPStatus.NOT_FOUND)
            return

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_json({"error": "Invalid Content-Length."}, HTTPStatus.BAD_REQUEST)
            return
        max_body_size = (
            MAX_UPLOAD_REQUEST_BODY_BYTES
            if request_path == "/api/add-test-type"
            else MAX_REQUEST_BODY_BYTES
        )
        if content_length <= 0 or content_length > max_body_size:
            self.send_json({"error": "Invalid request-body size."}, HTTPStatus.BAD_REQUEST)
            return

        try:
            data = json.loads(self.rfile.read(content_length).decode("utf-8"))
            if request_path == "/api/start":
                options = validate_options(data)
                RUN_STATE.start(options)
                result = RUN_STATE.snapshot(0)
                status = HTTPStatus.ACCEPTED
            elif request_path == "/api/stop":
                result = RUN_STATE.stop()
                status = HTTPStatus.OK
            else:
                with RUN_STATE.lock:
                    if RUN_STATE.running:
                        raise RuntimeError(
                            "Wait for the current test run to finish before changing JSON files."
                        )
                if not isinstance(data, dict):
                    raise ValueError("Request body must be a JSON object.")
                if request_path == "/api/add-test-type":
                    result = add_test_type(data)
                elif request_path == "/api/delete-preview":
                    result = make_deletion_preview(
                        str(data.get("mode", "")), data.get("items")
                    )
                else:
                    result = {"deleted": execute_deletion(data.get("token"))}
                    with RUN_STATE.lock:
                        if RUN_STATE.last_result_path is not None:
                            _, tests_root = get_file_root("tests")
                            last_result = (
                                tests_root / RUN_STATE.last_result_path
                            ).resolve()
                            if not last_result.is_file():
                                RUN_STATE.last_result_path = None
                status = HTTPStatus.OK
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            return
        except RuntimeError as error:
            self.send_json({"error": str(error)}, HTTPStatus.CONFLICT)
            return
        except OSError as error:
            self.send_json(
                {"error": str(error)}, HTTPStatus.INTERNAL_SERVER_ERROR
            )
            return

        self.send_json(result, status)


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--url",
        default=GUI_URL,
        help=f"URL on which to host the GUI (default: {GUI_URL}).",
    )
    return parser.parse_args()


def parse_gui_url(url: str) -> tuple[str, int, str]:
    parsed = urlparse(url.strip())
    if parsed.scheme != "http" or not parsed.hostname:
        raise ValueError("GUI URL must be a valid http:// URL.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("GUI URL must not contain credentials, a query, or a fragment.")
    if parsed.path not in {"", "/"}:
        raise ValueError("GUI URL path must be empty or '/'.")

    try:
        port = parsed.port or 80
    except ValueError as error:
        raise ValueError("GUI URL contains an invalid port.") from error
    if not 1 <= port <= 65535:
        raise ValueError("GUI URL port must be between 1 and 65535.")

    display_url = url.strip().rstrip("/") + "/"
    return parsed.hostname, port, display_url


def main() -> int:
    args = parse_arguments()
    try:
        host, port, gui_url = parse_gui_url(args.url)
    except ValueError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2

    try:
        server = ThreadingHTTPServer((host, port), GuiRequestHandler)
    except OSError as error:
        print(f"Could not host the GUI at {gui_url}: {error}", file=sys.stderr)
        return 1

    print(f"MathCore test GUI is available at {gui_url}")
    print("Press Ctrl+C to stop the GUI server.")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping GUI server.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
