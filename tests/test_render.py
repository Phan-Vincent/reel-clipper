"""render.py pure helpers: seg-key determinism, edit application, bench."""
import numpy as np
import pytest

from engine.analysis import Candidate
from engine.beats import BeatGrid
from engine.config import RenderConfig
from engine.render import _seg_key, _segments_for, _slot_onsets, apply_edits, build_bench


def _plan():
    return {
        "durations": {"a.mp4": 10.0, "b.mp4": 8.0},
        "picks": [
            {"src": "a.mp4", "start": 1.0, "length": 1.2, "trans": "zoom",
             "outro": "none", "ramp": "none", "thumb_jpg": "", "day": 1,
             "src_fps": 30.0, "excitement": 0.5},
        ],
    }


def _fake_info(path, fps=30.0, duration=8.0):
    from engine.probe import VideoInfo
    return VideoInfo(path=str(path), width=1920, height=1080, fps=fps,
                     duration=duration, has_audio=False)


def test_seg_key_deterministic_and_sensitive():
    cfg = RenderConfig(fill="blur", preset="cinematic", quality="final")
    d = _plan()["picks"][0]
    assert _seg_key(d, cfg) == _seg_key(d, cfg)
    d2 = dict(d, trans="blackzoom")
    assert _seg_key(d2, cfg) != _seg_key(d, cfg)
    d3 = dict(d, src="b.mp4")
    assert _seg_key(d3, cfg) != _seg_key(d, cfg)
    cfg2 = RenderConfig(fill="crop", preset="cinematic", quality="final")
    assert _seg_key(d, cfg2) != _seg_key(d, cfg)


def test_seg_key_custom_grade_hashed_in():
    cfg = RenderConfig(custom_grade={"saturation": 1.2, "warmth": 1.0})
    cfg2 = RenderConfig(custom_grade={"saturation": 1.3, "warmth": 1.0})
    d = _plan()["picks"][0]
    assert _seg_key(d, cfg) != _seg_key(d, cfg2)


def test_apply_edits_swaps_src_and_clamps_start(monkeypatch):
    monkeypatch.setattr("engine.render.video_info", _fake_info)
    plan = _plan()
    edits = [{"src": "b.mp4", "center": 5.0}]
    out = apply_edits(plan, edits, RenderConfig())
    d = out["picks"][0]
    assert d["src"] == "b.mp4"
    # 8.0s source, 1.2s length -> start clamped into [0, 6.8]
    assert 0.0 <= d["start"] <= 8.0 - 1.2 + 1e-9
    assert abs(d["start"] + 0.6 - 5.0) < 1e-9       # centered on 5.0


def test_apply_edits_clamps_near_end_of_source(monkeypatch):
    monkeypatch.setattr("engine.render.video_info", _fake_info)
    plan = _plan()
    # center 7.5 would put the window [6.9, 8.1] past the 8.0s source -> clamp
    out = apply_edits(plan, [{"src": "b.mp4", "center": 7.5}], RenderConfig())
    assert out["picks"][0]["start"] == 8.0 - 1.2


def test_apply_edits_ignores_none_and_out_of_range(monkeypatch):
    monkeypatch.setattr("engine.render.video_info", _fake_info)
    plan = _plan()
    out = apply_edits(plan, [None, {"src": "b.mp4", "center": 1.0}],
                      RenderConfig())
    assert out["picks"][0]["src"] == "a.mp4"         # slot 1 out of range


def test_apply_edits_strips_ramp_when_source_too_short(monkeypatch):
    monkeypatch.setattr("engine.render.video_info", _fake_info)
    plan = _plan()
    plan["picks"][0]["ramp"] = "rampin"              # needs 0.45s min + margin
    plan["durations"]["b.mp4"] = 0.4                 # too short for the ramp
    out = apply_edits(plan, [{"src": "b.mp4", "center": 0.2}], RenderConfig())
    assert out["picks"][0]["ramp"] == "none"


def test_apply_edits_order_permutes_picks():
    plan = _plan()
    plan["picks"] = [dict(plan["picks"][0], start=1.0 + i) for i in range(3)]
    out = apply_edits(plan, {"order": [2, 0, 1]}, RenderConfig())
    starts = [p["start"] for p in out["picks"]]
    assert starts == [3.0, 1.0, 2.0]     # [2, 0, 1] of [1.0, 2.0, 3.0]


def test_apply_edits_ignores_invalid_order():
    plan = _plan()
    plan["picks"] = [dict(plan["picks"][0], start=1.0 + i) for i in range(3)]
    out = apply_edits(plan, {"order": [9, 0]}, RenderConfig())
    assert [p["start"] for p in out["picks"]] == [1.0, 2.0, 3.0]


def test_apply_edits_trim_nudges_and_clamps(monkeypatch):
    monkeypatch.setattr("engine.render.video_info", _fake_info)
    plan = _plan()                                   # a.mp4 is 10s
    out = apply_edits(plan, {"trims": [{"slot": 0, "delta": 0.5}]},
                      RenderConfig())
    assert out["picks"][0]["start"] == 1.5
    out2 = apply_edits(plan, {"trims": [{"slot": 0, "delta": 99.0}]},
                       RenderConfig())
    assert out2["picks"][0]["start"] == 10.0 - 1.2   # clamped to end


def test_apply_edits_trim_negative_clamps_to_zero(monkeypatch):
    monkeypatch.setattr("engine.render.video_info", _fake_info)
    plan = _plan()
    out = apply_edits(plan, {"trims": [{"slot": 0, "delta": -99.0}]},
                      RenderConfig())
    assert out["picks"][0]["start"] == 0.0


def test_apply_edits_trim_follows_shot_after_reorder(monkeypatch):
    monkeypatch.setattr("engine.render.video_info", _fake_info)
    plan = _plan()
    plan["durations"]["b.mp4"] = 10.0
    plan["picks"] = [dict(plan["picks"][0], src="a.mp4", start=1.0),
                     dict(plan["picks"][0], src="b.mp4", start=2.0)]
    # reorder [1, 0] then trim the shot that WAS slot 0 (now at position 1)
    out = apply_edits(plan, {"order": [1, 0],
                             "trims": [{"slot": 0, "delta": 0.5}]},
                      RenderConfig())
    assert out["picks"][0]["src"] == "b.mp4"         # slot 1 moved to front
    assert out["picks"][1]["src"] == "a.mp4"
    assert out["picks"][1]["start"] == 1.5           # trim travelled with it


def test_apply_edits_trim_strips_ramp_when_clipped(monkeypatch):
    monkeypatch.setattr("engine.render.video_info", _fake_info)
    plan = _plan()
    plan["durations"]["a.mp4"] = 1.0                 # very short source
    plan["picks"][0]["ramp"] = "rampin"
    out = apply_edits(plan, {"trims": [{"slot": 0, "delta": 0.3}]},
                      RenderConfig())
    assert out["picks"][0]["ramp"] == "none"


def test_apply_edits_order_recomputes_cut_points():
    plan = _plan()
    # three slots of DIFFERENT lengths: 1.2, 0.8, 1.0
    plan["picks"] = [dict(plan["picks"][0], start=1.0 + i,
                          length=ln) for i, ln in enumerate((1.2, 0.8, 1.0))]
    plan["cut_points"] = [0.0, 1.2, 2.0]
    before = list(plan["cut_points"])
    out = apply_edits(plan, {"order": [2, 0, 1]}, RenderConfig())
    # boundaries follow the reordered lengths: 1.0, 1.2, 0.8 -> 0, 1.0, 2.2
    assert out["cut_points"][0] == 0.0
    assert abs(out["cut_points"][1] - 1.0) < 0.04
    assert abs(out["cut_points"][2] - 2.2) < 0.04
    assert before != out["cut_points"]


def test_apply_edits_ignores_nan_and_bool_edits():
    plan = _plan()
    plan["picks"] = [dict(plan["picks"][0], start=1.0 + i) for i in range(3)]
    out = apply_edits(plan, {
        "order": [float("nan"), 1, 0],          # NaN -> perm rejected
        "trims": [{"slot": 0, "delta": float("nan")}],
        "swaps": [{"slot": 0, "src": "b.mp4", "center": float("inf")}],
    }, RenderConfig())
    assert [p["start"] for p in out["picks"]] == [1.0, 2.0, 3.0]


def test_apply_edits_caption_set_clear_and_travel(monkeypatch):
    plan = _plan()
    plan["picks"] = [dict(plan["picks"][0], start=1.0 + i) for i in range(3)]
    out = apply_edits(plan, {"captions": [{"slot": 0, "caption": "  night run  "}]},
                      RenderConfig())
    assert out["picks"][0]["caption"] == "night run"     # trimmed
    assert "caption" not in out["picks"][1]
    # empty string clears an override (back to the global caption)
    out2 = apply_edits(out, {"captions": [{"slot": 0, "caption": "  "}]},
                       RenderConfig())
    assert out2["picks"][0].get("caption", "") == ""


def test_apply_edits_caption_follows_reorder(monkeypatch):
    plan = _plan()
    plan["picks"] = [dict(plan["picks"][0], start=1.0 + i) for i in range(2)]
    out = apply_edits(plan, {"order": [1, 0],
                             "captions": [{"slot": 0, "caption": "hero"}]},
                      RenderConfig())
    # slot 0's shot moved to position 1; its caption travelled with it
    assert out["picks"][1]["caption"] == "hero"


def test_apply_edits_ignores_junk_captions():
    plan = _plan()
    plan["picks"] = [dict(plan["picks"][0]) for _ in range(2)]
    out = apply_edits(plan, {"captions": [{"slot": 99, "caption": "x"},
                                          {"slot": 0}, "nope",
                                          {"slot": 1, "caption": "ok"}]},
                      RenderConfig())
    assert out["picks"][0].get("caption", "") == ""
    assert out["picks"][1]["caption"] == "ok"


def test_segments_for_falls_back_to_cfg_day():
    # single-day / no-chapter footage has day=0 on picks; the DAY badge must
    # still render via cfg.day (regression: per-slot captions broke it)
    picks = [{"day": 0, "caption": ""}, {"day": 2, "caption": "hero"}]
    segs = _segments_for(picks, [30, 30], RenderConfig(day=1))
    assert segs[0] == (30, 1, "")      # pick day 0 -> cfg.day 1
    assert segs[1] == (30, 2, "hero")   # multi-day pick keeps its day
    # per-slot caption override wins over the global
    segs2 = _segments_for([{"day": 0, "caption": "x"}], [30],
                          RenderConfig(day=1, caption="global"))
    assert segs2[0] == (30, 1, "x")


def test_apply_edits_swap_and_trim_combined(monkeypatch):
    monkeypatch.setattr("engine.render.video_info", _fake_info)
    plan = _plan()
    plan["picks"] = [dict(plan["picks"][0]) for _ in range(2)]
    out = apply_edits(plan, {
        "swaps": [{"slot": 0, "src": "b.mp4", "center": 4.0}],
        "trims": [{"slot": 0, "delta": 0.25}],
    }, RenderConfig())
    assert out["picks"][0]["src"] == "b.mp4"
    # swap centers on 4.0 (start 3.4), then trim +0.25 -> 3.65
    assert out["picks"][0]["start"] == 3.65


def test_slot_onsets_renormalizes_and_defaults():
    grid = BeatGrid(tempo=120.0, beats=[0.5, 1.0, 1.5, 2.0],
                    onset_strength=[0.2, 0.6, 0.4, 1.0])
    out = _slot_onsets(grid, [0.5, 1.0, 2.0])
    assert out[0] == pytest.approx(0.2) and out[1] == pytest.approx(0.6)
    assert out[2] == 1.0                             # peak renormalized to 1
    assert _slot_onsets(BeatGrid(120.0, [], []), [0.5]) == [0.0]


def test_build_bench_excludes_same_source_overlaps_only():
    # the bench's overlap guard is same-source (a different clip at the same
    # timestamp is a legitimate alternative); near-duplicate SAME-source shots
    # that overlap a pick are excluded
    def cand(src, s, e, score, thumb_jpg):
        c = Candidate(src=src, start=s, end=e, score=score,
                      exposure=0.8, thumb_jpg=thumb_jpg)
        c.thumb = np.zeros(12, np.float32)
        return c
    pool = [cand("a.mp4", 1.1, 2.1, 0.9, "t1"),      # same src, overlaps pick
            cand("a.mp4", 5.0, 6.0, 0.8, "t2"),      # same src, far away
            cand("b.mp4", 1.1, 2.1, 0.85, "t3")]     # diff src, same time
    picks = [cand("a.mp4", 1.0, 2.0, 0.9, "t0")]
    bench = build_bench(pool, picks, n=10)
    srcs = [x["src"] for x in bench]
    centers = {x["src"]: x["center"] for x in bench}
    assert "a.mp4" in srcs and centers["a.mp4"] == 5.5    # far same-src shot
    assert "b.mp4" in srcs                                # diff src always allowed


def test_build_bench_drops_low_exposure_and_dupes():
    def cand(src, s, e, score, exposure, thumb):
        c = Candidate(src=src, start=s, end=e, score=score,
                      exposure=exposure, thumb_jpg="t")
        c.thumb = thumb
        return c
    low = cand("a.mp4", 1.0, 2.0, 0.9, 0.2, np.zeros(12, np.float32))
    pool = [low, cand("b.mp4", 1.0, 2.0, 0.8, 0.9, np.zeros(12, np.float32))]
    bench = build_bench(pool, [], n=10)
    assert all(b["src"] != "a.mp4" for b in bench)    # exposure < 0.3 dropped
