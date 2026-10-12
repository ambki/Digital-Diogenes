from __future__ import annotations

import argparse
import io
import os
import shutil
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

# Subfolder this script owns under --path. --clean only ever removes this,
# never the --path root itself, so it's safe to point --path at a folder
# that also holds other things.
OWNED_SUBFOLDERS = ["elimination_test"]

# Buffer days added on top of --years so backdated/control mtimes land
# comfortably on the right side of DateOffset(years=...)'s cutoff in
# elimination_file_candidates.py, without needing pandas here just to match
# its exact calendar arithmetic.
BUFFER_DAYS = 400


def _days_ago(days: float) -> float:
    return (datetime.now() - timedelta(days=days)).timestamp()


def _unique_content(label: str, size_bytes: int = 512) -> bytes:
    """Content that is different every call."""
    return f"{label}\n".encode() + os.urandom(size_bytes)


def _zip_content(label: str) -> bytes:
    """Bytes of a real, valid .zip archive (not just a renamed file)."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(f"{label}.txt", _unique_content(label))
    return buffer.getvalue()


def _write_file(path: Path, content: bytes, mtime: float | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def add_old_files_fixture(
    files: dict[str, tuple[bytes, float | None]], years: int, index: int
) -> None:
    """
    Fixtures for the "old files" group: one file safely older than the
    --years cutoff, and one control file safely more recent than it (so you
    can confirm the recent one is NOT flagged).
    """
    old_ts = _days_ago(years * 365 + BUFFER_DAYS)
    recent_days = years * 365 - BUFFER_DAYS
    recent_ts = _days_ago(recent_days) if recent_days > 0 else None

    files[f"old_files/group_{index}/old_document_{index}.txt"] = (
        _unique_content(f"old-file-{index}"),
        old_ts,
    )
    files[f"old_files/group_{index}/recent_document_{index}.txt"] = (
        _unique_content(f"recent-file-{index}"),
        recent_ts,
    )


def add_zip_files_fixture(
    files: dict[str, tuple[bytes, float | None]], index: int
) -> None:
    """Fixtures for the "ZIP files" group: one real .zip and one control
    non-zip file (so you can confirm the control is NOT flagged)."""
    files[f"zip_files/archive_{index}.zip"] = (_zip_content(f"zip-file-{index}"), None)
    files[f"zip_files/not_a_zip_{index}.txt"] = (
        _unique_content(f"not-a-zip-{index}"),
        None,
    )


def add_unmixed_files_fixture(
    files: dict[str, tuple[bytes, float | None]], index: int
) -> None:
    """
    Fixtures for the "Unmixed folder" group: two files inside an `Unmixed`
    folder (one nested a level deeper, to prove recursion into it), and one
    control file in a sibling `Mixed` folder (so you can confirm it is NOT
    flagged).
    """
    files[f"imaging_project_{index}/Unmixed/channel1_{index}.tif"] = (
        _unique_content(f"unmixed-channel1-{index}"),
        None,
    )
    files[f"imaging_project_{index}/Unmixed/nested/channel2_{index}.tif"] = (
        _unique_content(f"unmixed-channel2-{index}"),
        None,
    )
    files[f"imaging_project_{index}/Mixed/channel3_{index}.tif"] = (
        _unique_content(f"mixed-channel3-{index}"),
        None,
    )


def add_overlap_fixture(
    files: dict[str, tuple[bytes, float | None]], years: int, index: int
) -> None:
    """
    A single file that qualifies for all three groups at once (old + .zip +
    inside an Unmixed folder), so --mode all also exercises the "unique vs.
    summed" overlap accounting in the global report.
    """
    old_ts = _days_ago(years * 365 + BUFFER_DAYS)
    files[f"overlap_project_{index}/Unmixed/old_archive_{index}.zip"] = (
        _zip_content(f"overlap-{index}"),
        old_ts,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Create a folder structure with known old files, .zip files, and "
            "'Unmixed' folder files, for testing elimination_file_candidates.py."
        )
    )
    parser.add_argument(
        "--path",
        required=True,
        help="Base folder where the test folder will be created.",
    )
    parser.add_argument(
        "--mode",
        choices=["all", "old", "zip", "unmixed"],
        default="all",
        help=(
            "Which elimination_file_candidates.py group to build a scenario "
            "for (default: all, builds every group plus a file overlapping "
            "all three)."
        ),
    )
    parser.add_argument(
        "--years",
        type=int,
        default=10,
        help=(
            "Age threshold (in years) to build fixtures around. Should match "
            "the --years you'll pass to elimination_file_candidates.py "
            "(default: 10)."
        ),
    )
    parser.add_argument(
        "--num-files",
        type=int,
        default=1,
        help=(
            "Multiplier applied to every fixture group, preserving their "
            "proportions while scaling up total file count. Default: 1."
        ),
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help=(
            "Remove this script's previously generated subfolder "
            "(elimination_test) under --path before regenerating. Never "
            "touches anything else under --path."
        ),
    )
    args = parser.parse_args()

    if args.years < 1:
        parser.error("--years must be a positive integer.")
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

    files: dict[str, tuple[bytes, float | None]] = {}

    for index in range(1, args.num_files + 1):
        if args.mode in {"all", "old"}:
            add_old_files_fixture(files, args.years, index)
        if args.mode in {"all", "zip"}:
            add_zip_files_fixture(files, index)
        if args.mode in {"all", "unmixed"}:
            add_unmixed_files_fixture(files, index)
        if args.mode == "all":
            add_overlap_fixture(files, args.years, index)

    root = base / "elimination_test"
    for rel_path, (content, mtime) in files.items():
        _write_file(root / rel_path, content, mtime)

    print(f"\nCreated elimination-candidates scenario at: {root}  ({len(files)} files)")
    print(
        "  Run: python elimination_file_candidates.py "
        f'--folder "{root}" --years {args.years}'
    )
    print(
        "\nNote: control files are included alongside each group (a recent "
        "file, a non-.zip file, and a file outside any Unmixed folder) so "
        "you can confirm the script does NOT flag them."
    )


if __name__ == "__main__":
    main()
