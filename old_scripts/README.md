# old_scripts

The original pipeline scripts, as they were before their shared helpers moved
into `diogenes_common.py` (the `_V2` scripts in the repository root). Kept
unchanged for reference and for runs started with them.

These scripts import helpers from each other (`quarantine_duplicates.py` and
`elimination_file_candidates.py` from `duplicates_check.py`;
`archive_old_files.py` from both), so all four must stay together in one
folder. They don't use `diogenes_common.py`.

Plans, settings, journals and notes are the same in both versions, so a plan
made here can be executed, verified or undone by the `_V2` script, and the
other way round.
