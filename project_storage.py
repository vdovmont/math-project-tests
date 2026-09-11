"""Shared storage paths and startup preparation for the application launcher."""

from pathlib import Path

from config import (
    PROJECT_FOLDER,
    DATA_FOLDER,
    SETTINGS_FOLDER,
    JSON_DATA_FOLDER,
    TEST_RESULTS_FOLDER,
    OPTIMIZER_DATA_FOLDER,
    SAVED_PROFILES_FILE,
    SAVED_URLS_FILE,
    TEST_SELECTION_FILE,
)


def migrate_legacy_storage(root: Path) -> None:
    # TODO: delete after 7 days (added 2026-09-08; remove after 2026-09-15).
    """Move legacy storage without overwriting files; safe to retry on startup."""
    root = root.resolve()
    moves = [
        (root / "json-data", root / JSON_DATA_FOLDER.relative_to(PROJECT_FOLDER)),
        (root / "tests", root / TEST_RESULTS_FOLDER.relative_to(PROJECT_FOLDER)),
        (root / ".json_http_tester_gui_profiles.json",
         root / SAVED_PROFILES_FILE.relative_to(PROJECT_FOLDER)),
        (root / ".json_http_tester_gui_test_selection.json",
         root / TEST_SELECTION_FILE.relative_to(PROJECT_FOLDER)),
        (root / ".json_http_tester_gui_urls.json",
         root / SAVED_URLS_FILE.relative_to(PROJECT_FOLDER)),
    ]

    def validate(source: Path, destination: Path) -> None:
        for path in (source, destination):
            if not path.resolve().is_relative_to(root) or path.is_symlink():
                raise OSError(f"Unsafe migration path: {path}")
        if not source.exists():
            return
        if destination.exists():
            if not source.is_dir() or not destination.is_dir():
                raise FileExistsError(
                    f"Migration conflict: {source} -> {destination}. "
                    "Keep or rename one of these files before restarting."
                )
        if source.is_dir():
            for child in source.iterdir():
                validate(child, destination / child.name)

    def move(source: Path, destination: Path) -> None:
        if not source.exists():
            return
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir() and destination.exists():
            for child in source.iterdir():
                move(child, destination / child.name)
            source.rmdir()
        else:
            source.rename(destination)

    # Check every move before changing anything, including paths in merged trees.
    for source, destination in moves:
        validate(source, destination)
    for source, destination in moves:
        move(source, destination)


def prepare_storage(root: Path = PROJECT_FOLDER) -> None:
    """Migrate old installations and ensure permanent storage directories exist."""
    migrate_legacy_storage(root)
    for folder in (JSON_DATA_FOLDER, TEST_RESULTS_FOLDER,
                   OPTIMIZER_DATA_FOLDER, SETTINGS_FOLDER):
        (root / folder.relative_to(PROJECT_FOLDER)).mkdir(parents=True, exist_ok=True)
