# Reel Clipper — development convenience targets.
# `make clean` is safe (caches only). The destructive targets are named
# explicitly and never run as part of `clean`.

PORT ?= 8000

.PHONY: sync run test clean clean-cache clean-work clean-output verify-reference

## Install pinned dependencies
sync:
	uv sync

## Start the web UI on $(PORT)
run:
	uv run uvicorn app.main:app --port $(PORT)

## Run the unit test suite (no ffmpeg required)
test:
	uv run pytest

## Safe cleanup: Python caches only. Never touches footage, jobs, packs, or downloads.
clean: clean-cache

clean-cache:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache

## Remove regenerable intermediates + scratch (analysis/transcript caches, job dirs,
## old CLI/smoke dirs). Preserves work/outputs/ (finished reels) and work/user_packs/
## (saved style packs).
clean-work:
	rm -rf work/jobs work/analysis_cache work/transcript_cache
	rm -rf work/cli work/sb_run work/sb_run2 work/sb_test work/stylerefs
	rm -f work/*.log work/*.mp3 work/*.mp4

## Remove rendered .mp4 files in output/ (keeps the folder + .gitkeep)
clean-output:
	find output -name '*.mp4' -delete

## Verify the reference reels against the tracked checksum manifest
verify-reference:
	cd reference && shasum -a 256 -c MANIFEST.sha256
