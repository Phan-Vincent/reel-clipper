module.exports = {
  daemon: true,
  run: [
    {
      method: "shell.run",
      params: {
        path: "app",
        message: [
          "uv run uvicorn app.main:app --port 8000"
        ],
        on: [{
          event: "/(http:\\/\\/[0-9.:]+)/",
          done: true
        }]
      }
    },
    {
      method: "local.set",
      params: {
        url: "{{input.event[1]}}"
      }
    }
  ]
}
