# Pinokio launcher

A 1-click [Pinokio](https://pinokio.computer) launcher for Reel Clipper.

**Pinokio's launcher entry is `pinokio.js`** (the `install.js` / `start.js` /
`update.js` / `reset.js` files are the individual menu actions). There is no
`pinokio.json` here because that file is optional social-link metadata, not
the launcher.

## One-time setup

1. Clone `https://github.com/Phan-Vincent/reel-clipper.git` and copy its
   whole `pinokio/` folder to `~/.pinokio/api/reel-clipper/`.
2. In Pinokio, open **Reel Clipper → Install** (clones the repo + `uv sync`),
   then **Start** (runs the web UI and opens it).

## Prerequisites

- `uv` on the host (https://docs.astral.sh/uv/)
- `ffmpeg` + `ffprobe` on PATH (`brew install ffmpeg` on macOS)

`install.js` runs `uv sync` (base deps). Auto-captions needs the optional
`captions` group — add `uv sync --group captions` to `install.js` if you want
speech-to-text out of the box.
