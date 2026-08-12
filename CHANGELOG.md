# CHANGELOG
All notable changes to this project will be documented in this file.
Format based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [Unreleased]

## [0.2.0] - 2026-06-01

Full rewrite of the interface as a single-page application (SPA), modeled on
wiki-archive, + backend fixes respecting the project rules.

### Added
- **static/app.js** (new): all the SPA logic — sidebar (job list) + detail
  panel filled in dynamically, with no page reload. Log stream replayed from
  `from_pos` then followed live, progress bar driven by the `[n/6]` markers,
  built-in creation form.
- **app.py**: `GET /api/jobs/{id}/logs?from_pos=` (SSE, replays then follows
  live), `POST /api/jobs/{id}/start` (creation and start decoupled).

### Changed
- **Interface**: replaced the 3 full-reload Jinja pages (`index.html`,
  `new_job.html`, `job_detail.html`) with a **single SPA shell**
  (`index.html`) + `static/app.js` + `static/style.css`. No more inline
  `<style>` blocks or `style=`/`onclick=` attributes.
- **static/style.css**: unique design system aligned with wiki-archive's blue
  palette (goodbye inconsistent purple).
- **pipeline.py**: `JobManager` enriched — rich in-memory state (`meta()` with
  PDF files + steps), replayable logs with `__END__` sentinel, full lifecycle
  (`create`/`start`/`pause`/`resume`/`stop`/`delete`), `[n/6]` markers,
  aligned status vocabulary (`pending/running/paused/stopped/done/error`).
- **grouping.py**: `max_pages_per_pdf` and `avg_pages_per_mod` are now
  **configurable** (no more hardcoding, per CONTEXT.md).

### Fixed
- **pipeline.py**: zombie jobs — a `running`/`paused` job on server restart
  stayed stuck forever; it now resets to `stopped` (can be restarted).
- **app.py**: temp file leak — the PDF download ZIP was never deleted
  (`background=None`); cleanup now handled via `BackgroundTask`.
- **app.py**: removed the duplicate log endpoint and the old `/events` SSE
  (the `pendingSSE`/double-polling machinery); `templates/` and `static/`
  paths now resolved via `Path(__file__).parent`; imports moved to the top.
- **thunderstore.py**: network errors are now **logged** instead of being
  silently swallowed (`print` -> `logging`), per the "errors are never
  silent" rule.
- **configs.py**: **atomic** config writes + warning if a JSON file is
  corrupted (instead of a silent `pass`).
- **html_builder.py**: table-of-contents anchors are now **unique** (no more
  collisions pointing to the wrong spot), non-list dependencies tolerated,
  `Unnamed` title fallback.
- **requirements.txt**: removed the `sse-starlette` dependency (SSE stream
  now handled via native `StreamingResponse`).

## [0.1.4] - 2026-05-09

### Fixed
- **html_builder.py**: WeasyPrint crash `'super' object has no attribute 'transform'` — root cause: `MarkdownIt` was passing raw readme HTML through as-is (`html: True` by default), including `<div style="transform:...">` from shields.io badges and others. Fix: `MarkdownIt(options_update={"html": False})` — HTML tags in readmes are now escaped as text instead of being injected into the document.
- **html_builder.py**: residual removal of `<style>` blocks and `style=` attributes in the rendered HTML (double protection).
- **job_detail.html**: Delete button didn't redirect — refactored with `window.location.replace('/')` (no history entry) and buttons disabled during the request.
- **job_detail.html**: Stop button showed a 404 alert if the job had already finished — 404s on stop/pause/resume are now silent.
- **job_detail.html**: `clearInterval` now guaranteed via a `pollTimer` variable (the old `statusPoll` reference could be captured before assignment).

## [0.1.3] - 2026-05-09

### Fixed
- **pipeline.py**: blocking logging — `open(log_file, "a")` called 49,000+ times (once per mod) stalled the pipeline. Replaced with a buffer flushed every 100 lines.
- **pipeline.py**: removed `packages.json` (several GB for 49k packages with full readmes). Replaced with `mods_index.json` (lightweight metadata only).
- **pipeline.py**: progress logged every 1,000 iterations instead of every mod.
- **pipeline.py**: lambda closure bug in `run_in_executor` for PDF conversion (`group_name` captured by reference inside the loop).
- **job_detail.html**: Stop button returned a 404 if the job had already finished -> blocking JS alert -> infinite polling after deletion. Fixed: silent 404, polling stopped on a 404 status.

## [0.1.2] - 2026-05-08

### Fixed
- **grouping.py**: critical bug — ascending sort kept the smallest groups and merged the biggest ones into "Other". Fixed by sorting descending.
- **html_builder.py**: TOC anchors placed after the content (link scrolled to the bottom). `id` attributes are now on the `div.mod-section`.
- **app.py**: removed dead code (`gc.collect()`, module-level SSE loop). SSE closes after 20s of silence if the job has finished.
- **job_detail.html**: SSE vs. initial log load race condition. SSE messages now buffered and replayed.
- **job_detail.html**: polling and SSE not closed after reaching a terminal state.

### Improved
- **thunderstore.py**: exponential retry (3 attempts, base 1.5s) on 429/5xx/network errors; `list_communities` now paginated.
- **pipeline.py**: inline extraction (no individual calls); `asyncio.sleep(0)` every 500 iterations; `run_in_executor` for WeasyPrint.
- **html_builder.py**: `html.escape` on all fields; metadata table; `pre`/`code` styles.
- **grouping.py**: category read from `mod["metadata"]["category"]`; cleaned-up imports.
- **app.py**: FastAPI lifespan; jobs sorted by descending date; `slug` fallback.
- **index.html**: clean ISO date (no `T`, no literal `...`).
- **new_job.html**: button feedback during creation; PDF workers default to 2.
- **job_detail.html**: Jinja2 `tojson` for safe JS injection.

## [0.1.1] - 2026-05-08

### Fixed
- Job deletion: JS URL suffix `/delete` -> `DELETE /api/jobs/{id}`.
- Detail extraction: removed individual calls (mass 404/403) -> `versions[]` data now inline in the list.

## [0.1.0] - 2026-05-08

### Added
- Full web interface (FastAPI + Jinja2 + SSE).
- 6-step pipeline: list packages, extract details, group by category, HTML, PDF, merge.
- Automatic grouping to stay <= 100 PDFs (NotebookLM constraint).
- Automatic PDF splitting if > 100 MB.
- Job controls: pause, resume, stop, delete.
- Advanced logging with log download.
- Async Thunderstore HTTP client with retry and fallbacks.
- Markdown cleanup: removal of images and external links.
- Docker with WeasyPrint and system dependencies (bookworm).
