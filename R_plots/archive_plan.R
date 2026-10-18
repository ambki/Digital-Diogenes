# Archive plan profile: what `archive_old_files.py plan` would move to
# move_to_archive, in number of files and in storage space: per year, per
# category, per folder, and what stays and why.
#
# How to use: run section "0. Setup" once (it reads the plan - a few minutes
# for a large volume). After that, every other section runs on its own, in
# any order and as often as you like: each depends only on Setup, prints its
# tables and shows its plot. Sections 1 and 2 also read the plan's listing
# (the first time they need it). The last line of each section
# (export_plot/write_csv) exports it into output_folder - run it, or skip it.
# In RStudio, put the cursor in a section and press Ctrl+Alt+T (Run current
# section).
#
# Input: an archive_plan_<timestamp>.csv written by `archive_old_files.py
# plan`, with its archive_plan_<timestamp>_settings.json next to it (for the
# root, the date range, the categories and the listing the plan was made
# from). Sections 1 and 2 also read that listing (the raw_csv in the
# settings, or raw_path below), to put the plan in proportion to the whole
# volume.
#
# Memory: the plan and the listing are read in chunks; each chunk is reduced
# to a few small columns (paths become their top folder) before the next one
# is read, and the listing is only kept as totals per year.
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

# Configuration. Same pattern as the other R scripts: the block for the
# volume you want goes last (later assignments win).
# raw_path: the listing the plan was made from. NA = the raw_csv recorded in
# the plan's settings (set it only if that file has moved since).

##Volume A
plan_path     <- r"(C:\path\to\storage_analysis\Volume_A\archive_dry\archive_plan_YYYYMMDD_HHMMSS.csv)"
raw_path      <- NA
output_folder <- r"(C:\path\to\storage_analysis\Volume_A\archive_dry)"

##Volume B
plan_path     <- r"(C:\path\to\storage_analysis\Volume_B\archive_dry\archive_plan_YYYYMMDD_HHMMSS.csv)"
raw_path      <- NA
output_folder <- r"(C:\path\to\storage_analysis\Volume_B\archive_dry)"

top_n      <- 15        # folders shown in the "top" charts
chunk_rows <- 500000    # rows read at a time (memory vs. speed)

# Plot style: validated default palette (light mode), categorical slots in
# fixed order, ink for all text, hairline grid.
surface <- "#fcfcfb"
ink     <- "#0b0b0b"
ink2    <- "#52514e"
muted   <- "#898781"
gridcol <- "#e1e0d9"
neutral <- "#c3c2b7"
series  <- c("#2a78d6", "#eb6834", "#1baf7a", "#eda100")  # blue, orange, aqua, yellow
move_colours <- c("Moves to the archive" = series[1], "Stays" = neutral)

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
                          ifelse(gb >= 1, sprintf("%.0f GB", gb),
                          ifelse(gb * 1024 < 0.1, "<0.1 MB", sprintf("%.1f MB", gb * 1024)))))
fmt_pct   <- function(x) ifelse(x < 0.001 & x > 0, "<0.1%", percent(x, accuracy = 0.1))

# Axis for a size in GB: round ticks in the unit that fits the largest value.
size_axis <- function(max_gb) {
  unit <- if (max_gb >= 1024) c(1 / 1024, " TB") else if (max_gb >= 1) c(1, " GB") else c(1024, " MB")
  list(labels = label_number(scale = as.numeric(unit[1]), suffix = unit[2], big.mark = ","),
       breaks = function(limits) pretty(limits * as.numeric(unit[1])) / as.numeric(unit[1]))
}

# Export a plot as <output_folder>/<name>.png (the last line of each section).
export_plot <- function(plot, name, width = 10, height = 6) {
  dir.create(output_folder, showWarnings = FALSE, recursive = TRUE)
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
    scale_fill_manual(values = c(series, neutral), drop = FALSE) +
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

# Horizontal bars split into "Moves" / "Stays", one panel for files and one for space.
move_stay_panels <- function(data, y, title, subtitle, caption) {
  panel <- function(x, panel_title, axis) {
    ggplot(data, aes(x = .data[[x]], y = .data[[y]], fill = fate)) +
      geom_col(width = 0.6, position = position_stack(reverse = TRUE)) +
      scale_fill_manual(values = move_colours, drop = FALSE) +
      scale_x_continuous(labels = axis$labels, breaks = axis$breaks, expand = expansion(mult = c(0, 0.05))) +
      labs(title = panel_title, x = NULL, y = NULL) + theme_plan
  }
  ((panel("files", "Files", list(labels = label_comma(), breaks = waiver())) +
      theme(plot.margin = margin(5, 30, 5, 5))) +
     (panel("gb", "Space", size_axis(max(data %>% group_by(.data[[y]]) %>% summarise(g = sum(gb)) %>% pull(g)))) +
        theme(axis.text.y = element_blank())) +
     plot_layout(guides = "collect") +
     plot_annotation(title = title, subtitle = subtitle, caption = caption, theme = theme_plan)) &
    theme(legend.position = "top", legend.justification = "left")
}

settings   <- fromJSON(sub("\\.csv$", "_settings.json", plan_path))
root       <- settings$root
prefix_len <- nchar(sub("[\\\\/]+$", "", root)) + 2  # root (without a trailing "\") + "\"
if (is.na(raw_path)) raw_path <- settings$raw_csv
selection  <- if (!is.null(settings$before)) sprintf("modified before %s", settings$before) else
                sprintf("modified from %s to %s (end excluded)", settings$from, settings$to)
selection  <- sprintf("%s (UTC) | categories: %s | label: %s",
                      selection, paste(settings$categories, collapse = ", "), settings$archive_label)

# Path relative to the root; its folder (file name dropped); first two folder levels.
rel_path   <- function(paths) substring(paths, prefix_len)
rel_dir    <- function(paths) {
  rel <- rel_path(paths)
  ifelse(grepl("\\", rel, fixed = TRUE), sub("\\\\[^\\\\]*$", "", rel), "(root)")
}
top_folder <- function(dirs) sub("^([^\\\\]*(\\\\[^\\\\]*)?).*$", "\\1", dirs)
year_of    <- function(stamps) {
  year <- substr(stamps, 1, 4)
  ifelse(year == "" | year == "1970", "unknown", year)
}

reduce_chunk <- function(x, pos) {
  is_folder <- x$kind == "folder"
  tibble(
    action   = x$action,
    reason   = x$reason,
    kind     = x$kind,
    role     = x$role,
    category = ifelse(x$category == "", "(no md5)", x$category),
    size_bytes = suppressWarnings(as.numeric(x$size_bytes)),
    year     = year_of(x$modified),
    folder   = top_folder(ifelse(is_folder, rel_path(x$original_path), rel_dir(x$original_path)))
  )
}

cat(sprintf("Reading plan in chunks of %s rows: %s\n", fmt_files(chunk_rows), plan_path))
plan <- read_csv_chunked(
  plan_path, DataFrameCallback$new(reduce_chunk), chunk_size = chunk_rows,
  col_types = cols_only(
    action = col_character(), reason = col_character(), kind = col_character(),
    role = col_character(), category = col_character(), size_bytes = col_character(),
    modified = col_character(), original_path = col_character()
  ),
  na = character()  # keep "" as "" (an empty category/reason is not missing data)
) %>% mutate(size_bytes = coalesce(size_bytes, 0))

files_in_plan <- plan %>% filter(kind %in% c("file", "system", "note"))
moves         <- files_in_plan %>% filter(action == "MOVE")
moved_files   <- nrow(moves)
moved_gb      <- sum(moves$size_bytes) / 2^30
folders_gone  <- plan %>% filter(kind == "folder", action == "MOVE")
invisible(gc())
cat(sprintf("Plan loaded: %s rows, %s files to move (%s), %s folders to remove.\n",
            fmt_files(nrow(plan)), fmt_files(moved_files), fmt_size(moved_gb), fmt_files(nrow(folders_gone))))
cat(selection, "\n")

# The listing, as totals per year (read once, the first time a section needs it).
.cache <- new.env()
get_listing <- function() {
  if (!exists("listing", envir = .cache, inherits = FALSE)) {
    if (!file.exists(raw_path)) stop(sprintf("Listing not found: %s (set raw_path)", raw_path))
    cat(sprintf("Reading the listing in chunks: %s\n", raw_path))
    listing <- read_csv_chunked(
      raw_path,
      DataFrameCallback$new(function(x, pos) {
        x %>% group_by(year = year_of(mtime)) %>%
          summarise(files = n(), gb = sum(size_gb, na.rm = TRUE), .groups = "drop")
      }),
      chunk_size = chunk_rows, na = character(),
      col_types = cols_only(mtime = col_character(), size_gb = col_double())
    ) %>% group_by(year) %>% summarise(files = sum(files), gb = sum(gb), .groups = "drop")
    assign("listing", listing, envir = .cache)
  }
  get("listing", envir = .cache)
}

# ---- 1. What the plan does, in files and in space ------------------------------

share_levels <- c("Moves to the archive", "Stays: in range, category not selected",
                  "Stays: in range, skipped (see section 5)", "Stays: outside the date range")
in_plan <- files_in_plan %>%
  mutate(category = case_when(
    action == "MOVE"                    ~ share_levels[1],
    reason == "CATEGORY_NOT_SELECTED"   ~ share_levels[2],
    TRUE                                ~ share_levels[3]
  )) %>%
  group_by(category) %>% summarise(files = n(), gb = sum(size_bytes) / 2^30, .groups = "drop")
volume <- get_listing() %>% summarise(files = sum(files), gb = sum(gb))
share <- bind_rows(in_plan, tibble(
  category = share_levels[4],
  files = volume$files - sum(in_plan$files), gb = volume$gb - sum(in_plan$gb)
)) %>% mutate(category = factor(category, levels = share_levels))
print(share)

p_share <- share_bars(
  share, "category",
  title    = "What the plan moves: share of files vs share of space",
  subtitle = sprintf("Moves: %s files (%s of the volume) = %s (%s of the volume)\n%s",
                     fmt_files(moved_files), fmt_pct(moved_files / volume$files),
                     fmt_size(moved_gb), fmt_pct(moved_gb / volume$gb), selection),
  caption  = sprintf("%s + %s", basename(plan_path), basename(raw_path))
)
print(p_share)
export_plot(p_share, "archive_volume_share", height = 4.5)

# ---- 2. By year modified: what moves and what stays ------------------------------

# Notes and system files that follow their emptied folder move whatever their
# date (usually recent), so they get their own colour.
year_colours <- c("Moves: in the date range" = series[1], "Moves: follows its emptied folder" = series[3],
                  "Stays" = neutral)
moved_by_year <- moves %>%
  mutate(fate = ifelse(reason == "FOLLOWS_FOLDER", names(year_colours)[2], names(year_colours)[1])) %>%
  group_by(year, fate) %>% summarise(files = n(), gb = sum(size_bytes) / 2^30, .groups = "drop")
stays_by_year <- get_listing() %>%
  left_join(moved_by_year %>% group_by(year) %>% summarise(m_files = sum(files), m_gb = sum(gb)), by = "year") %>%
  transmute(year, fate = "Stays",
            files = pmax(files - coalesce(m_files, 0), 0), gb = pmax(gb - coalesce(m_gb, 0), 0))
years <- bind_rows(moved_by_year, stays_by_year) %>%
  mutate(fate = factor(fate, levels = names(year_colours)),
         year = factor(year, levels = sort(unique(year))))
print(years %>% pivot_wider(names_from = fate, values_from = c(files, gb), values_fill = 0), n = Inf)

year_panel <- function(column, panel_title, axis) {
  ggplot(years, aes(x = year, y = .data[[column]], fill = fate)) +
    geom_col(width = 0.75, position = position_stack(reverse = TRUE)) +
    scale_fill_manual(values = year_colours) +
    scale_y_continuous(labels = axis$labels, breaks = axis$breaks, expand = expansion(mult = c(0, 0.05))) +
    scale_x_discrete(guide = guide_axis(check.overlap = TRUE)) +
    labs(title = panel_title, x = NULL, y = NULL) + theme_plan +
    theme(panel.grid.major.y = element_line(colour = gridcol, linewidth = 0.3),
          panel.grid.major.x = element_blank())
}
max_year_gb <- max(years %>% group_by(year) %>% summarise(g = sum(gb)) %>% pull(g))
p_years <- (year_panel("files", "Files", list(labels = label_comma(), breaks = waiver())) /
              year_panel("gb", "Space", size_axis(max_year_gb))) +
  plot_layout(guides = "collect") +
  plot_annotation(
    title = "Files per year last modified: what moves to the archive and what stays",
    subtitle = selection,
    caption = sprintf("%s + %s | \"unknown\": the scan couldn't read the file's date",
                      basename(plan_path), basename(raw_path)),
    theme = theme_plan
  ) & theme(legend.position = "top", legend.justification = "left")
print(p_years)
export_plot(p_years, "archive_by_year", width = 12, height = 7)

# ---- 3. By category: in the date range, what moves and what stays ----------------
# Data files in the date range only (categories don't apply to notes and system files).

categories <- files_in_plan %>%
  filter(kind == "file") %>%
  mutate(fate = factor(ifelse(action == "MOVE", "Moves to the archive", "Stays"), levels = names(move_colours))) %>%
  group_by(category, fate) %>%
  summarise(files = n(), gb = sum(size_bytes) / 2^30, .groups = "drop") %>%
  mutate(category = fct_rev(factor(category, levels = intersect(
    c("unique", "keeper", "duplicate", "unreadable", "(no md5)"), unique(category)))))
print(categories, n = Inf)

p_categories <- move_stay_panels(
  categories, "category",
  title    = "Files in the date range, by category",
  subtitle = sprintf("unique: md5 once | keeper: oldest copy of a group | duplicate: the other copies\n%s", selection),
  caption  = basename(plan_path)
)
print(p_categories)
export_plot(p_categories, "archive_by_category", width = 12, height = 4)

# ---- 4. Where the files to move are (top folders) --------------------------------

folders <- moves %>%
  group_by(folder) %>%
  summarise(files = n(), gb = sum(size_bytes) / 2^30, .groups = "drop") %>%
  left_join(folders_gone %>% count(folder, name = "folders_removed"), by = "folder") %>%
  mutate(folders_removed = coalesce(folders_removed, 0L))
print(folders %>% arrange(desc(gb)), n = top_n)

top_space <- folders %>% slice_max(gb, n = top_n, with_ties = FALSE) %>% mutate(folder = fct_reorder(folder, gb))
top_files <- folders %>% slice_max(files, n = top_n, with_ties = FALSE) %>% mutate(folder = fct_reorder(folder, files))
p_folders <- hbar(top_space, "gb", "folder", fmt_size(top_space$gb), sprintf("Top %d by space to move", top_n),
                  axis_labels = size_axis(max(top_space$gb))$labels) +
  hbar(top_files, "files", "folder", fmt_files(top_files$files), sprintf("Top %d by files to move", top_n)) +
  plot_annotation(
    title = "Where the files to move are (first two folder levels under the root)",
    subtitle = sprintf("Root: %s | %s folder(s) will be removed from the storage once emptied",
                       root, fmt_files(nrow(folders_gone))),
    caption = basename(plan_path), theme = theme_plan
  )
print(p_folders)
export_plot(p_folders, "archive_top_folders", width = 13, height = 6)

# ---- 5. What stays in the date range, and why (+ CSV) ----------------------------

skips <- files_in_plan %>%
  filter(action == "SKIP") %>%
  group_by(reason) %>%
  summarise(files = n(), gb = sum(size_bytes) / 2^30, .groups = "drop") %>%
  arrange(desc(files))
print(skips, n = Inf)

if (nrow(skips) > 0) {
  skips_plot <- skips %>% mutate(reason = fct_reorder(reason, files))
  p_skips <- hbar(skips_plot, "files", "reason", fmt_files(skips_plot$files), "Files") +
    hbar(skips_plot, "gb", "reason", fmt_size(skips_plot$gb), "Space",
         axis_labels = size_axis(max(skips_plot$gb))$labels) +
    plot_annotation(
      title = "Files in the date range that stay, per reason",
      subtitle = "CATEGORY_NOT_SELECTED and EXCLUDED_PATTERN can be switched to MOVE in the plan; the others can't",
      caption = basename(plan_path), theme = theme_plan
    )
  print(p_skips)
  export_plot(p_skips, "archive_skips", width = 12, height = 4)
}
write_csv(skips %>% mutate(gb = round(gb, 3)), file.path(output_folder, "archive_skips.csv"))
