"""Effects pass over the assembled montage: beat-gated white flashes and RGB
chroma-split glitches. Applied in a single ffmpeg pass so effect timing can
reference absolute montage time.

Hard cuts (the dominant transition) are handled by the concat demuxer in
assemble.py; this module only adds the accent effects on strong onsets.
"""
from __future__ import annotations

import math

from .config import OUT_W, OUT_H, FPS

# Per-segment intro transitions (applied to the first few frames of a segment,
# operating on the already-filled+graded 1080x1920 stream). These compose with
# the montage-wide flash/glitch overlay pass below.
INTRO_KINDS = ("none", "zoom", "blackzoom", "whipzoom")

# ---- whip-pan cuts -----------------------------------------------------------
# A directional motion-wipe across a cut: the outgoing segment pans off with
# heavy horizontal blur (whip-out), the incoming segment pans in from the SAME
# direction and settles (whip-in), so the cut reads as one continuous camera
# whip. Spatial chains applied after fill, BEFORE grade (vignette must not pan).
WHIP_OUT_KINDS = ("whipout_l", "whipout_r")
WHIP_IN_KINDS = ("whipin_l", "whipin_r")
_WHIP_FRAMES = 6   # outro whip length; intro settles over 5


def whip_pan_filter(kind: str, nframes: int, intensity: float = 1.0,
                    w: int = OUT_W, h: int = OUT_H) -> str:
    """Pre-grade spatial chain for a whip-pan half, or "" if not a whip kind.

    `intensity` scales both the pan travel (via the scale-up headroom) and the
    blur strength, so the drama slider genuinely tames the whips.
    """
    if kind not in WHIP_OUT_KINDS and kind not in WHIP_IN_KINDS:
        return ""
    inten = max(0.2, min(1.6, intensity))
    scale = 1.0 + 0.32 * inten           # travel headroom (1.32x at default)
    sigma = max(8, round(22 * inten))    # main whip blur
    sigma2 = max(4, round(8 * inten))    # settle blur (whip-in tail)
    w2 = round(w * scale / 2) * 2
    h2 = round(h * scale / 2) * 2
    xc, xmax, yc = (w2 - w) // 2, w2 - w, (h2 - h) // 2
    left = kind.endswith("_l")   # image content moves left (crop window right)
    if kind in WHIP_OUT_KINDS:
        n0 = max(0, nframes - _WHIP_FRAMES)
        p = f"pow(min(1,max(0,(n-{n0})/{_WHIP_FRAMES - 1})),2.2)"
        x = f"{xc}+{xmax - xc}*{p}" if left else f"{xc}-{xc}*{p}"
        blur = f"gblur=sigma={sigma}:sigmaV=2:enable='gte(n,{n0})'"
    else:
        p = "(1-pow(1-min(1,n/5),2))"
        x = f"{xc}*{p}" if left else f"{xmax}-{xc}*{p}"
        blur = (f"gblur=sigma={sigma}:sigmaV=2:enable='lt(n,3)',"
                f"gblur=sigma={sigma2}:sigmaV=2:enable='between(n,3,4)'")
    return (f"scale={w2}:{h2},crop={w}:{h}:x='{x}':y={yc},{blur}")


# ---- beat-matched speed ramps ------------------------------------------------
# A ramp re-times a segment's source footage with piecewise-constant speeds.
# Segment boundaries ARE beats, so speed snaps land exactly on the music.
# Slow phases (<0.7x) need a high-fps source (real slow-mo frames, no judder);
# fast phases are safe on any source.
RAMP_KINDS = ("none", "rampin", "rampout", "ramphold")

# (output-duration, speed) phases; duration None = absorbs the remaining length.
_RAMP_PHASES: dict[str, list[tuple[float | None, float]]] = {
    "rampin":  [(0.18, 2.6), (0.15, 1.5), (None, 1.0)],    # burst out of the cut
    "rampout": [(None, 0.82), (0.22, 2.6)],                # whip INTO the next cut
    "ramphold": [(0.15, 1.5), (None, 0.42), (0.20, 2.5)],  # hero slow-mo, snap out
}
_RAMP_MIN_LEN = {"rampin": 0.45, "rampout": 0.45, "ramphold": 0.75}
_MIN_FILLER = 0.08
# min source fps for ramps containing a slow phase: the 0.42x hold needs
# >= 30/0.42 ~= 72fps for judder-free real-frame slow-mo
RAMP_MIN_FPS = {"rampin": 0.0, "rampout": 0.0, "ramphold": 72.0}


def _resolve_ramp(kind: str, length: float):
    spec = _RAMP_PHASES.get(kind)
    if not spec or length < _RAMP_MIN_LEN.get(kind, 1e9):
        return None
    fixed = sum(d for d, _ in spec if d is not None)
    filler = length - fixed
    if filler < _MIN_FILLER:
        return None
    return [(filler if d is None else d, s) for d, s in spec]


def ramp_source_needed(kind: str, length: float) -> float:
    """Seconds of source footage a ramped segment consumes."""
    ph = _resolve_ramp(kind, length)
    return sum(d * s for d, s in ph) if ph else length


def atempo_chain(speed: float, eps: float = 1e-4) -> list[float]:
    """Factor an arbitrary speed into `atempo` factors (each 0.5..2.0) whose
    product equals `speed` — the audio-side reverse of a `setpts` ramp.

    ffmpeg's atempo accepts one factor in [0.5, 2.0]; chaining several covers
    the ramp speeds (2.6 = 2.0*1.3, 0.42 = 0.5*0.84). Returns [] for
    non-positive / non-finite input (caller substitutes silence).
    """
    if speed <= 0 or not math.isfinite(speed):
        return []
    s = speed
    chain: list[float] = []
    while s > 2.0 + eps:
        chain.append(2.0)
        s /= 2.0
    while s < 0.5 - eps:
        chain.append(0.5)
        s /= 0.5
    chain.append(round(s, 4))
    return chain


def ramp_setpts(kind: str, length: float) -> str:
    """Return a setpts chain mapping source time -> ramped output time,
    or "" if the segment is too short for this ramp."""
    ph = _resolve_ramp(kind, length)
    if not ph:
        return ""
    # piecewise-linear boundaries: (u_end_source, t_phase_start, u_phase_start, speed)
    bounds = []
    u = t = 0.0
    for d, s in ph:
        bounds.append((u + d * s, t, u, s))
        u += d * s
        t += d
    u_end, t0, u0, s = bounds[-1]
    expr = f"{t0:.4f}+(T-{u0:.4f})/{s}"
    for u_end, t0, u0, s in reversed(bounds[:-1]):
        expr = f"if(lt(T,{u_end:.4f}),{t0:.4f}+(T-{u0:.4f})/{s},{expr})"
    return f"setpts=PTS-STARTPTS,setpts='({expr})/TB'"


def segment_intro_filter(kind: str, intensity: float = 1.0,
                         w: int = OUT_W, h: int = OUT_H,
                         fps: int = FPS) -> str:
    """Return a comma-joinable filter chain for a segment-start transition."""
    if kind in (None, "none", ""):
        return ""
    inten = max(0.2, min(1.6, intensity))
    # fps= is REQUIRED: zoompan otherwise re-stamps the stream at its default
    # 25fps, silently discarding upstream (ramp) timing and slowing the segment.
    zp = (f"zoompan=z='{{z}}':d=1:x='iw/2-(iw/zoom/2)':"
          f"y='ih/2-(ih/zoom/2)':s={w}x{h}:fps={fps}")
    if kind == "zoom":                      # quick punch-in that settles
        a = 0.28 * inten
        z = f"if(lt(on,7),{1 + a:.3f}-{a:.3f}*on/7,1)"
        return zp.format(z=z)
    if kind == "blackzoom":                 # camcorder power-on: dip-to-black reveal
        a = 0.5 * inten
        z = f"if(lt(on,9),{1 + a:.3f}-{a:.3f}*on/9,1)"
        return zp.format(z=z) + ",fade=t=in:st=0:d=0.16"
    if kind == "whipzoom":                  # zoom + brief motion-blur swish
        a = 0.22 * inten
        z = f"if(lt(on,6),{1 + a:.3f}-{a:.3f}*on/6,1)"
        return zp.format(z=z) + f",gblur=sigma={int(26 * inten)}:sigmaV=1:enable='lt(n,3)'"
    return ""


def build_effects_filtergraph(
    duration: float,
    flash_times: list[float],
    glitch_times: list[float],
    intensity: float = 1.0,
    label_in: str = "0:v",
    label_out: str = "v",
    w: int = OUT_W,
    h: int = OUT_H,
    fps: int = FPS,
) -> str | None:
    """Return a -filter_complex string, or None if there is nothing to do."""
    flash_times = sorted(set(round(t, 3) for t in flash_times))
    glitch_times = sorted(set(round(t, 3) for t in glitch_times))
    if not flash_times and not glitch_times:
        return None

    cur = label_in
    parts: list[str] = []

    # --- RGB glitch (rgbashift) enabled in short windows around each time ---
    if glitch_times:
        gw = 0.12
        shift = int(round(22 * intensity))
        enable = "+".join(f"between(t,{t:.3f},{t + gw:.3f})" for t in glitch_times)
        parts.append(
            f"[{cur}]rgbashift=rh=-{shift}:rv=-6:bh={shift}:bv=6:edge=smear:"
            f"enable='gt({enable},0)'[gl]"
        )
        cur = "gl"

    # --- white flash: tiny flat rgba source (alpha ramp), scaled up, overlaid ---
    if flash_times:
        fw = 0.09
        peak = min(1.0, 0.85 * intensity)
        pulses = "+".join(
            f"max(0\\,1-abs(T-{t:.3f})/{fw})" for t in flash_times
        )
        alpha_expr = f"min(255\\,{int(peak * 255)}*({pulses}))"
        parts.append(
            f"color=c=white:s=64x114:r={fps}:d={duration:.3f},format=rgba[wsrc]"
        )
        parts.append(
            f"[wsrc]geq=r=255:g=255:b=255:a='{alpha_expr}'[wa]"
        )
        parts.append(
            f"[wa]scale={w}:{h}:flags=neighbor,format=rgba[flash]"
        )
        parts.append(
            f"[{cur}][flash]overlay=0:0:format=auto[fx]"
        )
        cur = "fx"

    parts.append(f"[{cur}]format=yuv420p[{label_out}]")
    return ";".join(parts)


def build_texture_effects(
    duration: float,
    intensity: float = 1.0,
    label_in: str = "0:v",
    label_out: str = "v",
    w: int = OUT_W,
    h: int = OUT_H,
    fps: int = FPS,
    film_grain: bool = False,
    vhs_jitter: bool = False,
    light_leak: bool = False,
) -> str | None:
    """Continuous / ambient "texture" effects (montage-wide, not beat-gated):

    - film grain   : animated temporal noise (`noise=alls=…:allf=t+u`)
    - vhs jitter   : rhythmic mild horizontal tracking error (`rgbashift`)
    - light leak   : warm pulsing wash overlaid with a slow sine alpha

    Returns a -filter_complex fragment (chained after the beat-gated effects),
    or None if nothing is enabled.
    """
    if not (film_grain or vhs_jitter or light_leak):
        return None
    inten = max(0.2, min(1.6, intensity))
    cur = label_in
    parts: list[str] = []

    if film_grain:
        n = max(2, round(8 * inten))
        parts.append(f"[{cur}]noise=alls={n}:allf=t+u[gr]")
        cur = "gr"

    if vhs_jitter:
        shift = max(2, round(5 * inten))
        period = 0.9
        enable = f"between(mod(t,{period}),0,0.08)"
        parts.append(
            f"[{cur}]rgbashift=rh={shift}:rv=0:bh={shift}:bv=0:edge=wrap:"
            f"enable='{enable}'[vj]"
        )
        cur = "vj"

    if light_leak:
        peak = min(1.0, 0.30 * inten)
        # slow ambient pulse (period ~2.2s) — distinct from the sharp white
        # flash, which is beat-gated and near-instantaneous
        alpha_expr = f"min(255\\,{int(peak * 255)}*(0.5+0.5*sin(2*PI*T/2.2)))"
        parts.append(
            f"color=c=#ff8c1a:s=64x114:r={fps}:d={duration:.3f},format=rgba[lsrc]"
        )
        parts.append(f"[lsrc]geq=r=255:g=140:b=26:a='{alpha_expr}'[la]")
        parts.append(f"[la]scale={w}:{h}:flags=neighbor,format=rgba[leak]")
        parts.append(f"[{cur}][leak]overlay=0:0:format=auto[ll]")
        cur = "ll"

    parts.append(f"[{cur}]format=yuv420p[{label_out}]")
    return ";".join(parts)
