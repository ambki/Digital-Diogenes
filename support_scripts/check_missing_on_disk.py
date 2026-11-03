"""
Confirm on the storage what R_plots/archive_missing_check.R found in the
records: for each file an archive plan couldn't find, is its content really
still somewhere - its keeper, its quarantined copy in duplicates\\, another
copy on the storage, or on tape?

Run it where the storage is reachable (same drive letter or path as in the
listings), with the path list the R script writes:

    python check_missing_on_disk.py --paths "C:\\...\\archive_missing_paths.csv"

Standalone: only the Python standard library, so this file can be copied on
its own to any machine that reaches the storage. Read-only: nothing on the
storage is changed; it only stats the listed paths (and, with --hash, reads
the files that exist, to compare their md5 with the expected one).

Writes, next to the path list (or into --output):
    archive_missing_disk.csv        one row per missing file: what was found
                                    and the verdict on the storage
    archive_missing_disk_paths.csv  one row per path checked: exists / missing
                                    / error, size, date (and md5 with --hash)
Both are UTF-8 with a BOM, so Excel opens them with the right characters.
"""

import argparse
import csv
import hashlib
import os
import sys
import threading
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

INPUT_COLUMNS = {"missing_path", "md5", "record_verdict", "tape_label", "role", "path"}
PATH_COLUMNS = ["missing_path", "role", "path", "state", "detail", "size_bytes", "modified", "md5_found", "same_content"]
FILE_COLUMNS = [
    "missing_path", "md5", "record_verdict", "tape_label", "file_back", "keeper", "quarantined_copy",
    "other_copies_on_storage", "other_copies_in_duplicates", "paths_checked", "paths_with_errors",
    "disk_verdict", "class",
]
OTHER_IN_DUPLICATES = {"other copy, listed in duplicates\\", "other copy, if quarantined"}


def to_long_path(path: str) -> str:
    """\\\\?\\ form, so paths over 260 characters work on Windows."""
    if os.name != "nt" or path.startswith("\\\\?\\"):
        return path
    path = os.path.abspath(path)
    return "\\\\?\\UNC\\" + path[2:] if path.startswith("\\\\") else "\\\\?\\" + path


def run_with_timeout(func, timeout: float):
    """(result, error): func() in a daemon thread, so a hung network share can't block the run."""
    box: dict = {}

    def target():
        try:
            box["value"] = func()
        except BaseException as exc:  # reported as the path's error
            box["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        return None, TimeoutError(f"timed out after {timeout:.0f}s")
    if "error" in box:
        return None, box["error"]
    return box["value"], None


def md5_of(path: str) -> str:
    digest = hashlib.md5()
    with open(to_long_path(path), "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def check(path: str, timeout: float, hash_files: bool) -> dict:
    """stat (and optionally hash) one path: state exists / missing / error."""
    value, error = run_with_timeout(lambda: os.stat(to_long_path(path)), timeout)
    if isinstance(error, (FileNotFoundError, NotADirectoryError)):
        return {"state": "missing"}
    if error is not None:
        return {"state": "error", "detail": str(error)}
    result = {
        "state": "exists",
        "size_bytes": value.st_size,
        "modified": datetime.fromtimestamp(value.st_mtime, timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
    }
    if hash_files:
        found, error = run_with_timeout(lambda: md5_of(path), max(timeout, 600))
        result["md5_found"] = found or ""
        if error is not None:
            result["detail"] = f"md5: {error}"
    return result


def plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word.replace('copy', 'copies')}"


def verdict(rows: list[dict], present) -> tuple[str, str, dict]:
    """The storage verdict for one missing file, from the results of its paths."""
    first = rows[0]
    found = Counter(row["role"] for row in rows if present(row))
    errors = sum(row["state"] == "error" for row in rows)
    others_storage = found["other copy"]
    others_dups = sum(found[r] for r in OTHER_IN_DUPLICATES)
    back = found["the missing file itself"] > 0
    keeper = found["keeper (journal)"] > 0
    q_journal = found["quarantined copy (journal)"] > 0
    q_lookup = found["quarantined copy (if not journaled)"] > 0

    places = []
    if first["tape_label"]:
        places.append(f"on tape ({first['tape_label']})")
    if back:
        places.append("the file itself is back at its path")
    if keeper:
        places.append("its keeper is on the storage")
    if others_storage:
        places.append(plural(others_storage, "other copy") + " on the storage")
    if q_journal:
        places.append("its quarantined copy is in duplicates\\")
    if q_lookup:
        places.append("its copy is in duplicates\\ (not in the journal)")
    if others_dups:
        places.append(plural(others_dups, "other copy") + " in duplicates\\")

    if first["tape_label"] or back or keeper or others_storage:
        cls, text = "OK", "content found: " + ", ".join(places)
    elif places:
        cls, text = "CHECK", "only in duplicates\\ (" + ", ".join(places) + "): it goes when the quarantine is finalized"
    elif errors:
        cls, text = "INCOMPLETE", f"no copy found, but {plural(errors, 'path')} couldn't be checked (see the paths file)"
    else:
        cls, text = "ALERT", "no copy found on tape or on the storage"

    def status(role):
        mine = [row for row in rows if row["role"] == role]
        if not mine:
            return ""
        return "found" if any(present(row) for row in mine) else mine[0]["state"]

    summary = {
        "file_back": status("the missing file itself"),
        "keeper": status("keeper (journal)"),
        "quarantined_copy": status("quarantined copy (journal)") or
                            ("found (not in the journal)" if q_lookup else status("quarantined copy (if not journaled)")),
        "other_copies_on_storage": f"{others_storage} of {sum(row['role'] == 'other copy' for row in rows)}",
        "other_copies_in_duplicates": others_dups,
        "paths_checked": len(rows),
        "paths_with_errors": errors,
    }
    return cls, f"{cls}: {text}", summary


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Check on the storage the paths written by R_plots/archive_missing_check.R (read-only)."
    )
    parser.add_argument("--paths", required=True, help="The archive_missing_paths.csv written by archive_missing_check.R.")
    parser.add_argument("--output", help="Folder for the results. Default: the folder of --paths.")
    parser.add_argument("--hash", action="store_true",
                        help="Also compute the md5 of every file found, and count it only if it matches "
                             "(slower: reads the files). Default: existence only.")
    parser.add_argument("--workers", type=int, default=8, help="Paths checked at the same time. Default: 8.")
    parser.add_argument("--io-timeout", type=float, default=10.0,
                        help="Seconds to wait for one stat before giving up on that path. Default: 10.")
    args = parser.parse_args()

    paths_file = Path(args.paths)
    if not paths_file.is_file():
        sys.exit(f"ERROR: '{paths_file}' not found.")
    with open(paths_file, newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        absent = INPUT_COLUMNS - set(reader.fieldnames or [])
        if absent:
            sys.exit(f"ERROR: '{paths_file}' is missing column(s) {sorted(absent)}; "
                     "use the archive_missing_paths.csv written by archive_missing_check.R.")
        rows = [row for row in reader if row["path"]]
    output = Path(args.output) if args.output else paths_file.parent
    output.mkdir(parents=True, exist_ok=True)

    # Each distinct path is checked once, however many missing files share it.
    unique = list(dict.fromkeys(os.path.normcase(row["path"]) for row in rows))
    original = {os.path.normcase(row["path"]): row["path"] for row in rows}
    print(f"Checking {len(unique)} path(s) for {len({r['missing_path'] for r in rows})} missing file(s)"
          f"{' (with md5)' if args.hash else ''} ...")
    results: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        for done, (k, result) in enumerate(zip(unique, executor.map(
                lambda k: check(original[k], args.io_timeout, args.hash), unique)), 1):
            results[k] = result
            if done % 200 == 0 or done == len(unique):
                print(f"\r  {done}/{len(unique)}", end="", flush=True)
    print()

    for row in rows:
        result = results[os.path.normcase(row["path"])]
        row.update({c: result.get(c, "") for c in ("state", "detail", "size_bytes", "modified", "md5_found")})
        row["same_content"] = "" if not row["md5_found"] else ("yes" if row["md5_found"] == row["md5"].lower() else "no")

    def present(row: dict) -> bool:
        return row["state"] == "exists" and row["same_content"] != "no"

    per_file = defaultdict(list)
    for row in rows:
        per_file[row["missing_path"]].append(row)
    summaries, classes = [], Counter()
    for missing_path, mine in per_file.items():
        cls, text, summary = verdict(mine, present)
        classes[cls] += 1
        summaries.append({"missing_path": missing_path, "md5": mine[0]["md5"],
                          "record_verdict": mine[0]["record_verdict"], "tape_label": mine[0]["tape_label"],
                          **summary, "disk_verdict": text, "class": cls})
    order = {"ALERT": 0, "INCOMPLETE": 1, "CHECK": 2, "OK": 3}
    summaries.sort(key=lambda s: (order[s["class"]], s["missing_path"]))

    files_out, paths_out = output / "archive_missing_disk.csv", output / "archive_missing_disk_paths.csv"
    for target, columns, data in ((files_out, FILE_COLUMNS, summaries), (paths_out, PATH_COLUMNS, rows)):
        partial = target.with_name(target.name + ".partial")
        with open(partial, "w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(data)
        partial.replace(target)

    print("\nOn the storage:")
    for cls in ("OK", "CHECK", "INCOMPLETE", "ALERT"):
        if classes[cls]:
            print(f"  {cls:<10} {classes[cls]} file(s)")
    errors = sum(r["state"] == "error" for r in results.values())
    if errors:
        print(f"  ({errors} path(s) couldn't be checked: permissions, timeouts or an unreachable share; see the paths file)")
    print(f"\nPer missing file: {files_out}\nPer path:         {paths_out}")


if __name__ == "__main__":
    main()
