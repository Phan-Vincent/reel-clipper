"""Style-from-reference: analyze a finished reel, produce an applicable pack.

Given a reference video someone loves, extract the machine-readable parts of
its editing style and map them onto this engine's own knobs:

  timing   — hard-cut rate + audio tempo  -> cuts_per_beat, min/max segment
  effects  — white-flash spikes, dip-to-black frames, whip-pan anisotropy
             bursts -> flash/glitch/zoom toggles + effect_intensity
  grade    — saturation, teal-orange highlight/shadow split, contrast,
             brightness, vignette falloff, grain energy -> StylePreset knobs

The grade mapping is anchored empirically: constants below were calibrated by
rendering known presets through the real pipeline and fitting the measured
stats back to the preset knobs (see NEUTRAL_* anchors).

This measures the *look*, not the artistry — speed-ramp/beat-nuance detection
is out of scope for the prototype and noted as such in the result.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import numpy as np

from . import ffmpeg
from .beats import detect_beats
from .probe import video_info

# Neutral anchors: stats of ungraded phone footage rendered through the
# pipeline with the "clean" preset (measured, not guessed).
NEUTRAL_SAT = 0.366       # measured: clean-preset render of phone footage
# Corpus-relative anchors (warmth/grain are content-confounded against the
# render pipeline — validated empirically — so they're anchored to the median
# of the reference corpus instead: "warmer/grainier than a typical reel")
CORPUS_SPLIT = 0.0295     # median R-B hi/lo split across reference corpus
CORPUS_GRAIN = 0.00683    # median flat-region residual across corpus
NEUTRAL_STD = 0.222
NEUTRAL_LUM = 0.46
NEUTRAL_VIG = 0.970       # corner/center luma ratio with no vignette
NEUTRAL_GRAIN = 0.0034
SAT_SLOPE = 2.6           # d(sat knob)/d(mean HSV saturation)  [fit: clean<->cine<->vhs]
WARM_SLOPE = 9.0          # d(warmth)/d(hi-lo R-B split)        [fit: w 0.7..1.35]
VIG_SLOPE = 1.3           # d(vignette)/d(ratio drop)           [fit: 4 presets]
GRAIN_SLOPE = 2000        # d(grain)/d(residual energy), corpus-relative


def _decode(path: str, width: int = 270, fps: float | None = None):
    info = video_info(path)
    h = max(2, int(round(info.height * width / max(1, info.width))))
    h -= h % 2
    vf = f"scale={width}:{h}"
    if fps:
        vf = f"fps={fps}," + vf
    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-i", path,
           "-vf", vf, "-f", "rawvideo", "-pix_fmt", "bgr24", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, bufsize=width * h * 3 * 4)
    frames = []
    assert proc.stdout is not None
    with ffmpeg.active_proc(proc):
        while True:
            buf = proc.stdout.read(width * h * 3)
            if len(buf) < width * h * 3:
                break
            frames.append(np.frombuffer(buf, np.uint8).reshape(h, width, 3))
        proc.stdout.close()
        proc.wait()
    return frames, info


def _cut_times(path: str, thr: float = 0.30) -> list[float]:
    proc = subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-nostats", "-nostdin", "-i", path,
         "-filter:v", f"select='gt(scene,{thr})',showinfo", "-f", "null", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    with ffmpeg.active_proc(proc):
        _out, err = proc.communicate()
    return [float(m) for m in re.findall(r"pts_time:([0-9.]+)", err)]


def measure(path: str, timing: bool = True) -> dict:
    """Raw, model-free measurements of a video. timing=False skips the cut/
    tempo passes (fast path for closed-loop grade probes)."""
    import cv2

    frames, info = _decode(path)
    n = len(frames)
    if n < 10:
        raise ValueError("reference too short to analyze")
    fps = n / max(0.1, info.duration)

    lum = np.array([f.mean() / 255.0 for f in frames])
    # flash spikes: bright outlier vs local neighborhood
    flashes = 0
    for i in range(2, n - 2):
        loc = (lum[i - 2] + lum[i + 2]) / 2
        if lum[i] > 0.80 and lum[i] > loc + 0.25:
            flashes += 1
    black_dips = int(np.sum((lum[int(fps):n - int(fps)] < 0.06)))

    # whip anisotropy bursts (validated detector from the whip-pan work)
    ratios = []
    for f in frames:
        g = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY)
        gx = np.abs(cv2.Sobel(g, cv2.CV_32F, 1, 0)).mean()
        gy = np.abs(cv2.Sobel(g, cv2.CV_32F, 0, 1)).mean()
        ratios.append(gy / (gx + 1e-6))
    r = np.array(ratios)
    whips = int(np.sum(r > np.median(r) * 1.8))

    # grade stats on "normal" frames only (skip flash/black outliers)
    normal = [f for f, l in zip(frames, lum) if 0.12 < l < 0.75]
    sample = normal[:: max(1, len(normal) // 60)][:60] or frames[:30]
    sats, splits, stds, lums, vigs, grains = [], [], [], [], [], []
    for f in sample:
        hsv = cv2.cvtColor(f, cv2.COLOR_BGR2HSV)
        sats.append(float(hsv[:, :, 1].mean()) / 255.0)
        g = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY).astype(np.float32) / 255.0
        stds.append(float(g.std()))
        lums.append(float(g.mean()))
        b, _, rch = f[:, :, 0].astype(np.float32), f[:, :, 1], f[:, :, 2].astype(np.float32)
        rb = (rch - b) / 255.0
        # warmth cast on NEAR-NEUTRAL pixels only — gray surfaces reflect the
        # grade's tint; saturated content (sky, water, grass) would bias it
        neutral_px = hsv[:, :, 1] < 110
        hi = (g > 0.55) & neutral_px
        lo = (g < 0.40) & neutral_px
        if hi.sum() > 50 and lo.sum() > 50:
            splits.append(float(rb[hi].mean() - rb[lo].mean()))
        h, w = g.shape
        cy, cx = h // 2, w // 2
        center = g[cy - h // 6:cy + h // 6, cx - w // 6:cx + w // 6].mean()
        corners = np.mean([g[:h // 6, :w // 6].mean(), g[:h // 6, -w // 6:].mean(),
                           g[-h // 6:, :w // 6].mean(), g[-h // 6:, -w // 6:].mean()])
        vigs.append(float(corners / (center + 1e-6)))
        # grain: residual high-frequency energy in low-gradient regions
        blur = cv2.GaussianBlur(g, (5, 5), 0)
        resid = np.abs(g - blur)
        flat = cv2.GaussianBlur(np.abs(cv2.Laplacian(blur, cv2.CV_32F)), (9, 9), 0) < 0.02
        if flat.sum() > 200:
            grains.append(float(resid[flat].mean()))

    cuts = _cut_times(path) if timing else []
    tempo = 0.0
    if timing:
        try:
            tempo = detect_beats(path).tempo
        except Exception:
            tempo = 0.0

    grain_sig = float(np.mean(grains)) if grains else 0.0

    return {
        "duration": round(info.duration, 2),
        "cut_rate": round(len(cuts) / max(0.1, info.duration), 3),
        "n_cuts": len(cuts),
        "tempo": round(float(tempo), 1),
        "flash_rate": round(flashes / max(0.1, info.duration), 3),
        "black_dips": black_dips,
        "whip_frames": whips,
        "whip_rate": round(whips / max(1, n), 4),
        "sat": round(float(np.mean(sats)), 4),
        "warm_split": round(float(np.mean(splits)) if splits else 0.0, 4),
        "contrast_std": round(float(np.mean(stds)), 4),
        "lum": round(float(np.mean(lums)), 4),
        "vig_ratio": round(float(np.mean(vigs)), 4),
        "grain": round(grain_sig, 5),
    }


def derive_pack(stats: dict, name: str) -> dict:
    """Map raw measurements onto engine knobs (clamped to sane preset ranges)."""
    clamp = lambda v, lo, hi: max(lo, min(hi, v))

    # timing: cuts per beat, snapped to the grid values the engine supports
    beat_rate = (stats["tempo"] / 60.0) if stats["tempo"] > 0 else 2.0
    cpb_raw = stats["cut_rate"] / max(0.5, beat_rate)
    cuts_per_beat = min([0.5, 1.0, 2.0], key=lambda v: abs(v - cpb_raw))

    grade = {
        "saturation": round(clamp(1.0 + (stats["sat"] - NEUTRAL_SAT) * SAT_SLOPE,
                                  0.75, 1.45), 3),
        "warmth": round(clamp(1.0 + (stats["warm_split"] - CORPUS_SPLIT) * WARM_SLOPE,
                              0.6, 1.5), 3),
        "contrast": round(clamp(1.03 + (stats["contrast_std"] - NEUTRAL_STD) * 1.5,
                                0.98, 1.10), 3),
        "brightness": round(clamp((stats["lum"] - NEUTRAL_LUM) * 0.15,
                                  -0.03, 0.03), 3),
        "gamma": 1.02,
        "vignette": round(clamp((NEUTRAL_VIG - stats["vig_ratio"]) * VIG_SLOPE,
                                0.0, 0.9), 3),
        "grain": round(clamp(7.0 + (stats["grain"] - CORPUS_GRAIN) * GRAIN_SLOPE,
                             0.0, 16.0), 1),
        "sharpen": 0.45,
    }

    has_flash = stats["flash_rate"] > 0.08
    has_whip = stats["whip_rate"] > 0.01
    has_black = stats["black_dips"] > 0
    intensity = clamp(0.5 + stats["flash_rate"] * 1.5 + stats["whip_rate"] * 8
                      + (0.15 if has_black else 0.0), 0.4, 1.3)

    return {
        "name": f"Ref: {name}",
        "custom_grade": grade,
        "cuts_per_beat": cuts_per_beat,
        "flash": has_flash,
        "glitch": has_flash,          # co-occur in this aesthetic; no direct detector yet
        "zoom_transitions": has_whip or has_black,
        "speed_ramps": stats["cut_rate"] > 1.2,   # proxy; no direct ramp detector yet
        "effect_intensity": round(intensity, 2),
        "stats": stats,
        "notes": ("prototype: speed-ramp/glitch detection are proxies; warmth+grain "
                  "are corpus-relative (vs typical IG reel), not absolute"),
    }


def extract_style(path: str, name: str | None = None) -> dict:
    stats = measure(path)
    pack = derive_pack(stats, (name or Path(path).stem)[:24])
    return pack


def save_pack(pack: dict, out_dir: str = "work/stylerefs") -> str:
    d = Path(out_dir)
    d.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", pack["name"])[:40]
    p = d / f"{safe}.json"
    p.write_text(json.dumps(pack, indent=2))
    return str(p)


# ---- closed-loop grade tuning ------------------------------------------------
# Open-loop application can't hit the reference's look: the knobs' effect
# depends on the target footage's own content (already-warm footage + "warm"
# knob = over-warm). The fix is differential: render short PROBE segments of
# the actual target footage with the current knobs, measure them with the
# same instrument used on the reference, and gradient-step the knobs until
# the probe stats land on the reference stats. Content confounds cancel
# because both sides of the comparison are measured, not modeled.

# damped gains per looped stat: d(knob)/d(stat error), from pipeline calibration
_TUNE = [
    # (knob,        stat,           gain, lo,    hi)
    ("saturation", "sat",           2.0,  0.70,  1.50),
    ("warmth",     "warm_split",    2.5,  0.60,  1.50),
    ("brightness", "lum",           0.45, -0.08, 0.08),
    ("contrast",   "contrast_std",  1.0,  0.96,  1.12),
    ("vignette",   "vig_ratio",    -1.0,  0.00,  0.90),   # knob raises -> ratio drops
]


def _probe_picks(picks: list, durations: dict, n_probe: int | None = None,
                 seconds: float = 2.5):
    """Probe the ACTUAL montage shots, spread across the timeline.

    Fixed windows from the longest clips measure content the reel never shows;
    the grade then fits the wrong material. These sample the real picks (same
    shots, same framing, same exposure mix as the output) with effects
    stripped — we're fitting the grade, not the motion.

    The probe count scales with how varied the footage is: 3 samples describe
    a 4-clip reel well but not a 19-clip one, where the grade would be fitted
    to a slice the montage barely shows. Probes are drawn from distinct source
    clips first, so the sample spans the content the reel actually contains.
    """
    from .selection import Pick
    # need room for a >=0.8s window plus tail margin, else tpad clones frames
    # into the probe and biases the very stats we're fitting
    usable = [pk for pk in picks if durations.get(pk.src, 0.0) >= 1.2]
    if not usable:
        return []
    n_src = len({pk.src for pk in usable})
    if n_probe is None:
        n_probe = min(6, max(3, round(n_src / 3)))
    n = min(n_probe, len(usable))

    # walk the timeline, preferring a source we haven't probed yet
    idxs, used_src = [], set()
    for i in range(n):
        anchor = round(i * (len(usable) - 1) / max(1, n - 1))
        cand = sorted(range(len(usable)), key=lambda j: abs(j - anchor))
        pick_i = next((j for j in cand
                       if usable[j].src not in used_src and j not in idxs),
                      next((j for j in cand if j not in idxs), anchor))
        idxs.append(pick_i)
        used_src.add(usable[pick_i].src)

    out, seen = [], set()
    for i in sorted(idxs):
        pk = usable[i]
        dur = durations.get(pk.src, 0.0)
        length = max(0.8, min(seconds, dur - 0.2))
        start = pk.start
        if start + length + 0.2 > dur:          # keep the window inside the clip
            start = max(0.0, dur - length - 0.2)
        key = (pk.src, round(start, 1))
        if key in seen:
            continue
        seen.add(key)
        out.append(Pick(src=pk.src, start=round(start, 3), length=round(length, 3)))
    return out


def fit_grade(ref_stats: dict, base_grade: dict, picks: list,
              durations: dict, cfg, work_dir: str, iters: int = 3,
              log=None) -> tuple[dict, list[dict]]:
    """Tune base_grade so probe renders of the target's OWN picked shots
    measure like the reference. Returns (tuned_grade, per-iteration trace)."""
    import copy
    from dataclasses import replace as dc_replace

    from .assemble import render_segment

    # both dicts can arrive from untrusted client JSON — degrade to open-loop
    # (return the base unchanged) rather than KeyError/TypeError mid-job
    knobs = [k for k, *_ in _TUNE]
    stats_needed = [s for _, s, *_ in _TUNE]
    def _num(d, keys):
        return all(isinstance(d.get(k), (int, float))
                   and not isinstance(d.get(k), bool) for k in keys)
    if not (_num(base_grade, knobs) and _num(ref_stats, stats_needed)):
        return base_grade, []

    probes = _probe_picks(picks, durations)
    if not probes:
        return base_grade, []
    work = Path(work_dir) / "styletune"
    work.mkdir(parents=True, exist_ok=True)

    grade = copy.deepcopy(base_grade)
    trace = []
    # gradient steps can overshoot (a knob hits a clamp, or the gain is too
    # hot for this footage), so keep the best-measured grade rather than
    # whatever the final iteration happens to land on
    best_dist, best_grade = float("inf"), copy.deepcopy(base_grade)
    for it in range(iters):
        probe_cfg = dc_replace(cfg, custom_grade=grade, quality="preview")
        stats = []
        for i, pk in enumerate(probes):
            out = work / f"probe_{it}_{i}.mp4"
            render_segment(pk, probe_cfg, str(out))
            stats.append(measure(str(out), timing=False))
        probe = {k: float(np.mean([s[k] for s in stats]))
                 for k in ("sat", "warm_split", "lum", "contrast_std", "vig_ratio")}
        err = {stat: ref_stats[stat] - probe[stat] for _, stat, *_ in _TUNE}
        dist = sum(abs(v) for v in err.values())
        trace.append({"iter": it, "probe": {k: round(v, 4) for k, v in probe.items()},
                      "dist": round(dist, 4),
                      "grade": {k: grade[k] for k in
                                ("saturation", "warmth", "brightness",
                                 "contrast", "vignette")}})
        if dist < best_dist:
            best_dist, best_grade = dist, copy.deepcopy(grade)
        if log:
            log(f"styletune iter {it}: dist={dist:.3f}")
        if dist < 0.05 or it == iters - 1:
            break
        for knob, stat, gain, lo, hi in _TUNE:
            grade[knob] = round(min(hi, max(lo, grade[knob] + err[stat] * gain)), 3)
    return best_grade, trace
