# Contributing to Altium Copilot

## How contributions work

`main` is protected — no one can push to it directly, including maintainers.
All changes go through a pull request and require:
- Passing CI (lint + tests)
- An approving review from a maintainer

**To contribute:**
1. Fork the repo
2. Create a branch (`git checkout -b my-feature`)
3. Make your changes and ensure `pytest` passes locally
4. Open a PR against `main`

Only maintainers can tag a release (`git tag v*`), which is what triggers the
build and publish workflow. Contributors cannot ship a release.

---

## DB Schema Changes

`server/export.py` defines `SCHEMA_VERSION` (currently 1). pcb-copilot refuses to open a
snapshot whose version is higher than its `SUPPORTED_SCHEMA_VERSION`, so a bump locks out
every pcb-copilot release that has not caught up.

Any PR that changes or removes an existing table or column in `_create_schema()`, or
changes what a column means, **must**:

1. Bump `SCHEMA_VERSION` in `server/export.py`
2. Bump `SUPPORTED_SCHEMA_VERSION` in `pcb-copilot/server/main.py` (separate repo), and
   release pcb-copilot before releasing altium-copilot

Failing to bump causes pcb-copilot to silently load a DB with an incompatible schema.

Adding a new table that existing readers do not query (for example `notes`) needs no
bump: pcb-copilot reads only the tables and columns it names. Bump when pcb-copilot
starts depending on the new table.

## Prerequisites

- Windows (the MCP uses PowerShell to talk to Altium Designer)
- Python 3.13+ with a virtual environment
- Node.js (for `npx @anthropic-ai/mcpb`)

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\pip install pyinstaller
```

## Running locally (no build needed)

```bash
.venv\Scripts\python server/main.py
```

Or register it directly with Claude Code:

```json
{
  "mcpServers": {
    "altium-copilot": {
      "command": "C:\\path\\to\\altium-copilot\\.venv\\Scripts\\python.exe",
      "args": ["C:\\path\\to\\altium-copilot\\server\\main.py"]
    }
  }
}
```

## Running tests

```bash
.venv\Scripts\pytest
```

## Releasing a new version

Releases are fully automated via GitHub Actions (`.github/workflows/release.yml`).
Push a version tag and the workflow builds the binary, packs the `.mcpb`, and
publishes a GitHub Release — no manual build steps needed.

```bash
git tag v0.2.0
git push origin v0.2.0
```

The workflow:
1. Reads the version from the tag (`v0.2.0` → `0.2.0`)
2. Patches `manifest.json` with that version
3. Compiles `server/dist/altium-copilot.exe` with PyInstaller on a Windows runner
4. Packs `altium-copilot-0.2.0.mcpb` with `npx @anthropic-ai/mcpb pack`
5. Creates a GitHub Release with the `.mcpb` as the downloadable asset

Users who already have the MCP installed will get updated instructions automatically
the next time the server starts (guidance is embedded in `SERVER_INSTRUCTIONS` and
takes effect immediately without any reinstallation step).

### Building locally (for testing before a release)

```bash
.venv\Scripts\pip install pyinstaller

.venv\Scripts\pyinstaller `
    --onefile `
    --name altium-copilot `
    --distpath server/dist `
    --workpath build `
    --add-data "server/scripts;scripts" `
    --add-data "server/checklists;checklists" `
    --add-data "server/ui;ui" `
    --add-data "manifest.json;." `
    --paths server `
    server/main.py
```

Test the resulting `server/dist/altium-copilot.exe` on a machine **without Python installed** before tagging.

## How the bundle works

| What | Where it lives |
|------|---------------|
| Server code + Python runtime | compiled into `server/dist/altium-copilot.exe` by PyInstaller |
| PowerShell scripts | embedded in the `.exe` via `--add-data`; extracted to `sys._MEIPASS/scripts/` at runtime |
| Review checklists | `server/checklists`, embedded the same way (`sys._MEIPASS/checklists/`) |
| Interactive views (MCP Apps pages) | `server/ui`, embedded the same way (`sys._MEIPASS/ui/`); `services/ui_views.py` serves them as `ui://` resources |
| Extension settings | `manifest.json` `user_config` (the form Claude Desktop shows), mapped by `mcp_config.env` to `ALTIUM_COPILOT_*` variables and by `mcp_config.args` to `--project-folders`; `services/settings.py` reads and checks them at start |

The `manifest.json` `mcp_config.command` points to the `.exe` directly, so Claude
Desktop needs nothing else on the PATH.

A new setting is a `user_config` field and its `env` entry in `manifest.json`, a `Field`
in `services/settings.py` with its validation, its variable in `SETTING_VARIABLES` in
`conftest.py` (so a developer's shell cannot change test results), the code that uses
it (a tool argument first, the setting next, the old default last) and a README row;
`tests/test_settings.py` fails until the manifest, the module and `conftest.py` agree.

## Project structure

```
server/
  main.py          — MCP tool definitions (FastMCP)
  altium.py        — Altium Designer client (PowerShell bridge)
  netlist_parser.py
  parsers/         — .PrjPcb, .SchDoc, .PcbDoc and Draftsman .PCBDwf parsers
  services/        — registry, sheet context, PCB index, design notes and image helpers
  scripts/         — PowerShell scripts called at runtime
  ui/              — MCP Apps views: shell.css and shell.js (shared, inlined) and one HTML
                     page per view, served as ui:// resources by services/ui_views.py
  dist/            — compiled binary (git-ignored, produced by PyInstaller)

tests/             — pytest suite
```

## What gets shipped

The GitHub Release contains only `altium-copilot.exe` and `checksums.txt`.
Python source, tests, docs, and the `.venv` are never shipped to end users.

## PCB parser oracle test (optional, local-only)

`tests/test_oracle_ipc2581.py` cross-validates the `.PcbDoc` parser against an
IPC-2581 export of the same board. It is skipped unless you set:

- `ALTIUM_COPILOT_ORACLE_PCBDOC` — path to any `.PcbDoc` you own
- `ALTIUM_COPILOT_ORACLE_CVG` — path to its IPC-2581 rev B export
  (Altium: File → Fabrication Outputs → IPC-2581, "Merge Net-Tie Nets" unchecked)

Use any board of your own. Never commit oracle files or values derived from them—
they are private design data. Run this before releases that touch `pcb_doc.py`.

## Draftsman parser check (optional, local-only)

`tests/fixtures/pcb-fab-drawing.PCBDwf` is synthetic: `tests/draftsman_fixture.py` builds
it from made-up XML in Altium's format (regenerate with `python tests/draftsman_fixture.py`).
To also check a drawing Altium wrote, set `ALTIUM_COPILOT_ORACLE_PCBDWF` to any `.PCBDwf`
of your own and run `pytest tests/test_pcb_dwf_parser.py`. Never commit that file.
