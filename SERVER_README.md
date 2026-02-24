# AI Browser — Server Mode

Run the AI Browser as a headless API server for remote clients (Android, web, free VPS).

**See [README.md](README.md) for full setup guide, troubleshooting, and desktop app help.**

## Quick Start

```bash
# Install dependencies (includes FastAPI, uvicorn)
pip install -r requirements.txt

# Run server (default: http://0.0.0.0:8765)
python server.py

# Custom port
python server.py --port 9000

# Headless Linux (e.g. free VPS) — use offscreen Qt
QT_QPA_PLATFORM=offscreen python server.py
```

## Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `GET /health` | GET | Health check |
| `POST /task` | POST | Run a task (blocking until complete) |
| `GET /task/stream` | GET | Run a task with SSE status stream |

## POST /task

**Request body:**
```json
{
  "task": "Search for best coffee makers on Amazon",
  "model_name": "minicpm-v",
  "provider": "ollama",
  "opencode_api_key": "",
  "use_playbooks": true,
  "screen_width": 1920,
  "screen_height": 1080,
  "use_extension": false
}
```

**Response:**
```json
{
  "success": true,
  "result": "Found 5 coffee makers...",
  "error": null,
  "status_log": ["🌐  Preparing Chromium…", "🧠  Task: ...", ...]
}
```

## GET /task/stream

Query params: `task`, `model_name`, `provider`, `opencode_api_key`, `use_playbooks`

Returns Server-Sent Events:
```
data: {"type": "status", "msg": "🌐  Preparing Chromium…"}
data: {"type": "status", "msg": "🧠  Task: ..."}
data: {"type": "done", "result": "Task completed."}
```

## Running Without Ollama (Android, Free VPS)

Ollama is not available on Android. Use cloud providers:

1. **OpenCode Zen** — Add your API key in the request:
   ```json
   {"task": "...", "provider": "opencode", "opencode_api_key": "your-key"}
   ```
   Get a key at https://opencode.ai/auth

2. **Future**: Add OpenRouter, Groq, etc. via `llm_providers.py` integration

## Free VPS Deployment

- **Oracle Cloud Free Tier**: 2 AMD VMs or 4 ARM VMs, 24 GB RAM total
- **Fly.io, Railway, Render**: Free tiers with limits
- **Requirements**: Python 3.10+, Chromium (Playwright), ~1 GB RAM

**Linux headless:**
```bash
export QT_QPA_PLATFORM=offscreen
playwright install chromium  # if not already
python server.py --host 0.0.0.0 --port 8765
```

**Extension mode** (`use_extension: true`) requires the Chrome extension to run on the same machine as the server — typically your PC, not a remote VPS. Use **Chromium mode** (default) for headless VPS deployment.
