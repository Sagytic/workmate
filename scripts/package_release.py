"""Create a reproducible source/build bundle without keys or private run state."""
from pathlib import Path
import os
import stat
from zipfile import ZipFile, ZIP_DEFLATED

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT.parent / "Workmate_v0.4.zip"
EXCLUDED = {"node_modules", ".venv", "__pycache__", ".pytest_cache", ".git", "work"}


def include(path):
    if not path.is_file():
        return False
    relative = path.relative_to(ROOT)
    if any(part in EXCLUDED for part in relative.parts):
        return False
    if (path.name.startswith(".env") and path.name != ".env.example") or path.name.lower() == "key.txt" or path.suffix in (".db", ".db-shm", ".db-wal", ".pyc"):
        return False
    if relative.parts[0] == "data" and relative.parts[1] not in ("fixtures", "examples"):
        return False
    if relative.parts[0] == 'verification' and relative.as_posix() != 'verification/v0.4/summary.json':
        return False  # Complete response histories stay on the development machine.
    if path.name.startswith('visual-review-') or path.suffix == '.ndjson':
        return False
    return path.is_file()


def sources():
    for directory, dirs, files in os.walk(ROOT, followlinks=False):
        # Prune before descent, including Windows junctions and symlinks.
        dirs[:] = [name for name in dirs if name not in EXCLUDED
                   and not (getattr(Path(directory, name).lstat(), 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 1024))]
        for name in files:
            path = Path(directory, name)
            if not path.is_symlink() and include(path):
                yield path


if __name__ == "__main__":
    paths = sorted(sources())
    with ZipFile(DEST, "w", ZIP_DEFLATED) as archive:
        for path in paths:
            archive.write(path, Path("workmate") / path.relative_to(ROOT))
    print(f"{DEST} | {len(paths)} files | {DEST.stat().st_size:,} bytes")
