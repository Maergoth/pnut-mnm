# PNUT M&M file audit

Parses, Navigation, und Timers. Reviewed October 5, 2026, for public publication at
`Maergoth/pnut-mnm`. This audit did not delete user data.

## Keep in source control

| Files | Why they are needed |
| --- | --- |
| `mnmparse/*.py`, `mnmparse/app/*.py` | Runtime parser, capture/OCR, session, export, desktop UI and overlays. No complete source module was proven unused. |
| `mnmparse/replay.py` | Used by imports to remove duplicate blocks in older logs; it is not just a development replay tool. |
| `mnmparse/cli.py`, `mnmparse/__main__.py`, `run.cmd`, `mnmparse/calibrate.py` | Supported command-line workflow. The Tk crop picker is source-only and deliberately excluded from the desktop executable. |
| `mnmparse/app/probe.py` | Optional `--probe-stalls` diagnostics used by the desktop application. |
| `PNUT M&M.cmd`, `make_shortcut.ps1` | Desktop source launcher and shortcut installer. `run.cmd` starts the CLI, so these launchers are not duplicates. |
| `mnmparser.spec`, `build_exe.ps1`, `requirements.txt` | Reproducible executable build and pinned runtime dependencies. PyInstaller is a separate build dependency documented in `requirements.txt`. |
| `config.example.json`, `cc.json` | Portable settings documentation and crowd-control registry. Never substitute personal `config.json` or `triggers.json`. |
| `mnmparse/trigger_presets.py` | Portable starter triggers, including their stable identities. Personal edits remain in ignored `triggers.json`. |
| `assets/icon.ico`, `mnmparse/app/icon.py` | Windows executable/shortcut resource and its generator. Although reproducible, keeping the icon makes builds and shortcuts straightforward. |
| `tests/test_*.py` | Regression coverage. The numbered audit files cover different failures, not redundant copies. |
| `tests/fixtures/ocr_sample.png`, `tests/fixtures/make_ocr_sample.py` | Synthetic OCR input and its reproducible generator; `test_ocr.py` uses the PNG. |
| `tests/fixtures/burst_ocr.json` | Anonymized tracker recording with synthetic character aliases, including clipped and garbled readings. Keep with its matching tracker/parser expectations. |
| `.gitignore`, `.gitattributes`, `README.md`, `docs/` | Repository policy, line-ending behavior and maintained documentation. |

## Keep local; exclude from Git and release payloads

- `logs/`, `exports/`, `config.json`, `triggers.json`, `vocabulary.json`: personal logs, settings, learned names and session/party state. Preserve these for the owner.
- `assets/reference/`, `tests/fixtures/frames/`, `tests/fixtures/burst2_ocr.json` and later recordings, `private/`, `SPEC.md`, `APP_SPEC.md`: private captures and original notes; already ignored. They are not required to run or build the app.
- `.venv/`, `build/`, `__pycache__/`, tool caches and `*.stackdump`: local environment, generated output or crash debris; reproducible and not repository inputs.
- `dist/`: distribute a newly built application separately from source control. An existing executable folder can also contain the owner's settings and logs, so do not assume every file inside it is disposable.
- `assets/sounds/` and `assets/ui/`: generated at runtime. The generator code is required; these cached files are not.
- Downloaded map caches: preserve local caching behavior, but exclude cached wiki media from the source repository. Keep map discovery/loading code and tests.

## Publication findings

1. **Recorded fixture resolved:** all three captured character identities and their readable OCR variants now use synthetic aliases. The 99 frames retain their original geometry, timestamps, numbers, NPC/ability text and text lengths. Validation preserved all 269 distinct line strings and the same 62 replay messages, including every message's timing, observation count, backlog and estimated-time flags. Dependent exact-string assertions were updated. The unsanitized original is retained only in ignored `private/fixture-originals/`; no original screenshots are published.
2. **Executable registry parity resolved:** `Pinning Throw` now has both root and stun categories in the built-in registry. A regression test checks every shipped `cc.json` ability against an empty data directory and verifies the same category behavior as a source checkout. The executable therefore needs no bundled `cc.json` for shipped behavior; a file beside the executable remains an optional user extension. Intimidate/Intimidation are built in as well.
3. **Content scan:** staged text contained no matches for personal Windows home/workspace paths, private-key headers or common GitHub/OpenAI/AWS credential token formats. This was a targeted scan, not a claim that every possible credential format was tested. No live logs or personal settings were staged.
4. **Documentation organized:** the README contains the user guide; implementation details have moved to `docs/DEVELOPMENT.md`. References to ignored `SPEC.md`/`APP_SPEC.md` are historical comments, not runtime dependencies.

No tracked file is recommended for unconditional deletion. The clear cleanup candidates are ignored generated files. The recorded-fixture and executable-registry findings above have been resolved without removing regression coverage or requiring personal settings in the release.
