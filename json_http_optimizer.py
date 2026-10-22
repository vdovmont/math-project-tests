"""Optimizer options and input-merging logic."""

import copy
import json
import math
import re
import time
from collections import defaultdict
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from config import (
    OPTIMIZER_BASE_URL as BASE_URL,
    DISTANCE_PERCENT,
    OPTIMIZER_MERGE_EQUAL_FIELDS,
    OPTIMIZER_MERGE_LIST_FIELDS,
    OPTIMIZER_MERGE_RENAME_DUPLICATE_FIELDS,
    OPTIMIZER_MERGE_UNIQUE_FIELDS,
    REQUEST_TIMEOUT_SECONDS,
    START_REQUEST,
    STATE_POLL_INTERVAL_SECONDS,
    STATE_REQUEST,
    STOP_REQUEST,
)


MISSING = object()
JOBS_TAKEN_PATTERN = re.compile(
    r"\b(\d+)\s*/\s*(\d+)\s+jobs\s+taken\b", re.IGNORECASE
)


class OptimizationStopped(Exception):
    pass


def find_jobs_taken(data: object) -> tuple[int, int] | None:
    """Return the first taken/total jobs value found in a response comment."""
    if isinstance(data, dict):
        comment = data.get("comment")
        if isinstance(comment, str):
            match = JOBS_TAKEN_PATTERN.search(comment)
            if match is not None:
                return int(match.group(1)), int(match.group(2))
        children = data.values()
    elif isinstance(data, list):
        children = data
    else:
        return None
    for child in children:
        counts = find_jobs_taken(child)
        if counts is not None:
            return counts
    return None


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
    map_routes = []
    seen_jobs = set()
    for route_index, route in enumerate(routes):
        if not isinstance(route, dict) or not isinstance(route.get("deliveries"), list):
            continue
        transport_id = route.get("transportID")
        route_id = f"{route_index}:{transport_id}"
        route_points = []
        route_depot = None
        missing_points = 0
        for delivery in route["deliveries"]:
            if not isinstance(delivery, dict):
                continue
            if route_depot is None and delivery.get("depot") is not None:
                route_depot = str(delivery["depot"])
            point = points.get(normalized_id(delivery.get("pointId")))
            if point is None:
                missing_points += 1
                continue
            route_points.append({
                "lat": point["lat"],
                "lon": point["lon"],
                "job_id": str(delivery.get("jobID", "")),
            })
        if route_depot is not None:
            map_routes.append({
                "id": route_id,
                "depot_id": route_depot,
                "transport_id": str(transport_id) if transport_id is not None else "",
                "points": route_points,
                "missing_points": missing_points,
            })
        for stop_number, delivery in enumerate(route["deliveries"], start=1):
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
                "job_extra_id": (
                    str(job.get("extraID")) if job.get("extraID") is not None else ""
                ),
                "point_id": str(job.get("point")),
                "lat": point["lat"],
                "lon": point["lon"],
                "original_depot": str(original_depot),
                "output_depot": str(output_depot),
                "transport_id": str(transport_id) if transport_id is not None else "",
                "route_id": route_id,
                "stop_number": stop_number,
                "arrival": delivery.get("arrival"),
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
        "routes": map_routes,
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


def input_entry_lines(json_text):
    """Return source lines for entries and their IDs in configured top-level lists."""
    decoder = json.JSONDecoder()
    length = len(json_text)

    def skip_whitespace(position):
        while position < length and json_text[position].isspace():
            position += 1
        return position

    def source_line(position):
        return len(re.findall(r"\r+\n|\r|\n", json_text[:position])) + 1

    def object_key_lines(start, end, wanted_keys):
        lines = {}
        position = skip_whitespace(start)
        if position >= end or json_text[position] != "{":
            return lines
        position += 1
        while position < end:
            position = skip_whitespace(position)
            if position >= end or json_text[position] == "}":
                return lines
            key_start = position
            key, position = decoder.raw_decode(json_text, position)
            position = skip_whitespace(position)
            if position >= end or json_text[position] != ":":
                return lines
            position = skip_whitespace(position + 1)
            if key in wanted_keys:
                lines[key] = source_line(key_start)
            _, position = decoder.raw_decode(json_text, position)
            position = skip_whitespace(position)
            if position < end and json_text[position] == ",":
                position += 1
                continue
            return lines
        return lines

    position = skip_whitespace(0)
    if position >= length or json_text[position] != "{":
        return {}
    position += 1
    result = {}
    wanted_fields = set(OPTIMIZER_MERGE_LIST_FIELDS)
    while True:
        position = skip_whitespace(position)
        if position >= length or json_text[position] == "}":
            return result
        key, position = decoder.raw_decode(json_text, position)
        position = skip_whitespace(position)
        if position >= length or json_text[position] != ":":
            return result
        position = skip_whitespace(position + 1)
        if key in wanted_fields and position < length and json_text[position] == "[":
            position += 1
            lines = []
            while True:
                position = skip_whitespace(position)
                if position >= length or json_text[position] == "]":
                    position += 1
                    break
                entry_start = position
                _, position = decoder.raw_decode(json_text, position)
                key_lines = object_key_lines(entry_start, position, {"id", "extraID"})
                lines.append({
                    "entry": source_line(entry_start),
                    "id": key_lines.get("id"),
                    "extraID": key_lines.get("extraID"),
                })
                position = skip_whitespace(position)
                if position < length and json_text[position] == ",":
                    position += 1
                    continue
                if position < length and json_text[position] == "]":
                    position += 1
                    break
                return result
            result[key] = lines
        else:
            _, position = decoder.raw_decode(json_text, position)
        position = skip_whitespace(position)
        if position < length and json_text[position] == ",":
            position += 1
            continue
        if position < length and json_text[position] == "}":
            return result
        return result


def compared_fields(documents):
    fields = []
    for configured in OPTIMIZER_MERGE_EQUAL_FIELDS:
        if configured == "options.*":
            option_names = set()
            for item in documents:
                document = item[1]
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
        values = [
            (item[0], nested_value(item[1], field)) for item in documents
        ]
        if len({value_key(value) for _, value in values}) > 1:
            differences.append((field, values))
    return differences


def collect_entries(documents, field):
    collected = []
    for item in documents:
        file_name, document = item[:2]
        raw_task_id = document.get("task_id")
        task_id_parts = (
            [part.strip() for part in raw_task_id.split("/") if part.strip()]
            if isinstance(raw_task_id, str) else []
        )
        task_id_suffix = task_id_parts[-1] if task_id_parts else None
        source_lines = item[2].get(field, []) if len(item) > 2 else []
        entries = document.get(field, [])
        if not isinstance(entries, list):
            raise ValueError(f"Field '{field}' in '{file_name}' must be a list.")
        for index, entry in enumerate(entries):
            source_location = source_lines[index] if index < len(source_lines) else None
            entry_line = (
                source_location.get("entry")
                if isinstance(source_location, dict) else source_location
            )
            id_line = (
                source_location.get("id")
                if isinstance(source_location, dict) else entry_line
            )
            extra_id_line = (
                source_location.get("extraID")
                if isinstance(source_location, dict) else entry_line
            )
            location = f"line {entry_line}" if entry_line else f"entry {index + 1}"
            id_location = f"line {id_line}" if id_line else location
            extra_id_location = f"line {extra_id_line}" if extra_id_line else location
            if not isinstance(entry, dict):
                raise ValueError(
                    f"{location.capitalize()} in '{field}' in '{file_name}' must be an object."
                )
            collected.append(
                (
                    file_name, location, id_location, extra_id_location,
                    task_id_suffix, copy.deepcopy(entry),
                )
            )
    return collected


def duplicate_values(entries, key, location_index):
    occurrences = defaultdict(list)
    for entry_data in entries:
        file_name, entry = entry_data[0], entry_data[-1]
        if key in entry:
            occurrences[value_key(entry[key])].append(
                (file_name, entry_data[location_index], entry[key])
            )
    return [items for items in occurrences.values() if len(items) > 1]


def merge_inputs(documents, distance_percent):
    """Return (merged document or None, report text, warning count, error count)."""
    report = [
        "Optimizer input merge report",
        "",
        f"Distance: {distance_percent:g}%",
        "",
    ]
    if not documents:
        report.extend(["Merge failed", "", "No initial JSON files were found."])
        return None, "\n".join(report) + "\n", 0, 1

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
    for field, duplicate_key in OPTIMIZER_MERGE_UNIQUE_FIELDS.items():
        location_index = 3 if duplicate_key == "extraID" else 2
        for occurrences in duplicate_values(
            collected.get(field, []), duplicate_key, location_index
        ):
            fatal_duplicates.append((field, occurrences))
    if fatal_duplicates:
        report.extend([
            "Merge failed", "", "Duplicate values were found in fields that must be unique:"
        ])
        for field, occurrences in fatal_duplicates:
            key_name = OPTIMIZER_MERGE_UNIQUE_FIELDS[field]
            heading = f"Field: {field}, {key_name}: {display_value(occurrences[0][2])}"
            report.extend(["", heading])
            report.extend(
                f"  {file_name}, {location}"
                for file_name, location, *_ in occurrences
            )
        return None, "\n".join(report) + "\n", 0, len(fatal_duplicates)

    merged = copy.deepcopy(documents[0][1])
    warnings = []
    for field in OPTIMIZER_MERGE_LIST_FIELDS:
        used_ids = {}
        merged_entries = []
        job_id_renames = {}
        field_entries = collected[field]
        duplicate_job_ids = set()
        if field == "jobList":
            id_counts = defaultdict(int)
            for *_, candidate_entry in field_entries:
                if "id" in candidate_entry:
                    id_counts[value_key(candidate_entry["id"])] += 1
            duplicate_job_ids = {
                candidate_key for candidate_key, count in id_counts.items() if count > 1
            }
            used_ids = {
                value_key(candidate_entry["id"]): None
                for *_, candidate_entry in field_entries
                if "id" in candidate_entry
            }
        if field == "jobList" and duplicate_job_ids:
            missing_task_ids = [
                (file_name, id_location)
                for file_name, _, id_location, _, task_id_suffix, entry in field_entries
                if "id" in entry
                and value_key(entry["id"]) in duplicate_job_ids
                and task_id_suffix is None
            ]
            if missing_task_ids:
                report.extend([
                    "Merge failed", "",
                    "Duplicate job IDs could not be renamed because task_id has no usable value:",
                ])
                report.extend(
                    f"  {file_name}, {id_location}"
                    for file_name, id_location in missing_task_ids
                )
                return None, "\n".join(report) + "\n", 0, len(missing_task_ids)
        for file_name, _, id_location, _, task_id_suffix, entry in field_entries:
            if field == "jobList" and "id" in entry:
                original_id = entry["id"]
                original_key = value_key(original_id)
                if original_key in duplicate_job_ids:
                    candidate = f"{original_id}_{task_id_suffix}"
                    collision_number = 2
                    while value_key(candidate) in used_ids:
                        candidate = f"{original_id}_{task_id_suffix}_{collision_number}"
                        collision_number += 1
                    entry["id"] = candidate
                    used_ids[value_key(candidate)] = (file_name, id_location)
                    rename_group = job_id_renames.setdefault(
                        original_key, {"original": original_id, "entries": []}
                    )
                    rename_group["entries"].append((candidate, file_name, id_location))
            if field in OPTIMIZER_MERGE_RENAME_DUPLICATE_FIELDS and "id" in entry:
                original_id = entry["id"]
                candidate_key = value_key(original_id)
                if candidate_key in used_ids:
                    first_file, first_location = used_ids[candidate_key]
                    suffix = 1
                    candidate = f"{original_id}_{suffix}"
                    while value_key(candidate) in used_ids:
                        suffix += 1
                        candidate = f"{original_id}_{suffix}"
                    entry["id"] = candidate
                    candidate_key = value_key(candidate)
                    warnings.append(
                        f"{field}: duplicate ID {display_value(original_id)} in "
                        f"{first_file}, {first_location} and {file_name}, "
                        f"{id_location}; the latter was changed to {display_value(candidate)}."
                    )
                used_ids[candidate_key] = (file_name, id_location)
            merged_entries.append(entry)
        rename_column_width = max(
            (
                len(f"{rename_group['original']} -> {candidate}")
                for rename_group in job_id_renames.values()
                for candidate, _, _ in rename_group["entries"]
            ),
            default=0,
        )
        for rename_group in job_id_renames.values():
            original_id = rename_group["original"]
            warning_lines = [f"jobList: duplicate ID {original_id} renamed:"]
            for candidate, file_name, id_location in rename_group["entries"]:
                rename_text = f"{original_id} -> {candidate}"
                warning_lines.append(
                    f"    {rename_text:<{rename_column_width}}: {file_name}, {id_location}"
                )
            warnings.append("\n".join(warning_lines))
        merged[field] = merged_entries

    empty_list_warnings = [
        f"Field '{field}' is empty; expect no jobs to be completed."
        for field in OPTIMIZER_MERGE_LIST_FIELDS
        if not collected[field]
    ]

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
        f"Warnings: {len(warnings) + len(empty_list_warnings)}",
    ])
    if empty_list_warnings:
        report.extend(["", "Empty-list warnings:"])
        report.extend(f"  {warning}" for warning in empty_list_warnings)
    if warnings:
        if empty_list_warnings:
            report.append("")
        else:
            report.append("")
        report.append("Warnings:")
        report.extend(f"  {warning}" for warning in warnings)
    warning_count = len(warnings) + len(empty_list_warnings)
    return merged, "\n".join(report) + "\n", warning_count, 0


