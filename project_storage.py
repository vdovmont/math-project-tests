"""Shared storage paths and startup preparation for both entry points."""

from pathlib import Path


PROJECT_FOLDER = Path(__file__).resolve().parent
DATA_FOLDER = PROJECT_FOLDER / "Data"
SETTINGS_FOLDER = PROJECT_FOLDER / "Settings"
JSON_DATA_FOLDER = DATA_FOLDER / "tests" / "json-data"
TEST_RESULTS_FOLDER = DATA_FOLDER / "tests" / "test-results"


def migrate_legacy_storage(root: Path) -> None:
    # TODO: delete after 7 days (added 2026-09-08; remove after 2026-09-15).
    """Move legacy storage without overwriting files; safe to retry on startup."""
    root = root.resolve()
    moves = [
        (root / "json-data", root / "Data/tests/json-data"),
        (root / "tests", root / "Data/tests/test-results"),
        *[(root / name, root / "Settings" / name) for name in (
            ".json_http_tester_gui_profiles.json",
            ".json_http_tester_gui_test_selection.json",
        )],
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
    for relative in ("Data/tests/json-data", "Data/tests/test-results",
                     "Data/optimizer", "Settings"):
        (root / relative).mkdir(parents=True, exist_ok=True)
