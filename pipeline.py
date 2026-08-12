import asyncio
import json
import os
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

# Supported export formats -> produced file extension
EXPORT_FORMATS = {"pdf": ".pdf", "md": ".md", "txt": ".txt"}

# Dedicated thread pool for CPU-bound operations (WeasyPrint, extraction)
_executor = ThreadPoolExecutor(max_workers=4)

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
    latest   = versions[0] if versions else {}
    changelogs = [
        "## Version {}\n{}".format(v["version_number"], v["changelog"])
        for v in versions if v.get("changelog")
    ]
    cats = pkg.get("categories") or []
    if isinstance(cats, list) and cats:
        category = cats[0]
    elif isinstance(cats, str) and cats:
        category = cats
    else:
        category = "Uncategorized"

    return {
        "name": "{}/{}".format(owner, name),
        "metadata": {
            "version":      latest.get("version_number", "N/A"),
            "author":       owner,
            "downloads":    pkg.get("total_downloads") or pkg.get("downloads", 0),
            "dependencies": latest.get("dependencies", []),
            "category":     category,
        },
        "readme":    latest.get("readme") or latest.get("description") or "",
        "changelog": "\n\n".join(changelogs),
    }


def _extract_all(packages: list[dict]) -> list[dict]:
    """Extracts all packages in a single pass (dedicated thread)."""
    results = []
    for pkg in packages:
        m = _extract_mod(pkg)
        if m:
            results.append(m)
    return results


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
                self.jobs = json.loads(self.jobs_file.read_text())
            except Exception:
                self.jobs = {}
        else:
            self.jobs = {}

        # Legacy statuses (before aligning on the wiki-archive vocabulary)
        _legacy_status = {"cancelled": "stopped", "failed": "error"}
        for jid, job in self.jobs.items():
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
        self.jobs_file.write_text(json.dumps(self.jobs, indent=2))

    # ── state exposed to the API / SPA ──────────────────────────────────────────
    def meta(self, job_id: str) -> dict:
        """Rich state dict expected by the SPA (sidebar + detail)."""
        job = self.jobs[job_id]
        community = job["community"]
        out = self.output_dir / community
        files = []
        if out.exists():
            for f in sorted(out.iterdir()):
                if f.is_file() and f.suffix.lower() in EXPORT_FORMATS.values():
                    files.append({"name": f.name, "size_mb": round(f.stat().st_size / 1_048_576, 2)})
        return {
            "id":         job_id,
            "community":  community,
            "status":     job["status"],
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
        job_id = datetime.now().strftime("%Y%m%d-%H%M%S-") + os.urandom(2).hex()
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

    def start(self, job_id: str) -> None:
        """Starts (or restarts) a job's pipeline as an asyncio task."""
        job = self.jobs[job_id]
        if job["status"] == "running":
            return
        # Start with a clean log
        self.logs[job_id] = []
        lf = self.log_file(job_id)
        if lf.exists():
            lf.unlink()
        job["status"] = "running"
        self._save_jobs()
        task = asyncio.create_task(self._run(job_id))
        self._ctrl.setdefault(job_id, {})["task"] = task

    def pause(self, job_id: str) -> bool:
        ctrl = self._ctrl.get(job_id)
        if not ctrl or self.jobs[job_id]["status"] != "running":
            return False
        ctrl["pause_event"].clear()
        self.jobs[job_id]["status"] = "paused"
        self._save_jobs()
        return True

    def resume(self, job_id: str) -> None:
        """Resumes a paused job, or restarts a stopped/finished/errored job."""
        ctrl = self._ctrl.get(job_id)
        if ctrl and self.jobs[job_id]["status"] == "paused":
            ctrl["pause_event"].set()
            self.jobs[job_id]["status"] = "running"
            self._save_jobs()
        else:
            self.start(job_id)

    def stop(self, job_id: str) -> bool:
        ctrl = self._ctrl.get(job_id)
        if not ctrl:
            return False
        if "cancel_event" in ctrl:
            ctrl["cancel_event"].set()
        if "pause_event" in ctrl:
            ctrl["pause_event"].set()  # unblock if paused
        return True

    def delete(self, job_id: str) -> None:
        self.stop(job_id)
        job_dir = self.jobs_dir / job_id
        if job_dir.exists():
            shutil.rmtree(job_dir)
        self.jobs.pop(job_id, None)
        self.logs.pop(job_id, None)
        self._ctrl.pop(job_id, None)
        self._save_jobs()

    # ── pipeline execution ──────────────────────────────────────────────────────
    async def _run(self, job_id: str) -> None:
        job       = self.jobs[job_id]
        community = job["community"]
        config    = job.get("params", {})
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
                concurrency=int(config.get("workers_api", 4)),
                delay=float(config.get("delay", 0.2)),
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
                mod_details = await loop.run_in_executor(_executor, _extract_all, packages)
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
                    [c["name"] for c in cats]
                    if cats
                    else list({m["metadata"]["category"] for m in mod_details})
                )
                groups = smart_grouping(
                    mod_details, cat_names,
                    max_groups=int(config.get("max_groups", 99)),
                    max_words_per_file=int(config.get("max_words_per_file", 200_000)),
                )
                await log("Produced {} groups".format(len(groups)), flush=True)
                (job_dir / "category_groups.json").write_text(
                    json.dumps({k: len(v) for k, v in groups.items()}, indent=2),
                    encoding="utf-8")

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
                    safe = group_name.replace("/", "_")
                    if "pdf" in formats:
                        content = await loop.run_in_executor(
                            _executor, build_group_html, group_name, mods)
                        (html_dir / "{}.html".format(safe)).write_text(content, encoding="utf-8")
                    for ext, builder in text_builders:
                        text = await loop.run_in_executor(_executor, builder, group_name, mods)
                        (final_output / "{}{}".format(safe, ext)).write_text(text, encoding="utf-8")
                        n_text += 1
                await log("Source files built ({} text file(s))".format(n_text), flush=True)

                # ── [5/6] PDF conversion ─────────────────────────────────────
                n_pdf = 0
                if "pdf" in formats:
                    await log("[5/6] Converting to PDF…", flush=True)
                    pdf_dir     = job_dir / "pdf"
                    pdf_dir.mkdir(exist_ok=True)
                    sem_pdf     = asyncio.Semaphore(int(config.get("workers_pdf", 2)))
                    max_size_mb = float(config.get("max_size_mb", 100))

                    async def convert_one(gname: str):
                        async with sem_pdf:
                            await check()
                            safe      = gname.replace("/", "_")
                            html_path = html_dir / "{}.html".format(safe)
                            pdf_path  = pdf_dir  / "{}.pdf".format(safe)
                            try:
                                def _render(hp=html_path, op=pdf_path):
                                    HTML(filename=str(hp)).write_pdf(str(op))
                                await loop.run_in_executor(_executor, _render)

                                size_mb = pdf_path.stat().st_size / (1024 * 1024)
                                if size_mb > max_size_mb:
                                    await log("PDF {} too large ({:.1f} MB), splitting…".format(safe, size_mb),
                                              flush=True)
                                    def _split(pp=pdf_path, sd=pdf_dir, sn=safe, sm=size_mb):
                                        pdf       = Pdf.open(str(pp))
                                        n_pages   = len(pdf.pages)
                                        part_size = max(1, int(n_pages / (sm / max_size_mb)) + 1)
                                        for idx, start in enumerate(range(0, n_pages, part_size)):
                                            part = Pdf.new()
                                            for p in pdf.pages[start:start + part_size]:
                                                part.pages.append(p)
                                            part.save(str(sd / "{}_part{}.pdf".format(sn, idx + 1)))
                                        pdf.close()
                                        pp.unlink()
                                    await loop.run_in_executor(_executor, _split)

                                await log("Converted {}".format(safe), flush=True)
                            except asyncio.CancelledError:
                                raise
                            except Exception as exc:
                                await log("Error converting {}: {}".format(safe, exc), flush=True)

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
            await flush()
            self.logs[job_id].append(END_MARKER)
            self._save_jobs()
            # Keep the task out of the control registry (finished)
            ctrl = self._ctrl.get(job_id)
            if ctrl:
                ctrl.pop("task", None)
