"""Shared Python configuration, grouped by the module each option came from.

Edit defaults here and restart start.py to apply them. Saved GUI profiles and
explicit command-line options still override their corresponding defaults.
Storage folders are paths inside PROJECT_FOLDER. Protocol parsing expressions,
API route tables, and runtime state remain in their implementation modules.
"""

from pathlib import Path


# -----------------------------------------------------------------------------
# project_storage.py
# -----------------------------------------------------------------------------
PROJECT_FOLDER = Path(__file__).resolve().parent
DATA_FOLDER = PROJECT_FOLDER / "Data"
SETTINGS_FOLDER = PROJECT_FOLDER / "Settings"
JSON_DATA_ROOT = DATA_FOLDER / "tests" / "references"
JSON_DATA_FOLDER = JSON_DATA_ROOT / "initial"
TEST_RESULTS_FOLDER = DATA_FOLDER / "tests" / "test-results"
OPTIMIZER_DATA_FOLDER = DATA_FOLDER / "optimizer"
OPTIMIZER_GROUPS_FOLDER = OPTIMIZER_DATA_FOLDER / "groups"
HELP_IMAGES_FOLDER = DATA_FOLDER / "help-images"
UKRAINE_BORDER_FILE = PROJECT_FOLDER / "map-data" / "ukraine-border.geojson"

# -----------------------------------------------------------------------------
# start.py
# -----------------------------------------------------------------------------
GUI_URL = "http://0.0.0.0:9010"
HTML_FILE = PROJECT_FOLDER / "gui.html"
MAX_REQUEST_BODY_BYTES = 64 * 1024

# -----------------------------------------------------------------------------
# json_http_tester.py
# -----------------------------------------------------------------------------
TESTER_BASE_URL = "http://127.0.0.1:9000"
GETQUEUE_REQUEST = "/getqueue"
START_REQUEST = "/start"
STATE_REQUEST = "/state"
STOP_REQUEST = "/stopcalculation"
TESTS_FOLDER = TEST_RESULTS_FOLDER
REQUEST_TIMEOUT_SECONDS = 30.0
STATE_POLL_INTERVAL_SECONDS = 1.0
CALCULATION_TIME_TOLERANCE_PERCENT = 0.0
TOTAL_DISTANCE_TOLERANCE_PERCENT = 0.0
TOTAL_VALUE_TOLERANCE_PERCENT = 0.0
TOTAL_DURATION_TOLERANCE_PERCENT = 0.0
JOBS_TOLERANCE_PERCENT = 0.0
PASS_MARK = "✓"
MAX_REFERENCE_FOLDER_NAME_LENGTH = 240

# -----------------------------------------------------------------------------
# json_http_optimizer.py
# -----------------------------------------------------------------------------
OPTIMIZER_BASE_URL = "http://127.0.0.1:9000"
DISTANCE_PERCENT = 0.0
# Dot-separated paths are supported. "options.*" compares every direct option.
OPTIMIZER_MERGE_EQUAL_FIELDS = (
    "company",
    "dynamic_servicetime",
    "ignore_depot_return_time",
    "router_url",
    "options.*",
)
# These arrays are concatenated in this order. Values configured as unique make
# the merge fail when duplicated. Duplicate job IDs are all suffixed with the
# final non-empty segment of their input's task_id; IDs in the rename list keep
# their first occurrence and suffix later occurrences.
OPTIMIZER_MERGE_LIST_FIELDS = ("depotList", "jobList", "points", "trList")
OPTIMIZER_MERGE_UNIQUE_FIELDS = {"jobList": "extraID"}
OPTIMIZER_MERGE_RENAME_DUPLICATE_FIELDS = ("depotList", "trList")

# -----------------------------------------------------------------------------
# gui_tester.py
# -----------------------------------------------------------------------------
SCRIPT_FOLDER = PROJECT_FOLDER
START_SCRIPT = SCRIPT_FOLDER / "start.py"
MAX_VIEW_FILE_BYTES = 20 * 1024 * 1024
MAX_UPLOAD_FILE_BYTES = 20 * 1024 * 1024
MAX_UPLOAD_FILES = 200
MAX_UPLOAD_REQUEST_BODY_BYTES = (MAX_UPLOAD_FILE_BYTES * 4) + (1024 * 1024)
DELETE_PREVIEW_TTL_SECONDS = 300
EXPECTED_FINALIZATION_SECONDS = 10.0
DISPLAY_DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"
SAVED_PROFILES_FILE = SETTINGS_FOLDER / "tester_profiles.json"
SAVED_URLS_FILE = SETTINGS_FOLDER / "urls.json"
REFERENCE_PROFILES_FILE = SETTINGS_FOLDER / "tester_reference_profiles.json"
ALLOWED_ERROR_PROFILES_FILE = SETTINGS_FOLDER / "tester_allowed_error_profiles.json"
TEST_SELECTION_FILE = SETTINGS_FOLDER / "tester_test_selection.json"
MAX_PROFILE_NAME_LENGTH = 80
MAX_SAVED_PROFILES = 100

# -----------------------------------------------------------------------------
# gui_optimizer.py
# -----------------------------------------------------------------------------
PROFILE_FILE = SETTINGS_FOLDER / "optimizer_profiles.json"
OPTIMIZER_GROUP_SELECTION_FILE = SETTINGS_FOLDER / "optimizer_group_selection.json"
OPTIMIZER_MAX_PROFILE_NAME_LENGTH = 80
OPTIMIZER_MAX_SAVED_PROFILES = 100
OPTIMIZER_MAX_GROUP_NAME_LENGTH = 80

# -----------------------------------------------------------------------------
# gui_help.py
# -----------------------------------------------------------------------------
HELP_EDITOR_USERNAME = "admin"
HELP_EDITOR_PASSWORD = "123"
TESTER_HELP_FILE = SETTINGS_FOLDER / "tester_help.json"
OPTIMIZER_HELP_FILE = SETTINGS_FOLDER / "optimizer_help.json"
HELP_SESSION_SECONDS = 8 * 60 * 60
HELP_MAX_REQUEST_BODY_BYTES = 12 * 1024 * 1024
HELP_MAX_IMAGE_BYTES = 8 * 1024 * 1024
