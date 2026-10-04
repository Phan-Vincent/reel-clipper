"""Job registry: worker pruning policies (keep-N finished, orphan sweep).

These exercise the disk-hygiene logic against a temp WORK_ROOT with fake
jobs/dirs — no real renders or ffmpeg.
"""
from pathlib import Path

import pytest

from app import jobs
from engine.config import RenderConfig


@pytest.fixture
def fake_env(monkeypatch, tmp_path):
    root = tmp_path / "jobs"
    root.mkdir()
    monkeypatch.setattr(jobs, "WORK_ROOT", root)
    monkeypatch.setattr(jobs, "_JOBS", {})
    monkeypatch.setattr(jobs, "_ARCHIVE", {})
    monkeypatch.setattr(jobs, "OUTPUT_ROOT", tmp_path / "outputs")
    return root


def _mkjob(root: Path, jid: str, status: str, mtime: float):
    d = root / jid
    d.mkdir()
    (d / "output.mp4").write_bytes(b"x" * 100)
    import os
    os.utime(d, (mtime, mtime))
    job = jobs.Job(id=jid, cfg=RenderConfig(), workdir=d, out_path=d / "output.mp4",
                   status=status)
    jobs._JOBS[jid] = job
    return job


def test_prune_finished_keeps_newest_n(fake_env):
    root = fake_env
    jobs.KEEP_FINISHED_JOBS = 2
    for i, st in enumerate(("done", "done", "error", "done", "cancelled")):
        _mkjob(root, f"j{i}", st, mtime=100.0 + i)
    freed = jobs.prune_finished_jobs(keep=2)
    remaining = sorted(jobs._JOBS)
    assert remaining == ["j3", "j4"]          # two newest finished kept
    assert (root / "j0").exists() is False
    assert (root / "j4").exists() is True
    assert freed == 300                       # 3 dirs x 100 bytes


def test_prune_finished_never_touches_active(fake_env):
    root = fake_env
    _mkjob(root, "old", "done", mtime=1.0)
    _mkjob(root, "queued", "queued", mtime=2.0)
    _mkjob(root, "running", "running", mtime=3.0)
    _mkjob(root, "sb", "storyboard", mtime=4.0)
    jobs.prune_finished_jobs(keep=0)          # keep zero finished
    assert sorted(jobs._JOBS) == ["queued", "running", "sb"]
    assert (root / "old").exists() is False


def test_prune_orphan_sweeps_old_unreachable_dirs(fake_env, monkeypatch):
    root = fake_env
    _mkjob(root, "live", "running", mtime=9999999999.0)   # in-registry job
    for i, m in enumerate((10.0, 20.0, 30.0, 40.0, 50.0)):  # orphans, no registry entry
        d = root / f"orph{i}"
        d.mkdir()
        (d / "output.mp4").write_bytes(b"y" * 100)
        import os
        os.utime(d, (m, m))
    monkeypatch.setattr("time.time", lambda: 10000.0)     # everything is old
    freed = jobs.prune_orphan_jobs(max_age_s=3600.0, keep_newest=2)
    # newest 2 orphans (40, 50) kept; older 3 swept; the live job untouched
    assert sorted(p.name for p in root.iterdir()) == ["live", "orph3", "orph4"]
    assert freed == 300


def test_prune_orphan_skips_fresh_dirs(fake_env, monkeypatch):
    root = fake_env
    d = root / "fresh"
    d.mkdir()
    (d / "output.mp4").write_bytes(b"z" * 100)
    import os
    os.utime(d, (9999.0, 9999.0))
    monkeypatch.setattr("time.time", lambda: 10000.0)
    freed = jobs.prune_orphan_jobs(max_age_s=3600.0, keep_newest=0)
    assert freed == 0 and d.exists()          # newer than 1h -> left alone


def test_abandon_removes_registry_and_dir(fake_env):
    root = fake_env
    job = _mkjob(root, "a1", "queued", mtime=100.0)
    jobs.abandon(job)
    assert "a1" not in jobs._JOBS
    assert (root / "a1").exists() is False


# ---- persistence --------------------------------------------------------------

def test_job_roundtrip(fake_env):
    root = fake_env
    job = _mkjob(root, "j1", "storyboard", mtime=100.0)
    job.plan = {"target": 8.0, "picks": [{"src": "a.mp4", "start": 1.0,
                                          "caption": "night run"}]}
    job.bench = [{"src": "a.mp4", "center": 2.0, "thumb_jpg": "t"}]
    job.reused, job.rendered = 5, 3
    d = job.to_dict()
    back = jobs.Job.from_dict(d)
    assert back.id == "j1" and back.mode == "full"
    assert back.cfg.to_dict() == job.cfg.to_dict()
    assert back.status == "storyboard" and back.pct == job.pct
    assert back.plan == job.plan and back.bench == job.bench
    assert back.reused == 5 and back.rendered == 3
    assert back.out_path == root / "j1" / "output.mp4"


def test_save_and_load_jobs(fake_env):
    root = fake_env
    job = _mkjob(root, "aabbccddeeff", "done", mtime=100.0)   # hex id
    job.plan = {"target": 6.0}
    jobs._save_job(job)
    assert (root / "aabbccddeeff" / "job.json").exists()
    jobs._JOBS.clear()
    n = jobs.load_jobs()
    assert n == 1
    back = jobs._JOBS["aabbccddeeff"]
    assert back.status == "done" and back.plan == {"target": 6.0}


def test_load_jobs_marks_interrupted_running(fake_env):
    root = fake_env
    for jid in ("aabbccddee01", "aabbccddee02"):
        job = _mkjob(root, jid, "running" if jid.endswith("01") else "queued",
                     mtime=100.0)
        jobs._save_job(job)
    jobs._JOBS.clear()
    jobs.load_jobs()
    assert jobs._JOBS["aabbccddee01"].status == "error"
    assert "restart" in jobs._JOBS["aabbccddee01"].error
    assert jobs._JOBS["aabbccddee02"].status == "error"


def test_load_jobs_skips_dirs_without_job_json(fake_env):
    root = fake_env
    (root / "orphan").mkdir()           # no job.json -> not a job
    jobs._JOBS.clear()
    assert jobs.load_jobs() == 0
    assert "orphan" not in jobs._JOBS


# ---- output preservation (downloads survive pruning) --------------------------

def test_prune_preserves_output_then_deletes_dir(fake_env, monkeypatch, tmp_path):
    root = fake_env
    job = _mkjob(root, "aabbccddee01", "done", mtime=100.0)
    (job.workdir / "output.mp4").write_bytes(b"reel-data")
    jobs.prune_finished_jobs(keep=0)
    assert (root / "aabbccddee01").exists() is False      # dir pruned
    preserved = jobs.OUTPUT_ROOT / "aabbccddee01.mp4"
    assert preserved.exists() and preserved.read_bytes() == b"reel-data"
    # the done job moves to the archive: panel row + download stay alive
    assert "aabbccddee01" not in jobs._JOBS
    archived = jobs._ARCHIVE["aabbccddee01"]
    assert archived.archived is True
    assert archived.find_output() == preserved
    assert jobs.get_job("aabbccddee01") is archived


def test_prune_error_jobs_not_archived(fake_env):
    root = fake_env
    job = _mkjob(root, "aabbccddee09", "error", mtime=100.0)
    (job.workdir / "output.mp4").write_bytes(b"partial")   # no finished reel
    jobs.prune_finished_jobs(keep=0)
    assert "aabbccddee09" not in jobs._JOBS
    assert "aabbccddee09" not in jobs._ARCHIVE
    assert not (jobs.OUTPUT_ROOT / "aabbccddee09.mp4").exists()


def test_find_output_prefers_workdir_copy(fake_env):
    root = fake_env
    job = _mkjob(root, "aabbccddee02", "done", mtime=100.0)
    (job.workdir / "output.mp4").write_bytes(b"live")
    jobs.OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    (jobs.OUTPUT_ROOT / "aabbccddee02.mp4").write_bytes(b"preserved")
    assert job.find_output() == job.out_path           # live copy wins
    # when the live copy is gone, the preserved one answers
    (job.workdir / "output.mp4").unlink()
    assert job.find_output() == jobs.OUTPUT_ROOT / "aabbccddee02.mp4"


def test_prune_outputs_keeps_newest(fake_env, monkeypatch, tmp_path):
    root = fake_env
    out_root = tmp_path / "outputs"
    monkeypatch.setattr(jobs, "OUTPUT_ROOT", out_root)
    out_root.mkdir()
    import os
    for i in range(5):
        p = out_root / f"{i:012x}.mp4"
        p.write_bytes(b"x")
        os.utime(p, (1000 + i, 1000 + i))
    jobs._prune_outputs(keep=2)
    remaining = sorted(p.name for p in out_root.glob("*.mp4"))
    assert remaining == ["000000000003.mp4", "000000000004.mp4"]


def test_hint_for_error_matches():
    assert "install" in jobs.hint_for_error("ffprobe failed for /x.mp4")
    assert "too dark" in jobs.hint_for_error("Clips produced no usable moments (too short/dark?)")
    assert "brew install" in jobs.hint_for_error("ffprobe failed")
    assert "corrupted" in jobs.hint_for_error("Error opening input file")
    assert jobs.hint_for_error("some unrelated failure") is None
    assert jobs.hint_for_error("") is None


def test_share_filename_includes_date_and_sanitized_location(tmp_path):
    from types import SimpleNamespace
    from app.main import _share_filename
    out = tmp_path / "o.mp4"
    out.write_bytes(b"x")
    job = SimpleNamespace(cfg=SimpleNamespace(location="New York!"))
    name = _share_filename(job, out)
    assert name.startswith("reel_") and name.endswith(".mp4")
    assert "new-york" in name and " " not in name


def test_share_filename_falls_back_without_location(tmp_path):
    from types import SimpleNamespace
    from app.main import _share_filename
    out = tmp_path / "o.mp4"
    out.write_bytes(b"x")
    assert _share_filename(None, out).endswith("_reel.mp4")
