# Quarantine execution report: what `quarantine_duplicates.py execute` (and
# verify / undo) actually did, from the files they write next to the plan:
#   <plan>_execute_results.csv  outcome per MOVE row (last execute run only)
#   <plan>_journal.csv          every completed move, all runs (timestamped)
#   <plan>_undo_journal.csv     every restore by `undo` (if any)
#   <plan>_verify_report.csv    md5 verification of the duplicates folder
#
# How to use: run section "0. Setup" once - it only sets things up, it reads
# nothing big. After that, every other section runs on its own, in any order:
# each reads the file it needs the first time it's needed (kept for later
# sections; re-run Setup after a new execute/verify to read fresh files),
# prints its tables and shows its plot. The last line of each section
# (export_plot / write_csv) exports it into output_folder - run it, or skip
# it. In RStudio: Ctrl+Alt+T runs the current section.
#
# Memory: on a large volume these files have one row per moved file (Volume B: ~5M),
# so they're read in chunks and each chunk is reduced to a few small columns
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
plan_path      <- r"(C:\path\to\storage_analysis\Volume_A\quarantine_dry\move_plan_YYYYMMDD_HHMMSS.csv)"
reports_folder <- dirname(plan_path)
output_folder  <- r"(C:\path\to\storage_analysis\Volume_A\quarantine_dry)"

##Volume B
plan_path      <- r"(C:\path\to\storage_analysis\Volume_B\quarantine_dry\move_plan_YYYYMMDD_HHMMSS.csv)"
reports_folder <- dirname(plan_path)
output_folder  <- r"(C:\path\to\storage_analysis\Volume_B\quarantine_dry)"

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

settings   <- fromJSON(sub("\\.csv$", "_settings.json", plan_path))
root       <- settings$root
prefix_len <- nchar(root) + 2  # root + "\"
planned_moves <- settings$moves  # NULL for plans made before the "moves" field

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
  "Skipped by a safety check"     = "#fab219",  # status: warning
  "Not moved"                     = "#fab219",
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

# Folder of a path relative to the root (file name dropped), and its first
# two levels, e.g. "attune\db-backup".
rel_dir <- function(paths) {
  rel <- substring(paths, prefix_len)
  ifelse(grepl("\\", rel, fixed = TRUE), sub("\\\\[^\\\\]*$", "", rel), "(root)")
}
top_folder <- function(dirs) sub("^([^\\\\]*(\\\\[^\\\\]*)?).*$", "\\1", dirs)

execute_class <- function(status) case_when(
  status %in% c("MOVED", "MOVED_RESUMED", "ALREADY_MOVED") ~ "Moved",
  startsWith(status, "SKIPPED_")                           ~ "Skipped by a safety check",
  TRUE                                                     ~ "Problem"  # CONFLICT, MISSING, ERROR, MOVE_FAILED, MOVE_TIMEOUT
)
verify_class <- function(status) case_when(
  status == "OK"                 ~ "Verified OK",
  status == "NOT_MOVED"          ~ "Not moved",
  startsWith(status, "SKIPPED_") ~ "SKIP in the plan",
  TRUE                           ~ "Problem"  # MISSING, MD5_MISMATCH, UNREADABLE, EXTRA, MOVED_NOT_JOURNALED, UNSCANNED_FOLDER
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

# Execute results, one small row per MOVE row; detail/path kept for problems only.
get_results <- function() cached("results", function() {
  cat(sprintf("Reading %s ...\n", basename(inputs[["results"]])))
  read_csv_chunked(need("results"), DataFrameCallback$new(function(x, pos) {
    class   <- execute_class(x$status)
    problem <- class == "Problem"
    tibble(
      status = x$status, class = class,
      size_bytes = suppressWarnings(as.numeric(x$size_bytes)),
      folder = top_folder(rel_dir(x$original_path)),
      long_path = x$long_path %in% c("True", "TRUE"),
      detail = ifelse(problem, x$detail, NA_character_),
      original_path = ifelse(problem, x$original_path, NA_character_)
    )
  }), chunk_size = chunk_rows, na = character(), col_types = cols_only(
    status = col_character(), detail = col_character(), size_bytes = col_character(),
    original_path = col_character(), long_path = col_character()
  ))
})

# Verify report, one small row per report row; detail/path kept for problems only.
get_verify <- function() cached("verify", function() {
  cat(sprintf("Reading %s ...\n", basename(inputs[["verify"]])))
  read_csv_chunked(need("verify"), DataFrameCallback$new(function(x, pos) {
    class   <- verify_class(x$status)
    problem <- class == "Problem"
    size    <- suppressWarnings(as.numeric(x$size_bytes))
    tibble(
      status = x$status, class = class,
      size_bytes = ifelse(is.na(size), suppressWarnings(as.numeric(x$size_found)), size),  # EXTRA rows: size found
      detail = ifelse(problem, x$detail, NA_character_),
      duplicates_path = ifelse(problem, x$duplicates_path, NA_character_)
    )
  }), chunk_size = chunk_rows, na = character(), col_types = cols_only(
    status = col_character(), detail = col_character(), size_bytes = col_character(),
    size_found = col_character(), duplicates_path = col_character()
  ))
})

# Journal + undo journal, aggregated while reading: moves per second (for the
# timeline) and per top folder (for space freed). Restores count negative.
get_journal <- function() cached("journal", function() {
  read_events <- function(path, statuses, sign) {
    if (!file.exists(path)) return(NULL)
    cat(sprintf("Reading %s ...\n", basename(path)))
    read_csv_chunked(path, DataFrameCallback$new(function(x, pos) {
      x <- x %>% filter(status %in% statuses)
      list(
        by_second = x %>% group_by(second = substr(timestamp, 1, 19)) %>%
          summarise(files = sign * n(), bytes = sign * sum(size_bytes), .groups = "drop"),
        by_folder = x %>% group_by(folder = top_folder(rel_dir(original_path))) %>%
          summarise(files = sign * n(), bytes = sign * sum(size_bytes), .groups = "drop")
      ) %>% bind_rows(.id = "table")
    }), chunk_size = chunk_rows, na = character(), col_types = cols_only(
      timestamp = col_character(), status = col_character(),
      size_bytes = col_double(), original_path = col_character()
    ))
  }
  events <- bind_rows(
    read_events(need("journal"), c("MOVED", "MOVED_RESUMED"), +1),
    read_events(inputs[["undo"]], "RESTORED", -1)
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
  axis <- if (x == "gb") size_axis(max(data$gb, na.rm = TRUE))
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

# ---- 1. Execute outcome: moved, skipped, problems --------------------------------
# From the last execute run (its results file is rewritten each run).

results <- get_results()
outcome <- by_status(results, c("Moved", "Skipped by a safety check", "Problem"))
print(outcome %>% arrange(desc(files)), n = Inf)
print(results %>% filter(long_path) %>% count(class, name = "long_path_files"))

totals <- results %>% group_by(class) %>% summarise(files = n(), gb = sum(size_bytes, na.rm = TRUE) / 2^30)
moved  <- totals %>% filter(class == "Moved")
p_outcome <- status_panels(
  outcome,
  title    = "Execute outcome per file to move",
  subtitle = sprintf("Moved: %s of %s files (%s)%s",
                     fmt_files(sum(moved$files)), fmt_files(nrow(results)), fmt_size(sum(moved$gb)),
                     if (is.null(planned_moves)) "" else sprintf(" | plan: %s moves", fmt_files(planned_moves))),
  caption  = sprintf("%s (last execute run)", basename(inputs[["results"]]))
)
print(p_outcome)
export_plot(p_outcome, "execution_outcome", width = 12, height = 4.5)

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
  timeline_panel("cum_gb", "Space moved (cumulative)", size_axis(max(timeline$cum_gb))) +
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
export_plot(p_timeline, "execution_timeline", height = 6)

# ---- 3. Verification of the duplicates folder -------------------------------------

verify <- get_verify()
verified <- by_status(verify, c("Verified OK", "Not moved", "SKIP in the plan", "Problem"))
print(verified %>% arrange(desc(files)), n = Inf)
problems_v <- verify %>% filter(class == "Problem")
if (nrow(problems_v) > 0) print(problems_v %>% select(status, detail, duplicates_path), n = 20)

ok <- verified %>% filter(status == "OK")
expected <- verify %>% filter(status %in% c("OK", "MISSING", "MD5_MISMATCH", "UNREADABLE")) %>% nrow()
p_verify <- status_panels(
  verified,
  title    = "Verification: is every moved file in the duplicates folder, unchanged?",
  subtitle = sprintf("Verified OK: %s of %s moved files (%s) | problems: %s",
                     fmt_files(sum(ok$files)), fmt_files(expected), fmt_size(sum(ok$gb)),
                     fmt_files(nrow(problems_v))),
  caption  = basename(inputs[["verify"]])
)
print(p_verify)
export_plot(p_verify, "execution_verification", width = 12, height = 4.5)

# ---- 4. Problems: what went wrong, and where -------------------------------------
# Problem rows of the last execute run (CONFLICT, MISSING, ERROR, MOVE_FAILED,
# MOVE_TIMEOUT), grouped by type: status + the start of the error message.

problems <- get_results() %>%
  filter(class == "Problem") %>%
  mutate(cause = sub(":.*$", "", detail),
         type  = paste0(status, ": ", ifelse(nchar(cause) > 45, paste0(substr(cause, 1, 44), "…"), cause)))
cat(sprintf("%s problem file(s) in the last execute run.\n", fmt_files(nrow(problems))))
if (nrow(problems) > 0) {
  types <- problems %>% count(type, name = "files") %>% slice_max(files, n = top_n, with_ties = FALSE) %>%
    mutate(type = fct_reorder(type, files))
  where <- problems %>% count(folder, name = "files") %>% slice_max(files, n = top_n, with_ties = FALSE) %>%
    mutate(folder = fct_reorder(folder, files))
  print(types); print(where)
  p_problems <- hbar(types, "files", "type", fmt_files(types$files), "By type") +
    hbar(where, "files", "folder", fmt_files(where$files), "By folder (two levels under the root)") +
    plot_annotation(
      title = "Files execute could not move",
      subtitle = "Re-running execute retries them; timeouts and locked files often succeed later",
      caption = basename(inputs[["results"]]), theme = theme_exec
    )
  print(p_problems)
  export_plot(p_problems, "execution_problems", width = 13, height = 5)
  write_csv(problems %>% select(status, detail, original_path), file.path(output_folder, "execution_problems.csv"))
}

# ---- 5. Space freed per folder (journal) -----------------------------------------
# Net of restores: what is in the duplicates folder now, per original folder.

freed <- get_journal()$by_folder %>% filter(files > 0)
print(freed %>% arrange(desc(gb)), n = top_n)

top_space <- freed %>% slice_max(gb, n = top_n, with_ties = FALSE) %>% mutate(folder = fct_reorder(folder, gb))
top_files <- freed %>% slice_max(files, n = top_n, with_ties = FALSE) %>% mutate(folder = fct_reorder(folder, files))
p_freed <- hbar(top_space, "gb", "folder", fmt_size(top_space$gb), sprintf("Top %d by space", top_n)) +
  hbar(top_files, "files", "folder", fmt_files(top_files$files), sprintf("Top %d by files", top_n)) +
  plot_annotation(
    title = "Moved into the duplicates folder, per original folder",
    subtitle = sprintf("Total: %s files, %s | root: %s", fmt_files(sum(freed$files)), fmt_size(sum(freed$gb)), root),
    caption = basename(inputs[["journal"]]), theme = theme_exec
  )
print(p_freed)
export_plot(p_freed, "execution_space_by_folder", width = 13, height = 6)
