# Quarantine plan profile: what `quarantine_duplicates.py plan` would move,
# in number of files and in storage space, and where it comes from.
#
# How to use: run section "0. Setup" once (it reads the plan - a few minutes
# for a large volume). After that, every other section runs on its own, in
# any order and as often as you like: each depends only on Setup, prints its
# tables and shows its plot. The last line of each section (ggsave/write_csv)
# exports it into output_folder - run it, or skip it. In RStudio, put the
# cursor in a section and press Ctrl+Alt+T (Run current section).
#
# Input: a move_plan_<timestamp>.csv written by `quarantine_duplicates.py
# plan`, with its move_plan_<timestamp>_settings.json next to it (for the
# volume root). Section 1 also uses duplicates_check.py's "<stem>_raw.csv"
# of the same scan, to put the plan in proportion to the whole volume.
#
# Memory: a plan for a large volume is several GB (Volume B: ~4 GB, ~6M rows), so it
# is never loaded whole - it's read in chunks and each chunk is reduced to a
# few small columns (paths are replaced by their top folder) before the next
# one is read. Peak ~1.3 GB for Volume B.
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

# Configuration. Same pattern as storage_report_by_year.R: the block for the
# volume you want goes last (later assignments win).
# raw_path: duplicates_check.py's raw listing of the same scan (section 1).
# NA = skip the whole-volume comparison.

##Volume A
plan_path     <- r"(C:\path\to\storage_analysis\Volume_A\quarantine_dry\move_plan_YYYYMMDD_HHMMSS.csv)"
raw_path      <- r"(C:\path\to\storage_analysis\Volume_A\duplicates_analysis\duplicates_volume_a_raw.csv)"
output_folder <- r"(C:\path\to\storage_analysis\Volume_A\quarantine_dry)"

##Volume B
plan_path     <- r"(C:\path\to\storage_analysis\Volume_B\quarantine_dry\move_plan_YYYYMMDD_HHMMSS.csv)"
raw_path      <- r"(C:\path\to\storage_analysis\Volume_B\duplicates_analysis\duplicates_volume_b_raw.csv)"
output_folder <- r"(C:\path\to\storage_analysis\Volume_B\quarantine_dry)"

top_n         <- 15       # folders / groups shown in the "top" charts
chunk_rows     <- 500000    # rows read at a time (memory vs. speed)

# Plot style: validated default palette (light mode), categorical slots in
# fixed order, ink for all text, hairline grid.
surface <- "#fcfcfb"
ink     <- "#0b0b0b"
ink2    <- "#52514e"
muted   <- "#898781"
gridcol <- "#e1e0d9"
series  <- c("#2a78d6", "#eb6834", "#1baf7a", "#eda100")  # blue, orange, aqua, yellow

theme_plan <- theme_minimal(base_size = 11) +
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
                          ifelse(gb >= 1, sprintf("%.0f GB", gb), sprintf("%.1f MB", gb * 1024))))
fmt_pct   <- function(x) ifelse(x < 0.001 & x > 0, "<0.1%", percent(x, accuracy = 0.1))

# Export a plot as <output_folder>/<name>.png (the last line of each section).
export_plot <- function(plot, name, width = 10, height = 6) {
  path <- file.path(output_folder, paste0(name, ".png"))
  ggsave(path, plot, width = width, height = height, dpi = 150, bg = surface)
  cat(sprintf("Saved: %s\n", path))
}

# Two bars (Files, Space), each split 100% by `fill_column`.
share_bars <- function(data, fill_column, title, subtitle, caption) {
  long <- data %>%
    pivot_longer(c(files, gb), names_to = "metric", values_to = "value") %>%
    mutate(metric = factor(recode(metric, files = "Files", gb = "Space"), levels = c("Space", "Files"))) %>%
    group_by(metric) %>% mutate(share = value / sum(value)) %>% ungroup()
  ggplot(long, aes(x = share, y = metric, fill = .data[[fill_column]])) +
    geom_col(width = 0.55, colour = surface, linewidth = 0.6, position = position_stack(reverse = TRUE)) +
    geom_text(aes(label = ifelse(share >= 0.04, fmt_pct(share), "")), colour = ink, size = 3.6,
              position = position_stack(vjust = 0.5, reverse = TRUE)) +
    scale_fill_manual(values = series, drop = FALSE) +
    scale_x_continuous(labels = percent, expand = expansion(mult = c(0, 0.01))) +
    labs(title = title, subtitle = subtitle, x = NULL, y = NULL, caption = caption) +
    theme_plan + guides(fill = guide_legend(nrow = 2))
}

# One horizontal bar per category, value labels at the bar ends.
hbar <- function(data, x, y, labels, title, axis_labels = label_comma(), y_title = NULL) {
  ggplot(data, aes(x = .data[[x]], y = .data[[y]])) +
    geom_col(width = 0.6, fill = series[1]) +
    geom_text(aes(label = labels), hjust = -0.1, colour = ink, size = 3.3) +
    scale_x_continuous(labels = axis_labels, expand = expansion(mult = c(0, 0.6))) +
    coord_cartesian(clip = "off") +
    labs(title = title, x = NULL, y = y_title) + theme_plan
}

settings   <- fromJSON(sub("\\.csv$", "_settings.json", plan_path))
root       <- settings$root
prefix_len <- nchar(root) + 2  # root + "\"

# Folder of a path, relative to the root (file name dropped).
rel_dir <- function(paths) {
  rel <- substring(paths, prefix_len)
  ifelse(grepl("\\", rel, fixed = TRUE), sub("\\\\[^\\\\]*$", "", rel), "(root)")
}
# First two folder levels, e.g. "attune\db-backup".
top_folder <- function(dirs) sub("^([^\\\\]*(\\\\[^\\\\]*)?).*$", "\\1", dirs)

reduce_chunk <- function(x, pos) {
  tibble(
    action     = x$action,
    reason     = x$reason,
    md5        = x$md5,
    size_bytes = x$size_bytes,
    folder     = top_folder(rel_dir(x$original_path)),
    keeper_path = ifelse(x$action == "KEEP", x$original_path, NA_character_)  # one per group
  )
}

cat(sprintf("Reading plan in chunks of %s rows: %s\n", fmt_files(chunk_rows), plan_path))
plan <- read_csv_chunked(
  plan_path, DataFrameCallback$new(reduce_chunk), chunk_size = chunk_rows,
  col_types = cols_only(
    action = col_character(), reason = col_character(), md5 = col_character(),
    size_bytes = col_double(), original_path = col_character()
  ),
  na = character()  # keep "" as "" (an empty reason/keeper is not missing data)
)
moves       <- plan %>% filter(action == "MOVE")
moved_files <- nrow(moves)
moved_gb    <- sum(moves$size_bytes) / 2^30
invisible(gc())
cat(sprintf("Plan loaded: %s rows, %s to move (%s).\n", fmt_files(nrow(plan)), fmt_files(moved_files), fmt_size(moved_gb)))

# ---- 1. What the plan does, in files and in space ------------------------------
# Needs the raw listing (raw_path) for the whole-volume view; read here only.

by_action <- plan %>%
  group_by(action) %>%
  summarise(files = n(), gb = sum(size_bytes) / 2^30, .groups = "drop")
print(by_action)
print(plan %>% filter(action == "SKIP") %>% count(reason, sort = TRUE, name = "files"))

share_levels <- c("Moved to duplicates", "Stays: keeper (one per group)",
                  "Stays: skipped duplicate", "Stays: unique file (no duplicate)")
share <- by_action %>%
  mutate(category = recode(action, MOVE = share_levels[1], KEEP = share_levels[2], SKIP = share_levels[3])) %>%
  select(category, files, gb)

base_label <- "of the files in duplicate groups"
if (!is.na(raw_path) && file.exists(raw_path)) {
  cat(sprintf("Reading volume totals from: %s\n", raw_path))
  volume <- read_csv_chunked(
    raw_path,
    DataFrameCallback$new(function(x, pos) tibble(files = nrow(x), gb = sum(x$size_gb, na.rm = TRUE))),
    chunk_size = chunk_rows, col_types = cols_only(size_gb = col_double())
  ) %>% summarise(files = sum(files), gb = sum(gb))
  share <- bind_rows(share, tibble(
    category = share_levels[4], files = volume$files - nrow(plan), gb = volume$gb - sum(by_action$gb)
  ))
  base_label <- "of the volume"
} else {
  cat(sprintf("Raw listing not found (%s) - showing the plan's files only.\n", raw_path))
}
share <- share %>% mutate(category = factor(category, levels = share_levels[share_levels %in% category]))
print(share)

p_share <- share_bars(
  share, "category",
  title    = "What the plan moves: share of files vs share of space",
  subtitle = sprintf("Moved: %s files (%s %s) = %s (%s %s)",
                     fmt_files(moved_files), fmt_pct(moved_files / sum(share$files)), base_label,
                     fmt_size(moved_gb), fmt_pct(moved_gb / sum(share$gb)), base_label),
  caption  = basename(plan_path)
)
print(p_share)
export_plot(p_share, "quarantine_volume_share", height = 4)

# ---- 2. Size of the files to move -----------------------------------------------

size_breaks <- c(0, 10 * 2^10, 2^20, 100 * 2^20, 2^30, Inf)
size_labels <- c("< 10 KB", "10 KB - 1 MB", "1 - 100 MB", "100 MB - 1 GB", "> 1 GB")
sizes <- moves %>%
  mutate(size_class = cut(size_bytes, size_breaks, size_labels, right = FALSE)) %>%
  group_by(size_class) %>%
  summarise(files = n(), gb = sum(size_bytes) / 2^30, .groups = "drop") %>%
  complete(size_class, fill = list(files = 0, gb = 0)) %>%
  mutate(files_share = files / sum(files), gb_share = gb / sum(gb), size_class = fct_rev(size_class))
print(sizes)

p_sizes <- hbar(sizes, "files_share", "size_class",
                sprintf("%s (%s)", fmt_pct(sizes$files_share), fmt_files(sizes$files)),
                "Share of files to move", axis_labels = percent) +
  hbar(sizes, "gb_share", "size_class",
       sprintf("%s (%s)", fmt_pct(sizes$gb_share), fmt_size(sizes$gb)),
       "Share of space to free", axis_labels = percent) +
  plot_annotation(
    title = "Files to move by size: share of files vs share of space",
    subtitle = sprintf("%s files, %s in total", fmt_files(moved_files), fmt_size(moved_gb)),
    caption = basename(plan_path), theme = theme_plan
  )
print(p_sizes)
export_plot(p_sizes, "quarantine_move_size_classes", height = 4.5)

# ---- 3. Copies per group (+ top groups CSV) --------------------------------------

groups <- plan %>%
  group_by(md5) %>%
  summarise(
    copies   = n(),
    moves    = sum(action == "MOVE"),
    gb_moved = sum(size_bytes[action == "MOVE"]) / 2^30,
    size_one = first(size_bytes),
    keeper   = first(keeper_path[action == "KEEP"]),
    .groups  = "drop"
  )
copy_breaks <- c(2, 3, 6, 11, 51, 201, 1001, Inf)
copy_labels <- c("2", "3-5", "6-10", "11-50", "51-200", "201-1,000", "> 1,000")
copies <- groups %>%
  mutate(bin = cut(copies, copy_breaks, copy_labels, right = FALSE)) %>%
  group_by(bin) %>%
  summarise(groups = n(), files = sum(copies), .groups = "drop") %>%
  complete(bin, fill = list(groups = 0, files = 0)) %>%
  mutate(bin = fct_rev(bin))
print(copies)

largest <- groups %>% slice_max(copies, n = 1, with_ties = FALSE)
p_copies <- hbar(copies, "groups", "bin", fmt_files(copies$groups), "Number of groups",
                 y_title = "Identical copies per group") +
  hbar(copies, "files", "bin", fmt_files(copies$files), "Number of files in those groups") +
  plot_annotation(
    title = "How many identical copies each file has",
    subtitle = sprintf("%s groups | median %s copies, mean %.1f | largest group: %s copies of %s",
                       fmt_files(nrow(groups)), median(groups$copies), mean(groups$copies),
                       fmt_files(largest$copies),
                       ifelse(largest$size_one == 0, "an empty file (all skipped)",
                              paste("a", fmt_size(largest$size_one / 2^30), "file"))),
    caption = basename(plan_path), theme = theme_plan
  )
print(p_copies)
export_plot(p_copies, "quarantine_copies_per_group", height = 4.5)

top_groups <- bind_rows(
  `most copies` = groups %>% slice_max(copies, n = top_n, with_ties = FALSE),
  `most space`  = groups %>% slice_max(gb_moved, n = top_n, with_ties = FALSE),
  .id = "ranking"
) %>% mutate(gb_moved = round(gb_moved, 3))
print(top_groups %>% select(ranking, copies, gb_moved, keeper), n = 2 * top_n)
write_csv(top_groups, file.path(output_folder, "quarantine_top_groups.csv"))

# ---- 4. Where the files to move are (top folders) --------------------------------

folders <- moves %>%
  group_by(folder) %>%
  summarise(files = n(), gb = sum(size_bytes) / 2^30, .groups = "drop")
print(folders %>% arrange(desc(files)), n = top_n)

top_files <- folders %>% slice_max(files, n = top_n, with_ties = FALSE) %>% mutate(folder = fct_reorder(folder, files))
top_space <- folders %>% slice_max(gb, n = top_n, with_ties = FALSE) %>% mutate(folder = fct_reorder(folder, gb))
p_folders <- hbar(top_files, "files", "folder", fmt_files(top_files$files), sprintf("Top %d by files to move", top_n)) +
  hbar(top_space, "gb", "folder", fmt_size(top_space$gb), sprintf("Top %d by space to free", top_n), axis_labels = fmt_size) +
  plot_annotation(
    title = "Where the files to move are (first two folder levels under the root)",
    subtitle = sprintf("Root: %s", root),
    caption = basename(plan_path), theme = theme_plan
  )
print(p_folders)
export_plot(p_folders, "quarantine_top_folders", width = 13, height = 6)
