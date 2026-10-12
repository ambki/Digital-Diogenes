from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
from tqdm import tqdm

from duplicates_check import (
    DEFAULT_IO_TIMEOUT_SECONDS,
    _normalize_filter_paths,
    build_skipped_folders_sheet,
    export_results_to_excel,
    raw_export_path,
    shorten_name,
    stat_with_timeout,
    validate_folder,
    walk_files_with_skip_report,
    write_csv,
)

UNMIXED_FOLDER_NAME = "Unmixed"

GROUP_LABELS = {
    "old_files": "Old Files",
    "zip_files": "ZIP Files",
    "unmixed_files": "Files Inside 'Unmixed' Folders",
}


def scan_folder_metadata(
    folder_path: str | Path,
    show_progress: bool = True,
    io_timeout: float = DEFAULT_IO_TIMEOUT_SECONDS,
    included: list[Path] | None = None,
    excluded: list[Path] | None = None,
) -> tuple[pd.DataFrame, list[dict[str, str]]]:
    """
    Recursively scan a folder and return (df, skipped_folders): a DataFrame
    with columns path, filename, size_bytes, mtime, modified; and a list of
    directories that couldn't be accessed (see walk_files_with_skip_report
    in duplicates_check.py).

    A locked/corrupted/unreachable folder or file is treated as unreadable
    (rather than aborting or hanging the scan) once it's unresponsive for
    `io_timeout` seconds — this covers both listing a folder's contents and
    stat'ing a file, since either can independently hang on a dangling
    network handle.

    `included`/`excluded` optionally restrict the scan to, or prune it away
    from, specific subfolders of `folder_path` (see walk_files_with_skip_report).

    Unlike scan_folder_md5() in duplicates_check.py, this does not hash file
    contents, since only size/age/name/path metadata is needed here.
    """
    folder = Path(folder_path)

    if show_progress:
        print(f"Scanning '{folder}' for files (this can take a while, so please be patient) ...")

    file_paths, skipped_folders = walk_files_with_skip_report(
        folder,
        io_timeout=io_timeout,
        show_progress=show_progress,
        included=included,
        excluded=excluded,
    )

    records: list[dict[str, str | int | float | None]] = []
    with tqdm(
        total=len(file_paths),
        desc=f"Collecting metadata ({folder.name or folder})",
        unit="file",
        disable=not show_progress,
    ) as progress:
        for file_path_str in file_paths:
            file_path = Path(file_path_str)
            progress.set_postfix(now=shorten_name(file_path.name), refresh=True)

            # Unreadable/unresponsive files still show up (e.g. for the
            # zip/Unmixed checks, which don't depend on stat()) but are
            # excluded from the age-based check since their real mtime is
            # unknown — hence the NaN sentinel rather than 0.0.
            size_bytes, mtime = stat_with_timeout(
                file_path, timeout=io_timeout, on_failure=(0, float("nan"))
            )

            records.append(
                {
                    "path": str(file_path),
                    "filename": file_path.name,
                    "size_bytes": size_bytes,
                    "mtime": mtime,
                    "modified": (
                        pd.to_datetime(mtime, unit="s").strftime("%Y-%m-%d %H:%M:%S")
                        if mtime == mtime  # False for NaN
                        else None
                    ),
                }
            )
            progress.update(1)

    return pd.DataFrame(records), skipped_folders


def build_raw_export(scan: pd.DataFrame) -> pd.DataFrame:
    """
    Full filename/path/mtime/size_gb listing of every file scanned (not
    just the old/zip/Unmixed groups) — same shape as duplicates_check.py's
    raw export (minus md5, which this script never computes), so it can
    also be used as a --resume-from input there. The scan already walks
    every file regardless of which groups are requested, so this is free.
    """
    return pd.DataFrame({
        "filename": scan["filename"],
        "path": scan["path"],
        "mtime": scan["modified"],
        "size_gb": scan["size_bytes"] / (1024 ** 3),
    })


def find_old_files(df: pd.DataFrame, years: int = 10) -> pd.DataFrame:
    """Return files whose modification time is older than `years` years ago."""
    if df.empty:
        return df.copy()

    cutoff_ts = (pd.Timestamp.now() - pd.DateOffset(years=years)).timestamp()
    mask = df["mtime"].notna() & (df["mtime"] < cutoff_ts)
    return df[mask].sort_values("mtime")


def find_zip_files(df: pd.DataFrame) -> pd.DataFrame:
    """Return files with a .zip extension (case-insensitive)."""
    if df.empty:
        return df.copy()

    mask = df["filename"].str.lower().str.endswith(".zip")
    return df[mask].sort_values("size_bytes", ascending=False)


def _path_has_folder_named(path: Path, name: str) -> bool:
    target = name.lower()
    return any(part.lower() == target for part in path.parent.parts)


def _nearest_ancestor_named(path: Path, name: str) -> str | None:
    """Return the full path of the closest-to-the-file ancestor folder named `name`."""
    target = name.lower()
    parts = path.parts
    matches = [index for index, part in enumerate(parts) if part.lower() == target]
    if not matches:
        return None
    return str(Path(*parts[: matches[-1] + 1]))


def find_unmixed_files(df: pd.DataFrame) -> pd.DataFrame:
    """Return files that live anywhere inside a folder named 'Unmixed'."""
    if df.empty:
        return df.copy()

    mask = df["path"].apply(lambda p: _path_has_folder_named(Path(p), UNMIXED_FOLDER_NAME))
    result = df[mask].copy()
    result["unmixed_folder"] = result["path"].apply(
        lambda p: _nearest_ancestor_named(Path(p), UNMIXED_FOLDER_NAME)
    )
    return result.sort_values("size_bytes", ascending=False)


def build_group_summary(group: str, df_group: pd.DataFrame) -> dict[str, str | int | float]:
    """Summarize one candidate group: file count and total size."""
    total_bytes = int(df_group["size_bytes"].sum()) if not df_group.empty else 0
    return {
        "group": group,
        "count": len(df_group),
        "total_size_bytes": total_bytes,
        "total_size_gb": total_bytes / (1024 ** 3),
    }


def build_global_summary(
    group_summaries: list[dict[str, str | int | float]], group_dfs: dict[str, pd.DataFrame]
) -> dict[str, int | float]:
    """
    Combine all groups into global numbers.

    "summed_*" adds up each group's numbers as-is (a file that qualifies for
    more than one group, e.g. an old .zip inside an Unmixed folder, is
    counted once per group it belongs to). "unique_*" deduplicates by path
    instead, so overlapping files are only counted once — the number that
    actually reflects total disk space that could be freed.
    """
    summed_count = sum(int(summary["count"]) for summary in group_summaries)
    summed_bytes = sum(int(summary["total_size_bytes"]) for summary in group_summaries)

    non_empty = [df.assign(group=name)[["path", "size_bytes", "group"]] for name, df in group_dfs.items() if not df.empty]
    combined = pd.concat(non_empty, ignore_index=True) if non_empty else pd.DataFrame(
        columns=["path", "size_bytes", "group"]
    )
    unique = combined.drop_duplicates(subset=["path"])
    overlap_paths = combined.loc[combined.duplicated(subset=["path"], keep=False), "path"].unique()
    overlap = unique[unique["path"].isin(overlap_paths)]

    return {
        "summed_count": summed_count,
        "summed_size_bytes": summed_bytes,
        "summed_size_gb": summed_bytes / (1024 ** 3),
        "unique_count": len(unique),
        "unique_size_bytes": int(unique["size_bytes"].sum()),
        "unique_size_gb": unique["size_bytes"].sum() / (1024 ** 3),
        "overlap_count": len(overlap),
        "overlap_size_gb": overlap["size_bytes"].sum() / (1024 ** 3),
    }


def build_directory_breakdown(df_group: pd.DataFrame, top_n: int = 20) -> pd.DataFrame:
    """Aggregate a group's files by parent directory, sorted by size descending."""
    if df_group.empty:
        return pd.DataFrame(columns=["directory", "count", "total_size_gb"])

    breakdown = (
        df_group.assign(directory=df_group["path"].apply(lambda p: str(Path(p).parent)))
        .groupby("directory")
        .agg(count=("path", "count"), total_size_bytes=("size_bytes", "sum"))
        .reset_index()
    )
    breakdown["total_size_gb"] = breakdown["total_size_bytes"] / (1024 ** 3)
    return breakdown.drop(columns=["total_size_bytes"]).sort_values(
        "total_size_gb", ascending=False
    ).head(top_n)


def print_group_report(label: str, df_group: pd.DataFrame, summary: dict) -> None:
    """Print a compact report for one candidate group: numbers, top folders, paths."""
    print(f"\n=== {label} ===")
    if df_group.empty:
        print("No files found.")
        return

    print(f"Files: {summary['count']} | Total size: {summary['total_size_gb']:.3f} GB")

    print("\nTop directories by size:")
    print(build_directory_breakdown(df_group).to_string(index=False))

    print("\nFile paths:")
    columns = [c for c in ["path", "size_bytes", "modified", "unmixed_folder"] if c in df_group.columns]
    if len(df_group) <= 50:
        print(df_group[columns].to_string(index=False))
    else:
        print(df_group[columns].head(50).to_string(index=False))
        print(f"... and {len(df_group) - 50} more (full list available with --excel).")


def print_global_report(summary: dict) -> None:
    """Print combined numbers across all candidate groups."""
    print("\n=== Global (All Groups) ===")
    print(
        f"Summed across groups: {summary['summed_count']} files | "
        f"{summary['summed_size_gb']:.3f} GB\n"
        f"Unique files (deduplicated across groups): {summary['unique_count']} | "
        f"{summary['unique_size_gb']:.3f} GB\n"
        f"Overlap (counted in more than one group): {summary['overlap_count']} files | "
        f"{summary['overlap_size_gb']:.3f} GB"
    )


def plot_storage_size_barplots(
    group_summaries: list[dict], global_summary: dict, output_dir: str | Path
) -> Path:
    """
    Save a two-panel barplot: storage size per group, and global storage size
    as absolute numbers (summed vs. deduplicated).
    """
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    fig, (ax_groups, ax_global) = plt.subplots(1, 2, figsize=(12, 5))

    def annotate_bars(ax, bars) -> None:
        for bar in bars:
            height = bar.get_height()
            ax.annotate(
                f"{height:.2f} GB",
                (bar.get_x() + bar.get_width() / 2, height),
                ha="center",
                va="bottom",
                fontsize=9,
            )

    group_names = [GROUP_LABELS[summary["group"]] for summary in group_summaries]
    group_sizes = [summary["total_size_gb"] for summary in group_summaries]
    bars_groups = ax_groups.bar(group_names, group_sizes, color=["#4C72B0", "#DD8452", "#55A868"])
    ax_groups.set_ylabel("Size (GB)")
    ax_groups.set_title("Storage Size by Group")
    ax_groups.tick_params(axis="x", rotation=15)
    annotate_bars(ax_groups, bars_groups)

    global_names = ["Summed\n(with overlap)", "Unique\n(deduplicated)"]
    global_sizes = [global_summary["summed_size_gb"], global_summary["unique_size_gb"]]
    bars_global = ax_global.bar(global_names, global_sizes, color=["#C44E52", "#8172B2"])
    ax_global.set_ylabel("Size (GB)")
    ax_global.set_title("Global Storage Size (Absolute)")
    annotate_bars(ax_global, bars_global)

    fig.suptitle("Elimination Candidates — Storage Size")
    fig.tight_layout()

    output_file = output_path / "storage_size_barplots.png"
    fig.savefig(output_file, dpi=150)
    plt.close(fig)

    print(f"\nBarplots written to: {output_file}")
    return output_file


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Scan a folder for files that are candidates for elimination: files "
            "older than N years, .zip files, and files inside folders named "
            "'Unmixed'."
        )
    )
    parser.add_argument("--folder", required=True, help="Folder to scan recursively.")
    parser.add_argument(
        "--years",
        type=int,
        default=10,
        help="Age threshold in years for the 'old files' group. Default: 10.",
    )
    parser.add_argument(
        "--excel",
        help="Optional path to write the full per-file results to an Excel workbook.",
    )
    parser.add_argument(
        "--plots-dir",
        default="elimination_plots",
        help="Directory to save barplot PNGs into. Default: 'elimination_plots'.",
    )
    parser.add_argument(
        "--no-plots",
        action="store_true",
        help="Skip generating barplots.",
    )
    parser.add_argument(
        "--io-timeout",
        type=float,
        default=DEFAULT_IO_TIMEOUT_SECONDS,
        help=(
            "Seconds to wait for a single blocking filesystem operation "
            "(listing a folder or stat'ing a file) before giving up on it "
            "and moving on (e.g. an unreachable network path or a stuck "
            f"handle). Default: {DEFAULT_IO_TIMEOUT_SECONDS:.0f}."
        ),
    )
    parser.add_argument(
        "--included",
        nargs="+",
        metavar="PATH",
        help=(
            "Restrict scanning to only these subfolder(s) (and their "
            "contents), given as one or more paths under --folder. Cannot "
            "be combined with --excluded. Default: scan everything, as now."
        ),
    )
    parser.add_argument(
        "--excluded",
        nargs="+",
        metavar="PATH",
        help=(
            "Skip these subfolder(s) (and their contents) during the scan, "
            "given as one or more paths under --folder. Cannot be combined "
            "with --included. Default: nothing is excluded."
        ),
    )
    args = parser.parse_args()

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

    return args


def main() -> None:
    args = parse_args()
    folder_path = validate_folder(args.folder, "Folder")

    scan, skipped_folders = scan_folder_metadata(
        folder_path,
        io_timeout=args.io_timeout,
        included=_normalize_filter_paths(args.included),
        excluded=_normalize_filter_paths(args.excluded),
    )

    if args.excel:
        # Written immediately once the scan finishes (before the group
        # filtering/plots/reports below) — the walk+stat already covers
        # every file regardless of which groups get requested, so this
        # costs nothing extra and secures a full raw listing on disk even
        # if something later in the run fails.
        raw_output = raw_export_path(args.excel)
        write_csv(build_raw_export(scan), raw_output)
        print(f"Raw file listing written to: {raw_output}")

    if skipped_folders:
        print(
            f"\n{len(skipped_folders)} folder(s) could not be accessed "
            "and were skipped (see warnings above)."
        )

    group_dfs = {
        "old_files": find_old_files(scan, years=args.years),
        "zip_files": find_zip_files(scan),
        "unmixed_files": find_unmixed_files(scan),
    }

    group_summaries = []
    for group, df_group in group_dfs.items():
        summary = build_group_summary(group, df_group)
        group_summaries.append(summary)
        label = GROUP_LABELS[group]
        if group == "old_files":
            label = f"Files Older Than {args.years} Years"
        print_group_report(label, df_group, summary)

    global_summary = build_global_summary(group_summaries, group_dfs)
    print_global_report(global_summary)

    if not args.no_plots:
        plot_storage_size_barplots(group_summaries, global_summary, args.plots_dir)

    if args.excel:
        # Each group's full file list is written as its own CSV rather than
        # a workbook sheet — a group like "old_files" can easily hold
        # millions of rows on a big scan, and Excel sheets are capped at
        # 1,048,576 rows (openpyxl raises "This sheet is too large!" and
        # nothing gets written at all past that). The xlsx report itself
        # only ever holds the naturally-small summary/skipped-folders data.
        sheets = {
            "summary": pd.DataFrame(group_summaries),
            "skipped_folders": build_skipped_folders_sheet([("Folder", skipped_folders)]),
        }
        output_file = export_results_to_excel(args.excel, sheets)
        print(f"\nExcel report written to: {output_file}")

        excel_path = Path(args.excel)
        for group, df_group in group_dfs.items():
            group_csv = excel_path.with_name(f"{excel_path.stem}_{group}.csv")
            write_csv(df_group, group_csv)
            print(f"{GROUP_LABELS[group]} listing written to: {group_csv}")


if __name__ == "__main__":
    main()
