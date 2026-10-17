from __future__ import annotations

import argparse
import os
import shutil
from datetime import datetime, timedelta
from pathlib import Path

# Subfolder this script owns under --path. --clean only ever removes this,
# never the --path root itself.
OWNED_SUBFOLDERS = ["archive_test"]

# Fixture dates, all far from CUTOFF so time zones never matter.
CUTOFF = "2015-01-01"
OLDEST = datetime(2008, 3, 1).timestamp()
OLD = datetime(2010, 6, 1).timestamp()
RECENT = (datetime.now() - timedelta(days=30)).timestamp()

QUARANTINE_NOTE = "original_file_location.txt"
QUARANTINE_HEADER = (
    "# Duplicate files moved out of this folder by quarantine_duplicates.py. "
    "Each line: the moved file, where it is now, and the identical copy that was kept.\n"
)


def _content(label: str, size_bytes: int = 512) -> bytes:
    """Content that is different every call."""
    return f"{label}\n".encode() + os.urandom(size_bytes)


def _write_file(path: Path, content: bytes, mtime: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    os.utime(path, (mtime, mtime))


def add_fixtures(files: dict[str, tuple[bytes, float]], root: Path, index: int, with_duplicates_folder: bool) -> None:
    """
    One of each scenario (paths relative to the storage root):

    - old_project_N: every file old -> all moved, folder removed; its recent
      desktop.ini follows the folder; a removed_folder line goes in the root note.
    - mixed_project_N: an old and a recent file -> the old one moves, the folder
      stays with a note; its all-old subfolder old_sub is removed.
    - dup_groups_N: one md5 in three places: A (oldest, the keeper), B (old
      duplicate), C (recent duplicate) -> exercises --categories.
    - quarantined_N: holds only a quarantine note pointing at dup_groups_N\\A
      -> moved and removed (or kept if the duplicates folder still exists),
      and gets a keeper_archived line when A is archived.
    - repo_partial_N\\.git: one old, one recent file -> GIT_PARTIAL, nothing moves.
    - repo_old_N: everything old, .git included -> moves whole.
    - unknown_N: a file with mtime 1970-01-01 -> MTIME_UNKNOWN.
    """
    n = index
    files[f"old_project_{n}/raw/data_{n}.bin"] = (_content(f"old-data-{n}"), OLD)
    files[f"old_project_{n}/raw/desktop.ini"] = (b"[.ShellClassInfo]\r\n", RECENT)
    files[f"old_project_{n}/report_{n}.txt"] = (_content(f"old-report-{n}"), OLD)

    files[f"mixed_project_{n}/old_{n}.bin"] = (_content(f"mixed-old-{n}"), OLD)
    files[f"mixed_project_{n}/recent_{n}.bin"] = (_content(f"mixed-recent-{n}"), RECENT)
    files[f"mixed_project_{n}/old_sub/deep_{n}.bin"] = (_content(f"mixed-deep-{n}"), OLD)

    shared = _content(f"duplicate-{n}")
    files[f"dup_groups_{n}/A/scan_{n}.tif"] = (shared, OLDEST)
    files[f"dup_groups_{n}/B/scan_{n}.tif"] = (shared, OLD)
    files[f"dup_groups_{n}/C/scan_{n}.tif"] = (shared, RECENT)

    note = QUARANTINE_HEADER + (
        f"moved_file: scan_{n}.tif | now_at: {root / 'duplicates' / f'quarantined_{n}' / f'scan_{n}.tif'} | "
        f"original_kept_at: {root / f'dup_groups_{n}' / 'A' / f'scan_{n}.tif'} | md5: 0 | moved_on: 2026-01-01 10:00:00\n"
    )
    files[f"quarantined_{n}/{QUARANTINE_NOTE}"] = (note.encode(), RECENT)
    if with_duplicates_folder:
        files[f"duplicates/quarantined_{n}/scan_{n}.tif"] = (shared, OLD)

    files[f"repo_partial_{n}/.git/objects/old_{n}"] = (_content(f"git-old-{n}"), OLD)
    files[f"repo_partial_{n}/.git/HEAD"] = (_content(f"git-head-{n}"), RECENT)

    files[f"repo_old_{n}/.git/objects/obj_{n}"] = (_content(f"git-obj-{n}"), OLD)
    files[f"repo_old_{n}/readme_{n}.txt"] = (_content(f"repo-readme-{n}"), OLD)

    files[f"unknown_{n}/zero_time_{n}.bin"] = (_content(f"zero-{n}"), 0.0)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create a storage folder with known old/recent files, duplicates, quarantine notes "
                    "and .git folders, for testing archive_old_files.py."
    )
    parser.add_argument("--path", required=True, help="Base folder where the test folder will be created.")
    parser.add_argument(
        "--num-files", type=int, default=1,
        help="Multiplier applied to every fixture, keeping their proportions. Default: 1.",
    )
    parser.add_argument(
        "--with-duplicates-folder", action="store_true",
        help="Also create <storage>\\duplicates (a quarantine that isn't finalized yet), so folders "
             "holding a quarantine note are kept (QUARANTINE_NOT_FINALIZED).",
    )
    parser.add_argument(
        "--clean", action="store_true",
        help="Remove this script's previously generated subfolder (archive_test) under --path first. "
             "Never touches anything else under --path.",
    )
    args = parser.parse_args()
    if args.num_files < 1:
        parser.error("--num-files must be a positive integer.")
    return args


def main() -> None:
    args = parse_args()
    base = Path(args.path).resolve()
    base.mkdir(parents=True, exist_ok=True)
    if args.clean:
        for name in OWNED_SUBFOLDERS:
            target = base / name
            if target.exists():
                shutil.rmtree(target)
                print(f"Removed existing {target}")

    root = base / "archive_test" / "storage"
    files: dict[str, tuple[bytes, float]] = {}
    for index in range(1, args.num_files + 1):
        add_fixtures(files, root, index, args.with_duplicates_folder)
    for rel_path, (content, mtime) in files.items():
        _write_file(root / rel_path, content, mtime)

    reports = base / "archive_test" / "reports"
    print(f"\nCreated archive scenario at: {root}  ({len(files)} files)")
    print(
        "  Plan (scans first):  python archive_old_files.py plan "
        f'--root "{root}" --output "{reports}" --before {CUTOFF} --categories all --archive-label TEST-0001'
    )
    print("  Then:               python archive_old_files.py execute --plan <the archive_plan_*.csv it wrote>")
    print(
        "\nExpected with --categories all: old_project_N and old_sub removed; mixed_project_N keeps its recent "
        "file and gets a note; dup_groups_N A and B moved, C stays; quarantined_N's note moves and gets a "
        "keeper_archived line (unless --with-duplicates-folder); repo_partial_N untouched (GIT_PARTIAL); "
        "repo_old_N moved whole; unknown_N skipped (MTIME_UNKNOWN)."
    )


if __name__ == "__main__":
    main()
