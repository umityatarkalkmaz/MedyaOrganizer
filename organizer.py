import os
import shutil
from pathlib import Path

# === User Configurable ===

# Define categories and their associated file extensions
file_categories = {
    "photo": [".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"],
    "video": [".mp4", ".mov", ".avi", ".mkv", ".webm"],
    "document": [".pdf", ".docx", ".xlsx", ".txt"]
}

# Directory to scan (current directory)
source_dir = Path(".").resolve()

# Output base directory
output_dir = source_dir / "OrganizedMedia"

# === Script Logic ===

def get_unique_path(path: Path) -> Path:
    """Generate a unique file path if a file with the same name already exists."""
    counter = 1
    new_path = path
    while new_path.exists():
        new_path = path.with_name(f"{path.stem}_{counter}{path.suffix}")
        counter += 1
    return new_path

def ask_organize_mode():
    """Ask user how to organize files: flat or folder."""
    while True:
        choice = input("How would you like to organize files? [flat/folder]: ").strip().lower()
        if choice in {"flat", "folder"}:
            return choice
        print("Invalid choice. Please enter 'flat' or 'folder'.")

def collect_files(mode="flat", move_files=False):
    """
    Recursively scan source_dir and collect files based on file_categories.

    Args:
        mode (str): 'flat' for single folder, 'folder' for categorized folders.
        move_files (bool): Whether to move files instead of copying.
    """
    count = 0
    for root, _, files in os.walk(source_dir):
        for file_name in files:
            file_path = Path(root) / file_name

            # Skip files already inside the output directory
            if output_dir in file_path.parents:
                continue

            ext = file_path.suffix.lower()
            target_path = None

            for category, extensions in file_categories.items():
                if ext in extensions:
                    if mode == "folder":
                        target_subdir = output_dir / category
                    else:  # flat mode
                        target_subdir = output_dir
                    target_subdir.mkdir(parents=True, exist_ok=True)
                    target_path = get_unique_path(target_subdir / file_path.name)
                    break

            if target_path:
                if move_files:
                    shutil.move(str(file_path), target_path)
                else:
                    shutil.copy2(str(file_path), target_path)
                count += 1

    print(f"{count} file(s) {'moved' if move_files else 'copied'} to '{output_dir}'.")

# === Entry Point ===

if __name__ == "__main__":
    print("Media Collecting...")
    organize_mode = ask_organize_mode()
    collect_files(mode=organize_mode, move_files=False)
