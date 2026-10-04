// Reel Clipper — Pinokio install script.
//
// Drop this `pinokio/` folder into
//   ~/.pinokio/api/reel-clipper/
// Pinokio will clone the repo into `app/` and run `uv sync`.
//
// Prerequisites on the host: `uv` (https://docs.astral.sh/uv/) and
// `ffmpeg` + `ffprobe` on PATH (e.g. `brew install ffmpeg`).
module.exports = {
  run: [
    {
      method: "shell.run",
      params: {
        message: [
          "git clone https://github.com/Phan-Vincent/reel-clipper.git app",
        ]
      }
    },
    {
      method: "shell.run",
      params: {
        path: "app",
        message: [
          "uv sync"
        ]
      }
    }
  ]
}
