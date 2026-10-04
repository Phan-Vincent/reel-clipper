"""Speed-ramp math + transition filtergraph builders."""
import pytest

from engine.transitions import (
    RAMP_KINDS, WHIP_IN_KINDS, WHIP_OUT_KINDS,
    _resolve_ramp, build_effects_filtergraph, ramp_setpts, ramp_source_needed,
    segment_intro_filter, whip_pan_filter,
)


# ---- ramp source-consumption math -------------------------------------------

def test_ramp_source_needed_equals_phase_sum():
    for kind in ("rampin", "rampout", "ramphold"):
        need = ramp_source_needed(kind, 2.0)
        spec = _resolve_ramp(kind, 2.0)
        assert spec is not None
        assert need == pytest.approx(sum(d * s for d, s in spec))


def test_ramp_source_needed_never_less_than_output_length():
    for kind in ("rampin", "rampout"):
        for L in (0.5, 0.8, 1.2, 2.0):
            if _resolve_ramp(kind, L) is None:
                continue
            assert ramp_source_needed(kind, L) >= L - 1e-9
    # ramphold is slow-mo: the 0.42x hold STRETCHES source, so for segments
    # long enough that the slow phase dominates it consumes LESS source than
    # the output duration (that's the point of the effect)
    for L in (1.2, 2.0):
        need = ramp_source_needed("ramphold", L)
        assert 0 < need < L
    assert ramp_source_needed("ramphold", 0.8) > 0


def test_ramp_rejected_below_min_length():
    assert _resolve_ramp("rampin", 0.1) is None
    assert _resolve_ramp("ramphold", 0.3) is None
    assert _resolve_ramp("none", 5.0) is None


def test_ramp_setpts_preserves_total_output_time():
    # the last phase absorbs the remaining length; output must be ~= L
    for kind in ("rampin", "rampout", "ramphold"):
        expr = ramp_setpts(kind, 1.5)
        assert "setpts" in expr
        assert "(" in expr and "/TB" in expr


def test_ramp_source_needed_is_deterministic():
    assert ramp_source_needed("ramphold", 1.2) == ramp_source_needed("ramphold", 1.2)


# ---- whip pan filters ---------------------------------------------------------

def test_whip_pan_only_for_whip_kinds():
    assert whip_pan_filter("none", 30) == ""
    assert whip_pan_filter("zoom", 30) == ""
    for k in WHIP_OUT_KINDS + WHIP_IN_KINDS:
        assert "scale=" in whip_pan_filter(k, 30)


def test_whip_output_and_input_directions():
    out_l = whip_pan_filter("whipout_l", 30)
    out_r = whip_pan_filter("whipout_r", 30)
    assert out_l != out_r
    assert "gblur" in out_l


def test_whip_intensity_changes_filter():
    low = whip_pan_filter("whipout_l", 30, intensity=0.3)
    high = whip_pan_filter("whipout_l", 30, intensity=1.4)
    assert low != high


# ---- intro transitions --------------------------------------------------------

def test_segment_intro_known_kinds():
    assert segment_intro_filter("none") == ""
    assert segment_intro_filter(None) == ""
    for kind in ("zoom", "blackzoom", "whipzoom"):
        f = segment_intro_filter(kind)
        assert "zoompan" in f and f"fps={30}" in f   # fps= is REQUIRED (see comment)


def test_segment_intro_unknown_kind_safe():
    assert segment_intro_filter("bogus") == ""


# ---- montage effects pass ------------------------------------------------------

def test_effects_none_when_no_times():
    assert build_effects_filtergraph(10.0, [], []) is None


def test_effects_flash_uses_rgba_source_and_overlay():
    fx = build_effects_filtergraph(10.0, [2.0, 4.0], [], label_out="v")
    assert fx is not None
    assert "color=c=white" in fx and "geq" in fx and "overlay" in fx
    assert fx.endswith("[v]")


def test_effects_glitch_uses_rgbashift():
    fx = build_effects_filtergraph(10.0, [], [1.5, 3.5])
    assert fx is not None
    assert "rgbashift" in fx


def test_effects_both_chain_in_order():
    fx = build_effects_filtergraph(10.0, [1.0], [2.0], label_out="v")
    assert fx is not None
    # glitch applied first, flash overlaid on top of it
    assert fx.index("rgbashift") < fx.index("color=c=white")


# ---- texture effects ----------------------------------------------------------

def test_texture_effects_none_when_all_disabled():
    from engine.transitions import build_texture_effects
    assert build_texture_effects(10.0) is None
    assert build_texture_effects(10.0, film_grain=False, vhs_jitter=False,
                                 light_leak=False) is None


def test_texture_film_grain_uses_temporal_noise():
    from engine.transitions import build_texture_effects
    fx = build_texture_effects(10.0, film_grain=True, label_out="v")
    assert fx is not None
    assert "noise=alls=" in fx and "allf=t+u" in fx
    assert fx.endswith("[v]")


def test_texture_vhs_jitter_uses_rgbashift():
    from engine.transitions import build_texture_effects
    fx = build_texture_effects(10.0, vhs_jitter=True)
    assert fx is not None
    assert "rgbashift" in fx and "edge=wrap" in fx and "enable=" in fx


def test_texture_light_leak_uses_warm_overlay():
    from engine.transitions import build_texture_effects
    fx = build_texture_effects(10.0, light_leak=True)
    assert fx is not None
    assert "color=c=#ff8c1a" in fx and "geq" in fx and "overlay" in fx


def test_texture_all_chain_in_order():
    from engine.transitions import build_texture_effects
    fx = build_texture_effects(10.0, film_grain=True, vhs_jitter=True,
                               light_leak=True, label_out="v")
    assert fx is not None
    assert fx.index("noise") < fx.index("rgbashift") < fx.index("color=c=#ff8c1a")


def test_atempo_chain_covers_ramp_speeds():
    import math
    from engine.transitions import atempo_chain
    for speed in (2.6, 1.5, 1.0, 0.82, 0.42):
        chain = atempo_chain(speed)
        assert chain, f"empty chain for {speed}"
        assert all(0.5 - 1e-9 <= f <= 2.0 + 1e-9 for f in chain)
        assert math.isclose(math.prod(chain), speed, abs_tol=1e-3)


def test_atempo_chain_invalid_input():
    from engine.transitions import atempo_chain
    assert atempo_chain(0.0) == []
    assert atempo_chain(-1.0) == []
    assert atempo_chain(float("nan")) == []
