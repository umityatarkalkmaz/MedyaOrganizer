"""Cross-platform file organizer: sorts by type, skips unmatched into 'other',
and detects true content-based duplicates via SHA-256.

Security model: the source tree is treated as UNTRUSTED input (it may be a shared
directory, a downloads folder, or an extracted archive). Every filesystem operation
therefore refuses to traverse symlinks, and destination names are claimed atomically
with O_CREAT|O_EXCL so a planted symlink can never redirect a write outside the
output directory.
"""

import argparse
import errno
import hashlib
import logging
import os
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
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
COPY_CHUNK_SIZE = 1024 * 1024  # 1 MB
REPORT_FILENAME = "duplicates_report.txt"

# Permission bits copied to the destination. Deliberately excludes setuid/setgid/sticky
# (0o7000) so organizing a tree can never reproduce a privileged binary.
SAFE_PERMISSION_MASK = 0o0777

EXTENSION_TO_CATEGORY: dict[str, str] = {
    ext: category for category, exts in FILE_CATEGORIES.items() for ext in exts
}

# Control characters are escaped before a filename reaches a log line or the report,
# so a crafted name cannot forge report rows or emit terminal escape sequences.
_CONTROL_CHAR_MAP = {c: f"\\x{c:02x}" for c in range(0x20)} | {0x7F: "\\x7f"}

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)


class UnsafePathError(Exception):
    """Raised when a path fails a safety precondition (symlink, non-regular file, ...)."""


def display_path(path: Path | str) -> str:
    """Render a path for logs/reports with control characters escaped."""
    return os.fsdecode(path).translate(_CONTROL_CHAR_MAP)


@dataclass
class ProcessedFile:
    """A file already placed in the output tree.

    ``final_path`` is the destination, not the source: under ``--move`` the source is
    gone, so a deferred hash must read the copy we control.
    """

    final_path: Path
    digest: bytes | None = None  # computed lazily, only when a size collision demands it


# === Safe filesystem primitives ===


def open_regular_file(path: Path) -> int:
    """Open a file read-only, refusing symlinks and anything that is not a regular file.

    O_NOFOLLOW closes the window where a checked path is swapped for a symlink before
    it is opened; the fstat guard rejects fifos and device nodes, which can block or
    stream unbounded data.
    """
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise UnsafePathError(f"not a regular file: {display_path(path)}")
    except BaseException:
        os.close(fd)
        raise
    return fd


def digest_fd(fd: int) -> bytes:
    """SHA-256 of an open file, read in chunks. Returns raw bytes (32B, not 64B hex)."""
    hasher = hashlib.sha256()
    os.lseek(fd, 0, os.SEEK_SET)
    while chunk := os.read(fd, HASH_CHUNK_SIZE):
        hasher.update(chunk)
    os.lseek(fd, 0, os.SEEK_SET)
    return hasher.digest()


def digest_path(path: Path) -> bytes:
    fd = open_regular_file(path)
    try:
        return digest_fd(fd)
    finally:
        os.close(fd)


def copy_fd(source_fd: int, target_fd: int, size: int) -> None:
    """Copy source_fd to target_fd, preferring the kernel's zero-copy path."""
    os.lseek(source_fd, 0, os.SEEK_SET)

    if hasattr(os, "copy_file_range"):
        try:
            remaining = size
            while remaining > 0:
                copied = os.copy_file_range(source_fd, target_fd, remaining)
                if copied == 0:
                    break
                remaining -= copied
            return
        except OSError:
            # Not supported on this filesystem pair - restart with a portable copy.
            os.lseek(source_fd, 0, os.SEEK_SET)
            os.lseek(target_fd, 0, os.SEEK_SET)
            os.ftruncate(target_fd, 0)

    while chunk := os.read(source_fd, COPY_CHUNK_SIZE):
        while chunk:
            chunk = chunk[os.write(target_fd, chunk):]


def apply_safe_metadata(target_path: Path, source_stat: os.stat_result) -> None:
    """Mirror timestamps and permissions, minus setuid/setgid/sticky."""
    os.chmod(target_path, stat.S_IMODE(source_stat.st_mode) & SAFE_PERMISSION_MASK)
    os.utime(target_path, ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns))


def reserve_target_path(target_path: Path, counters: dict[tuple[Path, str, str], int]) -> tuple[Path, int]:
    """Atomically claim a free destination name and return it with an open write fd.

    O_CREAT|O_EXCL|O_NOFOLLOW makes the claim fail on *any* existing entry, including a
    dangling symlink - which os.path.exists() reports as absent, and which a plain
    open() would happily follow outside the output directory.

    The counter is memoized per (dir, stem, suffix) so n colliding names cost ~n
    syscalls instead of n(n+1)/2.
    """
    key = (target_path.parent, target_path.stem, target_path.suffix)
    counter = counters.get(key, 0)

    while True:
        candidate = (target_path if counter == 0
                     else target_path.with_name(f"{target_path.stem}_{counter}{target_path.suffix}"))
        try:
            fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            counter += 1
            continue
        counters[key] = counter + 1
        return candidate, fd


def ensure_target_directory(
    output_dir: Path,
    category: str,
    mode: str,
    created: set[Path],
) -> Path:
    """Return the category directory, creating it at most once and rejecting symlinks."""
    target_dir = output_dir / category if mode == "folder" else output_dir
    if target_dir in created:
        return target_dir

    target_dir.mkdir(parents=True, exist_ok=True)
    if not stat.S_ISDIR(os.lstat(target_dir).st_mode):
        # mkdir(exist_ok=True) succeeds on a symlink pointing at a directory.
        raise UnsafePathError(f"target directory is a symlink: {display_path(target_dir)}")

    created.add(target_dir)
    return target_dir


def iter_source_files(source_dir: Path, output_dir: Path) -> Iterator[tuple[Path, os.stat_result]]:
    """Yield (path, lstat) for regular files under source_dir.

    Prunes the output subtree at descent time rather than filtering after the fact, so a
    re-run does not re-enumerate everything the previous run wrote. Symlinks are never
    followed, for files or directories.
    """
    output_str = str(output_dir)
    stack = [str(source_dir)]

    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_symlink():
                            logger.warning("Skipped symlink (not followed): %s", display_path(entry.path))
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            if entry.path != output_str:
                                stack.append(entry.path)
                            continue
                        entry_stat = entry.stat(follow_symlinks=False)
                        if stat.S_ISREG(entry_stat.st_mode):
                            yield Path(entry.path), entry_stat
                    except OSError as error:
                        logger.error("Failed to inspect '%s': %s", display_path(entry.path), error)
        except OSError as error:
            logger.error("Failed to scan '%s': %s", display_path(current), error)


class DuplicateReport:
    """Append-only duplicate log, opened lazily on the first duplicate."""

    def __init__(self, output_dir: Path) -> None:
        self._output_dir = output_dir
        self._handle = None
        self.count = 0

    def add(self, duplicate: Path, original: Path) -> None:
        if self._handle is None:
            self._output_dir.mkdir(parents=True, exist_ok=True)
            # O_NOFOLLOW: never write through a symlink planted at the report path.
            fd = os.open(self._output_dir / REPORT_FILENAME,
                         os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
            self._handle = os.fdopen(fd, "a", encoding="utf-8")
            timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
            self._handle.write(f"# run {timestamp}\n")

        self.count += 1
        self._handle.write(f"{display_path(duplicate)}  (duplicate of {display_path(original)})\n")

    @property
    def path(self) -> Path:
        return self._output_dir / REPORT_FILENAME

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None


# === Main routine ===


def find_duplicate(bucket: list[ProcessedFile], incoming: bytes) -> ProcessedFile | None:
    """Return the bucket entry matching `incoming`, hashing entries only as needed."""
    for entry in bucket:
        if entry.digest is None:
            entry.digest = digest_path(entry.final_path)
        if entry.digest == incoming:
            return entry
    return None


def place_file(
    source_path: Path,
    source_stat: os.stat_result,
    target_path: Path,
    target_fd: int,
    move_files: bool,
    source_fd: int | None,
) -> None:
    """Write source into the already-reserved target, then apply safe metadata."""
    if move_files and source_fd is None:
        # Nothing needed the content, so rename straight over our placeholder.
        # rename() replaces the destination entry itself and never follows a symlink.
        os.close(target_fd)
        try:
            os.rename(source_path, target_path)
        except OSError as error:
            if error.errno != errno.EXDEV:  # different filesystems: fall back to a copy
                raise
            fd = open_regular_file(source_path)
            try:
                fallback_fd = os.open(target_path, os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW)
                try:
                    copy_fd(fd, fallback_fd, source_stat.st_size)
                finally:
                    os.close(fallback_fd)
            finally:
                os.close(fd)
            os.unlink(source_path)
    else:
        # Reuse the fd the hash was computed from, so the bytes written are provably the
        # bytes that were hashed - a swap of the source path in between cannot desync them.
        # Note: an fd passed in by the caller stays the caller's to close.
        opened_here = source_fd is None
        fd = None
        try:
            fd = open_regular_file(source_path) if opened_here else source_fd
            copy_fd(fd, target_fd, source_stat.st_size)
        finally:
            os.close(target_fd)
            if opened_here and fd is not None:
                os.close(fd)
        if move_files:
            os.unlink(source_path)

    apply_safe_metadata(target_path, source_stat)


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
    size_index: dict[int, list[ProcessedFile]] = {}
    counters: dict[tuple[Path, str, str], int] = {}
    created_dirs: set[Path] = set()

    source_dir = source_dir.resolve()
    output_dir = output_dir.resolve()
    report = DuplicateReport(output_dir)

    try:
        for file_path, file_stat in iter_source_files(source_dir, output_dir):
            category = EXTENSION_TO_CATEGORY.get(file_path.suffix.lower())
            if category is None:
                if skip_unmatched:
                    continue
                category = UNMATCHED_CATEGORY

            bucket = size_index.setdefault(file_stat.st_size, [])
            source_fd: int | None = None
            incoming_digest: bytes | None = None

            try:
                # Files with a size nothing else shares cannot be duplicates, so they are
                # never opened or hashed. Only a size collision justifies reading bytes.
                if bucket:
                    source_fd = open_regular_file(file_path)
                    incoming_digest = digest_fd(source_fd)
                    original = find_duplicate(bucket, incoming_digest)
                    if original is not None:
                        report.add(file_path, original.final_path)
                        logger.info("Duplicate skipped: %s", display_path(file_path))
                        continue

                target_dir = ensure_target_directory(output_dir, category, mode, created_dirs)
                target_path, target_fd = reserve_target_path(target_dir / file_path.name, counters)

                try:
                    place_file(file_path, file_stat, target_path, target_fd, move_files, source_fd)
                except BaseException:
                    try:
                        os.unlink(target_path)  # do not leave an empty placeholder behind
                    except OSError:
                        pass
                    raise

                processed_count += 1
                bucket.append(ProcessedFile(final_path=target_path, digest=incoming_digest))
            except (OSError, UnsafePathError) as error:
                error_count += 1
                action = "move" if move_files else "copy"
                logger.error("Failed to %s '%s': %s", action, display_path(file_path), error)
            finally:
                if source_fd is not None:
                    try:
                        os.close(source_fd)
                    except OSError:
                        pass
    finally:
        report.close()

    action = "moved" if move_files else "copied"
    logger.info("%d file(s) %s to '%s'.", processed_count, action, display_path(output_dir))
    if report.count:
        logger.info("%d duplicate(s) skipped - see %s", report.count, display_path(report.path))
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
    answer = input("This will MOVE (not copy) files - this cannot be undone. Continue? [y/N]: ")
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

    if output_dir == source_dir:
        logger.error(
            "--output must differ from --source (every file would be treated as existing output). "
            "Try --output %s", display_path(source_dir / "OrganizedMedia"),
        )
        return

    mode = args.mode or prompt_organize_mode()

    if args.move and not confirm_move_operation():
        logger.info("Aborted by user.")
        return

    collect_media_files(source_dir, output_dir, mode=mode, move_files=args.move, skip_unmatched=args.skip_unmatched)


if __name__ == "__main__":
    main()
