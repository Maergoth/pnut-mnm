# PNUT M&M 1.0.13

- **Automated releases:** merging a version bump to main now runs the full regression and portable release gates and publishes the GitHub release with its Windows ZIP and SHA-256 checksum attached, built from that exact commit. The release only becomes visible once both assets are uploaded, so the in-app **Update** button no longer finds a release without its Windows package. Version 1.0.12 was tagged without a package and was never installable; its changes ship in this release.
- **Trigger regex time limits:** each regular expression now gets 30 ms per search (up from 10 ms) and a chat line's combined trigger matching gets 150 ms (up from 50 ms), so complex patterns on slower machines are disabled less eagerly.
- **Clearer slowdown warnings:** the warnings shown when an expression is disabled or triggers are skipped now explain that complex regular expressions cause slowdowns and should be simplified, and Trigger help gains a section on the time limits.

Download `PNUT-MnM-1.0.13-windows.zip`, extract it into a writable folder and run `PNUT M&M.exe` with `_internal` beside it. Installed copies can use **Settings > App updates**.
