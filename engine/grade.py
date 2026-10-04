"""Cinematic color grade. Builds the validated teal-orange filter chain from a
StylePreset, or routes through a user-supplied .cube LUT via lut3d."""
from __future__ import annotations

from .config import StylePreset


def _curves() -> str:
    # Gentle S-contrast, lifted (not crushed) shadows, warm bias in R, cool in B.
    return (
        "curves="
        "r='0/0.03 0.25/0.24 0.5/0.52 0.75/0.80 1/0.98':"
        "g='0/0.02 0.5/0.5 1/0.97':"
        "b='0/0.06 0.25/0.23 0.5/0.48 0.75/0.74 1/0.92'"
    )


def build_grade(preset: StylePreset) -> str:
    """Return a comma-joined filter string (no pad labels)."""
    parts: list[str] = []

    if preset.cube:
        parts.append(f"lut3d=file='{preset.cube}'")
    else:
        parts.append(_curves())
        w = preset.warmth
        # teal-orange split scaled by warmth
        parts.append(
            f"colorbalance="
            f"rs={-0.06 * w:.3f}:bs={0.07 * w:.3f}:"
            f"rm={0.03 * w:.3f}:bm={-0.02 * w:.3f}:"
            f"rh={0.09 * w:.3f}:bh={-0.08 * w:.3f}"
        )

    parts.append(
        f"eq=contrast={preset.contrast:.3f}:saturation={preset.saturation:.3f}:"
        f"gamma={preset.gamma:.3f}:brightness={preset.brightness:.3f}"
    )
    if preset.vignette > 0:
        parts.append(f"vignette={preset.vignette:.3f}")
    if preset.grain > 0:
        parts.append(f"noise=alls={int(preset.grain)}:allf=t+u")
    if preset.sharpen > 0:
        parts.append(f"unsharp=5:5:{preset.sharpen:.2f}:5:5:0.0")
    return ",".join(parts)
