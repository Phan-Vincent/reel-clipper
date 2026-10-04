"""Persistent user style packs.

An extracted style (styleref) is throwaway unless it can be kept. This stores
named packs as JSON on disk so they appear in the UI next to the built-in
Digicam/VHS/Minimal/Film packs and can be re-applied to any future render —
including the reference stats, so closed-loop grade tuning still runs.

Pack payloads arrive from client JSON, so every field is whitelisted and
coerced here; ids are generated (never client-supplied) and regex-checked on
lookup so a pack id can't escape the store directory.
"""
from __future__ import annotations

import json
import math
import re
import time
import uuid
from pathlib import Path

STORE = Path("work/user_packs")
ID_RE = re.compile(r"^[0-9a-f]{32}$")
MAX_PACKS = 40
NAME_MAX = 40

# knob -> (lo, hi) accepted from an untrusted pack payload
_GRADE_BOUNDS = {
    "contrast": (0.5, 2.0), "saturation": (0.0, 3.0), "gamma": (0.5, 2.0),
    "brightness": (-0.5, 0.5), "warmth": (0.0, 3.0), "vignette": (0.0, 1.5),
    "grain": (0.0, 50.0), "sharpen": (0.0, 2.0),
}
_STAT_KEYS = ("sat", "warm_split", "lum", "contrast_std", "vig_ratio",
              "grain", "cut_rate", "tempo", "n_cuts", "flash_rate",
              "black_dips", "whip_rate", "duration", "whip_frames")
_SKINS = ("digicam", "vhs", "minimal")

_DEFAULT_GRADE = {
    "contrast": 1.06, "saturation": 1.18, "gamma": 1.02, "brightness": 0.01,
    "warmth": 1.0, "vignette": 0.63, "grain": 7.0, "sharpen": 0.55,
}


def _num(v, lo: float, hi: float, default: float) -> float:
    # NaN/Infinity must fall back to the default, NOT be clamped: every
    # comparison with NaN is False, so max/min would silently yield a bound,
    # and json.dumps writes bare NaN/Infinity tokens that aren't valid JSON.
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return default
    return max(lo, min(hi, float(v)))


def clean_name(name: str) -> str:
    """Printable, trimmed, length-capped. Empty -> a generic fallback."""
    name = re.sub(r"[\x00-\x1f\x7f]", "", str(name or "")).strip()
    return name[:NAME_MAX] or "Untitled style"


def _clean_pack(raw: dict, name: str) -> dict:
    grade = raw.get("custom_grade")
    grade_out = None
    if isinstance(grade, dict):
        grade_out = {k: _num(grade.get(k), lo, hi, _DEFAULT_GRADE[k])
                     for k, (lo, hi) in _GRADE_BOUNDS.items()}
    stats = raw.get("ref_stats")
    stats_out = None
    if isinstance(stats, dict):
        # keep only genuinely numeric stats — never fabricate a 0.0, which
        # would look like a real measurement to the closed-loop tuner
        stats_out = {k: float(v) for k in _STAT_KEYS
                     if isinstance((v := stats.get(k)), (int, float))
                     and not isinstance(v, bool) and math.isfinite(v)}
    skin = raw.get("overlay_skin")
    return {
        "name": clean_name(name),
        "kind": "saved",
        "preset": raw.get("preset") if isinstance(raw.get("preset"), str) else "cinematic",
        "overlay_skin": skin if skin in _SKINS else "digicam",
        "cuts_per_beat": _num(raw.get("cuts_per_beat"), 0.25, 4.0, 1.0),
        "flash": bool(raw.get("flash", True)),
        "glitch": bool(raw.get("glitch", True)),
        "zoom_transitions": bool(raw.get("zoom_transitions", True)),
        "speed_ramps": bool(raw.get("speed_ramps", True)),
        "effect_intensity": _num(raw.get("effect_intensity"), 0.2, 1.5, 0.72),
        "custom_grade": grade_out,
        "ref_stats": stats_out,
    }


def save(raw: dict, name: str) -> dict:
    STORE.mkdir(parents=True, exist_ok=True)
    packs = list_packs()
    if len(packs) >= MAX_PACKS:
        raise ValueError(f"pack limit reached ({MAX_PACKS}) — delete one first")
    pack = _clean_pack(raw or {}, name)
    pack["id"] = uuid.uuid4().hex
    pack["created"] = time.time()
    (STORE / f"{pack['id']}.json").write_text(json.dumps(pack, indent=2))
    return pack


def list_packs() -> list[dict]:
    if not STORE.exists():
        return []
    out = []
    for p in STORE.glob("*.json"):
        if not ID_RE.match(p.stem):
            continue
        try:
            d = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(d, dict) and d.get("id") == p.stem:
            out.append(d)
    return sorted(out, key=lambda d: d.get("created", 0))


def get(pack_id: str) -> dict | None:
    if not ID_RE.match(pack_id or ""):
        return None
    p = STORE / f"{pack_id}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def delete(pack_id: str) -> bool:
    if not ID_RE.match(pack_id or ""):
        return False
    try:
        (STORE / f"{pack_id}.json").unlink()   # atomic; no exists() TOCTOU
    except OSError:
        return False
    return True
