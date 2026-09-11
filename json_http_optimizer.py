"""Optimizer core options. JSON processing and statistics are not implemented yet."""

import math
from urllib.parse import urlparse

from config import (
    OPTIMIZER_BASE_URL as BASE_URL,
    DISTANCE_PERCENT,
)


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


