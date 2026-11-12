# Missing-files check: the files an archive plan selected but couldn't find
# (FILE_MISSING), or that execute then found gone (MISSING,
# SKIPPED_FILE_MISSING) - are they really gone duplicates (moved to
# <root>\duplicates by quarantine_duplicates_V2.py, with their keeper on tape or
# still on the storage), or did something else remove them?
#
# Why they happen: an archive plan only knows its listing. When a listing
# (e.g. the previous <plan>_remaining_raw.csv) was made before a quarantine,
# it still lists the quarantined duplicates; once their keeper has gone to
# tape, the plan recomputes the groups and takes the oldest copy left in the
# listing - a quarantined duplicate - as the new keeper, selects it, and finds
# it gone.
#
# RECORDS ONLY: this script reads the record files (plans, quarantine journal,
# tape index, listings) and checks NOTHING on the storage, so it runs on any
# machine. To confirm its verdicts on the storage itself, run, where the
# storage is reachable:
#   python support_scripts\check_missing_on_disk.py --paths "<output_folder>\archive_missing_paths.csv"
# with the path list section 2 writes (the keepers, the quarantined copies and
# the other copies of each missing file's content). Then set disk_result to
# its output and run section 3: the records' verdicts next to what was found
# on the storage.
#
# How to use: set the configuration below, run section "0. Setup" once (it
# reads the inputs, each filtered to the missing files, so it's quick even on
# a large volume), then sections 1 and 2 in any order; section 3 once the
# Python script has run. The last lines of each section export it into
# output_folder. In RStudio: Ctrl+Alt+T runs the current section.
#
# Inputs (set them in the configuration block below):
#   REQUIRED  archive plan(s)        archive_plan_<date>_<time>.csv: their FILE_MISSING rows,
#                                    plus MISSING / SKIPPED_FILE_MISSING from the
#                                    <plan>_execute_results.csv next to each (if executed)
#   REQUIRED  _archived_to_tape.csv  what is on tape: tape_index, or <root>\_archived_to_tape.csv
#                                    with the root from each plan's settings
#   OPTIONAL  quarantine journal(s)  move_plan_<date>_<time>_journal.csv (+ its
#                                    _undo_journal.csv, found automatically): which files
#                                    the quarantine moved, to where, and their keeper
#   OPTIONAL  duplicates raw         the duplicates_check_V2.py _raw.csv the quarantine was
#                                    planned from, to tell whether a file was the keeper
#                                    (oldest copy) back then
#   OPTIONAL  plan listing(s)        listing_path, or (default) the raw_csv in each plan's
#                                    settings: the other copies of each content (if it
#                                    can't be found, the check notes it and runs without it)
#
# Install once: install.packages(c("readr", "dplyr", "tidyr", "ggplot2", "scales", "forcats", "jsonlite", "reactable", "htmlwidgets"))

# ---- 0. Setup (run once) -----------------------------------------------------

library(readr)
library(dplyr)
library(tidyr)
library(ggplot2)
library(scales)
library(forcats)
library(jsonlite)
library(reactable)
library(htmlwidgets)

# Configuration. Same pattern as the other R scripts: the block for the
# volume you want goes last (later assignments win).
#
# REQUIRED
#   plan_path      One or more archive plans. A wildcard (archive_plan_*.csv)
#                  takes EVERY plan in that folder (not only the newest, as in
#                  archive_plan.R), e.g. all the year-by-year batches; plans in
#                  several folders: c(r"(...)", r"(...)").
#   tape_index     The _archived_to_tape.csv. Must exist: the script stops if
#                  it isn't found. NA = <root>\_archived_to_tape.csv, with the
#                  root recorded in each plan's settings; give a path if the
#                  storage isn't reachable from this machine (a copy), or is
#                  now reached by another drive letter or path. (It's created
#                  by the first execute, so a plan that was never executed
#                  can't be checked yet.)
#   output_folder  Where the chart, table and CSVs are saved.
#
# OPTIONAL (NA = not used)
#   quarantine_journal  One or more quarantine journals, wildcards allowed
#                  (move_plan_*_journal.csv; the _undo_journal.csv next to each
#                  is found automatically). Strongly recommended if there was a
#                  quarantine: without it, no file counts as quarantined, so
#                  gone duplicates show as CHECK instead of OK.
#   duplicates_raw The duplicates_check_V2.py _raw.csv the quarantine was planned
#                  from. Without it, the "Keeper in the duplicates run" column
#                  stays empty and the "was the duplicates run's keeper"
#                  verdict can't appear.
#   listing_path   The listing(s) the plans were made from (the raw_csv in each
#                  plan's _settings.json, e.g. the previous _remaining_raw.csv
#                  or the scan's _raw.csv), wildcards allowed. NA = the
#                  raw_csv recorded in each plan's settings; give it when that
#                  path doesn't exist on this machine (Setup then prints
#                  "NOTE: listing not found"). Without a listing, the other
#                  copies of each content aren't known.

##Volume A
plan_path          <- r"(C:\path\to\storage_analysis\Volume_A\archive_dry\archive_plan_*.csv)"  # REQUIRED
tape_index         <- NA                                                                       # REQUIRED (NA = <root>\_archived_to_tape.csv)
output_folder      <- r"(C:\path\to\storage_analysis\Volume_A\archive_dry)"                    # REQUIRED
quarantine_journal <- r"(C:\path\to\storage_analysis\Volume_A\quarantine\move_plan_*_journal.csv)"  # OPTIONAL (NA = none)
duplicates_raw     <- NA                                                                       # OPTIONAL (NA = none)
listing_path       <- NA                                                                       # OPTIONAL (NA = raw_csv from the plans' settings)

chunk_rows <- 500000    # rows read at a time (memory vs. speed)
max_paths  <- 20        # other copies per content put in the path list for the disk check (oldest first)
max_named  <- 10        # other copies named per file in the table

# Wildcards: every file in that folder matching both the pattern and `name_regex`.
expand_paths <- function(paths, name_regex) {
  paths <- paths[!is.na(paths)]
  found <- as.character(unlist(lapply(paths, function(p) {
    if (!grepl("*", p, fixed = TRUE)) return(p)
    files <- list.files(dirname(p), pattern = glob2rx(basename(p)), full.names = TRUE)
    files[grepl(name_regex, basename(files))]
  })))
  missing <- found[!file.exists(found)]
  if (length(missing)) stop(sprintf("Not found: %s", paste(missing, collapse = ", ")))
  sort(unique(found))
}
plans <- expand_paths(plan_path, "^archive_plan_[0-9]{8}_[0-9]{6}\\.csv$")
if (length(plans) == 0) stop(sprintf("No archive_plan_<date>_<time>.csv matches %s", paste(plan_path, collapse = ", ")))
journals <- expand_paths(quarantine_journal, "_journal\\.csv$")
journals <- journals[!grepl("_undo_journal\\.csv$", journals)]
cat(sprintf("Plans: %d | quarantine journals: %d | duplicates raw: %s | tape index: %s\n",
            length(plans), length(journals), if (is.na(duplicates_raw)) "none" else basename(duplicates_raw),
            if (all(is.na(tape_index))) "<root>\\_archived_to_tape.csv (from the plans' settings)" else paste(tape_index, collapse = ", ")))

# Plot style: validated default palette (light mode), status colours (always
# shown with their label), ink for all text, hairline grid.
surface <- "#fcfcfb"
ink     <- "#0b0b0b"
ink2    <- "#52514e"
muted   <- "#898781"
gridcol <- "#e1e0d9"
class_colours <- c("OK" = "#0ca30c", "CHECK" = "#fab219", "ALERT" = "#d03b3b")

theme_check <- theme_minimal(base_size = 11) +
  theme(
    plot.background    = element_rect(fill = surface, colour = NA),
    panel.background   = element_rect(fill = surface, colour = NA),
    panel.grid.minor   = element_blank(),
    panel.grid.major.y = element_blank(),
    panel.grid.major.x = element_line(colour = gridcol, linewidth = 0.3),
    axis.text          = element_text(colour = muted),
    plot.title         = element_text(colour = ink, face = "bold"),
    plot.subtitle      = element_text(colour = ink2),
    plot.caption       = element_text(colour = muted, hjust = 0),
    legend.text        = element_text(colour = ink2),
    legend.title       = element_blank(),
    legend.position    = "top",
    legend.justification = "left"
  )
fmt_files <- label_comma()

# Save a plot as <folder>/<name>.png (folder: output_folder unless given).
export_plot <- function(plot, name, width = 10, height = 6, folder = output_folder) {
  dir.create(folder, showWarnings = FALSE, recursive = TRUE)
  path <- file.path(folder, paste0(name, ".png"))
  ggsave(path, plot, width = width, height = height, dpi = 150, bg = surface)
  cat(sprintf("Saved: %s\n", path))
}

# Save an interactive table as <folder>/<name>.html (folder: output_folder
# unless given). A single file needs pandoc (RStudio comes with it); without
# it, the page's scripts go in a <name>_files folder next to it - keep the two
# together.
export_table <- function(widget, name, folder = output_folder) {
  dir.create(folder, showWarnings = FALSE, recursive = TRUE)
  path <- file.path(folder, paste0(name, ".html"))
  single <- tryCatch({
    saveWidget(widget, path, selfcontained = TRUE, title = name, background = surface)
    TRUE
  }, error = function(e) FALSE)
  if (!single) saveWidget(widget, path, selfcontained = FALSE, title = name, background = surface)
  cat(sprintf("Saved: %s%s\n", path, if (single) "" else sprintf(" (+ folder %s_files: pandoc not found)", name)))
}

# Interactive tables: monospace path columns, and the note under a table's title.
mono <- list(fontFamily = "Consolas, monospace", fontSize = "12px")
path_col <- function(name) colDef(name = name, minWidth = 420, style = mono)
note_style <- sprintf("color:%s;font-family:Segoe UI,sans-serif;margin:0 0 10px", ink2)

# Comparison key for a Windows path (case-insensitive, one separator).
key <- function(paths) tolower(gsub("/", "\\", paths, fixed = TRUE))
# A date as text, or NA when unknown ("" or 1970, an unstat'able file).
known_date <- function(stamps) {
  stamps <- as.character(stamps)
  if_else(is.na(stamps) | stamps == "" | startsWith(stamps, "1970"), NA_character_, stamps)
}
# The earliest of some dates (as text), or NA if there are none.
earliest <- function(stamps) { stamps <- sort(stamps[!is.na(stamps)]); if (length(stamps)) stamps[1] else NA_character_ }
# Where a file of the storage would be if a quarantine moved it: <root>\duplicates\<its path under the root>.
in_duplicates_path <- function(paths, roots) paste0(roots, "\\duplicates\\", substring(paths, nchar(roots) + 2))

# A CSV read in chunks, keeping only the rows `keep` returns (always a tibble
# with the requested columns, even when nothing matches).
read_filtered <- function(path, keep, columns) {
  empty <- as_tibble(setNames(lapply(names(columns), function(c) character()), names(columns)))
  if (!file.exists(path)) return(empty)
  cat(sprintf("Reading %s ...\n", basename(path)))
  rows <- read_csv_chunked(path, DataFrameCallback$new(function(x, pos) keep(x)), chunk_size = chunk_rows,
                           na = character(), col_types = do.call(cols_only, columns))
  if (is.null(rows) || nrow(rows) == 0) empty else rows
}
text_cols <- function(...) setNames(lapply(c(...), function(c) col_character()), c(...))

# 1) The missing files of each plan: its FILE_MISSING rows, plus what execute found gone.
missing_of_plan <- function(plan) {
  settings <- fromJSON(sub("\\.csv$", "_settings.json", plan))
  gone <- read_filtered(sub("\\.csv$", "_execute_results.csv", plan),
                        function(x) x %>% filter(kind == "file", status %in% c("MISSING", "SKIPPED_FILE_MISSING")),
                        text_cols("status", "kind", "original_path")) %>%
    transmute(k = key(original_path), exec_status = status)
  read_filtered(plan, function(x) {
    x %>% filter(kind == "file", (action == "SKIP" & reason == "FILE_MISSING") | key(original_path) %in% gone$k)
  }, text_cols("action", "reason", "kind", "category", "copies", "md5", "size_bytes", "modified", "original_path")) %>%
    mutate(k = key(original_path)) %>%
    left_join(gone, by = "k") %>%
    transmute(k, original_path, md5 = tolower(md5), category, copies, modified, size_bytes,
              size_mb = suppressWarnings(as.numeric(size_bytes)) / 2^20,
              source = if_else(reason == "FILE_MISSING", "plan: FILE_MISSING", paste0("execute: ", exec_status)),
              plan = basename(plan), label = settings$archive_label,
              root = sub("[\\\\/]+$", "", settings$root), listing = settings$raw_csv)
}
# A path missing in several chained plans counts once (latest plan; all labels listed).
missing <- bind_rows(lapply(plans, missing_of_plan)) %>%
  arrange(plan) %>%
  group_by(k) %>%
  mutate(labels = paste(unique(label), collapse = ", ")) %>%
  slice_tail(n = 1) %>%
  ungroup() %>%
  mutate(valid_md5 = grepl("^[0-9a-f]{32}$", md5))
md5s <- unique(missing$md5[missing$valid_md5])
cat(sprintf("Missing files: %s (in %d plan(s))\n", fmt_files(nrow(missing)), length(plans)))
if (nrow(missing) == 0) stop("No FILE_MISSING / MISSING / SKIPPED_FILE_MISSING files in these plans: nothing to check.")

# 2) The quarantine: the latest event of each missing path (a move, or a restore by undo).
journal_events <- function(path, statuses, moved) {
  read_filtered(path, function(x) x %>% filter(status %in% statuses, key(original_path) %in% missing$k),
                text_cols("timestamp", "status", "original_path", "duplicates_path", "keeper_path")) %>%
    mutate(moved = moved)
}
quarantine <- bind_rows(lapply(journals, function(j) bind_rows(
  journal_events(j, c("MOVED", "MOVED_RESUMED"), TRUE),
  journal_events(sub("_journal\\.csv$", "_undo_journal.csv", j), "RESTORED", FALSE)
)))
quarantine <- if (nrow(quarantine) == 0) tibble(k = character(), duplicates_path = character(), keeper_path = character()) else
  quarantine %>%
    mutate(k = key(original_path), order = row_number()) %>%
    arrange(k, timestamp, order) %>%
    group_by(k) %>% slice_tail(n = 1) %>% ungroup() %>%
    filter(moved) %>%
    select(k, duplicates_path, keeper_path)
keeper_keys <- key(quarantine$keeper_path)

# 3) The tape: every archived copy of these contents, and the keepers' own rows.
tape_indexes <- if (all(is.na(tape_index))) unique(file.path(missing$root, "_archived_to_tape.csv")) else tape_index
absent <- tape_indexes[!file.exists(tape_indexes)]
if (length(absent)) stop(sprintf(paste0(
  "Tape index not found: %s. Set tape_index to where it is; it's created by the first execute, ",
  "so if no plan has been executed yet, there is nothing on tape to check against. If the storage isn't ",
  "reachable from this machine, give a copy of it."), paste(absent, collapse = ", ")))
tape <- bind_rows(lapply(tape_indexes, function(index) {
  read_filtered(index,
                function(x) x %>% filter(kind == "file", tolower(md5) %in% md5s | key(original_path) %in% keeper_keys),
                text_cols("archive_label", "kind", "original_path", "archive_path", "md5", "modified"))
})) %>% mutate(md5 = tolower(md5), k = key(original_path))

# 4) The listings: every copy of these contents in the plans' listings, with
# what the listing says about it (nothing is checked on the storage).
# Each listing goes with the root of its plans (listing_path: every listing given, with every root).
listing_pairs <- if (all(is.na(listing_path))) unique(paste(missing$listing, missing$root, sep = "|")) else
  as.vector(outer(expand_paths(listing_path, "\\.csv$"), unique(missing$root), paste, sep = "|"))
listed <- bind_rows(lapply(listing_pairs, function(pair) {
  listing <- sub("\\|.*$", "", pair)
  root    <- sub("^.*\\|", "", pair)
  if (!file.exists(listing)) cat(sprintf(
    "NOTE: listing not found, its copies aren't known: %s (set listing_path to where it is on this machine)\n", listing))
  read_filtered(listing, function(x) x %>% filter(tolower(md5) %in% md5s), text_cols("path", "mtime", "md5")) %>%
    mutate(root = root,
           area = case_when(startsWith(key(path), key(paste0(root, "\\duplicates\\")))      ~ "duplicates",
                            startsWith(key(path), key(paste0(root, "\\move_to_archive\\"))) ~ "move_to_archive",
                            TRUE ~ ""))
})) %>%
  mutate(md5 = tolower(md5), k = key(path)) %>%
  distinct(k, .keep_all = TRUE) %>%
  arrange(md5, is.na(known_date(mtime)), mtime, path) %>%   # oldest first
  mutate(status = case_when(k %in% missing$k          ~ "missing too",
                            area == "duplicates"      ~ "listed in duplicates\\",
                            area == "move_to_archive" ~ "listed in move_to_archive\\",
                            TRUE                      ~ "listed on the storage"),
         alive  = status != "missing too")

# Per missing file: what the listings say about the OTHER copies of its
# content (a breakdown, and their paths with their status, oldest first).
others <- missing %>%
  select(k, md5) %>%
  inner_join(listed %>% select(md5, other_k = k, path, status, alive, area, root, mtime),
             by = "md5", relationship = "many-to-many") %>%
  filter(other_k != k)
copies_now <- others %>%
  group_by(k) %>%
  summarise(
    n_others     = n(),
    also_missing = sum(status == "missing too"),
    n_storage    = sum(status == "listed on the storage"),
    n_duplicates = sum(status == "listed in duplicates\\"),
    n_archive    = sum(status == "listed in move_to_archive\\"),
    n_alive      = sum(alive),
    storage_date = earliest(known_date(mtime[status == "listed on the storage"])),
    other_copies = paste(c(head(sprintf("%s [%s]", path, status), max_named),
                           if (n() > max_named) sprintf("... +%d more", n() - max_named)), collapse = " | "),
    .groups = "drop") %>%
  mutate(copies_breakdown = paste0(
           sprintf("others listed %d: on the storage %d, also missing %d", n_others, n_storage, also_missing),
           if_else(n_duplicates > 0, sprintf(", in duplicates\\ %d", n_duplicates), ""),
           if_else(n_archive > 0, sprintf(", in move_to_archive\\ %d", n_archive), "")))
live_keys <- listed$k[listed$alive]

# 5) Optional: the duplicates run - was the file there, and was it its oldest copy?
then <- if (is.na(duplicates_raw)) NULL else
  read_filtered(duplicates_raw, function(x) x %>% filter(tolower(md5) %in% md5s), text_cols("path", "mtime", "md5")) %>%
    mutate(md5 = tolower(md5), k = key(path))
then_keeper <- if (is.null(then)) NULL else
  then %>% group_by(md5) %>%
    arrange(is.na(known_date(mtime)), mtime, path, .by_group = TRUE) %>%  # oldest; ties: first path alphabetically
    slice_head(n = 1) %>% ungroup() %>% select(md5, keeper_then = k)

# 6) One verdict per missing file, from the records.
tape_copy <- tape %>%
  group_by(md5) %>% arrange(is.na(known_date(modified)), modified, .by_group = TRUE) %>% slice_head(n = 1) %>% ungroup() %>%
  transmute(md5, tape_copy = archive_path, tape_label = archive_label, tape_date = known_date(modified))

checked <- missing %>%
  left_join(quarantine, by = "k") %>%
  left_join(tape_copy, by = "md5") %>%
  left_join(copies_now, by = "k") %>%
  mutate(
    quarantined      = !is.na(duplicates_path),
    n_alive          = coalesce(n_alive, 0L),
    copies_breakdown = coalesce(copies_breakdown, "no other copy listed"),
    other_copies     = coalesce(other_copies, ""),
    keeper_tape      = if_else(quarantined, tape$archive_label[match(key(keeper_path), tape$k)], NA_character_),
    keeper_listed    = quarantined & key(coalesce(keeper_path, "")) %in% live_keys,
    keeper_where     = case_when(!quarantined ~ "", !is.na(keeper_tape) ~ paste0("tape: ", keeper_tape),
                                 keeper_listed ~ "in the listing", TRUE ~ "not in the records"),
    record_copy      = !is.na(tape_copy) | n_alive > 0,
    this_date        = known_date(modified),
    # Where the oldest copy of the content is now (ties: tape first).
    oldest_copy      = case_when(
      is.na(this_date) & is.na(tape_date) & is.na(storage_date) ~ "unknown dates",
      !is.na(tape_date) & tape_date <= pmin(coalesce(this_date, "9"), coalesce(storage_date, "9")) ~ "on tape",
      !is.na(this_date) & this_date <= coalesce(storage_date, "9") ~ "this file",
      TRUE ~ "on the storage")
  )
if (!is.null(then)) {
  checked <- checked %>%
    left_join(then_keeper, by = "md5") %>%
    mutate(keeper_then = case_when(!k %in% then$k ~ "not in that run", k == keeper_then ~ "yes", TRUE ~ "no"))
} else {
  checked <- checked %>% mutate(keeper_then = "")
}
checked <- checked %>%
  mutate(verdict = case_when(
    !valid_md5                              ~ "ALERT: no md5, can't be checked",
    quarantined & !is.na(keeper_tape)       ~ "OK: quarantined, its keeper is on tape",
    quarantined & keeper_listed             ~ "OK: quarantined, its keeper is in the listing",
    quarantined & record_copy               ~ "CHECK: quarantined, its keeper isn't in the records, but another copy is",
    quarantined                             ~ "CHECK: quarantined, its quarantined copy is the only one in the records",
    keeper_then == "yes" & record_copy      ~ "CHECK: was the duplicates run's keeper, not in the quarantine journal; another copy is in the records",
    !is.na(tape_copy)                       ~ "CHECK: not in the quarantine journal; its content is on tape",
    n_alive > 0                             ~ "CHECK: not in the quarantine journal; another copy is in the listing",
    TRUE                                    ~ "ALERT: no other copy in the records"
  ),
  class = sub(":.*$", "", verdict))
print(checked %>% count(class, verdict, name = "files") %>% arrange(match(class, names(class_colours))), n = Inf)

# The paths to confirm on the storage, for support_scripts\check_missing_on_disk.py:
# per missing file, the file itself (is it back?), its quarantined copy and
# keeper (journal), where a quarantine would have put it (if not journaled),
# and up to max_paths other copies on the storage (oldest first), each with
# where a quarantine would have put it too.
paths_to_check <- bind_rows(
  checked %>% transmute(k, role = "the missing file itself", path = original_path),
  checked %>% filter(quarantined) %>% transmute(k, role = "quarantined copy (journal)", path = duplicates_path),
  checked %>% filter(quarantined) %>% transmute(k, role = "keeper (journal)", path = keeper_path),
  checked %>% filter(!quarantined) %>% transmute(k, role = "quarantined copy (if not journaled)",
                                                 path = in_duplicates_path(original_path, root)),
  others %>% filter(alive) %>% group_by(k) %>% slice_head(n = max_paths) %>% ungroup() %>%
    { bind_rows(transmute(., k, role = if_else(area == "duplicates", "other copy, listed in duplicates\\", "other copy"), path = path),
                filter(., area == "") %>% transmute(k, role = "other copy, if quarantined", path = in_duplicates_path(path, root))) }
) %>%
  filter(!is.na(path), path != "") %>%
  inner_join(checked %>% transmute(k, missing_path = original_path, md5, size_bytes,
                                   record_verdict = verdict, tape_label = coalesce(tape_label, "")), by = "k") %>%
  select(missing_path, md5, size_bytes, record_verdict, tape_label, role, path)
paths_file <- file.path(output_folder, "archive_missing_paths.csv")
disk_command <- sprintf('python support_scripts\\check_missing_on_disk.py --paths "%s"', paths_file)
cat(sprintf("Paths to confirm on the storage: %s (for %s missing file(s))\n",
            fmt_files(nrow(paths_to_check)), fmt_files(nrow(checked))))

# ---- 1. Verdict per missing file -----------------------------------------------

verdicts <- checked %>%
  count(class, verdict, name = "files") %>%
  mutate(class = factor(class, levels = names(class_colours))) %>%
  arrange(desc(as.integer(class)), files) %>%   # ggplot draws the first level at the bottom: OK on top
  mutate(verdict = fct_inorder(verdict))
p_verdicts <- ggplot(verdicts, aes(x = files, y = verdict, fill = class)) +
  geom_col(width = 0.6, show.legend = TRUE) +
  geom_text(aes(label = fmt_files(files)), hjust = -0.15, colour = ink, size = 3.4) +
  scale_fill_manual(values = class_colours, drop = FALSE) +
  scale_x_continuous(labels = label_comma(), breaks = function(l) unique(floor(pretty(l))),
                     expand = expansion(mult = c(0, 0.15))) +
  coord_cartesian(clip = "off") +
  labs(title = "Files the archive plans couldn't find: what the records say",
       subtitle = sprintf("%s missing file(s) in %d plan(s) | quarantine journals: %d | %s\nFrom the record files only; confirm on the storage with check_missing_on_disk.py",
                          fmt_files(nrow(checked)), length(plans), length(journals),
                          if (is.null(then)) "no duplicates raw" else "with the duplicates run"),
       caption = paste0("OK: a gone duplicate, its keeper is safe | CHECK: a copy of the content is in the records, ",
                        "but not where expected\nALERT: look into it"),
       x = NULL, y = NULL) +
  theme_check
print(p_verdicts)
export_plot(p_verdicts, "archive_missing_check", width = 12, height = 4)

# ---- 2. The missing files, per verdict (interactive table + CSVs) ------------------
# Click a verdict to see its files. Each file's row says where it went
# (quarantined to), where its keeper is, and where the other copies are,
# according to the records. Also writes the path list for the disk check.

table_rows <- checked %>%
  add_count(verdict, name = "n") %>%
  mutate(group = sprintf("%s: %s file(s)", verdict, fmt_files(n)),
         rank  = match(class, c("ALERT", "CHECK", "OK"))) %>%
  arrange(rank, desc(n), verdict, original_path) %>%
  transmute(group, file = sub("^.*\\\\", "", original_path),
            folder = sub("\\\\[^\\\\]*$", "", substring(original_path, nchar(root) + 2)),
            size_mb, modified, category, copies, copies_breakdown, other_copies, md5, original_path, source, labels,
            duplicates_path, keeper_path, keeper_where, tape_copy, tape_label, oldest_copy, keeper_then, verdict)

t_missing <- reactable(
  table_rows, groupBy = "group",
  columns = list(
    group            = colDef(name = "Verdict (from the records)", minWidth = 420, grouped = JS("function(cell) { return cell.value }")),
    file             = colDef(name = "File", minWidth = 200),
    folder           = colDef(name = "Folder (under the root)", minWidth = 260, style = mono),
    size_mb          = colDef(name = "Size (MB)", minWidth = 90, format = colFormat(digits = 1, separators = TRUE)),
    modified         = colDef(name = "Modified (UTC)", minWidth = 150),
    category         = colDef(name = "Category (in the plan)", minWidth = 110),
    copies           = colDef(name = "Copies (in the plan's listing)", minWidth = 110),
    copies_breakdown = colDef(name = "The other copies, per the listings", minWidth = 430),
    other_copies     = colDef(name = "Other copies [listing status], oldest first", minWidth = 600, style = mono),
    md5              = colDef(name = "md5", minWidth = 150, style = mono),
    original_path    = path_col("Original path"),
    source           = colDef(name = "Found missing by", minWidth = 190),
    labels           = colDef(name = "Archive label(s)", minWidth = 130),
    duplicates_path  = path_col("Quarantined to (journal)"),
    keeper_path      = path_col("Its keeper (journal)"),
    keeper_where     = colDef(name = "Keeper is", minWidth = 130),
    tape_copy        = path_col("Oldest copy on tape (archive path)"),
    tape_label       = colDef(name = "Tape label", minWidth = 110),
    oldest_copy      = colDef(name = "Oldest copy is", minWidth = 120),
    keeper_then      = colDef(name = "Keeper in the duplicates run", minWidth = 130),
    verdict          = colDef(name = "Verdict", minWidth = 300)
  ),
  filterable = TRUE, searchable = TRUE, resizable = TRUE, striped = TRUE, compact = TRUE, wrap = FALSE,
  defaultPageSize = 25, showPageSizeOptions = TRUE, pageSizeOptions = c(25, 100, 500),
  theme = reactableTheme(color = ink, backgroundColor = surface, borderColor = gridcol,
                         stripedColor = "#f3f2ee", highlightColor = "#ebeae4",
                         headerStyle = list(color = ink2), style = list(fontFamily = "Segoe UI, sans-serif"))
) %>% prependContent(
  htmltools::tags$h3("Files the archive plans couldn't find, per verdict",
                     style = sprintf("color:%s;font-family:Segoe UI,sans-serif;margin:8px 0 2px", ink)),
  htmltools::tags$p(sprintf("Click a verdict to see its files. Plans: %s", paste(basename(plans), collapse = ", ")),
                    style = note_style),
  htmltools::tags$p(
    htmltools::tags$b("These verdicts come from the record files only; nothing was checked on the storage. "),
    "To confirm them (are the keepers and the other copies really there?), run this where the storage is reachable:",
    style = note_style),
  htmltools::tags$pre(disk_command, style = sprintf(
    "color:%s;background:#f3f2ee;border:1px solid %s;padding:6px 10px;font-size:12px;white-space:pre-wrap", ink, gridcol))
)
print(t_missing)
export_table(t_missing, "archive_missing_check")
write_csv(table_rows %>% select(-group), file.path(output_folder, "archive_missing_check.csv"))
write_csv(paths_to_check, paths_file)
cat(sprintf("Saved: %s\n\nTo confirm the verdicts on the storage, run where it is reachable:\n  %s\n", paths_file, disk_command))
cat("Then set disk_result to the folder with its results and run section 3.\n")

# ---- 3. On the storage: the result of check_missing_on_disk.py (chart + interactive table) ----
# Set disk_result to the folder with archive_missing_disk.csv and
# archive_missing_disk_paths.csv, written by
# support_scripts\check_missing_on_disk.py from section 2's path list on a
# machine that reaches the storage. Shows how the records' verdicts (sections
# 1-2) hold up on the storage, and which copies were found where. Its chart
# and table are saved into that same folder, next to the Python results.
#   disk_result    The FOLDER with the results of
#                  support_scripts\check_missing_on_disk.py
#                  (archive_missing_disk.csv and archive_missing_disk_paths.csv,
#                  with those names; by default it writes them next to the
#                  path list, i.e. into output_folder). Only section 3
#                  uses it: run sections 0-2, then the Python script on the
#                  storage, then set this and run section 3.

disk_result        <- NA                                                                       # OPTIONAL: a folder (NA = section 3 skipped)

if (all(is.na(disk_result))) stop("Set disk_result to the folder with the results of check_missing_on_disk.py first (see section 2).")
disk_folder <- if (grepl("\\.csv$", disk_result, ignore.case = TRUE)) dirname(disk_result) else disk_result  # a file: its folder
disk_file   <- file.path(disk_folder, "archive_missing_disk.csv")
disk_paths  <- file.path(disk_folder, "archive_missing_disk_paths.csv")
if (!file.exists(disk_file)) stop(sprintf("Not found: %s (is disk_result the folder with the results of check_missing_on_disk.py?)", disk_file))
if (!file.exists(disk_paths)) cat(sprintf("NOTE: %s not found: the 'Copies found on the storage' column stays empty.\n", disk_paths))
disk_written <- format(file.mtime(disk_file), "%Y-%m-%d %H:%M")
disk <- read_csv(disk_file, col_types = cols(.default = col_character()), na = character()) %>%
  mutate(k = key(missing_path))
cat(sprintf("Disk check: %s file(s) from %s (written %s)\n", fmt_files(nrow(disk)), basename(disk_file), disk_written))
not_in_records <- sum(!disk$k %in% checked$k)
if (not_in_records > 0) cat(sprintf(
  "NOTE: %s file(s) of the disk check aren't among the missing files now: it comes from an older run of section 2.\n",
  fmt_files(not_in_records)))

# Per missing file, the copies found on the storage (from the paths file, if it's there).
found_paths <- if (!file.exists(disk_paths)) tibble(k = character(), found_on_storage = character()) else
  read_csv(disk_paths, col_types = cols(.default = col_character()), na = character()) %>%
    filter(state == "exists", same_content != "no") %>%
    group_by(k = key(missing_path)) %>%
    summarise(found_on_storage = paste(sprintf("%s [%s]", path, role), collapse = " | "), .groups = "drop")

disk_levels  <- c("OK", "CHECK", "INCOMPLETE", "ALERT")
disk_colours <- c("OK" = "#0ca30c", "CHECK" = "#fab219", "INCOMPLETE" = "#c3c2b7", "ALERT" = "#d03b3b")
disk_groups  <- c("OK"         = "OK: content found on tape or on the storage",
                  "CHECK"      = "CHECK: only in duplicates\\ (it goes when the quarantine is finalized)",
                  "INCOMPLETE" = "INCOMPLETE: no copy found, but some paths couldn't be checked",
                  "ALERT"      = "ALERT: no copy found on tape or on the storage")
compared <- checked %>%
  inner_join(disk %>% select(k, disk_class = class, disk_verdict, file_back, keeper, quarantined_copy,
                             other_copies_on_storage, other_copies_in_duplicates, paths_with_errors), by = "k") %>%
  left_join(found_paths, by = "k") %>%
  mutate(found_on_storage = coalesce(found_on_storage, ""),
         disk_class = factor(disk_class, levels = disk_levels))
print(compared %>% count(record = verdict, storage = disk_class, name = "files"), n = Inf)

# One bar per verdict from the records (same order as section 1), split by the verdict on the storage.
record_order <- checked %>% count(class, verdict) %>%
  arrange(desc(match(class, names(class_colours))), n) %>% pull(verdict)
p_disk <- ggplot(compared %>% count(verdict, disk_class, name = "files") %>%
                   mutate(verdict = factor(verdict, levels = record_order)),
                 aes(x = files, y = verdict, fill = disk_class)) +
  geom_col(width = 0.6, position = position_stack(reverse = TRUE), show.legend = TRUE) +
  scale_fill_manual(values = disk_colours, drop = FALSE) +
  scale_x_continuous(labels = label_comma(), breaks = function(l) unique(floor(pretty(l))),
                     expand = expansion(mult = c(0, 0.05))) +
  labs(title = "Records vs storage: what check_missing_on_disk.py found",
       subtitle = sprintf("%s missing file(s): one bar per verdict from the records (section 1), coloured by the verdict on the storage",
                          fmt_files(nrow(compared))),
       caption = sprintf(paste0("OK: content on tape or on the storage | CHECK: only in duplicates\\ | ",
                                "INCOMPLETE: paths couldn't be checked | ALERT: no copy found\n%s, written %s"),
                         basename(disk_file), disk_written),
       x = NULL, y = NULL) +
  theme_check
print(p_disk)
export_plot(p_disk, "archive_missing_disk", width = 12, height = 4, folder = disk_folder)

disk_rows <- compared %>%
  add_count(disk_class, name = "n") %>%
  mutate(group = sprintf("%s: %s file(s)", disk_groups[as.character(disk_class)], fmt_files(n))) %>%
  arrange(match(as.character(disk_class), c("ALERT", "INCOMPLETE", "CHECK", "OK")), original_path) %>%
  transmute(group, file = sub("^.*\\\\", "", original_path),
            folder = sub("\\\\[^\\\\]*$", "", substring(original_path, nchar(root) + 2)),
            size_mb, modified, disk_verdict, record_verdict = verdict, tape_label, file_back, keeper, quarantined_copy,
            other_copies_on_storage, other_copies_in_duplicates, paths_with_errors, found_on_storage,
            keeper_path, duplicates_path, md5, original_path)
t_disk <- reactable(
  disk_rows, groupBy = "group",
  columns = list(
    group                      = colDef(name = "On the storage", minWidth = 420, grouped = JS("function(cell) { return cell.value }")),
    file                       = colDef(name = "File", minWidth = 200),
    folder                     = colDef(name = "Folder (under the root)", minWidth = 260, style = mono),
    size_mb                    = colDef(name = "Size (MB)", minWidth = 90, format = colFormat(digits = 1, separators = TRUE)),
    modified                   = colDef(name = "Modified (UTC)", minWidth = 150),
    disk_verdict               = colDef(name = "Verdict on the storage", minWidth = 420),
    record_verdict             = colDef(name = "Verdict from the records (section 2)", minWidth = 360),
    tape_label                 = colDef(name = "Tape label", minWidth = 110),
    file_back                  = colDef(name = "File itself", minWidth = 90),
    keeper                     = colDef(name = "Keeper (journal)", minWidth = 110),
    quarantined_copy           = colDef(name = "Quarantined copy", minWidth = 170),
    other_copies_on_storage    = colDef(name = "Other copies on the storage (found of listed)", minWidth = 150),
    other_copies_in_duplicates = colDef(name = "Other copies in duplicates\\", minWidth = 130),
    paths_with_errors          = colDef(name = "Paths that couldn't be checked", minWidth = 120),
    found_on_storage           = colDef(name = "Copies found on the storage [role]", minWidth = 600, style = mono),
    keeper_path                = path_col("Its keeper (journal)"),
    duplicates_path            = path_col("Quarantined to (journal)"),
    md5                        = colDef(name = "md5", minWidth = 150, style = mono),
    original_path              = path_col("Original path")
  ),
  filterable = TRUE, searchable = TRUE, resizable = TRUE, striped = TRUE, compact = TRUE, wrap = FALSE,
  defaultPageSize = 25, showPageSizeOptions = TRUE, pageSizeOptions = c(25, 100, 500),
  theme = reactableTheme(color = ink, backgroundColor = surface, borderColor = gridcol,
                         stripedColor = "#f3f2ee", highlightColor = "#ebeae4",
                         headerStyle = list(color = ink2), style = list(fontFamily = "Segoe UI, sans-serif"))
) %>% prependContent(
  htmltools::tags$h3("Files the archive plans couldn't find: checked on the storage",
                     style = sprintf("color:%s;font-family:Segoe UI,sans-serif;margin:8px 0 2px", ink)),
  htmltools::tags$p(sprintf(paste0("Click a verdict to see its files. From %s (written %s) by check_missing_on_disk.py; ",
                                   "the verdicts from the records alone are in archive_missing_check.html."),
                            basename(disk_file), disk_written),
                    style = note_style)
)
print(t_disk)
export_table(t_disk, "archive_missing_disk", folder = disk_folder)
