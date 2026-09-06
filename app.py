import asyncio
import json
import os
import tempfile
import zipfile
from pathlib import Path

from fastapi import FastAPI, Request, HTTPException, Form
from fastapi.responses import HTMLResponse, FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.background import BackgroundTask

from pipeline import JobManager, END_MARKER
from thunderstore import ThunderstoreClient
from configs import load_config, save_config, is_valid_community

BASE_DIR = Path(__file__).parent

app = FastAPI(title="Thunderstore Archive")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")

job_manager = JobManager()


def _valid_community_or_400(community: str) -> str:
    """Rejects community names that could escape their folder/URL (path traversal)."""
    if not is_valid_community(community):
        raise HTTPException(
            400,
            "Invalid community name — use letters, digits, '.', '_' or '-' "
            "(Thunderstore slug), max 128 characters.",
        )
    return community


# ── Single-page app ──────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html")


# ── Jobs API ─────────────────────────────────────────────────────────────────────

@app.get("/api/jobs")
async def list_jobs():
    return job_manager.list_meta()


@app.post("/api/jobs")
async def create_job(community: str = Form(...), config: str = Form("{}")):
    community = _valid_community_or_400(community)
    try:
        config_dict = json.loads(config)
    except json.JSONDecodeError:
        raise HTTPException(400, "Invalid JSON config")
    if not isinstance(config_dict, dict):
        raise HTTPException(400, "Config must be a JSON object")
    job_id = job_manager.create(community, config_dict)
    return job_manager.meta(job_id)


@app.get("/api/jobs/{job_id}/status")
async def job_status(job_id: str):
    if job_id not in job_manager.jobs:
        raise HTTPException(404, "Job not found")
    return job_manager.meta(job_id)


@app.post("/api/jobs/{job_id}/start")
async def start_job(job_id: str):
    if job_id not in job_manager.jobs:
        raise HTTPException(404, "Job not found")
    await job_manager.start(job_id)
    return {"ok": True}


@app.post("/api/jobs/{job_id}/pause")
async def pause_job(job_id: str):
    if job_id not in job_manager.jobs:
        raise HTTPException(404, "Job not found")
    job_manager.pause(job_id)
    return {"ok": True}


@app.post("/api/jobs/{job_id}/resume")
async def resume_job(job_id: str):
    if job_id not in job_manager.jobs:
        raise HTTPException(404, "Job not found")
    await job_manager.resume(job_id)
    return {"ok": True}


@app.post("/api/jobs/{job_id}/stop")
async def stop_job(job_id: str):
    if job_id not in job_manager.jobs:
        raise HTTPException(404, "Job not found")
    job_manager.stop(job_id)
    return {"ok": True}


@app.delete("/api/jobs/{job_id}")
async def delete_job(job_id: str):
    if job_id not in job_manager.jobs:
        raise HTTPException(404, "Job not found")
    await job_manager.delete(job_id)
    return {"ok": True}


# ── Log stream (replayed from from_pos, then followed live) ──────────────────────

@app.get("/api/jobs/{job_id}/logs")
async def stream_logs(job_id: str, from_pos: int = 0):
    if job_id not in job_manager.jobs:
        raise HTTPException(404, "Job not found")

    async def _stream():
        pos = from_pos
        while True:
            if job_id not in job_manager.jobs:
                # Job deleted mid-stream: end the SSE instead of sending
                # keep-alives forever for a job that no longer exists.
                return
            chunk = job_manager.log_slice(job_id, pos)
            for line in chunk:
                pos += 1
                if line == END_MARKER:
                    yield "data: __END__\n\n"
                    return
                yield f"data: {line}\n\n"
            if not chunk:
                yield ": keepalive\n\n"
            await asyncio.sleep(0.25)

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/jobs/{job_id}/download-log")
async def download_job_log(job_id: str):
    log_path = job_manager.log_file(job_id)
    if not log_path.exists():
        raise HTTPException(404, "Log file not found")
    return FileResponse(
        log_path, media_type="application/octet-stream", filename=f"{job_id}.log"
    )


# ── ZIP download of the final PDFs ────────────────────────────────────────────────

@app.get("/api/jobs/{job_id}/download-pdfs")
async def download_job_pdfs(job_id: str):
    job = job_manager.jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    community  = job.get("community", job_id)
    output_dir = job_manager.output_dir / community
    if not output_dir.exists():
        raise HTTPException(404, "No output — the job may not have finished")
    exts  = {".pdf", ".md", ".txt"}
    files = [f for f in output_dir.iterdir() if f.is_file() and f.suffix.lower() in exts]
    if not files:
        raise HTTPException(404, "No files in the output folder")

    tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    tmp.close()
    with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            zf.write(f, f.name)

    # The temp file is deleted after sending (avoids a disk leak).
    return FileResponse(
        tmp.name,
        media_type="application/zip",
        filename=f"{community}-export.zip",
        background=BackgroundTask(os.unlink, tmp.name),
    )


# ── Output folder path (to copy into the file explorer) ──────────────────────────

def _in_docker() -> bool:
    """True if running inside a container (host path != internal /app path)."""
    return Path("/.dockerenv").exists() or os.environ.get("IN_DOCKER") == "1"


def _host_output_path(community: str) -> str:
    """
    Output folder path as seen by the user on their machine.
    In Docker, the container only knows /app/data; we rebuild the host path
    from HOST_DATA_DIR (the ./data folder on the host side) if provided,
    otherwise we fall back to a path relative to the project.
    """
    host_base = os.environ.get("HOST_DATA_DIR")
    if host_base:
        return str(Path(host_base) / "output" / community)
    if _in_docker():
        # No configuration provided: fall back to a path relative to the project folder on the host.
        return os.path.join("data", "output", community)
    return str((job_manager.output_dir / community).resolve())


@app.post("/api/jobs/{job_id}/reveal")
async def reveal_output(job_id: str):
    """Returns the output folder path (to copy/paste into the file explorer)."""
    job = job_manager.jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    community = job.get("community", job_id)
    out_dir   = job_manager.output_dir / community
    if not out_dir.exists():
        raise HTTPException(404, "No output — the job may not have finished")
    return {"path": _host_output_path(community)}


# ── Communities & categories ──────────────────────────────────────────────────────

@app.get("/api/communities")
async def list_communities():
    try:
        async with ThunderstoreClient() as client:
            communities = await client.list_communities()
        return [
            {
                "name": c["name"],
                "slug": c.get("identifier") or c.get("slug") or c["name"].lower(),
            }
            for c in communities
        ]
    except Exception as exc:
        raise HTTPException(502, f"Thunderstore unreachable: {exc}")


@app.get("/api/communities/{community}/categories")
async def get_community_categories(community: str):
    community = _valid_community_or_400(community)
    try:
        async with ThunderstoreClient() as client:
            # raise_on_error: a network failure must show up as a 502 in the
            # UI, not masquerade as "this community has no categories".
            cats = await client.get_categories(community, raise_on_error=True)
        return [c.get("name") or c.get("slug") or str(c) for c in cats if c]
    except Exception as exc:
        raise HTTPException(502, f"Failed to fetch categories: {exc}")


# ── Configs saved per community ───────────────────────────────────────────────────

@app.get("/api/configs/{community}")
async def get_config(community: str):
    community = _valid_community_or_400(community)
    return load_config(community)


@app.post("/api/configs/{community}")
async def post_config(community: str, request: Request):
    community = _valid_community_or_400(community)
    data = await request.json()
    if not isinstance(data, dict):
        raise HTTPException(400, "Config must be a JSON object")
    save_config(community, data)
    return {"status": "saved"}
