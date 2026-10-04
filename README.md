# Reel Clipper

Turn raw footage into aesthetic, beat-synced **9:16 Reels / TikToks** — cinematic
color grade, a "digicam / camcorder" UI overlay (blinking `● REC`, ticking
timecode, `++ DAY 01 ++`, location tag, viewfinder brackets), fast cuts synced to
your music, and light-leak / RGB-glitch accents on the beat.

Modeled on the reference reels in `reference/`.

## What it does

1. **Beat-detects** your music track (librosa) and builds a cut grid. When you
   upload a full song, it **suggests which section to use** — the drop / chorus /
   hook — with a waveform and click-to-set start (or `--analyze-music` on the CLI).
2. **Auto-chapters** multi-day footage: shooting dates come from embedded
   metadata, each day gets slots proportional to its footage, and the overlay's
   DAY 01 / DAY 02 counter (or the VHS date stamp) advances at each boundary.
   Set Day # to 0 (or `--no-chapters`) to opt out.
3. **Style packs** — Digicam '25, VHS '98, Clean Minimal, Warm Film — bundle a
   color grade, overlay skin, and effect defaults into one pick (`--pack vhs`).
   **Style-from-reference**: `--style-ref that_reel.mp4` (or the "Style
   reference" box in the web UI) analyzes a reel you love — cut rate vs its
   tempo, flash/whip/black-dip vocabulary, grade statistics — then
   **closed-loop tunes** the grade to your footage: short probe renders are
   measured against the reference and the knobs adjusted until the output
   measures like the reel (so already-warm footage gets less added warmth,
   not more). **Save any extracted style as a named pack** — it appears in the
   Style pack list next to the built-ins and re-applies (with fresh tuning) to
   any future render. CLI: `--list-packs`, `--saved-pack "My Style"`.
4. **Storyboard preview** (on by default in the web UI): after analysis you get
   a filmstrip of every picked shot + a bench of the best unused ones — swap any
   slot, then render; unchanged slots reuse their cached segments, so tweak
   cycles re-render only what you touched. Per-clip analysis is disk-cached
   (content-keyed), so repeat runs skip the slow scoring pass entirely.
2. **Analyzes** every raw clip — splits into shots, scores short windows by
   sharpness, motion, and exposure (OpenCV + PySceneDetect).
3. **Auto-selects** the best moments with a diversity rule (no repetitive or
   near-duplicate shots), and orders them for visual variety.
4. **Fits** 16:9 footage into 9:16 — blurred-background fill or full-bleed crop.
5. **Grades** with a teal-orange cinematic look (5 presets, or drop in a `.cube`).
6. **Overlays** the camcorder UI (rendered with Pillow — this ffmpeg build has no
   `drawtext`).
7. **Adds** white-flash + RGB-glitch on strong musical accents, zoom-punch /
   dip-to-black / whip-blur transitions on the beats, **whip-pan cuts** (a
   directional motion-wipe across the cut that reads as one continuous camera
   whip), and **beat-matched speed ramps** — punch-in bursts, whips that
   accelerate into the next cut, and rare slow-mo "hero" holds (120fps footage)
   that snap back to real time on the beat. Accent thresholds adapt to each
   track's own dynamics.
8. **Cuts to the beat**, muxes your music (loudness-normalized, fade-out), exports.

## Requirements

- macOS with `ffmpeg` + `ffprobe` on PATH (Homebrew).
- [`uv`](https://docs.astral.sh/uv/) (already installed here).

## Setup

```bash
uv sync            # creates a pinned Python 3.12 venv + installs deps
```

## Run — Web UI (recommended)

```bash
uv run uvicorn app.main:app --port 8000
# open http://localhost:8000
```

One-click: `./scripts/run.sh` (or `make run`) — checks prerequisites, `uv sync`s,
starts the server, and opens the browser.

A [Pinokio](https://pinokio.computer) 1-click launcher lives in `pinokio/`
(see `pinokio/README.md`) — copy it to `~/.pinokio/api/reel-clipper/` after
cloning this repository.

Drag in your clips, add a music track, pick a style / location / length, hit
**Generate**, watch the progress bar, then preview and download the `.mp4`.

## Run — Command line

```bash
uv run reel <folder-or-clips> -m music.mp3 --location "TOKYO" --day 1 \
    --duration 18 --preset cinematic --fill blur -o output/reel.mp4
```

Key flags: `--preset {cinematic,warm_film,moody,vibrant,clean}`,
`--fill {blur,crop}`, `--cuts-per-beat {0.5,1,2}`, `--no-overlay`,
`--no-flash`, `--no-glitch`, `--no-zoom`, `--no-ramps`,
`--quality {preview,final,draft}` (draft = cuts-only fast preview),
`--hw-accel` (h264_videotoolbox final encode),
`--keep-source-audio` (mix the clips' original sound under the music — the
music sidechain-ducks when footage is loud; tune with
`--duck-threshold` / `--duck-ratio`), `--caption "…"` (burn a caption
into the overlay; empty = none), `--auto-captions` (speech-to-text per slot),
`--verify` (measure how well cuts land on the beat),
`--dump-plan out.json` (export the edit plan), `--plan in.json` (re-render it),
`--from-job <id>` (duplicate a previous job's clips).

## Tests

```bash
uv run pytest        # unit tests — no ffmpeg required
```

## Make targets

```bash
make sync            # uv sync
make run             # start the web UI (PORT=8000 by default, override with PORT=…)
make test            # run the unit test suite
make clean           # caches only — safe, never touches footage/jobs/packs/downloads
make clean-work      # remove regenerable intermediates (job dirs, analysis/transcript
                     # caches, old CLI scratch) — keeps work/outputs/ + work/user_packs/
make clean-output    # delete rendered output/*.mp4
make verify-reference  # check the reference reels against reference/MANIFEST.sha256
```

## Reference reels

The example reels in `reference/` (used by style-from-reference) are large
binaries and are **not** tracked in git — see [REFERENCE.md](REFERENCE.md) for
provenance and how to verify integrity with `make verify-reference`.

## Storyboard

Review the auto-cut picks before rendering: swap any slot for a bench shot,
**trim** a slot's start within its source (±0.25 s steps, beat-locked length),
and **reorder** shots with the ⇦/⇨ buttons. Edits travel with a shot when you
reorder; only changed segments re-render. After a render the edits are baked
into the plan and the panel refreshes to show the final sequence.

## Original sound

Tick "Original sound (ducked)" (or `--keep-source-audio`) to mix the clips'
audio under the music: every pick's source-audio window is extracted and
concatenated into one track aligned to the montage, then the music is
sidechain-ducked against it (threshold −26 dB-ish, ratio 8) and the source is
mixed back at 0.8 weight. Speed-ramped segments contribute silence (the 0.42×
hold is below atempo's 0.5 floor). With no music, the original sound becomes
the audio track.

## Captions

The "Caption (burned in)" field (or `--caption`) renders word-wrapped text
(max 3 lines) in the overlay, above the DAY/location elements, in all three
skins. In the storyboard, select a slot to set a **per-slot caption override**
(shown as a `cc` badge); the override wins over the global caption and travels
with the shot when you reorder. Note: captions ride on the overlay —
disabling "Camcorder UI" removes them too.

**Auto-captions** (`--auto-captions` / the UI toggle) run speech-to-text on
each clip and turn the timed words into per-slot captions automatically.
It's an optional dependency: `uv sync --group captions` (faster-whisper), and
the model downloads on first use. Without it, auto-captions is silently
skipped. Note: faster-whisper's PyAV and OpenCV both ship a `libavdevice` —
macOS logs an "implemented in both" warning; it's cosmetic here but keep it in
mind if transcription ever misbehaves.

## Jobs survive restarts

Every job is persisted to `work/jobs/<id>/job.json` as it runs. Restarting the
server rehydrates the jobs panel — done/error/cancelled jobs keep their
download links, storyboard jobs stay editable and re-renderable (segments are
content-addressed, so "Render again" after a restart reuses unchanged shots),
and jobs that were mid-flight when the server died show
`interrupted by server restart`.

## Downloads outlive cleanup

Job dirs (raw clips + intermediates — the GBs) are pruned to the 6 most
recent finished jobs, but a finished reel's `output.mp4` is **preserved to
`work/outputs/`** first: pruned jobs stay in the panel as `archived` with a
working download link (re-render is blocked — the clips are gone). The 100
most recent outputs are kept, and retention is tunable via env:
`REEL_MAX_CONCURRENT`, `REEL_KEEP_JOBS`, `REEL_KEEP_OUTPUTS`.

## Jobs, queueing, cancellation

- The web UI runs at most **2 renders at once**; extra jobs queue and start
  automatically as workers free up. A **Jobs** panel shows every recent job
  (status, stage, progress, download) with a per-job cancel button, and the
  progress bar has its own Cancel.
- Cancelling a running job terminates its in-flight ffmpeg process (verified:
  segments, concat, effects pass, overlay, analysis decodes) and marks the job
  `cancelled`.
- **Disk hygiene**: uploads are capped (150 MB per music track, 4 GB per
  clip), duplicate clip filenames get `-1`/`-2` suffixes instead of silently
  overwriting, finished job dirs are pruned to the 6 most recent, and job dirs
  stranded by previous server runs are swept at startup.

## How it's built

```
engine/                pure-python engine (also runs headless via the CLI)
  config.py            RenderConfig + StylePreset registry
  probe.py  ffmpeg.py  ffprobe/ffmpeg subprocess helpers (+ progress parsing)
  beats.py             librosa beat grid (ffmpeg-energy fallback)
  analysis.py          OpenCV sharpness/motion/exposure + scene split
  selection.py         scoring, diversity de-dup, ordering
  fill.py  grade.py    9:16 framing + teal-orange grade filtergraphs
  overlay.py           Pillow camcorder UI -> qtrle alpha .mov
  transitions.py       beat-gated white-flash + rgb-glitch
  assemble.py          per-segment render -> concat -> effects/overlay -> mux
  render.py            orchestrator + CLI entry (`reel`)
app/                   FastAPI web UI
  main.py  jobs.py     upload, background job queue (max 2 concurrent),
                       cancellation, SSE progress, job list, range-served output
  static/              single-page frontend
```

## Notes

- **Vertical output** is 1080×1920 @ 30fps. Phone footage shot vertically fills the
  frame; 16:9 footage uses your chosen fill mode.
- The reference clips in `reference/` are *finished* reels (they already carry
  their own `REC`/location overlays) — great as a style target, but if you feed
  them back in as test footage you'll see two overlays stacked. Your own raw
  footage shows only the tool's overlay.
- First run downloads model/data for librosa; later runs are faster.
