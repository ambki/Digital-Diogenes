# Storage composition by year: unique vs. duplicate vs. zip vs. unmixed data.
#
# Inputs are the CSV outputs of duplicates_check.py and
# elimination_file_candidates.py run with --excel against the SAME folder:
#   - duplicates_check.py's companion "..._raw.csv"        (needs: path, mtime, md5, size_gb)
#   - elimination_file_candidates.py's "..._zip_files.csv"      (needs: path)
#   - elimination_file_candidates.py's "..._unmixed_files.csv"  (needs: path)
# The last section also compares two storages' "..._raw.csv" exports against
# each other by md5 (see "Cross-storage duplicates" at the end).
#
# Install once: install.packages(c("readr", "dplyr", "lubridate", "ggplot2", "scales", "forcats", "tidyr", "plotly", "htmlwidgets", "patchwork", "ggtext", "readxl"))

library(readr)
library(dplyr)
library(lubridate)
library(ggplot2)
library(scales)
library(forcats)
library(tidyr)
library(plotly)
library(htmlwidgets)
library(patchwork)
library(ggtext)
library(readxl)

# ---- Configuration -----------------------------------------------------
# One folder per source, plus just the filename within it

##Volume A
duplicates_folder  <- r"(C:\path\to\storage_analysis\Volume_A\duplicates_analysis)"
elimination_folder <- r"(C:\path\to\storage_analysis\Volume_A\elimination_results)"
output_folder       <- r"(C:\path\to\storage_analysis\Volume_A)"

duplicates_raw_file  <- "duplicates_volume_a_raw.csv"
elimination_raw_file <- "elimination_candidates_volume_a_raw.csv"  # fallback if duplicates_raw_file isn't available yet
zip_file             <- "elimination_candidates_volume_a_zip_files.csv"
unmixed_file         <- "elimination_candidates_volume_a_unmixed_files.csv"

##Volume B
duplicates_folder  <- r"(C:\path\to\storage_analysis\Volume_B\duplicates_analysis)"
elimination_folder <- r"(C:\path\to\storage_analysis\Volume_B\elimination_results)"
output_folder       <- r"(C:\path\to\storage_analysis\Volume_B)"

duplicates_raw_file  <- "duplicates_volume_b_raw.csv"
elimination_raw_file <- "elimination_candidates_volume_b_raw.csv"  # fallback if duplicates_raw_file isn't available yet
zip_file             <- "elimination_candidates_volume_b_zip_files.csv"
unmixed_file         <- "elimination_candidates_volume_b_unmixed_files.csv"


cutoff_year <- 2016   # years strictly before this become one "old data" bar

# Full paths, built from the folders + filenames above - nothing below this
# needs editing when only the folders/filenames change.
duplicates_raw_path  <- file.path(duplicates_folder, duplicates_raw_file)
elimination_raw_path <- file.path(elimination_folder, elimination_raw_file)
zip_files_path        <- file.path(elimination_folder, zip_file)
unmixed_files_path    <- file.path(elimination_folder, unmixed_file)

# Output files - generic names, written into output_folder.
output_png             <- file.path(output_folder, "storage_by_year.png")
output_percent_png     <- file.path(output_folder, "storage_by_year_percent.png")
output_html            <- file.path(output_folder, "storage_by_year_interactive.html")
output_top_wasted_png  <- file.path(output_folder, "top_wasted_directories.png")
output_cumulative_png  <- file.path(output_folder, "cumulative_unique_growth.png")

# ---- Load ---------------------------------------------------------------
# Accepts either the current CSV outputs or an older .xlsx report from
# before the CSV switch - duplicates_check.py's raw file used to be
# "..._raw.xlsx" (sheet "all_files"), and elimination_file_candidates.py's
# zip/unmixed listings used to live as "zip_files"/"unmixed_files" sheets in
# one workbook rather than separate CSVs. The column schemas are identical
# either way - only the container format changed - so reading either into
# the same shape is enough; no other code below needs to know which it got.

read_table_flex <- function(path, sheet) {
  ext <- tolower(tools::file_ext(path))
  if (ext == "csv") {
    read_csv(path, show_col_types = FALSE)
  } else if (ext %in% c("xlsx", "xls")) {
    read_excel(path, sheet = sheet)
  } else {
    stop(sprintf("Unrecognized file extension for '%s' - expected .csv or .xlsx", path))
  }
}

# Prefer duplicates_check.py's raw export (has md5, enables duplicate
# detection); fall back to elimination_file_candidates.py's own raw export
# (same filename/path/mtime/size_gb columns, just no md5) if the duplicates
# report isn't available yet - e.g. an interim report run before duplicates
# data has arrived. Falling back here is what makes that fallback actually
# happen; has_duplicate_data further down is what makes the missing md5
# column not break anything once it's loaded.
if (file.exists(duplicates_raw_path)) {
  raw_source <- duplicates_raw_path
} else {
  cat(sprintf(
    "\n'%s' not found - falling back to elimination's raw export:\n  %s\n",
    duplicates_raw_path, elimination_raw_path
  ))
  raw_source <- elimination_raw_path
}

files <- read_table_flex(raw_source, sheet = "all_files") %>%
  mutate(
    mtime = as.POSIXct(mtime, format = "%Y-%m-%d %H:%M:%S", tz = "UTC"),
    year  = year(mtime)
  ) %>%
  filter(!is.na(mtime))  # files whose age couldn't be read are excluded from a by-year view

zip_paths     <- read_table_flex(zip_files_path, sheet = "zip_files")$path
unmixed_paths <- read_table_flex(unmixed_files_path, sheet = "unmixed_files")$path

# ---- Manual mtime-year corrections ----------------------------------------
# Some files carry a corrupted mtime (e.g. a 2038/2028 that's really 2009,
# going by the folder name) - filesystem/migration corruption, not real
# content age. This only changes what gets plotted below; it never touches
# duplicates_raw_path/zip_files_path/unmixed_files_path or the files
# themselves on disk.
#
# First, a diagnostic to help you find every case, not just the ones you've
# already spotted: any file whose year is implausibly in the future.
suspicious <- files %>%
  filter(year > year(Sys.Date())) %>%
  distinct(year, path) %>%
  arrange(year)
if (nrow(suspicious) > 0) {
  cat(sprintf("\n%d file(s) with an implausible future mtime year - add corrections below:\n", nrow(suspicious)))
  print(suspicious, n = Inf)
}

# Add one row per confirmed case: a path substring to match, and the year it
# should actually be (fixed = TRUE below means plain substring matching, no
# regex special characters to worry about in a Windows path).
year_overrides <- list(
  # list(pattern = "\\\\server\\share\\SomeFolder\\2009\\", year = 2009),
  # list(pattern = "SomeFolderName", year = 2009),
)


for (ov in year_overrides) {
  matches <- grepl(ov$pattern, files$path, fixed = TRUE)
  files$year[matches] <- ov$year
}

# ---- Classify each file into exactly one bucket --------------------------
# Priority order: duplicate > zip > unmixed > unique, so the stacked segments
# always sum to the true total size for that year (a file could otherwise
# qualify for more than one group). "Duplicate" means specifically the
# redundant copies - within each identical-content (md5) group, the OLDEST
# copy is kept as "Unique" and the rest are "Duplicate", mirroring
# duplicates_check.py's own oldest-copy-is-kept logic.
#
# If `duplicates_raw_path` doesn't have an "md5" column (e.g. for an interim
# report run before duplicates_check.py's output is available - elimination's
# own raw export has no md5, since it never hashes), duplicate detection is
# skipped entirely rather than erroring: every file counts as not-duplicate,
# giving a Unique/Zip/Unmixed/Old breakdown now. Point `duplicates_raw_path`
# at the real duplicates_check.py raw export later and this picks duplicate
# detection back up automatically - no other change needed.

has_duplicate_data <- "md5" %in% names(files)
if (!has_duplicate_data) {
  cat(
    "\nNo 'md5' column in duplicates_raw_path - treating this as an interim ",
    "report with no duplicate data yet (Unique/Zip/Unmixed/Old only). The ",
    "'Duplicate' category and the top-wasted-directories chart will be empty ",
    "until you point duplicates_raw_path at duplicates_check.py's raw export.\n",
    sep = ""
  )
}

if (has_duplicate_data) {
  files <- files %>%
    group_by(md5) %>%
    mutate(
      dup_group_size = n(),
      # 1 = earliest copy in the group (kept); ties broken by original row order
      keep_rank = if_else(!is.na(md5), row_number(mtime), NA_integer_)
    ) %>%
    ungroup() %>%
    mutate(is_duplicate = !is.na(md5) & dup_group_size > 1 & keep_rank > 1)
} else {
  files <- files %>% mutate(is_duplicate = FALSE)
}

files <- files %>%
  mutate(
    is_zip       = path %in% zip_paths,
    is_unmixed   = path %in% unmixed_paths,
    category = case_when(
      is_duplicate ~ "Duplicate",
      is_zip       ~ "Zip",
      is_unmixed   ~ "Unmixed",
      TRUE         ~ "Unique"
    )
  )

# ---- Per-year stack (>= cutoff_year) -------------------------------------

by_year <- files %>%
  filter(year >= cutoff_year) %>%
  group_by(year, category) %>%
  summarise(size_gb = sum(size_gb), .groups = "drop") %>%
  mutate(year_label = as.character(year))

# ---- Everything older collapses into one unsplit bar ---------------------

old_label <- paste0("Pre-", cutoff_year)
old_data <- files %>%
  filter(year < cutoff_year) %>%
  summarise(size_gb = sum(size_gb), .groups = "drop") %>%
  mutate(year_label = old_label, category = "Old data")

plot_data <- bind_rows(old_data, by_year) %>%
  mutate(
    year_label = fct_relevel(factor(year_label), old_label),
    category   = factor(category, levels = c("Old data", "Unique", "Duplicate", "Zip", "Unmixed"))
  )

# ---- Categorical palette (colorblind-validated; see dataviz skill) -------
# "Old data" gets a neutral grey since it's not part of the categorical
# split - it reads as a distinct, deliberately unsplit bar.

palette <- c(
  "Old data"  = "#c3c2b7",
  "Unique"    = "#2a78d6",
  "Duplicate" = "#eb6834",
  "Zip"       = "#1baf7a",
  "Unmixed"   = "#eda100"
)

# ---- Plot -----------------------------------------------------------------

p <- ggplot(plot_data, aes(
    x = year_label, y = size_gb, fill = category,
    # "text" is unused by ggplot's own renderer (harmless "unknown aesthetic"
    # warning on the static PNG) but gives ggplotly() a clean hover tooltip
    # further down, without building a second plot from scratch.
    text = sprintf("%s<br>%s: %.1f GB", year_label, category, size_gb)
  )) +
  geom_col(color = "#fcfcfb", linewidth = 0.3, width = 0.7) +
  scale_fill_manual(values = palette, name = NULL) +
  scale_y_continuous(labels = label_comma(suffix = " GB"), expand = expansion(mult = c(0, 0.05))) +
  labs(
    title = "Storage composition by year",
    subtitle = paste0("Unique vs. duplicate vs. zip vs. unmixed files, pre-", cutoff_year, " grouped as old data"),
    x = NULL, y = "Total size"
  ) +
  theme_minimal(base_size = 12) +
  theme(
    panel.grid.minor   = element_blank(),
    panel.grid.major.x = element_blank(),
    panel.grid.major.y = element_line(color = "#e1e0d9"),
    axis.text.x  = element_text(angle = 45, hjust = 1),
    plot.title    = element_text(face = "bold", hjust = 0.5),
    plot.subtitle = element_text(hjust = 0.5),
    legend.position = "top"
    # legend.justification left at its default (centered) - the box has
    # since moved down and to a fixed inset position, so it no longer needs
    # the legend pushed aside to make room for it.
  )

# ---- Quick reclaimable-space summary ---------------------------------------
# total_gb/old_gb span every year (the true whole-scan total and everything
# pre-cutoff, regardless of category). The other four are scoped to
# year >= cutoff_year ONLY - matching exactly what the chart itself does,
# where "Old data" swallows every pre-cutoff file regardless of category, and
# Unique/Duplicate/Zip/Unmixed only ever apply to post-cutoff years. Without
# that scoping, a pre-cutoff unique file would get counted under both
# "Unique" and "Old data", and all five percentages would sum past 100%.
# With it, they're a true non-overlapping partition of total_gb.

summary_stats <- files %>%
  summarise(
    total_gb     = sum(size_gb),
    old_gb       = sum(size_gb[year < cutoff_year]),
    unique_gb    = sum(size_gb[year >= cutoff_year & category == "Unique"]),
    duplicate_gb = sum(size_gb[year >= cutoff_year & is_duplicate]),
    zip_gb       = sum(size_gb[year >= cutoff_year & is_zip & !is_duplicate]),
    unmixed_gb   = sum(size_gb[year >= cutoff_year & is_unmixed & !is_duplicate & !is_zip])
  )

fmt_gb <- label_comma(accuracy = 0.1, suffix = " GB")
pct_of_total <- function(x) 100 * x / summary_stats$total_gb

cat(sprintf(
  "\nTotal scanned: %s\nUnique: %s (%.1f%%)\nOld data (pre-%d): %s (%.1f%%)\nDuplicates: %s (%.1f%%)\nZip candidates: %s (%.1f%%)\nUnmixed candidates: %s (%.1f%%)\n",
  fmt_gb(summary_stats$total_gb),
  fmt_gb(summary_stats$unique_gb),    pct_of_total(summary_stats$unique_gb),
  cutoff_year, fmt_gb(summary_stats$old_gb),       pct_of_total(summary_stats$old_gb),
  fmt_gb(summary_stats$duplicate_gb), pct_of_total(summary_stats$duplicate_gb),
  fmt_gb(summary_stats$zip_gb),       pct_of_total(summary_stats$zip_gb),
  fmt_gb(summary_stats$unmixed_gb),   pct_of_total(summary_stats$unmixed_gb)
))

# ---- Summary box, inset to the right of the legend --------------------------
# A small bordered "KPI box" overlaid directly onto the chart via
# patchwork::inset_element(), instead of stacked as a separate full-width
# row above it (that collided with the title). `align_to = "full"` positions
# it relative to the whole rendered figure (title/legend included), not just
# the bars panel, so top/bottom line up with the legend row, not the bars.
#
# NOTE: these left/bottom/right/top fractions are a starting guess, not a
# rendered/verified fit (no R available to preview here) - nudge them after
# seeing the actual output if the box doesn't sit flush against the legend.

# Category names are colored to match their bars via inline HTML spans,
# which geom_richtext()'s markdown parser supports (e.g.
# "<span style='color:#2a78d6'>text</span>"). "Old data"'s bar color
# (#c3c2b7, a pale grey) reads fine as a bordered filled rectangle but would
# be nearly invisible as small text on the box's near-white background, so
# it gets a darker grey here instead of the exact bar hex - everything else
# reuses `palette` unchanged.
# Bold markdown ("**...**") nests fine inside the HTML color span - both get
# applied together, which also helps the lighter/mid-saturation hues (Zip,
# Unmixed) read less faint at small size than color alone.
name_color <- function(hex) sprintf("<span style='color:%s'>**", hex)
old_text_color <- "#78766d"

category_lines <- c(
  sprintf("%sUnique**</span>: %s (**%.1f%%**)", name_color(palette[["Unique"]]), fmt_gb(summary_stats$unique_gb), pct_of_total(summary_stats$unique_gb)),
  sprintf("%sOld**</span> (pre-%d): %s (**%.1f%%**)", name_color(old_text_color), cutoff_year, fmt_gb(summary_stats$old_gb), pct_of_total(summary_stats$old_gb)),
  sprintf("%sDuplicates**</span>: %s (**%.1f%%**)", name_color(palette[["Duplicate"]]), fmt_gb(summary_stats$duplicate_gb), pct_of_total(summary_stats$duplicate_gb)),
  sprintf("%sZip**</span>: %s (**%.1f%%**)", name_color(palette[["Zip"]]), fmt_gb(summary_stats$zip_gb), pct_of_total(summary_stats$zip_gb)),
  sprintf("%sUnmixed**</span>: %s (**%.1f%%**)", name_color(palette[["Unmixed"]]), fmt_gb(summary_stats$unmixed_gb), pct_of_total(summary_stats$unmixed_gb))
)
# Numbered/indented by hand rather than via Markdown's "1. item" list syntax -
# geom_richtext()'s parser is a minimal inline one, so it won't reliably turn
# that into a real indented, auto-numbered block list. "&nbsp;" is used for
# the indent since plain spaces collapse in HTML/Markdown rendering the same
# way a plain "\n" does (both are treated as regular whitespace, not layout).
numbered_lines <- sprintf("&nbsp;&nbsp;%d. %s", seq_along(category_lines), category_lines)

# "**...**" is Markdown bold, rendered by ggtext::geom_richtext() below - a
# plain annotate("text", ...) can only apply one font weight to the whole
# string, so bolding just the percentages needs richtext's markdown parsing.
# "<br>" is what actually produces line breaks here - "\n" would not.
summary_text <- paste(
  c(sprintf("Total: %s", fmt_gb(summary_stats$total_gb)), numbered_lines),
  collapse = "<br>"
)

summary_box <- ggplot() +
  annotate(
    # x = -1 anchors the text at the LEFT edge of the xlim(-1, 1) range below;
    # hjust = 0 then grows it rightward from there. x = 0 (the old value) is
    # the center of that range, so hjust = 0 only looked centered, not
    # flush-left - hjust alone can't do that without an anchor at the edge.
    "richtext", x = -1, y = 0, label = summary_text,
    hjust = 0, vjust = 0.5, size = 6, color = "#0b0b0b", lineheight = 1.4,
    # richtext draws its own background/border box by default - suppressed
    # here since plot.background below already provides the box look.
    fill = NA, label.color = NA
  ) +
  xlim(-1, 1) + ylim(-1, 1) +
  theme_void() +
  theme(
    plot.background = element_rect(fill = "#fcfcfb", color = "#c3c2b7", linewidth = 0.6),
    plot.margin = margin(6, 10, 6, 10)
  )

p_with_summary <- p + inset_element(
  summary_box,
  left = 0.12, right = 0.385, #Right and top are exagerated for ggsave
  bottom = 0.56, top = 0.795,
  align_to = "full"
)

print(p_with_summary)
ggsave(output_png, p_with_summary, width = 16, height = 10, dpi = 150)

# ---- 100%-stacked version: composition as a proportion, not absolute GB ----
# Absolute GB (the main plot) shows where the bulk of the data sits; this
# shows which years are proportionally worst for waste, independent of how
# much total data landed that year. "Old data" is excluded here - it's a
# single undifferentiated bucket, so its bar would trivially be 100% grey.

p_percent <- ggplot(by_year, aes(x = factor(year), y = size_gb, fill = category)) +
  geom_col(position = "fill", color = "#fcfcfb", linewidth = 0.3, width = 0.7) +
  scale_fill_manual(values = palette[c("Unique", "Duplicate", "Zip", "Unmixed")], name = NULL) +
  scale_y_continuous(labels = percent_format(), expand = expansion(mult = c(0, 0.02))) +
  labs(
    title = "Storage composition by year (% of that year's data)",
    x = NULL, y = NULL
  ) +
  theme_minimal(base_size = 12) +
  theme(
    panel.grid.minor   = element_blank(),
    panel.grid.major.x = element_blank(),
    axis.text.x  = element_text(angle = 45, hjust = 1),
    plot.title   = element_text(face = "bold"),
    legend.position = "top"
  )
#getwd()
ggsave(output_percent_png, p_percent, width = 12, height = 7, dpi = 150)

# ---- Interactive version (hover for exact GB per segment) ------------------
# Reuses the same plot object `p` built above - the "text" aesthetic added
# to it earlier is what feeds this tooltip.

interactive_widget <- ggplotly(p, tooltip = "text")
saveWidget(interactive_widget, output_html, selfcontained = TRUE)

# ---- Top 10 directories by wasted (duplicate) space -------------------------
# "Wasted" = size of the redundant copies only (the kept/oldest copy per
# md5 group is excluded, same convention as the reclaimable-space summary
# above), grouped by each file's immediate containing folder.

wasted_by_dir <- files %>%
  filter(is_duplicate) %>%
  mutate(dir = dirname(gsub("\\\\", "/", path))) %>%
  group_by(dir) %>%
  summarise(wasted_gb = sum(size_gb), n_files = n(), .groups = "drop") %>%
  arrange(desc(wasted_gb)) %>%
  slice_head(n = 10) %>%
  mutate(
    # Truncate to the path's tail for the chart label - the full path is
    # still in `wasted_by_dir` itself (printed below) if you need it.
    dir_label = if_else(
      nchar(dir) > 60,
      paste0("...", substr(dir, nchar(dir) - 57, nchar(dir))),
      dir
    )
  )

cat("\nTop directories by wasted (duplicate) space:\n")
print(wasted_by_dir %>% select(dir, wasted_gb, n_files), n = Inf)

p_top_wasted <- ggplot(wasted_by_dir, aes(x = fct_reorder(dir_label, wasted_gb), y = wasted_gb)) +
  geom_col(fill = palette[["Duplicate"]], width = 0.7) +
  geom_text(
    aes(label = label_comma(accuracy = 0.1, suffix = " GB")(wasted_gb)),
    hjust = -0.1, size = 3.3, color = "#0b0b0b"
  ) +
  coord_flip(clip = "off") +
  scale_y_continuous(labels = label_comma(suffix = " GB"), expand = expansion(mult = c(0, 0.15))) +
  labs(title = "Top 10 directories by wasted (duplicate) space", x = NULL, y = "Wasted size") +
  theme_minimal(base_size = 12) +
  theme(
    panel.grid.minor   = element_blank(),
    panel.grid.major.y = element_blank(),
    panel.grid.major.x = element_line(color = "#e1e0d9"),
    plot.title = element_text(face = "bold")
  )

ggsave(output_top_wasted_png, p_top_wasted, width = 12, height = 6, dpi = 150)

# ---- Cumulative storage growth over time: stacked bars + total line --------
# A separate chart with its own y-axis (not overlaid on the bar chart above -
# a cumulative running total and a single year's amount are different scales,
# and a dual-axis chart hides more than it shows). Spans every year present
# in the data, not just >= cutoff_year, since this is about the real growth
# trend rather than keeping old years readable as a stacked bar.
#
# Floating text labels along a continuous line collide once years are close
# together - bars fix that structurally, since each label is anchored to its
# own non-overlapping rectangle instead of a crowded line. Each year's bar is
# stacked into:
#   - Unique         - cumulative "category == Unique" total
#   - Zip + Unmixed  - cumulative (!is_duplicate total) - (Unique total),
#     i.e. the rest of the non-duplicate data (real, distinct content that's
#     still an elimination *candidate*, not confirmed waste like a duplicate)
# Both are "as of the start of that year" (the running total before that
# year's own growth was added), same convention as before - the bars would
# otherwise be one step ahead of the label the user asked for. The overlaid
# line marks the combined total (top of each stack) and connects the bars
# into a continuous trajectory between years.

cumulative_start_year <- 2020  # only affects which years the cumulative growth chart's x-axis shows

cumulative_wide <- files %>%
  filter(!is_duplicate) %>%
  mutate(is_unique_only = category == "Unique") %>%
  group_by(year) %>%
  summarise(
    unique_gb     = sum(size_gb[is_unique_only]),
    all_nondup_gb = sum(size_gb),
    .groups = "drop"
  ) %>%
  arrange(year) %>%
  mutate(
    unique_cum        = cumsum(unique_gb),
    all_nondup_cum    = cumsum(all_nondup_gb),
    unique_start      = lag(unique_cum, default = 0),
    all_nondup_start  = lag(all_nondup_cum, default = 0),
    zip_unmixed_start = all_nondup_start - unique_start
  ) %>%
  # Filtered AFTER the cumulative sums, not before - otherwise the running
  # total from the hidden years would be lost, and the first displayed bar
  # would wrongly start from zero instead of the true accumulated total.
  filter(year >= cumulative_start_year)

bar_data <- cumulative_wide %>%
  select(year, Unique = unique_start, `Zip + Unmixed` = zip_unmixed_start) %>%
  pivot_longer(-year, names_to = "segment", values_to = "segment_gb") %>%
  # ggplot stacks the first factor level at the TOP by default - listing
  # "Zip + Unmixed" first puts it on top (away from the x-axis, where it has
  # headroom) and "Unique" at the bottom.
  mutate(segment = factor(segment, levels = c("Zip + Unmixed", "Unique")))

# "Unique" reuses the Unique slot; "Zip + Unmixed" is a combined segment (not
# literally either the Zip or Unmixed category on its own), so it takes the
# same magenta slot used for it as a line previously, rather than reusing
# Zip's or Unmixed's individual color.
bar_palette <- c(
  "Unique"          = palette[["Unique"]],
  "Zip + Unmixed"   = "#e87ba4"
)

# Label ink is picked per segment for contrast against its own fill, not
# reused from the categorical palette - yellow reads strongly against the
# blue "Unique" fill; the magenta "Zip + Unmixed" fill is lighter, so dark
# ink contrasts better there than a light color would.
label_colors <- c(
  "Unique"          = "#FFD400",
  "Zip + Unmixed"   = "#0b0b0b"
)

p_cumulative <- ggplot(bar_data, aes(x = factor(year), y = segment_gb, fill = segment)) +
  geom_col(width = 0.7, color = "#fcfcfb", linewidth = 0.3) +
  geom_text(
    aes(
      label = if_else(segment_gb > 0, label_comma(accuracy = 0.1, suffix = " GB")(segment_gb), ""),
      color = segment
    ),
    position = position_stack(vjust = 0.5), angle = 90, size = 2.8, fontface = "bold"
  ) +
  scale_color_manual(values = label_colors, guide = "none") +
  geom_line(
    data = cumulative_wide,
    aes(x = factor(year), y = all_nondup_start, group = 1),
    inherit.aes = FALSE, color = "#0b0b0b", linewidth = 0.9
  ) +
  geom_point(
    data = cumulative_wide,
    aes(x = factor(year), y = all_nondup_start),
    inherit.aes = FALSE, color = "#0b0b0b", size = 1.6
  ) +
  scale_fill_manual(values = bar_palette, name = NULL) +
  scale_y_continuous(labels = label_comma(suffix = " GB"), expand = expansion(mult = c(0, 0.08))) +
  labs(
    title = "Cumulative storage growth over time",
    subtitle = "Stacked unique vs. zip/unmixed, numbers both as of the start of each year - line marks the combined total",
    x = NULL, y = "Cumulative size (start of year)"
  ) +
  theme_minimal(base_size = 12) +
  theme(
    panel.grid.minor   = element_blank(),
    panel.grid.major.x = element_blank(),
    axis.text.x  = element_text(angle = 45, hjust = 1),
    plot.title   = element_text(face = "bold"),
    legend.position = "top"
  )

ggsave(output_cumulative_png, p_cumulative, width = 12, height = 7, dpi = 150)

# ---- Cross-storage duplicates (from two existing raw exports) --------------
# Same question as `duplicates_check.py --origin A --destination B --mode
# between` - which file contents exist in BOTH storages, regardless of path
# or filename - but answered from two duplicates_raw_file exports that were
# already produced by separate `--folder` runs. Those already carry an md5 per
# file, so nothing is rescanned or re-hashed: this takes minutes, not days.
#
# Independent of the configuration at the top of the script (which only ever
# points at one storage at a time) - set both storages here. Uses
# read_table_flex(), year_overrides, palette, cutoff_year and fmt_gb from
# above, so run the earlier sections first if running this one on its own.
#
# Caveat: each raw export is a snapshot from when its scan ran. A file moved
# or deleted since then still shows up here as it was at scan time.
#
# Split into sections that can each be run on their own, in order the first
# time: only "Cross-storage: load & match" is slow (reading both CSVs), and
# everything after it only reuses `cross_files` - so to tweak a table or the
# figure, just re-run that one section, not the load.

# ---- Cross-storage: configuration -------------------------------------------

cross_root <- r"(C:\path\to\storage_analysis)"
storage_a <- list(label = "Volume A", raw = file.path(cross_root, "Volume_A", "duplicates_analysis", "duplicates_volume_a_raw.csv"))
storage_b <- list(label = "Volume B",  raw = file.path(cross_root, "Volume_B", "duplicates_analysis", "duplicates_volume_b_raw.csv"))
cross_output_folder <- file.path(cross_root, "cross_storage")

output_cross_summary_csv <- file.path(cross_output_folder, "cross_storage_between_summary.csv")
output_cross_dirs_csv    <- file.path(cross_output_folder, "cross_storage_by_directory.csv")
output_cross_dirs_year_csv <- file.path(cross_output_folder, "cross_by_dir_year.csv")
output_cross_png         <- file.path(cross_output_folder, "cross_storage_by_year.png")

# Own colors (same hexes as Unique/Duplicate in `palette` above) rather than
# reading `palette`: if that section hasn't been run in this session,
# `palette` silently resolves to grDevices::palette() - a function - instead.
cross_palette <- c(
  "Only in this storage"  = "#2a78d6",
  "Also in other storage" = "#eb6834"
)

old_label <- paste0("Pre-", cutoff_year)
fmt_gb <- label_comma(accuracy = 0.1, suffix = " GB")
# Add one row per confirmed case: a path substring to match, and the year it
# should actually be (fixed = TRUE below means plain substring matching, no
# regex special characters to worry about in a Windows path).
year_overrides <- list(
  # list(pattern = "\\\\server\\share\\SomeFolder\\2009\\", year = 2009),
  # list(pattern = "SomeFolderName", year = 2009),
)

# ---- Cross-storage: reader function -----------------------------------------
# Only the four columns needed are read (not filename too): at millions of
# rows per storage, every extra text column is a lot of RAM. Unreadable files
# (md5 NA) can't be matched by content, so they're dropped - but counted, so
# a storage with many of them doesn't look better covered than it is.
read_raw_md5 <- function(path, label) {
  keep_cols <- c("path", "mtime", "md5", "size_gb")
  if (tolower(tools::file_ext(path)) == "csv") {
    header <- names(read_csv(path, n_max = 0, show_col_types = FALSE))
    if (!"md5" %in% header) {
      stop(sprintf("'%s' has no md5 column - point it at duplicates_check.py's final raw export, not the pre-hash one.", path))
    }
    df <- read_csv(
      path,
      col_select = all_of(keep_cols),
      col_types = cols(.default = col_character(), size_gb = col_double()),
      show_col_types = FALSE
    )
  } else {
    df <- read_table_flex(path, sheet = "all_files") %>% select(all_of(keep_cols))
  }

  df <- df %>%
    mutate(
      mtime   = as.POSIXct(mtime, format = "%Y-%m-%d %H:%M:%S", tz = "UTC"),
      year    = year(mtime),
      storage = label
    )
  for (ov in year_overrides) {
    df$year[grepl(ov$pattern, df$path, fixed = TRUE)] <- ov$year
  }

  n_unreadable <- sum(is.na(df$md5))
  if (n_unreadable > 0) {
    cat(sprintf("%s: %d unreadable file(s) without md5 left out of the comparison.\n", label, n_unreadable))
  }
  df %>% filter(!is.na(md5))
}

# ---- Cross-storage: load & match (the slow part) ----------------------------
# Produces `cross_files`: every readable file from both storages, one row
# each, with `in_both` = its content (md5) also exists in the other storage.
# All sections below only read `cross_files`, so this needs re-running only
# when the configuration (which storages/files) changes.

cross_inputs_found <- file.exists(c(storage_a$raw, storage_b$raw))
if (!all(cross_inputs_found)) {
  stop("Cross-storage comparison - raw export(s) not found:\n",
       paste0("  ", c(storage_a$raw, storage_b$raw)[!cross_inputs_found], collapse = "\n"))
}
dir.create(cross_output_folder, recursive = TRUE, showWarnings = FALSE)

files_a <- read_raw_md5(storage_a$raw, storage_a$label)
files_b <- read_raw_md5(storage_b$raw, storage_b$label)

shared_md5 <- intersect(files_a$md5, files_b$md5)
cross_files <- bind_rows(files_a, files_b) %>%
  mutate(in_both = md5 %in% shared_md5)
rm(files_a, files_b)

# ---- Cross-storage: between summary (one row per shared md5) ----------------
# Same columns as duplicates_check.py's between_summary: count/size/total/
# wasted plus the oldest copy (the one to keep) and which storage it lives
# in. Paths are " | "-separated rather than a JSON list, so the CSV reads fine
# in Excel too.
# Note total_size_gb is computed BEFORE size_gb is overwritten by the
# per-group scalar - summarise() sees each new column straight away.

between_summary <- cross_files %>%
  filter(in_both) %>%
  group_by(md5) %>%
  summarise(
    count         = n(),
    count_a       = sum(storage == storage_a$label),
    count_b       = sum(storage == storage_b$label),
    total_size_gb = sum(size_gb),
    size_gb       = first(size_gb),
    wasted_gb     = total_size_gb - size_gb,
    .oldest         = order(mtime, na.last = TRUE)[1],
    oldest_path     = path[.oldest],
    oldest_modified = format(mtime[.oldest], "%Y-%m-%d %H:%M:%S"),
    oldest_storage  = storage[.oldest],
    paths_a = paste(path[storage == storage_a$label], collapse = " | "),
    paths_b = paste(path[storage == storage_b$label], collapse = " | "),
    .groups = "drop"
  ) %>%
  select(-.oldest) %>%
  arrange(desc(wasted_gb), desc(count))

write_csv(between_summary, output_cross_summary_csv)
cat(sprintf("Between summary written to: %s\n", output_cross_summary_csv))

# ---- Cross-storage: coverage per storage (console summary) ------------------
# How much of each storage already exists (by content) in the other one.
# "Shared" counts EVERY copy in that storage whose md5 is also in the other
# storage - i.e. what could go from this storage if the other one is the copy
# you keep.

coverage <- cross_files %>%
  group_by(storage) %>%
  summarise(
    files        = n(),
    total_gb     = sum(size_gb),
    shared_files = sum(in_both),
    shared_gb    = sum(size_gb[in_both]),
    .groups = "drop"
  ) %>%
  mutate(shared_pct = 100 * shared_gb / total_gb)

cat(sprintf(
  "\n=== %s <-> %s duplicates ===\nShared md5 groups: %d | File entries in those groups: %d | Wasted space: %s\n",
  storage_a$label, storage_b$label,
  nrow(between_summary), sum(between_summary$count), fmt_gb(sum(between_summary$wasted_gb))
))
for (i in seq_len(nrow(coverage))) {
  cat(sprintf(
    "%s: %s of %s (%.1f%%, %d of %d files) also exists in the other storage\n",
    coverage$storage[i], fmt_gb(coverage$shared_gb[i]), fmt_gb(coverage$total_gb[i]),
    coverage$shared_pct[i], coverage$shared_files[i], coverage$files[i]
  ))
}

# ---- Cross-storage: overlap per directory -----------------------------------
# Per directory, per storage: a folder at (or near) 100% shared is a whole
# folder that's already a copy of content in the other storage - the easiest
# candidates to act on, instead of going md5 group by md5 group.

cross_by_dir <- cross_files %>%
  mutate(dir = dirname(gsub("\\\\", "/", path))) %>%
  group_by(storage, dir) %>%
  summarise(
    files        = n(),
    total_gb     = sum(size_gb),
    shared_files = sum(in_both),
    shared_gb    = sum(size_gb[in_both]),
    .groups = "drop"
  ) %>%
  filter(shared_files > 0) %>%
  mutate(shared_pct = 100 * shared_gb / total_gb) %>%
  arrange(storage, desc(shared_gb))

write_csv(cross_by_dir, output_cross_dirs_csv)
cat(sprintf("Per-directory overlap written to: %s\n", output_cross_dirs_csv))

cat("\nTop directories by shared space, per storage:\n")
print(
  cross_by_dir %>% group_by(storage) %>% slice_head(n = 10) %>% ungroup() %>%
    select(storage, dir, shared_gb, shared_pct, shared_files),
  n = Inf
)

# Same, split by year (the file's real mtime year - no pre-cutoff grouping
# here, so nothing is hidden): shows which years of a folder are fully
# shared even when the folder as a whole isn't. Unlike cross_by_dir, rows
# with nothing shared are kept, so each folder's years add up to its total.
# year is NA for files with an unreadable mtime.

cross_by_dir_year <- cross_files %>%
  mutate(dir = dirname(gsub("\\\\", "/", path))) %>%
  group_by(storage, dir, year) %>%
  summarise(
    files        = n(),
    total_gb     = sum(size_gb),
    shared_files = sum(in_both),
    shared_gb    = sum(size_gb[in_both]),
    .groups = "drop"
  ) %>%
  mutate(shared_pct = 100 * shared_gb / total_gb) %>%
  arrange(storage, dir, year)

write_csv(cross_by_dir_year, output_cross_dirs_year_csv)
cat(sprintf("Per-directory, per-year overlap written to: %s\n", output_cross_dirs_year_csv))

# ---- Cross-storage: by-year data --------------------------------------------
# How much of each year's data also exists in the other storage, per storage.
# Pre-cutoff years collapse into one bar, as in the main chart; files with an
# unreadable mtime are left out of this view only.

cross_by_year <- cross_files %>%
  filter(!is.na(year)) %>%
  mutate(
    year_label = if_else(year < cutoff_year, old_label, as.character(year)),
    overlap    = if_else(in_both, "Also in other storage", "Only in this storage")
  ) %>%
  group_by(storage, year_label, overlap) %>%
  summarise(size_gb = sum(size_gb), .groups = "drop") %>%
  mutate(
    year_label = fct_relevel(factor(year_label), old_label),
    overlap    = factor(overlap, levels = c("Only in this storage", "Also in other storage"))
  ) %>%
  # pct = this segment's share of its own bar (storage + year), i.e. what the
  # % labels in the figure show.
  group_by(storage, year_label) %>%
  mutate(pct = 100 * size_gb / sum(size_gb)) %>%
  ungroup()

# Every bar's numbers, including any whose label is too small to draw.
print(
  cross_by_year %>%
    select(storage, year_label, overlap, size_gb, pct) %>%
    arrange(storage, year_label, overlap),
  n = Inf
)

# ---- Cross-storage: by-year figure ------------------------------------------
# Only uses `cross_by_year` - re-run just this section to tweak the figure.
#
# Each segment is labelled with its % of that bar. A label is only drawn when
# its segment is at least `cross_label_min_frac` of the tallest bar in the
# same panel (panels have their own y scale) - a thinner segment has no room
# for the text. Lower it to label more, raise it to declutter; the table
# printed above has every value either way.

cross_label_min_frac <- 0.04

cross_labels <- cross_by_year %>%
  group_by(storage) %>%
  mutate(panel_max = max(ave(size_gb, year_label, FUN = sum))) %>%
  ungroup() %>%
  mutate(label = if_else(size_gb >= cross_label_min_frac * panel_max, sprintf("%.0f%%", pct), ""))

p_cross <- ggplot(cross_by_year, aes(x = year_label, y = size_gb, fill = overlap)) +
  geom_col(color = "#fcfcfb", linewidth = 0.3, width = 0.7) +
  geom_text(
    data = cross_labels,
    aes(label = label),
    position = position_stack(vjust = 0.5),
    size = 3.2, fontface = "bold", color = "#ffffff"
  ) +
  facet_wrap(~storage, ncol = 1, scales = "free_y") +
  scale_fill_manual(values = cross_palette, name = NULL) +
  scale_y_continuous(labels = label_comma(suffix = " GB"), expand = expansion(mult = c(0, 0.05))) +
  labs(
    title = sprintf("Content shared between %s and %s, by year", storage_a$label, storage_b$label),
    subtitle = "Matched by md5 from each storage's existing raw export - no rescan",
    x = NULL, y = "Total size"
  ) +
  theme_minimal(base_size = 12) +
  theme(
    panel.grid.minor   = element_blank(),
    panel.grid.major.x = element_blank(),
    panel.grid.major.y = element_line(color = "#e1e0d9"),
    axis.text.x  = element_text(angle = 45, hjust = 1),
    plot.title   = element_text(face = "bold"),
    strip.text   = element_text(face = "bold", hjust = 0),
    legend.position = "top"
  )

print(p_cross)
ggsave(output_cross_png, p_cross, width = 16, height = 10, dpi = 150)
