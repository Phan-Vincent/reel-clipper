"""Auto-caption mapping (pure; transcription itself needs faster-whisper)."""
from engine.transcribe import captions_for_picks


def test_captions_for_picks_overlap():
    picks = [{"src": "a.mp4", "start": 0.0, "length": 2.0},
             {"src": "a.mp4", "start": 5.0, "length": 1.0}]
    t = {"a.mp4": [{"start": 0.5, "end": 1.2, "text": "hello"},
                   {"start": 1.3, "end": 1.8, "text": "world"},
                   {"start": 4.0, "end": 6.0, "text": "again"}]}
    caps = captions_for_picks(picks, t)
    assert caps[0] == "hello world"        # first window overlaps 2 segs
    assert caps[1] == "again"              # second overlaps the last seg


def test_captions_for_picks_no_overlap_is_empty():
    picks = [{"src": "a.mp4", "start": 10.0, "length": 1.0}]
    t = {"a.mp4": [{"start": 0.0, "end": 2.0, "text": "early"}]}
    assert captions_for_picks(picks, t) == {}


def test_captions_for_picks_missing_src():
    assert captions_for_picks([{"src": "x.mp4", "start": 0, "length": 1}], {}) == {}


def test_captions_for_picks_accepts_pick_objects():
    from engine.selection import Pick
    picks = [Pick(src="a.mp4", start=0.0, length=2.0)]
    t = {"a.mp4": [{"start": 0.5, "end": 1.0, "text": "hi there"}]}
    assert captions_for_picks(picks, t) == {0: "hi there"}
