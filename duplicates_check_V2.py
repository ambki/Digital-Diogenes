from __future__ import annotations

import argparse
import os
from collections.abc import Callable
from pathlib import Path

import pandas as pd

# The scanning and export helpers live in diogenes_common.py, which must sit
# in the same folder as this script.
from diogenes_common import (
    DEFAULT_IO_TIMEOUT_SECONDS,
    _normalize_filter_paths,
    _stringify_object_columns,
    build_raw_file_export,
    build_skipped_folders_sheet,
    raw_export_path,
    scan_folder_md5,
    validate_folder,
    write_csv,
)


def from_long_path(path: str) -> str:
    """Inverse of to_long_path, for showing/comparing paths."""
    if path.startswith("\\\\?\\UNC\\"):
        return "\\\\" + path[8:]
    if path.startswith("\\\\?\\"):
        return path[4:]
    return path


def _path_prefix_matcher(root: str | Path) -> Callable[[str], bool]:
    """
    Return a test for "is this path string under `root`?". Pure string
    comparison (abspath/normcase don't touch the disk), not Path.resolve()
    per file: that's a filesystem call with no timeout, millions of times
    over, and allocates a Path per file. Note it can't tell that a mapped
    drive (Z:\\...) and its UNC path (\\\\server\\share\\...) are the same place.
    """
    prefix = os.path.normcase(os.path.abspath(root)).rstrip("\\/") + os.sep
    return lambda p: os.path.normcase(os.path.abspath(p)).startswith(prefix)


def find_real_duplicates(df: pd.DataFrame) -> pd.DataFrame:
    """Return rows whose MD5 appears more than once, excluding unreadable files."""
    if df.empty:
        return df.copy()

    df_valid = df[df["md5"].notna()].copy()
    return df_valid[df_valid.duplicated(subset=["md5"], keep=False)].sort_values(
        ["md5", "path"]
    )


def _oldest_per_md5(
    df: pd.DataFrame, path_column: str = "path", extra_columns: list[str] | None = None
) -> pd.DataFrame:
    """
    For each md5 group in df, return the row with the smallest mtime
    (the oldest copy), as columns: md5, oldest_path, oldest_modified,
    plus any extra_columns renamed with an "oldest_" prefix.
    """
    extra_columns = extra_columns or []
    oldest_idx = df.groupby("md5")["mtime"].idxmin()
    oldest = df.loc[oldest_idx, ["md5", path_column, "mtime", *extra_columns]].copy()
    oldest["oldest_modified"] = pd.to_datetime(oldest["mtime"], unit="s").dt.strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    rename_map = {path_column: "oldest_path", **{c: f"oldest_{c}" for c in extra_columns}}
    return oldest.rename(columns=rename_map).drop(columns=["mtime"])


def build_duplicate_summary(df_dupes: pd.DataFrame) -> pd.DataFrame:
    """
    Summarize duplicate rows by MD5 hash. Includes oldest_path/oldest_modified
    for the oldest copy in each group (by modification time) — the one to
    keep, so the rest can be deleted.
    """
    if df_dupes.empty:
        return pd.DataFrame(
            columns=[
                "md5",
                "count",
                "size_bytes",
                "total_size_bytes",
                "size_gb",
                "total_size_gb",
                "wasted_gb",
                "filenames",
                "paths",
                "oldest_path",
                "oldest_modified",
            ]
        )

    summary = (
        df_dupes.groupby("md5")
        .agg(
            count=("path", "count"),
            size_bytes=("size_bytes", "first"),
            total_size_bytes=("size_bytes", "sum"),
            filenames=("filename", lambda values: list(values)),
            paths=("path", lambda values: list(values)),
        )
        .reset_index()
    )
    summary["size_gb"] = summary["size_bytes"] / (1024 ** 3)
    summary["total_size_gb"] = summary["total_size_bytes"] / (1024 ** 3)
    summary["wasted_gb"] = (
        summary["total_size_bytes"] - summary["size_bytes"]
    ) / (1024 ** 3)

    summary = summary.merge(_oldest_per_md5(df_dupes), on="md5", how="left")

    return summary.sort_values(["wasted_gb", "count"], ascending=[False, False])


def compare_folders_by_relative_path(
    df_origin: pd.DataFrame,
    df_destination: pd.DataFrame,
    origin_folder: str | Path,
    destination_folder: str | Path,
) -> pd.DataFrame:
    """
    Compare origin and destination folders using relative paths and MD5 hashes.
    Takes already-scanned DataFrames (see scan_folder_md5) to avoid re-hashing.
    Returns one row per origin file with status:
    OK, MISMATCH, UNREADABLE, or MISSING IN DEST.
    """
    # Vectorized (string prefix strip + merge) rather than iterrows() +
    # Path.relative_to() per row, which crawls at millions of files. Every
    # scanned path is built as str(Path(root) / ...), so it starts with
    # str(root) exactly.
    def _relative(df: pd.DataFrame, root: str | Path) -> pd.DataFrame:
        prefix_len = len(str(Path(root)))
        return pd.DataFrame({
            "relative_path": df["path"].str.slice(prefix_len).str.lstrip("\\/"),
            "md5": df["md5"],
            "size_bytes": df["size_bytes"],
        })

    if df_origin.empty:
        return pd.DataFrame(
            columns=["relative_path", "origin_md5", "dest_md5", "size_gb", "status"]
        )

    origin_rel = _relative(df_origin, origin_folder).drop_duplicates("relative_path")
    destination_rel = (
        _relative(df_destination, destination_folder)
        .drop_duplicates("relative_path")[["relative_path", "md5"]]
        .rename(columns={"md5": "dest_md5"})
    )

    merged = origin_rel.rename(columns={"md5": "origin_md5"}).merge(
        destination_rel, on="relative_path", how="left", indicator=True
    )
    in_dest = merged["_merge"] == "both"
    both_readable = merged["origin_md5"].notna() & merged["dest_md5"].notna()
    same_md5 = merged["origin_md5"] == merged["dest_md5"]

    merged["status"] = "UNREADABLE"
    merged.loc[in_dest & both_readable & same_md5, "status"] = "OK"
    merged.loc[in_dest & both_readable & ~same_md5, "status"] = "MISMATCH"
    merged.loc[~in_dest, "status"] = "MISSING IN DEST"
    merged["dest_md5"] = merged["dest_md5"].where(in_dest, None)
    merged["size_gb"] = merged["size_bytes"] / (1024 ** 3)

    return merged[
        ["relative_path", "origin_md5", "dest_md5", "size_gb", "status"]
    ].sort_values(["status", "relative_path"])


def compare_cross_folder_duplicates(
    df_origin: pd.DataFrame, df_destination: pd.DataFrame
) -> pd.DataFrame:
    """
    Find files present in both folders that share the same MD5,
    regardless of path or filename.
    Takes already-scanned DataFrames (see scan_folder_md5) to avoid re-hashing.
    """
    df_origin = df_origin.copy()
    df_destination = df_destination.copy()
    df_origin["source"] = "Origin"
    df_destination["source"] = "Destination"
    df_combined = pd.concat([df_origin, df_destination], ignore_index=True)

    if df_combined.empty:
        return pd.DataFrame()

    df_valid = df_combined[df_combined["md5"].notna()].copy()

    md5_in_both = (
        df_valid.groupby("md5")["source"].apply(lambda values: set(values)).reset_index()
    )
    md5_in_both = md5_in_both[
        md5_in_both["source"].apply(
            lambda sources: "Origin" in sources and "Destination" in sources
        )
    ]["md5"]

    df_between = df_valid[df_valid["md5"].isin(md5_in_both)].copy()
    if df_between.empty:
        return pd.DataFrame()

    summary_between = (
        df_between.groupby("md5")
        .agg(
            count=("path", "count"),
            size_bytes=("size_bytes", "first"),
            total_size_bytes=("size_bytes", "sum"),
            origin_paths=(
                "path",
                lambda values: [
                    path
                    for path, source in zip(values, df_between.loc[values.index, "source"])
                    if source == "Origin"
                ],
            ),
            dest_paths=(
                "path",
                lambda values: [
                    path
                    for path, source in zip(values, df_between.loc[values.index, "source"])
                    if source == "Destination"
                ],
            ),
        )
        .reset_index()
    )

    summary_between["size_gb"] = summary_between["size_bytes"] / (1024 ** 3)
    summary_between["total_size_gb"] = summary_between["total_size_bytes"] / (1024 ** 3)
    summary_between["wasted_gb"] = (
        summary_between["total_size_bytes"] - summary_between["size_bytes"]
    ) / (1024 ** 3)

    summary_between = summary_between.merge(
        _oldest_per_md5(df_between, extra_columns=["source"]), on="md5", how="left"
    )

    return summary_between.sort_values(
        ["wasted_gb", "count"], ascending=[False, False]
    )


MAX_TERMINAL_ROWS = 50


def _print_table_head(df: pd.DataFrame, max_rows: int = MAX_TERMINAL_ROWS) -> None:
    """
    Print at most `max_rows` rows of df. Rendering the whole table with
    to_string() is fine for a small test folder but, at millions of files,
    can mean hundreds of thousands of rows of path lists — formatting that
    alone can take hours with no output, looking exactly like a hang. The
    full tables go to the CSV exports anyway.
    """
    print(df.head(max_rows).to_string(index=False))
    if len(df) > max_rows:
        print(f"... ({len(df) - max_rows} more row(s) not shown; see the CSV export for all of them)")


def print_duplicate_report(label: str, summary: pd.DataFrame) -> None:
    """Print a compact duplicate summary to the terminal."""
    print(f"\n=== {label} ===")
    if summary.empty:
        print("No duplicates found.")
        return

    total_wasted_gb = summary["wasted_gb"].sum()
    print(
        f"Duplicate MD5 groups: {len(summary)} | "
        f"Duplicate file entries: {int(summary['count'].sum())} | "
        f"Wasted space: {total_wasted_gb:.3f} GB"
    )

    # build_duplicate_summary() has a "paths" column; compare_cross_folder_
    # duplicates() has "origin_paths"/"dest_paths" instead. Support both.
    path_columns = (
        ["paths"] if "paths" in summary.columns else ["origin_paths", "dest_paths"]
    )
    # oldest_source only exists for the between-folders summary.
    oldest_columns = [
        c for c in ["oldest_path", "oldest_modified", "oldest_source"] if c in summary.columns
    ]
    _print_table_head(
        summary[
            ["md5", "count", "size_gb", "total_size_gb", "wasted_gb",
             *oldest_columns, *path_columns]
        ]
    )


def print_integrity_report(results: pd.DataFrame) -> None:
    """Print cross-folder integrity results."""
    print("\n=== Cross-folder MD5 Integrity Check ===")
    if results.empty:
        print("No files to compare.")
        return

    total = len(results)
    ok = int((results["status"] == "OK").sum())
    mismatch = int((results["status"] == "MISMATCH").sum())
    missing = int((results["status"] == "MISSING IN DEST").sum())
    unreadable = int((results["status"] == "UNREADABLE").sum())

    print(
        f"Total origin files: {total} | OK: {ok} | "
        f"Mismatches: {mismatch} | Missing in destination: {missing} | "
        f"Unreadable: {unreadable}"
    )

    problems = results[results["status"] != "OK"]
    if not problems.empty:
        print("\nProblem files:")
        _print_table_head(
            problems[["relative_path", "status", "origin_md5", "dest_md5", "size_gb"]]
        )


def load_resume_file_paths(path: str | Path) -> list[str]:
    """
    Load a file-path list from a previously written raw export (either the
    early pre-hash version or the final one — see build_prehash_export()/
    build_raw_file_export()), for use with --resume-from. Only the `path`
    column is used: size/mtime are re-collected fresh and every file is
    re-hashed, so results reflect the current state of disk rather than
    trusting possibly-stale metadata from the earlier, interrupted run.

    The raw export is a `.csv` file; `.xlsx` is also accepted (reading the
    "all_files" sheet) for a raw export written before the switch to CSV.
    """
    path = Path(path)
    missing_path_column = ValueError(
        f"--resume-from file '{path}' is missing a 'path' column — it "
        "must be a raw export produced by this script (via --excel)."
    )
    if path.suffix.lower() == ".csv":
        # Only the path column is read (not filename/mtime/md5/size too):
        # at millions of rows the other columns alone are GBs of RAM.
        try:
            df = pd.read_csv(path, usecols=["path"], dtype={"path": str})
        except ValueError as exc:
            raise missing_path_column from exc
    else:
        df = pd.read_excel(path, sheet_name="all_files")
    if "path" not in df.columns:
        raise missing_path_column
    return df["path"].tolist()


def export_sheets_to_csv(
    excel_path: str | Path, sheets: dict[str, pd.DataFrame]
) -> list[tuple[str, Path]]:
    """
    Write each DataFrame as its own companion CSV (`<excel_stem>_<name>.csv`)
    instead of a sheet in the main `--excel` workbook.

    Unlike the raw file listing, `folder_duplicates`/`origin_duplicates`/
    `integrity_results` etc. don't have an a-priori bounded row count - a
    heavily-duplicated or fully-compared large scan can push any of them
    past Excel's 1,048,576-row-per-sheet cap just as easily as the raw
    listing can, so every sheet that scales with file/entry count is CSV
    for the same reason: no row limit, and much faster to write at scale.
    Returns (sheet_name, path) pairs for logging.
    """
    written: list[tuple[str, Path]] = []
    for sheet_name, df in sheets.items():
        out_path = Path(excel_path).with_name(f"{Path(excel_path).stem}_{sheet_name}.csv")
        written.append((sheet_name, write_csv(_stringify_object_columns(df), out_path)))
    return written


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scan folders for duplicate files and verify MD5 integrity."
    )
    parser.add_argument(
        "--folder",
        help="Single folder to scan recursively for duplicates across all subfolders.",
    )
    parser.add_argument(
        "--origin",
        help="Origin/source folder to scan.",
    )
    parser.add_argument(
        "--destination",
        help="Destination/backup folder to scan.",
    )
    parser.add_argument(
        "--mode",
        choices=["all", "between", "integrity"],
        default="all",
        help=(
            "Which check to run. With --folder, 'all' finds the duplicates inside "
            "that folder. With --origin/--destination, 'all' finds the duplicates "
            "inside each folder plus 'between' and 'integrity', which both need the pair."
        ),
    )
    parser.add_argument(
        "--excel",
        help="Optional path to write the results to an Excel workbook.",
    )
    parser.add_argument(
        "--threading",
        action="store_true",
        help=(
            "Hash files concurrently using multiple threads. Off by default; "
            "useful on storage that supports concurrent reads (e.g. network "
            "volumes, SSD/NVMe)."
        ),
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=None,
        help="Number of hashing threads to use. Requires --threading. Default: 8.",
    )
    parser.add_argument(
        "--io-timeout",
        type=float,
        default=DEFAULT_IO_TIMEOUT_SECONDS,
        help=(
            "Seconds to wait for a single blocking filesystem operation "
            "(listing a folder, stat'ing a file, or reading a file for "
            "hashing) before giving up on it and moving on (e.g. an "
            f"unreachable network path or a stuck handle). Default: {DEFAULT_IO_TIMEOUT_SECONDS:.0f}."
        ),
    )
    parser.add_argument(
        "--included",
        nargs="+",
        metavar="PATH",
        help=(
            "Restrict scanning to only these subfolder(s) (and their "
            "contents), given as one or more paths under the target "
            "folder(s). Cannot be combined with --excluded. Default: scan "
            "everything, as now."
        ),
    )
    parser.add_argument(
        "--excluded",
        nargs="+",
        metavar="PATH",
        help=(
            "Skip these subfolder(s) (and their contents) during the scan, "
            "given as one or more paths under the target folder(s). Cannot "
            "be combined with --included. Default: nothing is excluded."
        ),
    )
    parser.add_argument(
        "--resume-from",
        metavar="PATH",
        help=(
            "Resume from a previously written raw export (the "
            "'..._raw.csv' file from --excel, either the early pre-hash "
            "version or the final one) instead of re-walking the folder(s) "
            "from scratch. Useful if hashing was interrupted on a big scan "
            "— reuses the already-known file list, but still re-collects "
            "size/mtime and re-hashes every file fresh, so a since-deleted "
            "file is simply treated as unreadable. Cannot be combined with "
            "--included/--excluded."
        ),
    )
    args = parser.parse_args()

    if args.folder:
        if args.origin or args.destination:
            parser.error("Use either --folder or --origin/--destination, not both.")
        if args.mode != "all":
            parser.error(
                f"Mode '{args.mode}' needs --origin and --destination, not --folder."
            )
    else:
        if not args.origin or not args.destination:
            parser.error(
                "Either provide --folder, or provide both --origin and --destination."
            )

    if args.workers is not None and not args.threading:
        parser.error("--workers requires --threading to be set.")

    if args.threading:
        if args.workers is None:
            args.workers = 8
        elif args.workers < 1:
            parser.error("--workers must be a positive integer.")

    if args.excel and Path(args.excel).is_dir():
        parser.error(
            f"--excel must be a path to a file (e.g. ending in .xlsx), "
            f"not a folder: {args.excel}"
        )

    if args.io_timeout <= 0:
        parser.error("--io-timeout must be a positive number.")

    if args.included and args.excluded:
        parser.error("Use either --included or --excluded, not both.")

    for flag_name, paths in (("--included", args.included), ("--excluded", args.excluded)):
        for path in paths or []:
            try:
                validate_folder(path, flag_name)
            except ValueError as exc:
                parser.error(str(exc))

    if args.resume_from:
        if args.included or args.excluded:
            parser.error(
                "--resume-from cannot be combined with --included/--excluded "
                "(the file list to scan is already fixed)."
            )
        if not Path(args.resume_from).is_file():
            parser.error(f"--resume-from file does not exist: {args.resume_from}")

    return args


def main() -> None:
    args = parse_args()
    excel_sheets: dict[str, pd.DataFrame] = {}
    included_paths = _normalize_filter_paths(args.included)
    excluded_paths = _normalize_filter_paths(args.excluded)

    resume_file_paths = None
    if args.resume_from:
        resume_file_paths = load_resume_file_paths(args.resume_from)
        print(f"Resuming from '{args.resume_from}': {len(resume_file_paths)} file(s) loaded.")

    # Written right after each scan's stat step (before hashing starts), so
    # a raw filename/path/mtime/size listing hits disk well before the
    # slower hashing step finishes — see scan_folder_md5's on_stat_complete.
    # Superseded by the final md5-including write at the end of each branch
    # below once hashing actually completes.
    # Each scan's frame is appended to the CSV rather than kept in memory
    # and re-concatenated, so an origin scan's (possibly GB-sized) listing
    # isn't held for the rest of the run.
    partial_rows_written = 0

    def _write_partial_raw(frame: pd.DataFrame) -> None:
        nonlocal partial_rows_written
        partial_output = raw_export_path(args.excel)
        if partial_rows_written == 0:
            write_csv(frame, partial_output)
        else:
            frame.to_csv(partial_output, mode="a", header=False, index=False)
        partial_rows_written += len(frame)
        print(
            f"Raw file listing (pre-hash, {partial_rows_written} file(s) so far) "
            f"written to: {partial_output}"
        )

    on_stat_complete = _write_partial_raw if args.excel else None

    if args.folder:
        folder_path = validate_folder(args.folder, "Folder")
        if resume_file_paths is not None:
            in_folder = _path_prefix_matcher(folder_path)
            outside = sum(1 for p in resume_file_paths if not in_folder(p))
            if outside:
                example = next(p for p in resume_file_paths if not in_folder(p))
                print(
                    f"WARNING: {outside} of {len(resume_file_paths)} resumed "
                    f"path(s) are not under --folder '{folder_path}' (e.g. "
                    f"'{example}'). They are still scanned as listed; check the "
                    "--resume-from file matches this folder (a mapped drive vs. "
                    "its \\\\server\\share path counts as different here)."
                )
        folder_scan, folder_skipped = scan_folder_md5(
            folder_path,
            use_threading=args.threading,
            workers=args.workers or 8,
            io_timeout=args.io_timeout,
            included=included_paths,
            excluded=excluded_paths,
            on_stat_complete=on_stat_complete,
            resume_file_paths=resume_file_paths,
        )
        print("Finding duplicates and building summary ...")
        folder_duplicates = find_real_duplicates(folder_scan)
        folder_summary = build_duplicate_summary(folder_duplicates)

        print_duplicate_report("Folder Duplicates", folder_summary)

        if folder_skipped:
            print(
                f"\n{len(folder_skipped)} folder(s) could not be accessed "
                "and were skipped (see warnings above)."
            )

        if args.excel:
            # Every sheet is a CSV, not an Excel workbook — see
            # export_sheets_to_csv's docstring: folder_duplicates in
            # particular has no bounded row count (a heavily-duplicated
            # scan can put every file in it), so it's just as much at risk
            # of exceeding Excel's row cap as the raw listing is.
            written = export_sheets_to_csv(args.excel, {
                "folder_duplicates": folder_duplicates,
                "folder_summary": folder_summary,
                "skipped_folders": build_skipped_folders_sheet([("Folder", folder_skipped)]),
            })
            for sheet_name, path in written:
                print(f"{sheet_name} written to: {path}")

            raw_output = raw_export_path(args.excel)
            write_csv(build_raw_file_export([folder_scan]), raw_output)
            print(f"Raw file listing written to: {raw_output}")

        return

    origin_path = validate_folder(args.origin, "Origin")
    destination_path = validate_folder(args.destination, "Destination")

    # A --resume-from file combines both sides into one file list; split it
    # back into an origin subset and a destination subset by which root each
    # path falls under, so each scan resumes only its own files.
    origin_resume_paths = None
    destination_resume_paths = None
    if resume_file_paths is not None:
        in_origin = _path_prefix_matcher(origin_path)
        in_destination = _path_prefix_matcher(destination_path)
        origin_resume_paths = [p for p in resume_file_paths if in_origin(p)]
        destination_resume_paths = [p for p in resume_file_paths if in_destination(p)]
        del resume_file_paths

    # Each folder is hashed at most once and the resulting DataFrame is reused
    # across every check that needs it, instead of re-scanning per check.
    origin_scan, origin_skipped = scan_folder_md5(
        origin_path,
        use_threading=args.threading,
        workers=args.workers or 8,
        io_timeout=args.io_timeout,
        included=included_paths,
        excluded=excluded_paths,
        on_stat_complete=on_stat_complete,
        resume_file_paths=origin_resume_paths,
    )

    destination_scan, destination_skipped = scan_folder_md5(
        destination_path,
        use_threading=args.threading,
        workers=args.workers or 8,
        io_timeout=args.io_timeout,
        included=included_paths,
        excluded=excluded_paths,
        on_stat_complete=on_stat_complete,
        resume_file_paths=destination_resume_paths,
    )

    total_skipped = len(origin_skipped) + len(destination_skipped)
    if total_skipped:
        print(
            f"\n{total_skipped} folder(s) could not be accessed and were "
            "skipped (see warnings above)."
        )

    if args.mode == "all":
        print("Finding origin duplicates and building summary ...")
        origin_duplicates = find_real_duplicates(origin_scan)
        origin_summary = build_duplicate_summary(origin_duplicates)
        print_duplicate_report("Origin Duplicates", origin_summary)

        if args.excel:
            excel_sheets["origin_duplicates"] = origin_duplicates
            excel_sheets["origin_summary"] = origin_summary

    if args.mode == "all":
        print("Finding destination duplicates and building summary ...")
        destination_duplicates = find_real_duplicates(destination_scan)
        destination_summary = build_duplicate_summary(destination_duplicates)
        print_duplicate_report("Destination Duplicates", destination_summary)

        if args.excel:
            excel_sheets["destination_duplicates"] = destination_duplicates
            excel_sheets["destination_summary"] = destination_summary

    if args.mode in {"all", "between"}:
        print("Finding origin <-> destination duplicates ...")
        between_summary = compare_cross_folder_duplicates(
            origin_scan, destination_scan
        )
        print_duplicate_report("Origin <-> Destination Duplicates", between_summary)

        if args.excel:
            excel_sheets["between_summary"] = between_summary

    if args.mode in {"all", "integrity"}:
        print("Running integrity check ...")
        integrity_results = compare_folders_by_relative_path(
            origin_scan, destination_scan, origin_path, destination_path
        )
        print_integrity_report(integrity_results)

        if args.excel:
            excel_sheets["integrity_results"] = integrity_results

    if args.excel:
        excel_sheets["skipped_folders"] = build_skipped_folders_sheet(
            [("Origin", origin_skipped), ("Destination", destination_skipped)]
        )
        # Every sheet is a CSV, not an Excel workbook — see
        # export_sheets_to_csv's docstring: *_duplicates and
        # integrity_results both scale with file/entry count (integrity in
        # particular has one row per origin file, duplicates or not), so
        # either can independently exceed Excel's row cap on a big scan.
        for sheet_name, path in export_sheets_to_csv(args.excel, excel_sheets):
            print(f"{sheet_name} written to: {path}")

        raw_scans = [scan for scan in (origin_scan, destination_scan) if scan is not None]
        raw_output = raw_export_path(args.excel)
        write_csv(build_raw_file_export(raw_scans), raw_output)
        print(f"Raw file listing written to: {raw_output}")


if __name__ == "__main__":
    main()