# Headless / CI image for Reel Clipper.
# Build:  docker build -t reel-clipper .
# Run:    docker run -p 8000:8000 -v reel_work:/app/work reel-clipper
#
# Notes:
# - The render engine needs ffmpeg + ffprobe on PATH (any recent build works;
#   the pipeline avoids drawtext and uses Pillow for the overlay).
# - VideoToolbox hardware encode is macOS-only, so this image always uses
#   libx264 (the default when hw_accel is off).

FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# uv for pinned, fast, reproducible installs
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Install deps first (layer-cached) from the committed lockfile.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Then the source.
COPY engine/ engine/
COPY app/ app/
RUN uv sync --frozen --no-dev

EXPOSE 8000
# --host 0.0.0.0 so the port is reachable from outside the container.
CMD ["uv", "run", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
