"""Optimizer options and input-merging logic."""

import copy
import json
import math
from collections import defaultdict
from urllib.parse import urlparse

from config import (
    OPTIMIZER_BASE_URL as BASE_URL,
    DISTANCE_PERCENT,
    OPTIMIZER_MERGE_EQUAL_FIELDS,
    OPTIMIZER_MERGE_FATAL_DUPLICATE_FIELDS,
    OPTIMIZER_MERGE_LIST_FIELDS,
    OPTIMIZER_MERGE_RENAME_DUPLICATE_FIELDS,
)


MISSING = object()


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


