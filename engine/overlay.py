"""Camcorder / digicam UI overlay, in three skins.

This ffmpeg build has no drawtext, so the UI is drawn with Pillow into RGBA
frames and encoded as a qtrle alpha .mov, then composited via `overlay`.

Skins:
  digicam  — blinking REC, ticking timecode, viewfinder brackets,
             "++ DAY 01 ++", location tag  (the reference-reel look)
  vhs      — scanlines, chunky REC + SP badge, per-chapter date stamp,
             ticking tape counter, small location
  minimal  — tiny bottom-center location + optional DAY, nothing else

Auto-chapters: `day_ranges` = [(start_frame, end_frame, day_index)] switches
the baked day/date elements as the montage crosses a chapter boundary.
"""
from __future__ import annotations

import dataclasses
import math
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from . import ffmpeg
from .config import OUT_W, OUT_H, FPS, canvas

_FONT_CANDIDATES = [
    "/System/Library/Fonts/Menlo.ttc",
    "/System/Library/Fonts/Monaco.ttf",
    "/System/Library/Fonts/Supplemental/Courier New.ttf",
    "/Library/Fonts/Andale Mono.ttf",
]
_VHS_FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Courier New Bold.ttf",
    "/System/Library/Fonts/Courier.ttc",
    *_FONT_CANDIDATES,
]

WHITE = (255, 255, 255)
SHADOW = (0, 0, 0, 150)
UI_ALPHA = 215
REC_RED = (235, 45, 45)


def _font(size: int, candidates=None) -> ImageFont.FreeTypeFont:
    for path in (candidates or _FONT_CANDIDATES):
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()


def _text(draw: ImageDraw.ImageDraw, xy, s, font, anchor="la",
          color=WHITE, alpha=UI_ALPHA, shadow=2):
    """Text with a dark drop-shadow for legibility over bright footage."""
    x, y = xy
    draw.text((x + shadow, y + shadow), s, font=font, fill=SHADOW, anchor=anchor)
    draw.text((x, y), s, font=font, fill=(*color, alpha), anchor=anchor)


CAPTION_MAX_CHARS = 42
CAPTION_MAX_LINES = 3


def wrap_caption(text: str, max_chars: int = CAPTION_MAX_CHARS,
                 max_lines: int = CAPTION_MAX_LINES) -> list[str]:
    """Word-wrap a caption into at most `max_lines` lines of <= `max_chars`
    characters, hard-breaking overlong words (pure, for tests)."""
    text = (text or "").strip()
    if not text:
        return []
    lines: list[str] = []
    for w in text.split():
        while len(w) > max_chars:            # hard-break a single long word
            lines.append(w[:max_chars])
            w = w[max_chars:]
            if len(lines) >= max_lines:
                return lines[:max_lines]
        if not lines:
            lines.append(w)
        elif len(lines[-1]) + 1 + len(w) <= max_chars:
            lines[-1] += " " + w
        elif len(lines) < max_lines:
            lines.append(w)
        else:
            break
    return lines[:max_lines]


def _draw_caption(d, cfg, font, y_base, w=OUT_W):
    """Caption lines stacked bottom-up, centered horizontally, above y_base."""
    lines = wrap_caption(cfg.caption)
    if not lines:
        return
    lh = int(font.size * 1.15)
    y = y_base - lh * (len(lines) - 1)
    for ln in lines:
        _text(d, (w // 2, y), ln, font, anchor="ma")
        y += lh


def _brackets(draw: ImageDraw.ImageDraw, m: int, ln: int, lw: int,
              w: int = OUT_W, h: int = OUT_H):
    col = (255, 255, 255, 140)
    W, H = w, h
    corners = [
        ((m, m), (m + ln, m), (m, m + ln)),
        ((W - m, m), (W - m - ln, m), (W - m, m + ln)),
        ((m, H - m), (m + ln, H - m), (m, H - m - ln)),
        ((W - m, H - m), (W - m - ln, H - m), (W - m, H - m - ln)),
    ]
    for c, hx, vy in corners:
        draw.line([c, hx], fill=col, width=lw)
        draw.line([c, vy], fill=col, width=lw)


# ---- per-skin base layers (static per chapter/day) ---------------------------

def _base_digicam(cfg, day: int, w: int = OUT_W, h: int = OUT_H) -> Image.Image:
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    m = 54
    big = _font(44)
    small = _font(34)
    _brackets(d, m - 6, 46, 3, w, h)
    y_bottom = h - m - 46
    _text(d, (m, y_bottom), "<<   >>", small)
    if day and day > 0:
        _text(d, (m, y_bottom - 52), f"++ DAY {day:02d} ++", small)
    if cfg.location:
        _text(d, (w - m, y_bottom), f"{cfg.location.upper()} ++",
              big, anchor="ra")
    _draw_caption(d, cfg, small, y_bottom - 58, w)
    if cfg.show_rec:
        _text(d, (m + 42, m), "REC", big, anchor="la")
    return img


def _base_vhs(cfg, date_label: str | None, w: int = OUT_W,
              h: int = OUT_H) -> Image.Image:
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # scanlines: subtle dark rows over the whole frame
    for y in range(0, h, 4):
        d.line([(0, y), (w, y)], fill=(0, 0, 0, 24), width=1)
    m = 58
    big = _font(56, _VHS_FONT_CANDIDATES)
    mid = _font(44, _VHS_FONT_CANDIDATES)
    if cfg.show_rec:
        _text(d, (m + 52, m), "REC", big, anchor="la", alpha=235, shadow=3)
    _text(d, (w - m, m), "SP", mid, anchor="ra", alpha=225, shadow=3)
    y_bottom = h - m - 56
    if date_label:
        _text(d, (m, y_bottom - 64), date_label, mid, alpha=235, shadow=3)
    if cfg.location:
        _text(d, (w - m, y_bottom), cfg.location.upper(), mid,
              anchor="ra", alpha=225, shadow=3)
    _draw_caption(d, cfg, _font(36, _VHS_FONT_CANDIDATES), y_bottom - 72, w)
    return img


def _base_minimal(cfg, day: int, w: int = OUT_W, h: int = OUT_H) -> Image.Image:
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if cfg.location:
        _text(d, (w // 2, h - 96), cfg.location.upper(),
              _font(30), anchor="ma", alpha=175, shadow=1)
    if day and day > 0:
        _text(d, (w // 2, h - 58), f"DAY {day:02d}",
              _font(22), anchor="ma", alpha=140, shadow=1)
    _draw_caption(d, cfg, _font(30), h - 106, w)
    return img


def _base_layer(cfg, day: int, date_label: str | None,
                w: int = OUT_W, h: int = OUT_H) -> Image.Image:
    skin = getattr(cfg, "overlay_skin", "digicam")
    if skin == "vhs":
        return _base_vhs(cfg, date_label, w, h)
    if skin == "minimal":
        return _base_minimal(cfg, day, w, h)
    return _base_digicam(cfg, day, w, h)


def _build_ranges(segments) -> list[list]:
    """Compact per-segment styles into frame ranges.

    `segments` = [(n_frames, day, caption), ...] aligned with the montage.
    Consecutive segments with the SAME (day, caption) merge into one range so
    the base layers stay few. Returns [[f0, f1, (day, caption)], ...] (pure,
    for tests).
    """
    ranges: list[list] = []
    f0 = 0
    for nf, day, cap in segments:
        key = (day, cap)
        if ranges and ranges[-1][2] == key:
            ranges[-1][1] = f0 + nf
        else:
            ranges.append([f0, f0 + nf, key])
        f0 += nf
    return ranges


def render_overlay_mov(cfg, duration: float, out_path: str,
                       day_ranges: list | None = None,
                       chapter_dates: dict | None = None,
                       segments: list | None = None) -> str:
    """Render the animated UI to a qtrle alpha .mov of length `duration`.

    `segments` = [(n_frames, day, caption)] per pick: captions can change per
    slot. When None, the legacy day_ranges path is used with cfg.caption for
    every frame.
    """
    w, h = canvas(getattr(cfg, "aspect", "9:16"))
    fps = getattr(cfg, "fps", FPS)
    total = max(1, int(math.ceil(duration * fps)))
    skin = getattr(cfg, "overlay_skin", "digicam")
    if segments:
        ranges = _build_ranges(segments)
    else:
        ranges = [(int(a), int(b), (int(dy), cfg.caption))
                  for a, b, dy in (day_ranges or [])]
        if not ranges:
            ranges = [(0, total, (cfg.day, cfg.caption))]
    dates = chapter_dates or {}

    def date_for(day: int) -> str | None:
        return dates.get(day) or dates.get(str(day))

    bases = {}
    for _, _, key in ranges:
        if key not in bases:
            day, cap = key
            bases[key] = _base_layer(dataclasses.replace(cfg, caption=cap),
                                     day, date_for(day), w, h)

    tc_font = _font(36)
    vhs_font = _font(48, _VHS_FONT_CANDIDATES)
    m = 54 if skin != "vhs" else 58

    proc = subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-y", "-loglevel", "error", "-nostdin",
         "-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{w}x{h}",
         "-r", str(fps), "-i", "pipe:0",
         "-c:v", "qtrle", "-pix_fmt", "argb", out_path],
        stdin=subprocess.PIPE,
    )
    assert proc.stdin is not None
    with ffmpeg.active_proc(proc):
        ri = 0
        try:
            for f in range(total):
                while ri + 1 < len(ranges) and f >= ranges[ri][1]:
                    ri += 1
                img = bases[ranges[ri][2]].copy()
                d = ImageDraw.Draw(img)
                blink_on = (f // (fps // 2)) % 2 == 0
                if skin == "minimal":
                    pass                              # fully static
                elif skin == "vhs":
                    if cfg.show_rec and blink_on:
                        r = 14
                        d.ellipse([m, m + 10, m + 2 * r, m + 10 + 2 * r],
                                  fill=(*REC_RED, 245))
                    secs = f // fps
                    counter = f"{secs // 3600}:{(secs % 3600) // 60:02d}:{secs % 60:02d}"
                    _text(d, (m, h - m - 56), counter, vhs_font,
                          alpha=235, shadow=3)
                else:                                 # digicam
                    if cfg.show_rec and blink_on:
                        r = 11
                        d.ellipse([m, m + 8, m + 2 * r, m + 8 + 2 * r],
                                  fill=(*REC_RED, 235))
                    if cfg.show_timecode:
                        secs = f / fps
                        tc = (f"{int(secs // 3600):02d}:{int((secs % 3600) // 60):02d}:"
                              f"{int(secs % 60):02d}:{f % fps:02d}")
                        _text(d, (w - m, m), tc, tc_font, anchor="ra")
                proc.stdin.write(img.tobytes())
        finally:
            proc.stdin.close()
            proc.wait()
    if proc.returncode != 0:
        raise RuntimeError("overlay encode failed")
    return out_path
