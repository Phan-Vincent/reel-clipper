"""RenderConfig sanitization: from_dict whitelist + custom_grade bounds."""
import math

from engine.config import PRESETS, RenderConfig


def test_from_dict_ignores_unknown_fields():
    cfg = RenderConfig.from_dict({"preset": "moody", "bogus": 123, "clips": None})
    assert cfg.preset == "moody"
    assert not hasattr(cfg, "bogus")


def test_to_dict_roundtrip():
    cfg = RenderConfig(preset="vibrant", location="OSAKA", day=2,
                       target_duration=24.0, effect_intensity=0.9,
                       caption="night market run", keep_source_audio=True)
    back = RenderConfig.from_dict(cfg.to_dict())
    assert back.preset == "vibrant"
    assert back.location == "OSAKA"
    assert back.day == 2
    assert back.target_duration == 24.0
    assert back.effect_intensity == 0.9
    assert back.caption == "night market run"
    assert back.keep_source_audio is True


def test_preset_obj_known_preset():
    assert RenderConfig(preset="warm_film").preset_obj().warmth > 1.0


def test_preset_obj_unknown_falls_back_to_cinematic():
    assert RenderConfig(preset="nope").preset_obj().key == "cinematic"


def test_custom_grade_clamped_to_bounds():
    cfg = RenderConfig(custom_grade={"contrast": 99.0, "saturation": -5.0,
                                     "warmth": 0.5, "grain": 100.0})
    p = cfg.preset_obj()
    assert p.contrast == 2.0          # clamped high
    assert p.saturation == 0.0        # clamped low
    assert p.warmth == 0.5
    assert p.grain == 50.0


def test_custom_grade_rejects_bools_and_nan():
    cfg = RenderConfig(custom_grade={"contrast": True, "saturation": math.nan,
                                     "grain": "big", "warmth": 1.2})
    p = cfg.preset_obj()
    assert p.contrast == 1.06         # bool rejected -> default
    assert p.saturation == 1.18       # NaN rejected -> default
    assert p.grain == 7.0             # string rejected -> default
    assert p.warmth == 1.2            # valid number kept


def test_custom_grade_never_accepts_cube():
    # cube is a string f-formatted into the ffmpeg chain -> must not pass through
    cfg = RenderConfig(custom_grade={"cube": "/etc/passwd", "grain": 3.0})
    assert cfg.preset_obj().cube is None


def test_all_builtin_presets_build_sane_keys():
    for key, p in PRESETS.items():
        assert p.key == key and p.name


def test_validate_empty_for_defaults():
    assert RenderConfig().validate() == []


def test_validate_catches_out_of_range():
    p = RenderConfig(target_duration=1.0, cuts_per_beat=9.0, day=500,
                     effect_intensity=9.0, duck_ratio=99.0).validate()
    assert any("duration" in x for x in p)
    assert any("cuts_per_beat" in x for x in p)
    assert any("day" in x for x in p)
    assert any("effect_intensity" in x for x in p)
    assert any("duck_ratio" in x for x in p)


def test_validate_catches_unknown_enum_values():
    p = RenderConfig(preset="nope", fill="warp", overlay_skin="hologram",
                     quality="ultra").validate()
    assert any("preset" in x for x in p)
    assert any("fill" in x for x in p)
    assert any("overlay_skin" in x for x in p)
    assert any("quality" in x for x in p)


def test_canvas_resolves_aspects_and_defaults():
    from engine.config import canvas
    assert canvas("9:16") == (1080, 1920)
    assert canvas("1:1") == (1080, 1080)
    assert canvas("16:9") == (1920, 1080)
    assert canvas("4:3") == (1080, 1920)   # unknown -> 9:16 default


def test_validate_catches_bad_aspect_and_fps():
    p = RenderConfig(aspect="4:3", fps=120).validate()
    assert any("aspect" in x for x in p)
    assert any("fps" in x for x in p)
    assert RenderConfig(aspect="1:1", fps=60).validate() == []


def test_validate_never_raises_on_wrong_types():
    # A client can send any JSON type for a numeric field; validate() must
    # report it as a problem, not blow up with a TypeError.
    cases = [("target_duration", "18", "duration"), ("day", "5", "day"),
             ("effect_intensity", "x", "effect_intensity"),
             ("cuts_per_beat", [], "cuts_per_beat"),
             ("duck_ratio", {}, "duck_ratio"),
             ("min_segment", [1], "min_segment"),
             ("max_segment", None, "max_segment"),
             ("dedup", "high", "dedup"),
             ("min_gap", {"a": 1}, "min_gap"),
             ("max_per_clip", "ten", "max_per_clip"),
             ("music_start", "start", "music_start"),
             ("location", ["TOKYO"], "location"),
             ("caption", {"text": "hi"}, "caption")]
    for field, bad, keyword in cases:
        problems = RenderConfig(**{field: bad}).validate()   # must not raise
        assert any(keyword in p for p in problems), field


def test_validate_rejects_non_finite_numbers():
    # Python's json.loads accepts NaN/Infinity; they must be rejected, not
    # silently pass (NaN comparisons are always False).
    problems = RenderConfig(target_duration=math.nan,
                            max_segment=math.inf,
                            min_segment=-math.inf,
                            duck_threshold=math.nan).validate()
    assert any("duration" in p for p in problems)
    assert any("max_segment" in p for p in problems)
    assert any("min_segment" in p for p in problems)
    assert any("duck_threshold" in p for p in problems)


def test_validate_rejects_bad_min_max_segment():
    # max_segment <= 0 (or < min_segment) used to make cut_points loop forever.
    for max_seg in (0, -1, 0.1):
        p = RenderConfig(max_segment=max_seg).validate()
        assert any("max_segment" in x for x in p)
    # min_segment >= max_segment is also invalid
    p = RenderConfig(min_segment=2.0, max_segment=1.5).validate()
    assert any("max_segment" in x for x in p)


def test_validate_rejects_non_bool_toggles():
    # A string "false"/0/1 is truthy -> would silently enable a disabled
    # feature; require a real bool instead of coercing by truthiness.
    for field in ("flash", "glitch", "auto_captions", "keep_source_audio",
                  "overlay", "hw_accel"):
        for bad in ("false", 0, 1, None):
            p = RenderConfig(**{field: bad}).validate()
            assert any(field in x and "boolean" in x for x in p), (field, bad)
    assert RenderConfig(flash=True, glitch=False).validate() == []
