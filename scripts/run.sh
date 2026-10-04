#!/usr/bin/env bash
# One-click launcher for Reel Clipper: install pinned deps, start the web UI,
# and open the browser. Prefer `make run` for plain starts; this script also
# checks prerequisites and opens the browser for a true double-click launch.
set -euo pipefail
cd "$(dirname "$0")/.."

PORT="${PORT:-8000}"

if ! command -v uv >/dev/null 2>&1; then
  echo "error: 'uv' not found — install it first (https://docs.astral.sh/uv/)" >&2
  exit 1
fi
if ! command -v ffmpeg >/dev/null 2>&1 || ! command -v ffprobe >/dev/null 2>&1; then
  echo "error: ffmpeg + ffprobe not found on PATH — install via Homebrew (brew install ffmpeg)" >&2
  exit 1
fi

echo "Reel Clipper — installing dependencies (uv sync)…"
# Note: uv sync installs the base deps only. For speech-to-text auto-captions,
# run `uv sync --group captions` once (adds faster-whisper).
uv sync

# open the browser shortly after the server starts listening
(
  sleep 1.5
  if command -v open >/dev/null 2>&1; then
    open "http://localhost:${PORT}"
  elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "http://localhost:${PORT}"
  else
    echo "open http://localhost:${PORT} in your browser"
  fi
) &

echo "Starting Reel Clipper on http://localhost:${PORT}"
exec uv run uvicorn app.main:app --port "${PORT}"
