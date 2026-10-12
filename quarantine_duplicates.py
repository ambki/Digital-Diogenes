from __future__ import annotations

import argparse
import csv
import fnmatch
import gc
import hashlib
import json
import os
import re
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from itertools import chain
from pathlib import Path

import numpy as np
import pandas as pd
from tqdm import tqdm

from duplicates_check import (
    DEFAULT_IO_TIMEOUT_SECONDS,
    _bounded_map,
    _run_with_timeout,
    calculate_md5,
    scan_folder_md5,
    to_long_path,
)


DUPLICATES_FOLDER_NAME = "duplicates"
NOTE_FILENAME = "original_file_location.txt"
NOTE_HEADER = (
    "# Duplicate files moved out of this folder by quarantine_duplicates.py. "
    "Each line: the moved file, where it is now, and the identical copy that was kept.\n"
)
# Some file systems (FAT/exFAT, some NAS shares) only store mtime to 2s,
# and a CSV round trip can round it, so an exact comparison would flag
# unchanged files as changed.
MTIME_TOLERANCE_SECONDS = 2.0
# Windows MAX_PATH is 260 including the terminating NUL. Longer paths still
# work here (see to_long_path) but are flagged, since other tools may not
# handle them.
MAX_PATH_CHARS = 259
DEFAULT_EXCLUDES = ["desktop.ini", "thumbs.db", "*\\.git\\*", NOTE_FILENAME]

# Memory: every command streams its CSVs in blocks of CHUNK_ROWS rows rather
# than loading them whole. At ~6 million duplicates (a 1.7 GB duplicates
# CSV), holding the list in memory even once is several GB of Python
# strings, and each processing step used to add another copy - enough to
# push an 8 GB machine into swap. Sets of millions of paths are kept as
# 8-byte fingerprints in sorted numpy arrays (see path_hash/KeySet), not as
# ~300-byte strings. Blocks are kept small so every progress bar moves at
# least about once a second (bars advance per block); measured, smaller
# blocks cost no extra time.
CHUNK_ROWS = 20_000
# Pre-flight checks are sent to the file server in batches this size, so
# the progress bar moves steadily while a block is being checked.
PREFLIGHT_BATCH = 5_000
# Version of the plan file layout; plans written by an earlier version
# (no role/check columns) must be re-made.
PLAN_FORMAT = 2

PLAN_COLUMNS = [
    "action", "reason", "detail", "role", "md5", "size_bytes", "size_gb", "mtime", "modified",
    "original_path", "duplicates_path", "keeper_path", "keeper_size_bytes",
    "keeper_mtime", "long_path", "check",
]
# The columns `check` fingerprints: everything that follows from the inputs.
# `action` (and reason/detail) is deliberately left out, since switching it
# between MOVE and SKIP is the one supported edit of a plan.
CHECKED_COLUMNS = [
    "role", "md5", "original_path", "duplicates_path", "keeper_path",
    "size_bytes", "mtime", "keeper_size_bytes", "keeper_mtime",
]
JOURNAL_COLUMNS = [
    "timestamp", "status", "md5", "size_bytes", "mtime",
    "original_path", "duplicates_path", "keeper_path",
]
MOVED_STATUSES = {"MOVED", "MOVED_RESUMED"}
EXECUTE_RESULT_COLUMNS = [
    "status", "detail", "md5", "size_bytes", "modified",
    "original_path", "duplicates_path", "keeper_path", "long_path",
]
VERIFY_COLUMNS = [
    "status", "detail", "md5_expected", "md5_found", "original_path", "duplicates_path",
    "keeper_path", "size_bytes", "size_found", "mtime_original", "mtime_found", "long_path",
]
UNDO_RESULT_COLUMNS = ["status", "detail", "md5", "size_bytes", "mtime", "original_path", "duplicates_path", "keeper_path"]
RAW_COLUMNS = ["filename", "path", "mtime", "md5", "size_gb"]


# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------
# Every filesystem call below goes through to_long_path (from
# duplicates_check.py): moving a file into <root>\duplicates\ makes its path
# 11 characters longer, so a source path just under Windows' 260-character
# limit can end up just over it. Reports always show normal paths.


def _key(path: str) -> str:
    """Comparison key for a path: case-insensitive, normalized separators."""
    return os.path.normcase(os.path.abspath(path))


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


def _is_under(path: str, folder: str) -> bool:
    key, folder_key = _key(path), _key(folder)
    return key == folder_key or key.startswith(folder_key.rstrip("\\/") + os.sep)


def _fmt_mtime(epoch: float) -> str:
    # UTC, same as duplicates_check.py's exports (pd.to_datetime(unit="s")).
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _row_check(values: Iterable[str]) -> str:
    """Fingerprint of a plan row's CHECKED_COLUMNS (as the strings in the CSV)."""
    joined = "\x1f".join(values).encode("utf-8", "surrogatepass")
    return hashlib.blake2b(joined, digest_size=8).hexdigest()


# --------------------------------------------------------------------------
# CSV streaming
# --------------------------------------------------------------------------

def count_rows(path: str | Path) -> int:
    """Data rows in a CSV (newlines minus the header), for progress bar totals."""
    count = 0
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(16 << 20), b""):
            count += block.count(b"\n")
    return max(count - 1, 0)


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


# --------------------------------------------------------------------------
# Filesystem probes (all with timeouts, see duplicates_check._run_with_timeout)
# --------------------------------------------------------------------------

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


# --------------------------------------------------------------------------
# Notes (original_file_location.txt)
# --------------------------------------------------------------------------
# One appended note per source folder. Moves run on several threads, so
# writes to a folder's note are serialized by a lock picked from a fixed
# pool by folder (two folders may share a lock; that only costs a wait).

_NOTE_LOCKS = [threading.Lock() for _ in range(512)]


def _note_lock(folder: str) -> threading.Lock:
    return _NOTE_LOCKS[hash(_key(folder)) % len(_NOTE_LOCKS)]


def _note_now_at(line: str) -> str | None:
    """Comparison key of a note line's `now_at` path, or None if it has none."""
    # " | " and ": " can't occur inside a Windows path or filename ("|" and
    # ":" are invalid in names; a drive is "C:\", not "C: "), so splitting
    # on them is safe.
    for part in line.rstrip("\r\n").split(" | "):
        key, sep, value = part.partition(": ")
        if sep and key.strip() == "now_at":
            return _key(value)
    return None


def _read_note(note_path: str) -> list[str]:
    try:
        with open(to_long_path(note_path), encoding="utf-8") as handle:
            return handle.readlines()
    except FileNotFoundError:
        return []


def note_add(row, timeout: float, check_existing: bool) -> str:
    """
    Append the note line for a moved file. A fresh move can't already have
    a line (the file was still in place), so the note is only read first
    (check_existing) when resuming - reading it on every move would make a
    folder with thousands of moved files quadratically slow.
    Returns "" or an error message.
    """
    folder = os.path.dirname(row.original_path)
    note_path = os.path.join(folder, NOTE_FILENAME)
    line = (
        f"moved_file: {os.path.basename(row.original_path)} | now_at: {row.duplicates_path} | "
        f"original_kept_at: {row.keeper_path} | md5: {row.md5} | moved_on: {_now()}\n"
    )

    def _add() -> None:
        if check_existing:
            target = _key(row.duplicates_path)
            if any(_note_now_at(existing) == target for existing in _read_note(note_path)):
                return
        with open(to_long_path(note_path), "a", encoding="utf-8") as handle:
            if handle.tell() == 0:
                handle.write(NOTE_HEADER)
            handle.write(line)

    with _note_lock(folder):
        _, error = _run_with_timeout(_add, timeout, f"Timed out after {timeout:.0f}s writing '{note_path}'")
    return "" if error is None else f"{type(error).__name__}: {error}"


def note_remove(folder: str, duplicates_keys: set[str], timeout: float) -> str:
    """Remove the lines for these duplicates paths from a folder's note, in one rewrite."""
    note_path = os.path.join(folder, NOTE_FILENAME)

    def _remove() -> None:
        lines = _read_note(note_path)
        kept = [line for line in lines if _note_now_at(line) not in duplicates_keys]
        if len(kept) == len(lines):
            return
        if all(line.startswith("#") or not line.strip() for line in kept):
            os.remove(to_long_path(note_path))
        else:
            with open(to_long_path(note_path), "w", encoding="utf-8") as handle:
                handle.writelines(kept)

    with _note_lock(folder):
        _, error = _run_with_timeout(_remove, timeout, f"Timed out after {timeout:.0f}s updating '{note_path}'")
    return "" if error is None else f"{type(error).__name__}: {error}"


# --------------------------------------------------------------------------
# Journal state
# --------------------------------------------------------------------------

def journal_path(prefix: Path) -> Path:
    return Path(f"{prefix}_journal.csv")


def undo_journal_path(prefix: Path) -> Path:
    return Path(f"{prefix}_undo_journal.csv")


def iter_journal(path: Path, statuses: set[str]) -> Iterator[pd.DataFrame]:
    """Complete journal rows with one of `statuses`, in blocks."""
    if not path.exists():
        return
    for chunk in iter_csv(path):
        if any(c not in chunk.columns for c in JOURNAL_COLUMNS):
            raise SystemExit(f"ERROR: '{path}' is not a journal written by this script.")
        valid = chunk["status"].isin(statuses) & (chunk["original_path"] != "") & (chunk["keeper_path"] != "")
        yield chunk[valid]


def moved_now(prefix: Path) -> KeySet:
    """
    Fingerprints of the files currently moved out, per the journals: a file
    counts as moved if its latest event is a move (journal) rather than a
    restore (undo journal). Replaying both handles move -> undo -> move.
    """
    hashes, times, order, moved = [], [], [], []
    sequence = 0
    sources = [(journal_path(prefix), MOVED_STATUSES, True), (undo_journal_path(prefix), {"RESTORED"}, False)]
    for path, statuses, is_move in sources:
        for chunk in iter_journal(path, statuses):
            n = len(chunk)
            hashes.append(hash_array(chunk["original_path"], n))
            times.append(pd.to_datetime(chunk["timestamp"], errors="coerce").to_numpy("datetime64[s]").astype(np.int64))
            order.append(np.arange(sequence, sequence + n, dtype=np.int64))
            moved.append(np.full(n, is_move))
            sequence += n
    if not hashes:
        return KeySet()
    h, t, s, m = (np.concatenate(a) for a in (hashes, times, order, moved))
    sort = np.lexsort((s, t, h))  # by path, then time, then file order
    h, m = h[sort], m[sort]
    last = np.append(h[1:] != h[:-1], True)  # last event of each path
    return KeySet(h[last & m])


# --------------------------------------------------------------------------
# Inputs: duplicates_check.py's folder-mode CSVs
# --------------------------------------------------------------------------

def load_keepers(summary_csv: str | Path) -> dict[str, str]:
    """md5 -> keeper (oldest_path) from `<stem>_folder_summary.csv`. One entry per group."""
    keepers: dict[str, str] = {}
    with tqdm(total=count_rows(summary_csv), desc="Reading keepers from summary", unit="group") as progress:
        for chunk in iter_csv(summary_csv, ["md5", "oldest_path"]):
            keepers.update(zip(chunk["md5"], (os.path.abspath(p) for p in chunk["oldest_path"])))
            progress.update(len(chunk))
    return keepers


def iter_groups(duplicates_csv: str | Path) -> Iterator[pd.DataFrame]:
    """
    Blocks of `<stem>_folder_duplicates.csv` that always contain whole md5
    groups. duplicates_check.py writes that file sorted by md5, so a group's
    rows are contiguous; the rows of the last group in each block are held
    back and prepended to the next block. Stops with an error if the file
    turns out not to be grouped by md5.
    """
    finished: set[str] = set()
    carry: pd.DataFrame | None = None

    def _checked(block: pd.DataFrame) -> pd.DataFrame:
        md5s = block["md5"]
        groups = md5s.unique()
        if (md5s != md5s.shift()).sum() != len(groups) or not finished.isdisjoint(groups):
            raise SystemExit(
                f"ERROR: '{duplicates_csv}' is not grouped by md5 (duplicates_check.py writes it "
                "sorted by md5; was it re-sorted or edited?)."
            )
        finished.update(groups)
        return block

    for chunk in iter_csv(duplicates_csv, ["path", "size_bytes", "mtime", "md5"]):
        if carry is not None:
            chunk = pd.concat([carry, chunk], ignore_index=True)
        last = chunk["md5"].iat[-1]
        tail = (chunk["md5"] == last).to_numpy()
        carry = chunk[tail]
        if (~tail).any():
            yield _checked(chunk[~tail].reset_index(drop=True))
    if carry is not None and len(carry):
        yield _checked(carry.reset_index(drop=True))


# --------------------------------------------------------------------------
# Plan files
# --------------------------------------------------------------------------

def settings_path(plan_path: Path) -> Path:
    return plan_path.with_name(f"{plan_path.stem}_settings.json")


def load_settings(plan_path: Path) -> dict:
    if not plan_path.is_file():
        raise SystemExit(f"ERROR: plan file does not exist: {plan_path}")
    if not settings_path(plan_path).is_file():
        raise SystemExit(
            f"ERROR: the plan's settings file is missing: {settings_path(plan_path)} "
            "(it is written next to the plan by the 'plan' step)."
        )
    settings = json.loads(settings_path(plan_path).read_text(encoding="utf-8"))
    if settings.get("plan_format") != PLAN_FORMAT:
        raise SystemExit(
            f"ERROR: '{plan_path}' was made by an earlier version of this script. "
            "Re-run 'plan' to make a new one."
        )
    return settings


def iter_plan(plan_path: Path) -> Iterator[pd.DataFrame]:
    for chunk in iter_csv(plan_path):
        missing = [c for c in PLAN_COLUMNS if c not in chunk.columns]
        if missing:
            raise SystemExit(f"ERROR: plan '{plan_path}' is missing column(s): {missing}")
        chunk["action"] = chunk["action"].str.strip().str.upper()
        yield chunk


def validate_plan(plan_path: Path) -> int:
    """
    Check a (possibly hand-edited) plan before anything is moved, and return
    its number of MOVE rows. The only supported edit is changing `action`
    between MOVE and SKIP. Every row carries a `check` fingerprint of its
    paths, md5, keeper and scan size/mtime, so a changed path, keeper or
    value (a typo, an edited keeper) is caught; `role` says whether a row
    may be MOVE at all (not a keeper, not in a group that can't be moved).
    """
    errors: list[str] = []
    seen: list[np.ndarray] = []
    moves = 0
    with tqdm(total=count_rows(plan_path), desc="Validating plan", unit="row") as progress:
        for chunk in iter_plan(plan_path):
            for row in chunk.itertuples(index=False):
                if row.action not in ("MOVE", "SKIP", "KEEP"):
                    errors.append(f"unknown action '{row.action}' (allowed: MOVE, SKIP, KEEP): {row.original_path}")
                elif _row_check(getattr(row, c) for c in CHECKED_COLUMNS) != row.check:
                    errors.append(f"md5/paths/keeper/size/mtime differ from what 'plan' wrote: {row.original_path}")
                elif row.action == "MOVE" and row.role != "duplicate":
                    errors.append(f"marked MOVE but its role is '{row.role}' (keepers and blocked groups can't move): {row.original_path}")
                elif row.action == "KEEP" and row.role != "keeper":
                    errors.append(f"marked KEEP but is not its group's keeper: {row.original_path}")
                moves += row.action == "MOVE"
                if len(errors) > 1000:
                    break
            seen.append(hash_array(chunk["original_path"], len(chunk)))
            progress.update(len(chunk))
    if seen:
        all_seen = np.concatenate(seen)
        repeated = len(all_seen) - len(np.unique(all_seen))
        if repeated:
            errors.append(f"{repeated} original_path value(s) appear more than once")
    if errors:
        details = "\n".join(f"  - {e}" for e in errors[:20])
        more = f"\n  ... and {len(errors) - 20} more" if len(errors) > 20 else ""
        raise SystemExit(
            f"ERROR: the plan was changed beyond switching 'action' between MOVE and "
            f"SKIP ({len(errors)} problem(s)). Nothing was moved.\n{details}{more}"
        )
    return moves


def output_prefix(plan_path: Path, output: str | None) -> Path:
    """Base path for a plan's journal/report files: <output>/<plan stem>."""
    folder = Path(output) if output else plan_path.parent
    return folder / plan_path.stem


def check_output_folder(output: str | Path, duplicates_dir: str) -> None:
    if _is_under(str(output), duplicates_dir):
        raise SystemExit(
            f"ERROR: --output '{output}' is inside '{duplicates_dir}'; reports there "
            "would show up in their own verification as unexpected files."
        )


def _print_counts(title: str, counts: Counter, empty: str) -> None:
    print(f"\n=== {title} ===")
    print(" | ".join(f"{status} {count}" for status, count in counts.most_common()) or empty)


# --------------------------------------------------------------------------
# plan
# --------------------------------------------------------------------------

_SIZE_UNITS = {"B": 1, "KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3, "TB": 1024 ** 4}


def parse_size(text: str) -> int:
    """'500KB', '1.5MB', '2GB' or a plain number of bytes."""
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([KMGT]?B?)\s*", text.upper())
    if not match:
        raise argparse.ArgumentTypeError(f"invalid size '{text}' (examples: 0, 500KB, 1MB, 2GB)")
    unit = match.group(2) or "B"
    if not unit.endswith("B"):
        unit += "B"
    return int(float(match.group(1)) * _SIZE_UNITS[unit])


def build_exclude_matcher(patterns: list[str]) -> Callable[[str], str | None]:
    """
    Return a function giving the first pattern a path matches (or None).
    Patterns are case-insensitive wildcards matched against both the file
    name ('thumbs.db', '*.ini') and the full path ('*\\.git\\*').
    """
    normalized = [os.path.normcase(p) for p in patterns]

    def _match(path: str) -> str | None:
        full = os.path.normcase(path)
        name = os.path.basename(full)
        for original, pattern in zip(patterns, normalized):
            if fnmatch.fnmatchcase(name, pattern) or fnmatch.fnmatchcase(full, pattern):
                return original
        return None

    return _match


class PlanContext:
    def __init__(self, args: argparse.Namespace, root_abs: str, keepers: dict[str, str]) -> None:
        self.root_abs = root_abs
        self.duplicates_dir = os.path.join(root_abs, DUPLICATES_FOLDER_NAME)
        self.duplicates_prefix = os.path.normcase(self.duplicates_dir) + os.sep
        # No duplicates folder yet means no destination can exist, which
        # saves one check per file in pre-flight.
        self.duplicates_dir_exists = os.path.isdir(to_long_path(self.duplicates_dir))
        self.keepers = keepers
        self.matcher = build_exclude_matcher(DEFAULT_EXCLUDES + (args.exclude or []))
        self.min_size = args.min_size
        self.io_timeout = args.io_timeout
        self.workers = args.workers


def build_block(block: pd.DataFrame, ctx: PlanContext) -> pd.DataFrame:
    """
    Plan rows for one block of whole md5 groups: keeper, destination under
    <root>\\duplicates\\ (mirroring the path relative to root), role, and the
    filters (empty files, --min-size, exclude patterns). The numeric scan
    values are written as fixed strings so `check` matches them exactly
    when the plan is read back.
    """
    originals, destinations, keys = [], [], []
    for path in block["path"]:
        original = os.path.abspath(path)
        relative = relative_to_root(original, ctx.root_abs)
        if relative is None:
            raise SystemExit(
                f"ERROR: '{original}' in the duplicates list is not under --root "
                f"'{ctx.root_abs}'. Use the folder-mode output of duplicates_check.py for "
                "this same root (and the same drive letter or \\\\server\\share spelling). "
                "No plan was written."
            )
        originals.append(original)
        destinations.append(os.path.join(ctx.duplicates_dir, relative))
        keys.append(os.path.normcase(original))

    df = pd.DataFrame({
        "md5": block["md5"].to_numpy(),
        "original_path": originals,
        "duplicates_path": destinations,
        "size": block["size_bytes"].astype("int64").to_numpy(),
        "mtime_f": block["mtime"].astype("float64").to_numpy(),
    })
    keys = pd.Series(keys)
    del originals, destinations

    groups = df["md5"].unique()
    keeper_by_md5 = {m: ctx.keepers[m] for m in groups if m in ctx.keepers}
    keeper_key_by_md5 = {m: os.path.normcase(p) for m, p in keeper_by_md5.items()}
    df["keeper_path"] = df["md5"].map(keeper_by_md5).fillna("")
    is_keeper = (keys == df["md5"].map(keeper_key_by_md5)).to_numpy()

    keeper_rows = df[is_keeper].drop_duplicates("md5").set_index("md5")
    df["ksize"] = df["md5"].map(keeper_rows["size"])
    df["kmtime"] = df["md5"].map(keeper_rows["mtime_f"])

    in_duplicates_dir = keys.str.startswith(ctx.duplicates_prefix)
    group_size = df.groupby("md5")["md5"].transform("size")
    problem = pd.Series("", index=df.index, dtype=object)
    problem[~df["md5"].isin(keeper_rows.index)] = "KEEPER_NOT_IN_LIST"
    problem[df["keeper_path"] == ""] = "NO_KEEPER_IN_SUMMARY"
    problem[group_size < 2] = "NOT_A_DUPLICATE_GROUP"
    problem[df["md5"].isin(set(df.loc[in_duplicates_dir, "md5"]))] = "IN_DUPLICATES_FOLDER"
    problem = problem.to_numpy()

    n = len(df)
    role = np.where(is_keeper, "keeper", np.where(problem != "", "blocked", "duplicate")).astype(object)
    action = np.full(n, "MOVE", dtype=object)
    reason = np.full(n, "", dtype=object)
    detail = np.full(n, "", dtype=object)
    sizes = df["size"].to_numpy()
    for i in range(n):
        if role[i] == "keeper":
            action[i], reason[i] = "KEEP", "keeper"
        elif role[i] == "blocked":
            action[i], reason[i] = "SKIP", problem[i]
        elif sizes[i] == 0:
            action[i], reason[i] = "SKIP", "ZERO_BYTE"
        elif sizes[i] < ctx.min_size:
            action[i], reason[i], detail[i] = "SKIP", "BELOW_MIN_SIZE", f"min size {ctx.min_size} bytes"
        elif (pattern := ctx.matcher(df["original_path"].iat[i])) is not None:
            action[i], reason[i], detail[i] = "SKIP", "EXCLUDED_PATTERN", f"pattern {pattern}"
    df["action"], df["reason"], df["detail"], df["role"] = action, reason, detail, role

    # Keepers never move; SKIP rows keep their destination so they can be
    # switched to MOVE by hand.
    df.loc[is_keeper, "duplicates_path"] = ""
    df["long_path"] = df["duplicates_path"].str.len() > MAX_PATH_CHARS
    df["size_bytes"] = df["size"].astype(str)
    df["mtime"] = [repr(float(x)) for x in df["mtime_f"]]
    df["keeper_size_bytes"] = [str(int(x)) if x == x else "" for x in df["ksize"]]
    df["keeper_mtime"] = [repr(float(x)) if x == x else "" for x in df["kmtime"]]
    df["size_gb"] = df["size"] / (1024 ** 3)
    df["modified"] = pd.to_datetime(df["mtime_f"], unit="s").dt.strftime("%Y-%m-%d %H:%M:%S")
    df["check"] = [
        _row_check(values) for values in zip(*(df[c] for c in CHECKED_COLUMNS))
    ]
    # Keeper first in each group, as in the previous plan layout.
    df["_order"] = ~is_keeper
    return df.sort_values(["md5", "_order"], kind="stable").reset_index(drop=True)


def run_preflight(
    df: pd.DataFrame, ctx: PlanContext, executor: ThreadPoolExecutor, progress: tqdm
) -> None:
    """
    For every MOVE row, check (in place) that the keeper and the duplicate
    still exist with the size/mtime the scan recorded, and that nothing is
    in the way at the destination. Failures become SKIP with a reason.
    Sent in batches of PREFLIGHT_BATCH rows so the progress bar keeps moving.
    """
    move_rows = np.flatnonzero(df["action"].to_numpy() == "MOVE")
    progress.update(len(df) - len(move_rows))
    action = df["action"].to_numpy().copy()
    reason = df["reason"].to_numpy().copy()
    detail = df["detail"].to_numpy().copy()

    for start in range(0, len(move_rows), PREFLIGHT_BATCH):
        batch = move_rows[start:start + PREFLIGHT_BATCH]
        rows = df.iloc[batch]
        paths = list(dict.fromkeys(chain(
            rows["keeper_path"], rows["original_path"],
            rows["duplicates_path"] if ctx.duplicates_dir_exists else (),
        )))
        results = _bounded_map(executor, lambda p: probe(p, ctx.io_timeout), paths, ctx.workers * 64)
        probes = dict(zip(paths, results))
        for index, row in zip(batch, rows.itertuples(index=False)):
            problem = (
                check_against_scan(probes[row.keeper_path], row.ksize, row.kmtime, "KEEPER")
                or check_against_scan(probes[row.original_path], row.size, row.mtime_f, "DUPLICATE")
            )
            if problem is None and ctx.duplicates_dir_exists and probes[row.duplicates_path][0] != "missing":
                problem = ("DESTINATION_EXISTS", probes[row.duplicates_path][3])
            if problem is not None:
                action[index] = "SKIP"
                reason[index], detail[index] = problem
        progress.update(len(batch))
    df["action"], df["reason"], df["detail"] = action, reason, detail


def cmd_plan(args: argparse.Namespace) -> None:
    root_abs = os.path.abspath(args.root)
    if not os.path.isdir(to_long_path(root_abs)):
        raise SystemExit(f"ERROR: --root does not exist or is not a folder: {root_abs}")
    duplicates_dir = os.path.join(root_abs, DUPLICATES_FOLDER_NAME)
    check_output_folder(args.output, duplicates_dir)

    ctx = PlanContext(args, root_abs, load_keepers(args.summary_csv))
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    plan_path = Path(args.output) / f"move_plan_{stamp}.csv"
    partial_path = plan_path.with_name(plan_path.name + ".partial")
    plan_path.parent.mkdir(parents=True, exist_ok=True)

    groups = moves = long_paths = total_rows = 0
    move_bytes = 0
    skipped: Counter = Counter()
    if args.no_preflight:
        print("Pre-flight checks skipped (--no-preflight); execute still checks every file before moving it.")
    elif not ctx.duplicates_dir_exists:
        print(f"'{duplicates_dir}' doesn't exist yet, so destinations need no checking.")

    desc = "Building plan" if args.no_preflight else "Building plan + pre-flight checks"
    with open(partial_path, "w", newline="", encoding="utf-8") as handle, \
            ThreadPoolExecutor(max_workers=args.workers) as executor, \
            tqdm(total=count_rows(args.duplicates_csv), desc=desc, unit="file") as progress:
        for block in iter_groups(args.duplicates_csv):
            df = build_block(block, ctx)
            del block
            if args.no_preflight:
                progress.update(len(df))
            else:
                run_preflight(df, ctx, executor, progress)
            df.to_csv(handle, columns=PLAN_COLUMNS, header=total_rows == 0, index=False)

            is_move = df["action"] == "MOVE"
            groups += df["md5"].nunique()
            moves += int(is_move.sum())
            move_bytes += int(df.loc[is_move, "size"].sum())
            long_paths += int((is_move & df["long_path"]).sum())
            skipped.update(df.loc[df["action"] == "SKIP", "reason"])
            total_rows += len(df)
            progress.set_postfix(to_move=moves, refresh=False)
            del df
            gc.collect()
    partial_path.replace(plan_path)

    settings = {
        "plan_format": PLAN_FORMAT,
        "created_at": _now(),
        "root": root_abs,
        "duplicates_dir": duplicates_dir,
        "duplicates_csv": os.path.abspath(args.duplicates_csv),
        "summary_csv": os.path.abspath(args.summary_csv),
        "min_size_bytes": args.min_size,
        "exclude_patterns": DEFAULT_EXCLUDES + (args.exclude or []),
        "mtime_tolerance_seconds": MTIME_TOLERANCE_SECONDS,
        "preflight": not args.no_preflight,
        "rows": total_rows,
        "moves": moves,
    }
    settings_path(plan_path).write_text(json.dumps(settings, indent=2), encoding="utf-8")

    print("\n=== Move plan (dry run - nothing has been moved) ===")
    print(
        f"Groups: {groups} | Files to move: {moves} ({move_bytes / 1024 ** 3:.3f} GB) | "
        f"Long paths (> {MAX_PATH_CHARS} chars, moved and flagged): {long_paths}"
    )
    if skipped:
        print("Skipped: " + " | ".join(f"{reason} {count}" for reason, count in skipped.most_common()))
    print(f"Plan written to: {plan_path}")
    print(f"Settings written to: {settings_path(plan_path)}")
    print(
        "\nReview the plan (you may change 'action' from MOVE to SKIP for any row), then run:\n"
        f'  python quarantine_duplicates.py execute --plan "{plan_path}"'
    )


# --------------------------------------------------------------------------
# execute
# --------------------------------------------------------------------------

def cmd_execute(args: argparse.Namespace) -> None:
    plan_path = Path(args.plan)
    settings = load_settings(plan_path)
    prefix = output_prefix(plan_path, args.output)
    check_output_folder(prefix.parent, settings["duplicates_dir"])
    timeout = args.io_timeout

    total_moves = validate_plan(plan_path)
    moved = moved_now(prefix)
    if len(moved):
        print(f"Resuming: {len(moved)} file(s) already recorded as moved in {journal_path(prefix)}")

    _, error = _run_with_timeout(
        lambda: os.makedirs(to_long_path(settings["duplicates_dir"]), exist_ok=True),
        timeout, f"Timed out creating '{settings['duplicates_dir']}'",
    )
    if error is not None:
        raise SystemExit(f"ERROR: could not create '{settings['duplicates_dir']}': {error}")

    keeper_checks: dict[str, tuple[str, str] | None] = {}
    created_dirs: set[int] = set()
    counts: Counter = Counter()
    results_path = Path(f"{prefix}_execute_results.csv")

    with CsvAppender(journal_path(prefix), JOURNAL_COLUMNS, append=True, durable=True) as journal, \
            CsvAppender(results_path, EXECUTE_RESULT_COLUMNS, append=False) as results:

        def _record(row, status: str, detail: str = "") -> str:
            results.append([
                status, detail, row.md5, row.size_bytes, row.modified,
                row.original_path, row.duplicates_path, row.keeper_path, row.long_path,
            ])
            return status

        def _journal(row, status: str) -> None:
            journal.append([
                _now(), status, row.md5, row.size_bytes, row.mtime,
                row.original_path, row.duplicates_path, row.keeper_path,
            ])

        def _process(row) -> str:
            size, mtime = int(row.size_bytes), float(row.mtime)
            src = probe(row.original_path, timeout)
            dst = probe(row.duplicates_path, timeout)
            for state, _, _, error in (src, dst):
                if state == "error":
                    return _record(row, "ERROR", error)

            if src[0] == "missing" and dst[0] == "exists":
                # Moved earlier (possibly by a run killed before its bookkeeping).
                if not matches_scan(dst, size, mtime):
                    return _record(row, "CONFLICT", "original is gone and the file at the destination differs from the scan")
                note_error = note_add(row, timeout, check_existing=True)
                key = path_hash(row.original_path)
                if key in moved:
                    status = "ALREADY_MOVED"
                else:
                    _journal(row, "MOVED_RESUMED")
                    moved.add(key)
                    status = "MOVED_RESUMED"
                return _record(row, status, f"note not written: {note_error}" if note_error else "")
            if src[0] == "exists" and dst[0] == "exists":
                return _record(row, "CONFLICT", "both the original and a file at the destination exist; neither was touched")
            if src[0] == "missing":
                return _record(row, "MISSING", "neither the original nor the destination exists")

            if row.md5 not in keeper_checks:
                keeper_checks[row.md5] = check_against_scan(
                    probe(row.keeper_path, timeout), int(row.keeper_size_bytes), float(row.keeper_mtime), "KEEPER"
                )
            problem = keeper_checks[row.md5] or check_against_scan(src, size, mtime, "DUPLICATE")
            if problem is not None:
                return _record(row, f"SKIPPED_{problem[0]}", problem[1])

            parent = os.path.dirname(row.duplicates_path)
            parent_key = path_hash(parent)
            if parent_key not in created_dirs:
                _, error = _run_with_timeout(
                    lambda: os.makedirs(to_long_path(parent), exist_ok=True),
                    timeout, f"Timed out after {timeout:.0f}s creating '{parent}'",
                )
                if error is not None:
                    return _record(row, "MOVE_FAILED", f"could not create destination folder: {error}")
                created_dirs.add(parent_key)

            # os.rename, not shutil.move: within one volume it's the same
            # metadata-only operation, but it fails instead of silently
            # falling back to copy+delete across volumes, and never overwrites.
            _, error = _run_with_timeout(
                lambda: os.rename(to_long_path(row.original_path), to_long_path(row.duplicates_path)),
                timeout, f"Timed out after {timeout:.0f}s moving '{row.original_path}'",
            )
            if isinstance(error, TimeoutError):
                return _record(row, "MOVE_TIMEOUT", f"{error}; re-run execute to find out whether it completed")
            if error is not None:
                return _record(row, "MOVE_FAILED", f"{type(error).__name__}: {error}")
            # Order: rename -> note -> journal. A kill between them leaves a
            # state the resume branch above recognises and completes.
            note_error = note_add(row, timeout, check_existing=False)
            _journal(row, "MOVED")
            moved.add(path_hash(row.original_path))
            return _record(row, "MOVED", f"note not written: {note_error}" if note_error else "")

        interrupted = False
        executor = ThreadPoolExecutor(max_workers=args.workers)
        try:
            with tqdm(total=total_moves, desc="Moving duplicates", unit="file") as progress:
                for chunk in iter_plan(plan_path):
                    rows = list(chunk[chunk["action"] == "MOVE"].itertuples(index=False))
                    del chunk
                    for status in _bounded_map(executor, _process, rows, args.workers * 64):
                        counts[status] += 1
                        progress.update(1)
                    # Groups are contiguous in the plan; only the last one
                    # of a block can continue in the next.
                    keeper_checks.clear()
        except KeyboardInterrupt:
            interrupted = True
            print("\nStopping: letting moves already in progress finish ...")
        finally:
            executor.shutdown(wait=True, cancel_futures=True)

    _print_counts("Execute", counts, "Nothing to move.")
    print(f"Journal: {journal_path(prefix)}")
    print(f"Per-file results: {results_path}")
    if interrupted:
        raise SystemExit(
            "\nInterrupted. Re-run the same command to resume; finished moves are "
            "detected from the journal and the files on disk."
        )
    del moved, created_dirs
    gc.collect()
    if not args.no_verify:
        run_verify(plan_path, settings, prefix, args)


# --------------------------------------------------------------------------
# verify
# --------------------------------------------------------------------------

def run_verify(plan_path: Path, settings: dict, prefix: Path, args: argparse.Namespace) -> None:
    """
    Re-hash everything under <root>\\duplicates and compare it, by expected
    path and md5, with the moves recorded in the journal. Writes one report
    row per non-keeper plan row, plus EXTRA rows for unexpected files.
    Found files are indexed by path fingerprint (sorted numpy array), and
    the plan and report are streamed, so only the scan itself is held whole.
    """
    duplicates_dir = settings["duplicates_dir"]
    moved = moved_now(prefix)

    # Outcomes of MOVE rows that weren't moved, for the NOT_MOVED detail.
    # Only these are kept: the results file has a row for every move.
    not_moved: dict[int, str] = {}
    results_path = Path(f"{prefix}_execute_results.csv")
    if results_path.exists():
        for chunk in iter_csv(results_path, ["status", "detail", "original_path"]):
            chunk = chunk[~chunk["status"].isin(MOVED_STATUSES | {"ALREADY_MOVED"})]
            for status, detail, path in zip(chunk["status"], chunk["detail"], chunk["original_path"]):
                not_moved[path_hash(path)] = f"{status}: {detail}" if detail else status

    print(f"\nVerifying: re-hashing every file under '{duplicates_dir}' ...")
    found_paths = np.zeros(0, dtype=object)
    found_md5 = found_size = found_mtime = found_paths
    skipped_folders: list[dict[str, str]] = []
    if os.path.isdir(to_long_path(duplicates_dir)):
        scan, skipped_folders = scan_folder_md5(
            duplicates_dir, use_threading=args.threading, workers=args.workers, io_timeout=args.io_timeout,
        )
        found_paths = scan["path"].to_numpy(dtype=object)
        found_md5 = scan["md5"].to_numpy(dtype=object)
        found_size = scan["size_bytes"].to_numpy()
        found_mtime = scan["mtime"].to_numpy()
        del scan
        gc.collect()
    found_hashes = hash_array(found_paths, len(found_paths), desc="Indexing files found")
    order = np.argsort(found_hashes)
    sorted_hashes = found_hashes[order]
    del found_hashes
    matched = np.zeros(len(found_paths), dtype=bool)

    def _lookup(path: str) -> int | None:
        value = np.uint64(path_hash(path))
        index = int(np.searchsorted(sorted_hashes, value))
        if index < len(sorted_hashes) and sorted_hashes[index] == value:
            return int(order[index])
        return None

    def _found(position: int | None) -> list:
        if position is None:
            return ["", "", ""]
        return [found_size[position], _fmt_mtime(found_mtime[position]), found_md5[position] or ""]

    counts: Counter = Counter()
    report_path = Path(f"{prefix}_verify_report.csv")
    with CsvAppender(report_path, VERIFY_COLUMNS, append=False) as report:

        def _write(status, detail, row, position) -> None:
            size_found, mtime_found, md5_found = _found(position)
            report.append([
                status, detail, row.md5, md5_found, row.original_path, row.duplicates_path,
                row.keeper_path, row.size_bytes, size_found, row.modified, mtime_found, row.long_path,
            ])
            counts[status] += 1

        with tqdm(total=count_rows(plan_path), desc="Comparing with the plan", unit="row") as progress:
            for chunk in iter_plan(plan_path):
                for row in chunk.itertuples(index=False):
                    if row.action == "SKIP":
                        _write(f"SKIPPED_{row.reason}", row.detail, row, None)
                    elif row.action == "MOVE":
                        position = _lookup(row.duplicates_path)
                        if position is not None:
                            matched[position] = True
                        if path_hash(row.original_path) in moved:
                            if position is None:
                                _write("MISSING", "recorded as moved but not found in the duplicates folder", row, None)
                            elif not found_md5[position]:
                                _write("UNREADABLE", "found but could not be hashed", row, position)
                            elif found_md5[position] != row.md5:
                                _write("MD5_MISMATCH", "content differs from the scanned duplicate", row, position)
                            else:
                                _write("OK", "", row, position)
                        elif position is not None:
                            _write("MOVED_NOT_JOURNALED", "found but not in the journal; re-run execute to finish its bookkeeping", row, position)
                        else:
                            _write("NOT_MOVED", not_moved.get(path_hash(row.original_path), "NOT_EXECUTED"), row, None)
                progress.update(len(chunk))

        for position in np.flatnonzero(~matched):
            size_found, mtime_found, md5_found = _found(int(position))
            path = found_paths[position]
            report.append([
                "EXTRA", "in the duplicates folder but not an expected move of this plan", "", md5_found,
                "", path, "", "", size_found, "", mtime_found, len(path) > MAX_PATH_CHARS,
            ])
            counts["EXTRA"] += 1
        for skipped in skipped_folders:
            report.append(["UNSCANNED_FOLDER", skipped["error"], "", "", "", skipped["folder"], "", "", "", "", "", ""])
            counts["UNSCANNED_FOLDER"] += 1

    _print_counts("Verify", counts, "Nothing to verify.")
    problems = sum(counts[s] for s in ("MISSING", "MD5_MISMATCH", "UNREADABLE", "EXTRA", "MOVED_NOT_JOURNALED", "UNSCANNED_FOLDER"))
    if problems:
        print(f"{problems} row(s) need attention - see the report.")
    else:
        print("All recorded moves are present in the duplicates folder with the expected md5.")
    print(f"Verify report: {report_path}")


def cmd_verify(args: argparse.Namespace) -> None:
    plan_path = Path(args.plan)
    settings = load_settings(plan_path)
    prefix = output_prefix(plan_path, args.output)
    check_output_folder(prefix.parent, settings["duplicates_dir"])
    run_verify(plan_path, settings, prefix, args)


# --------------------------------------------------------------------------
# undo
# --------------------------------------------------------------------------

def cmd_undo(args: argparse.Namespace) -> None:
    """
    Move every journaled file back to where it came from and remove its
    line from original_file_location.txt (deleting the note once empty).
    Like execute, it decides from the files on disk, so it can be re-run
    after an interruption. Notes are rewritten once per folder per block.
    """
    plan_path = Path(args.plan)
    settings = load_settings(plan_path)
    prefix = output_prefix(plan_path, args.output)
    duplicates_dir = settings["duplicates_dir"]
    timeout = args.io_timeout
    if not journal_path(prefix).exists():
        print("Nothing to undo: the journal has no recorded moves.")
        return

    counts: Counter = Counter()
    emptied_dirs: set[str] = set()
    restored_parents: set[int] = set()
    results_path = Path(f"{prefix}_undo_results.csv")

    with CsvAppender(undo_journal_path(prefix), JOURNAL_COLUMNS, append=True, durable=True) as undo_journal, \
            CsvAppender(results_path, UNDO_RESULT_COLUMNS, append=False) as results:

        def _restore(row) -> tuple[str, str, object]:
            src = probe(row.original_path, timeout)
            dst = probe(row.duplicates_path, timeout)
            if "error" in (src[0], dst[0]):
                return "ERROR", src[3] or dst[3], row
            if dst[0] == "exists" and src[0] == "missing":
                if dst[1] != int(row.size_bytes):
                    return "CONFLICT", "file in the duplicates folder has a different size than the one moved", row
                parent = os.path.dirname(row.original_path)
                if path_hash(parent) not in restored_parents:
                    _run_with_timeout(
                        lambda: os.makedirs(to_long_path(parent), exist_ok=True),
                        timeout, "Timed out creating the original folder",
                    )
                    restored_parents.add(path_hash(parent))
                _, error = _run_with_timeout(
                    lambda: os.rename(to_long_path(row.duplicates_path), to_long_path(row.original_path)),
                    timeout, f"Timed out after {timeout:.0f}s restoring '{row.original_path}'",
                )
                if error is not None:
                    return "RESTORE_FAILED", f"{type(error).__name__}: {error}", row
                undo_journal.append([_now(), "RESTORED", *(getattr(row, c) for c in JOURNAL_COLUMNS[2:])])
                return "RESTORED", "", row
            if src[0] == "exists" and dst[0] == "missing":
                return "ALREADY_RESTORED", "", row
            if src[0] == "exists":
                return "CONFLICT", "both the original and the duplicates-folder copy exist; neither was touched", row
            return "MISSING", "neither the original nor the duplicates-folder copy exists", row

        def _clean_note(item: tuple[str, set[str]]) -> tuple[str, str]:
            folder, keys = item
            return folder, note_remove(folder, keys, timeout)

        def _record(status: str, detail: str, row) -> None:
            results.append([status, detail, *(getattr(row, c) for c in UNDO_RESULT_COLUMNS[2:])])
            counts[status] += 1

        interrupted = False
        executor = ThreadPoolExecutor(max_workers=args.workers)
        try:
            with tqdm(total=count_rows(journal_path(prefix)), desc="Restoring files", unit="file") as progress:
                for chunk in iter_journal(journal_path(prefix), MOVED_STATUSES):
                    done: list[tuple[str, str, object]] = []
                    for outcome in _bounded_map(executor, _restore, list(chunk.itertuples(index=False)), args.workers * 64):
                        done.append(outcome)
                        progress.update(1)
                    # One note rewrite per folder for this block.
                    notes: dict[str, set[str]] = {}
                    for status, _, row in done:
                        if status in ("RESTORED", "ALREADY_RESTORED"):
                            notes.setdefault(os.path.dirname(row.original_path), set()).add(_key(row.duplicates_path))
                            if status == "RESTORED":
                                emptied_dirs.add(os.path.dirname(row.duplicates_path))
                    note_errors = dict(_bounded_map(executor, _clean_note, list(notes.items()), args.workers * 64))
                    for status, detail, row in done:
                        error = note_errors.get(os.path.dirname(row.original_path), "")
                        if error and status in ("RESTORED", "ALREADY_RESTORED"):
                            detail = f"note not updated: {error}"
                        _record(status, detail, row)
        except KeyboardInterrupt:
            interrupted = True
            print("\nStopping: letting restores already in progress finish ...")
        finally:
            executor.shutdown(wait=True, cancel_futures=True)

    # Remove mirrored folders left empty, up to (not including) duplicates_dir.
    for folder in tqdm(sorted(emptied_dirs, key=len, reverse=True), desc="Removing empty folders", unit="folder"):
        while _is_under(folder, duplicates_dir) and _key(folder) != _key(duplicates_dir):
            try:
                os.rmdir(to_long_path(folder))
            except OSError:
                break
            folder = os.path.dirname(folder)

    _print_counts("Undo", counts, "Nothing restored.")
    print(f"Per-file results: {results_path}")
    if interrupted:
        raise SystemExit("\nInterrupted. Re-run the same command to continue.")


# --------------------------------------------------------------------------
# remaining
# --------------------------------------------------------------------------

def default_raw_csv(settings: dict) -> Path:
    """
    duplicates_check.py writes `<stem>_folder_duplicates.csv` and
    `<stem>_raw.csv` side by side (see its raw_export_path), so the raw
    listing can be found from the duplicates CSV the plan was built from.
    """
    duplicates_csv = Path(settings["duplicates_csv"])
    suffix = "_folder_duplicates"
    stem = duplicates_csv.stem[: -len(suffix)] if duplicates_csv.stem.endswith(suffix) else duplicates_csv.stem
    return duplicates_csv.with_name(f"{stem}_raw.csv")


def cmd_remaining(args: argparse.Namespace) -> None:
    """
    Derive the raw listing of what stays on the volume once <root>\\duplicates
    is deleted, without rehashing it: duplicates_check.py's raw listing,
    minus every file currently moved out (per the journal and undo
    journal), minus anything already under <root>\\duplicates, plus the
    original_file_location.txt notes (tiny, so they are hashed fresh).
    Same columns as the raw listing, so it works anywhere that one does
    (storage_report_by_year.R, duplicates_check.py --resume-from).
    The raw listing is streamed in blocks, so its size doesn't matter.
    """
    plan_path = Path(args.plan)
    settings = load_settings(plan_path)
    prefix = output_prefix(plan_path, args.output)
    check_output_folder(prefix.parent, settings["duplicates_dir"])
    raw_path = Path(args.raw_csv) if args.raw_csv else default_raw_csv(settings)
    if not raw_path.is_file():
        raise SystemExit(
            f"ERROR: raw listing not found: {raw_path}. Pass --raw-csv with the "
            "'<stem>_raw.csv' that duplicates_check.py wrote for this scan."
        )
    timeout = args.io_timeout

    print("Reading journals ...")
    moved = moved_now(prefix)
    note_folders: set[str] = set()
    for chunk in iter_journal(journal_path(prefix), MOVED_STATUSES):
        for path in chunk["original_path"]:
            if path_hash(path) in moved:
                note_folders.add(os.path.dirname(path))

    duplicates_prefix = _key(settings["duplicates_dir"]) + os.sep
    output_path = Path(f"{prefix}_remaining_raw.csv")
    partial_path = output_path.with_name(output_path.name + ".partial")
    raw_rows = removed_rows = kept_rows = 0
    removed_gb = kept_gb = 0.0
    first = True
    with open(partial_path, "w", newline="", encoding="utf-8") as handle, \
            tqdm(total=count_rows(raw_path), desc="Filtering raw listing", unit="file") as progress:
        for chunk in iter_csv(raw_path):
            missing_columns = [c for c in RAW_COLUMNS if c not in chunk.columns]
            if missing_columns:
                raise SystemExit(
                    f"ERROR: '{raw_path}' is missing column(s) {missing_columns}. Use duplicates_check.py's "
                    "final raw listing (the pre-hash one written early in a scan has no md5)."
                )
            keep = np.empty(len(chunk), dtype=bool)
            for i, path in enumerate(chunk["path"]):
                key = _key(path)
                gone = path_hash(path) in moved
                removed_rows += gone
                keep[i] = not gone and not key.startswith(duplicates_prefix) and os.path.basename(key) != NOTE_FILENAME
            sizes = pd.to_numeric(chunk["size_gb"], errors="coerce").fillna(0.0).to_numpy()
            removed_gb += float(sizes[~keep].sum())
            kept_gb += float(sizes[keep].sum())
            chunk[keep].to_csv(handle, columns=RAW_COLUMNS, header=first, index=False)
            first = False
            raw_rows += len(chunk)
            kept_rows += int(keep.sum())
            progress.update(len(chunk))

    # The notes didn't exist when the volume was scanned; any old note rows
    # were dropped above and are re-added fresh here.
    def _note_row(folder: str) -> list | None:
        note_path = os.path.join(folder, NOTE_FILENAME)
        state, size, mtime, _ = probe(note_path, timeout)
        if state != "exists":
            return None
        md5 = calculate_md5(note_path, timeout=timeout) or ""
        return [NOTE_FILENAME, note_path, _fmt_mtime(mtime), md5, size / (1024 ** 3)]

    notes_added = 0
    with open(partial_path, "a", newline="", encoding="utf-8") as handle, \
            ThreadPoolExecutor(max_workers=args.workers) as executor:
        writer = csv.writer(handle)
        if first:
            writer.writerow(RAW_COLUMNS)
        for row in tqdm(
            _bounded_map(executor, _note_row, sorted(note_folders), args.workers * 64),
            total=len(note_folders), desc="Hashing location notes", unit="file",
        ):
            if row is not None:
                writer.writerow(row)
                notes_added += 1
                kept_gb += row[4]
    partial_path.replace(output_path)

    print("\n=== Remaining files (as if the duplicates folder were deleted) ===")
    print(
        f"Raw listing: {raw_rows} file(s) | Removed: {removed_rows} file(s) ({removed_gb:.3f} GB) | "
        f"Notes added: {notes_added} | Remaining: {kept_rows + notes_added} file(s) ({kept_gb:.3f} GB)"
    )
    if removed_rows < len(moved):
        print(
            f"WARNING: {len(moved) - removed_rows} moved file(s) are not in the raw listing - "
            "check it is the raw export of the same scan the plan was built from."
        )
    print(f"Remaining listing: {output_path}")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Move duplicate files (from duplicates_check.py --folder) into a "
            f"'{DUPLICATES_FOLDER_NAME}' folder under the same root, keeping the "
            "oldest copy in place, then verify the move by md5."
        )
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def _common(p: argparse.ArgumentParser, workers_help: str) -> None:
        p.add_argument("--workers", type=int, default=8, help=f"{workers_help} Default: 8.")
        p.add_argument(
            "--io-timeout", type=float, default=DEFAULT_IO_TIMEOUT_SECONDS,
            help=f"Seconds to wait for one filesystem operation before giving up on it. Default: {DEFAULT_IO_TIMEOUT_SECONDS:.0f}.",
        )

    def _threading(p: argparse.ArgumentParser) -> None:
        p.add_argument("--threading", action="store_true", help="Hash files concurrently (with --workers threads) during verification.")

    plan = sub.add_parser("plan", help="Dry run: build and write the move plan. Moves nothing.")
    plan.add_argument("--duplicates-csv", required=True, help="The <stem>_folder_duplicates.csv from duplicates_check.py --folder.")
    plan.add_argument("--summary-csv", required=True, help="The <stem>_folder_summary.csv from the same run.")
    plan.add_argument("--root", required=True, help="The folder that was scanned (the --folder of duplicates_check.py).")
    plan.add_argument("--output", required=True, help="Folder to write the plan and, later, all reports into.")
    plan.add_argument(
        "--min-size", type=parse_size, default=0,
        help="Skip duplicates smaller than this (e.g. 500KB, 1MB, 2GB). Empty files are always skipped. Default: 0.",
    )
    plan.add_argument(
        "--exclude", nargs="+", metavar="PATTERN",
        help=f"Extra wildcard patterns never to move, matched against file name or full path (always excluded: {', '.join(DEFAULT_EXCLUDES)}).",
    )
    plan.add_argument(
        "--no-preflight", action="store_true",
        help="Don't check the files on the volume while planning (much faster on large volumes); execute still checks each file before moving it.",
    )
    _common(plan, "Threads for the pre-flight file checks.")

    execute = sub.add_parser("execute", help="Carry out a reviewed plan, then verify. Re-run the same command to resume.")
    execute.add_argument("--plan", required=True, help="The move_plan_<timestamp>.csv written by 'plan'.")
    execute.add_argument("--output", help="Folder for journal/reports. Default: the plan's folder.")
    execute.add_argument("--no-verify", action="store_true", help="Skip the md5 verification after moving.")
    _threading(execute)
    _common(execute, "Threads for moving files (and for hashing during verify with --threading).")

    verify = sub.add_parser("verify", help="Only re-run the md5 verification and report.")
    verify.add_argument("--plan", required=True, help="The move_plan_<timestamp>.csv that was executed.")
    verify.add_argument("--output", help="Folder with that plan's journal/reports. Default: the plan's folder.")
    _threading(verify)
    _common(verify, "Threads for hashing with --threading.")

    undo = sub.add_parser("undo", help="Move every journaled file back and clean up the notes.")
    undo.add_argument("--plan", required=True, help="The move_plan_<timestamp>.csv that was executed.")
    undo.add_argument("--output", help="Folder with that plan's journal. Default: the plan's folder.")
    _common(undo, "Threads for restoring files.")

    remaining = sub.add_parser(
        "remaining",
        help="Derive the raw listing of what stays once the duplicates folder is deleted, without rehashing.",
    )
    remaining.add_argument("--plan", required=True, help="The move_plan_<timestamp>.csv that was executed.")
    remaining.add_argument(
        "--raw-csv",
        help="duplicates_check.py's final '<stem>_raw.csv' for the same scan. Default: found next to the plan's --duplicates-csv.",
    )
    remaining.add_argument("--output", help="Folder with that plan's journal; the listing is written there too. Default: the plan's folder.")
    _common(remaining, "Threads for hashing the location notes.")

    args = parser.parse_args()
    if args.io_timeout <= 0:
        parser.error("--io-timeout must be a positive number.")
    if args.workers < 1:
        parser.error("--workers must be a positive integer.")
    return args


def main() -> None:
    args = parse_args()
    {
        "plan": cmd_plan, "execute": cmd_execute, "verify": cmd_verify,
        "undo": cmd_undo, "remaining": cmd_remaining,
    }[args.command](args)


if __name__ == "__main__":
    main()
