"""FastAPI app: upload footage + music, run a render job, stream progress,
serve the finished Reel for in-browser preview + download."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from engine import packstore
from engine.config import PACKS, PRESETS, RenderConfig
from engine.musicscan import analyze_track
from . import jobs

logger = logging.getLogger("reel")


def _check_env() -> None:
    """Fail loudly at startup if the tool's hard dependencies are missing,
    instead of surfacing a cryptic ffmpeg error mid-render."""
    missing = [t for t in ("ffmpeg", "ffprobe") if not shutil.which(t)]
    if missing:
        logger.error("missing binaries on PATH: %s — rendering will fail",
                     ", ".join(missing))
    for d in (jobs.WORK_ROOT, jobs.OUTPUT_ROOT):
        try:
            d.mkdir(parents=True, exist_ok=True)
            probe = d / ".write-test"
            probe.write_text("")
            probe.unlink()
        except OSError:
            logger.error("work dir not writable: %s", d)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    _check_env()
    # rehydrate jobs from the previous server run, then sweep orphaned dirs
    # (load first so live dirs are never swept), and trim finished jobs to the
    # keep-N window
    loaded = jobs.load_jobs()
    if loaded:
        logger.info("rehydrated %d job(s) from disk", loaded)
        jobs.prune_finished_jobs()
    freed = jobs.prune_orphan_jobs()
    if freed:
        logger.info("pruned %.0f MB of orphaned job dirs", freed / 1e6)
    yield


app = FastAPI(title="Reel Clipper", lifespan=_lifespan)

STATIC = Path(__file__).parent / "static"
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"}
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}

STAGING = jobs.WORK_ROOT / "staging"
_TOKEN_RE = re.compile(r"^mus_[0-9a-f]{32}\.[a-z0-9]+$")
STAGING_KEEP = 8                       # most-recent staged tracks to retain
STAGING_MAX_BYTES = 150 * 1024 * 1024  # per-track upload cap
CLIP_MAX_BYTES = 4 * 1024 * 1024 * 1024  # per-clip upload cap (4K raw footage)


def _staged_path(token: str) -> Path:
    """Resolve a music token to a staged file, guarding against traversal."""
    if not _TOKEN_RE.match(token or ""):
        raise HTTPException(400, "bad music token")
    p = (STAGING / token).resolve()
    if p.parent != STAGING.resolve() or not p.exists():
        raise HTTPException(404, "staged track not found")
    return p


def _prune_staging(keep: int = STAGING_KEEP) -> None:
    """Keep only the most-recent staged tracks so /api/analyze-music can't grow
    the staging dir without bound across repeated uploads."""
    if not STAGING.exists():
        return
    files = sorted(STAGING.glob("mus_*"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    for p in files[keep:]:
        try:
            p.unlink()
        except OSError:
            pass


def _save_capped(dst: Path, up: UploadFile, limit: int,
                 label: str = "file") -> None:
    total = 0
    with dst.open("wb") as f:
        while chunk := up.file.read(1 << 20):
            total += len(chunk)
            if total > limit:
                f.close()
                dst.unlink(missing_ok=True)
                raise HTTPException(
                    413, f"{label} too large ({limit // (1 << 20)} MB max)")
            f.write(chunk)


def _unique_path(dir: Path, name: str) -> Path:
    """A non-colliding path: two uploads named IMG_1234.MOV get -1/-2 suffixes
    instead of silently overwriting each other (they may be different clips
    from different folders)."""
    dst = dir / name
    stem, suffix = Path(name).stem, Path(name).suffix
    i = 1
    while dst.exists():
        dst = dir / f"{stem}-{i}{suffix}"
        i += 1
    return dst


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (STATIC / "index.html").read_text()


@app.get("/api/presets")
def presets() -> dict:
    return {"presets": [{"key": p.key, "name": p.name} for p in PRESETS.values()],
            "packs": [{"key": k, **v} for k, v in PACKS.items()],
            "saved": [{"key": p["id"], **p} for p in packstore.list_packs()]}


async def _json_body(request: Request) -> dict:
    """Parse a JSON object body, or 400. Kept separate from downstream
    try/excepts so a malformed body can't be misreported as their errors."""
    raw = await request.body()
    if not raw:
        return {}
    try:
        body = json.loads(raw)
    except ValueError:
        raise HTTPException(400, "malformed JSON body")
    if not isinstance(body, dict):
        raise HTTPException(400, "body must be a JSON object")
    return body


@app.post("/api/packs")
async def create_pack(request: Request) -> dict:
    """Persist an extracted style as a named pack (appears next to built-ins)."""
    body = await _json_body(request)
    pack_in = body.get("pack")
    if pack_in is not None and not isinstance(pack_in, dict):
        raise HTTPException(400, "'pack' must be a JSON object")
    try:
        pack = packstore.save(pack_in or {}, body.get("name") or "")
    except ValueError as e:
        raise HTTPException(409, str(e))
    return {"pack": {"key": pack["id"], **pack}}


@app.delete("/api/packs/{pid}")
def delete_pack(pid: str) -> dict:
    if not packstore.delete(pid):
        raise HTTPException(404, "pack not found")
    return {"ok": True}


@app.post("/api/analyze-music")
async def analyze_music(music: UploadFile, target: float = Form(18.0)):
    """Stage an uploaded track, analyze it, and return suggested sections +
    a reuse token so the render can grab the staged file without re-uploading."""
    ext = Path(music.filename or "track.mp3").suffix.lower()
    if ext not in AUDIO_EXTS and ext not in VIDEO_EXTS:
        raise HTTPException(400, "unsupported audio format")
    STAGING.mkdir(parents=True, exist_ok=True)
    _prune_staging()
    token = f"mus_{uuid.uuid4().hex}{ext}"
    dst = STAGING / token
    _save_capped(dst, music, STAGING_MAX_BYTES, "track")
    try:
        analysis = await asyncio.to_thread(analyze_track, str(dst), float(target))
    except Exception as e:  # noqa: BLE001
        dst.unlink(missing_ok=True)
        raise HTTPException(500, f"analysis failed: {e}")
    if not analysis.get("sections"):
        dst.unlink(missing_ok=True)
        raise HTTPException(422, "couldn't find usable sections in this track")
    return {"token": token, "analysis": analysis}


@app.post("/api/analyze-style")
async def analyze_style(reference: UploadFile):
    """Extract an applicable style pack from an uploaded reference reel.
    The file is only needed during extraction — the resulting pack travels in
    the render config — so it's deleted immediately after."""
    from engine.styleref import extract_style
    ext = Path(reference.filename or "ref.mp4").suffix.lower()
    if ext not in VIDEO_EXTS:
        raise HTTPException(400, "unsupported video format")
    STAGING.mkdir(parents=True, exist_ok=True)
    # ref_ prefix: distinct from the mus_ token namespace so analyze-music's
    # prune can't unlink an in-flight reference (and this transient file can't
    # evict a still-referenced staged music track)
    tmp = STAGING / f"ref_{uuid.uuid4().hex}{ext}"
    _save_capped(tmp, reference, STAGING_MAX_BYTES, "reference reel")
    try:
        pack = await asyncio.to_thread(
            extract_style, str(tmp), Path(reference.filename or "reel").stem)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(422, f"couldn't analyze this reel: {e}")
    finally:
        tmp.unlink(missing_ok=True)
    return {"pack": pack}


@app.get("/api/music-sections")
async def music_sections(token: str, target: float = 18.0):
    """Re-analyze an already-staged track (e.g. when the reel length changes)."""
    p = _staged_path(token)
    analysis = await asyncio.to_thread(analyze_track, str(p), float(target))
    return {"analysis": analysis}


def _client_config(raw: str) -> tuple[str, dict]:
    """Parse the client's `config` form field into (mode, cfg_dict).

    Malformed JSON or a non-object body is a 400, not a 500. Server-managed
    paths (`clips`, `music`, `export_audio`) are dropped: they come from
    uploads / `_attach_music`, and honoring a client-supplied path would let
    the request point ffmpeg at (and with `export_audio`, write to) any file
    on disk.
    """
    try:
        body = json.loads(raw or "{}")
    except ValueError:
        raise HTTPException(400, "malformed JSON config")
    if not isinstance(body, dict):
        raise HTTPException(400, "config must be a JSON object")
    mode = "storyboard" if body.pop("storyboard", False) else "full"
    for k in ("clips", "music", "export_audio"):
        body.pop(k, None)
    return mode, body


@app.post("/api/jobs")
async def create_job(
    clips: list[UploadFile],
    music: UploadFile | None = None,
    music_token: str | None = Form(None),
    config: str = Form("{}"),
):
    mode, cfg_dict = _client_config(config)
    cfg = RenderConfig.from_dict(cfg_dict)
    problems = cfg.validate()
    if problems:
        raise HTTPException(400, "invalid config: " + "; ".join(problems))

    # stage a job dir and stream uploads to disk; if an upload fails (size
    # cap / disk error) the never-started job is removed entirely instead of
    # lingering as a "queued" registry entry with partial files
    tmp = jobs.WORK_ROOT / "staging"
    tmp.mkdir(parents=True, exist_ok=True)
    job = jobs.create_job(cfg, mode=mode)
    try:
        saved_clips = _save_clips(job, clips)
        if not saved_clips:
            raise HTTPException(400, "No valid video clips uploaded.")
        cfg.clips = saved_clips
        _attach_music(job, cfg, music, music_token)
    except Exception:
        jobs.abandon(job)
        raise

    if mode == "storyboard":
        jobs.start_storyboard(job)
    else:
        jobs.start_job(job)
    return {"job_id": job.id}


def _save_clips(job: jobs.Job, clips: list[UploadFile]) -> list[str]:
    clip_dir = job.workdir / "clips"
    clip_dir.mkdir(exist_ok=True)
    saved: list[str] = []
    for up in clips:
        ext = Path(up.filename or "clip.mp4").suffix.lower()
        if ext not in VIDEO_EXTS:
            continue
        dst = _unique_path(clip_dir, Path(up.filename).name)
        _save_capped(dst, up, CLIP_MAX_BYTES, "clip")
        saved.append(str(dst))
    return saved


def _attach_music(job: jobs.Job, cfg: RenderConfig,
                  music: UploadFile | None, music_token: str | None) -> None:
    if music is not None and music.filename:
        mext = Path(music.filename).suffix.lower()
        if mext in AUDIO_EXTS or mext in VIDEO_EXTS:
            mdst = job.workdir / f"music{mext}"
            _save_capped(mdst, music, STAGING_MAX_BYTES, "music")
            cfg.music = str(mdst)
    elif music_token:
        # reuse the file already staged by /api/analyze-music (no re-upload)
        staged = _staged_path(music_token)
        mdst = job.workdir / f"music{staged.suffix}"
        shutil.copyfile(staged, mdst)
        cfg.music = str(mdst)


@app.get("/api/jobs")
def jobs_list() -> dict:
    """Recent jobs (newest first) for the UI's job panel."""
    return {"jobs": jobs.list_jobs()}


@app.get("/api/jobs/{jid}")
def job_status(jid: str) -> dict:
    job = jobs.get_job(jid)
    if not job:
        raise HTTPException(404, "job not found")
    return job.snapshot()


@app.post("/api/jobs/{jid}/cancel")
def cancel_job(jid: str) -> dict:
    job = jobs.get_job(jid)
    if not job:
        raise HTTPException(404, "job not found")
    if not jobs.cancel(job):
        raise HTTPException(409, "job can't be cancelled in its current state")
    return {"ok": True}


@app.get("/api/jobs/{jid}/events")
async def job_events(jid: str, request: Request) -> StreamingResponse:
    job = jobs.get_job(jid)
    if not job:
        raise HTTPException(404, "job not found")

    async def gen():
        last = None
        while True:
            if await request.is_disconnected():
                break
            snap = job.snapshot()
            key = (snap["status"], snap["stage"], snap["pct"])
            if key != last:
                yield f"data: {json.dumps(snap)}\n\n"
                last = key
            if snap["status"] in ("done", "error", "storyboard", "cancelled"):
                break
            await asyncio.sleep(0.25)

    return StreamingResponse(gen(), media_type="text/event-stream")


_THUMB_FP = re.compile(r"^[0-9a-f]{20}$")
_THUMB_NAME = re.compile(r"^c\d{4}\.jpg$")


def _thumb_url(path: str) -> str:
    """Map a cache thumb path (work/analysis_cache/<fp>/cNNNN.jpg) to its URL."""
    p = Path(path)
    if p.parent.parent.name == "analysis_cache":
        return f"/api/thumbs/{p.parent.name}/{p.name}"
    return ""


@app.get("/api/thumbs/{fp}/{name}")
def thumb(fp: str, name: str) -> FileResponse:
    from engine.cache import CACHE_ROOT
    if not _THUMB_FP.match(fp) or not _THUMB_NAME.match(name):
        raise HTTPException(400, "bad thumb path")
    p = CACHE_ROOT / fp / name
    if not p.exists():
        raise HTTPException(404, "thumb not found")
    return FileResponse(str(p), media_type="image/jpeg",
                        headers={"Cache-Control": "max-age=86400"})


@app.get("/api/jobs/{jid}/storyboard")
def storyboard(jid: str) -> dict:
    job = jobs.get_job(jid)
    if not job or job.plan is None:
        raise HTTPException(404, "storyboard not ready")
    plan = job.plan
    slots = []
    for i, d in enumerate(plan["picks"]):
        slots.append({
            "slot": i,
            "time": round(plan["cut_points"][i], 2),
            "length": d["length"],
            "src": Path(d["src"]).name,
            "start": d["start"],
            "thumb": _thumb_url(d.get("thumb_jpg", "")),
            "day": d.get("day", 0),
            "caption": d.get("caption", ""),   # per-slot override ("" = none)
            "effects": [x for x in (d["trans"], d["outro"], d["ramp"])
                        if x and x != "none"],
        })
    bench = [{"index": i, "src": Path(b["src"]).name,
              "center": b["center"], "thumb": _thumb_url(b.get("thumb_jpg", ""))}
             for i, b in enumerate(job.bench)]
    return {"slots": slots, "bench": bench,
            "tempo": plan["grid"]["tempo"], "target": plan["target"]}


@app.get("/api/jobs/{jid}/plan.json")
def job_plan(jid: str) -> Response:
    """Download the edit plan + config (re-render via `reel --plan`)."""
    job = jobs.get_job(jid)
    if not job or job.plan is None:
        raise HTTPException(404, "plan not ready")
    payload = json.dumps({"cfg": job.cfg.to_dict(), "plan": job.plan}, indent=2)
    return Response(payload, media_type="application/json",
                    headers={"Content-Disposition":
                             f"attachment; filename=reel_{jid}.plan.json"})


@app.post("/api/jobs/{jid}/render")
async def render_edited(jid: str, request: Request) -> dict:
    job = jobs.get_job(jid)
    if not job or job.plan is None:
        raise HTTPException(404, "job not found or not analyzed")
    if job.status not in ("storyboard", "done", "error"):
        raise HTTPException(409, "job is busy")
    if job.archived:
        # clips were cleaned up when the job dir was pruned — nothing to re-cut
        raise HTTPException(409, "job was archived — its clips were cleaned up; "
                                 "start a new job to re-render")
    body = await _json_body(request)
    raw = body.get("edits") or {}
    if not isinstance(raw, dict):
        raise HTTPException(400, "'edits' must be an object")
    order = raw.get("order")
    if order is not None and not isinstance(order, list):
        raise HTTPException(400, "'order' must be a list")
    swaps_raw, trims_raw = raw.get("swaps") or [], raw.get("trims") or []
    if not isinstance(swaps_raw, list) or not isinstance(trims_raw, list):
        raise HTTPException(400, "'swaps'/'trims' must be lists")
    # bench refs (bench_index) resolve to concrete src/center server-side;
    # malformed entries are skipped, never 500 (garbage from a stale client)
    n_slots = len(job.plan["picks"])
    swaps = []
    for e in swaps_raw:
        if not isinstance(e, dict):
            continue
        try:
            i, b = int(e.get("slot", -1)), int(e.get("bench_index", -1))
        except (TypeError, ValueError):
            continue
        if 0 <= i < n_slots and 0 <= b < len(job.bench):
            src_bench = job.bench[b]
            swaps.append({"slot": i, "src": src_bench["src"],
                          "center": src_bench["center"],
                          "thumb_jpg": src_bench.get("thumb_jpg", "")})
    trims = []
    for t in trims_raw:
        if not isinstance(t, dict):
            continue
        try:
            trims.append({"slot": int(t.get("slot", -1)),
                          "delta": float(t.get("delta", 0.0))})
        except (TypeError, ValueError):
            continue
    caps_raw = raw.get("captions") or []
    if not isinstance(caps_raw, list):
        raise HTTPException(400, "'captions' must be a list")
    captions = []
    for c in caps_raw:
        if not isinstance(c, dict) or "caption" not in c or c["caption"] is None:
            continue        # null would str() into the literal "None" on screen
        try:
            slot = int(c.get("slot", -1))
        except (TypeError, ValueError):
            continue
        if 0 <= slot < n_slots:
            captions.append({"slot": slot, "caption": str(c["caption"])})
    jobs.start_render(job, {"order": order, "swaps": swaps, "trims": trims,
                            "captions": captions})
    return {"ok": True}


@app.get("/api/jobs/{jid}/output.mp4")
def job_output(jid: str) -> FileResponse:
    if not re.fullmatch(r"[0-9a-f]{12}", jid or ""):
        raise HTTPException(404, "job not found")
    job = jobs.get_job(jid)
    out = job.find_output() if job else None
    if not out:
        # pruned in a PREVIOUS session: no registry entry, but the preserved
        # output may still be in work/outputs — keep the link best-effort
        preserved = jobs.OUTPUT_ROOT / f"{jid}.mp4"
        if preserved.exists():
            out = preserved
    if not out:
        raise HTTPException(404, "output not ready")
    return FileResponse(str(out), media_type="video/mp4",
                        filename=_share_filename(job, out))


def _share_filename(job, out: Path) -> str:
    """A social-friendly download name: reel_<date>_<location>.mp4.

    `job` may be None (pruned in a previous session); fall back to the file's
    mtime + a generic location.
    """
    import datetime
    loc = "reel"
    if job is not None and getattr(job.cfg, "location", ""):
        loc = re.sub(r"[^a-z0-9]+", "-", job.cfg.location.strip().lower())
        loc = loc.strip("-") or "reel"
    try:
        stamp = datetime.datetime.fromtimestamp(out.stat().st_mtime)
    except OSError:
        stamp = datetime.datetime.now()
    return f"reel_{stamp:%Y-%m-%d}_{loc}.mp4"


app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")
