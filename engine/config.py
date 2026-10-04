"""Configuration: render settings, style presets, and the preset registry.

Everything downstream (grade, fill, overlay, selection) reads from these
dataclasses so the CLI and the web UI drive the exact same engine.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict
from typing import Optional

# ---- Output canvas -----------------------------------------------------------
# Defaults (also the 9:16 vertical baseline). A render's actual canvas and
# frame rate are chosen from RenderConfig.aspect / RenderConfig.fps and
# threaded through the pipeline explicitly (see canvas()).
OUT_W = 1080
OUT_H = 1920
FPS = 30

# aspect key -> (width, height). All are 1080p-class so the grade/overlay
# font metrics hold across shapes.
ASPECTS: dict[str, tuple[int, int]] = {
    "9:16": (1080, 1920),
    "1:1": (1080, 1080),
    "16:9": (1920, 1080),
}
FPS_CHOICES = (30, 60)


def canvas(aspect: str) -> tuple[int, int]:
    """Resolve an aspect key to (width, height); unknown -> 9:16 default."""
    return ASPECTS.get(aspect, ASPECTS["9:16"])


@dataclass
class StylePreset:
    """A named color-grade look.

    A preset is either a filter *chain* (built in grade.py) or a 3D LUT file
    dropped into presets/*.cube. `cube` wins when set.
    """
    key: str
    name: str
    # teal-orange chain knobs (see grade.build_grade)
    contrast: float = 1.06
    saturation: float = 1.18
    gamma: float = 1.02
    brightness: float = 0.01
    warmth: float = 1.0          # scales the orange-highlight / teal-shadow split
    vignette: float = 0.63       # 0 disables; ~PI/5 == 0.628 is the reference look
    grain: float = 7.0           # noise alls; 0 disables
    sharpen: float = 0.55        # unsharp amount; 0 disables
    cube: Optional[str] = None   # absolute path to a .cube LUT (overrides chain color)


PRESETS: dict[str, StylePreset] = {
    "cinematic": StylePreset(
        key="cinematic", name="Cinematic (teal-orange)",
    ),
    "warm_film": StylePreset(
        key="warm_film", name="Warm Film", warmth=1.35, saturation=1.12,
        contrast=1.04, grain=10.0, vignette=0.7,
    ),
    "moody": StylePreset(
        key="moody", name="Moody / Muted", saturation=0.9, contrast=1.12,
        brightness=-0.02, warmth=0.7, vignette=0.85, grain=8.0,
    ),
    "vibrant": StylePreset(
        key="vibrant", name="Vibrant Travel", saturation=1.35, contrast=1.08,
        warmth=1.1, vignette=0.5, grain=4.0,
    ),
    "clean": StylePreset(
        key="clean", name="Clean (no grade)", contrast=1.0, saturation=1.0,
        gamma=1.0, brightness=0.0, warmth=1.0, vignette=0.0, grain=0.0,
        sharpen=0.0,
    ),
    "vhs": StylePreset(
        key="vhs", name="VHS (analog)", contrast=1.02, saturation=0.82,
        gamma=1.04, brightness=0.015, warmth=0.9, vignette=0.55, grain=15.0,
        sharpen=0.0,
    ),
}


# ---- Style packs -------------------------------------------------------------
# A pack bundles a grade + overlay skin + effect defaults into one choice.
# The web UI applies these to its controls (still individually overridable);
# the CLI applies them via --pack.
PACKS: dict[str, dict] = {
    "digicam": {"name": "Digicam '25", "preset": "cinematic",
                "overlay_skin": "digicam", "flash": True, "glitch": True,
                "zoom_transitions": True, "speed_ramps": True,
                "effect_intensity": 0.72},
    "vhs": {"name": "VHS '98", "preset": "vhs", "overlay_skin": "vhs",
            "flash": True, "glitch": True, "zoom_transitions": True,
            "speed_ramps": True, "effect_intensity": 0.9},
    "minimal": {"name": "Clean Minimal", "preset": "clean",
                "overlay_skin": "minimal", "flash": False, "glitch": False,
                "zoom_transitions": False, "speed_ramps": False,
                "effect_intensity": 0.4},
    "film": {"name": "Warm Film", "preset": "warm_film",
             "overlay_skin": "minimal", "flash": True, "glitch": False,
             "zoom_transitions": False, "speed_ramps": True,
             "effect_intensity": 0.6},
}


@dataclass
class RenderConfig:
    """Everything one render needs. Serializable to/from the web UI JSON."""
    # inputs
    clips: list[str] = field(default_factory=list)   # absolute paths to raw footage
    music: Optional[str] = None                       # absolute path to audio track
    music_start: float = 0.0     # use the track from this offset (s); 0 = start

    # look
    preset: str = "cinematic"
    custom_grade: Optional[dict] = None  # extracted style (styleref) overrides preset
    ref_stats: Optional[dict] = None     # reference reel stats -> closed-loop tuning
    tune_grade: bool = True              # differential fit of custom_grade to target
    fill: str = "blur"           # "blur" (blurred bg) or "crop" (full-bleed)

    # overlay (camcorder UI)
    overlay: bool = True
    overlay_skin: str = "digicam"  # "digicam" | "vhs" | "minimal"
    location: str = "TOKYO"      # bottom-right tag; "" to hide
    day: int = 1                 # "++ DAY 01 ++"; 0 to hide
    show_rec: bool = True
    show_timecode: bool = True
    auto_chapters: bool = True   # multi-day footage -> DAY 01/02/... advances
    caption: str = ""            # burned-in caption text ("" = none)

    # timing / cutting
    target_duration: float = 18.0   # seconds; montage is trimmed near this
    cuts_per_beat: float = 1.0       # 0.5 (every other beat), 1, 2 (subdivide)
    min_segment: float = 0.30        # never cut shorter than this (seconds)
    max_segment: float = 1.60        # or longer than this

    # transitions / effects
    flash: bool = True           # white-flash on strong onsets
    glitch: bool = True          # rgb chroma-split on strong onsets
    zoom_transitions: bool = True  # zoom-punch / dip-to-black / whip on cuts
    speed_ramps: bool = True     # beat-matched speed ramps (slow-mo needs hi-fps src)
    film_grain: bool = False     # continuous animated film grain (opt-in)
    vhs_jitter: bool = False     # rhythmic horizontal tracking error (opt-in)
    light_leak: bool = False     # warm pulsing light leak (opt-in)
    effect_intensity: float = 0.72  # drama dial 0.2-1.5: whip blur/travel, zoom
                                   # punch, flash/glitch strength (<0.45 drops
                                   # whip-pans entirely; ramps unaffected)

    # selection
    max_per_clip: int = 0        # 0 = auto (scale with footage); else hard cap
    min_gap: float = 5.0         # min seconds between two picks from same source
    dedup: float = 0.86          # reject near-duplicate shots above this corr (0..1)
    use_source_audio: bool = True  # score moments by source-audio energy (cheers)
    use_faces: bool = True       # small bonus for detected faces/people
    energy_match: bool = True    # put the most exciting shots on the strongest beats
    seed: int = 0

    # audio
    keep_source_audio: bool = False  # mix original clip audio under the music
                                   # (music sidechain-ducks when footage is loud)
    duck_threshold: float = 0.05    # sidechain threshold 0..1 (lower = duck sooner)
    duck_ratio: float = 8.0         # sidechain ratio (how hard the music dips)
    auto_captions: bool = False     # speech-to-text captions per slot (optional dep)
    export_audio: Optional[str] = None  # write the audio mix to this path (None = skip)

    # encode
    quality: str = "final"       # "preview" (veryfast) or "final"
    hw_accel: bool = False       # use h264_videotoolbox for the final encode
    aspect: str = "9:16"         # "9:16" | "1:1" | "16:9" output canvas
    fps: int = 30                # output frame rate (30 or 60)

    def preset_obj(self) -> StylePreset:
        if self.custom_grade:
            # custom_grade arrives from untrusted JSON (web config). Only the
            # NUMERIC grade knobs are accepted — never `cube` (a string that
            # is f-formatted into the ffmpeg filter chain) or key/name.
            # NaN/Infinity must fall back to the default, NOT be clamped:
            # every comparison with NaN is False, so max/min would silently
            # yield a bound (mirrors packstore._num).
            bounds = {"contrast": (0.5, 2.0), "saturation": (0.0, 3.0),
                      "gamma": (0.5, 2.0), "brightness": (-0.5, 0.5),
                      "warmth": (0.0, 3.0), "vignette": (0.0, 1.5),
                      "grain": (0.0, 50.0), "sharpen": (0.0, 2.0)}
            kw = {}
            for k, (lo, hi) in bounds.items():
                v = self.custom_grade.get(k)
                if isinstance(v, (int, float)) and not isinstance(v, bool) \
                        and math.isfinite(v):
                    kw[k] = max(lo, min(hi, float(v)))
            return StylePreset(key="custom", name="Extracted style", **kw)
        return PRESETS.get(self.preset, PRESETS["cinematic"])

    def to_dict(self) -> dict:
        return asdict(self)

    def validate(self) -> list[str]:
        """Human-readable problems with this config (empty = valid). Used by
        the web endpoint to 400 early and the CLI to fail with a clear reason
        instead of a cryptic mid-render error.

        Config values arrive from untrusted client JSON, so every comparison
        here must be type-safe: a string/object/NaN where a number is expected
        has to become a *problem*, never a TypeError or a silent pass that
        reaches the render pipeline."""
        problems: list[str] = []

        def num(v) -> bool:
            # NaN/Infinity are not valid JSON but Python's json.loads accepts
            # them, and every comparison with NaN is False — so reject them
            # explicitly instead of letting a NaN sail through as "in range".
            return (isinstance(v, (int, float)) and not isinstance(v, bool)
                    and math.isfinite(v))

        def intval(v) -> bool:
            return num(v) and float(v).is_integer()

        def num_range(field: str, v, lo: float, hi: float) -> None:
            if not num(v):
                problems.append(f"{field} {v!r} is not a finite number")
            elif not (lo <= v <= hi):
                problems.append(f"{field} {v} out of range ({lo}-{hi})")

        num_range("duration", self.target_duration, 3.0, 60.0)
        num_range("cuts_per_beat", self.cuts_per_beat, 0.25, 4.0)
        if not intval(self.day) or not (0 <= int(self.day) <= 99):
            problems.append(f"day {self.day!r} out of range (0-99)")
        num_range("effect_intensity", self.effect_intensity, 0.2, 1.5)
        # min/max segment are not user-facing, but a <=0 or non-finite
        # max_segment makes BeatGrid.cut_points loop forever — reject it here.
        num_range("min_segment", self.min_segment, 0.05, 5.0)
        num_range("max_segment", self.max_segment, 0.1, 10.0)
        if (num(self.min_segment) and num(self.max_segment)
                and self.max_segment <= self.min_segment):
            problems.append(
                f"max_segment {self.max_segment} must exceed min_segment {self.min_segment}")
        if not num(self.music_start) or self.music_start < 0:
            problems.append(f"music_start {self.music_start!r} must be a non-negative number")
        num_range("dedup", self.dedup, 0.0, 1.0)
        if not num(self.min_gap) or self.min_gap < 0:
            problems.append(f"min_gap {self.min_gap!r} must be a non-negative number")
        if not intval(self.max_per_clip) or int(self.max_per_clip) < 0:
            problems.append(f"max_per_clip {self.max_per_clip!r} must be a non-negative integer")
        if not isinstance(self.location, str):
            problems.append(f"location {self.location!r} must be a string")
        if not isinstance(self.caption, str):
            problems.append(f"caption {self.caption!r} must be a string")
        # Boolean toggles: a client string like "false" is truthy, which would
        # silently turn a disabled feature on. Require a real bool.
        for f in ("overlay", "show_rec", "show_timecode", "auto_chapters",
                  "flash", "glitch", "zoom_transitions", "speed_ramps",
                  "film_grain", "vhs_jitter", "light_leak", "use_source_audio",
                  "use_faces", "energy_match", "keep_source_audio",
                  "auto_captions", "hw_accel", "tune_grade"):
            if not isinstance(getattr(self, f), bool):
                problems.append(f"{f} {getattr(self, f)!r} must be a boolean")
        if self.preset not in PRESETS:
            problems.append(f"unknown preset {self.preset!r}")
        if self.fill not in ("blur", "crop"):
            problems.append(f"unknown fill {self.fill!r}")
        if self.overlay_skin not in ("digicam", "vhs", "minimal"):
            problems.append(f"unknown overlay_skin {self.overlay_skin!r}")
        if self.quality not in ("preview", "final", "draft"):
            problems.append(f"unknown quality {self.quality!r}")
        if self.aspect not in ASPECTS:
            problems.append(f"unknown aspect {self.aspect!r}")
        if self.fps not in FPS_CHOICES:
            problems.append(f"fps {self.fps} out of range (allowed {FPS_CHOICES})")
        num_range("duck_threshold", self.duck_threshold, 0.0, 1.0)
        num_range("duck_ratio", self.duck_ratio, 1.0, 30.0)
        return problems

    @classmethod
    def from_dict(cls, d: dict) -> "RenderConfig":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})
