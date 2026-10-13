"""Shared storage paths and startup preparation for the application launcher."""

from pathlib import Path

from config import (
    PROJECT_FOLDER,
    SETTINGS_FOLDER,
    JSON_DATA_FOLDER,
    TEST_RESULTS_FOLDER,
    OPTIMIZER_DATA_FOLDER,
    SAVED_PROFILES_FILE,
    SAVED_URLS_FILE,
    REFERENCE_PROFILES_FILE,
    ALLOWED_ERROR_PROFILES_FILE,
    TEST_SELECTION_FILE,
    PROFILE_FILE,
)


def migrate_settings_filenames(root: Path) -> None:
    """Rename current settings files without overwriting existing files."""
    root = root.resolve()
    settings_folder = root / SETTINGS_FOLDER.relative_to(PROJECT_FOLDER)
    renames = (
        (settings_folder / "profiles.json",
         root / SAVED_PROFILES_FILE.relative_to(PROJECT_FOLDER)),
        (settings_folder / "reference_profiles.json",
         root / REFERENCE_PROFILES_FILE.relative_to(PROJECT_FOLDER)),
        (settings_folder / "allowed_error_profiles.json",
         root / ALLOWED_ERROR_PROFILES_FILE.relative_to(PROJECT_FOLDER)),
        (settings_folder / "test_selection.json",
         root / TEST_SELECTION_FILE.relative_to(PROJECT_FOLDER)),
        (settings_folder / ".json_http_optimizer_gui_profiles.json",
         root / PROFILE_FILE.relative_to(PROJECT_FOLDER)),
    )

    pending_renames = []
    for source, destination in renames:
        if not source.exists():
            continue
        if source.is_symlink() or not source.is_file():
            raise OSError(f"Unsafe settings migration source: {source}")
        if not source.resolve().is_relative_to(root):
            raise OSError(f"Settings migration source is outside the project: {source}")
        if not destination.resolve().is_relative_to(root):
            raise OSError(
                f"Settings migration destination is outside the project: {destination}"
            )
        if destination.exists():
            raise FileExistsError(
                f"Settings migration conflict: {source} -> {destination}. "
                "Keep or rename one of these files before restarting."
            )
        pending_renames.append((source, destination))

    for source, destination in pending_renames:
        source.rename(destination)


def prepare_storage(root: Path = PROJECT_FOLDER) -> None:
    """Ensure permanent storage directories exist."""
    for folder in (JSON_DATA_FOLDER, TEST_RESULTS_FOLDER,
                   OPTIMIZER_DATA_FOLDER, SETTINGS_FOLDER):
        (root / folder.relative_to(PROJECT_FOLDER)).mkdir(parents=True, exist_ok=True)
    migrate_settings_filenames(root)
