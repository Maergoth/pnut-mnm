# PNUT M&M 1.0.15

- **XP collecting fix:** the XP reader recognizes the darker orange shades in the bar's animated fill. Previously, these colors broke track detection or caused valid bar readings to be rejected, leaving XP/hour collecting indefinitely despite a visible bar.
- **Stable comparisons:** small detected bar-width changes and briefly missing stat images preserve ongoing comparisons. A genuine resize, changed capture region or prolonged gap still requires fresh measurements.
- **Useful local evidence:** OCR diagnosis reports now include bounded mini-stat frame availability, detected rectangles, XP readings and completed measurement totals, making acquisition failures distinguishable from a display problem.

Installed copies can use **Settings > App updates**. For a portable copy, extract `PNUT-MnM-1.0.15-windows.zip` into a writable folder and run `PNUT M&M.exe` with `_internal` beside it.
