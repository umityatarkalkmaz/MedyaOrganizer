"""Cross-platform media organizer: sorts files into category folders by extension."""

import argparse
import logging
import shutil
from pathlib import Path

# === Configuration ===
FILE_CATEGORIES: dict[str, set[str]] = {
    "photo": {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"},
    "video": {".mp4", ".mov", ".avi", ".mkv", ".webm"},
    "document": {".pdf", ".docx", ".xlsx", ".txt"},
}

# Reverse lookup for O(1) extension -> category resolution
EXTENSION_TO_CATEGORY: dict[str, str] = {
    ext: category for category, exts in FILE_CATEGORIES.items() for ext in exts
}

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)


def get_unique_target_path(target_path: Path) -> Path:
    """Return a non-colliding path by appending an incrementing suffix if needed."""
    counter = 1
    candidate = target_path
    while candidate.exists():
        candidate = target_path.with_name(f"{target_path.stem}_{counter}{target_path.suffix}")
        counter += 1
    return candidate


def resolve_target_directory(output_dir: Path, category: str, mode: str) -> Path:
    """Build the destination directory for a given category based on organize mode."""
    return output_dir / category if mode == "folder" else output_dir


def collect_media_files(
    source_dir: Path,
    output_dir: Path,
    mode: str,
    move_files: bool = False,
) -> int:
    """
    Scan source_dir recursively and copy/move matched files into output_dir.

    Skips symlinked files (prevents accidental exfiltration of files outside
    source_dir) and files already inside output_dir. Continues on per-file
    errors instead of aborting the whole run.
    """
    processed_count = 0
    error_count = 0
    source_dir = source_dir.resolve()
    output_dir = output_dir.resolve()

    for file_path in source_dir.rglob("*"):
        if not file_path.is_file():
            continue
        if output_dir in file_path.resolve().parents:
            continue
        if file_path.is_symlink():
            logger.warning("Skipped symlink (not followed): %s", file_path)
            continue

        category = EXTENSION_TO_CATEGORY.get(file_path.suffix.lower())
        if category is None:
            continue

        target_dir = resolve_target_directory(output_dir, category, mode)
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = get_unique_target_path(target_dir / file_path.name)

        try:
            if move_files:
                shutil.move(str(file_path), target_path)
            else:
                shutil.copy2(str(file_path), target_path)
            processed_count += 1
        except OSError as error:
            error_count += 1
            logger.error("Failed to %s '%s': %s", "move" if move_files else "copy", file_path, error)

    action = "moved" if move_files else "copied"
    logger.info("%d file(s) %s to '%s'.", processed_count, action, output_dir)
    if error_count:
        logger.warning("%d file(s) failed and were skipped.", error_count)
    return processed_count


def prompt_organize_mode() -> str:
    """Ask the user how to organize files when --mode is not passed via CLI."""
    while True:
        choice = input("How would you like to organize files? [flat/folder]: ").strip().lower()
        if choice in {"flat", "folder"}:
            return choice
        print("Invalid choice. Please enter 'flat' or 'folder'.")


def confirm_move_operation() -> bool:
    """Require explicit confirmation before an irreversible move operation."""
    answer = input("This will MOVE (not copy) files — this cannot be undone. Continue? [y/N]: ")
    return answer.strip().lower() == "y"


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Organize media files by type into a target folder.")
    parser.add_argument("--source", type=Path, default=Path("."), help="Directory to scan (default: current directory)")
    parser.add_argument("--output", type=Path, default=None, help="Output directory (default: <source>/OrganizedMedia)")
    parser.add_argument("--mode", choices=["flat", "folder"], default=None, help="Organization mode; prompted if omitted")
    parser.add_argument("--move", action="store_true", help="Move files instead of copying (asks for confirmation)")
    return parser.parse_args()


def main() -> None:
    args = parse_arguments()
    source_dir = args.source.resolve()
    output_dir = (args.output or source_dir / "OrganizedMedia").resolve()
    mode = args.mode or prompt_organize_mode()

    if args.move and not confirm_move_operation():
        logger.info("Aborted by user.")
        return

    collect_media_files(source_dir, output_dir, mode=mode, move_files=args.move)


if __name__ == "__main__":
    main()
