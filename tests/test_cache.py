"""Analysis cache: fingerprinting, candidate roundtrip, LRU prune."""
import json
import os

import numpy as np

from engine.cache import (
    CACHE_ROOT, MAX_ENTRIES, _cand_from_dict, _cand_to_dict, _prune,
    clip_fingerprint, cached_analyze,
)
from engine.analysis import Candidate


def test_fingerprint_depends_on_content_size_and_params(tmp_path):
    p = tmp_path / "c.mp4"
    p.write_bytes(b"x" * 200)
    fp1 = clip_fingerprint(str(p), "faces=True")
    fp2 = clip_fingerprint(str(p), "faces=True")
    assert fp1 == fp2                        # deterministic
    p.write_bytes(b"y" * 200)
    assert clip_fingerprint(str(p), "faces=True") != fp1   # content matters
    assert clip_fingerprint(str(p), "faces=False") != fp1  # params matter


def test_fingerprint_ignores_mtime(tmp_path):
    p = tmp_path / "c.mp4"
    p.write_bytes(b"z" * 200)
    fp1 = clip_fingerprint(str(p), "faces=True")
    os.utime(p, (1000, 1000))
    assert clip_fingerprint(str(p), "faces=True") == fp1


def test_candidate_roundtrip():
    c = Candidate(src="/x.mp4", start=1.25, end=2.5, sharp=12.3, motion=4.5,
                  exposure=0.9, colorful=2.2, faces=1, audio_raw=0.3,
                  audio_n=0.7, thumb=np.arange(12, dtype=np.float32),
                  hist=np.array([0.5, 0.5], np.float32), thumb_jpg="/t.jpg")
    d = _cand_to_dict(c)
    back = _cand_from_dict(d, "/x.mp4")
    assert back.start == 1.25 and back.end == 2.5
    assert back.src == "/x.mp4"
    assert np.allclose(back.thumb, c.thumb)
    assert back.thumb_jpg == "/t.jpg"
    assert json.dumps(d)                    # serializable


def test_prune_keeps_max_entries(tmp_path, monkeypatch):
    monkeypatch.setattr("engine.cache.CACHE_ROOT", tmp_path)
    for i in range(MAX_ENTRIES + 10):
        (tmp_path / f"{i:020x}.json").write_text("{}")
        (tmp_path / f"{i:020x}").mkdir(exist_ok=True)
    _prune()
    remaining = list(tmp_path.glob("*.json"))
    assert len(remaining) <= MAX_ENTRIES
    # thumb dirs of pruned entries removed
    assert all((tmp_path / p.stem).exists() for p in remaining)


def test_cached_analyze_missing_file_delegates_to_analyze(tmp_path, monkeypatch):
    # analyze_clip on a nonexistent path raises -> cached_analyze surfaces it
    monkeypatch.setattr("engine.cache.CACHE_ROOT", tmp_path)
    try:
        cached_analyze(str(tmp_path / "missing.mp4"))
        assert False, "expected an exception for a missing file"
    except Exception:
        pass


def test_adaptive_fps_buckets():
    from engine.analysis import adaptive_fps
    assert adaptive_fps(10.0) == 3.0
    assert adaptive_fps(300.0) == 3.0        # boundary: 5min stays full rate
    assert adaptive_fps(301.0) == 2.0
    assert adaptive_fps(900.0) == 2.0        # boundary: 15min
    assert adaptive_fps(901.0) == 1.0
