"""Render-job registry with background execution, progress state, and
cancellation.

Two job modes:
- full: prepare + render in one background run (classic flow)
- storyboard: prepare only -> status "storyboard" with an editable plan;
  a later start_render(edits) call renders (content-addressed segments make
  re-renders after small edits cheap).

Concurrency: jobs run on a small worker pool (MAX_CONCURRENT), so a burst of
uploads queues instead of thrashing the machine with parallel ffmpeg passes.
Cancellation: `cancel(job)` sets a per-job event and terminates the ffmpeg
process currently running on the job's worker thread (see engine.ffmpeg);
stage-boundary checks turn that into a clean "cancelled" status.

Disk hygiene: finished (done/error/cancelled) job dirs are pruned to the most
recent KEEP_FINISHED_JOBS; orphaned dirs from previous server runs are swept
at startup (they are unreachable — the registry is in-memory).
"""
from __future__ import annotations

import json
import logging
import os
import queue
import re
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from engine import ffmpeg
from engine.config import RenderConfig
from engine.render import apply_edits, build_bench, prepare, render_plan
from engine.selection import Pick

logger = logging.getLogger("reel.jobs")

WORK_ROOT = Path("work/jobs")
OUTPUT_ROOT = Path("work/outputs")     # finished outputs survive job-dir pruning

# Tunable via env for a local install (REEL_MAX_CONCURRENT, REEL_KEEP_JOBS,
# REEL_KEEP_OUTPUTS)
MAX_CONCURRENT = int(os.environ.get("REEL_MAX_CONCURRENT", "2"))
KEEP_FINISHED_JOBS = int(os.environ.get("REEL_KEEP_JOBS", "6"))
KEEP_OUTPUTS = int(os.environ.get("REEL_KEEP_OUTPUTS", "100"))


class _Cancelled(Exception):
    """Internal: a job was cancelled between stages."""


_HINTS = (
    ("no usable video clips", "no playable video files — check the clips are mp4/mov/hevc and not empty"),
    ("no usable moments", "clips produced no usable shots — they may be too short, too dark, or blurred"),
    ("could not build a cut grid", "the music track is too short to cut to"),
    ("ffprobe failed", "ffmpeg/ffprobe may be missing — install with `brew install ffmpeg`"),
    ("no video stream", "a clip has no video stream — it may be audio-only or corrupted"),
    ("audio decode failed", "the audio couldn't be decoded — try a different music file"),
    ("error opening", "a file couldn't be opened — it may be corrupted or an unsupported format"),
)


def hint_for_error(message: str) -> str | None:
    """Map a render error to a one-line human hint (None if nothing matches)."""
    msg = (message or "").lower()
    for needle, hint in _HINTS:
        if needle in msg:
            return hint
    return None


@dataclass
class Job:
    id: str
    cfg: RenderConfig
    workdir: Path
    out_path: Path
    mode: str = "full"              # full | storyboard
    status: str = "queued"          # queued | running | storyboard | done | error | cancelled
    stage: str = "queued"
    pct: float = 0.0
    error: Optional[str] = None
    plan: Optional[dict] = None
    bench: list = field(default_factory=list)
    reused: int = 0
    rendered: int = 0
    archived: bool = False          # finished, workdir pruned, output preserved
    # cancellation bookkeeping (not serialized)
    _cancel: threading.Event = field(default_factory=threading.Event,
                                     repr=False, compare=False)
    _tid: Optional[int] = None      # worker thread id while running

    def snapshot(self) -> dict:
        return {
            "id": self.id, "status": self.status, "stage": self.stage,
            "pct": round(self.pct, 4), "error": self.error, "mode": self.mode,
            "reused": self.reused, "rendered": self.rendered,
            "archived": self.archived,
            "output": f"/api/jobs/{self.id}/output.mp4" if self.status == "done" else None,
        }

    def find_output(self) -> Optional[Path]:
        """The finished reel's file: the live workdir copy while it exists,
        else the preserved copy in OUTPUT_ROOT (survives job-dir pruning)."""
        if self.out_path.exists():
            return self.out_path
        preserved = OUTPUT_ROOT / f"{self.id}.mp4"
        return preserved if preserved.exists() else None

    # ---- persistence (jobs survive a server restart) -------------------------

    def to_dict(self) -> dict:
        return {
            "id": self.id, "cfg": self.cfg.to_dict(), "mode": self.mode,
            "status": self.status, "stage": self.stage, "pct": self.pct,
            "error": self.error, "plan": self.plan, "bench": self.bench,
            "reused": self.reused, "rendered": self.rendered,
        }

    @staticmethod
    def from_dict(d: dict) -> "Job":
        workdir = WORK_ROOT / str(d["id"])
        job = Job(id=str(d["id"]), cfg=RenderConfig.from_dict(d.get("cfg") or {}),
                  workdir=workdir, out_path=workdir / "output.mp4",
                  mode=str(d.get("mode") or "full"))
        job.status = str(d.get("status") or "error")
        job.stage = str(d.get("stage") or job.status)
        job.pct = float(d.get("pct") or 0.0)
        job.error = d.get("error")
        job.plan = d.get("plan")
        job.bench = d.get("bench") or []
        job.reused = int(d.get("reused") or 0)
        job.rendered = int(d.get("rendered") or 0)
        return job


_JOBS: dict[str, Job] = {}
_ARCHIVE: dict[str, Job] = {}   # pruned done jobs whose output was preserved
_LOCK = threading.Lock()


def _save_job(job: Job) -> None:
    """Persist the job to work/jobs/<id>/job.json (atomic write)."""
    try:
        tmp = job.workdir / "job.json.tmp"
        tmp.write_text(json.dumps(job.to_dict()))
        tmp.replace(job.workdir / "job.json")
    except (OSError, TypeError):
        logger.exception("failed to persist job %s", job.id)


def load_jobs() -> int:
    """Rehydrate jobs from job.json files left by a previous server run.
    Jobs that were mid-flight (queued/running) become 'error — interrupted'.
    Returns the number loaded. Call BEFORE prune_orphan_jobs so live dirs
    aren't swept."""
    if not WORK_ROOT.exists():
        return 0
    n = 0
    with _LOCK:
        for p in sorted(WORK_ROOT.iterdir()):
            jf = p / "job.json"
            if not p.is_dir() or not jf.exists():
                continue
            # ids are hex job dirs; anything else is not ours to interpret
            if not re.fullmatch(r"[0-9a-f]{12}", p.name):
                continue
            try:
                job = Job.from_dict(json.loads(jf.read_text()))
                if job.status in ("queued", "running"):
                    job.status = "error"
                    job.stage = "error"
                    job.error = "interrupted by server restart"
                _JOBS[job.id] = job
                n += 1
            except Exception:               # noqa: BLE001 — skip corrupt files
                logger.exception("skipping corrupt job.json %s", jf)
    return n

# ---- worker pool -------------------------------------------------------------

_QUEUE: "queue.Queue[tuple[Job, Callable[[], None]]]" = queue.Queue()
_WORKERS: list[threading.Thread] = []
_WORKER_LOCK = threading.Lock()


def _ensure_workers() -> None:
    with _WORKER_LOCK:
        # drop dead workers so a thread that died from BaseException can be
        # respawned instead of silently degrading the pool forever
        _WORKERS[:] = [t for t in _WORKERS if t.is_alive()]
        while len(_WORKERS) < MAX_CONCURRENT:
            t = threading.Thread(target=_worker_loop, daemon=True,
                                 name="reel-worker")
            t.start()
            _WORKERS.append(t)


def _worker_loop() -> None:
    while True:
        job, fn = _QUEUE.get()
        if job._cancel.is_set():
            job.status = "cancelled"
            job.stage = "cancelled"
            prune_finished_jobs()     # cancelled-while-queued jobs still count
            continue
        try:
            fn()
        except Exception:           # noqa: BLE001 — phase fns catch their own
            logger.exception("unhandled error in worker")


# ---- job lifecycle -----------------------------------------------------------

def create_job(cfg: RenderConfig, mode: str = "full") -> Job:
    jid = uuid.uuid4().hex[:12]
    workdir = WORK_ROOT / jid
    workdir.mkdir(parents=True, exist_ok=True)
    job = Job(id=jid, cfg=cfg, workdir=workdir, out_path=workdir / "output.mp4",
              mode=mode)
    with _LOCK:
        _JOBS[jid] = job
    _save_job(job)
    return job


def get_job(jid: str) -> Optional[Job]:
    with _LOCK:
        return _JOBS.get(jid) or _ARCHIVE.get(jid)


def _job_mtime(j: Job) -> float:
    """Newest-ness for the panel: the workdir job.json while it exists, else
    the preserved output's mtime (archived jobs have no workdir)."""
    for p in (j.workdir / "job.json", OUTPUT_ROOT / f"{j.id}.mp4"):
        if p.exists():
            try:
                return p.stat().st_mtime
            except OSError:
                continue
    return 0.0


def list_jobs(limit: int = 20) -> list[dict]:
    with _LOCK:
        all_jobs = list(_JOBS.values()) + [
            j for j in _ARCHIVE.values() if j.id not in _JOBS]
        all_jobs.sort(key=_job_mtime, reverse=True)
        return [j.snapshot() for j in all_jobs[:limit]]


def _cb(job: Job):
    """Progress callback that doubles as a stage-boundary cancel check."""
    def cb(stage: str, pct: float):
        if job._cancel.is_set():
            raise _Cancelled()
        job.stage = stage
        job.pct = pct
    return cb


def _run(job: Job, phase: Callable[[], None]) -> None:
    """Execute a phase on a worker thread; map outcomes onto job status."""
    job.status = "running"
    job.stage = "running"
    job._tid = threading.get_ident()
    # Route all ffmpeg subprocesses of this job (including any thread-pool
    # workers spawned inside render_plan) to this tid, so cancel() can
    # terminate the whole tree at once.
    tok = ffmpeg.set_cancel_tid(job._tid)
    try:
        phase()
        if job.status == "running":     # storyboard phase sets its own status
            job.status = "done"
            job.stage = "done"
            job.pct = 1.0
    except _Cancelled:
        # the cancel event fired between stages — kill anything still in
        # flight that cancel() couldn't reach (registration race)
        if job._tid is not None:
            ffmpeg.cancel_thread(job._tid)
        job.status = "cancelled"
        job.stage = "cancelled"
    except Exception as e:              # noqa: BLE001
        if job._cancel.is_set():
            job.status = "cancelled"    # ffmpeg was terminated mid-flight
            job.stage = "cancelled"
        else:
            job.status = "error"
            job.error = str(e)
            hint = hint_for_error(str(e))
            if hint:
                job.error = f"{e}\nHint: {hint}"
            logger.exception("job %s failed", job.id)
    finally:
        ffmpeg.reset_cancel_tid(tok)
        job._tid = None
        if job.workdir.exists():
            _save_job(job)      # persist before pruning (prune may rmtree)
        prune_finished_jobs()


def _phase_full(job: Job) -> None:
    plan, pool = prepare(job.cfg, str(job.workdir), _cb(job))
    job.plan = plan
    res = render_plan(job.cfg, str(job.workdir), plan,
                      str(job.out_path), _cb(job))
    job.reused, job.rendered = res["reused"], res["rendered"]


def _phase_storyboard(job: Job) -> None:
    plan, pool = prepare(job.cfg, str(job.workdir), _cb(job))
    job.plan = plan
    pf = set(Pick.__dataclass_fields__)  # type: ignore[attr-defined]
    picks = [Pick(**{k: v for k, v in d.items() if k in pf})
             for d in plan["picks"]]
    job.bench = build_bench(pool, picks)
    (job.workdir / "plan.json").write_text(json.dumps(plan))
    job.status = "storyboard"
    job.stage = "storyboard"
    job.pct = 0.42


def _phase_render(job: Job, edits: Optional[list]) -> None:
    plan = apply_edits(job.plan, edits or [], job.cfg)
    job.plan = plan
    (job.workdir / "plan.json").write_text(json.dumps(plan))
    res = render_plan(job.cfg, str(job.workdir), plan,
                      str(job.out_path), _cb(job))
    job.reused, job.rendered = res["reused"], res["rendered"]


def start_job(job: Job) -> None:
    """Queue phase 1: full mode renders everything; storyboard mode stops
    after prepare() and exposes the editable plan."""
    job._cancel.clear()             # a cancelled job can be re-queued fresh
    _ensure_workers()
    _QUEUE.put((job, lambda: _run(job, lambda: _phase_full(job))))


def start_storyboard(job: Job) -> None:
    job._cancel.clear()
    _ensure_workers()
    _QUEUE.put((job, lambda: _run(job, lambda: _phase_storyboard(job))))


def start_render(job: Job, edits: Optional[list] = None) -> None:
    """Queue phase 2 for storyboard jobs: apply slot edits, render."""
    job._cancel.clear()
    _ensure_workers()
    _QUEUE.put((job, lambda: _run(job, lambda: _phase_render(job, edits))))


def abandon(job: Job) -> None:
    """Remove a job that never ran (e.g. upload failed mid-stream): drop it
    from the registry and delete its partial workdir immediately."""
    with _LOCK:
        _JOBS.pop(job.id, None)
        _ARCHIVE.pop(job.id, None)
    shutil.rmtree(job.workdir, ignore_errors=True)   # also removes job.json


def cancel(job: Job) -> bool:
    """Request cancellation of a queued or running job. Terminates the ffmpeg
    process in flight (if any); the worker maps the interrupt to "cancelled"."""
    if job.status not in ("queued", "running"):
        return False
    job._cancel.set()
    if job.status == "queued":
        job.status = "cancelled"
        job.stage = "cancelled"
        _save_job(job)
        return True
    if job._tid is not None:
        from engine import ffmpeg
        ffmpeg.cancel_thread(job._tid)
    return True


# ---- disk hygiene ------------------------------------------------------------

def _dir_size(p: Path) -> int:
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


def _preserve_output(job: Job) -> Optional[Path]:
    """Move a finished job's output.mp4 into OUTPUT_ROOT so the download link
    survives the workdir being pruned. Returns the preserved path or None."""
    if not job.out_path.exists():
        return None
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    preserved = OUTPUT_ROOT / f"{job.id}.mp4"
    try:
        shutil.move(str(job.out_path), str(preserved))
        return preserved
    except OSError:
        logger.exception("failed to preserve output %s", job.id)
        return None


def _prune_outputs(keep: int = KEEP_OUTPUTS) -> None:
    """Keep only the most-recent preserved outputs (bounded disk growth);
    drop archive entries whose file was just removed (dead download links)."""
    if not OUTPUT_ROOT.exists():
        return
    outputs = sorted(OUTPUT_ROOT.glob("*.mp4"),
                     key=lambda p: p.stat().st_mtime, reverse=True)
    with _LOCK:
        for p in outputs[keep:]:
            p.unlink(missing_ok=True)
            _ARCHIVE.pop(p.stem, None)


def prune_finished_jobs(keep: int = KEEP_FINISHED_JOBS) -> int:
    """Delete the oldest finished (done/error/cancelled) job dirs beyond
    `keep`. Never touches queued/running/storyboard jobs. Finished outputs are
    preserved into OUTPUT_ROOT first, so download links outlive the dirs; done
    jobs with a preserved output move to the in-memory archive (panel row +
    download stay alive; render-again is blocked — clips are gone).
    Returns bytes freed. The registry lock is only held for the selection +
    bookkeeping, never across the (potentially multi-GB) rmtree."""
    with _LOCK:
        finished = [j for j in _JOBS.values()
                    if j.status in ("done", "error", "cancelled")]
        finished.sort(key=lambda j: j.workdir.stat().st_mtime
                      if j.workdir.exists() else 0.0)
        # oldest (len - keep) finished jobs are doomed; NOTE: [:-0] == [] in
        # Python, so slice by count, not negative index
        doomed = finished[:max(0, len(finished) - keep)]
    freed = 0
    for j in doomed:
        preserved = None
        if j.workdir.exists():
            freed += _dir_size(j.workdir)
            if j.status == "done":
                preserved = _preserve_output(j)   # keep the download alive
            shutil.rmtree(j.workdir, ignore_errors=True)
        elif j.status == "done":
            p = OUTPUT_ROOT / f"{j.id}.mp4"
            preserved = p if p.exists() else None
        with _LOCK:
            if _JOBS.get(j.id) is j:            # re-checked: may have been re-run
                del _JOBS[j.id]
                if preserved is not None and j.status == "done":
                    j.archived = True
                    _ARCHIVE[j.id] = j
    if doomed:
        _prune_outputs()
    return freed


def prune_orphan_jobs(max_age_s: float = 3600.0,
                      keep_newest: int = 3) -> int:
    """Sweep work/jobs/<id> dirs with no live in-memory job (left by previous
    server runs) that are older than `max_age_s`. The newest few orphan dirs
    are always kept (a favorite output from last session shouldn't vanish on
    restart). Returns bytes freed."""
    if not WORK_ROOT.exists():
        return 0
    with _LOCK:
        live = set(_JOBS)
    orphans = [p for p in WORK_ROOT.iterdir()
               if p.is_dir() and p.name not in live and p.name != "staging"]
    orphans.sort(key=lambda p: p.stat().st_mtime if p.stat().st_mtime else 0.0,
                 reverse=True)
    freed = 0
    for p in orphans[keep_newest:]:
        try:
            if p.stat().st_mtime > time.time() - max_age_s:
                continue      # fresh — might belong to a concurrent process
        except OSError:
            continue
        freed += _dir_size(p)
        shutil.rmtree(p, ignore_errors=True)
    return freed
