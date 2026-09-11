"""Shared storage paths and startup preparation for the application launcher."""

from pathlib import Path

from config import (
    PROJECT_FOLDER,
    DATA_FOLDER,
    SETTINGS_FOLDER,
    JSON_DATA_ROOT,
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
        (root / "Data/tests/json-data",
         root / JSON_DATA_ROOT.relative_to(PROJECT_FOLDER)),
        (root / "json-data", root / JSON_DATA_FOLDER.relative_to(PROJECT_FOLDER)),
        (root / "tests", root / TEST_RESULTS_FOLDER.relative_to(PROJECT_FOLDER)),
        (root / ".json_http_tester_gui_profiles.json",
         root / SAVED_PROFILES_FILE.relative_to(PROJECT_FOLDER)),
        (root / ".json_http_tester_gui_test_selection.json",
         root / TEST_SELECTION_FILE.relative_to(PROJECT_FOLDER)),
        (root / ".json_http_tester_gui_urls.json",
         root / SAVED_URLS_FILE.relative_to(PROJECT_FOLDER)),
    ]
    current_json_root = root / JSON_DATA_ROOT.relative_to(PROJECT_FOLDER)
    if current_json_root.is_dir():
        for source in current_json_root.iterdir():
            if source.is_file() and source.name.endswith(
                ("-input.json", "-output.json")
            ):
                moves.append(
                    (
                        source,
                        root / JSON_DATA_FOLDER.relative_to(PROJECT_FOLDER) / source.name,
                    )
                )

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

    # Detect collisions between multiple legacy sources before changing anything.
    planned_files: dict[Path, Path] = {}
    for source, destination in moves:
        if source.is_dir():
            planned_items = (
                (item, destination / item.relative_to(source))
                for item in source.rglob("*")
                if item.is_file()
            )
        elif source.is_file():
            planned_items = ((source, destination),)
        else:
            planned_items = ()
        for item, target in planned_items:
            resolved_target = target.resolve()
            previous = planned_files.get(resolved_target)
            if previous is not None and previous != item:
                raise FileExistsError(
                    f"Migration conflict: {previous} and {item} -> {target}. "
                    "Keep or rename one of these files before restarting."
                )
            planned_files[resolved_target] = item

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
