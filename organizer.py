"""Cross-platform file organizer: sorts by type, skips unmatched into 'other',
and detects true content-based duplicates via SHA-256."""

import argparse
import hashlib
import logging
import shutil
from pathlib import Path

# === Configuration ===
FILE_CATEGORIES: dict[str, set[str]] = {
    "photo": {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".svg", ".heic", ".heif", ".tiff", ".tif", ".ico"},
    "video": {".mp4", ".mov", ".avi", ".mkv", ".webm", ".flv", ".wmv", ".m4v", ".mpg", ".mpeg"},
    "document": {".pdf", ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt", ".txt", ".md", ".rtf", ".odt", ".csv"},
    "audio": {".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a", ".wma"},
    "archive": {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".tgz"},
    "code": {".py", ".js", ".ts", ".jsx", ".tsx", ".html", ".css", ".json", ".go", ".rs", ".c", ".cpp", ".h",
             ".hpp", ".java", ".sh", ".yaml", ".yml", ".sql", ".php", ".rb", ".swift", ".kt"},
    "design": {".psd", ".ai", ".sketch", ".fig", ".xd", ".aseprite", ".ase", ".blend", ".indd"},
    "font": {".ttf", ".otf", ".woff", ".woff2"},
    "ebook": {".epub", ".mobi", ".azw3"},
    "installer": {".exe", ".msi", ".dmg", ".pkg", ".deb", ".appimage", ".apk"},
}
UNMATCHED_CATEGORY = "other"
HASH_CHUNK_SIZE = 1024 * 1024  # 1 MB

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
    return output_dir / category if mode == "folder" else output_dir


def compute_file_hash(file_path: Path) -> str:
    """Compute SHA-256 of a file, reading in chunks to avoid loading it fully into memory."""
    hasher = hashlib.sha256()
    with file_path.open("rb") as f:
        for chunk in iter(lambda: f.read(HASH_CHUNK_SIZE), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def collect_media_files(
    source_dir: Path,
    output_dir: Path,
    mode: str,
    move_files: bool = False,
    skip_unmatched: bool = False,
) -> None:
    """
    Scan source_dir recursively and copy/move matched files into output_dir,
    skipping symlinks and content-duplicate files.
    """
    processed_count = 0
    error_count = 0
    duplicate_count = 0
    seen_hashes: dict[str, Path] = {}
    duplicate_report: list[str] = []

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
            if skip_unmatched:
                continue
            category = UNMATCHED_CATEGORY

        try:
            file_hash = compute_file_hash(file_path)
        except OSError as error:
            error_count += 1
            logger.error("Failed to read '%s': %s", file_path, error)
            continue

        if file_hash in seen_hashes:
            duplicate_count += 1
            duplicate_report.append(f"{file_path}  (duplicate of {seen_hashes[file_hash]})")
            logger.info("Duplicate skipped: %s", file_path)
            continue
        seen_hashes[file_hash] = file_path

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
    if duplicate_count:
        output_dir.mkdir(parents=True, exist_ok=True)
        report_path = output_dir / "duplicates_report.txt"
        report_path.write_text("\n".join(duplicate_report), encoding="utf-8")
        logger.info("%d duplicate(s) skipped — see %s", duplicate_count, report_path)
    if error_count:
        logger.warning("%d file(s) failed and were skipped.", error_count)


def prompt_organize_mode() -> str:
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
    parser = argparse.ArgumentParser(description="Organize files by type, skip unmatched into 'other', detect duplicates.")
    parser.add_argument("--source", type=Path, default=Path("."), help="Directory to scan (default: current directory)")
    parser.add_argument("--output", type=Path, default=None, help="Output directory (default: <source>/OrganizedMedia)")
    parser.add_argument("--mode", choices=["flat", "folder"], default=None, help="Organization mode; prompted if omitted")
    parser.add_argument("--move", action="store_true", help="Move files instead of copying (asks for confirmation)")
    parser.add_argument("--skip-unmatched", action="store_true",
                         help="Skip files with unrecognized extensions instead of putting them in 'other'")
    return parser.parse_args()


def main() -> None:
    args = parse_arguments()
    source_dir = args.source.resolve()
    output_dir = (args.output or source_dir / "OrganizedMedia").resolve()
    mode = args.mode or prompt_organize_mode()

    if args.move and not confirm_move_operation():
        logger.info("Aborted by user.")
        return

    collect_media_files(source_dir, output_dir, mode=mode, move_files=args.move, skip_unmatched=args.skip_unmatched)


if __name__ == "__main__":
    main()
