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


# -----------------------------------------------------------------------------
# Configuration: edit these values before running the script.
# -----------------------------------------------------------------------------
BASE_URL = "http://127.0.0.1:9000"
GETQUEUE_REQUEST = "/getqueue"
START_REQUEST = "/start"
STATE_REQUEST = "/state"
STOP_REQUEST = "/stopcalculation"
JSON_DATA_FOLDER = Path(".") / "json-data"
TESTS_FOLDER = Path(".") / "tests"
REQUEST_TIMEOUT_SECONDS = 30.0
STATE_POLL_INTERVAL_SECONDS = 1.0
CALCULATION_TIME_TOLERANCE_PERCENT = 0.0
TOTAL_DISTANCE_TOLERANCE_PERCENT = 0.0
TOTAL_VALUE_TOLERANCE_PERCENT = 0.0
JOBS_TOLERANCE_PERCENT = 0.0
PASS_MARK = "✓"

TOLERANCE_PERCENT_DEFAULTS = {
    "calculation_time": CALCULATION_TIME_TOLERANCE_PERCENT,
    "totalDistance": TOTAL_DISTANCE_TOLERANCE_PERCENT,
    "totalValue": TOTAL_VALUE_TOLERANCE_PERCENT,
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
    output_file = run_folder / f"{base_name}-test.json"
    with output_file.open("w", encoding="utf-8") as file:
        json.dump(state_response, file, indent=4, ensure_ascii=False)
        file.write("\n")

    return output_file


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


def create_comparison_report(
    input_files: list[Path],
    run_folder: Path,
    json_data_folder: Path,
    tolerance_percents: dict[str, float] | None = None,
) -> tuple[Path, bool]:
    """Compare generated test JSONs with expected output JSONs."""
    tolerances = TOLERANCE_PERCENT_DEFAULTS | (tolerance_percents or {})
    # The leading status lines are filled after all comparisons finish.
    report_parts: list[
        str | list[tuple[str, str, str, str, str, str, str]]
    ] = [
        "",
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
                f"{format_number(test_time)}s",
                f"{format_number(expected_time)}s",
                f"{format_number(tolerated_time)}s",
                format_difference(time_difference, suffix="s"),
                format_percentage_difference(test_time, expected_time),
                PASS_MARK if time_status == "PASSED" else time_status,
            )
        ]
        values_passed = time_status != "FAILED"
        for key, label in (
            ("totalDistance", "Total distance"),
            ("totalValue", "Total value"),
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
                    format_number(test_value),
                    format_number(expected_value),
                    format_number(tolerated_expected),
                    format_difference(difference),
                    format_percentage_difference(test_value, expected_value),
                    PASS_MARK if value_status == "PASSED" else value_status,
                )
            )
            values_passed = values_passed and value_status != "FAILED"

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
                format_jobs(test_jobs_taken, test_jobs_total),
                format_jobs(expected_jobs_taken, expected_jobs_total),
                format_jobs(tolerated_jobs_taken, expected_jobs_total),
                format_difference(jobs_difference, integer=True),
                format_difference_percentage(
                    jobs_difference, expected_jobs_taken
                ),
                PASS_MARK if jobs_status == "PASSED" else jobs_status,
            )
        )
        values_passed = values_passed and jobs_status != "FAILED"

        table_rows = [
            (
                "metric",
                "test",
                "expected",
                "tolerance",
                "difference",
                "difference\n(%)",
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
    report_parts[0] = "Overall test results:"
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


def main(arguments: list[str] | None = None) -> int:
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

    all_input_files = sorted(args.json_data_folder.glob("*-input.json"))
    input_files = all_input_files
    if args.test_types:
        requested_types = set(args.test_types)
        available_types = {
            file.name.removesuffix("-input.json") for file in all_input_files
        }
        unknown_types = sorted(requested_types - available_types)
        if unknown_types:
            print(
                f"Unknown test type(s): {', '.join(unknown_types)}",
                file=sys.stderr,
            )
            return 2
        input_files = [
            file
            for file in all_input_files
            if file.name.removesuffix("-input.json") in requested_types
        ]
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
            args.json_data_folder,
            tolerance_percents,
        )
        print(f"\nSaved comparison report: {display_path(report_file)}")
    except OSError as error:
        print(f"Could not create comparison report: {error}", file=sys.stderr)
        return 1

    return 1 if had_error or comparison_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
