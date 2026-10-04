"""Source-audio windows + the duck-mix filtergraph builder."""
from engine.assemble import build_mix_filter
from engine.sourceaudio import _input_args, source_windows


def _pk(src="a.mp4", start=1.0, length=1.2, ramp="none"):
    return {"src": src, "start": start, "length": length, "ramp": ramp}


def _has_audio(src):
    return src.endswith(".mp4")   # fake: only .mp4 clips have audio


def test_source_windows_plain_clip():
    ws = source_windows([_pk("a.mp4", 2.0, 1.5)], _has_audio)
    assert ws == [{"src": "a.mp4", "start": 2.0, "length": 1.5}]


def test_source_windows_silence_for_ramped_segment():
    ws = source_windows([_pk("a.mp4", ramp="ramphold")], _has_audio)
    assert ws == [{"src": None, "length": 1.2}]


def test_source_windows_silence_for_clip_without_audio():
    ws = source_windows([_pk("a.mov")], _has_audio)   # .mov -> no audio
    assert ws == [{"src": None, "length": 1.2}]


def test_source_windows_mixed():
    picks = [_pk("a.mp4"), _pk("b.mov"), _pk("c.mp4", ramp="rampin")]
    ws = source_windows(picks, _has_audio)
    assert [w["src"] for w in ws] == ["a.mp4", None, None]


def test_source_windows_preserves_length_for_silence():
    ws = source_windows([_pk("b.mov", length=0.8)], _has_audio)
    assert ws[0]["length"] == 0.8        # timeline stays aligned


def test_input_args_cap_each_clip_window_with_t():
    # regression: without -t, concat swallowed the REST of each clip (3s clip
    # x 16 picks -> 29s track for an 8s montage)
    args = _input_args([{"src": "a.mp4", "start": 1.0, "length": 0.5}])
    assert "-ss" in args and "1.000" in args
    assert "-t" in args and "0.500" in args
    assert "-vn" in args and args[-2:] == ["-i", "a.mp4"]


def test_input_args_silence_via_anullsrc():
    args = _input_args([{"src": None, "length": 1.2}])
    assert "anullsrc=r=44100:cl=stereo" in args
    assert "1.200" in args


def test_mix_filter_has_duck_and_mix():
    f = build_mix_filter(6.0)
    assert "sidechaincompress" in f
    assert "amix=inputs=2:weights='1 0.8'" in f
    assert "loudnorm=I=-14:TP=-1.5:LRA=11" in f
    assert "alimiter" in f


def test_mix_filter_duck_params_flow_through_and_clamp():
    f = build_mix_filter(6.0, threshold=0.12, ratio=10.0)
    assert "threshold=0.1200:ratio=10.0" in f
    # clamped to sane ranges
    f2 = build_mix_filter(6.0, threshold=5.0, ratio=-3.0)
    assert "threshold=0.9900" in f2 and "ratio=1.0" in f2


def test_mix_filter_uses_snd_label_not_s():
    # ffmpeg 8.x treats [s] as a stream-specifier shorthand and rejects it;
    # labels are [0:a]/[1:a] because the audio inputs come first (see the
    # sidechaincompress quirk documented in build_mix_filter)
    f = build_mix_filter(6.0)
    assert "[snd]" in f and "[s]" not in f
    assert f.startswith("[0:a]atrim") and "[1:a]atrim" in f
    assert f.endswith("[a]")             # the mux maps -map "[a]"


def test_mix_filter_fade_matches_duration():
    f = build_mix_filter(6.0)
    assert "atrim=0:6.000" in f
    assert "st=5.500" in f               # music fade-out at duration - 0.5
