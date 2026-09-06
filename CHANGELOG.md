# CHANGELOG
All notable changes to this project will be documented in this file.
Format based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [Unreleased]

### Fixed
- **pipeline.py**: deleting a **running** job left its task unwinding into
  already-removed state (logs, job folder), causing unhandled task
  exceptions; `start`/`resume`/`delete` are now async and cancel + await the
  previous task *before* removing state or restarting, so a job can never run
  twice or write into a deleted job folder.
- **pipeline.py**: clicking stop then restart quickly could launch a second
  run while the first was still shutting down; the stale task is now reaped
  first (`_await_stale_task`).
- **pipeline.py**: malformed job settings could **hang** the job forever
  (`workers_api: 0`/`workers_pdf: 0` -> `asyncio.Semaphore(0)` never
  releases) or crash it (non-numeric / NaN / out-of-range values). All
  numeric settings (`workers_api`, `delay`, `workers_pdf`, `max_size_mb`,
  `max_groups`, `max_words_per_file`) are now validated, clamped and logged.
- **pipeline.py**: a single malformed package record (e.g. a changelog
  without `version_number`, non-dict version/category entries) crashed the
  whole extraction with `KeyError`/`TypeError`. Extraction is now tolerant
  and isolated per package — bad records are logged and skipped, and the job
  log reports how many were skipped (a single bad mod can never kill the
  pipeline).
- **pipeline.py**: the "PDF workers" setting was silently capped by the
  fixed 4-thread global executor; a per-job executor sized from the setting
  now honors it (and is shut down with the job instead of accumulating
  threads across runs).
- **pipeline.py**: output files were named from category names with only `/`
  replaced — illegal Windows characters, path separators and sanitization
  collisions (`A/B` vs `A_B`) could silently overwrite each other's files.
  Names now go through `file_stem()` + a per-run uniqueness pass, shared by
  the HTML/PDF and Markdown/TXT stages.
- **pipeline.py**: a corrupted `jobs.json` was silently overwritten with an
  empty state file (whole job history lost); it is now logged, backed up as
  `jobs.json.bak-<timestamp>` and non-dict job records no longer crash the
  startup.
- **pipeline.py**: job id collision (same-second + 2 random bytes) could
  silently overwrite an existing job; `create()` now rerolls on collision.
- **grouping.py**: a generated split name colliding with a real category
  name (splitting "Mods" into "Mods_part1" while a category literally named
  "Mods_part1" exists) silently overwrote one of the groups and dropped its
  mods; group keys are now allocated collision-free.
- **html_builder.py / text_builder.py**: the markdown image-stripping regex
  was duplicated; a single `remove_images()` helper in html_builder is now
  shared (DRY). The residual `style=` scrubber now only touches real HTML
  tags (escaped readme HTML printed as text is preserved) and also removes
  unquoted attribute values.
- **thunderstore.py**: network errors were never logged once retries were
  exhausted (silent failure); every failure is now logged — a warning per
  retry and an error on the final raise (including non-retryable HTTP
  errors).
- **thunderstore.py / app.py**: a total Thunderstore outage was reported as
  an empty community/category list ("No community" in the UI) instead of an
  error. `list_communities` re-raises when nothing was collected (502 in the
  API) and `get_categories(..., raise_on_error=True)` lets the UI endpoints
  surface a 502 while the pipeline keeps its graceful fallback.
- **app.py**: the SSE log stream kept sending keep-alives forever when the
  job was deleted mid-stream; the stream now ends when the job disappears.
- **app.py**: removed the deprecated `TemplateResponse` signature (warning
  on every page load).
- **static/app.js**: a failed delete always announced "Job deleted" and left
  the job in place silently; delete errors are now surfaced and the UI only
  clears after a successful response.
- **static/app.js**: after a restart with "start from scratch", the files
  panel kept showing rows for deleted files; the panel is now refreshed and
  hidden/shown on every poll.
- **static/app.js**: the progress steps were duplicated client-side and
  could drift from the backend; the progress bar now uses the server-provided
  `steps` field (the local constant is only a fallback).
- **static/app.js**: silent fetch failures (job polling, communities,
  categories, config loading/saving) are now logged to the console and/or
  surfaced as toasts.
- **pipeline.py**: PDF splitting leaked the source PDF handle if a part
  save failed (`pdf.close()` now in `finally`); job teardown no longer dies
  when the final log flush fails.

### Security
- **app.py / configs.py**: community names were used to build output-folder
  and config-file paths and API URLs without validation — a crafted name
  such as `../../x` could escape the data directories (path traversal). All
  community inputs (job creation, categories, config endpoints) are now
  validated server-side against the Thunderstore slug pattern; config
  storage refuses unsafe names too.

### Added
- **tests/** (new): pytest suite covering the fixes — Thunderstore client
  retries/logging/pagination/concurrency and soft-vs-hard failure modes,
  markdown/HTML sanitization rules, text builders, grouping limits and name
  collisions, config validation, `JobManager` lifecycle (including
  delete-while-running and corrupt `jobs.json` handling), a full offline
  6-step pipeline run and the HTTP API endpoints. `requirements-dev.txt` +
  root `conftest.py` keep test runs hermetic (`./data` redirected to a
  tempdir). Run with `python -m pytest tests/`.

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
