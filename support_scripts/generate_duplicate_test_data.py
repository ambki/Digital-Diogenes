from __future__ import annotations

import argparse
import os
import shutil
from pathlib import Path

# Subfolders this script owns under --path. --clean only ever removes these,
# never the --path root itself, so it's safe to point --path at a folder
# that also holds other things.
OWNED_SUBFOLDERS = ["folder_test", "origin", "destination"]


def _unique_content(label: str, size_bytes: int = 512) -> bytes:
    """Content that is different every call (for MISMATCH/MISSING/OK fixtures)."""
    return f"{label}\n".encode() + os.urandom(size_bytes)


def _content_block(label: str, size_bytes: int = 4096) -> bytes:
    """Deterministic content reused byte-for-byte to create real duplicates."""
    pattern = f"{label}\n".encode()
    repeats = size_bytes // len(pattern) + 1
    return (pattern * repeats)[:size_bytes]


def _write_file(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def build_folder_test_files(large_file_mb: int, num_files: int) -> dict[str, bytes]:
    """Fixtures for `duplicates_check.py --folder ... --mode folder`.

    num_files repeats the whole fixture set (2 unique + 3-file duplicate
    group + large duplicate pair = 7 files per repetition) under a unique
    subfolder each time, keeping the proportions between them fixed while
    scaling up total file count.
    """
    files: dict[str, bytes] = {}

    for index in range(1, num_files + 1):
        files[f"unique_{index}_a.txt"] = _unique_content(f"folder-unique-{index}-a")
        files[f"unique_{index}_b.dat"] = _unique_content(f"folder-unique-{index}-b")

        dup_content = _content_block(f"folder-duplicate-group-{index}")
        files[f"duplicates/group_{index}/original.bin"] = dup_content
        files[f"duplicates/group_{index}/copy_1/duplicate.bin"] = dup_content
        files[f"duplicates/group_{index}/copy_2/nested/duplicate_again.bin"] = dup_content

        large_content = os.urandom(large_file_mb * 1024 * 1024)
        files[f"large_duplicates/group_{index}/big_original.bin"] = large_content
        files[f"large_duplicates/group_{index}/copy/big_copy.bin"] = large_content

    return files


def add_baseline(origin: dict[str, bytes], destination: dict[str, bytes]) -> None:
    """A file identical on both sides so origin/destination are never empty."""
    content = _unique_content("baseline-readme")
    origin["README.txt"] = content
    destination["README.txt"] = content


def add_origin_internal_duplicates(origin: dict[str, bytes], index: int) -> None:
    content = _content_block(f"origin-duplicate-group-{index}")
    origin[f"duplicates/group_{index}/file_a.bin"] = content
    origin[f"duplicates/group_{index}/file_b.bin"] = content
    origin[f"duplicates/group_{index}/nested/file_c.bin"] = content


def add_destination_internal_duplicates(
    destination: dict[str, bytes], index: int
) -> None:
    content = _content_block(f"destination-duplicate-group-{index}")
    destination[f"dest_duplicates/group_{index}/file_x.bin"] = content
    destination[f"dest_duplicates/group_{index}/file_y.bin"] = content


def add_cross_folder_moved_duplicate(
    origin: dict[str, bytes], destination: dict[str, bytes], index: int
) -> None:
    """
    Same content on both sides but at different relative paths.

    `--mode between` matches by content and will find this pair.
    `--mode integrity` matches by relative path and will instead report
    the origin file as MISSING IN DEST, even though its content did make
    it into the destination under a different path.
    """
    content = _content_block(f"moved-file-cross-folder-duplicate-{index}")
    origin[f"moved/original_location_{index}.bin"] = content
    destination[f"renamed_elsewhere/relocated_copy_{index}.bin"] = content


def add_integrity_ok(
    origin: dict[str, bytes], destination: dict[str, bytes], index: int
) -> None:
    content = _unique_content(f"integrity-ok-{index}")
    origin[f"docs/report_{index}.txt"] = content
    destination[f"docs/report_{index}.txt"] = content


def add_integrity_mismatch(
    origin: dict[str, bytes], destination: dict[str, bytes], index: int
) -> None:
    origin[f"docs/edited_after_backup_{index}.txt"] = _unique_content(
        f"integrity-mismatch-origin-{index}"
    )
    destination[f"docs/edited_after_backup_{index}.txt"] = _unique_content(
        f"integrity-mismatch-destination-{index}"
    )


def add_integrity_missing(origin: dict[str, bytes], index: int) -> None:
    """Only added to origin, so the destination is missing it entirely."""
    origin[f"docs/never_copied_{index}.txt"] = _unique_content(f"integrity-missing-{index}")


def add_large_duplicate_pair(
    origin: dict[str, bytes], destination: dict[str, bytes], size_mb: int, index: int
) -> None:
    """An identical multi-MB file on both sides, to exercise the progress
    bar and --threading with something that takes real time to hash."""
    content = os.urandom(size_mb * 1024 * 1024)
    origin[f"large/big_file_{index}.bin"] = content
    destination[f"large/big_file_{index}.bin"] = content


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Create a folder structure with known duplicates, mismatches, "
            "and missing files, for testing duplicates_check.py."
        )
    )
    parser.add_argument(
        "--path",
        required=True,
        help="Base folder where the test folders will be created.",
    )
    parser.add_argument(
        "--mode",
        choices=["all", "folder", "origin", "destination", "between", "integrity"],
        default="all",
        help=(
            "Which duplicates_check.py check to build a scenario for "
            "(default: all, builds every scenario at once)."
        ),
    )
    parser.add_argument(
        "--large-file-mb",
        type=int,
        default=20,
        help=(
            "Size in MB of each large duplicated file pair used to exercise "
            "the progress bar and --threading (default: 20)."
        ),
    )
    parser.add_argument(
        "--num-files",
        type=int,
        default=1,
        help=(
            "Multiplier applied to every fixture group (duplicate groups, "
            "moved/OK/mismatch/missing files, large pairs), preserving their "
            "proportions while scaling up total file count. Default: 1."
        ),
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help=(
            "Remove this script's previously generated subfolders "
            "(folder_test, origin, destination) under --path before "
            "regenerating. Never touches anything else under --path."
        ),
    )
    args = parser.parse_args()

    if args.large_file_mb < 1:
        parser.error("--large-file-mb must be a positive integer.")
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

    if args.mode in {"all", "folder"}:
        folder_test_root = base / "folder_test"
        for rel_path, content in build_folder_test_files(
            args.large_file_mb, args.num_files
        ).items():
            _write_file(folder_test_root / rel_path, content)

        print(f"\nCreated folder-mode scenario at: {folder_test_root}")
        print(
            "  Run: python duplicates_check.py "
            f'--folder "{folder_test_root}" --mode folder --threading --workers 8'
        )

    if args.mode != "folder":
        origin_files: dict[str, bytes] = {}
        destination_files: dict[str, bytes] = {}

        add_baseline(origin_files, destination_files)

        for index in range(1, args.num_files + 1):
            if args.mode in {"all", "origin"}:
                add_origin_internal_duplicates(origin_files, index)
            if args.mode in {"all", "destination"}:
                add_destination_internal_duplicates(destination_files, index)
            if args.mode in {"all", "between"}:
                add_cross_folder_moved_duplicate(origin_files, destination_files, index)
            if args.mode in {"all", "integrity"}:
                add_integrity_ok(origin_files, destination_files, index)
                add_integrity_mismatch(origin_files, destination_files, index)
                add_integrity_missing(origin_files, index)
            if args.mode == "all":
                add_large_duplicate_pair(
                    origin_files, destination_files, args.large_file_mb, index
                )

        origin_root = base / "origin"
        destination_root = base / "destination"
        for rel_path, content in origin_files.items():
            _write_file(origin_root / rel_path, content)
        for rel_path, content in destination_files.items():
            _write_file(destination_root / rel_path, content)

        print("\nCreated origin/destination scenario:")
        print(f"  Origin:      {origin_root}  ({len(origin_files)} files)")
        print(f"  Destination: {destination_root}  ({len(destination_files)} files)")
        print(
            "  Run: python duplicates_check.py "
            f'--origin "{origin_root}" --destination "{destination_root}" '
            f"--mode {args.mode} --threading --workers 8"
        )

    print(
        "\nNote: UNREADABLE status is not simulated here (it requires OS-level "
        "permission changes) - the file's status column just won't show it."
    )


if __name__ == "__main__":
    main()
