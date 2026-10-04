# MyBot

A desktop dock that lets you control your browser with natural language. Uses AI (Ollama, Google Gemini, Groq, OpenRouter, or OpenCode Zen) to navigate, fill forms, click buttons, and extract info. Drives your own Chrome through the MyBot Bridge extension.

**Created by AJ (Ahmed Jouini)**

---

## Quick Start (Desktop)

```bash
pip install -r requirements.txt
python main.py
```

Then load the Chrome extension (see [Chrome Extension Setup](#6-chrome-extension-setup)). Type a task in the dock bar (e.g. *"Search for coffee makers on Amazon"*) and press Enter. Switch providers and models from the dock dropdowns — only models usable for browser automation (vision + chat) are shown. No restart needed.

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

### 6. Chrome Extension Setup

MyBot controls your existing Chrome (your real profile) through the bundled extension.

1. Click the **📦** button in the dock to open the extension folder
2. In Chrome: `chrome://extensions` → Developer mode ON → Load unpacked → select the `extension` folder
3. The extension popup and the dock's status dot turn green when connected
4. If not connected, refresh the extension or reopen Chrome

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
| Slow first task | Model loads on first use; later tasks are faster |

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
├── main.py             # Desktop dock (PyQt6)
├── agent_logic.py      # Vision agent loop + LLM providers
├── extension_bridge.py # WebSocket bridge for Chrome extension
├── extension/          # Chrome extension (load unpacked)
└── requirements.txt
```

---

## License

See project license file.

---

*MyBot — Created by AJ (Ahmed Jouini)*
