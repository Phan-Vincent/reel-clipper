"""Orchestrator: raw footage + music -> finished 9:16 Reel.

Pipeline: probe -> beats -> analyze -> select -> render segments -> concat ->
effects+overlay -> mux music. Exposes `render()` (used by the web app with a
progress callback) and a `main()` CLI.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from . import ffmpeg
from .analysis import rescore
from .assemble import (concat_segments, effects_and_overlay, export_audio,
                       mux_music, render_segment)
from .beats import BeatGrid, detect_beats, strong_onset_times
from .cache import cached_analyze
from .chapters import allocate_slots, detect_chapters
from .config import PACKS, RenderConfig, ASPECTS, FPS_CHOICES
from .probe import is_video, video_info
from .selection import Pick, build_picks
from .transitions import RAMP_MIN_FPS, ramp_source_needed

ProgressCb = Optional[Callable[[str, float], None]]

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".hevc"}


def _emit(cb: ProgressCb, stage: str, pct: float) -> None:
    if cb:
        cb(stage, max(0.0, min(1.0, pct)))


def collect_clips(paths: list[str]) -> list[str]:
    """Expand folders to the video files inside; keep explicit files."""
    out: list[str] = []
    for p in paths:
        pp = Path(p)
        if pp.is_dir():
            for f in sorted(pp.iterdir()):
                if f.suffix.lower() in VIDEO_EXTS and is_video(str(f)):
                    out.append(str(f))
        elif pp.suffix.lower() in VIDEO_EXTS:
            out.append(str(pp))
    return out


def _slot_onsets(grid: BeatGrid, slot_starts: list[float]) -> list[float]:
    """Onset strength of the beat nearest each slot start (for energy matching).
    Renormalized to this grid's own max so downstream thresholds are relative
    to the track's dynamics, not librosa's absolute scale."""
    if not grid.beats or not grid.onset_strength:
        return [0.0] * len(slot_starts)
    beats = np.asarray(grid.beats)
    strengths = np.asarray(grid.onset_strength)
    out = []
    for s in slot_starts:
        j = int(np.argmin(np.abs(beats - s)))
        out.append(float(strengths[j]) if abs(beats[j] - s) < 0.15 else 0.0)
    peak = max(out)
    if peak > 0:
        out = [o / peak for o in out]
    return out


def _assign_effects(picks, slot_onsets, cfg, durations) -> None:
    """Jointly place zoom transitions and speed ramps per cut, scaled by beat
    strength. Composes with the flash/glitch overlay pass."""
    n = len(picks)

    def onset(i: float) -> float:
        return slot_onsets[i] if i < len(slot_onsets) else 0.0

    # Adaptive accent thresholds: absolute cutoffs miss entire tracks whose
    # normalized onsets run low. "Strong" = top ~40% of this reel's accented
    # cuts, "mid" = top ~75%; unaccented cuts (0.0) never qualify.
    nz = sorted((o for o in slot_onsets if o > 0.01), reverse=True)
    if nz:
        strong_thr = nz[min(len(nz) - 1, max(0, round(len(nz) * 0.4) - 1))]
        mid_thr = nz[min(len(nz) - 1, max(0, round(len(nz) * 0.75) - 1))]
    else:
        strong_thr = mid_thr = 2.0   # no accents detected -> calm branch only

    if cfg.zoom_transitions:
        rot = 0
        wdir = 0
        for i, pk in enumerate(picks):
            o = onset(i)
            if i == 0:
                pk.trans = "blackzoom"          # open from black
            elif o >= strong_thr:                # strong beat: punchy, black rare
                style = ["whippan", "zoom", "whipzoom",
                         "whippan", "zoom", "blackzoom"][rot % 6]
                if style == "whippan" and cfg.effect_intensity < 0.45:
                    style = "zoom"               # a barely-blurred whip reads wrong
                if style == "whippan":
                    # paired directional motion-wipe across this cut
                    d = "l" if wdir % 2 == 0 else "r"
                    picks[i - 1].outro = f"whipout_{d}"
                    pk.trans = f"whipin_{d}"
                    wdir += 1
                else:
                    pk.trans = style
            elif o >= mid_thr:
                pk.trans = ["zoom", "whipzoom", "none"][rot % 3]
            else:
                pk.trans = ["none", "none", "zoom"][rot % 3]
            if pk.trans != "none":
                rot += 1

    if not cfg.speed_ramps:
        return

    # hero slow-mo holds: rare, on the most exciting long high-fps segments.
    # Feasibility (source duration incl. tail MARGIN) is checked HERE, before
    # trans is stripped — a hero demoted later by the re-clamp would leave the
    # previous pick's whip-out dangling as a one-sided glitch.
    MARGIN = 0.3
    budget = max(1, n // 12)
    heroes = sorted(
        (i for i in range(1, n)
         if picks[i].length >= 0.75
         and picks[i].src_fps >= RAMP_MIN_FPS["ramphold"]
         and ramp_source_needed("ramphold", picks[i].length)
             <= durations.get(picks[i].src, 0.0) - MARGIN - 0.05),
        key=lambda i: picks[i].excitement, reverse=True)[:budget]
    for i in heroes:
        picks[i].ramp = "ramphold"
        picks[i].trans = "none"                 # let the slow-mo breathe

    # whip-out INTO strong beats; punch-in bursts OUT of them (alternating)
    rot = 0
    for i in range(1, n):
        if onset(i) < strong_thr:
            continue
        prev, cur = picks[i - 1], picks[i]
        if prev.ramp == "none" and prev.length >= 0.45:
            prev.ramp = "rampout"
        if (cur.ramp == "none" and cur.length >= 0.45
                and cur.trans in ("zoom", "none") and rot % 2 == 0):
            cur.ramp = "rampin"
        rot += 1

    # re-clamp starts: a ramped segment consumes ramp_source_needed() seconds.
    # Keep MARGIN of tail slack — container duration overstates the decodable
    # stream (audio tails) and -ss frame alignment can drop a frame; a flush
    # clamp under-runs -frames:v and drifts the whole montage off-beat.
    for pk in picks:
        if pk.ramp == "none":
            continue
        need = ramp_source_needed(pk.ramp, pk.length)
        dur = durations.get(pk.src, 0.0)
        if need > dur - MARGIN - 0.05:
            pk.ramp = "none"
            continue
        mid = pk.start + pk.length / 2
        pk.start = round(min(max(0.0, mid - need / 2), dur - need - MARGIN), 3)


def prepare(cfg: RenderConfig, work_dir: str,
            on_progress: ProgressCb = None) -> tuple[dict, list]:
    """Phases 1-4 (probe -> beats -> analyze -> select). Returns a
    JSON-serializable edit plan + the full candidate pool (for the storyboard
    bench). Rendering happens separately in render_plan()."""
    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)
    clips = collect_clips(cfg.clips)
    if not clips:
        raise ValueError("No usable video clips found.")

    # --- probe -----------------------------------------------------------
    _emit(on_progress, "probe", 0.02)
    infos = {c: video_info(c) for c in clips}
    durations = {c: v.duration for c, v in infos.items()}

    # --- beats + cut grid ------------------------------------------------
    _emit(on_progress, "beats", 0.06)
    target = cfg.target_duration
    # If a start offset was chosen (music analyzer suggestion), trim the section
    # once here so beats AND mux both operate on the same chosen window.
    music_path = cfg.music
    if music_path and cfg.music_start > 0:
        # clamp so the full target still fits — a start near the end would
        # otherwise silently shrink the whole reel to whatever tail remains.
        avail = ffmpeg.duration(music_path)
        start = min(cfg.music_start, max(0.0, avail - cfg.target_duration))
        section = str(work / "music_section.wav")
        ffmpeg.run(["-ss", f"{start:.3f}", "-i", music_path,
                    "-t", f"{cfg.target_duration + 1.0:.3f}",
                    "-ac", "2", "-ar", "44100", section],
                   desc="trim music section")
        music_path = section
    if music_path:
        target = min(target, ffmpeg.duration(music_path))
    if music_path:
        grid = detect_beats(music_path)
    else:
        grid = BeatGrid(tempo=120.0, beats=[i * 0.5 for i in range(int(target / 0.5) + 1)])
    cut_points = grid.cut_points(cfg.cuts_per_beat, cfg.min_segment,
                                 cfg.max_segment, target)
    segment_lengths = [cut_points[i + 1] - cut_points[i]
                       for i in range(len(cut_points) - 1)]
    if not segment_lengths:
        raise ValueError("Could not build a cut grid (track too short?).")

    # --- analyze clips (disk-cached) -------------------------------------
    # Analysis is decode-bound (an ffmpeg subprocess per clip) + numpy/cv2
    # (both release the GIL), so a small thread pool overlaps clips. Results
    # are still collected in clip order (ThreadPoolExecutor.map preserves it).
    pool = []
    workers = max(1, min(len(clips), os.cpu_count() or 4, 8))
    cancel_tid = ffmpeg.get_cancel_tid()

    def _analyze(c: str) -> list:
        # contextvars don't propagate into ThreadPoolExecutor workers, so
        # re-establish the cancellation scope here for this pool thread.
        tok = ffmpeg.set_cancel_tid(cancel_tid) if cancel_tid is not None else None
        try:
            return cached_analyze(c, use_faces=cfg.use_faces)
        finally:
            if tok is not None:
                ffmpeg.reset_cancel_tid(tok)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for i, cands in enumerate(ex.map(_analyze, clips)):
            _emit(on_progress, "analyze", 0.06 + 0.34 * (i / len(clips)))
            pool.extend(cands)
    if not pool:
        raise ValueError("Clips produced no usable moments (too short/dark?).")
    rescore(pool, use_source_audio=cfg.use_source_audio)

    # --- select ----------------------------------------------------------
    _emit(on_progress, "select", 0.42)
    slot_onsets = _slot_onsets(grid, cut_points[:-1])
    # day == 0 means "hide the day counter" — that intent extends to chapters
    # (their whole output is day counters), so it doubles as the web opt-out
    chapters = (detect_chapters(clips)
                if cfg.auto_chapters and cfg.day != 0 else None)

    if chapters:
        # multi-day trip: chronological chapters, slots proportional to
        # each day's footage, selection runs per-day so days don't compete
        share = {d: sum(durations[c] for c in clips
                        if chapters.day_of[c] == d)
                 for d in range(1, chapters.n_days + 1)}
        alloc = allocate_slots(len(segment_lengths), share)
        picks = []
        s0 = 0
        for d in sorted(alloc):
            k = alloc[d]
            day_clips = {c for c in clips if chapters.day_of[c] == d}
            day_pool = [c for c in pool if c.src in day_clips]
            day_picks = build_picks(day_pool, segment_lengths[s0:s0 + k],
                                    durations, cfg, slot_onsets[s0:s0 + k])
            if len(day_picks) < k:      # day pool unusable -> global fallback
                day_picks = build_picks(pool, segment_lengths[s0:s0 + k],
                                        durations, cfg, slot_onsets[s0:s0 + k])
            for pk in day_picks:
                pk.day = d
            picks.extend(day_picks)
            s0 += k
    else:
        picks = build_picks(pool, segment_lengths, durations, cfg, slot_onsets)

    for pk in picks:
        pk.src_fps = infos[pk.src].fps
    _assign_effects(picks, slot_onsets, cfg, durations)

    # a new day opens from black (and never inherits half a whip-pan)
    for i in range(1, len(picks)):
        if picks[i].day != picks[i - 1].day:
            picks[i].trans = "blackzoom"
            picks[i - 1].outro = "none"

    # speech-to-text captions (optional dep): transcribe each clip once and
    # map the timed words onto each pick's window. Storyboard overrides still
    # win — a pick only gets an auto caption if none is set yet.
    if cfg.auto_captions:
        _emit(on_progress, "captions", 0.43)
        from .transcribe import captions_for_picks, transcribe_clip
        transcripts = {c: transcribe_clip(c) for c in clips}
        auto = captions_for_picks([asdict(pk) for pk in picks], transcripts)
        for i, text in auto.items():
            if not picks[i].caption:
                picks[i].caption = text

    # closed-loop grade tuning: fit the extracted grade to THIS footage so the
    # rendered output measures like the reference (content confounds cancel).
    tuned_grade, tune_trace = None, []
    if cfg.custom_grade and cfg.ref_stats and cfg.tune_grade:
        _emit(on_progress, "styletune", 0.43)
        from .styleref import fit_grade
        tuned_grade, tune_trace = fit_grade(
            cfg.ref_stats, cfg.custom_grade, picks, durations, cfg, work_dir)

    plan = {
        "target": target,
        "music_path": music_path,
        "tuned_grade": tuned_grade,
        "tune_trace": tune_trace,
        "cut_points": [round(c, 4) for c in cut_points],
        "grid": {"tempo": grid.tempo, "beats": grid.beats,
                 "onset_strength": grid.onset_strength},
        "durations": durations,
        "chapter_dates": (chapters.dates if chapters else {}),
        "picks": [asdict(pk) for pk in picks],
    }
    return plan, pool


def build_bench(pool: list, picks: list, n: int = 24) -> list[dict]:
    """Top unused, mutually-distinct candidates — the storyboard's swap bench."""
    def overlaps(c) -> bool:
        cm = (c.start + c.end) / 2
        return any(pk.src == c.src and abs(cm - (pk.start + pk.length / 2)) < 3.0
                   for pk in picks)

    bench, chosen = [], []
    for c in sorted(pool, key=lambda x: x.score, reverse=True):
        if c.exposure < 0.3 or overlaps(c):
            continue
        if any(float(c.thumb @ b.thumb) > 0.88
               or (c.src == b.src and abs(c.start - b.start) < 4.0)
               for b in chosen):
            continue
        chosen.append(c)
        bench.append({"src": c.src, "center": round((c.start + c.end) / 2, 3),
                      "score": round(float(c.score), 4), "thumb_jpg": c.thumb_jpg})
        if len(bench) >= n:
            break
    return bench


def _apply_swap(d: dict, e: dict, durations: dict) -> None:
    """Replace one pick with a bench shot (source + centered start), re-clamping
    ramps for the new source."""
    L = d["length"]
    center = float(e["center"])
    if not math.isfinite(center):
        return
    dur = float(durations.get(e["src"], 0.0)) or L
    d["src"] = e["src"]
    d["start"] = round(max(0.0, min(center - L / 2,
                                    max(0.0, dur - L))), 3)
    d["thumb_jpg"] = e.get("thumb_jpg", "")
    info = video_info(e["src"])
    d["src_fps"] = info.fps
    if d["ramp"] != "none":
        need = ramp_source_needed(d["ramp"], L)
        if (d["ramp"] == "ramphold" and info.fps < RAMP_MIN_FPS["ramphold"]) \
                or need > dur - 0.35:
            d["ramp"] = "none"
        else:
            mid = d["start"] + L / 2
            d["start"] = round(min(max(0.0, mid - need / 2),
                                   dur - need - 0.3), 3)


def _apply_trim(d: dict, delta: float, durations: dict) -> None:
    """Nudge a pick's window within its source (length stays beat-locked)."""
    if not delta or not math.isfinite(delta):
        return
    L = d["length"]
    dur = float(durations.get(d["src"], 0.0)) or L
    d["start"] = round(max(0.0, min(d["start"] + delta,
                                    max(0.0, dur - L))), 3)
    if d["ramp"] != "none":
        need = ramp_source_needed(d["ramp"], L)
        if need > dur - 0.35:
            d["ramp"] = "none"
        else:
            mid = d["start"] + L / 2
            d["start"] = round(min(max(0.0, mid - need / 2),
                                   dur - need - 0.3), 3)


def _segments_for(picks: list[dict], nframes: list[int], cfg: RenderConfig):
    """Per-segment overlay styles [(nframes, day, caption), ...].

    A pick's `day` is 0 when auto-chapters found nothing (single-day / no
    dates — the common case); fall back to cfg.day so the DAY badge still
    renders, exactly like the legacy day_ranges path did. A pick-level
    caption override wins, else the global caption.
    """
    return [(nf, d.get("day", 0) or cfg.day, d.get("caption") or cfg.caption)
            for d, nf in zip(picks, nframes)]


def apply_edits(plan: dict, edits, cfg: RenderConfig) -> dict:
    """Apply storyboard edits to a plan.

    `edits` is a dict with any of:
      order  — permutation of original slot indices (the new shot sequence)
      swaps  — [{"slot": orig_index, "src", "center", "thumb_jpg"}]
      trims  — [{"slot": orig_index, "delta": seconds}]
    swaps/trims are keyed by ORIGINAL slot index and applied at the slot's
    position after `order` (so edits travel with the shot when reordering).

    For backward compat, a legacy LIST of position-indexed {src, center}|None
    is accepted as swaps with no reorder.
    """
    durations = plan["durations"]
    picks = plan["picks"]

    if isinstance(edits, list):
        for i, e in enumerate(edits or []):
            if not e or i >= len(picks):
                continue
            _apply_swap(picks[i], e, durations)
        return plan

    order = edits.get("order") if isinstance(edits, dict) else None
    swaps = (edits.get("swaps") or []) if isinstance(edits, dict) else []
    trims = (edits.get("trims") or []) if isinstance(edits, dict) else []

    pos_of = {i: i for i in range(len(picks))}
    if order:
        n = len(picks)
        perm = [int(o) for o in order
                if isinstance(o, (int, float)) and not isinstance(o, bool)
                and math.isfinite(float(o)) and 0 <= int(o) < n]
        if len(perm) == n and sorted(perm) == list(range(n)):
            plan["picks"] = picks = [picks[i] for i in perm]
            pos_of = {orig: pos for pos, orig in enumerate(perm)}
            # boundaries follow the new shot sequence: flash/glitch decisions
            # and the storyboard "time" label read cut_points[i], which must
            # equal the actual montage boundary at position i (nframes-based,
            # exactly like render_plan computes starts[i])
            nf = [max(1, round(p["length"] * cfg.fps)) for p in picks]
            cps, acc = [0.0], 0.0
            for nf_ in nf[:-1]:
                acc += nf_ / cfg.fps
                cps.append(acc)
            plan["cut_points"] = [round(c, 4) for c in cps]

    for e in swaps:
        if not isinstance(e, dict) or "src" not in e or "center" not in e:
            continue
        pos = pos_of.get(int(e.get("slot", -1)), -1)
        if 0 <= pos < len(picks):
            _apply_swap(picks[pos], e, durations)
    for t in trims:
        if not isinstance(t, dict):
            continue
        pos = pos_of.get(int(t.get("slot", -1)), -1)
        if 0 <= pos < len(picks):
            _apply_trim(picks[pos], float(t.get("delta", 0.0)), durations)
    for c in (edits.get("captions") or []):
        if not isinstance(c, dict) or "caption" not in c:
            continue
        pos = pos_of.get(int(c.get("slot", -1)), -1)
        if 0 <= pos < len(picks):
            # per-slot caption override; empty string = clear (back to global)
            picks[pos]["caption"] = str(c["caption"]).strip()
    return plan


def _seg_key(d: dict, cfg: RenderConfig) -> str:
    grade = (repr(sorted(cfg.custom_grade.items()))
             if cfg.custom_grade else cfg.preset)
    payload = "|".join(str(x) for x in (
        d["src"], d["start"], d["length"], d["trans"], d["outro"], d["ramp"],
        cfg.fill, grade, cfg.quality, cfg.effect_intensity, cfg.fps,
        cfg.aspect, "sv1"))
    return hashlib.sha1(payload.encode()).hexdigest()[:16]


def render_plan(cfg: RenderConfig, work_dir: str, plan: dict, out_path: str,
                on_progress: ProgressCb = None) -> dict:
    """Render a prepared/edited plan. Segments are content-addressed, so
    re-renders after storyboard edits only pay for the slots that changed."""
    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)
    if plan.get("tuned_grade"):
        # closed-loop-tuned grade replaces the raw extraction for all segments
        # (fresh cfg copy; _seg_key hashes the tuned grade -> cache correct)
        import dataclasses
        cfg = dataclasses.replace(cfg, custom_grade=plan["tuned_grade"])
    pick_fields = {f for f in Pick.__dataclass_fields__}  # type: ignore[attr-defined]
    picks = [Pick(**{k: v for k, v in d.items() if k in pick_fields})
             for d in plan["picks"]]
    cut_points = plan["cut_points"]
    grid = BeatGrid(**plan["grid"])
    music_path = plan.get("music_path")

    # --- render segments (content-addressed, incremental) ----------------
    seg_paths: list[str] = []
    nframes: list[int] = []
    reused = 0
    tasks: dict[str, tuple] = {}   # unique missing sp -> (pick, sp, nf)
    for pk, d in zip(picks, plan["picks"]):
        sp = work / f"seg_{_seg_key(d, cfg)}.mp4"
        nf = max(1, round(pk.length * cfg.fps))
        seg_paths.append(str(sp))
        nframes.append(nf)
        if sp.exists():
            reused += 1
        else:
            # dedupe identical slots (same content-addressed key) so two
            # concurrent workers can never render to the same tmp path
            tasks.setdefault(str(sp), (pk, sp, nf))

    if tasks:
        workers = max(1, min(len(tasks), os.cpu_count() or 4, 8))
        cancel_tid = ffmpeg.get_cancel_tid()

        def _do(item):
            pk, sp, nf = item
            # contextvars don't propagate into ThreadPoolExecutor workers;
            # re-establish the cancellation scope for this pool thread.
            tok = ffmpeg.set_cancel_tid(cancel_tid) if cancel_tid is not None else None
            try:
                tmp = work / f"{sp.stem}.tmp.mp4"
                render_segment(pk, cfg, str(tmp))
                tmp.replace(sp)
                return nf
            finally:
                if tok is not None:
                    ffmpeg.reset_cancel_tid(tok)

        done = 0
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for _ in ex.map(_do, tasks.values()):
                done += 1
                _emit(on_progress, "render",
                      0.44 + 0.34 * ((reused + done) / len(picks)))

    # --- concat ----------------------------------------------------------
    _emit(on_progress, "assemble", 0.80)
    silent = str(work / "montage_silent.mp4")
    concat_segments(seg_paths, work, silent)
    montage_dur = sum(nframes) / cfg.fps

    # effect times: strong-onset cut boundaries mapped to montage time
    strong = strong_onset_times(grid, cut_points)
    starts = [sum(nframes[:i]) / cfg.fps for i in range(len(nframes))]
    flash_times, glitch_times = [], []
    for i in range(1, len(picks)):
        if round(cut_points[i], 3) in strong:
            flash_times.append(starts[i])
            if i % 3 == 0:
                glitch_times.append(starts[i])

    # --- effects + overlay ----------------------------------------------
    _emit(on_progress, "effects", 0.84)
    # auto-chapter day ranges in montage frames (contiguous same-day runs)
    day_ranges = []
    if any(d.get("day", 0) > 0 for d in plan["picks"]):
        f0 = 0
        for d, nf in zip(plan["picks"], nframes):
            dy = d.get("day", 0)
            if day_ranges and day_ranges[-1][2] == dy:
                day_ranges[-1][1] = f0 + nf
            else:
                day_ranges.append([f0, f0 + nf, dy])
            f0 += nf
    composed = str(work / "montage_ui.mp4")
    if cfg.quality == "draft":
        # cuts-only preview: skip the (expensive) effects+overlay re-encode
        # entirely — concat of the fast segments IS the draft
        composed = silent
    else:
        effects_and_overlay(cfg, silent, montage_dur, flash_times, glitch_times,
                            work, composed, on_progress,
                            day_ranges=day_ranges or None,
                            chapter_dates=plan.get("chapter_dates") or None,
                            segments=_segments_for(plan["picks"], nframes, cfg))

    # --- source audio (optional) --------------------------------------
    source_track = None
    if cfg.keep_source_audio:
        _emit(on_progress, "sourceaudio", 0.92)
        from .sourceaudio import build_source_track
        source_track = build_source_track(plan["picks"], work)

    # --- mux music -------------------------------------------------------
    _emit(on_progress, "mux", 0.96)
    mux_music(composed, music_path, montage_dur, out_path,
              source_track=source_track,
              keep_source_audio=cfg.keep_source_audio,
              duck_threshold=cfg.duck_threshold, duck_ratio=cfg.duck_ratio)

    # --- optional audio-only export --------------------------------------
    if cfg.export_audio and (music_path or source_track):
        _emit(on_progress, "export", 0.98)
        Path(cfg.export_audio).parent.mkdir(parents=True, exist_ok=True)
        export_audio(music_path, montage_dur, cfg.export_audio,
                     source_track=source_track,
                     keep_source_audio=cfg.keep_source_audio,
                     duck_threshold=cfg.duck_threshold,
                     duck_ratio=cfg.duck_ratio)

    _emit(on_progress, "done", 1.0)
    return {"out": out_path, "reused": reused,
            "rendered": len(picks) - reused}


def render(cfg: RenderConfig, work_dir: str, out_path: str,
           on_progress: ProgressCb = None) -> str:
    """One-shot pipeline (CLI + non-storyboard web jobs)."""
    plan, _pool = prepare(cfg, work_dir, on_progress)
    render_plan(cfg, work_dir, plan, out_path, on_progress)
    return out_path


# ---- CLI --------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="reel", description="Turn raw footage into an aesthetic 9:16 Reel.")
    p.add_argument("clips", nargs="*", help="clip files or a folder of clips")
    p.add_argument("-m", "--music", help="music track (.mp3/.wav)")
    p.add_argument("-o", "--out", default="output/reel.mp4", help="output mp4")
    p.add_argument("--location", default="TOKYO")
    p.add_argument("--day", type=int, default=1)
    p.add_argument("--duration", type=float, default=18.0)
    p.add_argument("--preset", default=None,
                   help="color grade (default cinematic, or the pack's)")
    p.add_argument("--fill", choices=["blur", "crop"], default="blur")
    p.add_argument("--aspect", choices=list(ASPECTS), default="9:16",
                   help="output canvas aspect (9:16 vertical, 1:1 square, 16:9 landscape)")
    p.add_argument("--cuts-per-beat", type=float, default=None,
                   help="cuts per beat (default 1.0, or the pack's)")
    p.add_argument("--no-overlay", action="store_true")
    p.add_argument("--no-flash", action="store_true")
    p.add_argument("--no-glitch", action="store_true")
    p.add_argument("--no-zoom", action="store_true", help="disable zoom/whip/black transitions")
    p.add_argument("--no-ramps", action="store_true", help="disable beat-matched speed ramps")
    p.add_argument("--film-grain", action="store_true",
                   help="add continuous animated film grain")
    p.add_argument("--vhs-jitter", action="store_true",
                   help="add rhythmic VHS tracking jitter")
    p.add_argument("--light-leak", action="store_true",
                   help="add a warm pulsing light leak")
    p.add_argument("--intensity", type=float, default=None,
                   help="effects drama 0.2-1.5 (default 0.72, or the pack's)")
    p.add_argument("--pack", choices=list(PACKS),
                   help="style pack: grade + overlay skin + effect defaults")
    p.add_argument("--style-ref", metavar="VIDEO",
                   help="extract the style from a reference reel and apply it")
    p.add_argument("--saved-pack", metavar="NAME",
                   help="apply a saved style pack by name (see --list-packs)")
    p.add_argument("--list-packs", action="store_true",
                   help="list saved style packs and exit")
    p.add_argument("--skin", choices=["digicam", "vhs", "minimal"],
                   help="overlay skin (overrides the pack's)")
    p.add_argument("--no-chapters", action="store_true",
                   help="disable auto DAY 01/02 chapters on multi-day footage")
    p.add_argument("--quality", choices=["preview", "final", "draft"],
                   default="final")
    p.add_argument("--fps", type=int, choices=list(FPS_CHOICES), default=30,
                   help="output frame rate (30 or 60)")
    p.add_argument("--hw-accel", action="store_true",
                   help="use h264_videotoolbox for the final encode "
                        "(much faster on Apple Silicon)")
    p.add_argument("--music-start", type=float, default=0.0,
                   help="use the track from this offset in seconds")
    p.add_argument("--keep-source-audio", action="store_true",
                   help="mix the clips' original audio under the music "
                        "(music ducks when footage is loud)")
    p.add_argument("--duck-threshold", type=float, default=0.05,
                   help="sidechain duck threshold 0..1 (default 0.05)")
    p.add_argument("--duck-ratio", type=float, default=8.0,
                   help="sidechain duck ratio (default 8)")
    p.add_argument("--caption", default="",
                   help="burn a caption into the overlay (empty = none)")
    p.add_argument("--auto-captions", action="store_true",
                   help="speech-to-text captions per slot (needs faster-whisper)")
    p.add_argument("--export-audio", metavar="PATH", default=None,
                   help="also write the audio mix to PATH (.m4a or .mp3)")
    p.add_argument("--verify", action="store_true",
                   help="after render, measure how well the cuts land on the beat")
    p.add_argument("--dump-plan", metavar="JSON",
                   help="analyze and write {cfg, plan} to a file (no render)")
    p.add_argument("--plan", metavar="JSON",
                   help="re-render from an exported plan (skips analysis)")
    p.add_argument("--from-job", metavar="ID",
                   help="re-render a previous job's clips (override -m/-o)")
    p.add_argument("--analyze-music", action="store_true",
                   help="just print suggested song sections and exit")
    p.add_argument("--work", default="work/cli")
    args = p.parse_args(argv)

    if not args.clips and not (args.plan or args.from_job or args.list_packs):
        p.error("clips required (unless using --plan/--from-job/--list-packs)")

    if args.analyze_music:
        if not args.music:
            sys.stderr.write("--analyze-music needs -m/--music\n")
            return 1
        from .musicscan import analyze_track
        a = analyze_track(args.music, args.duration)
        if not a["sections"]:
            sys.stderr.write("couldn't analyze this track (undecodable or too short)\n")
            return 1
        print(f"track: {a['duration']}s  tempo: {a['tempo']} BPM")
        print("suggested sections:")
        for s in a["sections"]:
            mm, ss = divmod(int(s["start"]), 60)
            print(f"  {mm}:{ss:02d}  ({s['start']:.1f}s)  {s['label']} — {s['descriptor']}")
        return 0

    if args.list_packs:
        from . import packstore
        saved = packstore.list_packs()
        if not saved:
            print("no saved packs yet (save one from the web UI, "
                  "or extract with --style-ref)")
        for sp in saved:
            g = sp.get("custom_grade") or {}
            print(f"  {sp['id'][:8]}  {sp['name']}  "
                  f"(cuts/beat {sp['cuts_per_beat']}, "
                  f"sat {g.get('saturation', '-')}, warm {g.get('warmth', '-')})")
        return 0

    def prog(stage: str, pct: float):
        sys.stderr.write(f"\r[{pct*100:5.1f}%] {stage:<10}")
        sys.stderr.flush()

    # --plan: re-render straight from an exported {cfg, plan} (no analysis).
    # The plan's pick paths point at the original clip files, so it only works
    # while those clips still exist on disk.
    if args.plan:
        try:
            data = json.loads(Path(args.plan).read_text())
        except (OSError, ValueError) as e:
            sys.stderr.write(f"couldn't read plan: {e}\n")
            return 1
        cfg = RenderConfig.from_dict(data.get("cfg") or {})
        plan = data.get("plan")
        if not isinstance(plan, dict) or not plan.get("picks"):
            sys.stderr.write("plan is missing picks\n")
            return 1
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        try:
            render_plan(cfg, args.work, plan, args.out, prog)
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"\nError: {e}\n")
            return 1
        sys.stderr.write(f"\nDone -> {args.out}\n")
        return 0

    # --from-job: duplicate a previous job's clips/config, overriding only the
    # music + output (a quick "same footage, new track" variant).
    if args.from_job:
        jf = Path("work/jobs") / args.from_job / "job.json"
        if not jf.exists():
            sys.stderr.write(f"no job {args.from_job!r} on disk (it may have "
                             "been pruned — start a new job)\n")
            return 1
        try:
            base = json.loads(jf.read_text())
        except (OSError, ValueError) as e:
            sys.stderr.write(f"couldn't read job: {e}\n")
            return 1
        cfg = RenderConfig.from_dict(base.get("cfg") or {})
        missing = [c for c in cfg.clips if not Path(c).exists()]
        if missing:
            sys.stderr.write("job's clips were cleaned up — start a new job\n")
            return 1
        if args.music:
            cfg.music = args.music
            cfg.music_start = 0.0       # a new track starts from its beginning
        # else: keep the job's original music + its start offset
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        try:
            render(cfg, args.work, args.out, prog)
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"\nError: {e}\n")
            return 1
        sys.stderr.write(f"\nDone -> {args.out}\n")
        return 0

    # --style-ref: extract a pack from a reference reel, use it as the base
    ref_pack = None
    if args.saved_pack and args.style_ref:
        sys.stderr.write("--saved-pack and --style-ref are two different style "
                         "sources; pass only one\n")
        return 1
    if args.saved_pack:
        from . import packstore
        want = args.saved_pack.lower()
        saved = packstore.list_packs()
        # match by id (or id prefix) first, then by name
        matches = [sp for sp in saved if sp["id"].startswith(want)] or \
                  [sp for sp in saved if sp["name"].lower() == want]
        if not matches:
            sys.stderr.write(f"no saved pack matching {args.saved_pack!r} "
                             f"(try --list-packs)\n")
            return 1
        if len(matches) > 1:
            sys.stderr.write(
                f"{len(matches)} packs match {args.saved_pack!r} — use an id:\n"
                + "".join(f"  {sp['id'][:8]}  {sp['name']}\n" for sp in matches))
            return 1
        ref_pack = matches[0]
        sys.stderr.write(f"using saved pack: {ref_pack['name']} "
                         f"({ref_pack['id'][:8]})\n")
    if args.style_ref:
        from .styleref import extract_style, save_pack
        ref_pack = extract_style(args.style_ref)
        saved = save_pack(ref_pack)
        sys.stderr.write(f"extracted style from {Path(args.style_ref).name}\n")
        s = ref_pack["stats"]
        g = ref_pack["custom_grade"]
        sys.stderr.write(
            f"  {s['n_cuts']} cuts @ {s['cut_rate']}/s, tempo {s['tempo']} "
            f"-> {ref_pack['cuts_per_beat']} cuts/beat\n"
            f"  flash={ref_pack['flash']} zoom={ref_pack['zoom_transitions']} "
            f"intensity={ref_pack['effect_intensity']}\n"
            f"  grade: sat={g['saturation']} warm={g['warmth']} "
            f"vig={g['vignette']} grain={g['grain']}\n"
            f"  saved -> {saved}\n")

    # pack -> explicit flag -> built-in default (None sentinels track
    # explicitness, so "--preset cinematic" alongside a pack is honored)
    pk = PACKS.get(args.pack) if args.pack else None
    preset = args.preset or (pk["preset"] if pk else "cinematic")
    intensity = args.intensity if args.intensity is not None \
        else (pk["effect_intensity"] if pk else 0.72)
    cfg = RenderConfig(
        clips=args.clips, music=args.music, preset=preset, fill=args.fill,
        location=args.location, day=args.day, target_duration=args.duration,
        cuts_per_beat=(args.cuts_per_beat if args.cuts_per_beat is not None else 1.0),
        overlay=not args.no_overlay,
        flash=(pk["flash"] if pk else True) and not args.no_flash,
        glitch=(pk["glitch"] if pk else True) and not args.no_glitch,
        zoom_transitions=(pk["zoom_transitions"] if pk else True) and not args.no_zoom,
        speed_ramps=(pk["speed_ramps"] if pk else True) and not args.no_ramps,
        film_grain=args.film_grain,
        vhs_jitter=args.vhs_jitter,
        light_leak=args.light_leak,
        effect_intensity=intensity, music_start=args.music_start,
        auto_chapters=not args.no_chapters, quality=args.quality,
        hw_accel=args.hw_accel,
        keep_source_audio=args.keep_source_audio, caption=args.caption,
        duck_threshold=args.duck_threshold, duck_ratio=args.duck_ratio,
        auto_captions=args.auto_captions,
        export_audio=args.export_audio,
        overlay_skin=args.skin or (pk["overlay_skin"] if pk else "digicam"),
        aspect=args.aspect, fps=args.fps,
    )
    if ref_pack:
        # extracted style overrides grade + timing + effects (skin stays).
        # freshly-extracted packs carry "stats"; saved packs carry "ref_stats"
        cfg.custom_grade = ref_pack["custom_grade"]
        cfg.ref_stats = ref_pack.get("stats") or ref_pack.get("ref_stats")
        if args.saved_pack and not args.skin and ref_pack.get("overlay_skin"):
            cfg.overlay_skin = ref_pack["overlay_skin"]
        if args.cuts_per_beat is None:
            cfg.cuts_per_beat = ref_pack["cuts_per_beat"]
        cfg.flash = ref_pack["flash"] and not args.no_flash
        cfg.glitch = ref_pack["glitch"] and not args.no_glitch
        cfg.zoom_transitions = ref_pack["zoom_transitions"] and not args.no_zoom
        cfg.speed_ramps = ref_pack["speed_ramps"] and not args.no_ramps
        if args.intensity is None:
            cfg.effect_intensity = ref_pack["effect_intensity"]
    problems = cfg.validate()
    if problems:
        sys.stderr.write("invalid config:\n" +
                         "\n".join(f"  {p}" for p in problems) + "\n")
        return 1
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)

    if args.dump_plan:
        plan, _pool = prepare(cfg, args.work, prog)
        Path(args.dump_plan).parent.mkdir(parents=True, exist_ok=True)
        Path(args.dump_plan).write_text(
            json.dumps({"cfg": cfg.to_dict(), "plan": plan}, indent=2))
        sys.stderr.write(f"\nplan -> {args.dump_plan}\n")
        return 0

    try:
        render(cfg, args.work, args.out, prog)
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"\nError: {e}\n")
        return 1
    sys.stderr.write(f"\nDone -> {args.out}\n")
    if args.verify:
        if not args.music:
            sys.stderr.write("--verify needs -m/--music to check beat sync\n")
        else:
            from .beats import detect_beats
            from .verify import format_report, verify_rendered
            sys.stderr.write(format_report(
                verify_rendered(args.out, detect_beats(args.music))) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
