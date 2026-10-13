"""Optimizer options and input-merging logic."""

import copy
import json
import math
import time
from collections import defaultdict
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from config import (
    OPTIMIZER_BASE_URL as BASE_URL,
    DISTANCE_PERCENT,
    OPTIMIZER_MERGE_EQUAL_FIELDS,
    OPTIMIZER_MERGE_FATAL_DUPLICATE_FIELDS,
    OPTIMIZER_MERGE_LIST_FIELDS,
    OPTIMIZER_MERGE_RENAME_DUPLICATE_FIELDS,
    REQUEST_TIMEOUT_SECONDS,
    START_REQUEST,
    STATE_POLL_INTERVAL_SECONDS,
    STATE_REQUEST,
    STOP_REQUEST,
)


MISSING = object()


class OptimizationStopped(Exception):
    pass


def build_url(base_url, request_path):
    return f"{base_url.rstrip('/')}/{request_path.lstrip('/')}"


def request_json(url, timeout, body=None):
    headers = {"Accept": "application/json"}
    request_body = None
    if body is not None:
        request_body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    request = Request(url, data=request_body, headers=headers, method="GET")
    with urlopen(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return json.loads(response.read().decode(charset))


def run_optimization(
    input_json, base_url, *, on_query=None, on_progress=None, should_stop=None
):
    start_url = build_url(base_url, START_REQUEST)
    start_response = request_json(start_url, REQUEST_TIMEOUT_SECONDS, input_json)
    if not isinstance(start_response, dict):
        raise ValueError("/start response must be a JSON object.")
    query = start_response.get("query")
    if not isinstance(query, int) or isinstance(query, bool):
        raise ValueError("/start response has no numeric 'query'.")
    if on_query is not None:
        on_query(query, start_url)

    state_url = f"{build_url(base_url, STATE_REQUEST)}?{urlencode({'num': query})}"
    while True:
        if should_stop is not None and should_stop():
            raise OptimizationStopped()
        state_response = request_json(state_url, REQUEST_TIMEOUT_SECONDS)
        if should_stop is not None and should_stop():
            raise OptimizationStopped()
        if not (isinstance(state_response, dict) and state_response.get("solutions") == []):
            return query, state_response
        state = state_response.get("state")
        description = state.get("desc") if isinstance(state, dict) else None
        if on_progress is not None:
            on_progress(
                description.strip()
                if isinstance(description, str) and description.strip()
                else f"Processing query {query}"
            )
        time.sleep(STATE_POLL_INTERVAL_SECONDS)


def stop_optimization(base_url, query):
    stop_url = f"{build_url(base_url, STOP_REQUEST)}?{urlencode({'queue': query})}"
    request = Request(stop_url, headers={"Accept": "application/json"}, method="GET")
    with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        response.read()
    return stop_url


def normalized_id(value):
    if isinstance(value, bool) or value is None:
        return None
    text = str(value)
    if text.isdigit():
        return text.lstrip("0") or "0"
    return text


def build_assignment_map(merged_input, output_json):
    if not isinstance(merged_input, dict) or not isinstance(output_json, dict):
        raise ValueError("Map input and output must be JSON objects.")
    solutions = output_json.get("solutions")
    if not isinstance(solutions, list) or not solutions or not isinstance(solutions[0], dict):
        raise ValueError("Output JSON has no completed solution.")
    routes = solutions[0].get("routes")
    if not isinstance(routes, list):
        raise ValueError("Output solution has no routes list.")

    points = {}
    for point in merged_input.get("points", []):
        if not isinstance(point, dict):
            continue
        point_id = normalized_id(point.get("id"))
        lat, lon = point.get("lat"), point.get("lon")
        if point_id is not None and isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
            points[point_id] = {"lat": float(lat), "lon": float(lon)}

    input_jobs = {}
    for job in merged_input.get("jobList", []):
        if not isinstance(job, dict):
            continue
        job_id = normalized_id(job.get("id"))
        if job_id is not None:
            input_jobs[job_id] = job

    jobs = []
    seen_jobs = set()
    for route in routes:
        if not isinstance(route, dict) or not isinstance(route.get("deliveries"), list):
            continue
        for delivery in route["deliveries"]:
            if not isinstance(delivery, dict) or "depot" not in delivery:
                continue
            job_id = normalized_id(delivery.get("jobID"))
            job = input_jobs.get(job_id)
            if job is None or job_id in seen_jobs:
                continue
            point_id = normalized_id(job.get("point"))
            point = points.get(point_id)
            if point is None:
                continue
            original_depot = job.get("depotID")
            output_depot = delivery.get("depot")
            jobs.append({
                "job_id": str(job.get("id")),
                "point_id": str(job.get("point")),
                "lat": point["lat"],
                "lon": point["lon"],
                "original_depot": str(original_depot),
                "output_depot": str(output_depot),
                "changed": normalized_id(original_depot) != normalized_id(output_depot),
            })
            seen_jobs.add(job_id)

    depots = []
    for depot in merged_input.get("depotList", []):
        if not isinstance(depot, dict):
            continue
        point = points.get(normalized_id(depot.get("point")))
        if point is not None:
            depots.append({
                "id": str(depot.get("id")),
                "lat": point["lat"],
                "lon": point["lon"],
            })
    output_state = output_json.get("state")
    output_distance = (
        output_state.get("optimize_jobs_between_depots_distance", 0)
        if isinstance(output_state, dict) else 0
    )
    try:
        output_distance = float(output_distance)
    except (TypeError, ValueError):
        output_distance = 0.0
    if not math.isfinite(output_distance) or not 0 <= output_distance <= 100:
        output_distance = 0.0

    return {
        "jobs": jobs,
        "depots": depots,
        "changed_count": sum(1 for job in jobs if job["changed"]),
        "unchanged_count": sum(1 for job in jobs if not job["changed"]),
        "distance_percent": output_distance,
    }


def validate_options(data):
    if not isinstance(data, dict):
        raise ValueError("Options must be a JSON object.")
    base_url = data.get("base_url")
    if not isinstance(base_url, str):
        raise ValueError("Base URL must be a valid HTTP or HTTPS URL.")
    parsed = urlparse(base_url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Base URL must be a valid HTTP or HTTPS URL.")
    raw = data.get("distance_percent")
    try:
        distance = float(raw)
    except (TypeError, ValueError) as error:
        raise ValueError("Distance must be a number from 0 to 100.") from error
    if isinstance(raw, bool) or not math.isfinite(distance) or not 0 <= distance <= 100:
        raise ValueError("Distance must be a number from 0 to 100.")
    return {"base_url": base_url.strip(), "distance_percent": distance}


def display_value(value):
    if value is MISSING:
        return "<missing>"
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def compared_fields(documents):
    fields = []
    for configured in OPTIMIZER_MERGE_EQUAL_FIELDS:
        if configured == "options.*":
            option_names = set()
            for _, document in documents:
                options = document.get("options", {})
                if isinstance(options, dict):
                    option_names.update(options)
            fields.extend(f"options.{name}" for name in sorted(option_names))
        else:
            fields.append(configured)
    return fields


def nested_value(document, field):
    value = document
    for part in field.split("."):
        if not isinstance(value, dict) or part not in value:
            return MISSING
        value = value[part]
    return value


def value_key(value):
    if value is MISSING:
        return "<missing>"
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def find_value_differences(documents):
    differences = []
    for field in compared_fields(documents):
        values = [(name, nested_value(document, field)) for name, document in documents]
        if len({value_key(value) for _, value in values}) > 1:
            differences.append((field, values))
    return differences


def collect_entries(documents, field):
    collected = []
    for file_name, document in documents:
        entries = document.get(field, [])
        if not isinstance(entries, list):
            raise ValueError(f"Field '{field}' in '{file_name}' must be a list.")
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                raise ValueError(
                    f"Entry {index + 1} in '{field}' in '{file_name}' must be an object."
                )
            collected.append((file_name, index, copy.deepcopy(entry)))
    return collected


def duplicate_ids(entries):
    occurrences = defaultdict(list)
    for file_name, index, entry in entries:
        if "id" in entry:
            occurrences[value_key(entry["id"])].append((file_name, index, entry["id"]))
    return [items for items in occurrences.values() if len(items) > 1]


def merge_inputs(documents, distance_percent):
    """Return (merged document or None, report text, warning count, error count)."""
    if not documents:
        return None, "Merge failed\n\nNo initial JSON files were found.\n", 0, 1

    report = ["Optimizer input merge report", ""]
    if len(documents) >= 2:
        differences = find_value_differences(documents)
        if differences:
            report.extend(["Merge failed", "", "Required fields have different values:"])
            for field, values in differences:
                report.extend(["", f"Field: {field}"])
                report.extend(
                    f"  {file_name}: {display_value(value)}" for file_name, value in values
                )
            return None, "\n".join(report) + "\n", 0, len(differences)

    collected = {}
    try:
        for field in OPTIMIZER_MERGE_LIST_FIELDS:
            collected[field] = collect_entries(documents, field)
    except ValueError as error:
        report.extend(["Merge failed", "", str(error)])
        return None, "\n".join(report) + "\n", 0, 1

    fatal_duplicates = []
    for field in OPTIMIZER_MERGE_FATAL_DUPLICATE_FIELDS:
        for occurrences in duplicate_ids(collected.get(field, [])):
            fatal_duplicates.append((field, occurrences))
    if fatal_duplicates:
        report.extend(["Merge failed", "", "Matching job IDs were found:"])
        for field, occurrences in fatal_duplicates:
            report.extend(["", f"Field: {field}, ID: {display_value(occurrences[0][2])}"])
            report.extend(
                f"  {file_name}, entry {index + 1}" for file_name, index, _ in occurrences
            )
        return None, "\n".join(report) + "\n", 0, len(fatal_duplicates)

    merged = copy.deepcopy(documents[0][1])
    warnings = []
    for field in OPTIMIZER_MERGE_LIST_FIELDS:
        used_ids = {}
        merged_entries = []
        for file_name, index, entry in collected[field]:
            if field in OPTIMIZER_MERGE_RENAME_DUPLICATE_FIELDS and "id" in entry:
                original_id = entry["id"]
                candidate_key = value_key(original_id)
                if candidate_key in used_ids:
                    first_file, first_index = used_ids[candidate_key]
                    suffix = 1
                    candidate = f"{original_id}_{suffix}"
                    while value_key(candidate) in used_ids:
                        suffix += 1
                        candidate = f"{original_id}_{suffix}"
                    entry["id"] = candidate
                    candidate_key = value_key(candidate)
                    warnings.append(
                        f"{field}: duplicate ID {display_value(original_id)} in "
                        f"{first_file}, entry {first_index + 1} and {file_name}, "
                        f"entry {index + 1}; the latter was changed to {display_value(candidate)}."
                    )
                used_ids[candidate_key] = (file_name, index)
            merged_entries.append(entry)
        merged[field] = merged_entries

    options = merged.get("options")
    if options is None:
        options = {}
        merged["options"] = options
    if not isinstance(options, dict):
        report.extend(["Merge failed", "", "Field 'options' must be an object."])
        return None, "\n".join(report) + "\n", 0, 1
    options["optimize_jobs_between_depots"] = True
    options["optimize_jobs_between_depots_distance"] = distance_percent

    report.extend([
        "Merge completed",
        "",
        f"Input files: {len(documents)}",
        f"Warnings: {len(warnings)}",
    ])
    if warnings:
        report.extend(["", "Warnings:", *(f"  {warning}" for warning in warnings)])
    return merged, "\n".join(report) + "\n", len(warnings), 0


