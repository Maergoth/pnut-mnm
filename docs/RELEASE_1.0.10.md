# PNUT M&M 1.0.10

- **Stats in setup:** step 2 selects Combat chat and optional Mini stats from the same captured frame, with independent rectangles. New setups default to a separate stats region; select its rectangle to start HP and XP readings.
- **Low-health alert:** customize the HP threshold (10% by default) and use **Preview sound** beside its dropdown. The preview uses your selected alert volume and Triggers audio settings.
- **HP detection:** small OCR box changes no longer reset valid readings, and confirmation follows the capture/OCR cadence. Alerts still require two fresh low readings.
- **Revenge prompts:** incoming PvP attackers now appear in the floating timers overlay with a **+** button for 30 seconds, once per attacker per session.
- **Damage breakdown:** each skill shows its share of your total damage in a separate **Damage%** column. **Hit%** remains hit accuracy.
- **Revenge pop-out:** the hover header is replaced by a small, always-visible **×** in the top-right corner. Players/Guilds headings and their **+** controls remain together.
- Existing capture settings and older profiles/backups retain their configured stats mode. Stat OCR uses screen positions to pair labels and values, so the displayed text list can be out of order.

Download `PNUT-MnM-1.0.10-windows.zip`, extract it into a writable folder and run `PNUT M&M.exe` with `_internal` beside it. Installed copies can use **Settings > App updates**.
