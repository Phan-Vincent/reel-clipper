"""Filtergraph string builders: fill + grade."""
from engine.config import StylePreset
from engine.fill import fill_graph
from engine.grade import build_grade


def test_fill_blur_has_bg_fg_overlay():
    f = fill_graph("blur")
    assert "[0:v]split=2[bg][fg]" in f
    assert "gblur" in f
    assert "[bgb][fgs]overlay=" in f
    assert f.endswith("[fl]")


def test_fill_crop_is_scale_cover_plus_crop():
    f = fill_graph("crop")
    assert "force_original_aspect_ratio=increase" in f
    assert "crop=1080:1920" in f
    assert "gblur" not in f


def test_fill_label_params_respected():
    f = fill_graph("blur", label_in="rt", label_out="out")
    assert f.startswith("[rt]split=2") and f.endswith("[out]")


def test_fill_graph_custom_canvas_dims():
    f = fill_graph("crop", w=1080, h=1080)
    assert "crop=1080:1080" in f and "1080:1920" not in f
    f = fill_graph("blur", w=1920, h=1080)
    assert "1920:1080" in f and "1920:-2" in f


def test_grade_chain_contains_core_filters():
    g = build_grade(StylePreset(key="t", name="t"))
    assert "curves=" in g
    assert "colorbalance=" in g
    assert "eq=contrast=1.060:saturation=1.180" in g


def test_grade_clean_preset_is_nearly_empty():
    g = build_grade(StylePreset(key="c", name="c", contrast=1.0, saturation=1.0,
                                gamma=1.0, brightness=0.0, warmth=1.0,
                                vignette=0.0, grain=0.0, sharpen=0.0))
    assert "vignette" not in g and "noise=" not in g and "unsharp" not in g


def test_grade_cube_routes_via_lut3d():
    g = build_grade(StylePreset(key="c", name="c", cube="/tmp/look.cube"))
    assert g.startswith("lut3d=file='/tmp/look.cube'")
    assert "curves" not in g and "colorbalance" not in g


def test_grade_knobs_flow_through():
    g = build_grade(StylePreset(key="t", name="t", saturation=1.4, contrast=0.9,
                                vignette=0.5, grain=9.0, sharpen=0.7))
    assert "saturation=1.400" in g and "contrast=0.900" in g
    assert "vignette=0.500" in g
    assert "noise=alls=9" in g
    assert "unsharp=5:5:0.70" in g


def test_x264_quality_flags():
    from engine.assemble import _segment_common, _x264_common
    assert "veryfast" in _x264_common("preview") and "-crf" in _x264_common("preview")
    assert "-crf" in _x264_common("final")
    assert "veryfast" in _x264_common("draft")      # draft is fast
    # real renders use master-quality segments (crf 16); draft is fast
    seg = _segment_common("final")
    assert "-crf" in seg and "16" in seg and "-preset" in seg and "medium" in seg
    draft = _segment_common("draft")
    assert "26" in draft and "veryfast" in draft
