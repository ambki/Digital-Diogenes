from __future__ import annotations

import argparse
import hashlib
import json
import os
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import Executor, ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd
from tqdm import tqdm


DEFAULT_IO_TIMEOUT_SECONDS = 10.0


def to_long_path(path: str | Path) -> str:
    """
    Return `path` in Windows extended-length form (\\\\?\\C:\\... or
    \\\\?\\UNC\\server\\share\\...), which lifts the 260-character MAX_PATH
    limit for that call regardless of the system's LongPathsEnabled setting
    (off by default). Without it, a folder or file whose full path is over
    the limit can't be listed, stat'ed or opened ("The system cannot find
    the path specified"), so it's silently left out of the scan.

    Only used at the point of each filesystem call: paths stored in results
    and reports stay in their normal form (see from_long_path).
    """
    if os.name != "nt":
        return str(path)
    path = os.path.abspath(path)  # \\?\ paths aren't normalized: no "/", "." or ".."
    if path.startswith("\\\\?\\"):
        return path
    if path.startswith("\\\\"):
        return "\\\\?\\UNC\\" + path[2:]
    return "\\\\?\\" + path


def from_long_path(path: str) -> str:
    """Inverse of to_long_path, for showing/comparing paths."""
    if path.startswith("\\\\?\\UNC\\"):
        return "\\\\" + path[8:]
    if path.startswith("\\\\?\\"):
        return path[4:]
    return path


def shorten_name(name: str, max_len: int = 40) -> str:
    """Truncate a filename for display in a progress bar's postfix, so one
    long name doesn't wrap/clutter the line."""
    return name if len(name) <= max_len else name[: max_len - 3] + "..."


def _run_with_timeout(
    func, timeout: float, timeout_message: str
) -> tuple[object | None, BaseException | None]:
    """
    Run func() (no arguments — wrap call sites in a lambda/closure) in a
    daemon thread, waiting up to `timeout` seconds for it to finish.

    Several filesystem calls used here (scandir, stat, opening/reading a
    file) have no built-in timeout — a stale network path, a broken
    junction/reparse point, or a stuck handle can make any of them block
    indefinitely without ever raising an error, so a plain try/except can't
    catch it. Running the call in a thread lets the caller stop *waiting* on
    it after `timeout` seconds and move on, even though the thread itself
    can't be forcibly killed if it's stuck in a blocking OS call. It's
    marked daemon=True so an abandoned, permanently stuck thread never
    prevents the program from exiting.

    Returns (result, None) on success, or (None, error) where error is
    either the OSError the call raised, or a TimeoutError if it didn't
    finish in time.
    """
    result: dict[str, object] = {}

    def _target() -> None:
        try:
            result["value"] = func()
        except Exception as exc:  # not just OSError: anything else would
            # otherwise be silently lost and look like a (None, None) success.
            result["error"] = exc

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    thread.join(timeout=timeout)

    if thread.is_alive():
        return None, TimeoutError(timeout_message)
    if "error" in result:
        return None, result["error"]  # type: ignore[return-value]
    return result.get("value"), None


def _list_directory_with_timeout(
    path: str, timeout: float
) -> tuple[list[str] | None, list[str] | None, BaseException | None]:
    """List one directory's immediate entries (dirnames, filenames), giving
    up after `timeout` seconds instead of hanging forever. See _run_with_timeout."""

    def _do_list() -> tuple[list[str], list[str]]:
        dirnames: list[str] = []
        filenames: list[str] = []
        with os.scandir(to_long_path(path)) as entries:
            for entry in entries:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        dirnames.append(entry.name)
                    else:
                        filenames.append(entry.name)
                except OSError:
                    continue
        return dirnames, filenames

    value, error = _run_with_timeout(
        _do_list,
        timeout,
        f"Timed out after {timeout:.0f}s listing this folder "
        "(it may be an unreachable network path or a stuck handle)",
    )
    if error is not None:
        return None, None, error
    dirnames, filenames = value  # type: ignore[misc]
    return dirnames, filenames, None


def _bounded_map(
    executor: Executor, func: Callable, items: Iterable, max_in_flight: int
) -> Iterator:
    """
    Like executor.map(func, items) — results in input order — but with at
    most `max_in_flight` tasks submitted at once. executor.map() submits
    *every* item before yielding the first result, which for millions of
    files means millions of Future objects (several GB of RAM, possibly
    pushing the machine into swap) and minutes of apparent inactivity
    before the progress bar moves at all.
    """
    pending: deque = deque()
    for item in items:
        pending.append(executor.submit(func, item))
        if len(pending) >= max_in_flight:
            yield pending.popleft().result()
    while pending:
        yield pending.popleft().result()


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


def _is_within_any(path: Path, roots: list[Path]) -> bool:
    """True if `path` is one of `roots`, or a descendant of one of them."""
    return any(path == root or root in path.parents for root in roots)


def _is_ancestor_of_any(path: Path, roots: list[Path]) -> bool:
    """True if `path` is an ancestor of one of `roots` (or one of them)."""
    return any(path == root or path in root.parents for root in roots)


def walk_files_with_skip_report(
    folder: str | Path,
    io_timeout: float = DEFAULT_IO_TIMEOUT_SECONDS,
    show_progress: bool = True,
    included: list[Path] | None = None,
    excluded: list[Path] | None = None,
) -> tuple[list[str], list[dict[str, str]]]:
    """
    Recursively walk `folder`, returning every file path found.

    Paths are returned as plain strings, not Path objects: a Path costs
    ~1.2 KB per file once str()/hash() have been used on it (it caches its
    parsed parts), against ~0.2 KB for the string — at 6 million files
    that's ~7 GB vs ~1 GB, the difference between fitting in an 8 GB
    server's RAM and swapping it to a standstill. Wrap one in Path(...)
    only where needed, one at a time.

    A directory that can't be listed — whether it raises quickly
    (permission errors, a corrupted folder) or just hangs (an unreachable
    network path, a stuck handle) for more than `io_timeout` seconds — is
    skipped rather than aborting the whole walk. Each skipped directory is
    recorded as {"folder": <path>, "error": <message>} in the second return
    value, and a warning is printed for it immediately.

    `included`/`excluded`, if given, are lists of already-resolved
    subfolder paths (see _normalize_filter_paths). `excluded` prunes those
    subtrees before they're even listed. `included` restricts collection to
    only files inside those subtrees — directories that are ancestors of an
    included path are still descended into (without collecting their own
    files) so the included subfolders can be reached. At most one of the two
    is expected to be set (enforced by the caller/CLI).

    When show_progress is True, a live (indeterminate — the total folder
    count isn't known ahead of time) progress bar shows which folder is
    currently being listed and how many files have been found so far, so a
    folder with many subfolders doesn't look indistinguishable from a hang
    before the byte-based hashing progress bar even exists.
    """
    folder = Path(folder)
    file_paths: list[str] = []
    skipped_folders: list[dict[str, str]] = []
    stack = [str(folder)]

    with tqdm(
        desc=f"Finding files in {folder.name or folder}",
        unit="folder",
        disable=not show_progress,
    ) as progress:
        while stack:
            current = stack.pop()
            # resolve() touches the filesystem with no timeout (it can hang
            # on a stale network path just like scandir), so only pay for it
            # when a filter actually needs the resolved path.
            current_path = Path(current).resolve() if (included or excluded) else None

            if excluded and _is_within_any(current_path, excluded):
                continue

            collect_files = True
            if included:
                collect_files = _is_within_any(current_path, included)
                if not collect_files and not _is_ancestor_of_any(current_path, included):
                    continue

            progress.set_postfix(
                now=shorten_name(Path(current).name or current),
                files_found=len(file_paths),
                refresh=True,
            )
            dirnames, filenames, error = _list_directory_with_timeout(current, io_timeout)

            if error is not None:
                skipped_folders.append({"folder": current, "error": str(error)})
                print(f"WARNING: could not access '{current}': {error} - skipping.")
                progress.update(1)
                continue

            if collect_files:
                for filename in filenames:  # type: ignore[union-attr]
                    file_paths.append(os.path.join(current, filename))
            for dirname in dirnames:  # type: ignore[union-attr]
                stack.append(os.path.join(current, dirname))

            progress.update(1)

    return file_paths, skipped_folders


def _normalize_filter_paths(paths: list[str] | None) -> list[Path] | None:
    """Resolve a list of user-provided paths for use with `included`/`excluded`."""
    if not paths:
        return None
    return [Path(p).resolve() for p in paths]


def stat_with_timeout(
    file_path: str | Path,
    timeout: float = DEFAULT_IO_TIMEOUT_SECONDS,
    on_failure: tuple[int, float] = (0, 0.0),
    on_error: Callable[[BaseException], None] | None = None,
) -> tuple[int, float]:
    """
    `on_error`, if given, is called with every failure's exception
    (timeouts included), so a caller can count and report ordinary errors
    too instead of them vanishing silently — see _FailureTracker.

    Get (size_bytes, mtime) for file_path, giving up after `timeout`
    seconds instead of hanging (see _run_with_timeout — a dangling network
    handle can make stat() block indefinitely without ever raising).
    Returns `on_failure` on any failure — ordinary errors (permission
    denied, etc.) stay silent, matching the previous plain-stat() behavior,
    but a genuine timeout is printed as a warning, since that's exactly the
    kind of silent stall that's otherwise indistinguishable from the
    program just being slow. `on_failure` defaults to (0, 0.0); pass
    (0, float("nan")) if the caller needs to tell "genuinely zero" apart
    from "unknown" (as elimination_file_candidates.py does, to exclude
    unreadable files from its age-based check rather than treating them as
    infinitely old).
    """
    value, error = _run_with_timeout(
        lambda: os.stat(to_long_path(file_path)),
        timeout,
        f"Timed out after {timeout:.0f}s stat'ing '{file_path}' "
        "(it may be an unreachable network path or a stuck handle)",
    )
    if error is not None:
        if isinstance(error, TimeoutError):
            print(f"WARNING: {error} - treating as unreadable.")
        if on_error is not None:
            on_error(error)
        return on_failure
    return value.st_size, value.st_mtime  # type: ignore[union-attr]


def calculate_md5(
    file_path: str | Path,
    chunk_size: int = 1024 * 1024,
    timeout: float = DEFAULT_IO_TIMEOUT_SECONDS,
    on_error: Callable[[BaseException], None] | None = None,
) -> str | None:
    """
    Calculate MD5 checksum of a file, reading in chunks. Returns None if the
    file can't be read, including if reading stalls — i.e. no chunk arrives
    for more than `timeout` seconds (e.g. a dangling network handle). As in
    stat_with_timeout, only a genuine stall is printed as a warning here;
    `on_error`, if given, is called with every failure's exception.

    Unlike stat/listing (see _run_with_timeout), `timeout` here is an
    *inactivity* timeout, not a limit on total time: a multi-GB file that
    takes minutes to read but keeps making progress is hashed to the end.
    A total-time limit would silently give every large file an md5 of None
    (so the biggest duplicates would be missed), while leaving the
    abandoned thread still reading it in the background, competing for I/O
    with every later file. On a stall, the reader thread is also told to
    stop as soon as its current read returns, rather than carrying on.
    """
    state: dict[str, object] = {"last_progress": time.monotonic()}
    cancel = threading.Event()

    def _do_hash() -> None:
        try:
            hasher = hashlib.md5()
            with open(to_long_path(file_path), "rb") as file_handle:
                while chunk := file_handle.read(chunk_size):
                    if cancel.is_set():
                        return
                    hasher.update(chunk)
                    state["last_progress"] = time.monotonic()
            state["value"] = hasher.hexdigest()
        except Exception as exc:
            state["error"] = exc

    thread = threading.Thread(target=_do_hash, daemon=True)
    thread.start()
    while True:
        thread.join(timeout=min(1.0, timeout))
        if not thread.is_alive():
            break
        if time.monotonic() - state["last_progress"] > timeout:  # type: ignore[operator]
            cancel.set()
            message = (
                f"No data read for {timeout:.0f}s from '{file_path}' "
                "(it may be an unreachable network path or a stuck handle)"
            )
            print(f"WARNING: {message} - treating as unreadable.")
            if on_error is not None:
                on_error(TimeoutError(message))
            return None

    if "error" in state and on_error is not None:
        on_error(state["error"])  # type: ignore[arg-type]
    return state.get("value")  # type: ignore[return-value]


class _FailureTracker:
    """
    Thread-safe count of per-file failures in one scan step, keeping the
    first few error messages as examples. Ordinary errors (file not found,
    access denied) aren't printed one by one — at millions of files that
    would flood the terminal — but they must not vanish either: an
    unreachable drive makes *every* file fail in milliseconds, which
    otherwise looks exactly like a very fast successful scan.
    """

    def __init__(self, max_examples: int = 5) -> None:
        self.count = 0
        self.examples: list[str] = []
        self._max_examples = max_examples
        self._lock = threading.Lock()

    def __call__(self, error: BaseException) -> None:
        with self._lock:
            self.count += 1
            if len(self.examples) < self._max_examples:
                self.examples.append(f"{type(error).__name__}: {error}")

    def report(self, step: str, total: int) -> None:
        if not self.count:
            return
        print(f"WARNING: {self.count} of {total} file(s) failed during {step}. First error(s):")
        for example in self.examples:
            print(f"  - {example}")


def _check_resume_paths_reachable(
    file_paths: list[str], timeout: float, sample_size: int = 20
) -> None:
    """
    Before a --resume-from scan, stat a sample of the file list spread
    evenly across it, and stop with a clear error if *none* of them can be
    accessed. Resuming skips the folder walk, so nothing else checks the
    paths are reachable from *this* session — and a mapped drive letter
    (e.g. Z:) that existed when the list was written may not exist here
    (elevated/admin terminal, scheduled task, other user or remote
    session). Without this check, every file silently fails, fast.
    """
    if not file_paths:
        return
    step = max(1, len(file_paths) // sample_size)
    sample = file_paths[::step][:sample_size]
    errors = _FailureTracker(max_examples=3)
    for path in sample:
        stat_with_timeout(path, timeout=timeout, on_error=errors)
    if errors.count == len(sample):
        details = "\n".join(f"  - {example}" for example in errors.examples)
        raise SystemExit(
            f"ERROR: none of {len(sample)} sampled file(s) from the --resume-from "
            f"list can be accessed, e.g. '{sample[0]}':\n{details}\n"
            "If the paths use a mapped drive letter, check it exists in this "
            "terminal (mapped drives are per logon session: an elevated/admin "
            "terminal, scheduled task, or other user usually doesn't see them), "
            "or re-map it (e.g. `net use Z: \\\\server\\share`)."
        )


def scan_folder_md5(
    folder_path: str | Path,
    use_threading: bool = False,
    workers: int = 8,
    show_progress: bool = True,
    io_timeout: float = DEFAULT_IO_TIMEOUT_SECONDS,
    included: list[Path] | None = None,
    excluded: list[Path] | None = None,
    on_stat_complete: Callable[[pd.DataFrame], None] | None = None,
    resume_file_paths: list[str] | None = None,
) -> tuple[pd.DataFrame, list[dict[str, str]]]:
    """
    Recursively scan a folder, compute MD5 for every file, and return
    (df, skipped_folders): a DataFrame with columns path, filename,
    size_bytes, mtime, md5, one row per file found; and a list of
    directories that couldn't be accessed (see walk_files_with_skip_report).

    `on_stat_complete`, if given, is called once with a filename/path/mtime/
    size_gb DataFrame (no md5 yet — see build_prehash_export) right after
    the stat step finishes and before the slower hashing step starts, so a
    caller can write that out early (e.g. as a raw Excel export) well ahead
    of hashing completing.

    `resume_file_paths`, if given, skips the folder walk entirely (and
    `included`/`excluded`, which only apply to the walk) and scans exactly
    this file list instead — meant for resuming a scan whose hashing step
    was interrupted, from a raw export written by a previous run (see
    load_resume_file_paths). Size/mtime are still re-collected fresh and
    every file is still re-hashed, so a file that's since been deleted is
    simply treated as unreadable rather than causing an error.

    A locked/corrupted/unreachable folder or file is treated as unreadable
    (rather than aborting or hanging the scan) once it's unresponsive for
    `io_timeout` seconds — this covers listing a folder's contents,
    stat'ing a file, and reading a file's bytes for hashing, since any of
    those can independently hang on a dangling network handle.

    When use_threading is True, both the per-file stat step and the hashing
    step run concurrently using up to `workers` threads, which can speed
    things up on storage that supports concurrent reads (e.g. network
    volumes, SSD/NVMe) — and also means a single stuck file's io_timeout
    wait doesn't serialize behind every other file's.

    When show_progress is True, a progress bar tracks bytes hashed against
    the total bytes to hash (a more honest measure of "done" than file count
    when file sizes vary a lot), with files remaining shown alongside it.

    `included`/`excluded` optionally restrict the scan to, or prune it away
    from, specific subfolders of `folder_path` (see walk_files_with_skip_report).

    Memory: per-file state is kept as plain path strings plus numpy arrays
    of size/mtime and one md5 list, all indexed by position in the file
    list — not Path objects, a {Path: stats} dict, or a dict per file —
    so a multi-million-file scan fits in a few GB of RAM rather than
    tens of GB (see walk_files_with_skip_report).
    """
    folder = Path(folder_path)

    if show_progress:
        print(f"Scanning '{folder}' for files (this can take a while, so please be patient) ...")

    if resume_file_paths is not None:
        if show_progress:
            print(
                f"Resuming from {len(resume_file_paths)} previously discovered "
                "file(s); skipping the folder walk."
            )
        file_paths = resume_file_paths
        skipped_folders = []
        _check_resume_paths_reachable(file_paths, timeout=io_timeout)
    else:
        file_paths, skipped_folders = walk_files_with_skip_report(
            folder,
            io_timeout=io_timeout,
            show_progress=show_progress,
            included=included,
            excluded=excluded,
        )

    # (size_bytes, mtime) per file; mtime feeds the "oldest copy" columns in
    # build_duplicate_summary()/compare_cross_folder_duplicates() so users
    # know which duplicate to keep.
    total_files = len(file_paths)
    sizes = np.zeros(total_files, dtype=np.int64)
    mtimes = np.zeros(total_files, dtype=np.float64)
    stat_failures = _FailureTracker()
    hash_failures = _FailureTracker()

    def _stat(index: int) -> int:
        sizes[index], mtimes[index] = stat_with_timeout(
            file_paths[index], timeout=io_timeout, on_error=stat_failures
        )
        return index

    with tqdm(
        total=total_files,
        desc=f"Collecting file info ({folder.name or folder})",
        unit="file",
        disable=not show_progress,
    ) as stat_progress:
        if use_threading:
            with ThreadPoolExecutor(max_workers=workers) as executor:
                for index in _bounded_map(
                    executor, _stat, range(total_files), max_in_flight=workers * 64
                ):
                    stat_progress.set_postfix(
                        last=shorten_name(os.path.basename(file_paths[index])), refresh=False
                    )
                    stat_progress.update(1)
        else:
            for index in range(total_files):
                stat_progress.set_postfix(
                    now=shorten_name(os.path.basename(file_paths[index])), refresh=True
                )
                _stat(index)
                stat_progress.update(1)

    stat_failures.report("collecting file info", total_files)
    if total_files and stat_failures.count == total_files:
        raise SystemExit(
            f"ERROR: every one of the {total_files} file(s) failed to stat, so "
            "there is nothing to hash - stopping instead of producing empty "
            "results. See the errors above: the drive/share is most likely "
            "not reachable from this terminal."
        )

    if on_stat_complete is not None:
        if show_progress:
            print(f"Writing pre-hash file listing ({total_files} file(s)) ...")
        on_stat_complete(build_prehash_export(file_paths, sizes, mtimes))

    md5s: list[str | None] = [None] * total_files
    files_left = total_files
    progress_lock = threading.Lock()

    with tqdm(
        total=int(sizes.sum()),
        unit="B",
        unit_scale=True,
        unit_divisor=1024,
        desc=f"Hashing {folder.name or folder}",
        disable=not show_progress,
    ) as progress:

        def hash_and_report(index: int) -> None:
            nonlocal files_left
            # Shown *before* hashing starts (not after), so it stays visible
            # for the whole time a large file is being read — otherwise a
            # multi-GB file would look identical to a genuine hang until it
            # finally finishes.
            file_path = file_paths[index]
            with progress_lock:
                progress.set_postfix(
                    now=shorten_name(os.path.basename(file_path)), files_left=files_left, refresh=True
                )
            md5s[index] = calculate_md5(file_path, timeout=io_timeout, on_error=hash_failures)
            with progress_lock:
                files_left -= 1
                progress.set_postfix(files_left=files_left, refresh=False)
                progress.update(int(sizes[index]))

        if use_threading:
            with ThreadPoolExecutor(max_workers=workers) as executor:
                for _ in _bounded_map(
                    executor, hash_and_report, range(total_files), max_in_flight=workers * 64
                ):
                    pass
        else:
            for index in range(total_files):
                hash_and_report(index)

    hash_failures.report("hashing", total_files)
    if show_progress:
        print(f"Hashing done; building results table for {total_files} file(s) ...")
    df = pd.DataFrame({
        "path": file_paths,
        "filename": [os.path.basename(p) for p in file_paths],
        "size_bytes": sizes,
        "mtime": mtimes,
        "md5": md5s,
    })
    return df, skipped_folders


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


def validate_folder(folder_path: str | Path, label: str) -> Path:
    """Validate that the provided folder exists and is a directory."""
    path = Path(folder_path)
    if not path.exists():
        raise ValueError(f"{label} folder does not exist: {path}")
    if not path.is_dir():
        raise ValueError(f"{label} path is not a directory: {path}")
    return path


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


def _stringify_object_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Convert list/dict values to JSON strings before exporting."""
    if df.empty:
        return df.copy()

    export_df = df.copy()
    object_columns = export_df.select_dtypes(include=["object"]).columns

    for column in object_columns:
        export_df[column] = export_df[column].apply(
            lambda value: json.dumps(value, ensure_ascii=False)
            if isinstance(value, (list, dict))
            else value
        )

    return export_df


def export_results_to_excel(
    output_path: str | Path, sheets: dict[str, pd.DataFrame]
) -> Path:
    """Write result DataFrames to an Excel workbook."""
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    with pd.ExcelWriter(output) as writer:
        for sheet_name, dataframe in sheets.items():
            export_df = _stringify_object_columns(dataframe)
            export_df.to_excel(writer, sheet_name=sheet_name[:31], index=False)

    return output


def build_raw_file_export(scan_frames: list[pd.DataFrame]) -> pd.DataFrame:
    """
    Combine one or more scan_folder_md5() DataFrames into a tidy, one-row-
    per-file listing meant for external analysis (e.g. in R): filename,
    path, human-readable mtime, md5, and size in GB. Every file the script
    looked at, regardless of duplicate/mismatch status.
    """
    combined = pd.concat(scan_frames, ignore_index=True)
    return pd.DataFrame({
        "filename": combined["filename"],
        "path": combined["path"],
        "mtime": pd.to_datetime(combined["mtime"], unit="s").dt.strftime("%Y-%m-%d %H:%M:%S"),
        "md5": combined["md5"],
        "size_gb": combined["size_bytes"] / (1024 ** 3),
    })


def build_prehash_export(
    file_paths: list[str], sizes: np.ndarray, mtimes: np.ndarray
) -> pd.DataFrame:
    """
    Same filename/path/mtime/size_gb columns as build_raw_file_export(), but
    built directly from file paths + stat results rather than a finished
    scan — used to write the raw export early, right after the stat step
    and before the slower hashing step (see scan_folder_md5's
    on_stat_complete). Has no md5 column since hashing hasn't run yet.

    mtime is converted with one vectorized pd.to_datetime() call over the
    whole column, not a per-file scalar pd.to_datetime() in a loop — at a
    few million files the scalar form's per-call overhead adds up to
    upwards of ten minutes with no progress feedback, looking like a hang
    rather than the ~40x-slower path it actually is.
    """
    return pd.DataFrame({
        "filename": [os.path.basename(p) for p in file_paths],
        "path": file_paths,
        "mtime": pd.to_datetime(pd.Series(mtimes), unit="s").dt.strftime("%Y-%m-%d %H:%M:%S"),
        "size_gb": sizes / (1024 ** 3),
    })


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


def build_skipped_folders_sheet(
    scoped_skips: list[tuple[str, list[dict[str, str]]]],
) -> pd.DataFrame:
    """
    Combine skipped-folder lists from one or more scans (each tagged with a
    scope label, e.g. "Origin"/"Destination"/"Folder") into one DataFrame:
    columns scope, folder, error. Empty (columns only) if nothing was
    skipped anywhere.
    """
    rows = [
        {"scope": scope, "folder": entry["folder"], "error": entry["error"]}
        for scope, skipped in scoped_skips
        for entry in skipped
    ]
    return pd.DataFrame(rows, columns=["scope", "folder", "error"])


def write_csv(df: pd.DataFrame, path: str | Path) -> Path:
    """
    Write a DataFrame to CSV, creating the output folder first if it doesn't
    exist yet — plain `df.to_csv()` fails with FileNotFoundError otherwise.
    """
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(output, index=False)
    return output


def raw_export_path(excel_path: str | Path) -> Path:
    """
    Path for the companion raw-file-listing CSV, alongside the main report.

    Always `.csv`, regardless of the main report's extension: unlike
    `.xlsx`, CSV has no per-sheet row limit (Excel tops out at 1,048,576
    rows per sheet) and writes/reads far faster for a scan with millions of
    files, which is exactly the "every file, one row each" shape this
    listing has.
    """
    path = Path(excel_path)
    return path.with_name(f"{path.stem}_raw.csv")


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
        choices=["all", "folder", "origin", "destination", "between", "integrity"],
        default="all",
        help="Which check to run.",
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
        if args.mode not in {"all", "folder"}:
            parser.error("When using --folder, mode must be 'all' or 'folder'.")
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

    needs_origin_scan = args.mode in {"all", "origin", "between", "integrity"}
    needs_destination_scan = args.mode in {"all", "destination", "between", "integrity"}

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
    if needs_origin_scan:
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
    else:
        origin_scan, origin_skipped = None, []

    if needs_destination_scan:
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
    else:
        destination_scan, destination_skipped = None, []

    total_skipped = len(origin_skipped) + len(destination_skipped)
    if total_skipped:
        print(
            f"\n{total_skipped} folder(s) could not be accessed and were "
            "skipped (see warnings above)."
        )

    if args.mode in {"all", "origin"}:
        print("Finding origin duplicates and building summary ...")
        origin_duplicates = find_real_duplicates(origin_scan)
        origin_summary = build_duplicate_summary(origin_duplicates)
        print_duplicate_report("Origin Duplicates", origin_summary)

        if args.excel:
            excel_sheets["origin_duplicates"] = origin_duplicates
            excel_sheets["origin_summary"] = origin_summary

    if args.mode in {"all", "destination"}:
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