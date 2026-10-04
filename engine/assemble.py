"""Turn Picks + a beat grid into a finished output.mp4.

Passes:
  1. per-segment: trim -> fill(9:16) -> grade -> normalized x264 intermediate
  2. concat demuxer (stream copy) -> silent montage
  3. one re-encode pass: beat-gated flash/glitch effects + camcorder overlay
  4. mux user's music (loudnorm + fade out), stream-copy video
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Callable, Optional

from . import ffmpeg
from .config import RenderConfig, canvas
from .fill import fill_graph
from .grade import build_grade
from .overlay import render_overlay_mov
from .selection import Pick
from .transitions import (WHIP_IN_KINDS, build_effects_filtergraph,
                          build_texture_effects, ramp_setpts,
                          segment_intro_filter, whip_pan_filter)

ProgressCb = Optional[Callable[[str, float], None]]


def _x264_common(quality: str) -> list[str]:
    if quality == "preview":
        return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23"]
    if quality == "draft":
        return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "26"]
    return ["-c:v", "libx264", "-preset", "medium", "-crf", "19"]


def _segment_common(quality: str = "final") -> list[str]:
    """Segment encode. `final` gets a master-quality intermediate (crf 16) since
    the effects/overlay pass re-encodes once more; `preview` stays fast
    (veryfast crf 23); `draft` is a cuts-only fast preview (veryfast crf 26).

    Segments deliberately stay on libx264 (never h264_videotoolbox): the
    concat-demuxer stream-copy in concat_segments needs exact, closed-GOP,
    frame-count-precise streams — libx264 honors `-g/-keyint_min/-frames:v`
    exactly. Videotoolbox can emit async/B-frames and is only trusted for the
    final single-file effects encode (cfg.hw_accel).
    """
    if quality == "draft":
        return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "26"]
    if quality == "preview":
        return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23"]
    return ["-c:v", "libx264", "-preset", "medium", "-crf", "16"]


def _keyframe_flags() -> list[str]:
    # regular closed-GOP keyframes so concat-demuxer copy is seamless
    return ["-g", "48", "-keyint_min", "48", "-sc_threshold", "0"]


def render_segment(pick: Pick, cfg: RenderConfig, out_path: str) -> int:
    """Render one normalized graded segment. Returns its frame count."""
    w, h = canvas(cfg.aspect)
    fps = cfg.fps
    grade = build_grade(cfg.preset_obj())
    nframes = max(1, round(pick.length * fps))
    # whip-in occupies the intro slot as a PRE-grade spatial chain; the
    # zoompan intros stay post-grade. A segment can also carry a whip-out.
    whip_in = (whip_pan_filter(pick.trans, nframes, cfg.effect_intensity,
                               w, h)
               if pick.trans in WHIP_IN_KINDS else "")
    whip_out = whip_pan_filter(pick.outro, nframes, cfg.effect_intensity, w, h)
    whip = ",".join(x for x in (whip_in, whip_out) if x)
    intro = ("" if whip_in else
             segment_intro_filter(pick.trans, cfg.effect_intensity, w, h, fps))
    intro = f",{intro}" if intro else ""
    # tpad clones the last frame forever; -frames:v caps it, so the segment
    # ALWAYS hits its exact frame count even if the source under-runs (clip
    # shorter than the slot, seek slop near EOF). Freeze beats off-beat drift.
    guard = "tpad=stop_mode=clone:stop=-1"
    ramp = ramp_setpts(pick.ramp, pick.length) if pick.ramp != "none" else ""
    # whip `n` expressions require a fixed-fps stream, so fps always precedes
    # the body: whip (pre-grade, so the vignette doesn't pan) -> grade -> intro.
    body = (f"{whip + ',' if whip else ''}{grade}{intro},"
            f"{guard},format=yuv420p[v]")
    if ramp:
        # re-time at source fps, then resample; fill+grade see a fixed-fps stream
        pre = f"[0:v]{ramp},fps={fps}[rt];"
        frag = fill_graph(cfg.fill, "rt", "fl", w, h)
        filt = f"{pre}{frag};[fl]{body}"
    else:
        frag = fill_graph(cfg.fill, "0:v", "fl", w, h)
        filt = f"{frag};[fl]fps={fps},{body}"
    args = [
        "-ss", f"{pick.start:.3f}", "-i", pick.src,
        "-filter_complex", filt, "-map", "[v]", "-an",
        "-frames:v", str(nframes), "-r", str(fps),
        *_segment_common(cfg.quality), *_keyframe_flags(),
        "-pix_fmt", "yuv420p", out_path,
    ]
    ffmpeg.run(args, desc=f"segment {Path(pick.src).name}")
    return nframes


def concat_segments(seg_paths: list[str], work: Path, out_path: str) -> None:
    # concat demuxer resolves relative paths against the list file's own dir,
    # so write absolute paths to avoid a doubled prefix.
    listfile = work / "concat.txt"
    listfile.write_text(
        "".join(f"file '{Path(p).resolve()}'\n" for p in seg_paths))
    ffmpeg.run(["-f", "concat", "-safe", "0", "-i", str(listfile),
                "-c", "copy", out_path], desc="concat")


def effects_and_overlay(
    cfg: RenderConfig,
    montage_silent: str,
    duration: float,
    flash_times: list[float],
    glitch_times: list[float],
    work: Path,
    out_path: str,
    on_progress: ProgressCb = None,
    day_ranges: list | None = None,
    chapter_dates: dict | None = None,
    segments: list | None = None,
) -> None:
    """Single re-encode: add flash/glitch effects and the camcorder overlay.

    `segments` = [(n_frames, day, caption)] per pick drives the per-slot
    caption burn-in (see render_overlay_mov).
    """
    do_flash = flash_times if cfg.flash else []
    do_glitch = glitch_times if cfg.glitch else []
    w, h = canvas(cfg.aspect)
    fps = cfg.fps
    final = "fx" if cfg.overlay else "v"
    want_texture = cfg.film_grain or cfg.vhs_jitter or cfg.light_leak
    fg = build_effects_filtergraph(
        duration, do_flash, do_glitch, cfg.effect_intensity,
        label_in="0:v", label_out=("fg1" if want_texture else final),
        w=w, h=h, fps=fps,
    )
    tx = build_texture_effects(
        duration, cfg.effect_intensity,
        label_in=("fg1" if fg else "0:v"), label_out=final,
        w=w, h=h, fps=fps,
        film_grain=cfg.film_grain, vhs_jitter=cfg.vhs_jitter,
        light_leak=cfg.light_leak,
    )
    effects = ";".join(x for x in (fg, tx) if x)

    inputs = ["-i", montage_silent]
    ui_mov = None
    if cfg.overlay:
        ui_mov = str(work / "ui.mov")
        render_overlay_mov(cfg, duration, ui_mov,
                           day_ranges=day_ranges, chapter_dates=chapter_dates,
                           segments=segments)
        inputs += ["-i", ui_mov]

    if effects and cfg.overlay:
        filt = f"{effects};[fx][1:v]overlay=0:0:format=auto,format=yuv420p[v]"
    elif effects:
        filt = effects
    elif cfg.overlay:
        filt = "[0:v][1:v]overlay=0:0:format=auto,format=yuv420p[v]"
    else:
        shutil.copyfile(montage_silent, out_path)
        return

    venc = (["-c:v", "h264_videotoolbox", "-b:v", "10M"]
            if cfg.hw_accel else _x264_common(cfg.quality))
    total = max(1, int(duration * fps))
    args = [*inputs, "-filter_complex", filt, "-map", "[v]", "-an",
            "-r", str(fps), *venc, "-pix_fmt", "yuv420p", out_path]
    ffmpeg.run_with_progress(
        args, total_frames=total,
        on_progress=(lambda p: on_progress("effects", p)) if on_progress else None,
        desc="effects+overlay",
    )


def build_mix_filter(duration: float, threshold: float = 0.05,
                     ratio: float = 8.0) -> str:
    """Duck-mix filtergraph: music (0) normalized, sidechain-ducked against
    source audio (1), then mixed with the source kept natural.

    Constraints discovered empirically on ffmpeg 8.1:
    - label must be [snd], not [s] ('s' is a stream-specifier shorthand)
    - the audio inputs MUST precede the video input in the command line:
      with a video file as input 0, sidechaincompress fails to bind its
      labels ("Stream specifier 'snd' matches no streams") — an ffmpeg quirk.
    """
    end = max(0.1, duration - 0.5)
    thr = max(0.001, min(0.99, threshold))
    rat = max(1.0, min(30.0, ratio))
    return (
        f"[0:a]atrim=0:{duration:.3f},asetpts=PTS-STARTPTS,"
        f"afade=t=out:st={end:.3f}:d=0.5[m];"
        f"[1:a]atrim=0:{duration:.3f},asetpts=PTS-STARTPTS,"
        f"afade=t=in:st=0:d=0.1[snd];"
        f"[m][snd]sidechaincompress=threshold={thr:.4f}:ratio={rat:.1f}:"
        f"attack=10:release=300[duck];"
        f"[duck]loudnorm=I=-14:TP=-1.5:LRA=11[dm];"
        f"[dm][snd]amix=inputs=2:weights='1 0.8':duration=first,"
        f"alimiter=limit=0.95[a]"
    )


def mux_music(video: str, music: Optional[str], duration: float,
              out_path: str, source_track: Optional[str] = None,
              keep_source_audio: bool = False,
              duck_threshold: float = 0.05, duck_ratio: float = 8.0) -> None:
    # faststart moves the moov atom to the front -> instant browser playback
    # and friendlier uploads to IG/TikTok
    if music and keep_source_audio and source_track:
        # original clip sound under the music (music ducks when it's loud).
        # Audio inputs come FIRST: see build_mix_filter's ffmpeg-8.1 note.
        ffmpeg.run(["-i", music, "-i", source_track, "-i", video,
                    "-filter_complex", build_mix_filter(duration, duck_threshold,
                                                        duck_ratio),
                    "-map", "2:v", "-map", "[a]", "-c:v", "copy",
                    "-c:a", "aac", "-b:a", "192k", "-shortest",
                    "-movflags", "+faststart", out_path],
                   desc="mux music + source audio")
        return
    if keep_source_audio and source_track and not music:
        # no music: the original sound IS the audio track. -t bounds the
        # output (never truncates the video tail like -shortest would if the
        # wav's sum-of-rounded-windows comes out a hair shorter than montage)
        ffmpeg.run(["-i", video, "-i", source_track,
                    "-map", "0:v", "-map", "1:a", "-c:v", "copy",
                    "-c:a", "aac", "-b:a", "192k", "-t", f"{duration:.3f}",
                    "-movflags", "+faststart", out_path],
                   desc="mux source audio")
        return
    if not music:
        ffmpeg.run(["-i", video, "-c", "copy",
                    "-movflags", "+faststart", out_path], desc="finalize")
        return
    end = max(0.1, duration - 0.5)
    a = (f"[1:a]atrim=0:{duration:.3f},asetpts=PTS-STARTPTS,"
         f"afade=t=out:st={end:.3f}:d=0.5,"
         f"loudnorm=I=-14:TP=-1.5:LRA=11[a]")
    ffmpeg.run(["-i", video, "-i", music, "-filter_complex", a,
                "-map", "0:v", "-map", "[a]", "-c:v", "copy",
                "-c:a", "aac", "-b:a", "192k", "-shortest",
                "-movflags", "+faststart", out_path],
               desc="mux music")


def export_audio(music: Optional[str], duration: float, out_path: str,
                 source_track: Optional[str] = None,
                 keep_source_audio: bool = False,
                 duck_threshold: float = 0.05,
                 duck_ratio: float = 8.0) -> None:
    """Write the reel's audio mix to an audio-only file.

    Mirrors mux_music's audio decisions (ducked music+source, source-only, or
    music-only) but drops the video stream. Codec follows the output extension:
    .mp3 -> libmp3lame, anything else -> aac in an .m4a container.
    """
    ext = Path(out_path).suffix.lower()
    acodec = (["-c:a", "libmp3lame", "-b:a", "192k"] if ext == ".mp3"
              else ["-c:a", "aac", "-b:a", "192k"])
    if music and keep_source_audio and source_track:
        ffmpeg.run(["-i", music, "-i", source_track,
                    "-filter_complex", build_mix_filter(duration, duck_threshold,
                                                        duck_ratio),
                    "-map", "[a]", *acodec, "-t", f"{duration:.3f}", out_path],
                   desc="export audio (music + source)")
        return
    if keep_source_audio and source_track and not music:
        ffmpeg.run(["-i", source_track, "-map", "0:a", *acodec,
                    "-t", f"{duration:.3f}", out_path],
                   desc="export audio (source)")
        return
    if not music:
        raise ValueError("nothing to export: no music and no source audio")
    end = max(0.1, duration - 0.5)
    a = (f"[0:a]atrim=0:{duration:.3f},asetpts=PTS-STARTPTS,"
         f"afade=t=out:st={end:.3f}:d=0.5,"
         f"loudnorm=I=-14:TP=-1.5:LRA=11[a]")
    ffmpeg.run(["-i", music, "-filter_complex", a, "-map", "[a]",
                *acodec, "-t", f"{duration:.3f}", out_path],
               desc="export audio (music)")
