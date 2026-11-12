"""
Helpers shared by duplicates_check_V2.py, elimination_file_candidates_V2.py,
quarantine_duplicates_V2.py and archive_old_files_V2.py.

Each of those scripts needs only this file next to it, not the other scripts.
"""
from __future__ import annotations

import csv
import fnmatch
import hashlib
import json
import os
import re
import threading
import time
from collections import Counter, deque
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import Executor, ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from tqdm import tqdm


# --------------------------------------------------------------------------
# Shared constants
# --------------------------------------------------------------------------
# Several of these are a contract between the scripts: archive_old_files_V2.py
# recognizes quarantine_duplicates_V2.py's folder and notes by these names, and
# every script reads and writes raw listings with RAW_COLUMNS.

DEFAULT_IO_TIMEOUT_SECONDS = 10.0
DUPLICATES_FOLDER_NAME = "duplicates"
QUARANTINE_NOTE_FILENAME = "original_file_location.txt"
# Some file systems (FAT/exFAT, some NAS shares) only store mtime to 2s,
# and a CSV round trip can round it, so an exact comparison would flag
# unchanged files as changed.
MTIME_TOLERANCE_SECONDS = 2.0
# Windows MAX_PATH is 260 including the terminating NUL. Longer paths still
# work here (see to_long_path) but are flagged, since other tools may not
# handle them.
MAX_PATH_CHARS = 259
RETRY_RESULTS_SUFFIX = "__movedFailed"
RAW_COLUMNS = ["filename", "path", "mtime", "md5", "size_gb"]
# CSVs are streamed in blocks of this many rows rather than loaded whole
# (see the memory note in quarantine_duplicates_V2.py).
CHUNK_ROWS = 20_000


# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------

def _key(path: str) -> str:
    """Comparison key for a path: case-insensitive, normalized separators."""
    return os.path.normcase(os.path.abspath(path))


def _is_under(path: str, folder: str) -> bool:
    key, folder_key = _key(path), _key(folder)
    return key == folder_key or key.startswith(folder_key.rstrip("\\/") + os.sep)


# --------------------------------------------------------------------------
# CSV streaming
# --------------------------------------------------------------------------

def iter_csv(
    path: str | Path, columns: list[str] | None = None, chunksize: int = CHUNK_ROWS
) -> Iterator[pd.DataFrame]:
    """
    Read a CSV in blocks, every column as a string. keep_default_na=False: a
    file literally named "NA" or "null" must stay a string, not become NaN.
    Rows broken by a process being killed mid-write are skipped.
    """
    try:
        reader = pd.read_csv(
            path, usecols=columns, dtype=str, keep_default_na=False,
            chunksize=chunksize, on_bad_lines="skip",
        )
    except ValueError as exc:
        raise SystemExit(f"ERROR: '{path}' is missing expected column(s) {columns}: {exc}") from exc
    yield from reader


# --------------------------------------------------------------------------
# Plan, settings and journal files
# --------------------------------------------------------------------------

def journal_path(prefix: Path) -> Path:
    return Path(f"{prefix}_journal.csv")


def undo_journal_path(prefix: Path) -> Path:
    return Path(f"{prefix}_undo_journal.csv")


def settings_path(plan_path: Path) -> Path:
    return plan_path.with_name(f"{plan_path.stem}_settings.json")


def load_settings(plan_path: Path, script: str, plan_format: int, unnamed_is_mine: bool = False) -> dict:
    """
    The settings written next to a plan by `script`'s 'plan' step. Stops if
    the plan was made by another script or by another plan format version.
    Settings record the script that wrote them under "script"; with
    `unnamed_is_mine`, settings without it (written before that field
    existed) are taken as `script`'s own.

    `script` is the tool's name without a version ("archive_old_files",
    not "archive_old_files_V2"): the V2 scripts write the same plans as
    the originals, so each version accepts the other's plans.
    """
    if not plan_path.is_file():
        raise SystemExit(f"ERROR: plan file does not exist: {plan_path}")
    if not settings_path(plan_path).is_file():
        raise SystemExit(
            f"ERROR: the plan's settings file is missing: {settings_path(plan_path)} "
            "(it is written next to the plan by the 'plan' step)."
        )
    settings = json.loads(settings_path(plan_path).read_text(encoding="utf-8"))
    maker = settings.get("script", script if unnamed_is_mine else None)
    if maker != script:
        made_by = maker if maker else "an unknown script"
        raise SystemExit(f"ERROR: '{plan_path}' was made by {made_by}, not by {script}.")
    if settings.get("plan_format") != plan_format:
        raise SystemExit(
            f"ERROR: '{plan_path}' was made with another plan format than this version of {script}. "
            "Re-run 'plan' to make a new one."
        )
    return settings


def iter_plan(plan_path: Path, columns: list[str]) -> Iterator[pd.DataFrame]:
    """A plan in blocks, checked to have `columns`, with `action` stripped and upper-cased."""
    for chunk in iter_csv(plan_path):
        missing = [c for c in columns if c not in chunk.columns]
        if missing:
            raise SystemExit(f"ERROR: plan '{plan_path}' is missing column(s): {missing}")
        chunk["action"] = chunk["action"].str.strip().str.upper()
        yield chunk


def check_output_folder(output: str | Path, folder: str, why: str) -> None:
    """Stop if --output is inside `folder` (the folder files are moved into); `why` says why that's bad."""
    if _is_under(str(output), folder):
        raise SystemExit(f"ERROR: --output '{output}' is inside '{folder}'; {why}")


# --------------------------------------------------------------------------
# Filesystem access with timeouts, scanning and hashing (from duplicates_check_V2.py)
# --------------------------------------------------------------------------

def to_long_path(path: str | Path) -> str:
    """
    Return `path` in Windows extended-length form (\\\\?\\C:\\... or
    \\\\?\\UNC\\server\\share\\...), which lifts the 260-character MAX_PATH
    limit for that call regardless of the system's LongPathsEnabled setting
    (off by default). Without it, a folder or file whose full path is over
    the limit can't be listed, stat'ed or opened ("The system cannot find
    the path specified"), so it's silently left out of the scan.

    Only used at the point of each filesystem call: paths stored in results
    and reports stay in their normal form (see duplicates_check.from_long_path).
    """
    if os.name != "nt":
        return str(path)
    path = os.path.abspath(path)  # \\?\ paths aren't normalized: no "/", "." or ".."
    if path.startswith("\\\\?\\"):
        return path
    if path.startswith("\\\\"):
        return "\\\\?\\UNC\\" + path[2:]
    return "\\\\?\\" + path


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
    from "unknown" (as elimination_file_candidates_V2.py does, to exclude
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


def validate_folder(folder_path: str | Path, label: str) -> Path:
    """Validate that the provided folder exists and is a directory."""
    path = Path(folder_path)
    if not path.exists():
        raise ValueError(f"{label} folder does not exist: {path}")
    if not path.is_dir():
        raise ValueError(f"{label} path is not a directory: {path}")
    return path


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


# --------------------------------------------------------------------------
# Path fingerprints, file checks, CSV writing, notes, excludes (from quarantine_duplicates_V2.py)
# --------------------------------------------------------------------------

def path_hash(path: str) -> int:
    """
    64-bit fingerprint of a path's comparison key. Sets of millions of paths
    (moved files, files found in the duplicates folder) are kept as sorted
    numpy arrays of these - 8 bytes per path instead of ~300 for the string.
    The chance of two different paths sharing a fingerprint is about 1 in a
    million even at 6 million paths.
    """
    digest = hashlib.blake2b(_key(path).encode("utf-8", "surrogatepass"), digest_size=8).digest()
    return int.from_bytes(digest, "little")


def hash_array(paths: Iterable[str], count: int, desc: str | None = None) -> np.ndarray:
    """path_hash of every path, as a uint64 array (optionally with a progress bar)."""
    if not desc:
        return np.fromiter((path_hash(p) for p in paths), dtype=np.uint64, count=count)
    with tqdm(total=count, desc=desc, unit="file") as progress:
        result = np.empty(count, dtype=np.uint64)
        for i, path in enumerate(paths):
            result[i] = path_hash(path)
            if i % 10_000 == 0:
                progress.update(min(10_000, count - progress.n))
        progress.update(count - progress.n)
        return result


class KeySet:
    """
    Set membership for path fingerprints: a sorted uint64 array (built once)
    plus a small Python set for fingerprints added during the run.
    """

    def __init__(self, hashes: np.ndarray | None = None) -> None:
        self._sorted = np.unique(hashes) if hashes is not None and len(hashes) else np.zeros(0, np.uint64)
        self._added: set[int] = set()

    def __contains__(self, value: int) -> bool:
        if value in self._added:
            return True
        index = int(np.searchsorted(self._sorted, np.uint64(value)))
        return index < len(self._sorted) and int(self._sorted[index]) == value

    def add(self, value: int) -> None:
        self._added.add(value)  # set.add is atomic under the GIL

    def __len__(self) -> int:
        return len(self._sorted) + len(self._added)


def relative_to_root(path: str, root_abs: str) -> str | None:
    """`path` relative to `root_abs`, or None if it isn't under it."""
    prefix = root_abs.rstrip("\\/") + os.sep
    if not os.path.normcase(path).startswith(os.path.normcase(prefix)):
        return None
    return path[len(prefix):]


def _fmt_mtime(epoch: float) -> str:
    # UTC, same as duplicates_check_V2.py's exports (pd.to_datetime(unit="s")).
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _row_check(values: Iterable[str]) -> str:
    """Fingerprint of a plan row's CHECKED_COLUMNS (as the strings in the CSV)."""
    joined = "\x1f".join(values).encode("utf-8", "surrogatepass")
    return hashlib.blake2b(joined, digest_size=8).hexdigest()


def count_rows(path: str | Path) -> int:
    """Data rows in a CSV (newlines minus the header), for progress bar totals."""
    count = 0
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(16 << 20), b""):
            count += block.count(b"\n")
    return max(count - 1, 0)


class CsvAppender:
    """
    Thread-safe CSV writer, one row at a time, flushed immediately so rows
    survive the process being killed. With durable=True it is also fsync'd
    at most every couple of seconds (fsync per row would serialize millions
    of moves behind the disk); losing that last moment only matters for a
    power cut, and resuming works from the files on disk anyway.
    """

    def __init__(self, path: Path, columns: list[str], append: bool, durable: bool = False) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        is_new = not append or not path.exists() or path.stat().st_size == 0
        needs_newline = False
        if not is_new:
            # A kill can cut the last row short; start on a fresh line so
            # the next row isn't glued onto it (the broken row is skipped
            # when reading, see iter_csv).
            with open(path, "rb") as handle:
                handle.seek(-1, os.SEEK_END)
                needs_newline = handle.read(1) != b"\n"
        self._handle = open(path, "a" if not is_new else "w", newline="", encoding="utf-8")
        if needs_newline:
            self._handle.write("\r\n")
        self._writer = csv.writer(self._handle)
        if is_new:
            self._writer.writerow(columns)
        self._durable = durable
        self._last_sync = 0.0
        self._lock = threading.Lock()
        self._sync(force=True)

    def _sync(self, force: bool = False) -> None:
        self._handle.flush()
        if self._durable and (force or time.monotonic() - self._last_sync >= 2.0):
            os.fsync(self._handle.fileno())
            self._last_sync = time.monotonic()

    def append(self, values: list) -> None:
        with self._lock:
            self._writer.writerow(values)
            self._sync()

    def close(self) -> None:
        with self._lock:
            self._sync(force=True)
            self._handle.close()

    def __enter__(self) -> "CsvAppender":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


Probe = tuple[str, int, float, str]  # (state, size, mtime, error)


def probe(path: str, timeout: float) -> Probe:
    """
    stat() `path`: ("exists", size, mtime, ""), ("missing", 0, 0, ""), or
    ("error", 0, 0, message) for permission denied, timeouts, etc.
    "missing" and "error" are kept apart because only a genuinely missing
    file means "already moved" when resuming. Only these three values are
    kept (not the whole stat result), since pre-flight holds thousands.
    """
    value, error = _run_with_timeout(
        lambda: os.stat(to_long_path(path)),
        timeout,
        f"Timed out after {timeout:.0f}s stat'ing '{path}'",
    )
    if error is None:
        return "exists", value.st_size, value.st_mtime, ""  # type: ignore[union-attr]
    if isinstance(error, FileNotFoundError):
        return "missing", 0, 0.0, ""
    return "error", 0, 0.0, f"{type(error).__name__}: {error}"


def matches_scan(result: Probe, size: int, mtime: float) -> bool:
    return result[1] == size and abs(result[2] - mtime) <= MTIME_TOLERANCE_SECONDS


def check_against_scan(result: Probe, size: int, mtime: float, label: str) -> tuple[str, str] | None:
    """
    None if the probed file exists with the size/mtime recorded in the scan,
    else (reason, detail), e.g. ("KEEPER_CHANGED", "size ... vs scan ...").
    """
    state, current_size, current_mtime, error = result
    if state == "missing":
        return f"{label}_MISSING", ""
    if state == "error":
        return f"{label}_UNREADABLE", error
    if not matches_scan(result, size, mtime):
        return (
            f"{label}_CHANGED",
            f"size {current_size} vs scan {size}; "
            f"mtime {_fmt_mtime(current_mtime)} vs scan {_fmt_mtime(mtime)}",
        )
    return None


_NOTE_LOCKS = [threading.Lock() for _ in range(512)]


def _note_lock(folder: str) -> threading.Lock:
    return _NOTE_LOCKS[hash(_key(folder)) % len(_NOTE_LOCKS)]


def _read_note(note_path: str) -> list[str]:
    try:
        with open(to_long_path(note_path), encoding="utf-8") as handle:
            return handle.readlines()
    except FileNotFoundError:
        return []


def output_prefix(plan_path: Path, output: str | None) -> Path:
    """Base path for a plan's journal/report files: <output>/<plan stem>."""
    folder = Path(output) if output else plan_path.parent
    return folder / plan_path.stem


def execute_results_path(prefix: Path, retry_failed: bool = False) -> Path:
    """<prefix>_execute_results.csv, or its __movedFailed twin for --retry-failed runs."""
    return Path(f"{prefix}_execute_results{RETRY_RESULTS_SUFFIX if retry_failed else ''}.csv")


def execute_outcomes(prefix: Path, keep: Callable[[str], bool]) -> dict[int, tuple[str, str]]:
    """
    Latest execute outcome per file whose status passes `keep`, as path
    fingerprint -> (status, detail). A full run's results cover every MOVE
    row; a --retry-failed run's results cover only the rows it retried and
    override the full run's, unless a full run was made after them.
    """
    full, retry = execute_results_path(prefix), execute_results_path(prefix, retry_failed=True)
    if not full.exists():
        return {}
    outcomes: dict[int, tuple[str, str]] = {}
    for chunk in iter_csv(full, ["status", "detail", "original_path"]):
        chunk = chunk[chunk["status"].map(keep)]
        for status, detail, path in zip(chunk["status"], chunk["detail"], chunk["original_path"]):
            outcomes[path_hash(path)] = (status, detail)
    if retry.exists() and retry.stat().st_mtime > full.stat().st_mtime:
        for chunk in iter_csv(retry, ["status", "detail", "original_path"]):
            for status, detail, path in zip(chunk["status"], chunk["detail"], chunk["original_path"]):
                if keep(status):
                    outcomes[path_hash(path)] = (status, detail)
                else:
                    outcomes.pop(path_hash(path), None)
    return outcomes


def _print_counts(title: str, counts: Counter, empty: str) -> None:
    print(f"\n=== {title} ===")
    print(" | ".join(f"{status} {count}" for status, count in counts.most_common()) or empty)


def exclude_folder(pattern: str) -> str | None:
    """
    The folder an --exclude pattern names, if it's a plain path: no * or ?,
    starting with a drive (R:\\...) or \\\\server\\share (or / elsewhere), with
    or without a trailing separator. None for wildcard patterns and bare
    names ('thumbs.db'). Brackets count as part of the name here.
    """
    if "*" in pattern or "?" in pattern:
        return None
    cleaned = pattern.strip()
    if re.fullmatch(r"[A-Za-z]:[\\/]*", cleaned):
        return os.path.abspath(cleaned[:2] + os.sep)
    if not (re.match(r"[A-Za-z]:[\\/]", cleaned) or cleaned.startswith(("\\\\", "//")) or os.path.isabs(cleaned)):
        return None
    return os.path.abspath(cleaned.rstrip("\\/"))


def check_exclude_patterns(patterns: list[str] | None, root_abs: str) -> None:
    """Stop before planning if an --exclude pattern can't be what was meant."""
    for pattern in patterns or []:
        if '"' in pattern:
            raise SystemExit(
                f"ERROR: --exclude {pattern!r} contains a quote. In PowerShell, a path ending in a "
                'backslash followed by a quote ("R:\\folder\\") passes the quote on: write the folder '
                'without the trailing backslash ("R:\\folder"), or in single quotes. No plan was written.'
            )
        folder = exclude_folder(pattern)
        if folder is not None and relative_to_root(folder, root_abs) is None:
            raise SystemExit(
                f"ERROR: --exclude {pattern!r} is a folder that isn't under --root '{root_abs}'. "
                "Write it as the listing does (same drive letter or \\\\server\\share spelling). "
                "No plan was written."
            )


def build_exclude_matcher(patterns: list[str]) -> Callable[[str], str | None]:
    """
    Return a function giving the first pattern a path matches (or None),
    case-insensitively. A plain folder path (see exclude_folder) matches
    everything in that folder and below; any other pattern is a wildcard
    matched against both the file name ('thumbs.db', '*.ini') and the full
    path ('*\\.git\\*', 'R:\\Old\\*').
    """
    rules = []
    for original in patterns:
        folder = exclude_folder(original)
        if folder is not None:
            rules.append((original, True, os.path.normcase(folder).rstrip("\\/") + os.sep))
        else:
            rules.append((original, False, os.path.normcase(original)))

    def _match(path: str) -> str | None:
        full = os.path.normcase(path)
        name = os.path.basename(full)
        for original, is_folder, value in rules:
            if is_folder:
                if full.startswith(value):
                    return original
            elif fnmatch.fnmatchcase(name, value) or fnmatch.fnmatchcase(full, value):
                return original
        return None

    return _match


def report_excludes(patterns: list[str] | None, counts: Counter, unit: str, hint: str = "") -> None:
    """Per --exclude pattern, how many files it excluded; a warning for those that excluded none."""
    for pattern in patterns or []:
        n = counts.get(pattern, 0)
        kind = "folder" if exclude_folder(pattern) is not None else "pattern"
        print(f"  --exclude {pattern!r} ({kind}): {n} {unit}")
        if n == 0:
            print(
                f"  WARNING: --exclude {pattern!r} matched nothing.{hint} Check its spelling: a folder must be "
                "written as in the listing (same drive letter or \\\\server\\share); a wildcard pattern must "
                "match the whole path (e.g. '*\\Old Data\\*')."
            )
