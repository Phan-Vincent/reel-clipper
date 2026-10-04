"""Framing into the output canvas (default 9:16 1080x1920; width/height are
threaded from the config so 1:1 and 16:9 work too). Two validated variants:
blurred-bg fill and full-bleed center-crop. Returns a filtergraph fragment
operating on [label_in] and producing [label_out] at w x h (framing only — no
fps or pixel-format normalization, so a grade can be chained onto [label_out])."""
from __future__ import annotations

from .config import OUT_W, OUT_H


def fill_graph(fill: str, label_in: str = "0:v", label_out: str = "fl",
               w: int = OUT_W, h: int = OUT_H) -> str:
    """Build the fit-to-canvas filter fragment.

    blur -> footage full-width & centered over a blurred cover copy.
    crop -> scale-to-cover then center-crop (loses the sides).
    """
    if fill == "crop":
        return (
            f"[{label_in}]scale={w}:{h}:force_original_aspect_ratio=increase,"
            f"crop={w}:{h},setsar=1[{label_out}]"
        )
    # default: blurred background fill
    return (
        f"[{label_in}]split=2[bg][fg];"
        f"[bg]scale={w}:{h}:force_original_aspect_ratio=increase,"
        f"crop={w}:{h},gblur=sigma=30,eq=brightness=-0.06[bgb];"
        f"[fg]scale={w}:-2[fgs];"
        f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2:format=auto,setsar=1[{label_out}]"
    )
