# Banner template generation

This file guides the local `generate_template` command (Nano Banana 2 Lite).
Expand it with your brand rules, colors, and layout preferences.

## Target

- Size: **1500 × 500** px (X profile banner)
- Output: `assets/template/background.png` plus `assets/template/layout.yaml`
- The background must **not** include live revenue numbers, dates, or charts — leave clear empty regions for overlays

## Prompt notes (edit later)

- Visual style, brand name, mood, and color palette go here
- Reserve space for: total revenue, Apple revenue, Google revenue, period label, and a revenue chart
- Prefer high contrast so overlaid white/dark text remains readable

## Layout regions

The generator writes a starter `layout.yaml` with these region ids:

- `period_label`
- `total_revenue`
- `apple_revenue`
- `google_revenue`
- `revenue_chart`

Adjust coordinates in `layout.yaml` after generation if the AI background does not align.
