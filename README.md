# Digital-Diogenes

Tools for finding, verifying and cleaning up duplicate and obsolete files on
large storage volumes, and for moving old files off to a cheaper tape archive.
The repository contains four Python scripts for the pipeline and one
standalone helper, each documented in its own section
below, in the order they are used:

| Step | Script | Purpose |
|---|---|---|
| 1. Analyse | [`duplicates_check.py`](#duplicates_checkpy) | Scans one or two folders for duplicate files (by MD5) and verifies backups against their source. |
| 1. Analyse | [`elimination_file_candidates.py`](#elimination_file_candidatespy) | Scans a folder for files that are candidates for elimination (old files, ZIP files, files in `Unmixed` folders). Independent of `duplicates_check.py`, but run at the same step: it also gives insight into the data before anything is eliminated. |
| 2. Clean up | [`quarantine_duplicates.py`](#quarantine_duplicatespy) | Uses the duplicate list from `duplicates_check.py --folder` to move every copy but the oldest into a reversible `duplicates` quarantine folder. |
| 3. Archive | [`archive_old_files.py`](#archive_old_filespy) | Moves the files modified in a date range into a `move_to_archive` folder (same folder structure), ready to be copied to tape by hand. Scans the storage itself, or reuses the newest raw listing. |
| When needed | [`change_folder_permissions.py`](#change_folder_permissionspy) | Standalone, run by hand: gives an account Modify on a folder tree (e.g. when `execute` fails with *Access is denied*), later reduces everyone else to read only, and restores the previous permissions. |

The R scripts used for reporting live in [`R_plots/`](R_plots/), and the
scripts that generate test data live in [`support_scripts/`](support_scripts/).

The whole pipeline has been positively tested in WindowsOS 11 in storage volumes up to 40 Tbs of size. Nevertheless, it can work in linux systems as well and for bigger storage sizes. 

## Table of contents

- [duplicates_check.py](#duplicates_checkpy)
  - [What it does](#what-it-does)
  - [Requirements](#requirements)
  - [Usage](#usage)
  - [How each mode actually works](#how-each-mode-actually-works)
  - [Generating test data](#generating-test-data)
  - [Memory use on large scans](#memory-use-on-large-scans)
  - [Arguments](#arguments)
  - [Notes](#notes)
- [elimination_file_candidates.py](#elimination_file_candidatespy)
  - [What it does](#what-it-does-1)
  - [Why "summed" and "unique" differ globally](#why-summed-and-unique-differ-globally)
  - [Requirements](#requirements-1)
  - [Usage](#usage-1)
  - [Arguments](#arguments-1)
  - [Generating test data](#generating-test-data-1)
  - [Notes](#notes-1)
- [quarantine_duplicates.py](#quarantine_duplicatespy)
  - [Workflow at a glance](#workflow-at-a-glance)
  - [What happens to the files](#what-happens-to-the-files)
  - [Requirements](#requirements-2)
  - [Inputs: which file goes into which option](#inputs-which-file-goes-into-which-option)
  - [Usage](#usage-2)
  - [Progress steps: what each bar is doing](#progress-steps-what-each-bar-is-doing)
  - [Resuming after a crash or kill](#resuming-after-a-crash-or-kill)
  - [Skip reasons](#skip-reasons)
  - [Arguments](#arguments-2)
  - [Output files (in `--output`)](#output-files-in---output)
  - [Design decisions](#design-decisions)
  - [Notes](#notes-2)
- [archive_old_files.py](#archive_old_filespy)
  - [Workflow at a glance](#workflow-at-a-glance-1)
  - [What happens to the files and folders](#what-happens-to-the-files-and-folders)
  - [Notes: where every file went](#notes-where-every-file-went)
  - [Requirements](#requirements-3)
  - [Input: which listing to use](#input-which-listing-to-use)
  - [Usage](#usage-3)
  - [Skip reasons and statuses](#skip-reasons-and-statuses)
  - [Arguments](#arguments-3)
  - [Output files](#output-files)
  - [Generating test data](#generating-test-data-2)
  - [Design decisions](#design-decisions-1)
- [change_folder_permissions.py](#change_folder_permissionspy)

---

## duplicates_check.py

Scans one or two folders for duplicate files (by MD5 checksum) and, when given
an origin/destination pair, verifies that a backup is a faithful copy of its
source.

### What it does

- **Duplicate scan** — recursively hashes every file in a folder and reports
  groups of files that share the same MD5 (true duplicates), how much space
  they waste, and their paths.
- **Cross-folder duplicates** (`between`) — finds files that exist in *both*
  the origin and destination with the same MD5, regardless of filename or
  path.
- **Integrity check** (`integrity`) — for every file in the origin, checks
  whether the same relative path exists in the destination and whether its
  MD5 matches. Reports `OK`, `MISMATCH`, `MISSING IN DEST`, or `UNREADABLE`.

Results are printed to the terminal (at most the top 50 rows per table, so a
huge scan doesn't spend hours formatting output). Passing `--excel` additionally writes
every result table to its own CSV file (despite the flag's name — see
"Also export results" below for why it's CSV, not an actual `.xlsx`).

Every duplicate group (in `folder`/`origin`/`destination`/`between`) also
reports the **oldest copy** — the file with the earliest modification time —
as `oldest_path` and `oldest_modified`. For `between`, `oldest_source` says
whether that oldest copy lives in the origin or the destination. The idea is
to keep that one file and safely delete the rest of the group.

### Requirements

- Python 3.10+
- `pandas`, `tqdm`

```powershell
pip install pandas tqdm
```

### Usage

Run from the repository root.

#### Compare two folders (origin vs. destination/backup)

Runs all checks: origin duplicates, destination duplicates, cross-folder
duplicates, and integrity comparison.

```powershell
python duplicates_check.py --origin "C:\path\to\origin" --destination "C:\path\to\destination"
```

#### Scan a single folder for duplicates

```powershell
python duplicates_check.py --folder "C:\path\to\folder"
```

#### Run only one check

Use `--mode` with an origin/destination pair:

```powershell
python duplicates_check.py --origin "C:\origin" --destination "C:\destination" --mode integrity
python duplicates_check.py --origin "C:\origin" --destination "C:\destination" --mode between
python duplicates_check.py --origin "C:\origin" --destination "C:\destination" --mode origin
python duplicates_check.py --origin "C:\origin" --destination "C:\destination" --mode destination
```

#### Also export results

Add `--excel` with a target path to additionally write every result table to
its own CSV file. Omit it to only print to the terminal.

```powershell
python duplicates_check.py --origin "C:\origin" --destination "C:\destination" --excel "C:\reports\duplicates_report.xlsx"
```

Despite the flag's name (kept as `--excel` since that's the established
option — it just takes a base path, the extension you give it is ignored),
**nothing gets written as an actual `.xlsx` file.** Instead, one CSV per
result table lands next to the path you gave, named `<stem>_<table>.csv` —
for the command above: `duplicates_report_origin_duplicates.csv`,
`duplicates_report_origin_summary.csv`, `duplicates_report_between_summary.csv`,
`duplicates_report_integrity_results.csv`, `duplicates_report_skipped_folders.csv`,
plus `duplicates_report_raw.csv` (see below) — whichever of those apply to
the `--mode` you ran. The terminal output tells you exactly which files got
written and where.

This is CSV rather than an Excel workbook on purpose: Excel sheets are
capped at 1,048,576 rows, and several of these tables have no a-priori
bounded size — `*_duplicates` scales with how much of the scan turned out
to be duplicated (a heavily-duplicated multi-million-file scan can put
nearly every file in it), and `integrity_results` has one row per origin
file, duplicate or not. Either can blow straight through that cap on a big
scan — and when a single sheet in a workbook is too large, `openpyxl`
raises `This sheet is too large!` and **the whole write fails, nothing
gets saved at all**, not even the other sheets that would have fit fine.
Only `*_summary`/`skipped_folders` are reliably small regardless of scan
size, but since any of the others can fail unpredictably depending on your
specific data, everything is CSV for certainty rather than just the ones
most likely to be huge. CSV also writes/reads far faster at
multi-million-row scale besides.

`<stem>_raw.csv` is a plain, one-row-per-file listing of every file the
script scanned (not just duplicates/mismatches): `filename`, `path`,
`mtime` (human-readable), `md5`, and `size_gb`. Meant for further analysis
outside the script — e.g. loading into R to bucket files by year and chart
storage growth over time — rather than for reading directly. It's written
at a different point than the others, and actually written **twice**: once
right after each folder's file list and size/date are collected but
*before* hashing starts (same columns, minus `md5`, since it isn't known
yet), and again at the very end with `md5` filled in. On a big scan,
hashing (reading every file's full contents) is by far the slowest part,
so the early write gets you a usable file listing on disk well before that
finishes — useful on its own, and also what `--resume-from` (below) reads
from if hashing gets interrupted.

#### Resume an interrupted scan

If a big scan gets interrupted partway through hashing (crash, closed
terminal, etc.), you don't have to re-walk the whole folder tree again —
point `--resume-from` at the raw export it already wrote (the early
pre-hash version, or a final one from a previous run) instead of `--folder`/
`--origin`/`--destination`'s usual walk:

```powershell
python duplicates_check.py --folder "C:\path\to\folder" --excel "C:\reports\duplicates_report.xlsx" --resume-from "C:\reports\duplicates_report_raw.csv"
```

This reuses the file list from that export but still re-collects each
file's size/mtime and re-hashes it fresh — it's not blindly trusting old
metadata, just skipping the (often much slower, on a big remote folder)
directory-by-directory walk. A file that's been deleted since the export
was written is simply treated as unreadable, same as an ordinary permission
error, rather than causing an error. For `--origin`/`--destination`, one
export covering both sides works fine — files are matched back to origin or
destination by which root their path falls under. `--resume-from` can't be
combined with `--included`/`--excluded`, since the file list it loads is
already fixed.

The paths in the export are used exactly as written, so they must be
reachable from the terminal you resume in. This matters for **mapped drive
letters** (e.g. `Z:\...`): they belong to one logon session, so an
elevated/"Run as administrator" terminal, a scheduled task, or another user
usually doesn't see them, even though the same share works elsewhere. Before
doing anything else, the script stats a sample of 20 paths spread across the
list and stops with an error if none of them can be accessed. A warning is
also printed if resumed paths aren't under `--folder` (a mapped drive and its
`\\server\share` path count as different places here). In any scan, files
that fail to stat or hash are counted and reported with their first few
error messages, and a run where *every* file fails to stat stops before
hashing instead of writing empty results.

#### Speed up hashing with threading

By default, files are hashed one at a time. For large scans (thousands of
files, some multi-GB) on storage that supports concurrent reads — a network
volume that allows several users at once, or a local SSD/NVMe — add
`--threading` to hash several files in parallel:

```powershell
python duplicates_check.py --origin "C:\origin" --destination "C:\destination" --threading
```

`--threading` uses 8 worker threads by default. Tune that with `--workers`
(only valid together with `--threading`):

```powershell
python duplicates_check.py --origin "C:\origin" --destination "C:\destination" --threading --workers 16
```

Notes on choosing a worker count:

- More workers helps mainly by overlapping I/O wait time (multiple reads "in
  flight" at once), and to a lesser extent lets MD5 computation itself run on
  multiple CPU cores.
- On a single spinning disk, too many concurrent reads can cause seek
  thrashing and make things *slower* — keep `--threading` off or use a low
  worker count there.
- On a network volume, your own network link (and the server's total
  capacity across all its users) is still the ceiling — pushing worker count
  far past what the link/server can service in parallel won't add more
  speed.
- There's no universal best value; 8 is a reasonable starting point to tune
  up or down from.

#### Restrict the scan to, or exclude, specific subfolders

By default the scan covers everything under `--folder`/`--origin`/`--destination`.
To narrow that down — e.g. to scan a huge folder in smaller pieces to save
time/RAM, or to skip a subfolder you know is slow/irrelevant — use
`--included` or `--excluded` (not both) with one or more paths:

```powershell
python duplicates_check.py --folder "C:\path\to\folder" --included "C:\path\to\folder\2023" "C:\path\to\folder\2024"
python duplicates_check.py --folder "C:\path\to\folder" --excluded "C:\path\to\folder\Trash"
```

`--included` and `--excluded` both apply to whichever folder(s) the current
`--mode` scans (`--folder`, or `--origin`/`--destination`). Duplicates are
still only found *within a single run* — scanning `2023` and `2024` together
in one `--included` call finds duplicates between them, but scanning them as
two *separate* runs would not, since each run only sees its own files. See
[Also export results](#also-export-results) above for combining separate runs' raw
listings afterward if you do need to split the scan across multiple
invocations.

### How each mode actually works

Every mode starts from the same scan: a table of `path, filename, size_bytes, md5`
for each file in a folder. The modes differ only in how they compare rows of
that table.

#### `folder` / `origin` / `destination` — duplicates inside one folder

Groups the rows of **a single folder's table** by `md5`. Any hash that shows
up 2+ times is a duplicate group — filenames and paths are irrelevant.

> Answers: *"does this exact content exist more than once inside this one
> folder?"*

#### `between` — duplicates across the two folders

Stacks the origin table and the destination table together, then groups by
`md5` again — same logic as above, but now checking whether a hash appears
**on both sides**. Filenames and paths are still irrelevant; a file that was
renamed or moved to a completely different subfolder in the destination still
counts as a match.

> Answers: *"does this exact content exist somewhere in both folders,
> wherever it happens to sit?"*

#### `integrity` — did the backup copy succeed, file by file?

This one does **not** start by comparing hashes across all files. It matches
files by **relative path** first, then checks the hash only for that matched
pair:

1. Take an origin file, e.g. `origin_root\2024\img001.tif` → relative path
   `2024\img001.tif`.
2. Look for that *same relative path* under the destination root, e.g.
   `destination_root\2024\img001.tif`.
3. Based on what it finds there:
   - No file at that relative path in the destination → **MISSING IN DEST**
   - File exists, but the MD5 couldn't be read on one side → **UNREADABLE**
   - File exists, MD5s match → **OK**
   - File exists, MD5s differ → **MISMATCH**

> Answers: *"for every file the origin has, does the destination have that
> exact same file, in that exact same location?"*

#### Why `between` and `integrity` can disagree

Say `origin\2024\img001.tif` was backed up but landed at
`destination\archive\img001.tif` instead of `destination\2024\img001.tif`:

- `integrity` looks for `2024\img001.tif` in the destination, doesn't find it
  there → reports **MISSING IN DEST**, even though the file did get copied.
- `between` ignores location entirely and just checks "is this md5 present in
  both folders?" → finds it → reports it as a cross-folder duplicate, not
  missing.

So: `integrity` tells you whether the backup mirrors the origin's folder
structure faithfully; `between` tells you whether the content exists
somewhere in the destination at all, regardless of where.

### Generating test data

To test the script, you can run [`support_scripts\generate_duplicate_test_data.py`](support_scripts/generate_duplicate_test_data.py), which builds a folder structure with known duplicates, mismatches, and missing files. You can test as well every mode above (and features like `--threading` and the progress bar) without needing real backup data.

```powershell
python support_scripts\generate_duplicate_test_data.py --path "C:\path\to\test_area" --mode all --large-file-mb 20 --num-files 1
```

| Argument           | Description                                                                                   |
|--------------------|-------------------------------------------------------------------------------------------------|
| `--path`           | Base folder where the test folders will be created. Required.                                   |
| `--mode`           | Which check to build a scenario for: `all` (default), `folder`, `origin`, `destination`, `between`, `integrity`. |
| `--large-file-mb`  | Size in MB of each large duplicated file pair, used to actually exercise the progress bar and `--threading`. Default: **20**. |
| `--num-files`      | Multiplier applied to every fixture group (duplicate groups, moved/OK/mismatch/missing files, large pairs), keeping their proportions fixed while scaling up total file count — e.g. `--num-files 50` gives 50 duplicate groups, 50 moved files, 50 large pairs, etc. instead of 1 of each. Default: **1**. |
| `--clean`          | Remove this script's own previously generated subfolders (`folder_test`, `origin`, `destination`) under `--path` before regenerating. Never touches anything else under `--path`. |

Use `--num-files` for a longer-running scenario with many files (to watch the
progress bar/`files_left` counter tick over time, and give `--threading`
enough parallel work to matter) and `--large-file-mb` for a single file large
enough to watch its own hash crawl along — they're independent knobs.

For `--mode all` it creates:

- `folder_test\` — for `duplicates_check.py --folder ... --mode folder`: unique
  files, a small duplicate group, and a large duplicate pair.
- `origin\` and `destination\` — for the origin/destination modes:
  - a duplicate group inside `origin\` only (for `--mode origin`)
  - a duplicate group inside `destination\` only (for `--mode destination`)
  - a file moved to a different relative path with identical content in the
    destination (for `--mode between` — see *Why `between` and `integrity`
    can disagree* above)
  - one matching file (`OK`), one file edited after backup (`MISMATCH`), and
    one file never copied (`MISSING IN DEST`) (for `--mode integrity`)
  - an identical large file pair on both sides

For any other `--mode`, only the fixtures relevant to that check are built
(plus a baseline file so origin/destination are never empty).

It prints the exact `duplicates_check.py` command to run against the
generated data. `UNREADABLE` status isn't simulated, since that requires
OS-level permission changes rather than file content.

### Memory use on large scans

The script is built to scan millions of files on a machine with modest RAM
(it was tuned against ~6 million files on an 8 GB server). This matters
because in Python *every* value is its own object with a fixed overhead
(an int is 28 bytes, a float 24, a string 49 bytes plus its text), so at
millions of files the overhead, not the data, is what fills RAM. Once RAM
runs out, Windows starts swapping to disk and the scan slows to a crawl
that looks exactly like a hang (a progress bar that stops moving for hours).

Per-file state is therefore kept in the cheapest form available:

| Kept per file | Stored as | Why not the obvious alternative |
|---|---|---|
| File path | a plain `str` (~150 B) | A `pathlib.Path` (~1,170 B) caches the full path *plus* a list of every folder name *plus* a lowercased copy of each (used on Windows for case-insensitive comparison), i.e. the text ~3 times over across ~17 small objects. |
| Size and modification time | two numpy arrays (16 B total), indexed by file position | A `{path: (size, mtime)}` dict costs ~130 B per file (hash slot + tuple + int object + float object). |
| MD5 | one list of strings (~90 B), same indexing | A 5-key dict per file (~190 B) duplicates the size/mtime already stored and adds its own overhead. |
| Pending work with `--threading` | at most `workers × 64` tasks in flight | `executor.map()` creates *every* task up front, one `Future` (~1.6 KB, with its own lock/condition) per file, before returning a single result. |

The results DataFrame is built column-wise at the end, and its `path`
column points at the *same* string objects as the file list rather than
copying them.

#### How much RAM a scan needs

**A scan of 6 million files uses about 1.6 GB of RAM while it finds,
stats and hashes the files, and peaks at roughly 4 GB at the very end,
while it writes the CSV exports** (which briefly makes temporary copies of
the result tables). RAM use grows roughly in proportion to the number of
files scanned, so smaller scans need correspondingly less.

Where the 1.6 GB goes (size per file from the table above × 6 million):

| What the script holds in RAM | RAM at 6 million files |
|---|---|
| File list (every file's path) | ~1 GB |
| Pending hashing tasks (`--threading`) | ~0 |
| Every file's size and modification time | 0.1 GB |
| Every file's MD5 (results) | 0.5 GB |
| **Total during the scan** | **~1.6 GB** |
| **Peak, while writing the CSV exports** | **~4 GB** |

Other choices made for RAM efficiency:

- `--resume-from` reads only the `path` column of the raw CSV, not all of it.
- The pre-hash raw CSV is appended to disk per scan instead of being kept
  in memory for the rest of the run.
- The terminal shows at most the top 50 rows of each table; formatting a
  table with hundreds of thousands of rows can take hours with no output.
  The full tables are in the CSV exports.

If RAM still climbs toward the machine's limit, note which stage it's in
(finding files, collecting file info, hashing, or writing results); the
final export, which builds a second full table, is the next thing to trim.

### Arguments

| Argument        | Description                                                                 |
|-----------------|-------------------------------------------------------------------------------|
| `--folder`      | Single folder to scan recursively for duplicates. Mutually exclusive with `--origin`/`--destination`. |
| `--origin`      | Origin/source folder to scan. Required together with `--destination`.        |
| `--destination` | Destination/backup folder to scan. Required together with `--origin`.        |
| `--mode`        | Which check(s) to run: `all` (default), `folder`, `origin`, `destination`, `between`, `integrity`. |
| `--excel`       | Optional base path; if given, writes every result table to its own `<stem>_<table>.csv` (not an actual `.xlsx`, despite the flag's name — see above). If omitted, results are only printed to the terminal. |
| `--threading`   | Hash files concurrently using multiple threads. Off by default.              |
| `--workers`     | Number of hashing threads to use. Requires `--threading`. Default: 8.        |
| `--io-timeout`  | Seconds to wait for a single blocking filesystem operation (listing a folder, stat'ing a file, or reading a file for hashing) before giving up on it and moving on. Default: 30. |
| `--included`    | Restrict the scan to only these subfolder(s) (and their contents). One or more paths. Mutually exclusive with `--excluded`. Default: scan everything. |
| `--excluded`    | Skip these subfolder(s) (and their contents) during the scan. One or more paths. Mutually exclusive with `--included`. Default: nothing excluded. |
| `--resume-from` | Path to a previously written `..._raw.csv` export; resumes from its file list instead of re-walking the folder(s). Mutually exclusive with `--included`/`--excluded`. |

### Notes

- "Oldest copy" is based on file **modification time** (not creation time),
  since that's consistent across platforms — a file copied to a new location
  generally keeps its original modification time, so this points at the
  original rather than whichever copy happened to be scanned first.
- A progress bar is shown while each folder is hashed, tracking **bytes
  hashed** against the folder's total size (not file count) — a truer
  measure of progress when file sizes vary a lot, e.g. many small files
  alongside a few multi-GB ones. It also shows the number of files left as
  extra info alongside the bar.
- Every phase of scanning — finding files, collecting each file's size/date,
  and hashing — has its own live progress bar showing the current
  file/folder name (`now=...`), so on a big folder you can always tell it's
  still working through files rather than stuck, without needing to guess.
- Each folder is hashed **at most once per run**, regardless of `--mode`. The
  origin and destination scans are computed once and reused across the
  duplicates, `between`, and `integrity` checks, so `--mode all` does not
  re-hash files for each check.
- Unreadable files (permission errors, etc.) are excluded from duplicate
  detection but still counted as `UNREADABLE` in the integrity check.
- **Long paths (over 260 characters) are scanned too.** Windows' classic
  limit (MAX_PATH) makes any folder or file whose full path is longer than
  260 characters unreachable ("The system cannot find the path specified")
  unless the system setting `LongPathsEnabled` is on, which it isn't by
  default. Every filesystem call (listing a folder, stat'ing, opening to
  hash) therefore uses the Windows extended-length form `\\?\C:\...` /
  `\\?\UNC\server\share\...`, which lifts the limit regardless of that
  setting. Paths in every output keep their normal form. This also applies
  to `elimination_file_candidates.py` and `quarantine_duplicates.py`, which
  reuse these helpers.
- A folder or file that can't be accessed at all is skipped rather than
  aborting the whole scan — whether it fails fast (permission denied, a
  corrupted folder) or just hangs without ever raising an error (an
  unreachable network path, a stuck handle — the kind of folder that also
  hangs Explorer or `cmd`). This covers every blocking filesystem call the
  script makes: listing a folder's contents, `stat`'ing a file (size/mtime),
  and reading a file's bytes to hash it — a folder can list fine while one
  specific file inside it is what's actually stuck, so all three needed
  their own protection, not just the folder listing. Anything unresponsive
  is given up on after `--io-timeout` seconds (default: 30; tune it down
  for a faster scan on a flaky network, or up if some folders/files are
  just slow rather than actually stuck). For hashing, the timeout counts
  time with *no data read*, not total time, so a multi-GB file that keeps
  making progress is always hashed to the end. A skipped **folder** means its
  contents (and, in turn, all of its subfolders) are never analyzed at all;
  a skipped **file**'s stat and/or hash falls back to unreadable
  (size 0 / `md5=None`, same as an ordinary permission error) without
  affecting the rest of the scan. Either way, a warning is printed
  immediately, a final count of skipped folders is printed once scanning
  finishes ("N folder(s) could not be accessed..."), and with `--excel` the
  skipped folders are also recorded in a `..._skipped_folders.csv` (scope,
  folder, error) so it's not just a scrolled-past console line.
- The integrity check matches files by path *relative* to each root folder,
  so folder structure must line up between origin and destination for a file
  to be recognized as present.
- Hashing is MD5 over the full file content, done in 1 MB chunks, so large
  folders can take a while to scan.

---

## elimination_file_candidates.py

This is a script to detect specific formats/folders that the user is interested to eliminate, others than duplicates. It is now tailored to (spectral) flowcytometry, but you can fork it and add another requirements. Concretely, it scans a folder (recursively, across all subfolders) for files that are candidates for elimination, so you know what could be safely deleted and how much space it would free up.

### What it does

Three candidate groups, each built from the same folder scan:

- **Old files** — files whose modification time is older than `--years`
  years ago (default: 10).
- **ZIP files** — files with a `.zip` extension (case-insensitive).
- **Unmixed folder files** — files that live anywhere inside a folder named
  `Unmixed` (case-insensitive, at any depth). The folder itself isn't
  reported — the files inside it are. Specific to detect folders with unmixed files from spectral flowcytometry.

For each group, and globally across all three, it reports:

- File count and total size.
- The top directories by size within that group (a quick way to spot where
  most of the space is going).
- The full list of file paths in that group — printed to the terminal (up to
  50 rows, then a count of the rest) and always written in full to the Excel
  workbook if `--excel` is given.

It also saves a barplot (PNG) showing storage size per group and, globally,
as absolute numbers.

### Why "summed" and "unique" groups in the barplot

A file can belong to more than one group — e.g. an old `.zip` sitting inside
an `Unmixed` folder counts as both an "old file" and a "ZIP file" and an
"Unmixed folder file". The global report shows two numbers so nothing is
silently double-counted:

- **Summed** — each group's count/size added up as-is. A file in two groups
  is counted twice.
- **Unique (deduplicated)** — the same files counted once by path. This is
  the number that reflects how much disk space you'd actually free up by
  deleting every candidate.
- **Overlap** — the count/size of files that fall into more than one group,
  i.e. the gap between the two numbers above.

### Requirements

- Python 3.10+
- `pandas`, `tqdm`, `matplotlib` (and `openpyxl` if using `--excel`)

```powershell
pip install pandas tqdm matplotlib openpyxl
```

`elimination_file_candidates.py` also imports `validate_folder` and
`export_results_to_excel` from `duplicates_check.py`, so both files must stay
in the same folder.

### Usage

Run from the repository root.

#### Basic scan

```powershell
python elimination_file_candidates.py --folder "C:\path\to\folder"
```

#### Also export full results to Excel

```powershell
python elimination_file_candidates.py --folder "C:\path\to\folder" --excel "C:\reports\elimination_candidates.xlsx"
```

This writes the workbook itself (`summary` and `skipped_folders` sheets —
see Notes below) plus several companion CSVs next to it:

- `elimination_candidates_raw.csv` — every file the scan found, regardless
  of group (`filename`, `path`, `mtime`, `size_gb`). Written as soon as the
  scan finishes, since the walk/stat step already covers every file no
  matter which groups you asked for — this is effectively free. Same
  columns as `duplicates_check.py`'s own raw export (minus `md5`, since
  this script never hashes), so it also works as a `--resume-from` input
  there — see [Resume an interrupted scan](#resume-an-interrupted-scan) in
  the `duplicates_check.py` section for using it to scope a duplicate scan to a
  particular subset of files without re-walking the folder.
- `elimination_candidates_old_files.csv`, `..._zip_files.csv`, and
  `..._unmixed_files.csv` — one per group, each a full listing of just the
  files in that group.

These are CSV rather than workbook sheets because a group like "old files"
(or the raw listing) can easily hold millions of rows on a big scan, and
Excel sheets are capped at 1,048,576 rows — CSV has no such limit and
writes much faster besides.

#### Change the "old files" age threshold

Default is 10 years.

```powershell
python elimination_file_candidates.py --folder "C:\path\to\folder" --years 5
```

#### Change where barplots are saved, or skip them

```powershell
python elimination_file_candidates.py --folder "C:\path\to\folder" --plots-dir "C:\reports\plots"
python elimination_file_candidates.py --folder "C:\path\to\folder" --no-plots
```

### Arguments

| Argument      | Description                                                                          |
|---------------|----------------------------------------------------------------------------------------|
| `--folder`    | Folder to scan recursively. Required.                                                  |
| `--years`     | Age threshold in years for the "old files" group. Default: 10.                         |
| `--excel`     | Optional path to write results to an Excel workbook (summary/skipped-folders sheets), plus a companion raw CSV (every file scanned) and one CSV per group with its full per-file listing (see above). |
| `--plots-dir` | Directory to save barplot PNGs into. Default: `elimination_plots`.                     |
| `--no-plots`  | Skip generating barplots.                                                              |
| `--io-timeout` | Seconds to wait for a single blocking filesystem operation (listing a folder or stat'ing a file) before giving up on it and moving on. Default: 30. |
| `--included`   | Restrict the scan to only these subfolder(s) (and their contents), under `--folder`. One or more paths. Mutually exclusive with `--excluded`. Default: scan everything. |
| `--excluded`   | Skip these subfolder(s) (and their contents) during the scan, under `--folder`. One or more paths. Mutually exclusive with `--included`. Default: nothing excluded. |

### Generating test data

To test the script, you can run [`support_scripts\generate_elimination_test_data.py`](support_scripts/generate_elimination_test_data.py), which builds a folder structure with known old files, `.zip` files, `Unmixed` folder files, and a file that overlaps all three groups at once (to exercise the summed/unique/overlap accounting). There is also a control file next to each group that should **not** be flagged. Use it to try out the modes above without needing real backup data.

```powershell
python support_scripts\generate_elimination_test_data.py --path "C:\path\to\test_area" --mode all --years 10 --num-files 1
```

| Argument      | Description                                                                                   |
|---------------|-------------------------------------------------------------------------------------------------|
| `--path`      | Base folder where the test folder will be created. Required.                                    |
| `--mode`      | Which group to build a scenario for: `all` (default), `old`, `zip`, `unmixed`.                  |
| `--years`     | Age threshold (in years) to build fixtures around — pass the same value to `elimination_file_candidates.py --years`. Default: **10**. |
| `--num-files` | Multiplier applied to every fixture group, keeping proportions fixed while scaling up total file count. Default: **1**. |
| `--clean`     | Remove this script's own previously generated subfolder (`elimination_test`) under `--path` before regenerating. Never touches anything else under `--path`. |

It prints the exact `elimination_file_candidates.py` command to run against
the generated data.

### Notes

- "Old" is based on file **modification time** (not creation time), for the
  same reason as in `duplicates_check.py` — it's consistent across platforms
  and survives files being copied to a new location.
- Files that can't be `stat()`'d (permission errors, etc.) are treated as
  size 0 and excluded from the "old files" check (their real age is
  unknown), but can still show up under ZIP or Unmixed, since those checks
  only depend on the filename/path.
- The Excel workbook (when `--excel` is given) has a `summary` sheet with
  the count/size numbers for each group, and a `skipped_folders` sheet (see
  below). The full per-file listing — both the raw "every file scanned"
  export and each group's own listing — is written as CSVs instead of
  workbook sheets — see "Also export full results to Excel" above.
- Both scanning phases — finding files and collecting each file's metadata —
  have their own live progress bar showing the current file/folder name
  (`now=...`), so on a big folder you can always tell it's still working
  through files rather than stuck, without needing to guess.
- A folder or file that can't be accessed at all is skipped rather than
  aborting the whole scan — whether it fails fast (permission denied, a
  corrupted folder) or just hangs without ever raising an error (an
  unreachable network path, a stuck handle — the kind of folder that also
  hangs Explorer or `cmd`). This covers both listing a folder's contents
  and `stat`'ing a file (size/mtime), since a folder can list fine while a
  specific file inside it is what's actually stuck. Anything unresponsive
  is given up on after `--io-timeout` seconds (default: 30). A skipped
  **folder** means its contents (and all of its subfolders) are never
  analyzed at all; a skipped **file** falls back to unreadable (same as an
  ordinary permission error) without affecting the rest of the scan.
  Either way, a warning is printed immediately, a final count of skipped
  folders is printed once scanning finishes ("N folder(s) could not be
  accessed..."), and with `--excel` the skipped folders are also recorded
  in the `skipped_folders` sheet (scope, folder, error).
- **Long paths (over 260 characters) are scanned too**, regardless of the
  Windows `LongPathsEnabled` setting: folder listing and `stat` go through
  `duplicates_check.py`'s helpers, which use the extended-length form
  `\\?\C:\...` / `\\?\UNC\server\share\...` for every filesystem call.
  Paths in the outputs keep their normal form. See "Long paths" in the
  [`duplicates_check.py` notes](#notes).
- No file hashing is done here (unlike `duplicates_check.py`) — only
  filename, path, size, and modification time are needed, so scanning is
  fast even on large folders.

---

## quarantine_duplicates.py

Takes the duplicate list produced by `duplicates_check.py --folder` and, for
every group of identical files (same md5), keeps the **oldest** copy where it
is and moves every other copy into a `duplicates` folder under the same
storage root. The oldest file is kept because the oldest file is "a priori" the first generated, so the "rawest". Duplicates come from this very first (and oldest) file. 

The script then re-hashes everything in `duplicates` to prove that exactly the expected files landed there, unchanged.

**Nothing is deleted**. Space is only freed once you have reviewed the reports
and delete the `duplicates` folder yourself. Until then, every move can be
reversed with `undo`.

### Workflow at a glance

```
duplicates_check.py --folder ROOT --excel scan.xlsx
        │  scan_folder_duplicates.csv + scan_folder_summary.csv
        ▼
quarantine_duplicates.py plan      → move_plan_<timestamp>.csv   (dry run, review it)
        ▼
quarantine_duplicates.py execute   → moves + notes + journal, then verify
        ▼
quarantine_duplicates.py verify    → (optional) re-run the md5 check alone
quarantine_duplicates.py undo      → (optional) put everything back
quarantine_duplicates.py remaining → raw listing of what stays after deleting `duplicates`, no rehash
```

### What happens to the files

For a group `R:\A\scan.tif` (oldest), `R:\B\scan.tif`, `R:\C\x\scan.tif`:

- `R:\A\scan.tif` stays. It is the **keeper** (`oldest_path` in the
  summary CSV).
- `R:\B\scan.tif` → `R:\duplicates\B\scan.tif`
- `R:\C\x\scan.tif` → `R:\duplicates\C\x\scan.tif`
- In each of the duplicate locations a `R:\B\original_file_location.txt` and/or `R:\C\x\original_file_location.txt` files are created, pointing to the location of the oldest file. For example:

  ```
  moved_file: scan.tif | now_at: R:\duplicates\B\scan.tif | original_kept_at: R:\A\scan.tif | md5: a3f… | moved_on: 2026-10-06 12:11:02
  ```

The `duplicates` folder **mirrors the original folder structure**, so files
with the same name from different folders never collide, and every file's
origin can be read from its path. There is **one note per source folder**,
appended to, with one line per file moved out of it. A folder whose files
were all moved keeps only its note. That is intended.

### Requirements

- Python 3.10+
- `pandas`, `tqdm`

`quarantine_duplicates.py` imports helpers from `duplicates_check.py`, so both
files must stay in the same folder.

### Inputs: which file goes into which option

Every input comes from **one single run of `duplicates_check.py` in folder
mode** (`--folder`) on the storage you want to clean. For example:

```powershell
python duplicates_check.py --folder "R:\" --excel "C:\reports\duplicates_analysis\duplicates_report.xlsx"
```

In folder mode, `--excel` only sets the **folder and base name** of the
outputs. No `.xlsx` workbook is written; instead you get these CSVs next to
where the workbook would be:

| File written by duplicates_check.py | Contents | Used by quarantine_duplicates.py as |
|---|---|---|
| `duplicates_report_folder_duplicates.csv` | one row per duplicated file: `path, filename, size_bytes, mtime, md5` | `plan --duplicates-csv` |
| `duplicates_report_folder_summary.csv` | one row per md5 group, incl. `oldest_path` (the keeper) | `plan --summary-csv` |
| `duplicates_report_raw.csv` | every file scanned: `filename, path, mtime, md5, size_gb` | `remaining --raw-csv` (found automatically next to `--duplicates-csv`; pass it only if you moved it) |
| `duplicates_report_skipped_folders.csv` | folders that couldn't be scanned | not an input. Check it yourself: files in those folders were never checked for duplicates |

The other options are not files from the analysis:

| Option | What to give |
|---|---|
| `plan --root` | **Exactly the same folder** given to `duplicates_check.py --folder`, in the same spelling (`R:\` and `\\server\share\` count as different). `duplicates` is created directly under it. |
| `plan --output` | Any folder for the plan and all reports, e.g. `C:\reports\quarantine`. Not inside `<root>\duplicates`. |
| `execute` / `verify` / `undo` / `remaining` `--plan` | The `move_plan_<timestamp>.csv` that `plan` wrote into `--output`. Its `_settings.json` must stay next to it: it records the root and the two input CSVs, which is why these commands need no other input. |

Rules:

- **All inputs from the same run.** The duplicates and summary CSVs are
  cross-checked. If they come from different runs, the affected groups are
  skipped (`NO_KEEPER_IN_SUMMARY`, `KEEPER_NOT_IN_LIST`). Any path outside
  `--root` stops the plan.
- **Leave the input CSVs where they are** until you've finished with this
  plan. `execute` re-reads them (through the settings file) to check that
  the plan wasn't changed beyond `MOVE`↔`SKIP`.
- **Use the final raw CSV, not the early one.** duplicates_check.py writes
  `_raw.csv` once before hashing (no `md5` column) and overwrites it when
  the scan finishes. If the scan was interrupted, `remaining` refuses the
  md5-less file.
- **Use folder-mode outputs, not origin/destination ones.**
  `_between_summary.csv` pairs each file with its **backup copy**, so
  "deduplicating" it would remove your backup. It and
  `_integrity_results.csv` are refused anyway: they lack the required
  columns. `_origin_duplicates.csv` + `_origin_summary.csv` (and the
  `_destination_` pair) have the same format as the folder-mode files and
  would work with `--root` set to that side's folder. Since paths outside
  `--root` stop the plan, a mix-up with the other side can't move anything,
  but a dedicated `--folder` run is clearer.

Full example with the files above:

```powershell
python quarantine_duplicates.py plan `
    --duplicates-csv "C:\reports\duplicates_analysis\duplicates_report_folder_duplicates.csv" `
    --summary-csv    "C:\reports\duplicates_analysis\duplicates_report_folder_summary.csv" `
    --root   "R:\" `
    --output "C:\reports\quarantine"

python quarantine_duplicates.py execute   --plan "C:\reports\quarantine\move_plan_20261006_121052.csv"
python quarantine_duplicates.py remaining --plan "C:\reports\quarantine\move_plan_20261006_121052.csv"
```

### Usage

Run from the repository root.

#### 0. Scan for duplicates (folder mode)

```powershell
python duplicates_check.py --folder "R:\" --excel "C:\reports\scan.xlsx" --excluded "R:\duplicates"
```

Use `--excluded "R:\duplicates"` once a `duplicates` folder exists. Otherwise
every quarantined file is reported again together with its keeper. The plan
step refuses such groups anyway (`IN_DUPLICATES_FOLDER`).

#### 1. Plan (dry run, moves nothing)

```powershell
python quarantine_duplicates.py plan `
    --duplicates-csv "C:\reports\scan_folder_duplicates.csv" `
    --summary-csv    "C:\reports\scan_folder_summary.csv" `
    --root   "R:\" `
    --output "C:\reports\quarantine"
```

Prints a summary and writes `move_plan_<timestamp>.csv` plus
`move_plan_<timestamp>_settings.json` (the root, inputs and filters used)
into `--output`:

```
Groups: 1240 | Files to move: 3512 (412.700 GB) | Long paths (> 259 chars, moved and flagged): 2
Skipped: ZERO_BYTE 88 | EXCLUDED_PATTERN 41 | KEEPER_CHANGED 3 | DUPLICATE_MISSING 7
```

The plan has one row per file in the duplicate list:

| column | meaning |
|---|---|
| `action` | `MOVE`, `SKIP` or `KEEP` (the keeper) |
| `reason`, `detail` | why a row is `SKIP` (see *Skip reasons*) |
| `role` | `keeper`, `duplicate` (may be moved), or `blocked` (its group can't be moved, see `reason`) |
| `md5`, `size_bytes`, `size_gb`, `mtime`, `modified` | from the scan (`modified` is UTC, like duplicates_check.py) |
| `original_path` | where the file is now |
| `duplicates_path` | where it will go |
| `keeper_path`, `keeper_size_bytes`, `keeper_mtime` | the copy that stays, and its scan size/mtime |
| `long_path` | destination longer than 259 characters (see *Long paths*) |
| `check` | fingerprint of the row's role, md5, paths and scan values, used by `execute` to detect edits |

**Review it**, for example in R. In particular, check that the keepers sit in
sensible places: "oldest" is not always "the right one".

**Profiling a plan in R:
[`quarantine_plan.R`](R_plots/quarantine_plan.R).** Plots and tables of what the plan
would do, in number of files *and* in storage space. The two can differ a
lot: on Volume B, most of the volume's files could be moved but only a
small share of its space, because most duplicates are small files. Set `plan_path`, `raw_path`
(duplicates_check.py's `_raw.csv` of the same scan) and `output_folder` in
the configuration block, run section **0. Setup** once, then run any section
on its own, in any order (RStudio: Ctrl+Alt+T runs the current section).
Each section shows its plot; its last line exports it into `output_folder`
(run it or skip it):

| Section | What it shows | Exported as |
|---|---|---|
| 1. Files vs space | Moved / keeper / skipped / unique files, as a share of the volume's files and of its space | `quarantine_volume_share.png` |
| 2. Size of the files to move | Share of files vs share of space per size class | `quarantine_move_size_classes.png` |
| 3. Copies per group | How many identical copies each file has; top groups by copies and by space | `quarantine_copies_per_group.png`, `quarantine_top_groups.csv` |
| 4. Top folders | The folders (two levels under the root) with the most files to move and the most space to free | `quarantine_top_folders.png` |

Setup reads the plan in chunks and keeps only a few small columns per row,
so even Volume B's multi-GB plan stays around 1.3 GB of RAM (about 2–3 minutes).
Section 1 is the only one that reads the raw listing, so it is the slowest
section. Don't open a large plan with a plain `read_csv`: it loads every
path into memory.

**Editing the plan:** the only supported edit is changing `action` between
`MOVE` and `SKIP` (for files you'd rather not touch). Anything else, such as a
different keeper, edited paths or a keeper set to `MOVE`, is detected and
`execute` refuses to run: each row's `check` no longer matches, or its
`role` doesn't allow `MOVE`. Edit it with R or a text editor, **not Excel**:
Excel rewrites md5 values that look like numbers (`123e4567…`) and shortens
mtimes, which makes the plan fail validation.

**Large volumes (millions of duplicates): use `--no-preflight`.** It skips
the checks against the volume while planning (see *Progress steps*). The plan
is then built from the CSVs alone, in minutes. `execute` still checks every
file before moving it, so this is only less informative, not less safe:
stale-file skips show up in the execute results instead of in the plan.

Plans made by an earlier version of this script (without `role`/`check`)
are refused. Re-run `plan`.

#### 2. Execute

```powershell
python quarantine_duplicates.py execute --plan "C:\reports\quarantine\move_plan_20261006_121052.csv"
```

It does the following:

1. Validates the whole plan (see *Editing the plan*) before moving anything.
2. Creates `<root>\duplicates` if it doesn't exist.
3. For each `MOVE` row, on `--workers` threads (default 8; e.g. 16–32 for a
   large volume):
   - checks the **keeper** exists at `keeper_path` (so also by its file name)
     with the scan's size and mtime. If not, the whole group is skipped.
   - checks the duplicate itself still has the scan's size and mtime.
   - moves it (`os.rename`), appends its note line, then appends its journal
     line.
4. Writes `<plan>_execute_results.csv` (one row per `MOVE` row).
5. Runs **verify** (step 3 below), unless `--no-verify` is given.

**Retrying only the failed files.** Re-running the same command retries
everything that isn't moved yet, but it checks every `MOVE` row on disk again,
which is slow on a large volume. To retry only the files whose latest result is
`ERROR`, `MOVE_FAILED` or `MOVE_TIMEOUT`, add `--retry-failed`:

```powershell
python quarantine_duplicates.py execute --plan "C:\reports\quarantine\move_plan_20261006_121052.csv" --retry-failed
```

The same checks are run on each retried file. Its results go to
`<plan>_execute_results__movedFailed.csv`, so the full run's
`_execute_results.csv` is kept. Running `--retry-failed` again retries what
still failed (the retry file overrides the full run's results, unless a full run
was made after it). `CONFLICT`, `MISSING` and `SKIPPED_*` are not retried: look
at those first. For `MOVE_FAILED` with `[WinError 5] Access is denied`, give
yourself Modify on that folder first with
[`change_folder_permissions.py grant`](#change_folder_permissionspy).

**Re-running files after resolving them by hand.** `--retry-failed` doesn't
pick these up, since retrying them unchanged only gives the same result again.
Once they're resolved, run `execute` **without** `--retry-failed`. That re-checks
every row (the slow path), and each file still goes through all the checks, so
nothing unresolved is touched:

| status (detail) | how to resolve it | what the next `execute` does |
|---|---|---|
| `CONFLICT` (*both the original and a file at the destination exist*) | Remove or rename the file at the destination (left over from an earlier run or a manual copy). | Moves it as usual → `MOVED`. |
| | Or, if the destination copy is the right one, delete the original. | The destination matches the scan → `MOVED_RESUMED`. |
| `CONFLICT` (*original is gone and the file at the destination differs from the scan*) | The file was changed after the move; a re-run reports it as `CONFLICT` again. Decide by hand: move it back and make a new plan, or accept it as is (it stays out of the journal). | `CONFLICT` again. |
| `MISSING` | Put the original back. | Moves it as usual → `MOVED`. |
| `SKIPPED_*` | The keeper or the duplicate changed since the scan; the checks compare against the scan, so they're skipped again. | Re-scan with duplicates_check.py and make a new plan. |

Verify then checks only the retried files: it hashes each one at its
destination instead of re-hashing the whole duplicates folder, and writes
`<plan>_verify_report__movedFailed.csv` (same statuses as the full report,
without the folder-wide `EXTRA` and `UNSCANNED_FOLDER`). The full
`_verify_report.csv` is left as it was. Run `verify` on its own for a complete
check of the folder.

#### 3. Verify

Runs automatically after `execute`. Run it alone with:

```powershell
python quarantine_duplicates.py verify --plan "C:\reports\quarantine\move_plan_20261006_121052.csv" --threading
```

It re-hashes **every file under `<root>\duplicates`** and matches it to the
journal by **expected path and md5**. md5 alone isn't enough, because
duplicates share md5s by definition. It writes `<plan>_verify_report.csv`:

| status | meaning |
|---|---|
| `OK` | moved, found at the expected path, md5 matches |
| `MISSING` | journal says moved, but not found in `duplicates` |
| `MD5_MISMATCH` | found, but content differs from the scanned file |
| `UNREADABLE` | found, but could not be hashed |
| `EXTRA` | in `duplicates` but not an expected move of this plan |
| `MOVED_NOT_JOURNALED` | found, but the journal has no line (killed mid-file). Re-run `execute` |
| `NOT_MOVED` | `MOVE` row that wasn't moved; `detail` has the execute outcome (`MOVE_FAILED: …`, `SKIPPED_KEEPER_CHANGED`, …) |
| `SKIPPED_<reason>` | row that was `SKIP` in the plan |
| `UNSCANNED_FOLDER` | a folder in `duplicates` that couldn't be listed |

Columns: `status, detail, md5_expected, md5_found, original_path,
duplicates_path, keeper_path, size_bytes, size_found, mtime_original,
mtime_found, long_path`. This is the "missing / extra / raw metadata" report.

**Reporting on an execution in R:
[`quarantine_execution.R`](R_plots/quarantine_execution.R).** Plots of what
`execute`, verify and `undo` actually did, from the files they write next to
the plan. Set `plan_path` (the executed plan) and `output_folder`, run
**0. Setup** once (it reads nothing big), then any section on its own; each
reads the file it needs the first time, in chunks, and its last line exports
the plot:

| Section | What it shows | From | Exported as |
|---|---|---|---|
| 1. Execute outcome | Moved / skipped by a safety check / problem, per status, in files and space | `_execute_results.csv` (last run) | `execution_outcome.png` |
| 2. Progress over time | Cumulative files and space moved, all runs; restores by `undo` count down | `_journal.csv` + `_undo_journal.csv` | `execution_timeline.png` |
| 3. Verification | `OK`, `NOT_MOVED`, `SKIPPED_*` and problem statuses, in files and space | `_verify_report.csv` | `execution_verification.png` |
| 4. Problems | Files execute couldn't move, by error type and by folder | `_execute_results.csv` | `execution_problems.png`, `execution_problems.csv` |
| 5. Space per folder | What is in `duplicates` now, per original folder (net of restores) | `_journal.csv` + `_undo_journal.csv` | `execution_space_by_folder.png` |

#### 4. Undo (if needed)

```powershell
python quarantine_duplicates.py undo --plan "C:\reports\quarantine\move_plan_20261006_121052.csv"
```

Moves every journaled file back, removes its line from
`original_file_location.txt` (deleting the note once it's empty), and removes
the mirrored folders left empty in `duplicates`. A file is only restored if
the original location is free and the size in `duplicates` matches. Otherwise
it's reported as `CONFLICT`. Writes `<plan>_undo_journal.csv` and
`<plan>_undo_results.csv`. It is safe to re-run.

#### 5. Raw listing of the remaining files (no rehash)

```powershell
python quarantine_duplicates.py remaining --plan "C:\reports\quarantine\move_plan_20261006_121052.csv"
```

Writes `<plan>_remaining_raw.csv`: the volume's file listing as it will be
once you delete `duplicates`, without rehashing the volume. It is built from:

- duplicates_check.py's raw listing of the scan (`<stem>_raw.csv`, found next
  to the plan's `--duplicates-csv`, or given with `--raw-csv`),
- **minus** every file currently moved out. That list comes from replaying
  the journal and the undo journal: a file counts as moved if its latest
  event is a move, so files put back by `undo` stay listed. No network
  access is needed for this part,
- **minus** anything already under `<root>\duplicates`,
- **plus** the `original_file_location.txt` notes. They didn't exist at scan
  time and are tiny, so they are hashed fresh.

It has the same columns as the raw listing (`filename, path, mtime, md5,
size_gb`), so it can replace it anywhere: [`storage_report_by_year.R`](R_plots/storage_report_by_year.R),
the cross-storage comparison, or `duplicates_check.py --resume-from`.

It can be run before or after deleting `duplicates`; the result is the same.
Tested against a real rescan after deleting `duplicates`: identical paths,
md5s, mtimes and sizes.

Caveat: like any raw listing, it is a snapshot of the original scan. Files
added, changed or deleted on the volume since then (other than by this
script) are not reflected. Re-run it after an `undo`.

### Progress steps: what each bar is doing

Every step that can take a while shows a live progress bar (or a
"Loading/Writing …" line for CSV reads and writes, which can't report
progress). The **Touches the volume** column is what decides speed: local
steps take seconds to minutes even for millions of rows, while each
network round trip costs roughly 5–50 ms on a share.

| Command | Bar | What it does | Touches the volume |
|---|---|---|---|
| `plan` | Reading keepers from summary | Reads `md5` + `oldest_path` from the summary CSV (one entry per group). | No |
| `plan` | **Building plan + pre-flight checks** | Per block of 20,000 rows: normalizes each path, checks it's under `--root`, works out its destination, keeper, role and filters (empty, `--min-size`, exclude patterns), then runs the **pre-flight checks** and appends the block to the plan. Pre-flight is one "stat" (an existence + size + modified-time query, no content read) per path involved in a move: every keeper (once per group), every duplicate to move, and every destination (skipped while `duplicates\` doesn't exist yet). Confirms keepers and duplicates still match the scan and that destinations are free; failures become `SKIP` with a reason. With `--no-preflight` the bar is just "Building plan". | Pre-flight: **yes**, about one round trip per keeper + 1–2 per file to move. By far the longest part of `plan` (see below). The rest: no. |
| `execute` | Validating plan | Checks every plan row's `check` fingerprint and `role` (only `MOVE`↔`SKIP` edits allowed). | No |
| `execute` | **Moving duplicates** | Per file, on `--workers` threads: stat original + destination, keeper check (once per group), create the destination folder, rename, append the note line, append the journal line. | **Yes**: a few round trips per file. Time depends on file count, not size. |
| verify | Finding files in duplicates | Walks `<root>\duplicates` folder by folder. | Yes: one listing per folder |
| verify | Collecting file info | One stat per file found. | Yes |
| verify | **Hashing duplicates** | Reads every file in `duplicates` to compute its md5. Counts bytes, like duplicates_check.py. | **Yes: reads all the data.** Usually the longest step overall. |
| verify | Indexing files found | Fingerprints every found path for fast lookup. | No |
| verify | Comparing with the plan | Matches what was found against the journal by path + md5 and writes the report. | No |
| `undo` | Restoring files | Per journaled file, on `--workers` threads: stat both locations, rename back. Notes are then rewritten once per folder per block. | Yes |
| `undo` | Removing empty folders | Deletes mirrored folders in `duplicates` left empty. | Yes, one call per folder |
| `remaining` | (Reading journals) | Replays the journal and undo journal to know which files are currently moved. | No |
| `remaining` | Filtering raw listing | Streams the raw listing, dropping moved files (and anything under `duplicates\`). | No |
| `remaining` | Hashing location notes | Hashes each `original_file_location.txt` (a few KB each). | Yes, one small read per note |

**Why pre-flight is slow, and why it's there.** For Volume A (~60,000 groups,
~190,000 files to move) it's about 60,000 + 190,000 ≈ 250,000 stats over the
network on a first run (destinations aren't checked while `duplicates\`
doesn't exist yet). At 10–40 ms each across 8 threads, that's roughly 5–20
minutes. For Volume B (~230,000 groups, ~6M files to move) it's ~6M stats, i.e. hours.
Its purpose is that the plan you review already shows skips caused by files
that changed or disappeared since the scan. Safety doesn't depend on it:
`execute` repeats the same checks for each file just before moving it. So
for a large volume use `--no-preflight`, or raise `--workers` (e.g. 32).
Shares usually handle many parallel stats well.

**Time for `execute` on a large volume.** Each move takes a few network
round trips (~4), so 6 million moves/files at ~20 ms per file would take more than a day on one thread. With `--workers 16` it's a few hours. Verify then
re-reads all the moved data, like a duplicates_check.py scan of it.

### Resuming after a crash or kill

Just **run the same `execute` command again**. Each `MOVE` row is decided from
the files on disk, with the journal as the record:

| original exists | destination exists | what execute does |
|---|---|---|
| yes | no | not moved yet → checks and moves it |
| no | yes, same size/mtime as the scan | already moved (maybe killed before its bookkeeping) → adds the missing note/journal line → `MOVED_RESUMED` / `ALREADY_MOVED` |
| yes | yes | `CONFLICT`: nothing is touched |
| no | no | `MISSING` |

The per-file order is **rename → note line → journal line**. The journal is
flushed after every line, so it survives the process being killed, and
synced to disk every 2 s (syncing every line would serialize millions of
moves behind the disk). A kill at any point leaves a state the table above
recognises. Note and journal lines are never written twice. Ctrl+C stops
cleanly: moves already in progress finish, and the results so far are kept.

### Skip reasons

| reason | when |
|---|---|
| `ZERO_BYTE` | empty file (all empty files share one md5; they're usually placeholders). Always skipped. |
| `BELOW_MIN_SIZE` | smaller than `--min-size` |
| `EXCLUDED_PATTERN` | matches an exclude pattern (`detail` says which) |
| `KEEPER_MISSING` / `KEEPER_CHANGED` / `KEEPER_UNREADABLE` | keeper not found, or its size/mtime differ from the scan → whole group skipped |
| `DUPLICATE_MISSING` / `DUPLICATE_CHANGED` / `DUPLICATE_UNREADABLE` | same check for the file to move |
| `DESTINATION_EXISTS` | something is already at the destination path |
| `IN_DUPLICATES_FOLDER` | a file of the group is already inside `duplicates` |
| `NO_KEEPER_IN_SUMMARY` / `KEEPER_NOT_IN_LIST` / `NOT_A_DUPLICATE_GROUP` | duplicates and summary CSVs don't agree (e.g. from different runs) |

Keeper and duplicate checks run at `plan` time (so the review shows them,
unless `--no-preflight`) and **again** right before each move, so a plan
reviewed days earlier is still safe. mtimes are compared with a **±2 s tolerance**. Some file systems
(FAT/exFAT, some NAS shares) store mtime only to 2 s, and CSV round trips can
round it.

### Arguments

| Subcommand | Argument | Description |
|---|---|---|
| `plan` | `--duplicates-csv` | `<stem>_folder_duplicates.csv` from `duplicates_check.py --folder`. Required. |
| | `--summary-csv` | `<stem>_folder_summary.csv` from the same run. Required. |
| | `--root` | The scanned folder (`--folder` of duplicates_check.py). `duplicates` is created directly under it. Required. |
| | `--output` | Folder for the plan and, later, all reports. Can't be inside `<root>\duplicates`. Required. |
| | `--min-size` | Skip duplicates smaller than this: `500KB`, `1MB`, `2GB`, or bytes. Default: 0 (only empty files skipped). |
| | `--exclude` | Extra wildcard patterns never to move, matched case-insensitively against the file name or full path, e.g. `*.ini "*\Program Files\*"`. Always excluded: `desktop.ini`, `thumbs.db`, `*\.git\*`, `original_file_location.txt`. |
| | `--no-preflight` | Don't check the files on the volume while planning. Recommended for large volumes; `execute` still checks each file. |
| | `--workers` | Threads for the pre-flight file checks. Default: 8. |
| `execute` | `--plan` | The reviewed `move_plan_<timestamp>.csv`. Required. |
| | `--output` | Folder for journal/reports. Default: the plan's folder. |
| | `--no-verify` | Don't run verify afterwards. |
| | `--retry-failed` | Only retry the files whose latest result is `ERROR`, `MOVE_FAILED` or `MOVE_TIMEOUT`; results go to `_execute_results__movedFailed.csv`, and verify checks only those files (`_verify_report__movedFailed.csv`). |
| | `--workers` | Threads for moving files (and for hashing during verify with `--threading`). Default: 8. |
| | `--threading` | Hash concurrently during verify (as in duplicates_check.py). |
| `verify` | `--plan`, `--output`, `--threading`, `--workers` | As above. |
| `undo` | `--plan`, `--output` | As above. |
| | `--workers` | Threads for restoring files. Default: 8. |
| `remaining` | `--plan`, `--output` | As above. |
| | `--raw-csv` | duplicates_check.py's final `<stem>_raw.csv` of the same scan. Default: next to the plan's `--duplicates-csv`. |
| | `--workers` | Threads for hashing the location notes. Default: 8. |
| all | `--io-timeout` | Seconds to wait for one filesystem operation (stat, rename, …) before giving up on it. Default: 10. |

### Output files (in `--output`)

| file | written by | content |
|---|---|---|
| `move_plan_<ts>.csv` | plan | the plan (see above) |
| `move_plan_<ts>_settings.json` | plan | root, input CSVs, min size, exclude patterns |
| `move_plan_<ts>_journal.csv` | execute | one line per completed move, written live |
| `move_plan_<ts>_execute_results.csv` | execute | outcome per `MOVE` row |
| `move_plan_<ts>_execute_results__movedFailed.csv` | execute `--retry-failed` | outcome per retried row |
| `move_plan_<ts>_verify_report__movedFailed.csv` | execute `--retry-failed` | md5 verification of the retried rows only |
| `move_plan_<ts>_verify_report.csv` | execute / verify | md5 verification (missing, extra, metadata) |
| `move_plan_<ts>_undo_journal.csv`, `_undo_results.csv` | undo | restore record and outcome |
| `move_plan_<ts>_remaining_raw.csv` | remaining | raw listing of what stays after deleting `duplicates` |

### Design decisions

These were agreed before implementation; the reasons are kept here so they
aren't re-litigated.

- **Move, don't delete.** A quarantine folder makes every mistake reversible
  until you delete it yourself.
- **Keeper = oldest copy**, taken from `oldest_path` in duplicates_check.py's
  summary. Ties (equal mtime) go to the first path alphabetically, as in
  duplicates_check.py.
- **Mirrored folder structure** in `duplicates`: no name collisions, the
  origin is readable from the path, and undo is trivial.
- **One appended `original_file_location.txt` per source folder**, one line
  per moved file, rather than one file per moved file.
- **Keeper check before every move: exists by path/name, same size, same
  mtime** (±2 s), no re-hash. Fast, and catches a keeper that was deleted,
  replaced or modified since the scan. If it fails, the whole group stays put.
- **`os.rename`, not `shutil.move`.** On the same volume both are the same
  metadata-only operation: milliseconds per file whatever its size, no extra
  space, fully done or not done. Across volumes, `shutil.move` silently falls
  back to copy+delete (slow, needs space, can leave half-written files). On
  Windows `os.rename` also refuses to overwrite. So any cross-volume or
  name-clash case fails cleanly and is reported as `MOVE_FAILED`.
- **Dry run first, and execute takes the reviewed plan** (`--plan`), so what
  runs is exactly what was reviewed. Only `MOVE`↔`SKIP` edits are accepted.
- **Empty files always skipped**, plus `--min-size` and exclude patterns for
  small or location-dependent files (configs, `desktop.ini`, `.git`
  internals). Most of the space is in large files, and small files are the
  ones most likely to matter where they are.
- **Folder-mode input only.** The script refuses any path outside `--root`.
  Origin↔destination output (`between_summary`) lists a file and its *backup*
  and must never be "deduplicated".
- **Verification by expected path + md5**, not md5 alone.
- **Long paths are moved, and flagged.** Moving into `\duplicates\` adds 11
  characters, so a source path near Windows' 260-character limit (MAX_PATH)
  can end up over it. All filesystem calls use the `\\?\` extended-path form,
  which lifts that limit regardless of the system's `LongPathsEnabled`
  setting. Such rows have `long_path = True` in every report. Some tools
  (older Explorer, some backup software) may not open them. A failure is
  reported as `MOVE_FAILED`, for example a server-side path limit or a single
  path segment over 255 characters.
- **Resume from disk state + journal** (see *Resuming*). Live journal,
  flushed per line, synced to disk every 2 s.
- **Built for millions of files on an 8 GB machine.** No command loads a
  whole CSV. The duplicates CSV, plan, journal and raw listing are all
  streamed in blocks of 20,000 rows, small enough that every progress bar
  moves at least about once a second. This relies on duplicates_check.py writing
  the duplicates CSV sorted by md5, so each block holds whole groups; a file
  that isn't is refused. Sets of millions of paths (moved files, files found
  in `duplicates`) are kept as 8-byte fingerprints in sorted arrays instead
  of ~300-byte strings. The chance of two paths sharing a fingerprint is
  about 1 in a million at 6M paths. Moves and restores run on `--workers`
  threads, because one at a time would take days at that scale. The only
  step that holds a whole list is verify's scan of `duplicates`, the same
  scan duplicates_check.py does.
- **Reports go to `--output`**, never inside `duplicates` (they would
  otherwise show up as `EXTRA` in their own verification).
- **Not handled:** several plans moving into the same `duplicates` folder.
  Each verify only knows its own plan's journal, so files from an earlier
  plan show up as `EXTRA`.
- **Sync interaction not handled:** this targets an independent backup
  storage, not a root that is mirrored by a sync job.

### Notes

- **Long paths end to end:** duplicates_check.py uses the same `\\?\`
  handling (the shared `to_long_path` helper lives there), so files deeper
  than 260 characters are scanned, grouped and can be quarantined. Still
  check its `skipped_folders.csv`: folders can be skipped for other reasons
  (permissions, timeouts), and their files never reach this script.
- Every filesystem operation has a timeout (`--io-timeout`), as in
  duplicates_check.py, so an unreachable share or stuck handle is reported
  instead of hanging the run. A rename that times out is reported as
  `MOVE_TIMEOUT`. Re-running `execute` finds out whether it completed.
- The `duplicates` folder and the notes are ordinary files: deleting
  `duplicates` frees the space, and the notes stay as a record of where each
  file's surviving copy is.

---

## archive_old_files.py

Isolates old files so they can be moved from an active (expensive) storage to
a tape (cheaper) archive. Every file whose **modification time** falls in a
date range is moved into a `move_to_archive` folder under the same storage
root, with **exactly the same folder structure**. Files outside the range stay
where they are, untouched. The script never writes to tape: you copy
`move_to_archive` to tape yourself, check it against the manifest, and delete
it.

It works like [`quarantine_duplicates.py`](#quarantine_duplicatespy), with the
same safety mechanisms: a reviewable dry-run plan, `MOVE`↔`SKIP` as the only
allowed edit, a journal, resume after a crash, `--retry-failed`, verify by md5,
and `undo`.

### Workflow at a glance

```
archive_old_files.py plan     → (scans + hashes the storage, unless --raw-csv is given)
                              → archive_plan_<timestamp>.csv  (dry run, review it)
archive_old_files.py execute  → moves, notes, removes emptied folders, manifest, root index,
                                then verify and <plan>_remaining_raw.csv
   (copy move_to_archive to tape, check it with archive_manifest.csv, delete move_to_archive)
archive_old_files.py verify   → (optional) re-run the md5 check alone
archive_old_files.py undo     → (optional) put everything back
```

The archive step is self-contained: without `--raw-csv`, `plan` scans and
hashes the storage itself (the same scan as `duplicates_check.py --folder`).
With `--raw-csv` it reuses an existing listing and plans in minutes, so you
can try several date ranges or categories against one scan.

### What happens to the files and folders

For `R:\P\a.tif` (2010), `R:\P\b.tif` (2024) and `R:\P\old\c.tif` (2009),
with `--before 2016-01-01`:

- `R:\P\a.tif` → `R:\move_to_archive\P\a.tif`
- `R:\P\old\c.tif` → `R:\move_to_archive\P\old\c.tif`
- `R:\P\b.tif` stays.
- `R:\P\old` is now empty, so it is **removed** from the storage.
- `R:\P` keeps `b.tif` and gets an `archived_files_location.txt` note (see below).

**Folders emptied by the run are removed**, together with parents that become
empty. A folder counts as empty when only notes and system files are left in
it (`archived_files_location.txt`, `original_file_location.txt`,
`desktop.ini`, `thumbs.db`). Those then **move along with the folder**
(reason `FOLLOWS_FOLDER`), whatever their date, so no folder is left holding
only text files. This includes folders that held nothing but a quarantine note
before this run (every file in them was a quarantined duplicate). Folders that
were already completely empty before the run are left alone. Removal uses
`rmdir`, which only succeeds on a really empty folder, so a folder that still
holds anything is never removed (`FOLDER_KEPT`).

Two exceptions:

- **A `.git` folder only moves whole.** If only some of its files fall in the
  range, none of them move (`GIT_PARTIAL`), since moving part of a repository
  breaks it.
- **A quarantine that isn't finalized.** While `<root>\duplicates` exists,
  `quarantine_duplicates.py undo` is still possible, so folders holding a
  quarantine note are kept (`QUARANTINE_NOT_FINALIZED`). Delete `duplicates`
  first and plan again to include them.

`<root>\duplicates` and `<root>\move_to_archive` are never archived.

### Notes: where every file went

Every note line names the **archive label** (the tape) and the path inside
`move_to_archive`, and ends with the plan it came from. A user on the storage,
and a user restoring from tape, can both find where a file went.

| Where | File | What it holds |
|---|---|---|
| Every folder in `move_to_archive` | `archived_files_location.txt` | one line per file in that folder: original path, label, archive path, md5, size, modified |
| Every folder on the storage that lost files but still exists | `archived_files_location.txt` | the same lines, plus one `removed_folder` line per subfolder that was removed |
| `move_to_archive\archive_manifest.csv` | manifest, goes to tape | every file of the batch, one row each with all its metadata |
| `<root>\_archived_to_tape.csv` | root index, stays on the storage | every file and removed folder of every batch (cumulative) |
| Quarantine notes (`original_file_location.txt`) | a `keeper_archived` line | added when the keeper they point at is archived, so the pointer doesn't go stale |

```
moved_file: a.tif | original_path: R:\P\a.tif | archive_label: LTO-0042 | archive_path: move_to_archive\P\a.tif | md5: a3f… | size_bytes: 1234 | modified: 2010-06-01 10:00:00 | archived_on: 2026-10-20 10:15:00 | plan: archive_plan_20261020_101500
removed_folder: R:\P\old | reason: all files archived | archive_label: LTO-0042 | archive_path: move_to_archive\P\old | archived_on: 2026-10-20 10:15:00 | plan: archive_plan_20261020_101500
keeper_archived: R:\A\scan.tif | archive_label: LTO-0042 | archive_path: move_to_archive\A\scan.tif | archived_on: 2026-10-20 10:15:00 | plan: archive_plan_20261020_101500
```

**Notes move with their folder, also in later runs.** A note is never
selected by its date. It only moves when its folder ends up holding nothing
but notes and system files. It then goes to the archive side and is merged
with the note this run wrote there, so the copy on tape carries the folder's
whole history. Example over two runs:

1. **Run 1 (`--archive-label LTO-0001`):** `F\x.tif` and `F\y.tif` are
   archived. `F\z.tif` (recent) stays, so `F` keeps a note with two lines
   pointing to `LTO-0001`.
2. **Run 2, years later (`LTO-0007`):** `z.tif` is archived, leaving `F`
   with only its note. The note moves to `move_to_archive\F\`, the `z.tif`
   line is added, and `F` is removed. Its parent gets a `removed_folder: F |
   archive_label: LTO-0007` line.

On tape `LTO-0007`, `F\archived_files_location.txt` then reads:

```
moved_file: x.tif | … | archive_label: LTO-0001 | archive_path: move_to_archive\F\x.tif | …
moved_file: y.tif | … | archive_label: LTO-0001 | archive_path: move_to_archive\F\y.tif | …
moved_file: z.tif | … | archive_label: LTO-0007 | archive_path: move_to_archive\F\z.tif | …
```

Someone restoring `F` from `LTO-0007` learns that the rest of the folder is
on `LTO-0001`. Deleting the note instead would lose that link.

### Requirements

- Python 3.10+
- `pandas`, `numpy`, `tqdm`

**`archive_old_files.py` imports helpers from `duplicates_check.py` (the
scan) and `quarantine_duplicates.py` (journal, CSV streaming, file checks),
so all three files must stay in the same folder.** You don't have to *run*
either of the other two. If one is missing, the script stops at startup and
names it.

### Input: which listing to use

Without `--raw-csv`, `plan` scans and hashes `--root` itself and writes
`archive_scan_<timestamp>_raw.csv` into `--output`. With `--raw-csv`, always
pass the **newest listing of the storage**:

| What happened last on the storage | Listing to pass |
|---|---|
| nothing, or only a scan | `duplicates_check.py`'s `<stem>_raw.csv`, or nothing (let `plan` scan) |
| a quarantine was executed | `quarantine_duplicates.py`'s `<plan>_remaining_raw.csv` |
| an earlier archive was executed | this script's `<plan>_remaining_raw.csv` |

An outdated listing can't do damage: files that no longer exist are skipped
(`FILE_MISSING`), and `plan` warns when that's more than a few percent. The
listing's paths must be under `--root` in the same spelling (`R:\` and
`\\server\share\` count as different), as in `quarantine_duplicates.py`.

If the scan of `plan` is interrupted during hashing, the pre-hash listing it
already wrote can be resumed with `duplicates_check.py --folder <root> --excel
<x> --resume-from <listing>`, and its final `_raw.csv` passed as `--raw-csv`.

**Categories** (`--categories`, required) are worked out from the listing's
own md5 column, so every listing above works:

| Category | Meaning |
|---|---|
| `all` | every file in the date range (the only choice for a listing without md5) |
| `unique` | its md5 occurs once on the storage |
| `keeper` | the oldest copy of a duplicate group (ties: first path alphabetically, as in duplicates_check.py) |
| `duplicate` | the other copies |
| `unreadable` | no md5 (couldn't be hashed) |

Categories describe the storage as listed. After a quarantine, a former keeper
has no copies left, so it counts as `unique`. Archiving `keeper` while its
`duplicate` copies stay active is allowed; their quarantine notes get the
`keeper_archived` line.

### Usage

Run from the repository root.

#### 1. Plan (dry run, moves nothing)

```powershell
python archive_old_files.py plan `
    --root   "R:\" `
    --output "C:\reports\archive" `
    --before 2016-01-01 `
    --categories unique keeper `
    --archive-label LTO-0042
```

Add `--raw-csv <listing>` to skip the scan. Use `--from 2010-01-01 --to
2016-01-01` instead of `--before` for a window.

**Which days the dates include.** A date means midnight at the start of that
day, so:

| Option | Selects | Example |
|---|---|---|
| `--before D` | mtime earlier than D 00:00:00. **D itself is not included.** | `--before 2021-01-01` → up to 2020-12-31 23:59:59 |
| `--from A --to B` | from A 00:00:00 (included) up to B 00:00:00 (not included) | `--from 2010-01-01 --to 2021-01-01` → 2010 to 2020 |

Consecutive windows (`--to 2021-01-01`, then `--from 2021-01-01`) never
overlap or leave a gap. To include a day, give the next one (`--before
2021-01-02` includes 1 January 2021), or add a time (`--before "2021-01-01
12:00"`).

Dates are **UTC**, like the mtimes in every listing, so the cutoff is
midnight UTC: 01:00 local time in Central European winter, 02:00 in summer.
A file saved on 1 January 2021 at 00:30 local winter time has the UTC mtime
2020-12-31 23:30, so `--before 2021-01-01` *does* select it. This only
matters for files modified within an hour or two of midnight on the cutoff
date.

The summary shows the files to move per year, per category and per top
folder (two levels under the root), the skips, the folders that will be
removed, and the quarantine notes to update. Re-run `plan --raw-csv …` with
other dates for free.

The plan has one row per file in the date range (plus files with an
unknown mtime), then one row per note/system file following its folder, per
quarantine note to update, and per folder to remove:

| column | meaning |
|---|---|
| `action` | `MOVE` or `SKIP` |
| `reason`, `detail` | why (`DATE_RANGE`, `FOLLOWS_FOLDER`, `FOLDER_EMPTIED`, `KEEPER_ARCHIVED`, or a skip reason) |
| `kind` | `file`, `system`, `note`, `folder`, `keeper_note` |
| `role` | `candidate` (selected by date), `follow`, `folder`, `keeper_note`, or `blocked` (may not be switched to `MOVE`) |
| `category`, `copies` | from the listing's md5s |
| `md5`, `size_bytes`, `size_gb`, `mtime`, `modified` | from the listing (`modified` is UTC) |
| `original_path`, `archive_path` | where it is now, where it goes |
| `extra` | for `keeper_note` rows: the archived keeper |
| `long_path`, `check` | as in quarantine_duplicates.py |

**Editing the plan:** only `action` between `MOVE` and `SKIP`, as in
quarantine_duplicates.py. Use R or a text editor, not Excel. Switching a
file to `SKIP` keeps its folder on the storage (`FOLDER_KEPT`).

**Excluding folders (`--exclude`).** List one or more patterns after
`--exclude`, separated by spaces, each in quotes:

```powershell
--exclude "R:\projects\instrument_A\*" "R:\shared\keep_online\*"
```

A pattern must match a file's **whole** full path or its whole file name
(case-insensitive), so a folder pattern **must end in `\*`**:

| Pattern | Matches the files inside the folder? |
|---|---|
| `"R:\projects\instrument_A\*"` | yes: that folder and everything under it |
| `"*\instrument_A\*"` | yes: every folder named `instrument_A`, at any depth |
| `"R:\projects\instrument_A"` or `"R:\projects\instrument_A\"` | **no**: nothing is excluded, and no warning is given |

Use the same spelling as the listing (`R:\…` or `\\server\share\…`). Check
the plan summary: excluded files show up as `EXCLUDED_PATTERN` in the
"Skipped" line. They can still be switched to `MOVE` by hand, they keep their
folders (and the parent folders) on the storage, and they still count for the
duplicate categories. `--exclude` doesn't shorten the scan: to skip folders in
the scan too, scan with `duplicates_check.py --folder … --excluded <folders>`
and pass its `_raw.csv` as `--raw-csv` (their files then don't count for the
categories either).

`--no-preflight` skips checking every file on the storage while planning
(much faster on a large volume); `execute` checks each file anyway.

**Profiling a plan in R:
[`archive_plan.R`](R_plots/archive_plan.R).** Plots and tables of what the
plan would do, in number of files *and* in storage space. Set `plan_path` and
`output_folder` in the configuration block. `plan_path` can end in
`archive_plan_*.csv` instead of the exact file name: the plan in that folder
is found by its name (the journal and report files next to it are ignored),
and with several plans the newest is used (the listing is found through the
plan's settings file; set `raw_path` only if it has moved), run section
**0. Setup** once, then any section on its own (RStudio: Ctrl+Alt+T). Each
section's last line exports its plot into `output_folder`:

| Section | What it shows | Exported as |
|---|---|---|
| 1. Files vs space | Moves / in range but category not selected / in range but skipped / outside the date range, as a share of the volume's files and space | `archive_volume_share.png` |
| 2. By year | Files and space per year last modified, from `first_year` to `last_year` (set at the top of the section; default 1995 to the current year): moving (selected by date, or following an emptied folder) vs staying. The chart for choosing a cutoff. Dates outside that range (often impossible ones such as 1065 or 9785, from wrong clocks or tools) and unknown dates aren't drawn; the caption gives their totals | `archive_by_year.png` |
| 3. By category | unique / keeper / duplicate / unreadable in the date range, moving vs staying | `archive_by_category.png` |
| 4. Top folders | The folders (two levels under the root) with the most space and files to move, and the number of folders to remove | `archive_top_folders.png` |
| 5. Skips | Files in the range that stay, per reason | `archive_skips.png`, `archive_skips.csv` |

Sections 1 and 2 read the plan's listing (only once, kept as totals per
year), so they take longest on a large volume.

#### 2. Execute

```powershell
python archive_old_files.py execute --plan "C:\reports\archive\archive_plan_20261020_101500.csv" --workers 16
```

1. Validates the plan, refuses if `move_to_archive` already holds another
   batch, and creates it.
2. **Moves the files** (`os.rename`, so metadata-only on the same volume),
   after checking each still has the listing's size and mtime (±2 s). Writes
   the note line on both sides, then the journal line.
3. **Updates the quarantine notes** of archived keepers.
4. **Removes the emptied folders**, deepest first: moves their notes and
   system files along, removes the folder, and writes the `removed_folder`
   line in its parent.
5. Writes `archive_manifest.csv` and updates `<root>\_archived_to_tape.csv`.
6. Runs **verify** (unless `--no-verify`) and writes
   **`<plan>_remaining_raw.csv`** (unless `--no-remaining`).

Re-run the same command to resume after a crash or Ctrl+C. Retry only the
files that failed (`ERROR`, `MOVE_FAILED`, `MOVE_TIMEOUT`) with
`--retry-failed`, as in quarantine_duplicates.py; notes and emptied folders
are always processed again. For `MOVE_FAILED` with *Access is denied*, see
[`change_folder_permissions.py`](#change_folder_permissionspy).

**`<plan>_remaining_raw.csv`** is the listing of what stays on the storage:
the plan's listing minus the moved files, with the notes this run wrote
re-hashed. It's written in minutes, without rescanning. It matches a real
rescan, and it's the input for the next archive or quarantine plan and for
[`storage_report_by_year.R`](R_plots/storage_report_by_year.R).

#### 3. Copy to tape

Copy `<root>\move_to_archive` to tape, check the copy against
`archive_manifest.csv` (one row per file, with md5), then delete
`move_to_archive`. The next `plan` refuses to start while `move_to_archive`
still holds files.

#### 4. Verify (runs automatically after execute)

```powershell
python archive_old_files.py verify --plan "C:\reports\archive\archive_plan_20261020_101500.csv" --threading
```

Re-hashes everything under `move_to_archive` and matches it to the journal by
expected path and md5. Notes are only checked for presence, since their
content grows by design. Writes `<plan>_verify_report.csv` with the
quarantine_duplicates.py statuses (`OK`, `MISSING`, `MD5_MISMATCH`,
`UNREADABLE`, `EXTRA`, `MOVED_NOT_JOURNALED`, `NOT_MOVED`, `SKIPPED_<reason>`,
`UNSCANNED_FOLDER`), plus `SIZE_MISMATCH` (listing without md5),
`FOLDER_REMOVED` and `FOLDER_KEPT`.

**Reporting on an execution in R:
[`archive_execution.R`](R_plots/archive_execution.R).** Plots of what
`execute`, verify and `undo` actually did, from the files they write next to
the plan. Set `plan_path` (the executed plan; `archive_plan_*.csv` works here
too) and `output_folder`, run
**0. Setup** once (it reads nothing big), then any section on its own:

| Section | What it shows | From | Exported as |
|---|---|---|---|
| 1. Execute outcome | Moved / stayed with a kept folder / skipped by a safety check / problem, per status, in files and space | `_execute_results.csv` (last run) | `archive_execution_outcome.png` |
| 2. Progress over time | Cumulative files and space moved, all runs; restores by `undo` count down | `_journal.csv` + `_undo_journal.csv` | `archive_execution_timeline.png` |
| 3. Verification | `OK`, `NOT_MOVED`, `SKIPPED_*` and problem statuses, in files and space | `_verify_report.csv` | `archive_execution_verification.png` |
| 4. Problems | What execute couldn't do (e.g. `MOVE_FAILED: PermissionError`), by type and by folder | `_execute_results.csv` | `archive_execution_problems.png`, `archive_execution_problems.csv` |
| 5. Folders | Emptied folders removed vs kept, why they were kept, and the quarantine notes updated | `_execute_results.csv` | `archive_execution_folders.png`, `archive_execution_kept_folders.csv` |
| 6. Moved per folder | What is in `move_to_archive` now, per original folder (net of restores) | `_journal.csv` + `_undo_journal.csv` | `archive_execution_space_by_folder.png` |

For the storage as a whole, before and after:
[`storage_report_by_year.R`](R_plots/storage_report_by_year.R) works
unchanged on the scan's `archive_scan_<ts>_raw.csv` (before) and on
`<plan>_remaining_raw.csv` (after); its storage comparison section can compare
the two directly.

#### 5. Undo (before deleting `move_to_archive`)

```powershell
python archive_old_files.py undo --plan "C:\reports\archive\archive_plan_20261020_101500.csv"
```

Moves every journaled file back, recreates the removed folders, removes every
line this plan added (in notes on both sides, in quarantine notes and in the
root index; a note left with only its header is deleted), removes the
manifest and the emptied `move_to_archive`, and rewrites
`<plan>_remaining_raw.csv`. It's safe to re-run. Tested against a rescan: data
files are back with identical paths, md5s and mtimes.

### Skip reasons and statuses

| reason / status | when |
|---|---|
| `CATEGORY_NOT_SELECTED` | in the date range but not in `--categories` (may be switched to `MOVE`) |
| `EXCLUDED_PATTERN` | matches `--exclude` |
| `MTIME_UNKNOWN` | the scan couldn't stat the file (its mtime reads as 1970-01-01), so its age is unknown |
| `GIT_PARTIAL` | in a `.git` folder whose files aren't all selected |
| `FILE_MISSING` / `FILE_CHANGED` / `FILE_UNREADABLE` | the file is gone or differs from the listing (pre-flight, or `SKIPPED_…` in execute) |
| `FOLDER_KEPT` (execute) | the folder still holds files, a follower is set to `SKIP`, or the quarantine isn't finalized; `detail` says which |
| `KEEPER_NOT_MOVED` (execute) | the keeper of a quarantine note wasn't moved, so the note wasn't changed |
| `CONFLICT`, `MISSING`, `MOVED_RESUMED`, `ALREADY_MOVED` | as in quarantine_duplicates.py's *Resuming* table |

### Arguments

| Subcommand | Argument | Description |
|---|---|---|
| `plan` | `--root` | The storage folder. `move_to_archive` is created directly under it. Required. |
| | `--output` | Folder for the plan and all reports. Not inside `move_to_archive`. Required. |
| | `--raw-csv` | The newest raw listing of `--root` (see *Input*). Default: scan and hash `--root`. |
| | `--before DATE` | Files modified before DATE 00:00 UTC (DATE itself is not included), e.g. `2016-01-01`. See *Which days the dates include*. |
| | `--from DATE --to DATE` | Files modified from DATE (included) to DATE (excluded). Instead of `--before`. |
| | `--categories` | `all`, or one or more of `unique`, `keeper`, `duplicate`, `unreadable`. Required. |
| | `--archive-label` | Name of the tape or batch (e.g. `LTO-0042`), written in every note, the manifest and the index. Required. |
| | `--exclude` | One or more wildcard patterns never to move, matched against the whole file name or full path. A folder needs a trailing `\*`, e.g. `--exclude "R:\A\*" "R:\B\*"`. See *Excluding folders*. |
| | `--no-preflight` | Don't check the files on the storage while planning. |
| | `--threading`, `--workers` | Hash concurrently during the scan; threads for scan, pre-flight and reading quarantine notes. Default: 8. |
| `execute` | `--plan` | The reviewed `archive_plan_<timestamp>.csv`. Required. |
| | `--output` | Folder for journal/reports. Default: the plan's folder. |
| | `--no-verify` / `--no-remaining` | Skip verify / the remaining listing afterwards. |
| | `--retry-failed` | Only retry the files whose latest result is `ERROR`, `MOVE_FAILED` or `MOVE_TIMEOUT`. |
| | `--workers`, `--threading` | Threads for moving (and for hashing during verify with `--threading`). Default: 8. |
| `verify` | `--plan`, `--output`, `--threading`, `--workers` | As above. |
| `undo` | `--plan`, `--output`, `--no-remaining`, `--workers` | As above. |
| all | `--io-timeout` | Seconds to wait for one filesystem operation before giving up on it. Default: 10. |

### Output files

| file | where | written by | content |
|---|---|---|---|
| `archive_scan_<ts>_raw.csv`, `_skipped_folders.csv` | `--output` | plan (without `--raw-csv`) | the scan's raw listing and unscannable folders |
| `archive_plan_<ts>.csv`, `_settings.json` | `--output` | plan | the plan; root, listing, dates, categories, label |
| `archive_plan_<ts>_journal.csv`, `_undo_journal.csv` | `--output` | execute / undo | every move, removed folder and restore, written live |
| `archive_plan_<ts>_execute_results.csv` (`__movedFailed`) | `--output` | execute | outcome per row |
| `archive_plan_<ts>_verify_report.csv` (`__movedFailed`) | `--output` | execute / verify | md5 verification |
| `archive_plan_<ts>_remaining_raw.csv` | `--output` | execute / undo | raw listing of what stays on the storage |
| `archive_plan_<ts>_undo_results.csv` | `--output` | undo | outcome per restore |
| `archive_manifest.csv` | `<root>\move_to_archive` | execute | every file of the batch with all its metadata (goes to tape) |
| `_archived_to_tape.csv` | `<root>` | execute / undo | cumulative index of everything archived from this storage |
| `archived_files_location.txt` | folders on both sides | execute | the notes (see above) |

### Generating test data

[`support_scripts\generate_archive_test_data.py`](support_scripts/generate_archive_test_data.py)
builds a storage with one of each case: an all-old project (removed whole, with
a recent `desktop.ini` that follows it), a mixed project (old file moves, a
subfolder is removed, the folder stays with a note), a duplicate group (old
keeper, old duplicate, recent duplicate), a folder holding only a quarantine
note, a partly old and a fully old `.git` repository, and a file with an
unknown mtime. It prints the commands to run and what to expect.

```powershell
python support_scripts\generate_archive_test_data.py --path "C:\path\to\test_area" --num-files 2 [--with-duplicates-folder] [--clean]
```

`--with-duplicates-folder` also creates `<storage>\duplicates`, to see folders
with a quarantine note kept (`QUARANTINE_NOT_FINALIZED`).

### Design decisions

- **Move, don't copy or delete.** `os.rename` on the same volume is instant
  and needs no extra space; nothing is lost until you delete
  `move_to_archive` yourself, and `undo` reverses everything until then.
- **Per file, by modification time**, as in elimination_file_candidates.py,
  with the same mirrored structure as quarantine's `duplicates` folder.
- **The plan step is the preview**: no separate analysis script. Re-planning
  from a listing is fast, so trying other cutoffs costs nothing.
- **Emptied folders are removed, their notes and system files go with
  them**, so the storage isn't left with folders holding only text files. The
  history travels to tape instead of being deleted.
- **Notes on both sides, the manifest and the root index** each answer a
  different question: "what left this folder", "where did this tape folder
  come from", "check the tape copy", and "find anything archived from this
  storage".
- **The remaining listing is written automatically**, so the next plan, a
  quarantine or the R reports always start from the current state without a
  multi-day rescan.
- **Categories from the listing itself**, not from duplicates_check.py's
  summary, so they work with any listing, including remaining listings.
- **One batch at a time** in `move_to_archive`: a new plan refuses to start
  while it still holds files.
- **Same scale design as quarantine_duplicates.py**: CSVs streamed in blocks
  of 20,000 rows, path sets as 8-byte fingerprints, work on `--workers`
  threads, timeouts on every filesystem call, long paths through `\\?\`.

---

## change_folder_permissions.py

A standalone helper, not part of the pipeline: run it by hand when needed.
Typical case: `quarantine_duplicates.py execute` reports `MOVE_FAILED` with
`[WinError 5] Access is denied` for a folder and its subfolders, because the
account can read there but not delete or rename. It has three steps, each
run on its own:

| Step | What it does |
|---|---|
| `grant <folder>` | Gives the account **Modify** on the folder and everything under it: one inheritable entry on the folder, plus one on every item that doesn't inherit it (folders with inheritance switched off). Items whose permissions can't be read or changed go to the errors file. `--take-ownership`: for those (*Access is denied*), take ownership for the account and try again; the original owner is recorded. |
| `lockdown <folder>` | Every user or group with write access keeps **read only**: on the folder (which stops inheriting from above, so write access given higher up is reduced too) and on every item with entries of its own. The account (with Modify), SYSTEM and Administrators keep their rights. No *Everyone* entry is added and no Deny entries are used. `--exclude NAME_OR_PATH` (repeatable) leaves a folder and its contents as they are, e.g. where instruments still write. |
| `restore <changes.csv>` | Puts back the permissions from before a `grant` or `lockdown` run, from the changes file it wrote. To undo both, restore the lockdown first, then the grant. |

```powershell
# 1. Modify for you on the folder that failed, then retry the moves
python change_folder_permissions.py grant "\\server\share\data\problem_folder" --dry-run
python change_folder_permissions.py grant "\\server\share\data\problem_folder"
python quarantine_duplicates.py execute --plan "C:\reports\quarantine\move_plan_20261006_121052.csv" --retry-failed

# 2. Later, once you decide: read only for everyone else
python change_folder_permissions.py lockdown "\\server\share\data" --exclude "\\server\share\data\instrument_backups"

# Undo a step
python change_folder_permissions.py restore permissions_lockdown_20261016_101500_changes.csv
```

**The account.** By default it's you. `--account` takes `DOMAIN\user`,
`user@domain`, an email address (looked up in Active Directory if it's not
the sign-in name) or a SID; `--account-from PATH` uses the owner of a file or
folder. On a network share, the file server may know an account by another
ID than your PC: on a PC signed in with a cloud (Entra ID) account, this PC
sees an `S-1-12-...` ID, while a file server in the on-premises domain knows
the same person as `S-1-5-21-...`, and a grant to the first wouldn't apply
there. The script warns about this; use `--account-from` with a folder you
created on that share (e.g. the `duplicates` folder made by `execute`) to
get the server's ID.

**Output files** (in `--output`, default: the current folder; never inside
the folder being changed):

| file | content |
|---|---|
| `permissions_<step>_<timestamp>_changes.csv` | every item changed: `path`, `type`, `owner_before`, `owner_after`, `dacl_before`, `dacl_after` (permissions as SDDL). Written before each change, so even a killed run can be restored. `restore` reads this file. |
| `permissions_<step>_<timestamp>_errors.csv` | every item that couldn't be listed, read or changed: `timestamp`, `step`, `path`, `operation`, `error_code`, `message` (the Windows error) |

`--dry-run` writes the changes file as `..._dryrun_changes.csv` and changes
nothing. Each step asks for confirmation (`--yes` skips it).

**Notes**

- Permissions are read and written with the Windows security API (the one
  `icacls` uses), so it works the same whatever the Windows language, and
  long paths (`\\?\`) work.
- After `grant`/`lockdown` on the folder itself, Windows passes the change
  on to everything that inherits from it. On a large tree that takes a
  while, without progress shown.
- Changing permissions needs *Change permissions* on the item or being its
  owner, and taking ownership needs admin rights on the file server. What
  still fails stays in the errors file. A retention or archive lock on the
  storage can't be lifted with permissions; that's for the storage admin.
- The share's own permissions (on the share, not on its files) also apply,
  and the stricter of the two wins. If the share gives you read only, no
  file permission helps.
- An item with no permissions set at all (everyone has full access) isn't
  changed by `lockdown`; it's listed in the errors file to set by hand.
- Owners of files can always change those files' permissions, so a user
  could give themselves write access back on files they own.
