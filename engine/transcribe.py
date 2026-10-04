"""Speech-to-text captions, disk-cached per clip.

Optional feature: needs `faster-whisper` (install with
`uv sync --group captions`). Without it, everything degrades gracefully —
transcribe_clip returns [] and captions are simply not added.
"""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

logger = logging.getLogger("reel.transcribe")

CACHE_ROOT = Path("work/transcript_cache")
_MODEL = None


def _fingerprint(path: str) -> str:
    """Content fingerprint (first 1MB + size), like engine.cache's clip key."""
    p = Path(path)
    h = hashlib.sha1()
    with p.open("rb") as f:
        h.update(f.read(1 << 20))
    h.update(f"|{p.stat().st_size}".encode())
    return h.hexdigest()[:20]


def _model():
    """Lazy singleton WhisperModel (base = fast enough on CPU)."""
    global _MODEL
    if _MODEL is None:
        try:
            from faster_whisper import WhisperModel
            _MODEL = WhisperModel("base", device="cpu", compute_type="int8")
        except ImportError:
            logger.info("faster-whisper not installed — auto-captions disabled "
                        "(uv sync --group captions)")
            return None
        except Exception:                       # noqa: BLE001 — model download fail etc.
            logger.exception("failed to load whisper model")
            return None
    return _MODEL


def transcribe_clip(path: str) -> list[dict]:
    """Timed segments [{start, end, text}] for a clip's speech, or [] if
    transcription is unavailable or fails."""
    try:
        fp = _fingerprint(path)
    except OSError:
        return []
    CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    meta = CACHE_ROOT / f"{fp}.json"
    if meta.exists():
        try:
            return json.loads(meta.read_text())
        except Exception:                       # noqa: BLE001 — corrupt cache
            meta.unlink(missing_ok=True)

    model = _model()
    if model is None:
        return []
    try:
        segments, _info = model.transcribe(path)
        out = [{"start": round(s.start, 3), "end": round(s.end, 3),
                "text": s.text.strip()} for s in segments]
    except Exception:                           # noqa: BLE001 — undecodable / no speech
        logger.warning("transcription failed for %s", Path(path).name)
        return []
    try:
        meta.write_text(json.dumps(out))
    except OSError:
        pass
    return out


def _field(pk, key):
    """Read a pick field from either a dict or a Pick dataclass."""
    return pk[key] if isinstance(pk, dict) else getattr(pk, key)


def captions_for_picks(picks: list, transcripts_by_src: dict) -> dict:
    """Map timed transcription segments onto each pick's source window.

    Returns {pick_index: caption} for picks whose window overlaps any spoken
    text (joined in order). Pure, for tests; accepts dicts or Pick objects.
    """
    out: dict[int, str] = {}
    for i, pk in enumerate(picks):
        segs = transcripts_by_src.get(_field(pk, "src"), [])
        lo = _field(pk, "start")
        hi = lo + _field(pk, "length")
        words = [s["text"] for s in segs if s["end"] > lo and s["start"] < hi]
        text = " ".join(words).strip()
        if text:
            out[i] = text
    return out
