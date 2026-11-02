# Missing-files check: the files an archive plan selected but couldn't find
# (FILE_MISSING), or that execute then found gone (MISSING,
# SKIPPED_FILE_MISSING) - are they really gone duplicates (moved to
# <root>\duplicates by quarantine_duplicates.py, with their keeper on tape or
# still on the storage), or did something else remove them?
#
# Why they happen: an archive plan only knows its listing. When a listing
# (e.g. the previous <plan>_remaining_raw.csv) was made before a quarantine,
# it still lists the quarantined duplicates; once their keeper has gone to
# tape, the plan recomputes the groups and takes the oldest copy left in the
# listing - a quarantined duplicate - as the new keeper, selects it, and finds
# it gone.
#
# How to use: set the configuration below, run section "0. Setup" once (it
# reads the inputs, each filtered to the missing files, so it's quick even on
# a large volume), then sections 1 and 2 in any order. The last line of each
# section exports it into output_folder. In RStudio: Ctrl+Alt+T runs the
# current section.
#
# Inputs:
#   archive plan(s)        archive_plan_<date>_<time>.csv: their FILE_MISSING rows,
#                          plus MISSING / SKIPPED_FILE_MISSING from the
#                          <plan>_execute_results.csv next to each (if executed)
#   quarantine journal(s)  move_plan_<date>_<time>_journal.csv (+ its
#                          _undo_journal.csv, found automatically): which files
#                          the quarantine moved, to where, and their keeper
#   <root>\_archived_to_tape.csv   what is on tape (found from each plan's settings)
#   each plan's listing    the raw_csv in its settings: the other copies on the storage
#   duplicates raw         optional: the duplicates_check.py _raw.csv the
#                          quarantine was planned from, to tell whether a file
#                          was the keeper (oldest copy) back then
# Disk: only the few paths involved are checked (file.exists): the keepers,
# the quarantined copies, and up to max_checks other copies per content.
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
# plan_path: one or more plans. A wildcard (archive_plan_*.csv) takes EVERY
#   plan in that folder (not only the newest, as in archive_plan.R), e.g. all
#   the year-by-year batches; several folders: c(r"(...)", r"(...)").
# quarantine_journal: one or more quarantine journals, wildcards allowed
#   (move_plan_*_journal.csv); NA if there was no quarantine.
# duplicates_raw: the duplicates_check.py _raw.csv, or NA.

##Volume A
plan_path          <- r"(C:\path\to\storage_analysis\Volume_A\archive_dry\archive_plan_*.csv)"
quarantine_journal <- r"(C:\path\to\storage_analysis\Volume_A\quarantine\move_plan_*_journal.csv)"
duplicates_raw     <- NA
output_folder      <- r"(C:\path\to\storage_analysis\Volume_A\archive_dry)"

##Volume B
plan_path          <- r"(C:\path\to\storage_analysis\Volume_B\archive_dry\archive_plan_*.csv)"
quarantine_journal <- r"(C:\path\to\storage_analysis\Volume_B\quarantine\move_plan_*_journal.csv)"
duplicates_raw     <- NA
output_folder      <- r"(C:\path\to\storage_analysis\Volume_B\archive_dry)"

chunk_rows <- 500000    # rows read at a time (memory vs. speed)
max_checks <- 20        # other storage copies checked on disk per content (oldest first)

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
cat(sprintf("Plans: %d | quarantine journals: %d | duplicates raw: %s\n",
            length(plans), length(journals), if (is.na(duplicates_raw)) "none" else basename(duplicates_raw)))

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

export_plot <- function(plot, name, width = 10, height = 6) {
  dir.create(output_folder, showWarnings = FALSE, recursive = TRUE)
  path <- file.path(output_folder, paste0(name, ".png"))
  ggsave(path, plot, width = width, height = height, dpi = 150, bg = surface)
  cat(sprintf("Saved: %s\n", path))
}

# Save an interactive table as <output_folder>/<name>.html. A single file
# needs pandoc (RStudio comes with it); without it, the page's scripts go in
# a <name>_files folder next to it - keep the two together.
export_table <- function(widget, name) {
  dir.create(output_folder, showWarnings = FALSE, recursive = TRUE)
  path <- file.path(output_folder, paste0(name, ".html"))
  single <- tryCatch({
    saveWidget(widget, path, selfcontained = TRUE, title = name, background = surface)
    TRUE
  }, error = function(e) FALSE)
  if (!single) saveWidget(widget, path, selfcontained = FALSE, title = name, background = surface)
  cat(sprintf("Saved: %s%s\n", path, if (single) "" else sprintf(" (+ folder %s_files: pandoc not found)", name)))
}

# Comparison key for a Windows path (case-insensitive, one separator).
key <- function(paths) tolower(gsub("/", "\\", paths, fixed = TRUE))
# A date as text, or NA when unknown ("" or 1970, an unstat'able file).
known_date <- function(stamps) ifelse(is.na(stamps) | stamps == "" | startsWith(stamps, "1970"), NA_character_, stamps)

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
    transmute(k, original_path, md5 = tolower(md5), category, copies, modified,
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
tape <- bind_rows(lapply(unique(missing$root), function(root) {
  read_filtered(file.path(root, "_archived_to_tape.csv"),
                function(x) x %>% filter(kind == "file", tolower(md5) %in% md5s | key(original_path) %in% keeper_keys),
                text_cols("archive_label", "kind", "original_path", "archive_path", "md5", "modified"))
})) %>% mutate(md5 = tolower(md5), k = key(original_path))

# 4) The storage: other copies of these contents in each plan's listing (not
# the missing files themselves, nor anything in duplicates\ or move_to_archive\).
storage <- bind_rows(lapply(unique(paste(missing$listing, missing$root, sep = "|")), function(pair) {
  listing <- sub("\\|.*$", "", pair)
  root    <- key(sub("^.*\\|", "", pair))
  read_filtered(listing, function(x) x %>% filter(tolower(md5) %in% md5s), text_cols("path", "mtime", "md5")) %>%
    filter(!startsWith(key(path), paste0(root, "\\duplicates\\")),
           !startsWith(key(path), paste0(root, "\\move_to_archive\\")))
})) %>%
  mutate(md5 = tolower(md5), k = key(path)) %>%
  distinct(k, .keep_all = TRUE) %>%
  filter(!k %in% missing$k)
# Checked on disk, oldest first, up to max_checks per content.
storage <- storage %>%
  group_by(md5) %>% arrange(is.na(known_date(mtime)), mtime, path, .by_group = TRUE) %>%
  slice_head(n = max_checks) %>% ungroup() %>%
  mutate(exists = file.exists(path))
cat(sprintf("Checked on disk: %s storage copies\n", fmt_files(nrow(storage))))

# 5) Optional: the duplicates run - was the file there, and was it its oldest copy?
then <- if (is.na(duplicates_raw)) NULL else
  read_filtered(duplicates_raw, function(x) x %>% filter(tolower(md5) %in% md5s), text_cols("path", "mtime", "md5")) %>%
    mutate(md5 = tolower(md5), k = key(path))
then_keeper <- if (is.null(then)) NULL else
  then %>% group_by(md5) %>%
    arrange(is.na(known_date(mtime)), mtime, path, .by_group = TRUE) %>%  # oldest; ties: first path alphabetically
    slice_head(n = 1) %>% ungroup() %>% select(md5, keeper_then = k)

# 6) One verdict per missing file.
first_per_md5 <- function(rows, date_column) {
  rows %>% group_by(md5) %>%
    arrange(is.na(known_date(.data[[date_column]])), .data[[date_column]], .by_group = TRUE) %>%
    slice_head(n = 1) %>% ungroup()
}
tape_copy <- first_per_md5(tape, "modified") %>%
  transmute(md5, tape_copy = archive_path, tape_label = archive_label, tape_date = known_date(modified))
storage_oldest <- first_per_md5(storage, "mtime") %>% transmute(md5, storage_date = known_date(mtime))
storage_copy <- first_per_md5(storage %>% filter(exists), "mtime") %>% transmute(md5, storage_copy = path)

checked <- missing %>%
  left_join(quarantine, by = "k") %>%
  left_join(tape_copy, by = "md5") %>%
  left_join(storage_oldest, by = "md5") %>%
  left_join(storage_copy, by = "md5") %>%
  mutate(
    quarantined      = !is.na(duplicates_path),
    quarantined_copy = ifelse(quarantined, ifelse(file.exists(duplicates_path), "still there", "gone (quarantine finalized?)"), ""),
    keeper_tape      = ifelse(quarantined, tape$archive_label[match(key(keeper_path), tape$k)], NA_character_),
    keeper_storage   = quarantined & file.exists(coalesce(keeper_path, "")),
    keeper_where     = case_when(!quarantined ~ "", !is.na(keeper_tape) ~ paste0("tape: ", keeper_tape),
                                 keeper_storage ~ "storage", TRUE ~ "not found"),
    has_copy         = !is.na(tape_copy) | !is.na(storage_copy) | quarantined_copy == "still there",
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
    !valid_md5                             ~ "ALERT: no md5, can't be checked",
    quarantined & !is.na(keeper_tape)      ~ "OK: quarantined, its keeper is on tape",
    quarantined & keeper_storage           ~ "OK: quarantined, its keeper is on the storage",
    quarantined & has_copy                 ~ "CHECK: quarantined, its keeper not found, but a copy exists",
    keeper_then == "yes" & has_copy        ~ "CHECK: was the duplicates run's keeper, removed outside the scripts; a copy exists",
    has_copy                               ~ "CHECK: not quarantined (removed outside the scripts); a copy exists",
    TRUE                                   ~ "ALERT: no copy of its content found"
  ),
  class = sub(":.*$", "", verdict))
print(checked %>% count(class, verdict, name = "files") %>% arrange(match(class, names(class_colours))), n = Inf)

# ---- 1. Verdict per missing file -----------------------------------------------

verdicts <- checked %>%
  count(class, verdict, name = "files") %>%
  mutate(class = factor(class, levels = names(class_colours))) %>%
  arrange(desc(as.integer(class)), files) %>%   # ggplot draws the first level at the bottom: OK on top
  mutate(verdict = fct_inorder(verdict))
p_verdicts <- ggplot(verdicts, aes(x = files, y = verdict, fill = class)) +
  geom_col(width = 0.6) +
  geom_text(aes(label = fmt_files(files)), hjust = -0.15, colour = ink, size = 3.4) +
  scale_fill_manual(values = class_colours, drop = FALSE) +
  scale_x_continuous(labels = label_comma(), breaks = function(l) unique(floor(pretty(l))),
                     expand = expansion(mult = c(0, 0.15))) +
  coord_cartesian(clip = "off") +
  labs(title = "Files the archive plans couldn't find: what happened to them",
       subtitle = sprintf("%s missing file(s) in %d plan(s) | quarantine journals: %d | %s",
                          fmt_files(nrow(checked)), length(plans), length(journals),
                          if (is.null(then)) "no duplicates raw" else "with the duplicates run"),
       caption = "OK: a gone duplicate, its keeper is safe | CHECK: a copy of the content exists, but not where expected | ALERT: look into it",
       x = NULL, y = NULL) +
  theme_check
print(p_verdicts)
export_plot(p_verdicts, "archive_missing_check", width = 12, height = 4)

# ---- 2. The missing files, per verdict (interactive table + CSV) -------------------
# Click a verdict to see its files. Each file's row says where it went
# (quarantined to), where its keeper is, and where other copies are.

table_rows <- checked %>%
  add_count(verdict, name = "n") %>%
  mutate(group = sprintf("%s: %s file(s)", verdict, fmt_files(n)),
         rank  = match(class, c("ALERT", "CHECK", "OK"))) %>%
  arrange(rank, desc(n), verdict, original_path) %>%
  transmute(group, file = sub("^.*\\\\", "", original_path),
            folder = sub("\\\\[^\\\\]*$", "", substring(original_path, nchar(root) + 2)),
            size_mb, modified, category, copies, md5, original_path, source, labels,
            duplicates_path, quarantined_copy, keeper_path, keeper_where,
            tape_copy, tape_label, storage_copy, oldest_copy, keeper_then, verdict)

mono <- list(fontFamily = "Consolas, monospace", fontSize = "12px")
path_col <- function(name) colDef(name = name, minWidth = 420, style = mono)
t_missing <- reactable(
  table_rows, groupBy = "group",
  columns = list(
    group            = colDef(name = "Verdict", minWidth = 420, grouped = JS("function(cell) { return cell.value }")),
    file             = colDef(name = "File", minWidth = 200),
    folder           = colDef(name = "Folder (under the root)", minWidth = 260, style = mono),
    size_mb          = colDef(name = "Size (MB)", minWidth = 90, format = colFormat(digits = 1, separators = TRUE)),
    modified         = colDef(name = "Modified (UTC)", minWidth = 150),
    category         = colDef(name = "Category (in the plan)", minWidth = 110),
    copies           = colDef(name = "Copies", minWidth = 70),
    md5              = colDef(name = "md5", minWidth = 150, style = mono),
    original_path    = path_col("Original path"),
    source           = colDef(name = "Found missing by", minWidth = 190),
    labels           = colDef(name = "Archive label(s)", minWidth = 130),
    duplicates_path  = path_col("Quarantined to"),
    quarantined_copy = colDef(name = "Quarantined copy", minWidth = 150),
    keeper_path      = path_col("Its keeper (quarantine)"),
    keeper_where     = colDef(name = "Keeper is now", minWidth = 130),
    tape_copy        = path_col("Oldest copy on tape (archive path)"),
    tape_label       = colDef(name = "Tape label", minWidth = 110),
    storage_copy     = path_col("Oldest copy still on the storage"),
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
                    style = sprintf("color:%s;font-family:Segoe UI,sans-serif;margin:0 0 10px", ink2))
)
print(t_missing)
export_table(t_missing, "archive_missing_check")
write_csv(table_rows %>% select(-group), file.path(output_folder, "archive_missing_check.csv"))
