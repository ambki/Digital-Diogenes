# Archive execution report: what `archive_old_files.py execute` (and verify /
# undo) actually did, from the files they write next to the plan:
#   <plan>_execute_results.csv  outcome per file, folder and quarantine note (last execute run only)
#   <plan>_journal.csv          every completed move and removed folder, all runs (timestamped)
#   <plan>_undo_journal.csv     every restore by `undo` (if any)
#   <plan>_verify_report.csv    md5 verification of move_to_archive
#
# How to use: run section "0. Setup" once - it only sets things up, it reads
# nothing big. After that, every other section runs on its own, in any order:
# each reads the file it needs the first time it's needed (kept for later
# sections; re-run Setup after a new execute/verify to read fresh files),
# prints its tables and shows its plot. The last line of each section
# (export_plot / write_csv) exports it into output_folder - run it, or skip
# it. In RStudio: Ctrl+Alt+T runs the current section.
#
# Memory: on a large volume these files have one row per moved file, so
# they're read in chunks and each chunk is reduced to a few small columns
# (paths become their top folder; only problem rows keep their full path).
#
# Install once: install.packages(c("readr", "dplyr", "tidyr", "ggplot2", "scales", "forcats", "patchwork", "jsonlite"))

# ---- 0. Setup (run once) -----------------------------------------------------

library(readr)
library(dplyr)
library(tidyr)
library(ggplot2)
library(scales)
library(forcats)
library(patchwork)
library(jsonlite)

# Configuration. The block for the volume you want goes last (later
# assignments win). plan_path: the plan that was EXECUTED (placeholders
# below - point them at your executed plan). reports_folder: where execute
# wrote its journal and reports - its --output, which defaults to the
# plan's folder.

##Volume A
plan_path      <- r"(C:\path\to\storage_analysis\Volume_A\archive_dry\archive_plan_YYYYMMDD_HHMMSS.csv)"
reports_folder <- dirname(plan_path)
output_folder  <- r"(C:\path\to\storage_analysis\Volume_A\archive_dry)"

##Volume B
plan_path      <- r"(C:\path\to\storage_analysis\Volume_B\archive_dry\archive_plan_YYYYMMDD_HHMMSS.csv)"
reports_folder <- dirname(plan_path)
output_folder  <- r"(C:\path\to\storage_analysis\Volume_B\archive_dry)"

top_n      <- 15        # folders / problem types shown in the "top" charts
chunk_rows <- 500000    # rows read at a time (memory vs. speed)
run_gap_minutes <- 15   # a pause longer than this in the journal starts a new run

prefix <- file.path(reports_folder, tools::file_path_sans_ext(basename(plan_path)))
inputs <- c(
  results = paste0(prefix, "_execute_results.csv"),
  journal = paste0(prefix, "_journal.csv"),
  undo    = paste0(prefix, "_undo_journal.csv"),
  verify  = paste0(prefix, "_verify_report.csv")
)
print(tibble(file = basename(inputs), found = file.exists(inputs)))

settings      <- fromJSON(sub("\\.csv$", "_settings.json", plan_path))
root          <- settings$root
prefix_len    <- nchar(sub("[\\\\/]+$", "", root)) + 2  # root (without a trailing "\") + "\"
planned_moves <- settings$moves
label         <- settings$archive_label

# Plot style: validated default palette (light mode), status colours for
# outcomes (always shown with their label), ink for all text, hairline grid.
surface <- "#fcfcfb"
ink     <- "#0b0b0b"
ink2    <- "#52514e"
muted   <- "#898781"
gridcol <- "#e1e0d9"
blue    <- "#2a78d6"
class_colours <- c(
  "Moved"                         = "#0ca30c",  # status: good
  "Verified OK"                   = "#0ca30c",
  "Folder removed"                = "#0ca30c",
  "Skipped by a safety check"     = "#fab219",  # status: warning
  "Stayed: folder kept"           = "#fab219",
  "Not moved"                     = "#fab219",
  "Folder kept"                   = "#fab219",
  "SKIP in the plan"              = "#c3c2b7",  # neutral: never meant to move
  "Problem"                       = "#d03b3b"   # status: critical
)

theme_exec <- theme_minimal(base_size = 11) +
  theme(
    plot.background    = element_rect(fill = surface, colour = NA),
    panel.background   = element_rect(fill = surface, colour = NA),
    panel.grid.minor   = element_blank(),
    panel.grid.major.y = element_blank(),
    panel.grid.major.x = element_line(colour = gridcol, linewidth = 0.3),
    axis.text          = element_text(colour = muted),
    axis.title         = element_text(colour = ink2),
    plot.title         = element_text(colour = ink, face = "bold"),
    plot.subtitle      = element_text(colour = ink2),
    plot.caption       = element_text(colour = muted, hjust = 0),
    legend.text        = element_text(colour = ink2),
    legend.title       = element_blank(),
    legend.position    = "top",
    legend.justification = "left"
  )

fmt_files <- label_comma()
fmt_size  <- function(gb) ifelse(gb == 0, "0", ifelse(gb >= 1024, sprintf("%.2f TB", gb / 1024),
                          ifelse(gb >= 1, sprintf("%.0f GB", gb),
                          ifelse(gb * 1024 < 0.1, "<0.1 MB", sprintf("%.1f MB", gb * 1024)))))
fmt_pct   <- function(x) ifelse(x < 0.001 & x > 0, "<0.1%", percent(x, accuracy = 0.1))

# Axis for a size in GB: round ticks in the unit that fits the largest value.
size_axis <- function(max_gb) {
  unit <- if (max_gb >= 1024) c(1 / 1024, " TB") else if (max_gb >= 1) c(1, " GB") else c(1024, " MB")
  list(labels = label_number(scale = as.numeric(unit[1]), suffix = unit[2], big.mark = ","),
       breaks = function(limits) pretty(limits * as.numeric(unit[1])) / as.numeric(unit[1]))
}
# Count axis with whole-number ticks only.
count_breaks <- function(limits) unique(floor(pretty(limits)))

# Export a plot as <output_folder>/<name>.png (the last line of each section).
export_plot <- function(plot, name, width = 10, height = 6) {
  dir.create(output_folder, showWarnings = FALSE, recursive = TRUE)
  path <- file.path(output_folder, paste0(name, ".png"))
  ggsave(path, plot, width = width, height = height, dpi = 150, bg = surface)
  cat(sprintf("Saved: %s\n", path))
}

# Path relative to the root; its folder (file name dropped); first two folder levels.
rel_path   <- function(paths) substring(paths, prefix_len)
rel_dir    <- function(paths) {
  rel <- rel_path(paths)
  ifelse(grepl("\\", rel, fixed = TRUE), sub("\\\\[^\\\\]*$", "", rel), "(root)")
}
top_folder <- function(dirs) sub("^([^\\\\]*(\\\\[^\\\\]*)?).*$", "\\1", dirs)

# Outcome classes. Files (kind file/system/note), folders and quarantine notes
# share the results file; each section picks its own rows.
execute_class <- function(status) case_when(
  status %in% c("MOVED", "MOVED_RESUMED", "ALREADY_MOVED")    ~ "Moved",
  startsWith(status, "SKIPPED_")                              ~ "Skipped by a safety check",
  status == "FOLDER_KEPT"                                     ~ "Stayed: folder kept",
  TRUE                                                        ~ "Problem"  # CONFLICT, MISSING, ERROR, MOVE_FAILED, MOVE_TIMEOUT
)
folder_class <- function(status) case_when(
  status %in% c("REMOVED", "ALREADY_REMOVED") ~ "Folder removed",
  status == "FOLDER_KEPT"                     ~ "Folder kept",
  TRUE                                        ~ "Problem"  # ERROR
)
note_class <- function(status) case_when(
  status %in% c("NOTE_UPDATED", "KEEPER_NOT_MOVED") ~ "Note updated or not needed",
  TRUE                                              ~ "Problem"  # NOTE_MISSING, NOTE_FAILED, ERROR
)
verify_class <- function(status) case_when(
  status == "OK"                 ~ "Verified OK",
  status == "NOT_MOVED"          ~ "Not moved",
  startsWith(status, "SKIPPED_") ~ "SKIP in the plan",
  TRUE                           ~ "Problem"  # MISSING, MD5_MISMATCH, SIZE_MISMATCH, UNREADABLE, EXTRA, MOVED_NOT_JOURNALED, UNSCANNED_FOLDER
)

need <- function(name) {
  if (!file.exists(inputs[[name]])) stop(sprintf("Not found: %s (has execute/verify run for this plan?)", inputs[[name]]))
  inputs[[name]]
}

# Each file is read the first time a section needs it and kept in .cache, so
# sections stay independent without re-reading GBs. Re-running Setup clears it.
.cache <- new.env()
cached <- function(name, loader) {
  if (!exists(name, envir = .cache, inherits = FALSE)) assign(name, loader(), envir = .cache)
  get(name, envir = .cache)
}

# Execute results, one small row per result row; detail/path kept for
# problems and kept folders only.
get_results <- function() cached("results", function() {
  cat(sprintf("Reading %s ...\n", basename(inputs[["results"]])))
  read_csv_chunked(need("results"), DataFrameCallback$new(function(x, pos) {
    is_folder <- x$kind == "folder"
    class     <- case_when(is_folder ~ folder_class(x$status),
                           x$kind == "keeper_note" ~ note_class(x$status),
                           TRUE ~ execute_class(x$status))
    keep      <- class %in% c("Problem", "Folder kept")
    tibble(
      status = x$status, class = class, kind = x$kind,
      size_bytes = suppressWarnings(as.numeric(x$size_bytes)),
      folder = top_folder(ifelse(is_folder, rel_path(x$original_path), rel_dir(x$original_path))),
      long_path = x$long_path %in% c("True", "TRUE"),
      detail = ifelse(keep, x$detail, NA_character_),
      original_path = ifelse(keep, x$original_path, NA_character_)
    )
  }), chunk_size = chunk_rows, na = character(), col_types = cols_only(
    status = col_character(), detail = col_character(), kind = col_character(),
    size_bytes = col_character(), original_path = col_character(), long_path = col_character()
  ))
})

# Verify report, one small row per report row (folders are in section 5);
# detail/path kept for problems only.
get_verify <- function() cached("verify", function() {
  cat(sprintf("Reading %s ...\n", basename(inputs[["verify"]])))
  read_csv_chunked(need("verify"), DataFrameCallback$new(function(x, pos) {
    x <- x %>% filter(kind != "folder")
    class   <- verify_class(x$status)
    problem <- class == "Problem"
    size    <- suppressWarnings(as.numeric(x$size_bytes))
    tibble(
      status = x$status, class = class,
      size_bytes = ifelse(is.na(size), suppressWarnings(as.numeric(x$size_found)), size),  # EXTRA rows: size found
      detail = ifelse(problem, x$detail, NA_character_),
      archive_path = ifelse(problem, x$archive_path, NA_character_)
    )
  }), chunk_size = chunk_rows, na = character(), col_types = cols_only(
    status = col_character(), detail = col_character(), kind = col_character(),
    size_bytes = col_character(), size_found = col_character(), archive_path = col_character()
  ))
})

# Journal + undo journal, aggregated while reading: files moved per second
# (for the timeline) and per top folder (for space moved). Restores, and moves
# journaled but then reported NOT_MOVED, count negative. Folder events
# (REMOVED) are left out here.
get_journal <- function() cached("journal", function() {
  read_events <- function(path, statuses, sign) {
    if (!file.exists(path)) return(NULL)
    cat(sprintf("Reading %s (%s) ...\n", basename(path), paste(statuses, collapse = ", ")))
    read_csv_chunked(path, DataFrameCallback$new(function(x, pos) {
      x <- x %>% filter(status %in% statuses, kind != "folder") %>% mutate(size_bytes = coalesce(size_bytes, 0))
      list(
        by_second = x %>% group_by(second = substr(timestamp, 1, 19)) %>%
          summarise(files = sign * n(), bytes = sign * sum(size_bytes), .groups = "drop"),
        by_folder = x %>% group_by(folder = top_folder(rel_dir(original_path))) %>%
          summarise(files = sign * n(), bytes = sign * sum(size_bytes), .groups = "drop")
      ) %>% bind_rows(.id = "table")
    }), chunk_size = chunk_rows, na = character(), col_types = cols_only(
      timestamp = col_character(), status = col_character(), kind = col_character(),
      size_bytes = col_double(), original_path = col_character()
    ))
  }
  events <- bind_rows(
    read_events(need("journal"), c("MOVED", "MOVED_RESUMED"), +1),
    read_events(inputs[["journal"]], "NOT_MOVED", -1),
    read_events(inputs[["undo"]], c("RESTORED", "ALREADY_RESTORED"), -1)
  )
  list(
    by_second = events %>% filter(table == "by_second") %>% group_by(second) %>%
      summarise(files = sum(files), bytes = sum(bytes), .groups = "drop"),
    by_folder = events %>% filter(table == "by_folder") %>% group_by(folder) %>%
      summarise(files = sum(files), gb = sum(bytes) / 2^30, .groups = "drop")
  )
})

# x scale for a bar chart: sizes (column "gb") or counts.
bar_scale <- function(data, x) {
  axis <- if (x == "gb") size_axis(max(data$gb, 1e-9, na.rm = TRUE))
          else list(labels = label_comma(), breaks = count_breaks)
  scale_x_continuous(labels = axis$labels, breaks = axis$breaks, expand = expansion(mult = c(0, 0.5)))
}

# Horizontal bars per status, coloured by outcome class, labelled at the end.
status_bars <- function(data, x, labels, title) {
  ggplot(data, aes(x = .data[[x]], y = status, fill = class)) +
    geom_col(width = 0.6) +
    geom_text(aes(label = labels), hjust = -0.1, colour = ink, size = 3.3) +
    scale_fill_manual(values = class_colours) +
    bar_scale(data, x) +
    coord_cartesian(clip = "off") +
    labs(title = title, x = NULL, y = NULL) + theme_exec
}

# One horizontal bar per category (single series).
hbar <- function(data, x, y, labels, title) {
  ggplot(data, aes(x = .data[[x]], y = .data[[y]])) +
    geom_col(width = 0.6, fill = blue) +
    geom_text(aes(label = labels), hjust = -0.1, colour = ink, size = 3.3) +
    bar_scale(data, x) +
    coord_cartesian(clip = "off") +
    labs(title = title, x = NULL, y = NULL) + theme_exec
}

# Status rows for the bar charts: grouped by class (in class_levels order,
# top to bottom), largest first within a class.
by_status <- function(rows, class_levels) {
  rows %>%
    group_by(status, class) %>%
    summarise(files = n(), gb = sum(size_bytes, na.rm = TRUE) / 2^30, .groups = "drop") %>%
    mutate(class = factor(class, levels = class_levels)) %>%
    arrange(desc(as.integer(class)), files) %>%   # ggplot draws the first level at the bottom
    mutate(status = fct_inorder(status))
}

# Files and Space panels side by side, one shared legend.
status_panels <- function(data, title, subtitle, caption) {
  ((status_bars(data, "files", fmt_files(data$files), "Files") +
      theme(plot.margin = margin(5, 40, 5, 5)) +   # room between the two panels' axis labels
      (status_bars(data, "gb", fmt_size(data$gb), "Space") +
         theme(axis.text.y = element_blank()))) +
     plot_layout(guides = "collect") +
     plot_annotation(title = title, subtitle = subtitle, caption = caption, theme = theme_exec)) &
    theme(legend.position = "top", legend.justification = "left")
}

# ---- 1. Execute outcome per file: moved, skipped, problems ----------------------
# From the last execute run (its results file is rewritten each run). Files
# selected by date, plus the notes and system files that followed their
# folder. Folders and quarantine notes are in section 5.

files <- get_results() %>% filter(kind %in% c("file", "system", "note"))
outcome <- by_status(files, c("Moved", "Stayed: folder kept", "Skipped by a safety check", "Problem"))
print(outcome %>% arrange(desc(files)), n = Inf)
print(files %>% filter(long_path) %>% count(class, name = "long_path_files"))

moved <- files %>% filter(class == "Moved")
p_outcome <- status_panels(
  outcome,
  title    = "Execute outcome per file",
  subtitle = sprintf(paste0("Moved to move_to_archive: %s of %s files (%s) | label: %s\n",
                            "plan: %s files selected by date, plus the notes and system files that followed their emptied folders"),
                     fmt_files(nrow(moved)), fmt_files(nrow(files)),
                     fmt_size(sum(moved$size_bytes, na.rm = TRUE) / 2^30), label, fmt_files(planned_moves)),
  caption  = sprintf("%s (last execute run)", basename(inputs[["results"]]))
)
print(p_outcome)
export_plot(p_outcome, "archive_execution_outcome", width = 12, height = 4.5)

# ---- 2. Progress over time (journal) ---------------------------------------------
# All runs, from the journal; restores by undo count down. A pause longer than
# run_gap_minutes starts a new run.

journal <- get_journal()
timeline <- journal$by_second %>%
  mutate(time = as.POSIXct(second, format = "%Y-%m-%d %H:%M:%S", tz = "")) %>%
  filter(!is.na(time)) %>%
  arrange(time)
span_hours <- as.numeric(difftime(max(timeline$time), min(timeline$time), units = "hours"))
bin <- if (span_hours > 2) "1 min" else "1 sec"  # minute bins for long runs
timeline <- timeline %>%
  mutate(time = as.POSIXct(as.character(cut(time, bin)), tz = "")) %>%
  group_by(time) %>% summarise(files = sum(files), bytes = sum(bytes), .groups = "drop") %>%
  mutate(
    run       = cumsum(c(TRUE, diff(as.numeric(time)) > run_gap_minutes * 60)),
    cum_files = cumsum(files),
    cum_gb    = cumsum(bytes) / 2^30
  )
runs <- timeline %>%
  group_by(run) %>%
  summarise(start = min(time), end = max(time), files = sum(files), gb = sum(bytes) / 2^30, .groups = "drop") %>%
  mutate(minutes = pmax(as.numeric(difftime(end, start, units = "mins")), 1), files_per_hour = round(files / minutes * 60))
print(runs)

time_labels <- if (bin == "1 sec") "%H:%M:%S" else if (span_hours > 48) "%d %b" else "%d %b %H:%M"
timeline_panel <- function(column, title, axis) {
  ggplot(timeline, aes(x = time, y = .data[[column]], group = run)) +
    geom_step(colour = blue, linewidth = 0.7) +
    scale_y_continuous(labels = axis$labels, breaks = axis$breaks) +
    scale_x_datetime(date_labels = time_labels) +
    labs(title = title, x = NULL, y = NULL) + theme_exec +
    theme(panel.grid.major.y = element_line(colour = gridcol, linewidth = 0.3),
          panel.grid.major.x = element_blank())
}
p_timeline <- timeline_panel("cum_files", "Files moved (cumulative)", list(labels = label_comma(), breaks = count_breaks)) /
  timeline_panel("cum_gb", "Space moved (cumulative)", size_axis(max(timeline$cum_gb, 1e-9))) +
  plot_annotation(
    title = "Progress of the moves over time",
    subtitle = sprintf("%d run(s) | now moved: %s files, %s | average while running: %s files/hour",
                       nrow(runs), fmt_files(last(timeline$cum_files)), fmt_size(last(timeline$cum_gb)),
                       fmt_files(round(sum(runs$files) / sum(runs$minutes) * 60))),
    caption = sprintf("%s%s | binned per %s", basename(inputs[["journal"]]),
                      if (file.exists(inputs[["undo"]])) " + undo journal (restores count down)" else "",
                      sub("1 ", "", bin)),
    theme = theme_exec
  )
print(p_timeline)
export_plot(p_timeline, "archive_execution_timeline", height = 6)

# ---- 3. Verification of move_to_archive -------------------------------------------

verify <- get_verify()
verified <- by_status(verify, c("Verified OK", "Not moved", "SKIP in the plan", "Problem"))
print(verified %>% arrange(desc(files)), n = Inf)
problems_v <- verify %>% filter(class == "Problem")
if (nrow(problems_v) > 0) print(problems_v %>% select(status, detail, archive_path), n = 20)

ok <- verified %>% filter(status == "OK")
expected <- verify %>% filter(status %in% c("OK", "MISSING", "MD5_MISMATCH", "SIZE_MISMATCH", "UNREADABLE")) %>% nrow()
p_verify <- status_panels(
  verified,
  title    = "Verification: is every moved file in move_to_archive, unchanged?",
  subtitle = sprintf("Verified OK: %s of %s moved files (%s) | problems: %s",
                     fmt_files(sum(ok$files)), fmt_files(expected), fmt_size(sum(ok$gb)),
                     fmt_files(nrow(problems_v))),
  caption  = basename(inputs[["verify"]])
)
print(p_verify)
export_plot(p_verify, "archive_execution_verification", width = 12, height = 4.5)

# ---- 4. Problems: what went wrong, and where -------------------------------------
# Problem rows of the last execute run (CONFLICT, MISSING, ERROR, MOVE_FAILED,
# MOVE_TIMEOUT, and failed quarantine note updates), grouped by type: status +
# the start of the error message. MOVE_FAILED with "Access is denied" is
# usually missing rights on the folder: see change_folder_permissions.py.

problems <- get_results() %>%
  filter(class == "Problem") %>%
  mutate(cause = sub(":.*$", "", detail),
         type  = paste0(status, ": ", ifelse(nchar(cause) > 45, paste0(substr(cause, 1, 44), "…"), cause)))
cat(sprintf("%s problem row(s) in the last execute run.\n", fmt_files(nrow(problems))))
if (nrow(problems) > 0) {
  types <- problems %>% count(type, name = "files") %>% slice_max(files, n = top_n, with_ties = FALSE) %>%
    mutate(type = fct_reorder(type, files))
  where <- problems %>% count(folder, name = "files") %>% slice_max(files, n = top_n, with_ties = FALSE) %>%
    mutate(folder = fct_reorder(folder, files))
  print(types); print(where)
  p_problems <- hbar(types, "files", "type", fmt_files(types$files), "By type") +
    hbar(where, "files", "folder", fmt_files(where$files), "By folder (two levels under the root)") +
    plot_annotation(
      title = "What execute could not do",
      subtitle = "Re-run execute (or execute --retry-failed) to retry; timeouts and locked files often succeed later",
      caption = basename(inputs[["results"]]), theme = theme_exec
    )
  print(p_problems)
  export_plot(p_problems, "archive_execution_problems", width = 13, height = 5)
  write_csv(problems %>% select(status, kind, detail, original_path), file.path(output_folder, "archive_execution_problems.csv"))
}

# ---- 5. Folders: removed from the storage, or kept and why ------------------------
# Kept-folder reasons are normalized (counts and example names dropped), e.g.
# "still has file(s) that were not moved". Also counts the quarantine notes updated.

folders <- get_results() %>% filter(kind == "folder")
keeper_notes <- get_results() %>% filter(kind == "keeper_note") %>% count(status, name = "notes")
print(folders %>% count(status, name = "folders"))
print(keeper_notes)

kept <- folders %>%
  filter(class != "Folder removed") %>%
  mutate(reason = detail %>% sub(", e\\.g\\..*$", "", .) %>% sub(":.*$", "", .) %>%
           gsub("[0-9]+ ", "", .) %>% sub("'.*$", "", .) %>% trimws())
reasons <- kept %>% count(reason, name = "folders") %>% mutate(reason = fct_reorder(reason, folders))
print(reasons)

summary_rows <- folders %>% count(class, name = "folders") %>%
  mutate(class = fct_rev(factor(class, levels = c("Folder removed", "Folder kept", "Problem"))))
p_folders <- (ggplot(summary_rows, aes(x = folders, y = class, fill = class)) +
                geom_col(width = 0.6, show.legend = FALSE) +
                geom_text(aes(label = fmt_files(folders)), hjust = -0.1, colour = ink, size = 3.3) +
                scale_fill_manual(values = class_colours) +
                scale_x_continuous(labels = label_comma(), breaks = count_breaks, expand = expansion(mult = c(0, 0.5))) +
                coord_cartesian(clip = "off") +
                labs(title = "Emptied folders", x = NULL, y = NULL) + theme_exec)
if (nrow(reasons) > 0) {
  p_folders <- p_folders + theme(plot.margin = margin(5, 40, 5, 5)) +
    hbar(reasons, "folders", "reason", fmt_files(reasons$folders), "Why folders were kept")
}
p_folders <- p_folders +
  plot_annotation(
    title = "Folders removed from the storage, or kept",
    subtitle = sprintf("Quarantine notes updated (keeper archived): %s",
                       fmt_files(sum(keeper_notes$notes[keeper_notes$status == "NOTE_UPDATED"]))),
    caption = sprintf("%s | a kept folder still holds data; re-run execute once it's resolved", basename(inputs[["results"]])),
    theme = theme_exec
  )
print(p_folders)
export_plot(p_folders, "archive_execution_folders", width = 13, height = 4)
if (nrow(kept) > 0) {
  write_csv(kept %>% select(status, reason, detail, original_path), file.path(output_folder, "archive_execution_kept_folders.csv"))
}

# ---- 6. Moved per folder (journal) -----------------------------------------------
# Net of restores: what is in move_to_archive now, per original folder.

moved_folders <- get_journal()$by_folder %>% filter(files > 0)
print(moved_folders %>% arrange(desc(gb)), n = top_n)

top_space <- moved_folders %>% slice_max(gb, n = top_n, with_ties = FALSE) %>% mutate(folder = fct_reorder(folder, gb))
top_files <- moved_folders %>% slice_max(files, n = top_n, with_ties = FALSE) %>% mutate(folder = fct_reorder(folder, files))
p_space <- hbar(top_space, "gb", "folder", fmt_size(top_space$gb), sprintf("Top %d by space", top_n)) +
  hbar(top_files, "files", "folder", fmt_files(top_files$files), sprintf("Top %d by files", top_n)) +
  plot_annotation(
    title = "Moved into move_to_archive, per original folder",
    subtitle = sprintf("Total: %s files, %s | root: %s",
                       fmt_files(sum(moved_folders$files)), fmt_size(sum(moved_folders$gb)), root),
    caption = basename(inputs[["journal"]]), theme = theme_exec
  )
print(p_space)
export_plot(p_space, "archive_execution_space_by_folder", width = 13, height = 6)
