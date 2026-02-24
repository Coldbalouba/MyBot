# MyBot

A desktop dock that lets you control your browser with natural language. Uses AI (Ollama, Google Gemini, Groq, OpenRouter, or OpenCode Zen) to navigate, fill forms, click buttons, and extract info. Supports built-in Chromium or your own Chrome via extension.

**Created by AJ (Ahmed Jouini)**

---

## Quick Start (Desktop)

```bash
pip install -r requirements.txt
playwright install chromium
python main.py
```

Type a task in the dock bar (e.g. *"Search for coffee makers on Amazon"*) and press Enter. Switch providers and models from the dock dropdowns — only models usable for browser automation (vision + chat) are shown. No restart needed.

---

## Setup Guide

### 1. AI Provider: Ollama (Local)

**Best for:** Running everything locally, no API keys.

1. Install Ollama: https://ollama.ai
2. Run `ollama serve` (or start the Ollama app)
3. Pull a vision model: `ollama pull minicpm-v` or `ollama pull llama3.2-vision`
4. In the dock, select **Ollama (local)** from the provider dropdown and pick your model
5. If models don’t appear, click the refresh (↻) button

**Troubleshooting:**
- *"Cannot connect to Ollama"* → Start Ollama (`ollama serve`)
- *"No models installed"* → `ollama pull minicpm-v`
- *Slow first task* → Model loads on first use; later tasks are faster

---

### 2. AI Provider: OpenRouter (Cloud — 400+ Models)

**Best for:** Access to 400+ models (GPT, Claude, Gemini, Llama, etc.) through one API.

1. Get an API key: https://openrouter.ai/keys
2. In the dock, open Settings (⚙) → enter your OpenRouter API key
3. Select **OpenRouter** from the provider dropdown
4. Click the refresh (↻) button to load models
5. Pick a model (e.g. `google/gemini-2.0-flash`, `anthropic/claude-3.5-sonnet`)

**Notes:** Model IDs include the provider prefix (e.g. `openai/gpt-4o`). Single unified endpoint.

**Troubleshooting:** *"API key invalid"* → Check key at openrouter.ai/keys

---

### 3. AI Provider: Google Gemini (Free tier)

**Best for:** Free, no credit card. Good rate limits (60 RPM).

1. Get an API key: https://aistudio.google.com/app/apikey
2. In the dock, open Settings (⚙) → enter your Google API key
3. Select **Google Gemini** from the provider dropdown
4. Click refresh, then pick a model (e.g. `gemini-2.0-flash`, `gemini-1.5-pro`)

**Troubleshooting:** *"API key invalid"* → Get key at aistudio.google.com/app/apikey

---

### 4. AI Provider: Groq (Free tier — fast Llama)

**Best for:** Very fast inference, free tier. **Browser automation requires vision** — use the vision model.

1. Get an API key: https://console.groq.com/keys
2. In the dock, open Settings (⚙) → enter your Groq API key
3. Select **Groq** from the provider dropdown
4. Click refresh, then pick **`meta-llama/llama-4-scout-17b-16e-instruct`** (the only Groq model that supports images)

**Troubleshooting:** *"API key invalid"* → Get key at console.groq.com/keys

---

### 5. AI Provider: OpenCode Zen (Cloud)

**Best for:** No local GPU, Android, VPS, or when Ollama isn’t available.

1. Get an API key: https://opencode.ai/auth
2. In the dock, open Settings (⚙) → enter your OpenCode API key
3. Select **OpenCode Zen** in the provider dropdown
4. Pick a model (e.g. `glm-5-free`, `minimax-m2.5-free` for free tiers)

**Model endpoints:** OpenCode Zen routes each model to the correct API path:
- GPT models → `/responses`
- Claude models → `/messages`
- Gemini models → `/models/{id}`
- MiniMax, GLM, Kimi, etc. → `/chat/completions`

**Troubleshooting:**
- *"API key is invalid"* → Check key at opencode.ai/auth, ensure no extra spaces
- *"Cannot connect to OpenCode Zen"* → Check internet; firewall may block the request
- Free models: `big-pickle`, `minimax-m2.5-free`, `kimi-k2.5-free`, `glm-5-free`

---

### 6. Browser Modes

| Mode | Description | When to use |
|------|-------------|-------------|
| **Chromium** (default) | Launches a separate Chromium window | Headless, server, VPS |
| **Chrome Extension** | Controls your existing Chrome via extension | Use your real Chrome profile |

**Using Chrome Extension mode:**
1. Click the **Chrome** toggle in the dock
2. Click the **📦** button to open the extension folder
3. In Chrome: `chrome://extensions` → Developer mode ON → Load unpacked → select the `extension` folder
4. The extension should show a green dot when connected
5. If not connected, refresh the extension or reopen Chrome

---

## Server Mode (Headless / API)

Use the server for Android clients, web apps, or VPS deployment.

### Start the Server

```bash
pip install -r requirements.txt
playwright install chromium
python server.py
```

Server runs at **http://0.0.0.0:8765** by default.

### Options

```bash
python server.py --port 9000      # Custom port
python server.py --host 127.0.0.1 # Bind to localhost only
```

### Linux Headless (e.g. VPS)

```bash
export QT_QPA_PLATFORM=offscreen
python server.py --host 0.0.0.0
```

### Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `GET /health` | GET | Health check |
| `POST /task` | POST | Run a task (blocking) |
| `GET /task/stream` | GET | Run task with SSE status stream |

### Example: Run a Task

```bash
curl -X POST http://localhost:8765/task \
  -H "Content-Type: application/json" \
  -d '{"task": "Search for best laptops on Amazon", "provider": "opencode", "opencode_api_key": "your-key"}'
```

### Example: With OpenCode (No Ollama)

```json
{
  "task": "Find the price of iPhone 16 on Apple.com",
  "provider": "opencode",
  "opencode_api_key": "sk-your-key",
  "model_name": "glm-5-free"
}
```

### Example: With OpenRouter

```json
{
  "task": "Search for laptops on Amazon",
  "provider": "openrouter",
  "openrouter_api_key": "sk-or-your-key",
  "model_name": "google/gemini-2.0-flash"
}
```

### Example: With Google Gemini

```json
{
  "task": "Search for laptops on Amazon",
  "provider": "google",
  "google_api_key": "your-gemini-api-key",
  "model_name": "gemini-2.0-flash"
}
```

### Example: With Groq

```json
{
  "task": "Search for laptops on Amazon",
  "provider": "groq",
  "groq_api_key": "gsk-your-key",
  "model_name": "llama-3.2-90b-vision-preview"
}
```

### Example: Stream Status (SSE)

```bash
curl "http://localhost:8765/task/stream?task=Search+coffee+on+Google&provider=opencode&opencode_api_key=your-key"
```

---

## Free VPS Deployment

For hosting on free tiers (no Ollama on the server).

### Requirements

- Python 3.10+
- Playwright Chromium (~400 MB)
- ~1 GB RAM
- Use OpenCode Zen (or another cloud API) — not Ollama

### AI Providers Supported in Server Mode

| Provider | API Key | Notes |
|----------|---------|-------|
| **ollama** | None | Local models, requires Ollama running |
| **google** | google_api_key | Google Gemini, free tier |
| **groq** | groq_api_key | Fast Llama, free tier |
| **openrouter** | openrouter_api_key | 400+ models (GPT, Claude, Gemini, etc.) |
| **opencode** | opencode_api_key | Curated coding models |

### VPS Hosting Providers

| Provider | Free Tier | Notes |
|----------|-----------|-------|
| **Oracle Cloud** | 2 AMD VMs or 4 ARM VMs, 24 GB RAM | Good for long-running server |
| **Fly.io** | Limited free tier | Docker-friendly |
| **Railway** | Free tier with limits | Easy deploy |
| **Render** | Free tier | Sleeps when idle |

### Deploy on Linux (VPS)

```bash
# Install Python 3.10+, then:
pip install -r requirements.txt
playwright install chromium
playwright install-deps  # Linux dependencies for Chromium

# Run headless
export QT_QPA_PLATFORM=offscreen
python server.py --host 0.0.0.0 --port 8765
```

Use `provider: "opencode"` and `opencode_api_key` in requests; no Ollama needed on the server.

---

## Android / Mobile

Ollama does not run on Android. Use server mode + OpenCode:

1. Run the server on a PC or VPS (see above)
2. Ensure the server is reachable (same network or public IP/domain)
3. Point your Android app or web client at `http://your-server:8765`
4. Send requests with `provider: "opencode"` and your API key

---

## Troubleshooting

### Desktop app

| Issue | Fix |
|-------|-----|
| Dock doesn’t start | `pip install -r requirements.txt`; check Python 3.10+ |
| "Ollama not available" | Start Ollama (`ollama serve`) |
| "OpenCode key invalid" | Settings → verify key at opencode.ai/auth |
| "OpenRouter key invalid" | Settings → verify key at openrouter.ai/keys |
| "Google/Groq key invalid" | Settings → verify keys at aistudio.google.com, console.groq.com |
| Extension not connecting | Load unpacked extension; check green dot; restart Chrome |
| Chromium fails to launch | `playwright install chromium` |
| Slow first task | Model loads on first use; later tasks are faster |

### Server

| Issue | Fix |
|-------|-----|
| Server won’t start | `pip install fastapi uvicorn`; check port is free |
| "Module not found" | `pip install -r requirements.txt` |
| Chromium fails (Linux) | `playwright install-deps` and `playwright install chromium` |
| Black/blank display (Linux) | `export QT_QPA_PLATFORM=offscreen` |
| CORS errors from web client | Server has CORS enabled for `*`; check client URL |

### OpenCode Zen

| Issue | Fix |
|-------|-----|
| 401 / Invalid key | Refresh key at opencode.ai/auth |
| 403 | Key may be disabled or rate-limited |
| Timeout | Check firewall/proxy; OpenCode may be slow |

### Google Gemini

| Issue | Fix |
|-------|-----|
| 401 / Invalid key | Get key at aistudio.google.com/app/apikey |
| **429 Too Many Requests** | Google free tier rate limits. The app auto-retries with backoff. If it still fails, wait a moment between tasks. |
| Empty model list | Enter API key in Settings, then click refresh |
| **"429 OpenRouter" when I selected Google?** | If you selected **OpenRouter** as provider and picked a model like `google/gemini-2.0-flash`, you're using OpenRouter (a proxy), not the official Google API. Switch the provider dropdown to **"Google Gemini"** and add your Google API key in Settings to use the direct API. The status line shows "Provider: X" — verify it matches what you expect. |

### Groq

| Issue | Fix |
|-------|-----|
| 401 / Invalid key | Get key at console.groq.com/keys |
| **400 Bad Request** | Only `meta-llama/llama-4-scout-17b-16e-instruct` supports images. The app auto-switches to it when needed. |

### OpenRouter

| Issue | Fix |
|-------|-----|
| 401 / Invalid key | Refresh key at openrouter.ai/keys |
| **429 Too Many Requests** | Free-tier models have strict rate limits (often ~1 req/min). The app auto-retries with backoff (8s, 16s, 32s, 64s). If it still fails, wait a minute between tasks or switch to a paid model. |
| Empty model list | Enter API key in Settings, then click refresh |
| Timeout | Check firewall; OpenRouter has 400+ models, list may take a few seconds |

---

## Project Structure

```
mybot/
├── main.py           # Desktop dock (PyQt6)
├── server.py         # Headless API server (FastAPI)
├── agent_logic.py    # Browser automation + LLM
├── extension_bridge.py # WebSocket bridge for Chrome extension
├── playbook.py       # Record/replay task sequences
├── extension/        # Chrome extension (load unpacked)
├── playbooks/        # Saved playbooks (auto-created)
└── requirements.txt
```

---

## License

See project license file.

---

*MyBot — Created by AJ (Ahmed Jouini)*
