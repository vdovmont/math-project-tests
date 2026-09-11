#!/usr/bin/env python3
"""Create a timestamped test-run folder using MathCore metadata."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from config import JSON_DATA_FOLDER

from config import (
    TESTER_BASE_URL as BASE_URL,
    GETQUEUE_REQUEST,
    START_REQUEST,
    STATE_REQUEST,
    STOP_REQUEST,
    TESTS_FOLDER,
    REQUEST_TIMEOUT_SECONDS,
    STATE_POLL_INTERVAL_SECONDS,
    CALCULATION_TIME_TOLERANCE_PERCENT,
    TOTAL_DISTANCE_TOLERANCE_PERCENT,
    TOTAL_VALUE_TOLERANCE_PERCENT,
    TOTAL_DURATION_TOLERANCE_PERCENT,
    JOBS_TOLERANCE_PERCENT,
    PASS_MARK,
    MAX_REFERENCE_FOLDER_NAME_LENGTH,
)


TOLERANCE_PERCENT_DEFAULTS = {
    "calculation_time": CALCULATION_TIME_TOLERANCE_PERCENT,
    "totalDistance": TOTAL_DISTANCE_TOLERANCE_PERCENT,
    "totalValue": TOTAL_VALUE_TOLERANCE_PERCENT,
    "totalDuration": TOTAL_DURATION_TOLERANCE_PERCENT,
    "jobs": JOBS_TOLERANCE_PERCENT,
}

CALCULATION_TIME_PATTERN = re.compile(
    r"Calculation time:\s*"
    r"([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)\s*s"
)
JOBS_TAKEN_PATTERN = re.compile(
    r"\b(\d+)\s*/\s*(\d+)\s+jobs\s+taken\b", re.IGNORECASE
)


def parse_arguments(arguments: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=BASE_URL)
    parser.add_argument("--json-data-folder", type=Path, default=JSON_DATA_FOLDER)
    parser.add_argument("--tests-folder", type=Path, default=TESTS_FOLDER)
    parser.add_argument("--reference-folder", type=Path)
    parser.add_argument("--generate-reference-folder", type=Path)
    parser.add_argument("--reference-name")
    parser.add_argument("--comparison-reference-label", default="Initial")
    parser.add_argument("--comparison-test-label")
    parser.add_argument(
        "--test-type",
        dest="test_types",
        action="append",
        help="Run only this test type (repeat for multiple types).",
    )
    parser.add_argument(
        "--timeout", type=float, default=REQUEST_TIMEOUT_SECONDS
    )
    parser.add_argument(
        "--poll-interval", type=float, default=STATE_POLL_INTERVAL_SECONDS
    )
    parser.add_argument(
        "--calculation-time-tolerance-percent",
        type=float,
        default=CALCULATION_TIME_TOLERANCE_PERCENT,
    )
    parser.add_argument(
        "--total-distance-tolerance-percent",
        type=float,
        default=TOTAL_DISTANCE_TOLERANCE_PERCENT,
    )
    parser.add_argument(
        "--total-value-tolerance-percent",
        type=float,
        default=TOTAL_VALUE_TOLERANCE_PERCENT,
    )
    parser.add_argument(
        "--total-duration-tolerance-percent",
        type=float,
        default=TOTAL_DURATION_TOLERANCE_PERCENT,
    )
    parser.add_argument(
        "--jobs-tolerance-percent",
        dest="jobs_tolerance_percent",
        type=float,
        default=JOBS_TOLERANCE_PERCENT,
    )
    return parser.parse_args(arguments)


def build_url(base_url: str, request_path: str) -> str:
    return f"{base_url.rstrip('/')}/{request_path.lstrip('/')}"


def display_path(path: Path | str) -> str:
    """Return a user-facing path relative to the launch directory."""
    resolved_path = Path(path).resolve()
    try:
        return os.path.relpath(resolved_path, Path.cwd().resolve())
    except ValueError:
        # Windows cannot construct a relative path across different drives.
        return resolved_path.name


def request_json(url: str, timeout: float, body: object | None = None) -> object:
    """Send a GET request and decode its JSON response."""
    headers = {"Accept": "application/json"}
    request_body = None
    if body is not None:
        request_body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"

    request = Request(url, data=request_body, headers=headers, method="GET")

    with urlopen(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return json.loads(response.read().decode(charset))


def get_queue_data(base_url: str, request_path: str, timeout: float) -> dict[str, object]:
    """Request /getqueue and return its JSON object."""
    data = request_json(build_url(base_url, request_path), timeout)

    if not isinstance(data, dict):
        raise ValueError("/getqueue response must be a JSON object")
    return data


def safe_folder_component(value: str) -> str:
    """Replace characters that are invalid in Windows folder names."""
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value.strip())
    value = value.rstrip(". ")
    if not value:
        raise ValueError("folder name component is empty")
    return value


def extract_mathcore_metadata(queue_data: dict[str, object]) -> tuple[str, str]:
    """Extract and normalize the MathCore version and commit hash."""
    raw_version = queue_data.get("MathCore version")
    raw_hash = queue_data.get("MathCore commit hash")

    if not isinstance(raw_version, str) or not raw_version.strip():
        raise ValueError("/getqueue response has no valid 'MathCore version'")
    if not isinstance(raw_hash, str) or not raw_hash.strip():
        raise ValueError("/getqueue response has no valid 'MathCore commit hash'")

    version = raw_version.strip()
    if version.startswith("Ver:"):
        version = version.removeprefix("Ver:").strip()

    return safe_folder_component(version), safe_folder_component(raw_hash)


def get_query_number(start_response: object) -> int:
    if not isinstance(start_response, dict):
        raise ValueError("/start response must be a JSON object")

    query = start_response.get("query")
    if not isinstance(query, int) or isinstance(query, bool):
        raise ValueError("/start response has no numeric 'query'")
    return query


def state_is_processing(state_response: object) -> bool:
    """An empty solutions list means MathCore is still processing the query."""
    return (
        isinstance(state_response, dict)
        and state_response.get("solutions") == []
    )


def get_state_description(state_response: object, query: int) -> str:
    if isinstance(state_response, dict):
        state = state_response.get("state")
        if isinstance(state, dict):
            description = state.get("desc")
            if isinstance(description, str) and description.strip():
                return description.strip()
    return f"Processing query {query}"


def process_input_file(
    input_file: Path,
    run_folder: Path,
    base_url: str,
    timeout: float,
    poll_interval: float,
    output_suffix: str = "-test.json",
) -> Path:
    with input_file.open("r", encoding="utf-8") as file:
        input_json = json.load(file)

    start_url = build_url(base_url, START_REQUEST)
    print(f"GET {start_url} <- {input_file.name}")
    start_response = request_json(
        start_url, timeout, body=input_json
    )
    query = get_query_number(start_response)
    print(f"Query number: {query}")

    state_url = f"{build_url(base_url, STATE_REQUEST)}?{urlencode({'num': query})}"
    progress_was_shown = False
    progress_width = 0
    try:
        while True:
            state_response = request_json(state_url, timeout)
            if not state_is_processing(state_response):
                break

            description = get_state_description(state_response, query)
            progress_width = max(progress_width, len(description))
            print(f"\r{description.ljust(progress_width)}", end="", flush=True)
            progress_was_shown = True
            time.sleep(poll_interval)
    finally:
        if progress_was_shown:
            # Clear the terminal line, then let the next message replace it.
            print(f"\r{' ' * progress_width}\r", end="", flush=True)

    base_name = input_file.name.removesuffix("-input.json")
    output_file = run_folder / f"{base_name}{output_suffix}"
    with output_file.open("w", encoding="utf-8") as file:
        json.dump(state_response, file, indent=4, ensure_ascii=False)
        file.write("\n")

    return output_file


def select_input_files(json_data_folder: Path, test_types: list[str] | None) -> list[Path]:
    all_input_files = sorted(json_data_folder.glob("*-input.json"))
    if not test_types:
        return all_input_files
    requested_types = set(test_types)
    available_types = {
        file.name.removesuffix("-input.json") for file in all_input_files
    }
    unknown_types = sorted(requested_types - available_types)
    if unknown_types:
        raise ValueError(f"Unknown test type(s): {', '.join(unknown_types)}")
    return [
        file
        for file in all_input_files
        if file.name.removesuffix("-input.json") in requested_types
    ]


def generate_reference_outputs(args: argparse.Namespace, started_at: datetime) -> int:
    temporary_folder = args.generate_reference_folder
    if temporary_folder is None or not args.reference_name:
        print("Error: reference generation requires a folder and name.", file=sys.stderr)
        return 2
    if not args.json_data_folder.is_dir():
        print(f"JSON data folder does not exist: {display_path(args.json_data_folder)}", file=sys.stderr)
        return 2
    try:
        input_files = select_input_files(args.json_data_folder, args.test_types)
    except ValueError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2
    if not input_files:
        print(f"No *-input.json files found in: {display_path(args.json_data_folder)}", file=sys.stderr)
        return 2
    if not temporary_folder.is_dir():
        print("Reference generation folder does not exist.", file=sys.stderr)
        return 2
    existing_items = list(temporary_folder.iterdir())
    if any(
        not item.is_file() or not item.name.endswith("-output.json")
        for item in existing_items
    ):
        print("Reference generation folder contains unsupported files.", file=sys.stderr)
        return 2
    for item in existing_items:
        item.unlink()

    getqueue_url = build_url(args.base_url, GETQUEUE_REQUEST)
    print(f"GET {getqueue_url}")
    try:
        queue_data = get_queue_data(args.base_url, GETQUEUE_REQUEST, args.timeout)
        version, _ = extract_mathcore_metadata(queue_data)
    except (HTTPError, URLError, TimeoutError, OSError, ValueError) as error:
        print(f"Could not read MathCore metadata: {error}", file=sys.stderr)
        return 1

    timestamp = started_at.strftime("%Y.%m.%d-%H.%M.%S")
    safe_url_name = safe_folder_component(args.reference_name)
    safe_version = safe_folder_component(version)[:80]
    suffix = f"-{safe_version}-{timestamp}"
    available_name_length = MAX_REFERENCE_FOLDER_NAME_LENGTH - len(suffix)
    final_name = f"{safe_url_name[:available_name_length]}{suffix}"
    final_folder = temporary_folder.parent / final_name
    if final_folder.exists():
        print(f"Reference folder already exists: {display_path(final_folder)}", file=sys.stderr)
        return 1

    output_folder = temporary_folder
    had_error = False
    for input_file in input_files:
        print(f"\n=== {input_file.name} ===")
        try:
            output_file = process_input_file(
                input_file,
                output_folder,
                args.base_url,
                args.timeout,
                args.poll_interval,
                "-output.json",
            )
            if output_folder == temporary_folder:
                temporary_folder.rename(final_folder)
                output_folder = final_folder
                output_file = output_folder / output_file.name
                print(f"Renamed reference folder: {display_path(output_folder)}")
            print(f"Saved result: {display_path(output_file)}")
        except HTTPError as error:
            print(f"HTTP request failed with status {error.code}: {error.reason}", file=sys.stderr)
            had_error = True
        except (URLError, TimeoutError, OSError) as error:
            print(f"Request or file error: {error}", file=sys.stderr)
            had_error = True
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            print(f"Invalid JSON or response: {error}", file=sys.stderr)
            had_error = True
    if output_folder == temporary_folder:
        return 1
    print(f"Reference outputs folder: {display_path(output_folder)}")
    return 1 if had_error else 0


def require_number(data: dict[str, object], field: str) -> int | float:
    value = data.get(field)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"field '{field}' must be a number")
    return value


def find_result_object(data: object) -> dict[str, object]:
    """Find the solution object inside a possibly wrapped state response."""
    required_fields = {
        "comment",
        "totalDistance",
        "totalValue",
    }

    if isinstance(data, dict):
        if required_fields.issubset(data):
            return data
        children = data.values()
    elif isinstance(data, list):
        children = data
    else:
        children = ()

    for child in children:
        try:
            return find_result_object(child)
        except ValueError:
            continue

    raise ValueError(
        "could not find a result object containing comment, totalDistance, "
        "and totalValue"
    )


def sum_numeric_entries(data: object, key: str) -> int | float:
    """Sum every numeric value stored under key anywhere in a JSON value."""
    total: int | float = 0
    found = False

    def visit(value: object) -> None:
        nonlocal total, found
        if isinstance(value, dict):
            for child_key, child in value.items():
                if child_key == key:
                    if not isinstance(child, (int, float)) or isinstance(child, bool):
                        raise ValueError(f"field '{key}' must be numeric")
                    if not math.isfinite(child):
                        raise ValueError(f"field '{key}' must be finite")
                    total += child
                    found = True
                else:
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(data)
    if not found:
        raise ValueError(f"could not find any '{key}' values")
    return total


def extract_result_metrics(
    data: object,
) -> dict[str, int | float | tuple[int, int]]:
    result = find_result_object(data)

    comment = result.get("comment")
    if not isinstance(comment, str):
        raise ValueError("field 'comment' must be a string")

    calculation_time_match = CALCULATION_TIME_PATTERN.search(comment)
    if calculation_time_match is None:
        raise ValueError("'comment' has no calculation time")

    jobs_match = JOBS_TAKEN_PATTERN.search(comment)
    if jobs_match is None:
        raise ValueError("'comment' has no 'taken/total jobs taken' value")
    jobs_taken = int(jobs_match.group(1))
    jobs_total = int(jobs_match.group(2))
    if jobs_taken > jobs_total:
        raise ValueError("jobs taken cannot be greater than total jobs")

    return {
        "calculation_time": float(calculation_time_match.group(1)),
        "totalDistance": require_number(result, "totalDistance"),
        "totalValue": require_number(result, "totalValue"),
        "totalDuration": sum_numeric_entries(data, "totalDuration"),
        "jobs": (jobs_taken, jobs_total),
    }


def format_number(value: int | float, *, integer: bool = False) -> str:
    formatted = f"{value:.1f}"
    if integer:
        return formatted.rstrip("0").rstrip(".")
    return formatted


def format_difference(
    value: int | float, *, suffix: str = "", integer: bool = False
) -> str:
    rounded_value = round(value) if integer else round(value, 1)
    if rounded_value == 0:
        return "0"
    if integer:
        return f"{value:+.0f}"
    return f"{value:+.1f}{suffix}"


def format_percentage_difference(
    test_value: int | float, expected_value: int | float
) -> str:
    difference = test_value - expected_value
    return format_difference_percentage(difference, expected_value)


def format_difference_percentage(
    difference: int | float, denominator: int | float
) -> str:
    if difference == 0:
        return "0"
    if denominator == 0:
        return "N/A"

    percentage = difference / abs(denominator) * 100
    absolute_percentage = abs(percentage)
    decimal_places = (
        3
        if absolute_percentage < 0.1
        and not math.isclose(absolute_percentage, 0.1)
        else 1
    )
    rounded_percentage = round(percentage, decimal_places)
    if rounded_percentage == 0:
        return f"{0:.{decimal_places}f}%"
    return f"{percentage:+.{decimal_places}f}%"


def value_is_within_tolerance(
    test_value: int | float,
    expected_value: int | float,
    tolerance_percent: float,
) -> bool:
    return test_value <= expected_value_with_tolerance(
        expected_value, tolerance_percent
    )


def expected_value_with_tolerance(
    expected_value: int | float, tolerance_percent: float
) -> float:
    allowed_error = abs(expected_value) * tolerance_percent / 100
    return expected_value + allowed_error


def comparison_status(
    test_value: int | float,
    expected_value: int | float,
    tolerated_expected_value: int | float,
) -> str:
    if test_value <= expected_value:
        return "PASSED"
    if test_value <= tolerated_expected_value:
        return "PASSED (WITH TOLERANCE)"
    return "FAILED"


def jobs_value_with_tolerance(
    expected_jobs_taken: int, tolerance_percent: float
) -> int:
    return math.ceil(expected_jobs_taken / (1 + tolerance_percent / 100))


def jobs_comparison_status(
    test_jobs_taken: int,
    expected_jobs_taken: int,
    tolerated_jobs_taken: int,
) -> str:
    if test_jobs_taken >= expected_jobs_taken:
        return "PASSED"
    if test_jobs_taken >= tolerated_jobs_taken:
        return "PASSED (WITH TOLERANCE)"
    return "FAILED"


def format_jobs(taken: int, total: int) -> str:
    return f"{taken}/{total}"


def extract_output_version(output_file: Path) -> str:
    """Read the MathCore version recorded in an output JSON's state description."""
    with output_file.open("r", encoding="utf-8") as file:
        output = json.load(file)
    if not isinstance(output, dict):
        raise ValueError("output JSON must contain an object")
    state = output.get("state")
    description = state.get("desc") if isinstance(state, dict) else None
    if not isinstance(description, str) or not description.strip():
        raise ValueError("output JSON has no valid state description")
    version = description.strip()
    if version.startswith("Ver:"):
        version = version.removeprefix("Ver:").strip()
    if not version:
        raise ValueError("output JSON has no valid version")
    return version


def first_output_version(input_files: list[Path], folder: Path, suffix: str) -> str:
    """Return the version from the first usable output in input-file order."""
    for input_file in input_files:
        output_file = folder / f"{input_file.name.removesuffix('-input.json')}{suffix}"
        if not output_file.is_file():
            continue
        try:
            return extract_output_version(output_file)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            continue
    return "version unavailable"


def create_comparison_report(
    input_files: list[Path],
    run_folder: Path,
    json_data_folder: Path,
    tolerance_percents: dict[str, float] | None = None,
    reference_label: str = "Initial",
    test_label: str = "Test URL",
) -> tuple[Path, bool]:
    """Compare generated test JSONs with expected output JSONs."""
    tolerances = TOLERANCE_PERCENT_DEFAULTS | (tolerance_percents or {})
    reference_display = reference_label
    if reference_label.casefold() == "initial":
        reference_version = first_output_version(
            input_files, json_data_folder, "-output.json"
        )
        reference_display = f"{reference_label} ({reference_version})"
    test_version = first_output_version(input_files, run_folder, "-test.json")
    comparison_lines = [
        reference_display,
        "vs",
        f"{test_label} ({test_version})",
    ]
    # The leading status lines are filled after all comparisons finish.
    report_parts: list[
        str | list[tuple[str, str, str, str, str, str, str]]
    ] = [
        "",
        "",
        "",
        "",
        "",
        *comparison_lines,
        "",
        "",
        "",
    ]
    comparison_failed = False

    for input_file in input_files:
        base_name = input_file.name.removesuffix("-input.json")
        test_file = run_folder / f"{base_name}-test.json"
        expected_file = json_data_folder / f"{base_name}-output.json"
        report_parts.append(f"[{base_name}]")
        status_line_index = len(report_parts)
        report_parts.append("")
        report_parts.append("")

        if not test_file.is_file():
            report_parts[status_line_index] = "    FAILED"
            report_parts.append(
                f"ERROR: test result is missing: {display_path(test_file)}"
            )
            report_parts.extend(("", "", ""))
            comparison_failed = True
            continue
        if not expected_file.is_file():
            report_parts[status_line_index] = "    FAILED"
            report_parts.append(
                f"ERROR: expected output is missing: {display_path(expected_file)}"
            )
            report_parts.extend(("", "", ""))
            comparison_failed = True
            continue

        try:
            with test_file.open("r", encoding="utf-8") as file:
                test_metrics = extract_result_metrics(json.load(file))
            with expected_file.open("r", encoding="utf-8") as file:
                expected_metrics = extract_result_metrics(json.load(file))
        except (OSError, json.JSONDecodeError, ValueError) as error:
            report_parts[status_line_index] = "    FAILED"
            report_parts.append(f"ERROR: {error}")
            report_parts.extend(("", "", ""))
            comparison_failed = True
            continue

        test_time = test_metrics["calculation_time"]
        expected_time = expected_metrics["calculation_time"]
        time_difference = test_time - expected_time
        tolerated_time = expected_value_with_tolerance(
            expected_time, tolerances["calculation_time"]
        )
        time_status = comparison_status(
            test_time, expected_time, tolerated_time
        )

        rows = [
            (
                "Calculation time",
                f"{format_number(expected_time)}s",
                f"{format_number(tolerated_time)}s",
                f"{format_number(test_time)}s",
                format_difference(time_difference, suffix="s"),
                format_percentage_difference(test_time, expected_time),
                PASS_MARK if time_status == "PASSED" else time_status,
            )
        ]
        values_passed = time_status != "FAILED"

        test_jobs_taken, test_jobs_total = test_metrics["jobs"]
        expected_jobs_taken, expected_jobs_total = expected_metrics["jobs"]
        jobs_difference = test_jobs_taken - expected_jobs_taken
        tolerated_jobs_taken = jobs_value_with_tolerance(
            expected_jobs_taken, tolerances["jobs"]
        )
        jobs_status = jobs_comparison_status(
            test_jobs_taken, expected_jobs_taken, tolerated_jobs_taken
        )
        rows.append(
            (
                "Jobs",
                format_jobs(expected_jobs_taken, expected_jobs_total),
                format_jobs(tolerated_jobs_taken, expected_jobs_total),
                format_jobs(test_jobs_taken, test_jobs_total),
                format_difference(jobs_difference, integer=True),
                format_difference_percentage(
                    jobs_difference, expected_jobs_taken
                ),
                PASS_MARK if jobs_status == "PASSED" else jobs_status,
            )
        )
        values_passed = values_passed and jobs_status != "FAILED"

        for key, label in (
            ("totalDistance", "Total distance"),
            ("totalValue", "Total value"),
            ("totalDuration", "Total time"),
        ):
            test_value = test_metrics[key]
            expected_value = expected_metrics[key]
            difference = test_value - expected_value
            tolerated_expected = expected_value_with_tolerance(
                expected_value, tolerances[key]
            )
            value_status = comparison_status(
                test_value, expected_value, tolerated_expected
            )
            rows.append(
                (
                    label,
                    format_number(expected_value),
                    format_number(tolerated_expected),
                    format_number(test_value),
                    format_difference(difference),
                    format_percentage_difference(test_value, expected_value),
                    PASS_MARK if value_status == "PASSED" else value_status,
                )
            )
            values_passed = values_passed and value_status != "FAILED"

        table_rows = [
            (
                "metric",
                "expected",
                "tolerance",
                "test",
                "difference",
                "difference(%)",
                "status",
            ),
            *rows,
        ]
        report_parts.append(table_rows)

        report_parts[status_line_index] = (
            "    PASSED" if values_passed else "    FAILED"
        )
        report_parts.extend(("", "", ""))
        comparison_failed = comparison_failed or not values_passed

    overall_result = "FAILED" if comparison_failed else "PASSED"
    report_parts[0] = "Overall test result:"
    report_parts[1] = f"    {overall_result}"

    tables = [part for part in report_parts if isinstance(part, list)]
    column_widths = (
        [
            max(
                len(line)
                for table in tables
                for row in table
                for line in row[column].splitlines()
            )
            for column in range(len(tables[0][0]))
        ]
        if tables
        else []
    )

    report_lines: list[str] = []
    for part in report_parts:
        if isinstance(part, str):
            report_lines.append(part)
            continue
        for row_index, row in enumerate(part):
            cell_lines = [value.splitlines() or [""] for value in row]
            for line_index in range(max(len(lines) for lines in cell_lines)):
                report_lines.append(
                    "  ".join(
                        (
                            lines[line_index]
                            if line_index < len(lines)
                            else ""
                        ).ljust(column_widths[column])
                        for column, lines in enumerate(cell_lines)
                    ).rstrip()
                )
            if row_index == 0:
                report_lines.append(
                    "  ".join("-" * width for width in column_widths)
                )

    report_file = run_folder / "result.txt"
    with report_file.open("w", encoding="utf-8") as file:
        file.write("\n".join(report_lines))

    return report_file, comparison_failed


def run(arguments: list[str] | None = None) -> int:
    args = parse_arguments(arguments)
    started_at = datetime.now()

    if args.timeout <= 0:
        print("Error: timeout must be greater than zero.", file=sys.stderr)
        return 2
    if args.poll_interval <= 0:
        print("Error: poll interval must be greater than zero.", file=sys.stderr)
        return 2
    tolerance_percents = {
        "calculation_time": args.calculation_time_tolerance_percent,
        "totalDistance": args.total_distance_tolerance_percent,
        "totalValue": args.total_value_tolerance_percent,
        "totalDuration": args.total_duration_tolerance_percent,
        "jobs": args.jobs_tolerance_percent,
    }
    if any(
        not math.isfinite(value) or value < 0
        for value in tolerance_percents.values()
    ):
        print(
            "Error: tolerance percentages must be finite and non-negative.",
            file=sys.stderr,
        )
        return 2
    if not args.base_url.strip():
        print("Error: base URL must not be empty.", file=sys.stderr)
        return 2
    if args.generate_reference_folder is not None:
        return generate_reference_outputs(args, started_at)

    try:
        args.tests_folder.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        print(
            f"Could not create tests folder "
            f"'{display_path(args.tests_folder)}': {error}",
            file=sys.stderr,
        )
        return 1

    getqueue_url = build_url(args.base_url, GETQUEUE_REQUEST)
    print(f"GET {getqueue_url}")

    try:
        queue_data = get_queue_data(
            args.base_url, GETQUEUE_REQUEST, args.timeout
        )
        version, commit_hash = extract_mathcore_metadata(queue_data)
    except HTTPError as error:
        print(f"GET /getqueue failed with HTTP {error.code}: {error.reason}", file=sys.stderr)
        return 1
    except (URLError, TimeoutError, OSError) as error:
        print(f"GET /getqueue request failed: {error}", file=sys.stderr)
        return 1
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        print(f"Invalid /getqueue response: {error}", file=sys.stderr)
        return 1

    # Colons are not permitted in Windows folder names, so time uses dots.
    timestamp = started_at.strftime("%Y.%m.%d-%H.%M.%S")
    run_folder = args.tests_folder / f"{timestamp}-{version}-{commit_hash}"

    try:
        run_folder.mkdir()
    except FileExistsError:
        print(
            f"Test-run folder already exists: {display_path(run_folder)}",
            file=sys.stderr,
        )
        return 1
    except OSError as error:
        print(
            f"Could not create test-run folder "
            f"'{display_path(run_folder)}': {error}",
            file=sys.stderr,
        )
        return 1

    print(f"Created test-run folder: {display_path(run_folder)}")

    if not args.json_data_folder.is_dir():
        print(
            f"JSON data folder does not exist: "
            f"{display_path(args.json_data_folder)}",
            file=sys.stderr,
        )
        return 2

    try:
        input_files = select_input_files(args.json_data_folder, args.test_types)
    except ValueError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2
    if not input_files:
        print(
            f"No *-input.json files found in: "
            f"{display_path(args.json_data_folder)}",
            file=sys.stderr,
        )
        return 2

    had_error = False
    for input_file in input_files:
        print(f"\n=== {input_file.name} ===")
        try:
            output_file = process_input_file(
                input_file,
                run_folder,
                args.base_url,
                args.timeout,
                args.poll_interval,
            )
            print(f"Saved result: {display_path(output_file)}")
        except HTTPError as error:
            print(
                f"HTTP request failed with status {error.code}: {error.reason}",
                file=sys.stderr,
            )
            had_error = True
        except (URLError, TimeoutError, OSError) as error:
            print(f"Request or file error: {error}", file=sys.stderr)
            had_error = True
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            print(f"Invalid JSON or response: {error}", file=sys.stderr)
            had_error = True

    try:
        report_file, comparison_failed = create_comparison_report(
            input_files,
            run_folder,
            args.reference_folder or args.json_data_folder,
            tolerance_percents,
            args.comparison_reference_label,
            args.comparison_test_label or args.base_url,
        )
        print(f"\nSaved comparison report: {display_path(report_file)}")
    except OSError as error:
        print(f"Could not create comparison report: {error}", file=sys.stderr)
        return 1

    return 1 if had_error or comparison_failed else 0
