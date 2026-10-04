"""Thin subprocess wrappers around ffmpeg / ffprobe.

Keeps every shell-out in one place: consistent logging, error surfacing, and
a helper that streams `-progress` so the web UI can show a real progress bar.

Cancellation: each long-running subprocess is registered per-thread in
`_ACTIVE`, so a job worker can terminate the ffmpeg process currently running
on its thread (see `cancel_thread` / `active_proc`). `run` uses
`Popen.communicate`, which drains stdout AND stderr concurrently — no pipe
deadlock even when ffmpeg dumps a lot of stderr on an error path.
"""
from __future__ import annotations

import contextvars
import json
import re
import subprocess
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Optional, Sequence

FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"

# Base flags applied to every ffmpeg invocation.
_BASE = [FFMPEG, "-hide_banner", "-y", "-loglevel", "error", "-nostdin"]


class FFmpegError(RuntimeError):
    pass


# ---- cancellation plumbing ---------------------------------------------------

# One *set* of subprocesses per cancellation scope id (normally a job worker's
# thread id). A job may run several ffmpeg subprocesses concurrently (parallel
# segment renders on a thread pool), so a scope tracks all of them.
_ACTIVE: dict[int, set[subprocess.Popen]] = {}
_ACTIVE_LOCK = threading.Lock()
_CANCEL_TID: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "reel_cancel_tid", default=None)


def set_cancel_tid(tid: int) -> contextvars.Token:
    """Route this thread's (and any thread-pool threads it spawns — contextvars
    propagate) active_proc registrations to `tid`, so one cancel() can reach
    every ffmpeg subprocess of the job no matter which worker runs it."""
    return _CANCEL_TID.set(tid)


def get_cancel_tid() -> int | None:
    """The cancellation scope id active on this thread, if any."""
    return _CANCEL_TID.get()


def reset_cancel_tid(token: contextvars.Token) -> None:
    _CANCEL_TID.reset(token)


@contextmanager
def active_proc(proc: subprocess.Popen):
    """Track `proc` as an active subprocess under the current cancellation
    scope (a job worker tid, or this thread when no scope is set)."""
    tid = _CANCEL_TID.get() or threading.get_ident()
    with _ACTIVE_LOCK:
        _ACTIVE.setdefault(tid, set()).add(proc)
    try:
        yield
    finally:
        with _ACTIVE_LOCK:
            s = _ACTIVE.get(tid)
            if s is not None:
                s.discard(proc)
                if not s:
                    del _ACTIVE[tid]


def cancel_thread(tid: int) -> bool:
    """Terminate every subprocess currently registered under scope `tid`."""
    with _ACTIVE_LOCK:
        procs = list(_ACTIVE.get(tid, ()))
    killed = False
    for proc in procs:
        if proc.poll() is None:
            try:
                proc.terminate()
                killed = True
            except OSError:
                pass
    return killed


# ---- plain runs --------------------------------------------------------------

def run(args: Sequence[str], *, desc: str = "ffmpeg") -> None:
    """Run an ffmpeg command (args come *after* the base flags)."""
    cmd = _BASE + list(args)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    # The proc is registered immediately after Popen, so a job cancel can
    # terminate it (cancel_thread) and communicate() returns promptly. The
    # bounded wait is belt-and-braces: it caps a cancel that misses the
    # registration window AND prevents a wedged ffmpeg from holding a worker
    # forever. 20 min is far above any legit single op (segment ~seconds,
    # mux ~seconds) — only a genuinely hung process hits it.
    with active_proc(proc):
        try:
            _out, err = proc.communicate(timeout=1200)
        except subprocess.TimeoutExpired:
            proc.kill()
            _out, err = proc.communicate()
    if proc.returncode != 0:
        raise FFmpegError(
            f"{desc} failed (exit {proc.returncode}).\n"
            f"cmd: {' '.join(cmd)}\n"
            f"stderr:\n{err[-4000:]}"
        )


def run_with_progress(
    args: Sequence[str],
    *,
    total_frames: Optional[int] = None,
    on_progress: Optional[Callable[[float], None]] = None,
    desc: str = "ffmpeg",
) -> None:
    """Run ffmpeg, parsing `-progress pipe:1` to report 0..1 completion."""
    cmd = _BASE + list(args) + ["-progress", "pipe:1", "-nostats"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    err_lines: list[str] = []
    t = threading.Thread(target=lambda: err_lines.extend(proc.stderr or ()),
                         daemon=True, name="ffmpeg-stderr")
    t.start()
    frame_re = re.compile(r"^frame=(\d+)")
    assert proc.stdout is not None
    try:
        with active_proc(proc):
            for line in proc.stdout:
                if on_progress and total_frames:
                    m = frame_re.match(line.strip())
                    if m:
                        on_progress(min(1.0, int(m.group(1)) / max(1, total_frames)))
    finally:
        # bounded wait + kill escalation: a cancel that lands just before this
        # Popen was registered can't terminate it, so _Cancelled would otherwise
        # stall here until ffmpeg finishes on its own
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        t.join(timeout=5)
    if proc.returncode != 0:
        err = "".join(err_lines)
        raise FFmpegError(
            f"{desc} failed (exit {proc.returncode}).\n"
            f"cmd: {' '.join(cmd)}\n"
            f"stderr:\n{err[-4000:]}"
        )


# ---- ffprobe helpers ---------------------------------------------------------

def probe(path: str | Path) -> dict:
    """Return ffprobe JSON for streams + format."""
    out = subprocess.run(
        [FFPROBE, "-v", "error", "-print_format", "json",
         "-show_streams", "-show_format", str(path)],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        raise FFmpegError(f"ffprobe failed for {path}: {out.stderr}")
    return json.loads(out.stdout)


def duration(path: str | Path) -> float:
    info = probe(path)
    return float(info.get("format", {}).get("duration", 0.0) or 0.0)
