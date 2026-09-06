# Thunderstore Archive

A web app (FastAPI) that archives the mods of a [Thunderstore](https://thunderstore.io/)
community into PDF/Markdown/text, grouped by category — built for importing into NotebookLM.

6-step pipeline (fetch -> extract -> group -> HTML -> PDF -> final copy), single-page
interface with live logs.

## Native usage (recommended)

Requirements: Python 3.11 or 3.12.

Double-click `run.bat` — creates the venv, installs dependencies, starts the server on
http://127.0.0.1:8080, and opens the browser.

### PDF export on Windows

PDF export depends on [WeasyPrint](https://weasyprint.org/), which requires the GTK3 runtime
(pango/cairo/gdk-pixbuf), which cannot be installed via pip on Windows. Without this runtime,
the app starts normally and the Markdown/text exports work; only the PDF export is unavailable
(clear message in the job logs). To enable it, install the
[GTK3 Runtime Environment](https://github.com/tschoonj/GTK-for-Windows-Runtime-Environment-Installer/releases).

## Docker usage (optional)

```bash
docker compose up -d --build
```

Interface at http://localhost:8081. `update.bat`/`update.sh` rebuild and restart the
container (double-click on Windows).

## Tests

```bash
pip install -r requirements-dev.txt   # adds pytest
python -m pytest tests/
```

The suite covers the Thunderstore HTTP client (retries, logging, pagination),
the markdown/HTML sanitization rules, grouping, config storage and the
`JobManager` lifecycle (including the delete-while-running and corrupt
`jobs.json` cases), plus an offline end-to-end pipeline run and the HTTP API.
Network access is never needed: the client tests inject a mock HTTP
transport and the pipeline test stubs the Thunderstore client.

## Configuration

A `.env` file at the root holds the sensitive configuration (not versioned, see `.gitignore`).

## Architecture

```
app.py              FastAPI: serves the interface + REST endpoints + log stream
pipeline.py         JobManager (full lifecycle) + 6-step orchestrator
thunderstore.py     Async Thunderstore HTTP client
grouping.py         Smart grouping of mods by category
html_builder.py     Markdown -> cleaned HTML (for PDF rendering)
configs.py          Per-community config (atomic write)
templates/           Interface (single page)
static/               Interface CSS + JS
```

Architecture and pipeline details: see `CONTEXT.md`. History: see `CHANGELOG.md`.
