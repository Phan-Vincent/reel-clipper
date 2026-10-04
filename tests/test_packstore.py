"""packstore: untrusted pack payload sanitization (whitelist + coercion)."""
import math

from engine import packstore


def test_num_nan_inf_fall_back_to_default():
    assert packstore._num(math.nan, 0, 1, 0.5) == 0.5
    assert packstore._num(math.inf, 0, 1, 0.5) == 0.5
    assert packstore._num(-math.inf, 0, 1, 0.5) == 0.5


def test_num_bool_rejected():
    assert packstore._num(True, 0, 1, 0.5) == 0.5
    assert packstore._num(False, 0, 1, 0.5) == 0.5


def test_num_clamps():
    assert packstore._num(99.0, 0, 1, 0.5) == 1.0
    assert packstore._num(-2.0, 0, 1, 0.5) == 0.0


def test_clean_name_strips_control_chars_and_caps():
    assert packstore.clean_name("a\x00b\nc") == "abc"
    assert packstore.clean_name("   ") == "Untitled style"
    assert len(packstore.clean_name("x" * 100)) <= packstore.NAME_MAX


def test_clean_pack_whitelists_grade_keys():
    raw = {"custom_grade": {"saturation": 3.0, "warmth": -1.0,
                            "evil": 9.0, "contrast": 1.1}}
    pack = packstore._clean_pack(raw, "t")
    g = pack["custom_grade"]
    assert set(g) == set(packstore._GRADE_BOUNDS)      # only known knobs
    assert g["saturation"] == 3.0
    assert g["warmth"] == 0.0
    assert "evil" not in g


def test_clean_pack_ref_stats_only_numeric():
    raw = {"ref_stats": {"sat": 0.4, "tempo": 128.0, "n_cuts": "many",
                         "bogus": 1.0, "grain": math.nan}}
    stats = packstore._clean_pack(raw, "t")["ref_stats"]
    assert stats == {"sat": 0.4, "tempo": 128.0}
    assert "bogus" not in stats       # not a measured key
    assert "grain" not in stats       # NaN dropped, never fabricated


def test_clean_pack_skin_and_flags():
    raw = {"overlay_skin": "vhs", "flash": 0, "zoom_transitions": "yes",
           "effect_intensity": 5.0}
    pack = packstore._clean_pack(raw, "t")
    assert pack["overlay_skin"] == "vhs"
    assert pack["flash"] is False
    assert pack["zoom_transitions"] is True     # truthy string -> bool
    assert pack["effect_intensity"] == 1.5      # clamped


def test_clean_pack_bad_grade_is_none():
    pack = packstore._clean_pack({"custom_grade": "nope"}, "t")
    assert pack["custom_grade"] is None
