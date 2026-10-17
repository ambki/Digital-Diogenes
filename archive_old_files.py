from __future__ import annotations

import argparse
import csv
import gc
import json
import os
from collections import Counter
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
from tqdm import tqdm

# This script reuses the scanning helpers of duplicates_check.py and the
# move/journal helpers of quarantine_duplicates.py. Neither has to be run
# first, but both files must sit in the same folder as this one.
try:
    from duplicates_check import (
        DEFAULT_IO_TIMEOUT_SECONDS,
        _bounded_map,
        _list_directory_with_timeout,
        _run_with_timeout,
        build_raw_file_export,
        build_skipped_folders_sheet,
        calculate_md5,
        scan_folder_md5,
        to_long_path,
        write_csv,
    )
    from quarantine_duplicates import (
        DUPLICATES_FOLDER_NAME,
        MAX_PATH_CHARS,
        MTIME_TOLERANCE_SECONDS,
        RAW_COLUMNS,
        CsvAppender,
        KeySet,
        _fmt_mtime,
        _is_under,
        _key,
        _note_lock,
        _now,
        _print_counts,
        _read_note,
        _row_check,
        build_exclude_matcher,
        check_against_scan,
        count_rows,
        execute_outcomes,
        execute_results_path,
        hash_array,
        iter_csv,
        matches_scan,
        output_prefix,
        path_hash,
        probe,
        relative_to_root,
    )
    from quarantine_duplicates import NOTE_FILENAME as QUARANTINE_NOTE_FILENAME
except ModuleNotFoundError as exc:
    if exc.name not in ("duplicates_check", "quarantine_duplicates"):
        raise
    raise SystemExit(
        f"ERROR: {exc.name}.py was not found. archive_old_files.py uses its helpers, so "
        "duplicates_check.py and quarantine_duplicates.py must be in the same folder as "
        f"this script ({Path(__file__).resolve().parent})."
    ) from exc


ARCHIVE_FOLDER_NAME = "move_to_archive"
NOTE_FILENAME = "archived_files_location.txt"
NOTE_HEADER = (
    "# Files moved from active storage to the tape archive by archive_old_files.py. "
    "Each line: one file (or removed folder), where it was, and where it went "
    f"(archive label + path inside {ARCHIVE_FOLDER_NAME}).\n"
)
INDEX_FILENAME = "_archived_to_tape.csv"
MANIFEST_FILENAME = "archive_manifest.csv"
SYSTEM_FILENAMES = {"desktop.ini", "thumbs.db"}
NOTE_FILENAMES = {NOTE_FILENAME, QUARANTINE_NOTE_FILENAME.lower(), INDEX_FILENAME.lower()}
# What may be left in a folder that is removed: these move along with it.
FOLLOW_FILENAMES = SYSTEM_FILENAMES | {NOTE_FILENAME, QUARANTINE_NOTE_FILENAME.lower()}

CATEGORY_NAMES = ["", "unique", "keeper", "duplicate", "unreadable"]
CATEGORY_CODES = {name: code for code, name in enumerate(CATEGORY_NAMES) if name}
UNIQUE, KEEPER, DUPLICATE, UNREADABLE = 1, 2, 3, 4
CATEGORY_CHOICES = ["all", "unique", "keeper", "duplicate", "unreadable"]

CHUNK_ROWS = 20_000
PREFLIGHT_BATCH = 5_000
PLAN_FORMAT = 1

PLAN_COLUMNS = [
    "action", "reason", "detail", "kind", "role", "category", "copies", "md5",
    "size_bytes", "size_gb", "mtime", "modified", "original_path", "archive_path",
    "extra", "long_path", "check",
]
# As in quarantine_duplicates.py: everything but action/reason/detail is
# fingerprinted, since switching action between MOVE and SKIP is the only
# supported edit of a plan.
CHECKED_COLUMNS = [
    "kind", "role", "category", "copies", "md5", "original_path", "archive_path",
    "extra", "size_bytes", "mtime",
]
# Roles that may be MOVE: "candidate" (a file selected by date, or a SKIP that
# could be), "follow" (a note/system file that goes with its emptied folder),
# "folder" (remove the folder once empty), "keeper_note" (append a line to a
# quarantine note). "blocked" rows can never move.
MOVABLE_ROLES = {"candidate", "follow", "folder", "keeper_note"}
FILE_KINDS = {"file", "system", "note"}

JOURNAL_COLUMNS = [
    "timestamp", "status", "kind", "reason", "category", "copies", "md5",
    "size_bytes", "mtime", "original_path", "archive_path",
]
ON_STATUSES = {"MOVED", "MOVED_RESUMED", "REMOVED"}
OFF_STATUSES = {"NOT_MOVED", "RESTORED", "ALREADY_RESTORED", "FOLDER_RESTORED"}
RETRY_STATUSES = {"ERROR", "MOVE_FAILED", "MOVE_TIMEOUT"}
RETRY_RESULTS_SUFFIX = "__movedFailed"
EXECUTE_RESULT_COLUMNS = [
    "status", "detail", "kind", "md5", "size_bytes", "modified",
    "original_path", "archive_path", "long_path",
]
VERIFY_COLUMNS = [
    "status", "detail", "kind", "md5_expected", "md5_found", "original_path", "archive_path",
    "size_bytes", "size_found", "mtime_original", "mtime_found", "long_path",
]
VERIFY_PROBLEMS = {
    "MISSING", "MD5_MISMATCH", "SIZE_MISMATCH", "UNREADABLE", "EXTRA",
    "MOVED_NOT_JOURNALED", "UNSCANNED_FOLDER",
}
UNDO_RESULT_COLUMNS = [
    "status", "detail", "kind", "md5", "size_bytes", "mtime", "original_path", "archive_path",
]
MANIFEST_COLUMNS = [
    "original_path", "archive_path", "relative_path", "filename", "kind", "category",
    "copies", "reason", "size_bytes", "size_gb", "mtime", "modified", "md5",
    "source_root", "archive_label", "archived_on", "plan",
]
INDEX_COLUMNS = [
    "archived_on", "archive_label", "plan", "kind", "original_path", "archive_path",
    "md5", "size_bytes", "modified",
]


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def _git_root(key: str) -> str:
    """The outermost `.git` folder `key` (a normcased path) is inside, or ""."""
    marker = os.sep + ".git" + os.sep
    index = key.find(marker)
    return key[: index + len(marker) - 1] if index >= 0 else ""


def _kind_of(name: str) -> str:
    """'note', 'system' or 'file', from a lowercased file name."""
    if name in NOTE_FILENAMES:
        return "note"
    if name in SYSTEM_FILENAMES:
        return "system"
    return "file"


def _depth(path: str) -> int:
    return path.rstrip("\\/").count(os.sep)


def _parse_date(text: str) -> float:
    """'YYYY-MM-DD' (optionally with a time) as UTC epoch seconds."""
    try:
        stamp = pd.Timestamp(text)
    except (ValueError, TypeError) as exc:
        raise argparse.ArgumentTypeError(f"invalid date '{text}' (example: 2016-01-01)") from exc
    if stamp.tzinfo is not None:
        stamp = stamp.tz_convert("UTC").tz_localize(None)
    return (stamp - pd.Timestamp("1970-01-01")).total_seconds()


def _note_field(line: str, field: str) -> str | None:
    """Value of `field` in a ' | '-separated note line, or None."""
    for part in line.rstrip("\r\n").split(" | "):
        key, sep, value = part.partition(": ")
        if sep and key.strip() == field:
            return value
    return None


def _line_id(line: str) -> str:
    """Identity of one of this script's note lines: its first field + its plan."""
    parts = line.rstrip("\r\n").split(" | ")
    return f"{parts[0]}\x1f{parts[-1]}"


def _with_newline(line: str) -> str:
    return line if line.endswith("\n") else line + "\n"


def _dir_has_entries(path: str) -> bool:
    try:
        with os.scandir(to_long_path(path)) as entries:
            return next(entries, None) is not None
    except FileNotFoundError:
        return False


def journal_path(prefix: Path) -> Path:
    return Path(f"{prefix}_journal.csv")


def undo_journal_path(prefix: Path) -> Path:
    return Path(f"{prefix}_undo_journal.csv")


def remaining_path(prefix: Path) -> Path:
    return Path(f"{prefix}_remaining_raw.csv")


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
    if settings.get("script") != "archive_old_files" or settings.get("plan_format") != PLAN_FORMAT:
        raise SystemExit(
            f"ERROR: '{plan_path}' is not a plan made by this version of archive_old_files.py. "
            "Re-run 'plan' to make a new one."
        )
    return settings


def check_output_folder(output: str | Path, archive_dir: str) -> None:
    if _is_under(str(output), archive_dir):
        raise SystemExit(
            f"ERROR: --output '{output}' is inside '{archive_dir}'; reports there would "
            "go to tape and show up in their own verification."
        )


# --------------------------------------------------------------------------
# Notes (archived_files_location.txt)
# --------------------------------------------------------------------------
# Same approach as quarantine_duplicates.py's notes: one appended note per
# folder, writes to a folder's note serialized by a lock from a fixed pool.
# Every line this script writes ends in "| plan: <plan name>", which is how
# undo finds and removes exactly its own lines, also from quarantine notes.

def note_append(note_path: str, line: str, timeout: float, check_existing: bool, header: str | None = NOTE_HEADER) -> str:
    """Append `line` to a note (creating it with `header`). Returns "" or an error message."""

    def _add() -> None:
        if check_existing:
            target = _line_id(line)
            if any(_line_id(existing) == target for existing in _read_note(note_path)):
                return
        with open(to_long_path(note_path), "a", encoding="utf-8") as handle:
            if handle.tell() == 0 and header:
                handle.write(header)
            handle.write(line)

    with _note_lock(os.path.dirname(note_path)):
        _, error = _run_with_timeout(_add, timeout, f"Timed out after {timeout:.0f}s writing '{note_path}'")
    return "" if error is None else f"{type(error).__name__}: {error}"


def note_merge_into(src: str, dst: str, timeout: float) -> str:
    """
    Move the note `src` onto `dst`, which already exists: `dst` gets every
    line of `src` plus the lines only it has, then `src` is deleted. Used
    when a folder's note follows the folder to the archive side, where this
    run already wrote a note with the same lines; `src` may also hold lines
    of earlier runs, so it is the more complete one.
    """

    def _merge() -> None:
        src_lines = [_with_newline(line) for line in _read_note(src)]
        seen = set(src_lines)
        merged = src_lines + [line for line in map(_with_newline, _read_note(dst)) if line not in seen]
        partial = dst + ".partial"
        with open(to_long_path(partial), "w", encoding="utf-8") as handle:
            handle.writelines(merged)
        os.replace(to_long_path(partial), to_long_path(dst))
        os.remove(to_long_path(src))

    with _note_lock(os.path.dirname(dst)):
        _, error = _run_with_timeout(_merge, timeout, f"Timed out after {timeout:.0f}s merging '{src}'")
    return "" if error is None else f"{type(error).__name__}: {error}"


def note_strip(note_path: str, plan_name: str, timeout: float) -> str:
    """Remove this plan's lines from a note; delete the note if nothing but comments is left."""
    tag = f" | plan: {plan_name}"

    def _strip() -> None:
        lines = _read_note(note_path)
        kept = [line for line in lines if not line.rstrip("\r\n").endswith(tag)]
        if len(kept) == len(lines):
            return
        if all(line.startswith("#") or not line.strip() for line in kept):
            os.remove(to_long_path(note_path))
        else:
            with open(to_long_path(note_path), "w", encoding="utf-8") as handle:
                handle.writelines(kept)

    with _note_lock(os.path.dirname(note_path)):
        _, error = _run_with_timeout(_strip, timeout, f"Timed out after {timeout:.0f}s updating '{note_path}'")
    return "" if error is None else f"{type(error).__name__}: {error}"


# --------------------------------------------------------------------------
# Journal state
# --------------------------------------------------------------------------

def iter_journal(path: Path) -> Iterator[pd.DataFrame]:
    if not path.exists():
        return
    for chunk in iter_csv(path):
        if any(c not in chunk.columns for c in JOURNAL_COLUMNS):
            raise SystemExit(f"ERROR: '{path}' is not a journal written by archive_old_files.py.")
        yield chunk[chunk["original_path"] != ""]


def journal_state(prefix: Path, kinds: set[str]) -> KeySet:
    """
    Fingerprints of the paths (of `kinds`) that are currently moved out (or,
    for folders, removed), per the journal and undo journal: the latest event
    of each path decides. Same replay as quarantine_duplicates.moved_now.
    """
    hashes, times, order, on = [], [], [], []
    sequence = 0
    for path in (journal_path(prefix), undo_journal_path(prefix)):
        for chunk in iter_journal(path):
            chunk = chunk[chunk["kind"].isin(kinds) & chunk["status"].isin(ON_STATUSES | OFF_STATUSES)]
            n = len(chunk)
            if not n:
                continue
            hashes.append(hash_array(chunk["original_path"], n))
            times.append(pd.to_datetime(chunk["timestamp"], errors="coerce").to_numpy("datetime64[s]").astype(np.int64))
            order.append(np.arange(sequence, sequence + n, dtype=np.int64))
            on.append(chunk["status"].isin(ON_STATUSES).to_numpy())
            sequence += n
    if not hashes:
        return KeySet()
    h, t, s, m = (np.concatenate(a) for a in (hashes, times, order, on))
    sort = np.lexsort((s, t, h))
    h, m = h[sort], m[sort]
    last = np.append(h[1:] != h[:-1], True)
    return KeySet(h[last & m])


def iter_current(prefix: Path, kinds: set[str], state: KeySet) -> Iterator:
    """Journal rows (of `kinds`) for the paths in `state`, one row per path."""
    seen = KeySet()
    for chunk in iter_journal(journal_path(prefix)):
        chunk = chunk[chunk["kind"].isin(kinds) & chunk["status"].isin(ON_STATUSES)]
        for row in chunk.itertuples(index=False):
            key = path_hash(row.original_path)
            if key in state and key not in seen:
                seen.add(key)
                yield row


# --------------------------------------------------------------------------
# Plan
# --------------------------------------------------------------------------

class PlanContext:
    """The root, the listing and the selection rules, plus per-row categories."""

    def __init__(self, args: argparse.Namespace, root_abs: str, listing: Path) -> None:
        self.root_abs = root_abs
        self.root_key = os.path.normcase(root_abs)
        self.archive_dir = os.path.join(root_abs, ARCHIVE_FOLDER_NAME)
        self.duplicates_dir = os.path.join(root_abs, DUPLICATES_FOLDER_NAME)
        self.archive_prefix = os.path.normcase(self.archive_dir) + os.sep
        self.duplicates_prefix = os.path.normcase(self.duplicates_dir) + os.sep
        self.duplicates_dir_exists = os.path.isdir(to_long_path(self.duplicates_dir))
        self.listing = listing
        columns = set(pd.read_csv(listing, nrows=0).columns)
        missing = {"path", "mtime", "size_gb"} - columns
        if missing:
            raise SystemExit(f"ERROR: '{listing}' is missing column(s) {sorted(missing)}; it must be a raw listing.")
        self.has_md5 = "md5" in columns
        self.lo, self.hi = args.lo, args.hi
        self.all_categories = "all" in args.categories
        self.category_codes = {CATEGORY_CODES[c] for c in args.categories if c != "all"}
        if not self.all_categories and not self.has_md5:
            raise SystemExit(
                f"ERROR: '{listing}' has no md5 column, so only --categories all is possible "
                "(the pre-hash listing of an interrupted scan has none; use the final one)."
            )
        self.matcher = build_exclude_matcher(args.exclude) if args.exclude else None
        self.io_timeout = args.io_timeout
        self.workers = args.workers
        print("Counting rows in the listing ...")
        self.total_rows = count_rows(listing)
        self.category = np.zeros(self.total_rows + 1, dtype=np.uint8)
        self.copies = np.zeros(self.total_rows + 1, dtype=np.int32)

    def archive_path_for(self, original: str) -> str:
        return os.path.join(self.archive_dir, relative_to_root(original, self.root_abs) or "")

    def blocks(self, desc: str) -> Iterator[pd.DataFrame]:
        """The listing in normalized blocks, with a progress bar. Row numbers are stable across passes."""
        columns = ["path", "mtime", "size_gb"] + (["md5"] if self.has_md5 else [])
        start = 0
        with tqdm(total=self.total_rows, desc=desc, unit="file") as progress:
            for chunk in iter_csv(self.listing, columns, CHUNK_ROWS):
                progress.update(len(chunk))
                chunk = chunk[chunk["path"] != ""]
                if len(chunk):
                    yield self._normalize(chunk, start)
                    start += len(chunk)

    def _normalize(self, chunk: pd.DataFrame, start: int) -> pd.DataFrame:
        originals = [os.path.abspath(p) for p in chunk["path"]]
        keys = [os.path.normcase(p) for p in originals]
        rels = []
        for original in originals:
            rel = relative_to_root(original, self.root_abs)
            if rel is None:
                raise SystemExit(
                    f"ERROR: '{original}' in the listing is not under --root '{self.root_abs}'. "
                    "Use a listing of this same root (and the same drive letter or "
                    "\\\\server\\share spelling). No plan was written."
                )
            rels.append(rel)
        names = [os.path.basename(k).lower() for k in keys]
        stamps = pd.to_datetime(chunk["mtime"], format="%Y-%m-%d %H:%M:%S", errors="coerce")
        sizes = pd.to_numeric(chunk["size_gb"], errors="coerce").fillna(0.0).to_numpy() * (1024 ** 3)
        return pd.DataFrame({
            "row": np.arange(start, start + len(chunk), dtype=np.int64),
            "original": originals,
            "key": keys,
            "rel": rels,
            "name": names,
            "kind": [_kind_of(n) for n in names],
            "area": [
                "duplicates" if k.startswith(self.duplicates_prefix)
                else "archive" if k.startswith(self.archive_prefix) else ""
                for k in keys
            ],
            "folder": [os.path.dirname(p) for p in originals],
            "folder_key": [os.path.dirname(k) for k in keys],
            "mtime": (stamps - pd.Timestamp("1970-01-01")).dt.total_seconds().to_numpy(dtype=float),
            "size": np.rint(sizes).astype(np.int64),
            "md5": chunk["md5"].str.strip().str.lower().to_numpy() if self.has_md5 else np.full(len(chunk), ""),
        })

    def category_ok(self, code: int) -> bool:
        return self.all_categories or code in self.category_codes


def compute_categories(ctx: PlanContext) -> None:
    """
    Fill ctx.category/ctx.copies per listing row from the listing's own md5s:
    unique (md5 once), keeper (oldest copy; ties go to the first path
    alphabetically, as in duplicates_check.py), duplicate (the other copies),
    unreadable (no md5). Only data files outside duplicates/ and
    move_to_archive/ take part. md5s are kept as 8-byte prefixes, so 6
    million files need about 150 MB here.
    """
    if not ctx.has_md5:
        return
    rows_list, keys_list, mtimes_list = [], [], []
    for block in ctx.blocks("Grouping files by md5"):
        data = ((block["kind"] == "file") & (block["area"] == "")).to_numpy()
        valid = data & block["md5"].str.fullmatch(r"[0-9a-f]{32}").fillna(False).to_numpy()
        ctx.category[block["row"].to_numpy()[data & ~valid]] = UNREADABLE
        sub = block[valid]
        rows_list.append(sub["row"].to_numpy())
        keys_list.append(np.array([int(m[:16], 16) for m in sub["md5"]], dtype=np.uint64))
        mtimes = sub["mtime"].to_numpy(dtype=float)
        mtimes_list.append(np.where(np.isnan(mtimes) | (mtimes == 0), np.inf, mtimes))
    if not rows_list:
        return
    rows, keys, mtimes = (np.concatenate(a) for a in (rows_list, keys_list, mtimes_list))
    del rows_list, keys_list, mtimes_list
    if not len(rows):
        return
    _, group, counts = np.unique(keys, return_inverse=True, return_counts=True)
    size = counts[group]
    ctx.copies[rows] = size
    ctx.category[rows] = np.where(size == 1, UNIQUE, DUPLICATE)
    minimum = np.full(len(counts), np.inf)
    np.minimum.at(minimum, group, mtimes)
    tied = (size > 1) & (mtimes == minimum[group])
    ties = np.bincount(group[tied], minlength=len(counts))
    ctx.category[rows[tied & (ties[group] == 1)]] = KEEPER
    ambiguous = tied & (ties[group] > 1)
    if not ambiguous.any():
        return
    # Several copies share the oldest mtime: the first path alphabetically wins.
    group_of = dict(zip(rows[ambiguous].tolist(), group[ambiguous].tolist()))
    best: dict[int, tuple[str, int]] = {}
    row = 0
    for chunk in iter_csv(ctx.listing, ["path"], CHUNK_ROWS):
        for path in chunk["path"]:
            if path == "":
                continue
            g = group_of.get(row)
            if g is not None and (g not in best or path < best[g][0]):
                best[g] = (path, row)
            row += 1
    ctx.category[[r for _, r in best.values()]] = KEEPER


class Decisions:
    """What the plan does with each row of one block (see decide)."""

    def __init__(self, n: int) -> None:
        self.action = [""] * n
        self.reason = [""] * n
        self.detail = [""] * n
        self.role = [""] * n
        self.blocking = np.zeros(n, dtype=bool)
        self.follow = np.zeros(n, dtype=bool)
        self.git = [""] * n

    def set(self, i: int, action: str, reason: str, role: str, detail: str = "") -> None:
        self.action[i], self.reason[i], self.role[i], self.detail[i] = action, reason, role, detail


def decide(block: pd.DataFrame, ctx: PlanContext, partial_git: set[str] | None) -> Decisions:
    """
    Per row: the plan action (or "" for rows that don't go into the plan),
    whether it keeps its folder from being removed (`blocking`), and whether
    it would follow its folder if the folder is emptied (`follow`).
    `partial_git` is None in the first pass, when it isn't known yet.
    """
    d = Decisions(len(block))
    mtimes = block["mtime"].to_numpy(dtype=float)
    known = ~np.isnan(mtimes) & (mtimes != 0)
    in_range = known & (mtimes >= ctx.lo) & (mtimes < ctx.hi)
    categories = ctx.category[block["row"].to_numpy()]
    for i, (kind, area, key, original, name) in enumerate(zip(
        block["kind"], block["area"], block["key"], block["original"], block["name"]
    )):
        if area:
            continue
        if kind == "note":
            if name == QUARANTINE_NOTE_FILENAME.lower() and ctx.duplicates_dir_exists:
                d.blocking[i] = True
            else:
                d.follow[i] = True
            continue
        pattern = ctx.matcher(original) if ctx.matcher else None
        if kind == "system":
            if not in_range[i]:
                d.follow[i] = True
            elif pattern:
                d.set(i, "SKIP", "EXCLUDED_PATTERN", "candidate", f"pattern {pattern}")
                d.blocking[i] = True
            else:
                d.set(i, "MOVE", "DATE_RANGE", "candidate")
            continue
        d.git[i] = _git_root(key)
        d.blocking[i] = True
        if not known[i]:
            d.set(i, "SKIP", "MTIME_UNKNOWN", "blocked", "the file could not be stat'ed in the scan")
        elif not in_range[i]:
            pass
        elif pattern:
            d.set(i, "SKIP", "EXCLUDED_PATTERN", "candidate", f"pattern {pattern}")
        elif not ctx.category_ok(int(categories[i])):
            d.set(i, "SKIP", "CATEGORY_NOT_SELECTED", "candidate", CATEGORY_NAMES[categories[i]])
        elif partial_git is not None and d.git[i] in partial_git:
            d.set(i, "SKIP", "GIT_PARTIAL", "blocked", f"not every file of '{d.git[i]}' is selected")
        else:
            d.set(i, "MOVE", "DATE_RANGE", "candidate")
            d.blocking[i] = False
    return d


def _block_upwards(folders: dict[str, list], key: str, root_key: str) -> None:
    """Mark `key` and every ancestor as kept (not removable)."""
    while key in folders:
        folders[key][0] = True
        parent = os.path.dirname(key)
        if key == root_key or parent == key:
            break
        key = parent


def _finish_rows(df: pd.DataFrame) -> pd.DataFrame:
    for column in PLAN_COLUMNS:
        if column not in df.columns:
            df[column] = ""
    df["long_path"] = df["archive_path"].str.len() > MAX_PATH_CHARS
    df["check"] = [_row_check(values) for values in zip(*(df[c] for c in CHECKED_COLUMNS))]
    return df[PLAN_COLUMNS]


def _scan_fields(mtime: float) -> tuple[str, str]:
    """(mtime, modified) plan strings; empty if the mtime is unknown."""
    if mtime != mtime or mtime == 0:
        return "", ""
    return repr(float(mtime)), _fmt_mtime(mtime)


class PlanStats:
    def __init__(self) -> None:
        self.moves = 0
        self.move_bytes = 0
        self.long_paths = 0
        self.out_of_range = 0
        self.out_of_range_bytes = 0
        self.ignored = Counter()
        self.skipped = Counter()
        self.by_year: Counter = Counter()
        self.by_year_bytes: Counter = Counter()
        self.by_category: Counter = Counter()
        self.by_category_bytes: Counter = Counter()
        self.by_folder_bytes: Counter = Counter()
        self.by_folder: Counter = Counter()

    def add_moves(self, df: pd.DataFrame) -> None:
        if df.empty:
            return
        sizes = df["size_bytes"].astype(np.int64)
        self.moves += len(df)
        self.move_bytes += int(sizes.sum())
        self.long_paths += int(df["long_path"].sum())
        years = df["modified"].str[:4]
        categories = df["category"].replace("", "-")
        folders = df["top_folder"]
        for counter, bytes_counter, labels in (
            (self.by_year, self.by_year_bytes, years),
            (self.by_category, self.by_category_bytes, categories),
            (self.by_folder, self.by_folder_bytes, folders),
        ):
            grouped = sizes.groupby(labels.to_numpy())
            counter.update(grouped.size().to_dict())
            bytes_counter.update({k: int(v) for k, v in grouped.sum().items()})


def _top_folder(rel: str) -> str:
    parts = rel.split(os.sep)[:-1]
    return os.sep.join(parts[:2]) if parts else "(root)"


def run_preflight(df: pd.DataFrame, ctx: PlanContext, executor: ThreadPoolExecutor) -> None:
    """For every MOVE row, check (in place) that the file still has the listing's size/mtime."""
    moves = np.flatnonzero(df["action"].to_numpy() == "MOVE")
    action = df["action"].to_numpy().copy()
    reason = df["reason"].to_numpy().copy()
    detail = df["detail"].to_numpy().copy()
    for start in range(0, len(moves), PREFLIGHT_BATCH):
        batch = moves[start:start + PREFLIGHT_BATCH]
        paths = df["original_path"].to_numpy()[batch]
        results = list(_bounded_map(executor, lambda p: probe(p, ctx.io_timeout), paths, ctx.workers * 64))
        for index, result in zip(batch, results):
            problem = check_against_scan(
                result, int(df["size_bytes"].iat[index]), float(df["mtime"].iat[index]), "FILE"
            )
            if problem is not None:
                action[index] = "SKIP"
                reason[index], detail[index] = problem
    df["action"], df["reason"], df["detail"] = action, reason, detail


def scan_root(args: argparse.Namespace, root_abs: str, output: Path, stamp: str) -> Path:
    """Scan and hash --root (as duplicates_check.py --folder does) into a raw listing."""
    listing = output / f"archive_scan_{stamp}_raw.csv"
    excluded = [
        Path(p).resolve() for p in (
            os.path.join(root_abs, DUPLICATES_FOLDER_NAME), os.path.join(root_abs, ARCHIVE_FOLDER_NAME)
        ) if os.path.isdir(to_long_path(p))
    ]

    def _write_prehash(frame: pd.DataFrame) -> None:
        write_csv(frame, listing)
        print(f"Raw file listing (pre-hash) written to: {listing}")

    scan, skipped = scan_folder_md5(
        root_abs, use_threading=args.threading, workers=args.workers, io_timeout=args.io_timeout,
        excluded=excluded or None, on_stat_complete=_write_prehash,
    )
    print("Writing the raw file listing ...")
    write_csv(build_raw_file_export([scan]), listing)
    del scan
    gc.collect()
    skipped_path = output / f"archive_scan_{stamp}_skipped_folders.csv"
    write_csv(build_skipped_folders_sheet([("Folder", skipped)]), skipped_path)
    print(f"Raw file listing written to: {listing}")
    if skipped:
        print(f"WARNING: {len(skipped)} folder(s) could not be scanned; their files are not in the plan. See {skipped_path}")
    print(
        "To re-plan later (other dates/categories) without rescanning, pass this listing:\n"
        f'  --raw-csv "{listing}"'
    )
    return listing


def cmd_plan(args: argparse.Namespace) -> None:
    root_abs = os.path.abspath(args.root)
    if not os.path.isdir(to_long_path(root_abs)):
        raise SystemExit(f"ERROR: --root does not exist or is not a folder: {root_abs}")
    archive_dir = os.path.join(root_abs, ARCHIVE_FOLDER_NAME)
    check_output_folder(args.output, archive_dir)
    if _dir_has_entries(archive_dir):
        raise SystemExit(
            f"ERROR: '{archive_dir}' already exists and is not empty (an earlier batch?). "
            "Copy it to tape and delete it, or undo that plan, before planning a new batch."
        )
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    if args.raw_csv:
        listing = Path(args.raw_csv)
    else:
        print(f"No --raw-csv given: scanning and hashing '{root_abs}' first.")
        listing = scan_root(args, root_abs, output, stamp)

    ctx = PlanContext(args, root_abs, listing)
    compute_categories(ctx)

    # Pass 2: which folders end up empty, and which .git folders are only
    # partly selected. One entry per folder: [kept, display path, git root].
    folders: dict[str, list] = {}
    git_total: Counter = Counter()
    git_selected: Counter = Counter()
    for block in ctx.blocks("Finding folders to empty"):
        d = decide(block, ctx, partial_git=None)
        for i, (area, folder_key, folder) in enumerate(zip(block["area"], block["folder_key"], block["folder"])):
            if area:
                continue
            entry = folders.get(folder_key)
            if entry is None:
                entry = folders[folder_key] = [False, folder, ""]
            if d.blocking[i]:
                entry[0] = True
            if d.git[i]:
                entry[2] = d.git[i]
                git_total[d.git[i]] += 1
                git_selected[d.git[i]] += d.action[i] == "MOVE"
    partial_git = {g for g, total in git_total.items() if 0 < git_selected[g] < total}
    for entry in folders.values():
        if entry[2] in partial_git:
            entry[0] = True
    for key in list(folders):
        while key != ctx.root_key:
            parent = os.path.dirname(key)
            if parent == key or parent in folders:
                break
            folders[parent] = [False, os.path.dirname(folders[key][1]), ""]
            key = parent
    for key in sorted(folders, key=_depth, reverse=True):
        parent = os.path.dirname(key)
        if folders[key][0] and key != ctx.root_key and parent in folders:
            folders[parent][0] = True
    emptied_before = {k for k, e in folders.items() if not e[0] and k != ctx.root_key}

    # Pass 3: write the file rows (with pre-flight), collecting what the
    # follow/keeper-note/folder rows need.
    plan_path = output / f"archive_plan_{stamp}.csv"
    partial_path = plan_path.with_name(plan_path.name + ".partial")
    stats = PlanStats()
    follow_rows: list[dict] = []
    quarantine_notes: list[str] = []
    selected_hashes: list[np.ndarray] = []
    preflight_missing = 0
    total_rows = 0
    if args.no_preflight:
        print("Pre-flight checks skipped (--no-preflight); execute still checks every file before moving it.")
    with open(partial_path, "w", newline="", encoding="utf-8") as handle, \
            ThreadPoolExecutor(max_workers=args.workers) as executor:
        for block in ctx.blocks("Building plan" if args.no_preflight else "Building plan + pre-flight checks"):
            d = decide(block, ctx, partial_git)
            rows_np = block["row"].to_numpy()
            for i, (area, kind, name) in enumerate(zip(block["area"], block["kind"], block["name"])):
                if area:
                    stats.ignored[area] += 1
                elif kind == "file" and not d.action[i]:
                    stats.out_of_range += 1
                    stats.out_of_range_bytes += int(block["size"].iat[i])
                if not area and kind == "note" and name == QUARANTINE_NOTE_FILENAME.lower():
                    quarantine_notes.append(block["original"].iat[i])
                if d.follow[i] and block["folder_key"].iat[i] in emptied_before:
                    follow_rows.append({
                        "folder_key": block["folder_key"].iat[i], "kind": kind,
                        "category": CATEGORY_NAMES[ctx.category[rows_np[i]]] if kind != "note" else "",
                        "copies": str(ctx.copies[rows_np[i]] or "") if kind != "note" else "",
                        "md5": block["md5"].iat[i], "size": int(block["size"].iat[i]),
                        "mtime": float(block["mtime"].iat[i]), "original": block["original"].iat[i],
                        "rel": block["rel"].iat[i],
                    })
            in_plan = np.array([a != "" for a in d.action], dtype=bool)
            if not in_plan.any():
                continue
            sub = block[in_plan]
            sub_rows = rows_np[in_plan]
            scan_fields = [_scan_fields(m) for m in sub["mtime"]]
            df = pd.DataFrame({
                "action": np.array(d.action, dtype=object)[in_plan],
                "reason": np.array(d.reason, dtype=object)[in_plan],
                "detail": np.array(d.detail, dtype=object)[in_plan],
                "kind": sub["kind"].to_numpy(),
                "role": np.array(d.role, dtype=object)[in_plan],
                "category": [CATEGORY_NAMES[c] for c in ctx.category[sub_rows]],
                "copies": [str(c) if c else "" for c in ctx.copies[sub_rows]],
                "md5": sub["md5"].to_numpy(),
                "size_bytes": sub["size"].astype(str).to_numpy(),
                "size_gb": sub["size"].to_numpy() / (1024 ** 3),
                "mtime": [f[0] for f in scan_fields],
                "modified": [f[1] for f in scan_fields],
                "original_path": sub["original"].to_numpy(),
                "archive_path": [os.path.join(ctx.archive_dir, r) for r in sub["rel"]],
                "extra": "",
            })
            if not args.no_preflight:
                run_preflight(df, ctx, executor)
            df = _finish_rows(df)
            df.to_csv(handle, header=total_rows == 0, index=False)
            total_rows += len(df)

            is_move = (df["action"] == "MOVE").to_numpy()
            skipped = df[~is_move]
            stats.skipped.update(skipped["reason"])
            preflight_missing += int((skipped["reason"] == "FILE_MISSING").sum())
            for reason, folder_key in zip(skipped["reason"], sub["folder_key"].to_numpy()[~is_move]):
                if reason.startswith("FILE_"):
                    _block_upwards(folders, folder_key, ctx.root_key)
            moves = df[is_move].assign(top_folder=[_top_folder(r) for r in sub["rel"].to_numpy()[is_move]])
            stats.add_moves(moves)
            selected_hashes.append(hash_array(moves.loc[moves["kind"] == "file", "original_path"], int((moves["kind"] == "file").sum())))
            del df, moves, sub
        emptied = {k for k, e in folders.items() if not e[0] and k != ctx.root_key}

        # Notes and system files that follow their emptied folder.
        follow = [r for r in follow_rows if r["folder_key"] in emptied]
        if follow:
            scan_fields = [_scan_fields(r["mtime"]) for r in follow]
            df = _finish_rows(pd.DataFrame({
                "action": "MOVE", "reason": "FOLLOWS_FOLDER", "detail": "",
                "kind": [r["kind"] for r in follow], "role": "follow",
                "category": [r["category"] for r in follow], "copies": [r["copies"] for r in follow],
                "md5": [r["md5"] for r in follow], "size_bytes": [str(r["size"]) for r in follow],
                "size_gb": [r["size"] / (1024 ** 3) for r in follow],
                "mtime": [f[0] for f in scan_fields], "modified": [f[1] for f in scan_fields],
                "original_path": [r["original"] for r in follow],
                "archive_path": [os.path.join(ctx.archive_dir, r["rel"]) for r in follow],
                "extra": "",
            }))
            df.to_csv(handle, header=total_rows == 0, index=False)
            total_rows += len(df)

        # Quarantine notes that point at a keeper being archived.
        selected = KeySet(np.concatenate(selected_hashes) if selected_hashes else None)
        keeper_rows: list[tuple[str, str]] = []
        note_errors: list[str] = []

        def _kept_paths(note: str) -> tuple[str, list[str], str]:
            lines, error = _run_with_timeout(lambda: _read_note(note), args.io_timeout, f"Timed out reading '{note}'")
            if error is not None:
                return note, [], f"{type(error).__name__}: {error}"
            kept = {os.path.abspath(v): None for v in (_note_field(line, "original_kept_at") for line in lines) if v}
            return note, list(kept), ""

        if quarantine_notes and len(selected):
            for note, kept, error in tqdm(
                _bounded_map(executor, _kept_paths, quarantine_notes, args.workers * 64),
                total=len(quarantine_notes), desc="Reading quarantine notes", unit="note",
            ):
                if error:
                    note_errors.append(f"{note}: {error}")
                keeper_rows.extend((note, k) for k in kept if path_hash(k) in selected and relative_to_root(k, root_abs))
        if keeper_rows:
            df = _finish_rows(pd.DataFrame({
                "action": "MOVE", "reason": "KEEPER_ARCHIVED", "detail": "",
                "kind": "keeper_note", "role": "keeper_note",
                "original_path": [n for n, _ in keeper_rows],
                "archive_path": [ctx.archive_path_for(k) for _, k in keeper_rows],
                "extra": [k for _, k in keeper_rows],
                "size_gb": 0.0,
            }))
            df.to_csv(handle, header=total_rows == 0, index=False)
            total_rows += len(df)

        # Folders removed from the storage once empty, deepest first.
        folder_keys = sorted(emptied, key=_depth, reverse=True)
        if folder_keys:
            displays = [folders[k][1] for k in folder_keys]
            df = _finish_rows(pd.DataFrame({
                "action": "MOVE", "reason": "FOLDER_EMPTIED", "detail": "",
                "kind": "folder", "role": "folder",
                "original_path": displays,
                "archive_path": [ctx.archive_path_for(p) for p in displays],
                "size_gb": 0.0,
            }))
            df.to_csv(handle, header=total_rows == 0, index=False)
            total_rows += len(df)
        if total_rows == 0:
            pd.DataFrame(columns=PLAN_COLUMNS).to_csv(handle, index=False)
    partial_path.replace(plan_path)

    settings = {
        "script": "archive_old_files",
        "plan_format": PLAN_FORMAT,
        "created_at": _now(),
        "root": root_abs,
        "archive_dir": archive_dir,
        "duplicates_dir": ctx.duplicates_dir,
        "raw_csv": os.path.abspath(listing),
        "archive_label": args.archive_label,
        "before": args.before,
        "from": args.date_from,
        "to": args.date_to,
        "categories": args.categories,
        "exclude_patterns": args.exclude or [],
        "mtime_tolerance_seconds": MTIME_TOLERANCE_SECONDS,
        "preflight": not args.no_preflight,
        "rows": total_rows,
        "moves": stats.moves,
    }
    settings_path(plan_path).write_text(json.dumps(settings, indent=2), encoding="utf-8")
    print_plan_summary(args, stats, len(follow), len(keeper_rows), len(folder_keys), ctx)
    if note_errors:
        print(f"WARNING: {len(note_errors)} quarantine note(s) could not be read, e.g. {note_errors[0]}")
    if stats.moves and preflight_missing > max(10, stats.moves // 20):
        print(
            f"WARNING: {preflight_missing} file(s) of the listing no longer exist. Is '{listing}' the "
            "newest listing of this volume (e.g. the _remaining_raw.csv of a later quarantine or archive run)?"
        )
    print(f"Plan written to: {plan_path}")
    print(f"Settings written to: {settings_path(plan_path)}")
    print(
        "\nReview the plan (you may change 'action' between MOVE and SKIP), then run:\n"
        f'  python archive_old_files.py execute --plan "{plan_path}"'
    )


def _selection_text(args: argparse.Namespace) -> str:
    if args.before:
        return f"modified before {args.before} (UTC)"
    return f"modified from {args.date_from} to {args.date_to} (UTC, end excluded)"


def print_plan_summary(args, stats: PlanStats, follow: int, keeper_notes: int, folders: int, ctx: PlanContext) -> None:
    gb = 1024 ** 3
    print("\n=== Archive plan (dry run - nothing has been moved) ===")
    print(f"Selection: {_selection_text(args)} | categories: {', '.join(args.categories)} | label: {args.archive_label}")
    print(
        f"Files to move: {stats.moves} ({stats.move_bytes / gb:.3f} GB) | "
        f"Long paths (> {MAX_PATH_CHARS} chars, moved and flagged): {stats.long_paths}"
    )
    print(f"Outside the date range (stay): {stats.out_of_range} ({stats.out_of_range_bytes / gb:.3f} GB)")
    if stats.ignored:
        print("Ignored (already in duplicates/ or move_to_archive/): " + " | ".join(f"{k} {v}" for k, v in stats.ignored.items()))
    if stats.skipped:
        print("Skipped: " + " | ".join(f"{reason} {count}" for reason, count in stats.skipped.most_common()))
    print(
        f"Folders removed from the storage once emptied: {folders} | "
        f"notes/system files following their folder: {follow} | quarantine notes to update: {keeper_notes}"
    )
    if ctx.duplicates_dir_exists:
        print(
            f"Note: '{ctx.duplicates_dir}' exists (quarantine not finalized), so folders holding a "
            "quarantine note are kept."
        )

    def _table(title: str, counts: Counter, sizes: Counter, limit: int | None = None, by_size: bool = False) -> None:
        if not counts:
            return
        keys = sorted(counts, key=lambda k: -sizes[k]) if by_size else sorted(counts)
        print(f"\n{title}:")
        for key in keys[:limit]:
            print(f"  {key:<40} {counts[key]:>10} file(s) {sizes[key] / gb:>12.3f} GB")

    _table("Files to move by year modified", stats.by_year, stats.by_year_bytes)
    _table("Files to move by category", stats.by_category, stats.by_category_bytes)
    _table("Top folders by space to move", stats.by_folder, stats.by_folder_bytes, limit=15, by_size=True)
    print()


# --------------------------------------------------------------------------
# Plan validation (execute)
# --------------------------------------------------------------------------

def iter_plan(plan_path: Path) -> Iterator[pd.DataFrame]:
    for chunk in iter_csv(plan_path):
        missing = [c for c in PLAN_COLUMNS if c not in chunk.columns]
        if missing:
            raise SystemExit(f"ERROR: plan '{plan_path}' is missing column(s): {missing}")
        chunk["action"] = chunk["action"].str.strip().str.upper()
        yield chunk


def validate_plan(plan_path: Path) -> Counter:
    """Check a (possibly hand-edited) plan; return the number of MOVE rows per kind and per "role:<role>"."""
    errors: list[str] = []
    seen: list[np.ndarray] = []
    moves: Counter = Counter()
    with tqdm(total=count_rows(plan_path), desc="Validating plan", unit="row") as progress:
        for chunk in iter_plan(plan_path):
            for row in chunk.itertuples(index=False):
                if row.action not in ("MOVE", "SKIP"):
                    errors.append(f"unknown action '{row.action}' (allowed: MOVE, SKIP): {row.original_path}")
                elif _row_check(getattr(row, c) for c in CHECKED_COLUMNS) != row.check:
                    errors.append(f"paths/md5/size/mtime/role differ from what 'plan' wrote: {row.original_path}")
                elif row.action == "MOVE" and row.role not in MOVABLE_ROLES:
                    errors.append(f"marked MOVE but its role is '{row.role}' ({row.reason}): {row.original_path}")
                elif row.action == "MOVE":
                    moves[row.kind] += 1
                    moves[f"role:{row.role}"] += 1
                if len(errors) > 1000:
                    break
            files = chunk[chunk["kind"].isin(FILE_KINDS)]
            seen.append(hash_array(files["original_path"], len(files)))
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
            f"ERROR: the plan was changed beyond switching 'action' between MOVE and SKIP "
            f"({len(errors)} problem(s)). Nothing was moved.\n{details}{more}"
        )
    return moves


# --------------------------------------------------------------------------
# execute
# --------------------------------------------------------------------------

class Run:
    """State shared by the execute phases."""

    def __init__(self, plan_path: Path, settings: dict, prefix: Path, args: argparse.Namespace,
                 journal: CsvAppender, results: CsvAppender) -> None:
        self.plan_path = plan_path
        self.plan_name = plan_path.stem
        self.settings = settings
        self.root = settings["root"]
        self.archive_dir = settings["archive_dir"]
        self.label = settings["archive_label"]
        self.prefix = prefix
        self.timeout = args.io_timeout
        self.journal = journal
        self.results = results
        self.moved = journal_state(prefix, FILE_KINDS)
        self.removed = journal_state(prefix, {"folder"})
        self.created_dirs: set[int] = set()
        self.counts: Counter = Counter()

    def rel(self, path: str) -> str:
        return relative_to_root(path, self.root) or path

    def record(self, status: str, detail: str, kind: str, md5: str, size: str, modified: str,
               original: str, archive: str, long_path) -> str:
        self.results.append([status, detail, kind, md5, size, modified, original, archive, long_path])
        self.counts[status] += 1
        return status

    def record_row(self, row, status: str, detail: str = "") -> str:
        return self.record(status, detail, row.kind, row.md5, row.size_bytes, row.modified,
                           row.original_path, row.archive_path, row.long_path)

    def journal_row(self, status: str, kind: str, reason: str, category: str, copies: str, md5: str,
                    size: str, mtime: str, original: str, archive: str) -> None:
        self.journal.append([_now(), status, kind, reason, category, copies, md5, size, mtime, original, archive])

    def makedirs(self, folder: str) -> str:
        key = path_hash(folder)
        if key in self.created_dirs:
            return ""
        _, error = _run_with_timeout(
            lambda: os.makedirs(to_long_path(folder), exist_ok=True),
            self.timeout, f"Timed out after {self.timeout:.0f}s creating '{folder}'",
        )
        if error is not None:
            return f"could not create folder: {error}"
        self.created_dirs.add(key)
        return ""

    def file_line(self, row) -> str:
        return (
            f"moved_file: {os.path.basename(row.original_path)} | original_path: {row.original_path} | "
            f"archive_label: {self.label} | archive_path: {self.rel(row.archive_path)} | md5: {row.md5} | "
            f"size_bytes: {row.size_bytes} | modified: {row.modified} | archived_on: {_now()} | plan: {self.plan_name}\n"
        )

    def write_file_notes(self, row, check_existing: bool) -> str:
        """The same line in the source folder's note and in the archive folder's note."""
        line = self.file_line(row)
        errors = [
            note_append(os.path.join(os.path.dirname(path), NOTE_FILENAME), line, self.timeout, check_existing)
            for path in (row.original_path, row.archive_path)
        ]
        errors = [e for e in errors if e]
        return f"note not written: {'; '.join(errors)}" if errors else ""

    # Phase 1: files selected by date ------------------------------------

    def move_file(self, row) -> str:
        size, mtime = int(row.size_bytes), float(row.mtime)
        src = probe(row.original_path, self.timeout)
        dst = probe(row.archive_path, self.timeout)
        for state, _, _, error in (src, dst):
            if state == "error":
                return self.record_row(row, "ERROR", error)

        def _journal(status: str) -> None:
            self.journal_row(status, row.kind, row.reason, row.category, row.copies, row.md5,
                             row.size_bytes, row.mtime, row.original_path, row.archive_path)

        if src[0] == "missing" and dst[0] == "exists":
            # Moved earlier (possibly by a run killed before its bookkeeping).
            if not matches_scan(dst, size, mtime):
                return self.record_row(row, "CONFLICT", "original is gone and the file in the archive folder differs from the scan")
            note_error = self.write_file_notes(row, check_existing=True)
            key = path_hash(row.original_path)
            if key in self.moved:
                return self.record_row(row, "ALREADY_MOVED", note_error)
            _journal("MOVED_RESUMED")
            self.moved.add(key)
            return self.record_row(row, "MOVED_RESUMED", note_error)
        if src[0] == "exists" and dst[0] == "exists":
            return self.record_row(row, "CONFLICT", "both the original and a file in the archive folder exist; neither was touched")
        if src[0] == "missing":
            return self.record_row(row, "MISSING", "neither the original nor the archive copy exists")

        problem = check_against_scan(src, size, mtime, "FILE")
        if problem is not None:
            return self.record_row(row, f"SKIPPED_{problem[0]}", problem[1])
        error = self.makedirs(os.path.dirname(row.archive_path))
        if error:
            return self.record_row(row, "MOVE_FAILED", error)
        # os.rename, as in quarantine_duplicates.py: metadata-only on one
        # volume, never overwrites, never falls back to copy+delete.
        _, error = _run_with_timeout(
            lambda: os.rename(to_long_path(row.original_path), to_long_path(row.archive_path)),
            self.timeout, f"Timed out after {self.timeout:.0f}s moving '{row.original_path}'",
        )
        if isinstance(error, TimeoutError):
            return self.record_row(row, "MOVE_TIMEOUT", f"{error}; re-run execute to find out whether it completed")
        if error is not None:
            return self.record_row(row, "MOVE_FAILED", f"{type(error).__name__}: {error}")
        # rename -> notes -> journal, as in quarantine_duplicates.py.
        note_error = self.write_file_notes(row, check_existing=False)
        _journal("MOVED")
        self.moved.add(path_hash(row.original_path))
        return self.record_row(row, "MOVED", note_error)

    # Phase 2: quarantine notes pointing at an archived keeper ----------

    def update_keeper_note(self, row) -> str:
        if path_hash(row.extra) not in self.moved:
            return self.record_row(row, "KEEPER_NOT_MOVED", row.extra)
        note = row.original_path
        state = probe(note, self.timeout)
        if state[0] == "missing":
            # Already moved along with its folder by an earlier (resumed) run.
            note = os.path.join(self.archive_dir, self.rel(row.original_path))
            state = probe(note, self.timeout)
        if state[0] != "exists":
            return self.record_row(row, "NOTE_MISSING" if state[0] == "missing" else "ERROR", state[3])
        line = (
            f"keeper_archived: {row.extra} | archive_label: {self.label} | "
            f"archive_path: {self.rel(row.archive_path)} | archived_on: {_now()} | plan: {self.plan_name}\n"
        )
        error = note_append(note, line, self.timeout, check_existing=True, header=None)
        return self.record_row(row, "NOTE_FAILED" if error else "NOTE_UPDATED", error)

    # Phase 3: emptied folders ------------------------------------------

    def move_follower(self, src: str, dst: str, plan_row) -> str:
        """
        Move one note/system file along with its folder. Journaled *before*
        the move (and NOT_MOVED after a failure), unlike phase 1: such a file
        may have been created by this very run, so a kill right after the
        move would otherwise leave nothing that knows it was moved.
        Returns "" or an error.
        """
        state = probe(src, self.timeout)
        if state[0] == "missing":
            return ""
        if state[0] == "error":
            return state[3]
        name = os.path.basename(src).lower()
        kind = "note" if name in NOTE_FILENAMES else "system"
        md5 = plan_row.md5 if plan_row is not None else ""
        category = plan_row.category if plan_row is not None else ""
        copies = plan_row.copies if plan_row is not None else ""
        values = [kind, "FOLLOWS_FOLDER", category, copies, md5, str(state[1]), repr(float(state[2])), src, dst]
        self.journal_row("MOVED", *values)
        error = self.makedirs(os.path.dirname(dst))
        if not error:
            target = probe(dst, self.timeout)
            if target[0] == "error":
                error = target[3]
            elif target[0] == "exists" and name == NOTE_FILENAME:
                error = note_merge_into(src, dst, self.timeout)
            elif target[0] == "exists":
                error = "a file with this name is already in the archive folder"
            else:
                _, exc = _run_with_timeout(
                    lambda: os.rename(to_long_path(src), to_long_path(dst)),
                    self.timeout, f"Timed out after {self.timeout:.0f}s moving '{src}'",
                )
                error = "" if exc is None else f"{type(exc).__name__}: {exc}"
        if error:
            self.journal_row("NOT_MOVED", *values)
            self.record("MOVE_FAILED", error, kind, md5, str(state[1]), _fmt_mtime(state[2]), src, dst, len(dst) > MAX_PATH_CHARS)
            return error
        self.moved.add(path_hash(src))
        self.record("MOVED", "follows its folder", kind, md5, str(state[1]), _fmt_mtime(state[2]), src, dst, len(dst) > MAX_PATH_CHARS)
        return ""

    def removed_folder_line(self, row) -> str:
        """Record the removed folder in its parent's note (the nearest folder that still exists)."""
        line = (
            f"removed_folder: {row.original_path} | reason: all files archived | archive_label: {self.label} | "
            f"archive_path: {self.rel(row.archive_path)} | archived_on: {_now()} | plan: {self.plan_name}\n"
        )
        parent = os.path.dirname(row.original_path.rstrip("\\/"))
        return note_append(os.path.join(parent, NOTE_FILENAME), line, self.timeout, check_existing=True)

    def finalize_folder(self, row, followers: dict[str, object]) -> str:
        folder = row.original_path

        def _kept(detail: str) -> str:
            for follower in followers.values():
                self.record_row(follower, "FOLDER_KEPT", detail)
            return self.record_row(row, "FOLDER_KEPT", detail)

        dirnames, filenames, error = _list_directory_with_timeout(folder, self.timeout)
        if isinstance(error, FileNotFoundError):
            note_error = self.removed_folder_line(row)
            if path_hash(folder) not in self.removed:
                self.journal_row("REMOVED", "folder", row.reason, "", "", "", "", "", folder, row.archive_path)
                self.removed.add(path_hash(folder))
            return self.record_row(row, "ALREADY_REMOVED", f"note not written: {note_error}" if note_error else "")
        if error is not None:
            return self.record_row(row, "ERROR", f"{type(error).__name__}: {error}")
        if any(f.action != "MOVE" for f in followers.values()):
            return _kept("a file that follows the folder is set to SKIP in the plan")
        if dirnames:
            return _kept(f"still has {len(dirnames)} subfolder(s), e.g. '{dirnames[0]}'")
        others = [f for f in filenames if f.lower() not in FOLLOW_FILENAMES]
        if others:
            return _kept(f"still has {len(others)} file(s) that were not moved, e.g. '{others[0]}'")
        if os.path.isdir(to_long_path(self.settings["duplicates_dir"])) and any(
            f.lower() == QUARANTINE_NOTE_FILENAME.lower() for f in filenames
        ):
            return _kept(f"QUARANTINE_NOT_FINALIZED: '{self.settings['duplicates_dir']}' still exists")
        for filename in filenames:
            error = self.move_follower(
                os.path.join(folder, filename), os.path.join(row.archive_path, filename),
                followers.get(filename.lower()),
            )
            if error:
                return self.record_row(row, "FOLDER_KEPT", f"could not move '{filename}': {error}")
        _, error = _run_with_timeout(
            lambda: os.rmdir(to_long_path(folder)), self.timeout, f"Timed out removing '{folder}'",
        )
        if error is not None:
            return self.record_row(row, "FOLDER_KEPT", f"could not remove the folder: {type(error).__name__}: {error}")
        self.journal_row("REMOVED", "folder", row.reason, "", "", "", "", "", folder, row.archive_path)
        self.removed.add(path_hash(folder))
        note_error = self.removed_folder_line(row)
        return self.record_row(row, "REMOVED", f"note not written: {note_error}" if note_error else "")


def _plan_rows(plan_path: Path, keep) -> Iterator[list]:
    """Blocks of plan rows (as namedtuples) for which keep(chunk) is True."""
    for chunk in iter_plan(plan_path):
        chunk = chunk[keep(chunk)]
        if len(chunk):
            yield list(chunk.itertuples(index=False))


def cmd_execute(args: argparse.Namespace) -> None:
    plan_path = Path(args.plan)
    settings = load_settings(plan_path)
    prefix = output_prefix(plan_path, args.output)
    check_output_folder(prefix.parent, settings["archive_dir"])
    archive_dir = settings["archive_dir"]

    moves = validate_plan(plan_path)
    if not journal_path(prefix).exists() and _dir_has_entries(archive_dir):
        raise SystemExit(
            f"ERROR: '{archive_dir}' already holds files that this plan didn't move (an earlier "
            "batch?). Copy it to tape and delete it first."
        )
    retry_keys = None
    if args.retry_failed:
        if not execute_results_path(prefix).exists():
            raise SystemExit(f"ERROR: no '{execute_results_path(prefix)}' to retry from; run execute without --retry-failed first.")
        failed = execute_outcomes(prefix, lambda status: status in RETRY_STATUSES)
        if not failed:
            print(f"Nothing to retry: no {', '.join(sorted(RETRY_STATUSES))} in the latest execute results.")
            return
        retry_keys = np.fromiter(failed, dtype=np.uint64, count=len(failed))
        print(f"Retrying {len(failed)} file(s) that failed ({', '.join(sorted(RETRY_STATUSES))}) in the latest execute results")

    _, error = _run_with_timeout(
        lambda: os.makedirs(to_long_path(archive_dir), exist_ok=True),
        args.io_timeout, f"Timed out creating '{archive_dir}'",
    )
    if error is not None:
        raise SystemExit(f"ERROR: could not create '{archive_dir}': {error}")

    results_path = execute_results_path(prefix, args.retry_failed)
    interrupted = False
    with CsvAppender(journal_path(prefix), JOURNAL_COLUMNS, append=True, durable=True) as journal, \
            CsvAppender(results_path, EXECUTE_RESULT_COLUMNS, append=False) as results:
        run = Run(plan_path, settings, prefix, args, journal, results)
        if len(run.moved):
            print(f"Resuming: {len(run.moved)} file(s) already recorded as moved in {journal_path(prefix)}")
        executor = ThreadPoolExecutor(max_workers=args.workers)
        try:
            # Phase 1: the files selected by date.
            def _phase1(chunk: pd.DataFrame) -> pd.Series:
                keep = (chunk["action"] == "MOVE") & (chunk["role"] == "candidate")
                if retry_keys is not None:
                    keep &= np.isin(hash_array(chunk["original_path"], len(chunk)), retry_keys)
                return keep

            total = len(retry_keys) if retry_keys is not None else moves["role:candidate"]
            with tqdm(total=total, desc="Moving files to the archive folder", unit="file") as progress:
                for rows in _plan_rows(plan_path, _phase1):
                    for _ in _bounded_map(executor, run.move_file, rows, args.workers * 64):
                        progress.update(1)

            # Phase 2: quarantine notes of archived keepers.
            if moves["keeper_note"]:
                with tqdm(total=moves["keeper_note"], desc="Updating quarantine notes", unit="note") as progress:
                    for rows in _plan_rows(plan_path, lambda c: (c["action"] == "MOVE") & (c["kind"] == "keeper_note")):
                        for _ in _bounded_map(executor, run.update_keeper_note, rows, args.workers * 64):
                            progress.update(1)

            # Phase 3: emptied folders, deepest first; one depth level at a time.
            if moves["folder"]:
                followers: dict[str, dict[str, object]] = {}
                for rows in _plan_rows(plan_path, lambda c: c["role"] == "follow"):
                    for row in rows:
                        followers.setdefault(_key(os.path.dirname(row.original_path)), {})[
                            os.path.basename(row.original_path).lower()
                        ] = row
                levels: dict[int, list] = {}
                for rows in _plan_rows(plan_path, lambda c: (c["action"] == "MOVE") & (c["kind"] == "folder")):
                    for row in rows:
                        levels.setdefault(_depth(row.original_path), []).append(row)

                def _finalize(row) -> str:
                    return run.finalize_folder(row, followers.get(_key(row.original_path), {}))

                with tqdm(total=moves["folder"], desc="Removing emptied folders", unit="folder") as progress:
                    for depth in sorted(levels, reverse=True):
                        for _ in _bounded_map(executor, _finalize, levels[depth], args.workers * 64):
                            progress.update(1)
        except KeyboardInterrupt:
            interrupted = True
            print("\nStopping: letting moves already in progress finish ...")
        finally:
            executor.shutdown(wait=True, cancel_futures=True)
        counts = run.counts

    _print_counts("Execute", counts, "Nothing to move.")
    print(f"Journal: {journal_path(prefix)}")
    print(f"Per-file results: {results_path}")
    if interrupted:
        raise SystemExit(
            "\nInterrupted. Re-run the same command to resume; finished moves are detected "
            "from the journal and the files on disk."
        )
    write_manifest_and_index(plan_path, settings, prefix)
    gc.collect()
    if not args.no_verify:
        if retry_keys is not None:
            run_verify_retried(plan_path, settings, prefix, retry_keys, args)
        else:
            run_verify(plan_path, settings, prefix, args)
    if not args.no_remaining:
        write_remaining(plan_path, settings, prefix, args)


# --------------------------------------------------------------------------
# Manifest (goes to tape) and root index (stays on the storage)
# --------------------------------------------------------------------------

def write_manifest_and_index(plan_path: Path, settings: dict, prefix: Path) -> None:
    """
    Rewrite <archive>\\archive_manifest.csv (every file of this batch, one
    row each, with all its metadata) and this plan's rows in the cumulative
    <root>\\_archived_to_tape.csv, from the journal's current state. Both are
    rebuilt from scratch, so re-running execute or undo keeps them exact.
    """
    plan_name = plan_path.stem
    root, archive_dir, label = settings["root"], settings["archive_dir"], settings["archive_label"]
    moved = journal_state(prefix, FILE_KINDS)
    removed = journal_state(prefix, {"folder"})

    manifest = os.path.join(archive_dir, MANIFEST_FILENAME)
    index = os.path.join(root, INDEX_FILENAME)
    index_partial = index + ".partial"
    manifest_rows = index_rows = 0
    manifest_handle = None
    if len(moved) and os.path.isdir(to_long_path(archive_dir)):
        manifest_handle = open(to_long_path(manifest + ".partial"), "w", newline="", encoding="utf-8")
    try:
        manifest_writer = csv.writer(manifest_handle) if manifest_handle else None
        if manifest_writer:
            manifest_writer.writerow(MANIFEST_COLUMNS)
        with open(to_long_path(index_partial), "w", newline="", encoding="utf-8") as index_handle:
            index_writer = csv.writer(index_handle)
            index_writer.writerow(INDEX_COLUMNS)
            if os.path.exists(to_long_path(index)):
                for chunk in iter_csv(to_long_path(index)):
                    chunk = chunk[chunk["plan"] != plan_name]
                    chunk.reindex(columns=INDEX_COLUMNS, fill_value="").to_csv(index_handle, header=False, index=False)
                    index_rows += len(chunk)
            for row in iter_current(prefix, FILE_KINDS, moved):
                mtime = float(row.mtime) if row.mtime else 0.0
                modified = _fmt_mtime(mtime) if mtime else ""
                if manifest_writer:
                    size = int(row.size_bytes) if row.size_bytes else 0
                    manifest_writer.writerow([
                        row.original_path, row.archive_path,
                        relative_to_root(row.archive_path, archive_dir) or row.archive_path,
                        os.path.basename(row.original_path), row.kind, row.category, row.copies,
                        row.reason, row.size_bytes, size / (1024 ** 3), row.mtime, modified, row.md5,
                        root, label, row.timestamp, plan_name,
                    ])
                    manifest_rows += 1
                index_writer.writerow([row.timestamp, label, plan_name, row.kind, row.original_path,
                                       row.archive_path, row.md5, row.size_bytes, modified])
                index_rows += 1
            for row in iter_current(prefix, {"folder"}, removed):
                index_writer.writerow([row.timestamp, label, plan_name, "folder", row.original_path,
                                       row.archive_path, "", "", ""])
                index_rows += 1
    finally:
        if manifest_handle:
            manifest_handle.close()
    if manifest_handle:
        os.replace(to_long_path(manifest + ".partial"), to_long_path(manifest))
        print(f"Manifest ({manifest_rows} file(s)) written to: {manifest}")
    elif os.path.exists(to_long_path(manifest)):
        os.remove(to_long_path(manifest))
    if index_rows:
        os.replace(to_long_path(index_partial), to_long_path(index))
        print(f"Root index updated: {index}")
    else:
        os.remove(to_long_path(index_partial))
        if os.path.exists(to_long_path(index)):
            os.remove(to_long_path(index))


# --------------------------------------------------------------------------
# verify
# --------------------------------------------------------------------------

def run_verify(plan_path: Path, settings: dict, prefix: Path, args: argparse.Namespace) -> None:
    """
    Re-hash everything under <root>\\move_to_archive and compare it, by
    expected path and md5, with the moves recorded in the journal (as
    quarantine_duplicates.py's verify does). Notes are only checked for
    presence: their content grows by design. The manifest and the
    archive-side notes written by this script are not reported.
    """
    archive_dir = settings["archive_dir"]
    root = settings["root"]
    moved = journal_state(prefix, FILE_KINDS)
    removed = journal_state(prefix, {"folder"})
    outcomes = {
        key: f"{status}: {detail}" if detail else status
        for key, (status, detail) in execute_outcomes(
            prefix, lambda status: status not in {"MOVED", "MOVED_RESUMED", "ALREADY_MOVED"}
        ).items()
    }

    print(f"\nVerifying: re-hashing every file under '{archive_dir}' ...")
    found_paths = np.zeros(0, dtype=object)
    found_md5 = found_size = found_mtime = found_paths
    skipped_folders: list[dict[str, str]] = []
    if os.path.isdir(to_long_path(archive_dir)):
        scan, skipped_folders = scan_folder_md5(
            archive_dir, use_threading=args.threading, workers=args.workers, io_timeout=args.io_timeout,
        )
        manifest_key = _key(os.path.join(archive_dir, MANIFEST_FILENAME))
        scan = scan[scan["path"].map(_key) != manifest_key]
        found_paths = scan["path"].to_numpy(dtype=object)
        found_md5 = scan["md5"].to_numpy(dtype=object)
        found_size = scan["size_bytes"].to_numpy()
        found_mtime = scan["mtime"].to_numpy()
        del scan
        gc.collect()
    found_hashes = hash_array(found_paths, len(found_paths), desc="Indexing files found")
    order = np.argsort(found_hashes)
    sorted_hashes = found_hashes[order]
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

        def _write(status, detail, kind, md5, original, archive, size, modified, position, long_path) -> None:
            size_found, mtime_found, md5_found = _found(position)
            report.append([status, detail, kind, md5, md5_found, original, archive, size,
                           size_found, modified, mtime_found, long_path])
            counts[status] += 1

        with tqdm(total=count_rows(plan_path), desc="Comparing with the plan", unit="row") as progress:
            for chunk in iter_plan(plan_path):
                for row in chunk.itertuples(index=False):
                    if row.kind == "keeper_note":
                        continue

                    def _w(status: str, detail: str = "", position: int | None = None) -> None:
                        _write(status, detail, row.kind, row.md5, row.original_path, row.archive_path,
                               row.size_bytes, row.modified, position, row.long_path)

                    if row.kind == "folder":
                        if path_hash(row.original_path) in removed:
                            _w("FOLDER_REMOVED")
                        elif row.action == "SKIP":
                            _w(f"SKIPPED_{row.reason}", row.detail)
                        else:
                            _w("FOLDER_KEPT", outcomes.get(path_hash(row.original_path), "NOT_EXECUTED"))
                        continue
                    if row.action == "SKIP":
                        _w(f"SKIPPED_{row.reason}", row.detail)
                        continue
                    position = _lookup(row.archive_path)
                    if position is not None:
                        matched[position] = True
                    if path_hash(row.original_path) in moved:
                        if position is None:
                            _w("MISSING", "recorded as moved but not found in the archive folder")
                        elif row.kind == "note":
                            _w("OK", "note: present (its content grows, so it isn't compared)", position)
                        elif not found_md5[position]:
                            _w("UNREADABLE", "found but could not be hashed", position)
                        elif not row.md5:
                            ok = str(found_size[position]) == row.size_bytes
                            _w("OK" if ok else "SIZE_MISMATCH", "no md5 in the listing; size compared", position)
                        elif found_md5[position] != row.md5:
                            _w("MD5_MISMATCH", "content differs from the scanned file", position)
                        else:
                            _w("OK", "", position)
                    elif position is not None:
                        _w("MOVED_NOT_JOURNALED", "found but not in the journal; re-run execute to finish its bookkeeping", position)
                    else:
                        _w("NOT_MOVED", outcomes.get(path_hash(row.original_path), "NOT_EXECUTED"))
                progress.update(len(chunk))

        for position in np.flatnonzero(~matched):
            path = found_paths[position]
            if os.path.basename(path).lower() == NOTE_FILENAME:
                continue  # the archive-side notes written by this script
            original = os.path.join(root, relative_to_root(path, archive_dir) or "")
            if path_hash(original) in moved:
                status, detail = "OK", "moved along with its folder"
            else:
                status, detail = "EXTRA", "in the archive folder but not a move of this plan"
            _write(status, detail, "", "", original if status == "OK" else "", path, "", "",
                   int(position), len(path) > MAX_PATH_CHARS)
        for skipped in skipped_folders:
            report.append(["UNSCANNED_FOLDER", skipped["error"], "", "", "", "", skipped["folder"], "", "", "", "", ""])
            counts["UNSCANNED_FOLDER"] += 1

    _print_counts("Verify", counts, "Nothing to verify.")
    problems = sum(counts[s] for s in VERIFY_PROBLEMS)
    if problems:
        print(f"{problems} row(s) need attention - see the report.")
    else:
        print("All recorded moves are present in the archive folder with the expected md5.")
    print(f"Verify report: {report_path}")


def run_verify_retried(plan_path: Path, settings: dict, prefix: Path, retry_keys: np.ndarray, args: argparse.Namespace) -> None:
    """Verify only the rows an execute --retry-failed run retried, by hashing each destination."""
    moved = journal_state(prefix, FILE_KINDS)
    timeout = args.io_timeout

    def _check(row) -> list:
        dst = probe(row.archive_path, timeout)
        found = ["", "", ""] if dst[0] != "exists" else [dst[1], _fmt_mtime(dst[2]), ""]
        if path_hash(row.original_path) not in moved:
            if dst[0] == "exists":
                return ["MOVED_NOT_JOURNALED", "found but not in the journal; re-run execute", found]
            return ["NOT_MOVED", "", found]
        if dst[0] == "missing":
            return ["MISSING", "recorded as moved but not found in the archive folder", found]
        if dst[0] == "error":
            return ["UNREADABLE", dst[3], found]
        md5 = calculate_md5(row.archive_path, timeout=timeout)
        found[2] = md5 or ""
        if not md5:
            return ["UNREADABLE", "found but could not be hashed", found]
        if row.md5 and md5 != row.md5:
            return ["MD5_MISMATCH", "content differs from the scanned file", found]
        return ["OK", "", found]

    counts: Counter = Counter()
    report_path = Path(f"{prefix}_verify_report{RETRY_RESULTS_SUFFIX}.csv")
    print(f"\nVerifying the {len(retry_keys)} retried file(s) only ...")
    with CsvAppender(report_path, VERIFY_COLUMNS, append=False) as report, \
            ThreadPoolExecutor(max_workers=args.workers) as executor, \
            tqdm(total=len(retry_keys), desc="Verifying retried files", unit="file") as progress:
        for rows in _plan_rows(plan_path, lambda c: (c["action"] == "MOVE") & (c["role"] == "candidate")
                               & np.isin(hash_array(c["original_path"], len(c)), retry_keys)):
            for row, (status, detail, (size_found, mtime_found, md5_found)) in zip(
                rows, _bounded_map(executor, _check, rows, args.workers * 64)
            ):
                report.append([status, detail, row.kind, row.md5, md5_found, row.original_path, row.archive_path,
                               row.size_bytes, size_found, row.modified, mtime_found, row.long_path])
                counts[status] += 1
                progress.update(1)
    _print_counts("Verify (retried files)", counts, "Nothing to verify.")
    print(f"Verify report: {report_path}")


def cmd_verify(args: argparse.Namespace) -> None:
    plan_path = Path(args.plan)
    settings = load_settings(plan_path)
    prefix = output_prefix(plan_path, args.output)
    check_output_folder(prefix.parent, settings["archive_dir"])
    run_verify(plan_path, settings, prefix, args)


# --------------------------------------------------------------------------
# Remaining listing (written automatically by execute and undo)
# --------------------------------------------------------------------------

def _touched_notes(plan_path: Path, settings: dict, prefix: Path) -> set[str]:
    """Every note this plan may have written to or removed lines from."""
    notes: set[str] = {os.path.join(settings["root"], INDEX_FILENAME)}
    for chunk in iter_journal(journal_path(prefix)):
        for kind, original, archive in zip(chunk["kind"], chunk["original_path"], chunk["archive_path"]):
            if kind == "folder":
                notes.add(os.path.join(os.path.dirname(original.rstrip("\\/")), NOTE_FILENAME))
            else:
                notes.add(os.path.join(os.path.dirname(original), NOTE_FILENAME))
                notes.add(os.path.join(os.path.dirname(archive), NOTE_FILENAME))
    for rows in _plan_rows(plan_path, lambda c: c["kind"] == "keeper_note"):
        notes.update(row.original_path for row in rows)
    return notes


def write_remaining(plan_path: Path, settings: dict, prefix: Path, args: argparse.Namespace) -> None:
    """
    The raw listing of what stays on the storage, without rescanning: the
    plan's input listing, minus every file currently moved out, minus
    anything under move_to_archive, with the notes this run wrote or changed
    (and the root index) re-hashed fresh. Same columns as duplicates_check.py's
    raw listing, so it is the input for the next plan (archive or quarantine)
    and for the R reports.
    """
    listing = Path(settings["raw_csv"])
    if not listing.is_file():
        print(f"WARNING: the plan's listing '{listing}' is gone; remaining listing not written.")
        return
    moved = journal_state(prefix, FILE_KINDS)
    archive_prefix = _key(settings["archive_dir"]) + os.sep
    refresh = sorted(n for n in _touched_notes(plan_path, settings, prefix) if not _key(n).startswith(archive_prefix))
    refresh_keys = KeySet(hash_array(refresh, len(refresh)))

    output = remaining_path(prefix)
    partial = output.with_name(output.name + ".partial")
    kept_rows = removed_rows = 0
    first = True
    with open(partial, "w", newline="", encoding="utf-8") as handle:
        with tqdm(total=count_rows(listing), desc="Writing the remaining listing", unit="file") as progress:
            for chunk in iter_csv(listing):
                chunk = chunk.reindex(columns=RAW_COLUMNS, fill_value="")
                keep = np.empty(len(chunk), dtype=bool)
                for i, path in enumerate(chunk["path"]):
                    key = path_hash(path)
                    keep[i] = path != "" and key not in moved and key not in refresh_keys and not _key(path).startswith(archive_prefix)
                removed_rows += int((~keep).sum())
                chunk[keep].to_csv(handle, columns=RAW_COLUMNS, header=first, index=False)
                first = False
                kept_rows += int(keep.sum())
                progress.update(len(chunk))

        def _note_row(path: str) -> list | None:
            state, size, mtime, _ = probe(path, args.io_timeout)
            if state != "exists":
                return None
            md5 = calculate_md5(path, timeout=args.io_timeout) or ""
            return [os.path.basename(path), path, _fmt_mtime(mtime), md5, size / (1024 ** 3)]

        writer = csv.writer(handle)
        if first:
            writer.writerow(RAW_COLUMNS)
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            for row in tqdm(_bounded_map(executor, _note_row, refresh, args.workers * 64),
                            total=len(refresh), desc="Hashing notes", unit="file"):
                if row is not None:
                    writer.writerow(row)
                    kept_rows += 1
    partial.replace(output)
    print(f"Remaining listing ({kept_rows} file(s) on the storage) written to: {output}")


# --------------------------------------------------------------------------
# undo
# --------------------------------------------------------------------------

def cmd_undo(args: argparse.Namespace) -> None:
    """
    Move every journaled file back, recreate the removed folders, remove
    every line this plan added to notes (on both sides and in quarantine
    notes), and clean up move_to_archive. Decides from the files on disk, so
    it can be re-run after an interruption.
    """
    plan_path = Path(args.plan)
    settings = load_settings(plan_path)
    prefix = output_prefix(plan_path, args.output)
    archive_dir = settings["archive_dir"]
    timeout = args.io_timeout
    if not journal_path(prefix).exists():
        print("Nothing to undo: the journal has no recorded moves.")
        return

    moved = journal_state(prefix, FILE_KINDS)
    removed = journal_state(prefix, {"folder"})
    counts: Counter = Counter()
    archive_folders: set[str] = set()
    results_path = Path(f"{prefix}_undo_results.csv")
    interrupted = False

    with CsvAppender(undo_journal_path(prefix), JOURNAL_COLUMNS, append=True, durable=True) as undo_journal, \
            CsvAppender(results_path, UNDO_RESULT_COLUMNS, append=False) as results:

        def _journal(status: str, row) -> None:
            undo_journal.append([_now(), status, *(getattr(row, c) for c in JOURNAL_COLUMNS[2:])])

        def _record(status: str, detail: str, row) -> str:
            results.append([status, detail, row.kind, row.md5, row.size_bytes, row.mtime, row.original_path, row.archive_path])
            counts[status] += 1
            return status

        def _restore(row) -> str:
            src = probe(row.original_path, timeout)
            dst = probe(row.archive_path, timeout)
            if "error" in (src[0], dst[0]):
                return _record("ERROR", src[3] or dst[3], row)
            if dst[0] == "exists" and src[0] == "missing":
                if row.kind != "note" and row.size_bytes and dst[1] != int(row.size_bytes):
                    return _record("CONFLICT", "the file in the archive folder has a different size than the one moved", row)
                _run_with_timeout(
                    lambda: os.makedirs(to_long_path(os.path.dirname(row.original_path)), exist_ok=True),
                    timeout, "Timed out creating the original folder",
                )
                _, error = _run_with_timeout(
                    lambda: os.rename(to_long_path(row.archive_path), to_long_path(row.original_path)),
                    timeout, f"Timed out after {timeout:.0f}s restoring '{row.original_path}'",
                )
                if error is not None:
                    return _record("RESTORE_FAILED", f"{type(error).__name__}: {error}", row)
                _journal("RESTORED", row)
                return _record("RESTORED", "", row)
            if src[0] == "exists" and dst[0] == "missing":
                _journal("ALREADY_RESTORED", row)
                return _record("ALREADY_RESTORED", "", row)
            if src[0] == "exists":
                return _record("CONFLICT", "both the original and the archive copy exist; neither was touched", row)
            return _record("MISSING", "neither the original nor the archive copy exists", row)

        executor = ThreadPoolExecutor(max_workers=args.workers)
        try:
            rows = list(iter_current(prefix, FILE_KINDS, moved))
            archive_folders.update(os.path.dirname(row.archive_path) for row in rows)
            with tqdm(total=len(rows), desc="Restoring files", unit="file") as progress:
                for _ in _bounded_map(executor, _restore, rows, args.workers * 64):
                    progress.update(1)
            del rows
            for row in iter_current(prefix, {"folder"}, removed):
                archive_folders.add(row.archive_path)
                if not os.path.isdir(to_long_path(row.original_path)):
                    _run_with_timeout(
                        lambda: os.makedirs(to_long_path(row.original_path), exist_ok=True),
                        timeout, "Timed out recreating the folder",
                    )
                _journal("FOLDER_RESTORED", row)
                _record("FOLDER_RESTORED", "", row)

            notes = sorted(_touched_notes(plan_path, settings, prefix) - {os.path.join(settings["root"], INDEX_FILENAME)})
            note_errors = [
                f"{note}: {error}" for note, error in tqdm(
                    zip(notes, _bounded_map(executor, lambda n: note_strip(n, plan_path.stem, timeout), notes, args.workers * 64)),
                    total=len(notes), desc="Removing this plan's note lines", unit="note",
                ) if error
            ]
        except KeyboardInterrupt:
            interrupted = True
            note_errors = []
            print("\nStopping: letting restores already in progress finish ...")
        finally:
            executor.shutdown(wait=True, cancel_futures=True)

    if interrupted:
        raise SystemExit("\nInterrupted. Re-run the same command to continue.")
    write_manifest_and_index(plan_path, settings, prefix)
    # Remove archive folders left empty, up to and including move_to_archive.
    for folder in tqdm(sorted(archive_folders, key=len, reverse=True), desc="Removing empty archive folders", unit="folder"):
        while _is_under(folder, archive_dir):
            try:
                os.rmdir(to_long_path(folder))
            except OSError:
                break
            if _key(folder) == _key(archive_dir):
                break
            folder = os.path.dirname(folder)
    try:
        os.rmdir(to_long_path(archive_dir))
    except OSError:
        pass

    _print_counts("Undo", counts, "Nothing restored.")
    if note_errors:
        print(f"WARNING: {len(note_errors)} note(s) could not be updated, e.g. {note_errors[0]}")
    print(f"Per-file results: {results_path}")
    if not args.no_remaining:
        write_remaining(plan_path, settings, prefix, args)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Move files modified in a date range into a 'move_to_archive' folder under the same "
            "root (same folder structure), ready to be copied to tape by hand, then verify by md5."
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
        p.add_argument("--threading", action="store_true", help="Hash files concurrently (with --workers threads).")

    def _remaining(p: argparse.ArgumentParser) -> None:
        p.add_argument("--no-remaining", action="store_true", help="Don't write <plan>_remaining_raw.csv afterwards.")

    plan = sub.add_parser("plan", help="Dry run: (scan,) select by date and write the plan. Moves nothing.")
    plan.add_argument("--root", required=True, help="The storage folder to archive from. move_to_archive is created directly under it.")
    plan.add_argument("--output", required=True, help="Folder to write the plan and, later, all reports into.")
    plan.add_argument(
        "--raw-csv",
        help="The newest raw listing of --root (duplicates_check.py's <stem>_raw.csv, or a _remaining_raw.csv "
             "of quarantine_duplicates.py or of this script). Default: scan and hash --root first.",
    )
    dates = plan.add_argument_group("date range (on each file's modification time, UTC)")
    dates.add_argument("--before", metavar="DATE", help="Select files modified before DATE (e.g. 2016-01-01).")
    dates.add_argument("--from", dest="date_from", metavar="DATE", help="Select files modified on/after DATE. Requires --to.")
    dates.add_argument("--to", dest="date_to", metavar="DATE", help="... and before DATE. Requires --from.")
    plan.add_argument(
        "--categories", required=True, nargs="+", choices=CATEGORY_CHOICES,
        help="Which files in the date range to move: all, or any of unique (md5 occurs once), keeper (oldest "
             "copy of a duplicate group), duplicate (the other copies), unreadable (no md5).",
    )
    plan.add_argument(
        "--archive-label", required=True,
        help="Name of the tape/batch these files go to (e.g. LTO-0042). Written in every note and the manifest.",
    )
    plan.add_argument(
        "--exclude", nargs="+", metavar="PATTERN",
        help="Wildcard patterns never to move, matched case-insensitively against file name or full path.",
    )
    plan.add_argument(
        "--no-preflight", action="store_true",
        help="Don't check the files on the volume while planning (faster); execute still checks each file.",
    )
    _threading(plan)
    _common(plan, "Threads for the pre-flight checks, reading quarantine notes and (with --threading) hashing the scan.")

    execute = sub.add_parser("execute", help="Carry out a reviewed plan, then verify. Re-run the same command to resume.")
    execute.add_argument("--plan", required=True, help="The archive_plan_<timestamp>.csv written by 'plan'.")
    execute.add_argument("--output", help="Folder for journal/reports. Default: the plan's folder.")
    execute.add_argument("--no-verify", action="store_true", help="Skip the md5 verification after moving.")
    execute.add_argument(
        "--retry-failed", action="store_true",
        help=f"Only retry the files whose latest execute result is {', '.join(sorted(RETRY_STATUSES))}; "
             f"results go to <plan>_execute_results{RETRY_RESULTS_SUFFIX}.csv. Notes and emptied folders are "
             "always processed again.",
    )
    _remaining(execute)
    _threading(execute)
    _common(execute, "Threads for moving files (and for hashing during verify with --threading).")

    verify = sub.add_parser("verify", help="Only re-run the md5 verification and report.")
    verify.add_argument("--plan", required=True, help="The archive_plan_<timestamp>.csv that was executed.")
    verify.add_argument("--output", help="Folder with that plan's journal/reports. Default: the plan's folder.")
    _threading(verify)
    _common(verify, "Threads for hashing with --threading.")

    undo = sub.add_parser("undo", help="Move everything back, recreate removed folders and remove this plan's note lines.")
    undo.add_argument("--plan", required=True, help="The archive_plan_<timestamp>.csv that was executed.")
    undo.add_argument("--output", help="Folder with that plan's journal. Default: the plan's folder.")
    _remaining(undo)
    _common(undo, "Threads for restoring files.")

    args = parser.parse_args()
    if args.io_timeout <= 0:
        parser.error("--io-timeout must be a positive number.")
    if args.workers < 1:
        parser.error("--workers must be a positive integer.")
    if args.command == "plan":
        if args.before and (args.date_from or args.date_to):
            parser.error("Use either --before, or --from with --to, not both.")
        if not args.before and not (args.date_from and args.date_to):
            parser.error("Give a date range: --before DATE, or --from DATE --to DATE.")
        try:
            if args.before:
                args.lo, args.hi = float("-inf"), _parse_date(args.before)
            else:
                args.lo, args.hi = _parse_date(args.date_from), _parse_date(args.date_to)
        except argparse.ArgumentTypeError as exc:
            parser.error(str(exc))
        if args.lo >= args.hi:
            parser.error("--from must be before --to.")
        if "all" in args.categories and len(args.categories) > 1:
            parser.error("--categories all can't be combined with other categories.")
        label = args.archive_label.strip()
        if not label or any(c in label for c in "|\r\n"):
            parser.error("--archive-label must be non-empty and contain no '|' or line breaks.")
        args.archive_label = label
        if args.raw_csv and not Path(args.raw_csv).is_file():
            parser.error(f"--raw-csv file does not exist: {args.raw_csv}")
    return args


def main() -> None:
    args = parse_args()
    {"plan": cmd_plan, "execute": cmd_execute, "verify": cmd_verify, "undo": cmd_undo}[args.command](args)


if __name__ == "__main__":
    main()
