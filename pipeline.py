import asyncio
import json
import logging
import math
import os
import re
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from datetime import datetime
try:
    from weasyprint import HTML
    WEASYPRINT_AVAILABLE = True
    WEASYPRINT_ERROR = None
except (ImportError, OSError) as exc:
    # On Windows without the GTK3 runtime (pango/cairo/gdk-pixbuf), the import
    # fails with an OSError even if the pip package is installed. We degrade
    # gracefully instead of crashing the whole pipeline: md/txt exports still
    # work without PDF.
    HTML = None
    WEASYPRINT_AVAILABLE = False
    WEASYPRINT_ERROR = str(exc)
from pikepdf import Pdf
from thunderstore import ThunderstoreClient
from html_builder import build_group_html
from text_builder import build_group_markdown, build_group_txt
from grouping import smart_grouping

logger = logging.getLogger("pipeline")

# Supported export formats -> produced file extension
EXPORT_FORMATS = {"pdf": ".pdf", "md": ".md", "txt": ".txt"}

# End-of-stream sentinel for logs (replayed then followed live by the SPA)
END_MARKER = "__END__"

# Pipeline steps. The `marker` is emitted at the start of each phase's log
# line; the SPA uses it to drive the progress bar. Single source of truth,
# shared with the frontend via /api/jobs (the "steps" field).
STEPS = [
    {"id": "fetch",   "label": "Fetching",     "marker": "[1/6]"},
    {"id": "extract", "label": "Extraction",   "marker": "[2/6]"},
    {"id": "group",   "label": "Grouping",     "marker": "[3/6]"},
    {"id": "build",   "label": "Building",     "marker": "[4/6]"},
    {"id": "export",  "label": "Export",       "marker": "[5/6]"},
    {"id": "copy",    "label": "Done",         "marker": "[6/6]"},
]

# Statuses (vocabulary aligned with wiki-archive to share CSS/JS):
# pending | running | paused | stopped | done | error
_TERMINAL = ("done", "error", "stopped")

# Safety bounds applied to numeric job settings. Values inside these ranges
# are used as given (the web UI lets the user pick within them); values
# outside (or garbage) are clamped/fallback with a warning instead of hanging
# or crashing the pipeline (e.g. concurrency 0 makes asyncio.Semaphore block
# forever).
_NUM_BOUNDS = {
    "workers_api":        (1, 64, 4),
    "delay":              (0.0, 60.0, 0.2),
    "workers_pdf":        (1, 64, 2),
    "max_size_mb":        (1.0, 10000.0, 100.0),
    "max_groups":         (1, 10000, 99),
    "max_words_per_file": (1, 100_000_000, 200_000),
}


def _cfg_number(config: dict, key: str, minimum: float, maximum: float,
                default: float, integer: bool) -> float:
    """Reads a numeric job setting, clamped to [minimum, maximum].

    A missing value returns the default; an invalid or out-of-range value is
    logged and replaced (never silently) so the job can't hang or crash on a
    malformed config.
    """
    raw = config.get(key)
    if raw is None:
        return default
    try:
        value = int(raw) if integer else float(raw)
    except (TypeError, ValueError):
        logger.warning(
            "Ignoring invalid job config %r=%r (expected a number); using %r",
            key, raw, default)
        return default
    if isinstance(value, float) and math.isnan(value):
        logger.warning(
            "Ignoring invalid job config %r=NaN; using %r", key, default)
        return default
    if value < minimum or value > maximum:
        logger.warning(
            "Job config %r=%r is out of range [%s, %s]; clamped", key, raw,
            minimum, maximum)
        return max(minimum, min(maximum, value))
    return value


def _cfg_int(config: dict, key: str) -> int:
    lo, hi, default = _NUM_BOUNDS[key]
    return int(_cfg_number(config, key, lo, hi, default, integer=True))


def _cfg_float(config: dict, key: str) -> float:
    lo, hi, default = _NUM_BOUNDS[key]
    return _cfg_number(config, key, lo, hi, default, integer=False)


# Characters that cannot appear in a file name on Windows or that would act as
# path separators (a group name comes from a category name — user content).
_ILLEGAL_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
# Windows device names (CON, PRN, AUX, NUL, COM1-9, LPT1-9) that would make
# file creation silently fail or misbehave.
_WIN_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {
    "COM{}".format(i) for i in range(1, 10)
} | {"LPT{}".format(i) for i in range(1, 10)}


def file_stem(name: str) -> str:
    """Sanitizes a group/category name into a safe output file stem.

    Group names are user content (Thunderstore categories), so a name such as
    "A/B" or "a:b" must not create sub-folders or illegal file names — this
    also guarantees PDF / Markdown / TXT files always use the same stem.
    """
    stem = _ILLEGAL_FILENAME_CHARS.sub("_", name or "")
    stem = stem.strip().strip(".").strip()
    if not stem:
        stem = "Unnamed"
    if stem.upper() in _WIN_RESERVED:
        stem = "_" + stem
    return stem


def _unique_stems(group_names: list) -> dict:
    """Maps each group name to a file stem unique within the run.

    Two different group names can sanitize to the same stem (e.g. "A/B" and
    "A_B"); without this, one file would silently overwrite the other.
    """
    used: set = set()
    mapping: dict = {}
    for name in group_names:
        base = file_stem(name)
        stem, i = base, 1
        while stem in used:
            i += 1
            stem = "{}_{}".format(base, i)
        used.add(stem)
        mapping[name] = stem
    return mapping


def _split_part_stem(stem: str, index: int, used: set) -> str:
    """File stem for PDF split artifact n°`index` of an oversized group.

    Artifacts are named "<stem>_part<index>"; the `used` set holds every stem
    that must not be clobbered (the other groups of the run). A group
    literally called "X_part1" must never be overwritten by a part of "X".
    """
    name = "{}_part{}".format(stem, index)
    while name in used:
        name += "_"
    used.add(name)
    return name


def _extract_mod(pkg: dict) -> dict | None:
    """Extracts a package's data. Runs in a thread."""
    owner = pkg.get("owner") or ""
    name  = pkg.get("name")  or ""
    if not owner or not name:
        full  = pkg.get("full_name", "")
        parts = full.split("-", 1)
        owner = parts[0] if not owner else owner
        name  = parts[1] if len(parts) > 1 else (name or full)
    if not owner or not name:
        return None

    versions = pkg.get("versions") or []
    # Latest *valid* version record — garbage entries before it must not win.
    latest = next((v for v in versions if isinstance(v, dict)), {})
    changelogs = [
        "## Version {}\n{}".format(v.get("version_number", "N/A"), v["changelog"])
        for v in versions
        if isinstance(v, dict) and v.get("changelog")
    ]

    # Categories can be plain strings (v1 list API) or {"name"/"slug"} objects
    # (cyberstorm API); tolerate both instead of leaking a dict into metadata.
    def _cat_label(raw) -> str:
        if isinstance(raw, dict):
            return raw.get("name") or raw.get("slug") or ""
        return str(raw or "")

    cats = pkg.get("categories") or []
    category = "Uncategorized"
    if isinstance(cats, list):
        for c in cats:
            label = _cat_label(c)
            if label:
                category = label
                break
    elif isinstance(cats, str) and cats:
        category = cats

    return {
        "name": "{}/{}".format(owner, name),
        "metadata": {
            # "or": the key may exist with a null value — same as missing.
            "version":      latest.get("version_number") or "N/A",
            "author":       owner,
            "downloads":    pkg.get("total_downloads") or pkg.get("downloads", 0),
            "dependencies": latest.get("dependencies") or [],
            "category":     category,
        },
        "readme":    latest.get("readme") or latest.get("description") or "",
        "changelog": "\n\n".join(changelogs),
    }


def _extract_all(packages: list) -> tuple:
    """Extracts all packages in a single pass (dedicated thread).

    Each package is isolated: one malformed record is logged and skipped —
    it must never crash the whole pipeline. Returns (mods, skipped_count).
    """
    results: list = []
    skipped = 0
    for pkg in packages:
        try:
            mod = _extract_mod(pkg)
        except Exception as exc:
            # A single inaccessible/malformed package: log, skip, continue.
            skipped += 1
            logger.warning(
                "Skipping malformed package %s: %s",
                pkg.get("full_name") or pkg.get("name") or "<?>", exc)
            continue
        if mod:
            results.append(mod)
        else:
            skipped += 1
            logger.warning(
                "Skipping package with no owner/name: %r",
                (pkg.get("full_name") or pkg.get("name") or "<?>")[:200])
    return results, skipped


class JobManager:
    """
    Manages the full lifecycle of jobs and their in-memory state.

    - self.jobs   : {job_id: {community, status, created_at, params}}  (persisted)
    - self.logs   : {job_id: [lines]}  — replayable logs (in memory), with END_MARKER at the end
    - self._ctrl  : {job_id: {pause_event, cancel_event, task}}  — execution control
    """

    def __init__(self, data_dir: str = "./data"):
        self.data_dir   = Path(data_dir)
        self.jobs_dir   = self.data_dir / "jobs"
        self.output_dir = self.data_dir / "output"
        self.jobs_file  = self.jobs_dir / "jobs.json"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.logs: dict[str, list[str]] = {}
        self._ctrl: dict[str, dict] = {}
        self._load_jobs()

    # ── persistence ────────────────────────────────────────────────────────────
    def _load_jobs(self):
        if self.jobs_file.exists():
            try:
                data = json.loads(self.jobs_file.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    raise ValueError("jobs.json root must be a JSON object")
                self.jobs = data
            except Exception as exc:
                # Corrupt state file: never overwrite it silently — keep a
                # timestamped backup and start empty (logged).
                logger.error(
                    "Unreadable %s (%s) — keeping a backup and starting empty",
                    self.jobs_file, exc)
                backup = self.jobs_file.with_suffix(
                    ".json.bak-{}".format(datetime.now().strftime("%Y%m%d-%H%M%S")))
                try:
                    shutil.copy(self.jobs_file, backup)
                except OSError as copy_exc:
                    logger.error("Could not back up %s: %s", self.jobs_file, copy_exc)
                self.jobs = {}
        else:
            self.jobs = {}

        # Legacy statuses (before aligning on the wiki-archive vocabulary)
        _legacy_status = {"cancelled": "stopped", "failed": "error"}
        for jid, job in list(self.jobs.items()):
            if not isinstance(job, dict):
                # Malformed record (e.g. hand-edited file): drop it with a log
                # instead of crashing the whole app on startup.
                logger.warning("Ignoring malformed job record %r in jobs.json", jid)
                del self.jobs[jid]
                continue
            # Migrate older records: key "created" -> "created_at", old statuses
            # -> new ones, params always present.
            if "created_at" not in job and "created" in job:
                job["created_at"] = job.pop("created")
            job.setdefault("created_at", "")
            job.setdefault("params", {})
            job["status"] = _legacy_status.get(job.get("status"), job.get("status"))
            # Fix zombie jobs: a "running"/"paused" job on startup no longer has
            # a live task -> reset it to "stopped" (can be restarted).
            if job.get("status") in ("running", "paused"):
                job["status"] = "stopped"
            # Reload logs from the file so they can be replayed.
            log_file = self.jobs_dir / jid / "pipeline.log"
            lines = log_file.read_text(encoding="utf-8").splitlines() if log_file.exists() else []
            if job.get("status") in _TERMINAL:
                lines.append(END_MARKER)
            self.logs[jid] = lines
        self._save_jobs()

    def _save_jobs(self):
        self.jobs_file.write_text(
            json.dumps(self.jobs, indent=2), encoding="utf-8")

    # ── state exposed to the API / SPA ──────────────────────────────────────────
    def meta(self, job_id: str) -> dict:
        """Rich state dict expected by the SPA (sidebar + detail)."""
        job = self.jobs[job_id]
        community = job.get("community") or ""
        out = self.output_dir / community
        files = []
        if community and out.exists():
            for f in sorted(out.iterdir()):
                if f.is_file() and f.suffix.lower() in EXPORT_FORMATS.values():
                    files.append({"name": f.name, "size_mb": round(f.stat().st_size / 1_048_576, 2)})
        return {
            "id":         job_id,
            "community":  community,
            "status":     job.get("status", "pending"),
            "created_at": job.get("created_at", ""),
            "params":     job.get("params", {}),
            "files":      files,
            "steps":      STEPS,
        }

    def list_meta(self) -> list[dict]:
        metas = [self.meta(jid) for jid in self.jobs]
        metas.sort(key=lambda m: m["created_at"], reverse=True)
        return metas

    def log_file(self, job_id: str) -> Path:
        return self.jobs_dir / job_id / "pipeline.log"

    def log_slice(self, job_id: str, pos: int) -> list[str]:
        return self.logs.get(job_id, [])[pos:]

    # ── CRUD + lifecycle ─────────────────────────────────────────────────────────
    def create(self, community: str, params: dict) -> str:
        while True:
            job_id = datetime.now().strftime("%Y%m%d-%H%M%S-") + os.urandom(2).hex()
            if job_id not in self.jobs and not (self.jobs_dir / job_id).exists():
                break  # (re)roll on the extremely unlikely id collision
        (self.jobs_dir / job_id).mkdir(parents=True, exist_ok=True)
        self.jobs[job_id] = {
            "community":  community,
            "status":     "pending",
            "created_at": datetime.now().isoformat(),
            "params":     params,
        }
        self.logs[job_id] = []
        self._save_jobs()
        return job_id

    async def start(self, job_id: str) -> None:
        """Starts (or restarts) a job's pipeline as an asyncio task."""
        job = self.jobs[job_id]
        if job["status"] == "running":
            return
        # If a previous task is still unwinding (e.g. stop then restart
        # clicked quickly), cancel it and wait so two runs can never share a
        # job's state or clobber each other's logs/status.
        await self._await_stale_task(job_id)
        # Start with a clean log
        self.logs[job_id] = []
        lf = self.log_file(job_id)
        if lf.exists():
            lf.unlink()
        job["status"] = "running"
        self._save_jobs()
        task = asyncio.create_task(self._run(job_id))
        self._ctrl.setdefault(job_id, {})["task"] = task

    async def _await_stale_task(self, job_id: str) -> None:
        """Cancels and awaits any still-running task of this job."""
        ctrl = self._ctrl.get(job_id)
        if not ctrl:
            return
        task = ctrl.get("task")
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def pause(self, job_id: str) -> bool:
        ctrl = self._ctrl.get(job_id)
        if not ctrl or self.jobs[job_id]["status"] != "running":
            return False
        ctrl["pause_event"].clear()
        self.jobs[job_id]["status"] = "paused"
        self._save_jobs()
        return True

    async def resume(self, job_id: str) -> None:
        """Resumes a paused job, or restarts a stopped/finished/errored job."""
        ctrl = self._ctrl.get(job_id)
        if ctrl and self.jobs[job_id]["status"] == "paused":
            ctrl["pause_event"].set()
            self.jobs[job_id]["status"] = "running"
            self._save_jobs()
        else:
            await self.start(job_id)

    def stop(self, job_id: str) -> bool:
        ctrl = self._ctrl.get(job_id)
        if not ctrl:
            return False
        if "cancel_event" in ctrl:
            ctrl["cancel_event"].set()
        if "pause_event" in ctrl:
            ctrl["pause_event"].set()  # unblock if paused
        return True

    async def delete(self, job_id: str) -> None:
        """Stops (if needed) and removes a job and its working directory.

        Async because a running task must be cancelled and awaited *before*
        its state is removed — otherwise the dying task keeps writing to the
        already-deleted job directory / log and dies with unhandled errors.
        """
        await self._await_stale_task(job_id)
        job_dir = self.jobs_dir / job_id
        if job_dir.exists():
            shutil.rmtree(job_dir, ignore_errors=True)
        self.jobs.pop(job_id, None)
        self.logs.pop(job_id, None)
        self._ctrl.pop(job_id, None)
        self._save_jobs()

    # ── pipeline execution ──────────────────────────────────────────────────────
    async def _run(self, job_id: str) -> None:
        job       = self.jobs[job_id]
        community = job["community"]
        config    = job.get("params", {}) or {}
        if not isinstance(config, dict):
            # Legacy/hand-edited record with a non-object "params" value:
            # never let it crash the run, just ignore it (logged).
            logger.warning("Job %s has non-object params %r — ignoring them", job_id, config)
            config = {}
        job_dir   = self.jobs_dir / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        log_file  = job_dir / "pipeline.log"

        pause_event = asyncio.Event()
        pause_event.set()
        cancel_event = asyncio.Event()
        self._ctrl[job_id] = {
            **self._ctrl.get(job_id, {}),
            "pause_event":  pause_event,
            "cancel_event": cancel_event,
        }

        # Dedicated executor for CPU-bound work (extraction, HTML building,
        # WeasyPrint). Sized from the job's "PDF workers" setting so that
        # setting is actually honored (a fixed pool of 4 silently capped it).
        executor = ThreadPoolExecutor(max_workers=_cfg_int(config, "workers_pdf"))

        _buffer: list[str] = []

        async def log(msg: str, flush: bool = False):
            line = "{} {}".format(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
            self.logs[job_id].append(line)   # immediate -> visible in the SSE stream
            _buffer.append(line)
            if flush or len(_buffer) >= 50:
                with open(log_file, "a", encoding="utf-8") as f:
                    f.write("\n".join(_buffer) + "\n")
                _buffer.clear()

        async def flush():
            if _buffer:
                with open(log_file, "a", encoding="utf-8") as f:
                    f.write("\n".join(_buffer) + "\n")
                _buffer.clear()

        async def check():
            await pause_event.wait()
            if cancel_event.is_set():
                raise asyncio.CancelledError("Job cancelled by user")

        loop = asyncio.get_event_loop()

        try:
            await log("Starting job for {}".format(community), flush=True)

            # Start from scratch: wipe the working files and previous output to
            # avoid accumulating stale files (old split, old format…). No
            # network cache: everything is re-downloaded.
            if config.get("clean_start", True):
                await log("Starting from scratch: cleaning up previous files…", flush=True)
                removed = 0
                for d in (job_dir / "html", job_dir / "pdf", self.output_dir / community):
                    if d.exists():
                        shutil.rmtree(d, ignore_errors=True)
                        removed += 1
                await log("{} folder(s) cleaned up".format(removed), flush=True)

            async with ThunderstoreClient(
                concurrency=_cfg_int(config, "workers_api"),
                delay=_cfg_float(config, "delay"),
            ) as client:

                # ── [1/6] Package listing ────────────────────────────────
                await log("[1/6] Fetching the package list…", flush=True)
                await check()
                packages = await client.list_packages(community)
                total    = len(packages)
                await log("Found {} packages".format(total), flush=True)
                await check()

                # ── [2/6] Extraction (dedicated thread) ─────────────────────
                await log("[2/6] Extracting mod details…", flush=True)
                mod_details, skipped = await loop.run_in_executor(
                    executor, _extract_all, packages)
                if skipped:
                    await log(
                        "⚠ {} malformed/empty package record(s) skipped (see server log)"
                        .format(skipped),
                        flush=True)
                packages = []  # free the raw package data from memory
                await log("Extracted {}/{} mods".format(len(mod_details), total), flush=True)
                await check()

                # Category blacklist filter
                blacklist = set(config.get("blacklist_categories", []))
                if blacklist:
                    before = len(mod_details)
                    mod_details = [m for m in mod_details
                                   if m["metadata"]["category"] not in blacklist]
                    await log(
                        "Blacklist: {} mods removed ({} categories)".format(
                            before - len(mod_details), len(blacklist)),
                        flush=True,
                    )

                # Filter out mods with no readme
                if config.get("skip_empty_readme", False):
                    before = len(mod_details)
                    mod_details = [m for m in mod_details if m["readme"].strip()]
                    skipped = before - len(mod_details)
                    if skipped:
                        await log("{} mods with no README skipped".format(skipped), flush=True)

                # Lightweight index
                index = [
                    {"name": m["name"], "category": m["metadata"]["category"],
                     "version": m["metadata"]["version"], "downloads": m["metadata"]["downloads"]}
                    for m in mod_details
                ]
                (job_dir / "mods_index.json").write_text(
                    json.dumps(index, indent=2), encoding="utf-8")

                # ── [3/6] Grouping ───────────────────────────────────────────
                await log("[3/6] Grouping by category…", flush=True)
                await check()
                cats      = await client.get_categories(community)
                cat_names = (
                    [c["name"] for c in cats if isinstance(c, dict) and c.get("name")]
                    if cats
                    else list({m["metadata"]["category"] for m in mod_details})
                )
                groups = smart_grouping(
                    mod_details, cat_names,
                    max_groups=_cfg_int(config, "max_groups"),
                    max_words_per_file=_cfg_int(config, "max_words_per_file"),
                )
                await log("Produced {} groups".format(len(groups)), flush=True)
                (job_dir / "category_groups.json").write_text(
                    json.dumps({k: len(v) for k, v in groups.items()}, indent=2),
                    encoding="utf-8")
                # Sanitized, unique file stems for every group (shared by the
                # HTML/PDF stage and the md/txt stage).
                stems = _unique_stems(list(groups))
                # Split artifacts of an oversized PDF get their names from
                # used_part_stems, seeded with every group stem (see below).

                # Requested formats (at least one; "pdf" by default if config is empty/invalid)
                formats = [f for f in (config.get("export_formats") or []) if f in EXPORT_FORMATS]
                if not formats:
                    formats = ["pdf"]
                if "pdf" in formats and not WEASYPRINT_AVAILABLE:
                    await log(
                        "⚠ PDF unavailable (missing GTK3 runtime: {}). "
                        "Install GTK3 for Windows (https://github.com/tschoonj/GTK-for-Windows-Runtime-Environment-Installer/releases) "
                        "then restart. PDF export skipped for this job.".format(WEASYPRINT_ERROR),
                        flush=True)
                    formats = [f for f in formats if f != "pdf"] or ["txt"]
                await log("Export formats: {}".format(", ".join(formats)), flush=True)

                # ── [4/6] Building the source files ──────────────────────────
                # PDF goes through an intermediate HTML step (WeasyPrint render);
                # Markdown and TXT are written directly into data/output.
                await log("[4/6] Building files ({} group(s))…".format(len(groups)),
                          flush=True)
                final_output = self.output_dir / community
                final_output.mkdir(parents=True, exist_ok=True)

                html_dir = job_dir / "html"
                if "pdf" in formats:
                    html_dir.mkdir(exist_ok=True)

                # Text generators -> written directly into the final output folder
                text_builders = []
                if "md" in formats:
                    text_builders.append((".md", build_group_markdown))
                if "txt" in formats:
                    text_builders.append((".txt", build_group_txt))

                n_text = 0
                for group_name, mods in groups.items():
                    await check()
                    stem = stems[group_name]
                    if "pdf" in formats:
                        content = await loop.run_in_executor(
                            executor, build_group_html, group_name, mods)
                        (html_dir / "{}.html".format(stem)).write_text(content, encoding="utf-8")
                    for ext, builder in text_builders:
                        text = await loop.run_in_executor(executor, builder, group_name, mods)
                        (final_output / "{}{}".format(stem, ext)).write_text(text, encoding="utf-8")
                        n_text += 1
                await log("Source files built ({} text file(s))".format(n_text), flush=True)

                # ── [5/6] PDF conversion ─────────────────────────────────────
                n_pdf = 0
                if "pdf" in formats:
                    await log("[5/6] Converting to PDF…", flush=True)
                    pdf_dir     = job_dir / "pdf"
                    pdf_dir.mkdir(exist_ok=True)
                    workers_pdf = _cfg_int(config, "workers_pdf")
                    sem_pdf     = asyncio.Semaphore(workers_pdf)
                    max_size_mb = _cfg_float(config, "max_size_mb")
                    # Names claimed by split artifacts (every group's output
                    # stem is reserved up front, so an artifact can never
                    # clobber another group's PDF/MD/TXT — e.g. a part of "X"
                    # won't overwrite a group literally called "X_part1").
                    # Artifacts of a given group are created sequentially, and
                    # the pool (workers_pdf) bounds overall parallelism: two
                    # parts never race for the same name.
                    used_part_stems: set = set(stems.values())

                    async def convert_one(gname: str):
                        async with sem_pdf:
                            await check()
                            stem      = stems[gname]
                            html_path = html_dir / "{}.html".format(stem)
                            pdf_path  = pdf_dir  / "{}.pdf".format(stem)
                            try:
                                def _render(hp=html_path, op=pdf_path):
                                    HTML(filename=str(hp)).write_pdf(str(op))
                                await loop.run_in_executor(executor, _render)

                                size_mb = pdf_path.stat().st_size / (1024 * 1024)
                                if size_mb > max_size_mb:
                                    await log("PDF {} too large ({:.1f} MB), splitting…".format(stem, size_mb),
                                              flush=True)
                                    def _split(pp=pdf_path, sd=pdf_dir, sn=stem, sm=size_mb):
                                        pdf       = Pdf.open(str(pp))
                                        try:
                                            n_pages   = len(pdf.pages)
                                            part_size = max(1, int(n_pages / (sm / max_size_mb)) + 1)
                                            for idx, start in enumerate(range(0, n_pages, part_size)):
                                                part = Pdf.new()
                                                for p in pdf.pages[start:start + part_size]:
                                                    part.pages.append(p)
                                                part.save(str(sd / "{}.pdf".format(_split_part_stem(sn, idx + 1, used_part_stems))))
                                        finally:
                                            pdf.close()
                                        pp.unlink()
                                    await loop.run_in_executor(executor, _split)

                                await log("Converted {}".format(stem), flush=True)
                            except asyncio.CancelledError:
                                raise
                            except Exception as exc:
                                await log("Error converting {}: {}".format(stem, exc), flush=True)

                    await asyncio.gather(*[convert_one(n) for n in groups])
                    await log("PDF generation complete", flush=True)

                    # Copy the PDFs (possibly split) to the final output
                    for pdf_file in pdf_dir.glob("*.pdf"):
                        shutil.copy(pdf_file, final_output / pdf_file.name)
                        n_pdf += 1
                else:
                    await log("[5/6] PDF not requested — step skipped.", flush=True)

                # ── [6/6] Summary ─────────────────────────────────────────────
                await log("[6/6] Finalizing…", flush=True)
                await log(
                    "✓ DONE — {} PDF(s), {} text file(s) in data/output/{}/".format(
                        n_pdf, n_text, community),
                    flush=True,
                )

            job["status"] = "done"

        except asyncio.CancelledError:
            await log("⏹ Job stopped.", flush=True)
            job["status"] = "stopped"
        except Exception as exc:
            await log("✗ ERROR: {}".format(exc), flush=True)
            job["status"] = "error"
        finally:
            # Teardown must survive a failing flush (e.g. job dir removed out
            # from under us) so the terminal state is always persisted.
            try:
                await flush()
            except Exception as exc:
                logger.error("Failed to flush job log for %s: %s", job_id, exc)
            try:
                executor.shutdown(wait=False, cancel_futures=True)
            except Exception as exc:  # pragma: no cover — defensive
                logger.error("Failed to shut down executor for %s: %s", job_id, exc)
            logs = self.logs.get(job_id)
            if logs is not None:
                logs.append(END_MARKER)
            self._save_jobs()
            # Keep the task out of the control registry (finished)
            ctrl = self._ctrl.get(job_id)
            if ctrl:
                ctrl.pop("task", None)
