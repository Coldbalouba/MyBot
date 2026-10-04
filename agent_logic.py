"""
agent_logic.py — Chrome Extension Agent Worker
==============================================
Runs the vision agent loop in a QThread so the GUI never blocks.
Emits Qt signals for real-time status, results, and errors.

Created by AJ (Ahmed Jouini).

Drives the user's real Chrome via the MyBot Bridge extension (WebSocket).

Key improvements:
  • Async HTTP (httpx) — no blocking requests in async contexts
  • LLM token streaming — real-time token display in the log pane
  • Smart screenshot downscaling — reduces vision inference time by 60-80%
  • Model pre-warming — eliminates cold-start latency on first task
"""

import asyncio
import base64
import io
import json
import logging
import re
import threading
import traceback

import httpx
from PyQt6.QtCore import QThread, pyqtSignal

log = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────

SYSTEM_INSTRUCTION = (
    "You are a highly capable browser automation agent. "
    "Execute the user's instructions precisely and efficiently. "
    "Navigate websites, fill forms, click buttons, and extract information as needed. "
    "NEVER give up. If a step fails, remember it did not work and try a DIFFERENT approach. "
    "Never repeat the same action more than twice — if something did not work, reason about why and try something else. "
    "Keep exploring alternative strategies until the task is complete."
)

OLLAMA_BASE_URL = "http://127.0.0.1:11434"

# AI Providers
PROVIDER_OLLAMA = "ollama"
PROVIDER_OPENCODE = "opencode"
PROVIDER_OPENROUTER = "openrouter"
PROVIDER_GOOGLE = "google"
PROVIDER_GROQ = "groq"

_PROVIDER_LABELS = {
    PROVIDER_OLLAMA: "Ollama",
    PROVIDER_GOOGLE: "Google Gemini",
    PROVIDER_GROQ: "Groq",
    PROVIDER_OPENROUTER: "OpenRouter",
    PROVIDER_OPENCODE: "OpenCode Zen",
}

# Google Gemini (API key at aistudio.google.com/app/apikey)
GOOGLE_GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
GOOGLE_MODELS_URL = f"{GOOGLE_GEMINI_BASE}/models"

# Groq — fast Llama inference, free tier (API key at console.groq.com)
# CRITICAL: Only meta-llama/llama-4-scout-17b-16e-instruct supports vision (images).
# Other Groq models return 400 when given image inputs.
GROQ_BASE = "https://api.groq.com/openai/v1"
GROQ_CHAT_URL = f"{GROQ_BASE}/chat/completions"
GROQ_MODELS_URL = f"{GROQ_BASE}/models"
GROQ_VISION_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"

# OpenCode Zen — curated models from opencode.ai (API key at opencode.ai/auth)
OPENCODE_ZEN_BASE = "https://opencode.ai/zen/v1"
OPENCODE_ZEN_BASE_URL = OPENCODE_ZEN_BASE  # default for chat/completions
OPENCODE_ZEN_MODELS_URL = f"{OPENCODE_ZEN_BASE}/models"

# OpenRouter — unified gateway to 400+ models (API key at openrouter.ai/keys)
OPENROUTER_BASE = "https://openrouter.ai/api/v1"
OPENROUTER_CHAT_URL = f"{OPENROUTER_BASE}/chat/completions"
OPENROUTER_MODELS_URL = f"{OPENROUTER_BASE}/models"

# OpenCode Zen model → endpoint path (full URL = OPENCODE_ZEN_BASE + path)
# /responses = GPT (OpenAI format), /chat/completions = standard, /messages = Anthropic, /models/{id} = Google
OPENCODE_MODEL_ENDPOINTS: dict[str, str] = {
    # GPT (responses endpoint)
    "gpt-5.2": "responses",
    "gpt-5.2-codex": "responses",
    "gpt-5.1": "responses",
    "gpt-5.1-codex": "responses",
    "gpt-5.1-codex-max": "responses",
    "gpt-5.1-codex-mini": "responses",
    "gpt-5": "responses",
    "gpt-5-codex": "responses",
    "gpt-5-nano": "responses",
    # Claude (messages - Anthropic format; we try OpenAI format first, may need adapter)
    "claude-opus-4-6": "messages",
    "claude-opus-4-5": "messages",
    "claude-opus-4-1": "messages",
    "claude-sonnet-4-6": "messages",
    "claude-sonnet-4-5": "messages",
    "claude-sonnet-4": "messages",
    "claude-haiku-4-5": "messages",
    "claude-3-5-haiku": "messages",
    # Gemini (per-model URL)
    "gemini-3.1-pro": "models/gemini-3.1-pro",
    "gemini-3-pro": "models/gemini-3-pro",
    "gemini-3-flash": "models/gemini-3-flash",
    # chat/completions (default path)
    "minimax-m2.5": "chat/completions",
    "minimax-m2.5-free": "chat/completions",
    "minimax-m2.1": "chat/completions",
    "glm-5": "chat/completions",
    "glm-5-free": "chat/completions",
    "glm-4.7": "chat/completions",
    "glm-4.6": "chat/completions",
    "kimi-k2.5": "chat/completions",
    "kimi-k2.5-free": "chat/completions",
    "kimi-k2-thinking": "chat/completions",
    "kimi-k2": "chat/completions",
    "qwen3-coder": "chat/completions",
    "big-pickle": "chat/completions",
}


def _get_opencode_chat_url(model_id: str) -> str:
    """Return the full OpenCode Zen URL to POST chat requests for this model."""
    key = model_id.lower().strip()
    path = OPENCODE_MODEL_ENDPOINTS.get(key)
    if path is None:
        # Infer from model name prefix for models not in our map
        if key.startswith("gpt-"):
            path = "responses"
        elif key.startswith("claude-"):
            path = "messages"
        elif key.startswith("gemini-"):
            path = f"models/{key}"
        else:
            path = "chat/completions"
    return f"{OPENCODE_ZEN_BASE}/{path}"

# Extension agent limits
EXT_MAX_STEPS = 75  # Never give up — allow many steps to try different approaches

# Screenshot compression defaults — preserve full resolution, only re-encode
# to JPEG for ~3× size savings.  DO NOT downscale below native resolution
# because vision models need crisp text and chart details.
SCREENSHOT_MAX_WIDTH = 1920
SCREENSHOT_MAX_HEIGHT = 1200
SCREENSHOT_QUALITY = 92  # JPEG quality (1-100) — 92 is visually lossless


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║                      ASYNC OLLAMA HELPERS (httpx)                           ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

async def check_ollama_running() -> tuple[bool, str]:
    """
    Async check if Ollama server is reachable.
    Uses httpx so it never blocks the event loop.
    """
    async with httpx.AsyncClient(timeout=4.0) as client:
        try:
            resp = await client.get(f"{OLLAMA_BASE_URL}/api/tags")
            resp.raise_for_status()
            models = [m["name"] for m in resp.json().get("models", [])]
            if not models:
                return False, "Ollama is running but has no models installed."
            return True, f"Ollama OK — {len(models)} model(s) available."
        except httpx.ConnectError:
            return False, (
                "Cannot connect to Ollama at localhost:11434.\n"
                "Make sure Ollama is running (ollama serve)."
            )
        except Exception as e:
            return False, f"Ollama check failed: {e}"


async def fetch_ollama_models_async() -> list[str]:
    """Async fetch of available model names. Returns [] on failure."""
    async with httpx.AsyncClient(timeout=4.0) as client:
        try:
            resp = await client.get(f"{OLLAMA_BASE_URL}/api/tags")
            resp.raise_for_status()
            return sorted(m["name"] for m in resp.json().get("models", []))
        except Exception:
            return []


def _opencode_model_usable_for_agent(mid: str) -> bool:
    """
    OpenCode Zen is curated for coding/chat. Exclude embedding, audio, image-generation.
    """
    lid = mid.lower()
    blocklist = [
        "embed", "embedding", "whisper", "tts", "asr", "speech",
        "image-generation", "dall-e", "flux", "stable-diffusion",
    ]
    return not any(x in lid for x in blocklist)


def fetch_opencode_models(api_key: str | None = None) -> list[str]:
    """
    Fetch OpenCode Zen model IDs usable for browser automation (chat + vision).
    Excludes embedding, audio, image-generation models.
    """
    try:
        headers = {}
        if api_key and api_key.strip():
            headers["Authorization"] = f"Bearer {api_key.strip()}"
        resp = httpx.get(OPENCODE_ZEN_MODELS_URL, headers=headers, timeout=10.0)
        resp.raise_for_status()
        data = resp.json()
        models = [
            m["id"] for m in data.get("data", [])
            if isinstance(m, dict) and m.get("id") and _opencode_model_usable_for_agent(m["id"])
        ]
        return sorted(models)
    except Exception:
        return []


async def check_opencode_accessible(api_key: str) -> tuple[bool, str]:
    """
    Async check if OpenCode Zen API is reachable and the API key is valid.
    Uses the /models endpoint as a lightweight ping.
    """
    if not api_key or not api_key.strip():
        return False, "OpenCode Zen requires an API key. Get one at https://opencode.ai/auth"
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            headers = {"Authorization": f"Bearer {api_key.strip()}"}
            resp = await client.get(OPENCODE_ZEN_MODELS_URL, headers=headers)
            if resp.status_code in (401, 403):
                return False, (
                    "OpenCode Zen API key is invalid, expired, or unauthorized.\n"
                    "Check your key at https://opencode.ai/auth"
                )
            resp.raise_for_status()
            data = resp.json()
            models = data.get("data") or []
            count = len([m for m in models if isinstance(m, dict) and m.get("id")])
            return True, f"OpenCode Zen OK — {count} model(s) available."
        except httpx.ConnectError:
            return False, (
                "Cannot connect to OpenCode Zen (opencode.ai).\n"
                "Check your internet connection."
            )
        except httpx.HTTPStatusError as e:
            return False, f"OpenCode Zen error: {e.response.status_code} — {e.response.text[:200]}"
        except Exception as e:
            return False, f"OpenCode Zen check failed: {e}"


# ═══════════════════════════════════════════════════════════════════════════
# OpenRouter — unified API, single endpoint for all models
# ═══════════════════════════════════════════════════════════════════════════

def _openrouter_model_usable_for_agent(m: dict) -> bool:
    """
    True if model can be used for browser automation: chat + vision (image input), text output.
    Excludes: image-generation, audio-only, embedding-only, TTS.
    """
    mid = (m.get("id") or "").lower()
    # Blocklist: image gen, audio, embedding
    if any(x in mid for x in ["image-generation", "dall-e", "flux", "stable-diffusion", "imagen"]):
        return False
    if any(x in mid for x in ["whisper", "whisper-large", "asr", "tts", "speech-to-text"]):
        return False
    if any(x in mid for x in ["embed", "embedding", "text-embedding"]):
        return False
    arch = m.get("architecture") or {}
    inp = arch.get("input_modalities") or []
    out = arch.get("output_modalities") or []
    # Must accept image input (vision) for screenshots
    if "image" not in inp and "file" not in inp:
        return False
    # Must output text (chat), not image generation only
    if not out:
        return False
    if "text" not in out and "image" in out:
        return False  # image-gen only
    return True


def fetch_openrouter_models(api_key: str | None = None) -> list[str]:
    """
    Fetch OpenRouter model IDs usable for browser automation: vision + chat, text output.
    Excludes image-generation, audio, embedding models.
    """
    try:
        headers = {"Content-Type": "application/json"}
        if api_key and api_key.strip():
            headers["Authorization"] = f"Bearer {api_key.strip()}"
        resp = httpx.get(OPENROUTER_MODELS_URL, headers=headers, timeout=15.0)
        resp.raise_for_status()
        data = resp.json()
        models = [
            m["id"] for m in data.get("data", [])
            if isinstance(m, dict) and m.get("id") and _openrouter_model_usable_for_agent(m)
        ]
        return sorted(models)
    except Exception:
        return []


async def check_openrouter_accessible(api_key: str) -> tuple[bool, str]:
    """
    Async check if OpenRouter API is reachable and the API key is valid.
    Uses the /models endpoint as a lightweight ping.
    """
    if not api_key or not api_key.strip():
        return False, "OpenRouter requires an API key. Get one at https://openrouter.ai/keys"
    async with httpx.AsyncClient(timeout=12.0) as client:
        try:
            headers = {"Authorization": f"Bearer {api_key.strip()}"}
            resp = await client.get(OPENROUTER_MODELS_URL, headers=headers)
            if resp.status_code in (401, 403):
                return False, (
                    "OpenRouter API key is invalid or unauthorized.\n"
                    "Check your key at https://openrouter.ai/keys"
                )
            resp.raise_for_status()
            data = resp.json()
            models = data.get("data") or []
            count = len([m for m in models if isinstance(m, dict) and m.get("id")])
            return True, f"OpenRouter OK — {count} model(s) available."
        except httpx.ConnectError:
            return False, (
                "Cannot connect to OpenRouter (openrouter.ai).\n"
                "Check your internet connection."
            )
        except httpx.HTTPStatusError as e:
            return False, f"OpenRouter error: {e.response.status_code} — {e.response.text[:200]}"
        except Exception as e:
            return False, f"OpenRouter check failed: {e}"


def _messages_to_oai(messages: list[dict]) -> list[dict]:
    """Convert internal message format (with images) to OpenAI API format. Reusable for OpenRouter."""
    out = []
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content", "")
        images = m.get("images", [])
        if images:
            parts = [{"type": "text", "text": content}]
            for b64 in images:
                parts.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                })
            out.append({"role": role, "content": parts})
        else:
            out.append({"role": role, "content": content})
    return out


async def openrouter_chat_stream(
    model_id: str,
    messages: list[dict],
    api_key: str,
    *,
    on_token=None,
) -> str:
    """
    Stream chat completion from OpenRouter. Single endpoint for all models.
    Uses OpenAI-compatible format. Returns full response text.
    Retries on 429 (rate limit) with exponential backoff — free-tier models have strict limits.
    """
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key.strip()}",
        "Accept": "text/event-stream",
        "HTTP-Referer": "https://ai-browser.local",
    }
    body = {
        "model": model_id,
        "messages": _messages_to_oai(messages),
        "stream": True,
    }
    max_retries = 4
    base_delay = 8  # seconds — free tier often allows 1 req/min
    full_text = ""

    for attempt in range(max_retries):
        async with httpx.AsyncClient(timeout=120.0) as client:
            async with client.stream(
                "POST", OPENROUTER_CHAT_URL, json=body, headers=headers
            ) as resp:
                if resp.status_code == 429:
                    # Rate limited — drain response, back off, retry
                    try:
                        await resp.aread()  # consume body so connection can be reused
                    except Exception:
                        pass
                    retry_after = resp.headers.get("Retry-After")
                    if retry_after and str(retry_after).isdigit():
                        wait = int(retry_after)
                    else:
                        wait = base_delay * (2**attempt)  # 8, 16, 32, 64 s
                    if attempt < max_retries - 1:
                        log.warning(
                            "OpenRouter 429 rate limit — waiting %ds before retry %d/%d",
                            wait, attempt + 1, max_retries - 1,
                        )
                        await asyncio.sleep(wait)
                        continue
                    try:
                        err_body = await resp.aread()
                        err_text = err_body.decode("utf-8", errors="replace")[:500]
                    except Exception:
                        err_text = ""
                    raise RuntimeError(f"OpenRouter 429 (rate limit). {err_text}")
                if resp.status_code >= 400:
                    try:
                        err_body = await resp.aread()
                        err_text = err_body.decode("utf-8", errors="replace")[:600]
                    except Exception:
                        err_text = ""
                    raise RuntimeError(
                        f"OpenRouter API error {resp.status_code}. "
                        f"Check model ID (use full ID e.g. google/gemini-2.0-flash). "
                        f"Response: {err_text}"
                    )
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data = line[6:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                        delta = chunk.get("choices", [{}])[0].get("delta", {})
                        content = delta.get("content") or ""
                        if content:
                            full_text += content
                            if on_token:
                                on_token(content)
                    except json.JSONDecodeError:
                        pass
                return full_text
    return full_text


# ═══════════════════════════════════════════════════════════════════════════
# Google Gemini — free tier, vision support (API key at aistudio.google.com)
# ═══════════════════════════════════════════════════════════════════════════

def _messages_to_gemini(messages: list[dict]) -> list[dict]:
    """Convert internal message format to Gemini contents/parts format."""
    contents = []
    for m in messages:
        role = m.get("role", "user")
        if role == "system":
            role = "user"  # Gemini folds system into user
        elif role == "assistant":
            role = "model"  # Gemini uses "model" for assistant
        content = m.get("content", "")
        images = m.get("images", [])
        parts = []
        if content:
            parts.append({"text": content})
        for b64 in images:
            parts.append({
                "inlineData": {"mimeType": "image/jpeg", "data": b64},
            })
        if not parts:
            continue
        contents.append({"role": role, "parts": parts})
    return contents


def _google_model_usable_for_agent(m: dict) -> bool:
    """
    True if model supports generateContent (chat with vision). Excludes embedding-only.
    """
    name = (m.get("name") or "")
    model_id = name[7:] if name.startswith("models/") else name
    lid = model_id.lower()
    # Exclude embedding, audio-only
    if "embedding" in lid or "embed-" in lid or "text-embedding" in lid:
        return False
    if "speech" in lid or ("audio" in lid and "gemini" not in lid):
        return False
    # Must support generateContent (REST uses supportedGenerationMethods)
    methods = m.get("supportedGenerationMethods") or m.get("supported_generation_methods") or []
    methods_str = [str(x) for x in methods]
    if any(g in methods_str for g in ["generateContent", "GENERATE_CONTENT"]):
        return True
    # Fallback: include gemini-* chat models (API may omit field)
    return "gemini" in lid and "embed" not in lid


def fetch_google_models(api_key: str | None = None) -> list[str]:
    """
    Fetch Gemini model IDs usable for browser automation. Excludes embedding, audio-only.
    """
    try:
        key = (api_key or "").strip()
        if not key:
            return []
        url = f"{GOOGLE_MODELS_URL}?key={key}"
        resp = httpx.get(url, timeout=12.0)
        resp.raise_for_status()
        data = resp.json()
        models = []
        for m in data.get("models", []) or []:
            if not isinstance(m, dict) or not m.get("name"):
                continue
            if not _google_model_usable_for_agent(m):
                continue
            name = m["name"]
            if name.startswith("models/"):
                models.append(name[7:])
        return sorted(set(models))
    except Exception:
        return []


async def check_google_accessible(api_key: str) -> tuple[bool, str]:
    """Check if Google Gemini API is reachable and key is valid."""
    if not api_key or not api_key.strip():
        return False, "Google Gemini requires an API key. Get one at https://aistudio.google.com/app/apikey"
    try:
        url = f"{GOOGLE_MODELS_URL}?key={api_key.strip()}"
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url)
            if resp.status_code in (401, 403):
                return False, (
                    "Google Gemini API key is invalid.\n"
                    "Get a key at https://aistudio.google.com/app/apikey"
                )
            resp.raise_for_status()
            data = resp.json()
            models = data.get("models") or []
            return True, f"Google Gemini OK — {len(models)} model(s) available."
    except httpx.ConnectError:
        return False, "Cannot connect to Google Gemini. Check your internet connection."
    except Exception as e:
        return False, f"Google Gemini check failed: {e}"


async def google_chat_stream(
    model_id: str,
    messages: list[dict],
    api_key: str,
    *,
    on_token=None,
) -> str:
    """
    Stream chat from Google Gemini. Uses streamGenerateContent with alt=sse.
    Retries on 429 (rate limit) like OpenRouter — Google free tier also rate limits.
    """
    # Strip OpenRouter-style prefix if present (e.g. google/gemini-2.0-flash -> gemini-2.0-flash)
    mid = model_id.strip()
    for prefix in ("google/", "models/"):
        if mid.lower().startswith(prefix):
            mid = mid[len(prefix):]
            break
    if not mid:
        mid = "gemini-2.0-flash"

    key = api_key.strip()
    url = f"{GOOGLE_GEMINI_BASE}/models/{mid}:streamGenerateContent?alt=sse&key={key}"
    body = {"contents": _messages_to_gemini(messages)}
    full_text = ""
    max_retries = 4
    base_delay = 4

    for attempt in range(max_retries):
        async with httpx.AsyncClient(timeout=120.0) as client:
            async with client.stream("POST", url, json=body) as resp:
                if resp.status_code == 429:
                    try:
                        await resp.aread()
                    except Exception:
                        pass
                    retry_after = resp.headers.get("Retry-After")
                    wait = int(retry_after) if (retry_after and str(retry_after).isdigit()) else base_delay * (2**attempt)
                    if attempt < max_retries - 1:
                        log.warning(
                            "Google Gemini 429 rate limit — waiting %ds before retry %d/%d",
                            wait, attempt + 1, max_retries - 1,
                        )
                        await asyncio.sleep(wait)
                        continue
                    try:
                        err_body = await resp.aread()
                        err_text = err_body.decode("utf-8", errors="replace")[:500]
                    except Exception:
                        err_text = ""
                    raise RuntimeError(
                        f"Google Gemini API 429 (rate limit). "
                        f"Wait a moment and retry. Response: {err_text}"
                    )
                if resp.status_code >= 400:
                    try:
                        err_body = await resp.aread()
                        err_text = err_body.decode("utf-8", errors="replace")[:600]
                    except Exception:
                        err_text = ""
                    raise RuntimeError(
                        f"Google Gemini API error {resp.status_code}. "
                        f"Check model ID (e.g. gemini-2.0-flash, gemini-1.5-pro). "
                        f"Response: {err_text}"
                    )
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data = line[6:].strip()
                    try:
                        chunk = json.loads(data)
                        cands = chunk.get("candidates", [])
                        if cands:
                            content = cands[0].get("content", {})
                            for part in content.get("parts", []):
                                t = part.get("text", "")
                                if t:
                                    full_text += t
                                    if on_token:
                                        on_token(t)
                    except json.JSONDecodeError:
                        pass
                return full_text
    return full_text


# ═══════════════════════════════════════════════════════════════════════════
# Groq — fast Llama, free tier (API key at console.groq.com)
# ═══════════════════════════════════════════════════════════════════════════

def _groq_model_usable_for_agent(mid: str) -> bool:
    """
    Browser automation needs vision. Only meta-llama/llama-4-scout-17b-16e-instruct supports images.
    Exclude: Whisper (audio), any non-chat models.
    """
    lid = mid.lower()
    if "whisper" in lid or "distil-whisper" in lid:
        return False
    # Only vision model works with screenshots
    return mid == GROQ_VISION_MODEL


def fetch_groq_models(api_key: str | None = None) -> list[str]:
    """
    Fetch Groq models usable for browser automation. Only vision-capable chat models.
    Excludes Whisper (audio), image-gen. Groq has one vision model for agent use.
    """
    try:
        headers = {}
        if api_key and api_key.strip():
            headers["Authorization"] = f"Bearer {api_key.strip()}"
        resp = httpx.get(GROQ_MODELS_URL, headers=headers, timeout=10.0)
        resp.raise_for_status()
        data = resp.json()
        models = [
            m["id"] for m in data.get("data", [])
            if isinstance(m, dict) and m.get("id") and _groq_model_usable_for_agent(m["id"])
        ]
        return sorted(models)
    except Exception:
        return []


async def check_groq_accessible(api_key: str) -> tuple[bool, str]:
    """Check if Groq API is reachable and key is valid."""
    if not api_key or not api_key.strip():
        return False, "Groq requires an API key. Get one at https://console.groq.com/keys"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            headers = {"Authorization": f"Bearer {api_key.strip()}"}
            resp = await client.get(GROQ_MODELS_URL, headers=headers)
            if resp.status_code in (401, 403):
                return False, (
                    "Groq API key is invalid.\n"
                    "Get a key at https://console.groq.com/keys"
                )
            resp.raise_for_status()
            data = resp.json()
            models = data.get("data") or []
            return True, f"Groq OK — {len(models)} model(s) available."
    except httpx.ConnectError:
        return False, "Cannot connect to Groq. Check your internet connection."
    except Exception as e:
        return False, f"Groq check failed: {e}"


def _messages_have_images(messages: list[dict]) -> bool:
    """True if any message contains images."""
    for m in messages:
        if m.get("images"):
            return True
    return False


def _ensure_groq_image_size(b64: str, max_bytes: int = 3_800_000) -> str:
    """Ensure base64 image is under max_bytes (Groq limit 4MB). Downscale if needed."""
    import base64 as b64mod
    try:
        raw = b64mod.b64decode(b64)
        if len(raw) <= max_bytes:
            return b64
    except Exception:
        return b64
    try:
        from PIL import Image
        import io
        img = Image.open(io.BytesIO(raw)).convert("RGB")
        ratio = (max_bytes / len(raw)) ** 0.5
        new_size = (max(100, int(img.width * ratio)), max(100, int(img.height * ratio)))
        buf = io.BytesIO()
        img.resize(new_size, Image.LANCZOS).save(buf, format="JPEG", quality=75, optimize=True)
        return b64mod.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        return b64


async def groq_chat_stream(
    model_id: str,
    messages: list[dict],
    api_key: str,
    *,
    on_token=None,
) -> str:
    """
    Stream chat from Groq. OpenAI-compatible API.
    Only meta-llama/llama-4-scout-17b-16e-instruct supports images — we auto-switch if needed.
    """
    has_images = _messages_have_images(messages)
    effective_model = model_id
    if has_images and model_id != GROQ_VISION_MODEL:
        effective_model = GROQ_VISION_MODEL
        log.warning(
            "Groq: model '%s' does not support images. Using vision model '%s'.",
            model_id, GROQ_VISION_MODEL,
        )
    # Groq base64 image limit 4MB — compress if needed
    if has_images:
        msgs = []
        for m in messages:
            mm = dict(m)
            if mm.get("images"):
                mm["images"] = [_ensure_groq_image_size(img) for img in mm["images"]]
            msgs.append(mm)
        messages = msgs

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key.strip()}",
        "Accept": "text/event-stream",
    }
    body = {
        "model": effective_model,
        "messages": _messages_to_oai(messages),
        "stream": True,
    }
    full_text = ""
    async with httpx.AsyncClient(timeout=120.0) as client:
        async with client.stream("POST", GROQ_CHAT_URL, json=body, headers=headers) as resp:
            if resp.status_code >= 400:
                try:
                    err_body = await resp.aread()
                    err_text = err_body.decode("utf-8", errors="replace")[:600]
                except Exception:
                    err_text = ""
                raise RuntimeError(
                    f"Groq API error {resp.status_code}. "
                    f"For browser automation (images) use model '{GROQ_VISION_MODEL}'. "
                    f"Response: {err_text}"
                )
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data = line[6:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                    delta = chunk.get("choices", [{}])[0].get("delta", {})
                    content = delta.get("content") or ""
                    if content:
                        full_text += content
                        if on_token:
                            on_token(content)
                except json.JSONDecodeError:
                    pass
    return full_text


def _opencode_messages_to_oai(messages: list[dict]) -> list[dict]:
    """Convert internal message format (with images) to OpenAI API format."""
    return _messages_to_oai(messages)


async def opencode_chat_stream(
    model_id: str,
    messages: list[dict],
    api_key: str,
    *,
    on_token=None,
) -> str:
    """
    Stream chat completion from OpenCode Zen using the correct endpoint for the model.
    Returns full response text. Calls on_token(token_str) for each streamed token.
    """
    url = _get_opencode_chat_url(model_id)
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key.strip()}",
        "Accept": "text/event-stream",
    }
    body = {
        "model": model_id,
        "messages": _opencode_messages_to_oai(messages),
        "stream": True,
    }
    full_text = ""
    async with httpx.AsyncClient(timeout=120.0) as client:
        async with client.stream("POST", url, json=body, headers=headers) as resp:
            if resp.status_code >= 400:
                try:
                    err_body = await resp.aread()
                    err_text = err_body.decode("utf-8", errors="replace")[:600]
                except Exception:
                    err_text = ""
                raise RuntimeError(
                    f"OpenCode Zen API error {resp.status_code}. "
                    f"Check API key and model ID. Response: {err_text}"
                )
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data = line[6:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                    delta = chunk.get("choices", [{}])[0].get("delta", {})
                    content = delta.get("content") or ""
                    if content:
                        full_text += content
                        if on_token:
                            on_token(content)
                except json.JSONDecodeError:
                    pass
    return full_text


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║                SMART SCREENSHOT DOWNSCALING                                 ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

def _downscale_screenshot(
    raw_b64: str,
    max_width: int = SCREENSHOT_MAX_WIDTH,
    max_height: int = SCREENSHOT_MAX_HEIGHT,
    quality: int = SCREENSHOT_QUALITY,
) -> str:
    """
    Downscale + compress a base64-encoded screenshot for faster LLM vision.

    1. Decode from base64 PNG
    2. Resize to fit within max_width × max_height (preserving aspect ratio)
    3. Re-encode as JPEG at *quality*% — typically 3-5× smaller than PNG
    4. Return raw base64 string (no data-URL prefix)

    Falls back to the original if Pillow is unavailable or the image is
    already small enough.
    """
    try:
        from PIL import Image
    except ImportError:
        log.debug("Pillow not installed — skipping screenshot downscale")
        return raw_b64

    try:
        img_bytes = base64.b64decode(raw_b64)
        img = Image.open(io.BytesIO(img_bytes))

        orig_w, orig_h = img.size

        # Skip if already small
        if orig_w <= max_width and orig_h <= max_height:
            # Still re-encode as JPEG for compression
            buf = io.BytesIO()
            img_rgb = img.convert("RGB") if img.mode != "RGB" else img
            img_rgb.save(buf, format="JPEG", quality=quality, optimize=True)
            result_b64 = base64.b64encode(buf.getvalue()).decode("ascii")
            saved = len(raw_b64) - len(result_b64)
            if saved > 0:
                log.debug(
                    "Screenshot compressed %dx%d: %dK → %dK (JPEG q%d)",
                    orig_w, orig_h,
                    len(raw_b64) // 1024, len(result_b64) // 1024, quality,
                )
            return result_b64

        # Compute new dimensions (preserve aspect ratio)
        ratio = min(max_width / orig_w, max_height / orig_h)
        new_w = int(orig_w * ratio)
        new_h = int(orig_h * ratio)

        img_resized = img.resize((new_w, new_h), Image.LANCZOS)
        img_rgb = img_resized.convert("RGB") if img_resized.mode != "RGB" else img_resized

        buf = io.BytesIO()
        img_rgb.save(buf, format="JPEG", quality=quality, optimize=True)
        result_b64 = base64.b64encode(buf.getvalue()).decode("ascii")

        log.info(
            "Screenshot downscaled %dx%d → %dx%d: %dK → %dK (%.0f%% smaller)",
            orig_w, orig_h, new_w, new_h,
            len(raw_b64) // 1024, len(result_b64) // 1024,
            (1 - len(result_b64) / max(len(raw_b64), 1)) * 100,
        )
        return result_b64

    except Exception as e:
        log.warning("Screenshot downscale failed: %s — using original", e)
        return raw_b64


# Full-page screenshot dimensions (match extension) for downscale limits
FULL_PAGE_IMAGE_WIDTH = 1200
FULL_PAGE_IMAGE_MAX_HEIGHT = 6000


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║                  MODEL PRE-WARMING / SINGLETON POOL                         ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

class ModelPool:
    """
    Singleton pool that caches LLM client instances and pre-warms models.

    Pre-warming sends a tiny dummy prompt to Ollama so the model weights are
    loaded into GPU/RAM *before* the first real task.  Subsequent tasks get
    an instant response because the model is already hot.

    Usage::

        pool = ModelPool.get()
        pool.warm_model("minicpm-v")          # call once on startup / model change
        client = pool.get_ollama_client()      # reuse the same AsyncClient
    """

    _instance: "ModelPool | None" = None
    _lock = threading.Lock()

    def __init__(self):
        self._warmed: set[str] = set()
        self._warming_lock = threading.Lock()

    @classmethod
    def get(cls) -> "ModelPool":
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def is_warm(self, model_name: str) -> bool:
        return model_name in self._warmed

    def warm_model(self, model_name: str) -> None:
        """
        Pre-warm a model by sending a keep_alive request to Ollama.
        Blocks briefly — call from a background thread.
        """
        with self._warming_lock:
            if model_name in self._warmed:
                return
            try:
                # POST /api/generate with an empty prompt and keep_alive
                # loads the model into memory without generating anything.
                resp = httpx.post(
                    f"{OLLAMA_BASE_URL}/api/generate",
                    json={
                        "model": model_name,
                        "prompt": "",
                        "keep_alive": "10m",
                    },
                    timeout=60.0,
                )
                if resp.status_code < 400:
                    self._warmed.add(model_name)
                    log.info("Model '%s' pre-warmed successfully", model_name)
                else:
                    log.warning(
                        "Model pre-warm returned %d for '%s'",
                        resp.status_code, model_name,
                    )
            except Exception as e:
                log.warning("Model pre-warm failed for '%s': %s", model_name, e)

    def mark_warm(self, model_name: str):
        """Mark a model as warm without actually warming it (e.g. after first use)."""
        self._warmed.add(model_name)

    def invalidate(self, model_name: str):
        """Remove a model from the warm set (e.g. after Ollama restart)."""
        self._warmed.discard(model_name)


class ModelWarmupWorker(QThread):
    """Background thread that pre-warms a model in Ollama."""
    warmup_done = pyqtSignal(str, bool)  # (model_name, success)

    def __init__(self, model_name: str, parent=None):
        super().__init__(parent)
        self._model_name = model_name

    def run(self):
        pool = ModelPool.get()
        pool.warm_model(self._model_name)
        self.warmup_done.emit(self._model_name, pool.is_warm(self._model_name))


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║                      BACKGROUND WORKERS                                     ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

class ModelFetchWorker(QThread):
    """Fetches available models in a background thread (Ollama, OpenCode, OpenRouter, Google, Groq)."""
    models_fetched = pyqtSignal(list)   # list[str]
    fetch_failed = pyqtSignal(str)

    def __init__(
        self,
        provider: str = PROVIDER_OLLAMA,
        opencode_api_key: str = "",
        openrouter_api_key: str = "",
        google_api_key: str = "",
        groq_api_key: str = "",
        parent=None,
    ):
        super().__init__(parent)
        self._provider = provider
        self._opencode_api_key = opencode_api_key or ""
        self._openrouter_api_key = openrouter_api_key or ""
        self._google_api_key = google_api_key or ""
        self._groq_api_key = groq_api_key or ""

    def run(self):
        if self._provider == PROVIDER_GOOGLE:
            try:
                models = fetch_google_models(self._google_api_key)
                if models:
                    self.models_fetched.emit(models)
                else:
                    self.fetch_failed.emit(
                        "Google Gemini models empty. Check API key at aistudio.google.com/app/apikey"
                    )
            except Exception as e:
                self.fetch_failed.emit(f"Google Gemini: {e}")
        elif self._provider == PROVIDER_GROQ:
            try:
                models = fetch_groq_models(self._groq_api_key)
                if models:
                    self.models_fetched.emit(models)
                else:
                    self.fetch_failed.emit(
                        "Groq models empty. Check API key at console.groq.com/keys"
                    )
            except Exception as e:
                self.fetch_failed.emit(f"Groq: {e}")
        elif self._provider == PROVIDER_OPENCODE:
            try:
                models = fetch_opencode_models(self._opencode_api_key)
                if models:
                    self.models_fetched.emit(models)
                else:
                    self.fetch_failed.emit("OpenCode Zen models list is empty or request failed")
            except Exception as e:
                self.fetch_failed.emit(f"OpenCode Zen: {e}")
        elif self._provider == PROVIDER_OPENROUTER:
            try:
                models = fetch_openrouter_models(self._openrouter_api_key)
                if models:
                    self.models_fetched.emit(models)
                else:
                    self.fetch_failed.emit(
                        "OpenRouter models list empty or request failed. "
                        "Check your API key at openrouter.ai/keys"
                    )
            except Exception as e:
                self.fetch_failed.emit(f"OpenRouter: {e}")
        else:
            try:
                resp = httpx.get(f"{OLLAMA_BASE_URL}/api/tags", timeout=4.0)
                resp.raise_for_status()
                models = sorted(m["name"] for m in resp.json().get("models", []))
                self.models_fetched.emit(models)
            except httpx.ConnectError:
                self.fetch_failed.emit("Ollama not reachable at localhost:11434")
            except Exception as e:
                self.fetch_failed.emit(str(e))


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║                      EXTENSION AGENT HELPERS                                ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

_EXTENSION_SYSTEM_PROMPT = """\
You are an expert AI browser automation agent with strong visual understanding.
This is a MULTI-TURN conversation. Each turn you receive:
  - A FULL-PAGE screenshot of the entire tab (always the same format: 1200px wide, full page height up to 6000px)
  - A list of interactive elements with coordinates in that same image space
  - System feedback about the outcome of your previous action

FULL-PAGE SCREENSHOT (fixed size):
  - The screenshot is ALWAYS the full tab content in one image — not just the visible viewport.
  - Same coordinate system every time: 1200px width, height = full page (capped). No zoom or scroll confusion.
  - You see the whole page at once. Element coordinates (x, y) are in this full-page image.
  - Click coordinates are in the same space: use the (x, y) from the element list or from the image directly.

CACHING:
  - Screenshots are CACHED when the page hasn't changed (no navigation, no form submission).
  - When you see "cached screenshot", the page layout is UNCHANGED from the previous step.
  - Element coordinates from cached screenshots are STILL VALID.
  - Fresh full-page screenshots are taken after: navigation, Enter key (form submit), or every 5 steps.

NEVER GIVE UP. Keep trying different approaches until the task is complete.
REMEMBER WHAT DID NOT WORK. If an action failed or made no progress, do NOT repeat it.
NEVER repeat the same action (same type + same params) more than TWICE.

RESPOND WITH ONLY a single JSON object. No markdown, no explanation outside the JSON.

Required format — ALWAYS include "reasoning" before "action":
  {"reasoning":"<what you see + why this action advances the task>","action":"...","...":"..."}
Optional (use when helpful): "plan":["step1","step2",...], "confidence":0.0-1.0

Available actions:
  {"reasoning":"...","action":"navigate","url":"<full URL>"}
  {"reasoning":"...","action":"click","x":<int>,"y":<int>}
  {"reasoning":"...","action":"type","text":"<string>"}
  {"reasoning":"...","action":"scroll","direction":"down|up","amount":<pixels, default 300>}
  {"reasoning":"...","action":"press_key","key":"<key or combo>"}
  {"reasoning":"...","action":"select_all_and_type","text":"<string>"}
  {"reasoning":"...","action":"done","result":"<detailed summary of findings/outcome>"}

KEYBOARD — use "key" for press_key (single keys and combinations):
  Single: Enter, Tab, Escape, Backspace, Delete, Insert, Space
          Home, End, PageUp, PageDown
          ArrowUp, ArrowDown, ArrowLeft, ArrowRight
          F1-F12, CapsLock
          Letters a-z, digits 0-9 (use for shortcuts, e.g. "a" for Ctrl+A)
  Combos: Ctrl+Key, Alt+Key, Shift+Key, Meta+Key (Meta = Win/Cmd)
  Examples: "Ctrl+C" (copy), "Ctrl+V" (paste), "Ctrl+A" (select all), "Ctrl+F" (find),
            "Ctrl+Enter" (submit), "Alt+Tab" (switch app), "Meta+T" (new tab),
            "Escape" (close menu/cancel), "Enter" (submit form), "Tab" (next field)

Critical rules:
1. ALWAYS start with "reasoning" — describe what you see and WHY your chosen action advances the task.
2. "click" x/y are pixel coordinates on the full-page screenshot (1200px wide, same coordinate system as the element list).
3. To edit an input field: click it ONCE → then use "type" or "select_all_and_type" on the NEXT step.
   "type" appends text. "select_all_and_type" selects all existing text and replaces it.
   NEVER click the same field twice in a row — after clicking, the field is focused, so TYPE into it.
4. Use "navigate" to go directly to a URL when you know it.
5. Use "done" ONLY when the task is fully complete. Include a thorough answer.
6. NEVER use "wait". There is no "wait" action. The page is already loaded when you see the screenshot.
7. If the answer is already visible on screen (chart, price, data), use "done" immediately.
8. NEVER repeat the same action more than twice. If something did not work, reason about why and try something else.
9. Pay attention to [System feedback] messages — they tell you what happened. If something failed, remember it.
10. When using cached screenshots, you can reference elements from previous steps — their positions haven't changed.
11. Consider multiple ways to accomplish the task. If one path fails, switch to an alternative approach.
12. On sites with dynamic lists (LinkedIn, search results, feeds): if you don't see all results in the screenshot,
    use "scroll" to load more content. The page may lazy-load content as you scroll. After scrolling, a new
    screenshot will be taken and you'll see the newly loaded content.
13. TWO-PANEL LAYOUTS (LinkedIn, job sites, search results): Many pages split content into LEFT and RIGHT panels.
    - LEFT panel: usually a scrollable list (job cards, search results, links). Look for titles, company names, "Apply".
    - RIGHT panel: detail view for the selected item.
    If you see a layout with a list on the left and details on the right, DESCRIBE what you see in BOTH panels.
    Job listings often show: job title, company, location, "Apply"/"Save" buttons. Do NOT say "I don't see listings"
    if there is a list area with cards/items — look carefully at the LEFT side of the screenshot.
"""


def _parse_action(text: str) -> dict | None:
    """Extract a JSON action object from the LLM's response."""
    text = re.sub(r"```(?:json)?\s*", "", text)
    text = text.strip().rstrip("`")
    try:
        obj = json.loads(text)
        if isinstance(obj, dict) and "action" in obj:
            return obj
    except json.JSONDecodeError:
        pass
    for m in re.finditer(r"\{[^{}]+\}", text):
        try:
            obj = json.loads(m.group())
            if isinstance(obj, dict) and "action" in obj:
                return obj
        except json.JSONDecodeError:
            continue
    return None


def _ocr_screenshot(raw_b64: str, max_chars: int = 1500) -> str:
    """14. OCR + vision hybrid: extract text from screenshot for text-heavy pages."""
    try:
        import pytesseract
        from PIL import Image
    except ImportError:
        return ""
    try:
        raw = base64.b64decode(raw_b64)
        img = Image.open(io.BytesIO(raw)).convert("RGB")
        text = pytesseract.image_to_string(img)
        if text and text.strip():
            return text.strip().replace("\n\n", "\n")[:max_chars]
    except Exception:
        pass
    return ""


def _state_diff_summary(before: dict, after: dict) -> str:
    """11. State diff — brief summary of what changed for model feedback."""
    if not before or not after:
        return ""
    url_changed = after.get("url") != before.get("url")
    dom_changed = after.get("domHash") != before.get("domHash")
    if url_changed:
        return "State: URL changed (navigation). "
    if dom_changed:
        return "State: DOM changed. "
    return "State: unchanged. "


def _data_url_to_raw_b64(data_url: str) -> str:
    """Strip ``data:image/png;base64,`` prefix and return raw base64."""
    if "," in data_url:
        return data_url.split(",", 1)[1]
    return data_url


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║                           AGENT WORKER                                      ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

class AgentWorker(QThread):
    """
    Background thread that drives Chrome through the extension bridge
    on its own event loop.

    Emits ``token_stream`` for real-time LLM output.
    """

    # ── Signals ───────────────────────────────────────────────────────────
    status_update = pyqtSignal(str)   # step-level status lines
    task_finished = pyqtSignal(str)   # final result text
    error_occurred = pyqtSignal(str)  # non-recoverable error
    stuck_need_human = pyqtSignal(str)  # agent stuck, human-in-the-loop suggested
    token_stream = pyqtSignal(str)    # real-time LLM tokens

    # ── Init ──────────────────────────────────────────────────────────────

    def __init__(
        self,
        task: str,
        model_name: str = "minicpm-v",
        extension_bridge=None,
        custom_guidelines: str = "",
        provider: str = PROVIDER_OLLAMA,
        opencode_api_key: str = "",
        openrouter_api_key: str = "",
        google_api_key: str = "",
        groq_api_key: str = "",
        fallback_models: list[tuple[str, str]] | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self.task = task
        self.model_name = model_name
        self.extension_bridge = extension_bridge
        self.custom_guidelines = custom_guidelines.strip()
        # Normalize provider (case, whitespace) so comparisons always match
        p = str(provider or "").lower().strip()
        valid = (PROVIDER_OLLAMA, PROVIDER_OPENCODE, PROVIDER_OPENROUTER, PROVIDER_GOOGLE, PROVIDER_GROQ)
        self.provider = p if p in valid else PROVIDER_OLLAMA
        self.opencode_api_key = (opencode_api_key or "").strip()
        self.openrouter_api_key = (openrouter_api_key or "").strip()
        self.google_api_key = (google_api_key or "").strip()
        self.groq_api_key = (groq_api_key or "").strip()
        # Priority list of (slot number, provider, model); slot 1 is the primary model.
        self.model_chain: list[tuple[int, str, str]] = [(1, self.provider, self.model_name)]
        for slot, (fb_provider, fb_model) in enumerate(fallback_models or [], start=2):
            fb_provider = str(fb_provider or "").lower().strip()
            fb_model = str(fb_model or "").strip()
            if fb_provider in valid and fb_model:
                self.model_chain.append((slot, fb_provider, fb_model))
        self._stop_requested = False

    def request_stop(self):
        self._stop_requested = True

    async def _check_provider(self, provider: str) -> tuple[bool, str]:
        if provider == PROVIDER_OPENCODE:
            return await check_opencode_accessible(self.opencode_api_key)
        if provider == PROVIDER_OPENROUTER:
            return await check_openrouter_accessible(self.openrouter_api_key)
        if provider == PROVIDER_GOOGLE:
            return await check_google_accessible(self.google_api_key)
        if provider == PROVIDER_GROQ:
            return await check_groq_accessible(self.groq_api_key)
        return await check_ollama_running()

    # ── Thread entry point ────────────────────────────────────────────────

    def run(self):
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(self._execute_extension())
            finally:
                try:
                    pending = asyncio.all_tasks(loop)
                    for t in pending:
                        t.cancel()
                    if pending:
                        loop.run_until_complete(
                            asyncio.gather(*pending, return_exceptions=True)
                        )
                except Exception:
                    pass
                loop.close()
        except Exception as exc:
            tb = traceback.format_exc()
            self.error_occurred.emit(f"Fatal error: {exc}\n{tb}")

    # ══════════════════════════════════════════════════════════════════════
    #  CHROME EXTENSION AGENT LOOP (streaming vision)
    # ══════════════════════════════════════════════════════════════════════

    async def _execute_extension(self):
        """
        Drive the user's Chrome through the WebSocket extension bridge.
        Streams LLM tokens to ``token_stream`` in real time.

        Resilient to transient disconnections: if the extension drops mid-step,
        we wait for it to reconnect and retry the step instead of giving up.
        """
        import ollama as ollama_lib

        bridge = self.extension_bridge
        if bridge is None:
            self.error_occurred.emit(
                "Chrome extension bridge is not available.\n\n"
                "1. Click the 📦 Install button to load the extension.\n"
                "2. Make sure the extension shows a green dot in its popup."
            )
            return

        # Wait for the extension to connect (up to 10 s) if not already connected
        if not bridge.connected:
            self.status_update.emit("⏳  Waiting for Chrome extension to connect…")
            connected = await bridge.wait_for_connection(timeout=10.0)
            if not connected:
                self.error_occurred.emit(
                    "Chrome extension did not connect within 10 seconds.\n\n"
                    "1. Click the 📦 Install button to load the extension.\n"
                    "2. Make sure the extension shows a green dot in its popup."
                )
                return

        # ── Check each model's backend; keep the reachable ones in priority order ──
        reachable: list[tuple[int, str, str]] = []
        failures: list[str] = []
        for slot, provider, model in self.model_chain:
            label = f"Model {slot} ({_PROVIDER_LABELS.get(provider, provider)} / {model})"
            self.status_update.emit(f"🔍  Checking {label}…")
            ok, msg = await self._check_provider(provider)
            if ok:
                reachable.append((slot, provider, model))
                if msg:
                    self.status_update.emit(f"  ↳ {msg}")
            else:
                failures.append(f"{label}: {msg}")
                self.status_update.emit(f"  ↳ ⚠️  Skipping — {msg}")
        if not reachable:
            self.error_occurred.emit("No configured model is available:\n" + "\n".join(failures))
            return
        self.model_chain = reachable
        active = 0
        _, self.provider, self.model_name = self.model_chain[active]

        self.status_update.emit(f'🧠  Task: "{self.task}"')
        chain_desc = "  →  ".join(
            f"{slot}. {_PROVIDER_LABELS.get(p, p)} / {m}" for slot, p, m in self.model_chain
        )
        self.status_update.emit(f"⚡  Models: {chain_desc}  |  Mode: Chrome Extension")

        ollama_client = None

        def _ollama_to_openai_messages(msgs: list) -> list:
            """Convert Ollama-style messages (with images) to OpenAI format."""
            out = []
            for m in msgs:
                role = m.get("role", "user")
                content = m.get("content", "")
                images = m.get("images", [])
                if images:
                    parts = [{"type": "text", "text": content}]
                    for b64 in images:
                        parts.append({
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                        })
                    out.append({"role": role, "content": parts})
                else:
                    out.append({"role": role, "content": content})
            return out

        async def _chat_once(provider: str, model: str, messages: list) -> str:
            nonlocal ollama_client
            on_token = lambda t: self.token_stream.emit(t)  # noqa: E731
            if provider == PROVIDER_OPENCODE:
                return await opencode_chat_stream(model, messages, self.opencode_api_key, on_token=on_token)
            if provider == PROVIDER_OPENROUTER:
                return await openrouter_chat_stream(model, messages, self.openrouter_api_key, on_token=on_token)
            if provider == PROVIDER_GOOGLE:
                return await google_chat_stream(model, messages, self.google_api_key, on_token=on_token)
            if provider == PROVIDER_GROQ:
                return await groq_chat_stream(model, messages, self.groq_api_key, on_token=on_token)
            if ollama_client is None:
                ollama_client = ollama_lib.AsyncClient(host=OLLAMA_BASE_URL)
            full_text = ""
            stream = await ollama_client.chat(model=model, messages=messages, stream=True)
            async for chunk in stream:
                content = ""
                if hasattr(chunk, "message"):
                    content = getattr(chunk.message, "content", "")
                elif isinstance(chunk, dict):
                    content = chunk.get("message", {}).get("content", "")
                if content:
                    full_text += content
                    on_token(content)
            return full_text

        async def _chat_stream(messages: list) -> str:
            """Stream chat on the active model; on failure fall back to the next model in priority order."""
            nonlocal active
            while True:
                slot, provider, model = self.model_chain[active]
                try:
                    return await _chat_once(provider, model, messages)
                except Exception as e:
                    if self._stop_requested or active + 1 >= len(self.model_chain):
                        raise
                    active += 1
                    next_slot, next_provider, next_model = self.model_chain[active]
                    self.provider, self.model_name = next_provider, next_model
                    self.status_update.emit(
                        f"⚠️  Model {slot} ({_PROVIDER_LABELS.get(provider, provider)} / {model}) failed: "
                        f"{str(e)[:200]}\n"
                        f"↪️  Falling back to Model {next_slot} "
                        f"({_PROVIDER_LABELS.get(next_provider, next_provider)} / {next_model})"
                    )

        action_history: list[str] = []        # human-readable log per step
        action_signatures: list[str] = []     # exact action fingerprints for loop detection
        conversation: list[dict] = []         # multi-turn LLM messages (user+assistant)
        consecutive_failures = 0

        # Screenshot caching — only refresh when page actually changes
        last_screenshot_b64: str | None = None
        last_dom_elements: list[dict] = []
        last_url: str = ""
        steps_since_screenshot = 0
        last_action_type: str = ""  # Track last action to detect navigation/Enter
        last_press_key: str = ""  # Track which key was pressed (for Enter detection)
        task_plan: list[str] = []  # 3. Task decomposition: sub-tasks from model

        # ── Helper: wait for page to fully settle ─────────────────────────
        async def _wait_page_settle(reason: str, extra_delay: float = 0.0):
            self.status_update.emit(f"⏳  Waiting for page to fully load ({reason})…")
            try:
                result = await bridge.wait_stable(quiet_ms=2500, timeout_ms=20000)
                waited = result.get("data", {}).get("waited", 0)
                stop_reason = result.get("data", {}).get("reason", "?")
                if waited > 500:
                    self.status_update.emit(
                        f"    ↳ page settled after {waited}ms ({stop_reason})"
                    )
            except Exception as ws_err:
                self.status_update.emit(
                    f"    ↳ stability check unavailable ({ws_err}), using flat delay"
                )
                await asyncio.sleep(3.0)
            if extra_delay > 0:
                await asyncio.sleep(extra_delay)

        # ── Helper: build a fingerprint for an action ─────────────────────
        def _action_sig(action: dict) -> str:
            """
            Unique signature for an action including its parameters.
            click(100,200) != click(300,400) — both are 'click' but different.
            """
            act = action.get("action", "?")
            if act == "click":
                return f"click({action.get('x')},{action.get('y')})"
            elif act == "type":
                return f"type({action.get('text', '')[:30]})"
            elif act == "navigate":
                return f"navigate({action.get('url', '')[:60]})"
            elif act == "scroll":
                return f"scroll({action.get('direction','down')})"
            elif act == "press_key":
                return f"press_key({action.get('key','')})"
            elif act == "select_all_and_type":
                return f"select_all_and_type({action.get('text', '')[:30]})"
            return act

        # ── Helper: actions tried 2+ times (forbidden — never repeat) ──────
        def _forbidden_actions() -> list[str]:
            """Actions that were tried 2+ times — model must NOT repeat them."""
            from collections import Counter
            counts = Counter(action_signatures)
            return [sig for sig, c in counts.items() if c >= 2]

        # ── Helper: detect stuck / repeating loops ────────────────────────
        def _detect_stuck() -> str | None:
            """
            Return a nudge message if the model is stuck, else None.
            Trigger after only 2 identical actions (never repeat same step > twice).
            """
            if len(action_signatures) < 2:
                return None

            last_2 = action_signatures[-2:]

            # 2+ identical EXACT actions — forbidden, try something else
            if len(set(last_2)) == 1:
                forbidden = _forbidden_actions()
                forbidden_str = "; ".join(forbidden[:5]) if forbidden else last_2[0]
                return (
                    f"CRITICAL: You have already tried '{last_2[0]}' twice with no progress. "
                    f"DO NOT repeat it. It did not work. "
                    f"Actions that did NOT work (forbidden): {forbidden_str}. "
                    f"Try a COMPLETELY DIFFERENT approach — different element, different URL, "
                    f"different strategy. Reason about why the previous approach failed and "
                    f"try something new. Or if the answer is visible on screen, use 'done'."
                )

            # Check for alternating pattern with exact matches
            if len(action_signatures) >= 4:
                last_4 = action_signatures[-4:]
                if (last_4[0] == last_4[2] and last_4[1] == last_4[3]
                        and last_4[0] != last_4[1]):
                    return (
                        f"WARNING: You are alternating between '{last_4[0]}' and "
                        f"'{last_4[1]}' without progress. Stop this pattern. "
                        f"Try something completely new."
                    )

            return None

        for step in range(1, EXT_MAX_STEPS + 1):
            if self._stop_requested:
                self.status_update.emit("🛑  Stopped by user.")
                return

            try:
                # ── 1. Determine if we need a fresh screenshot ─────────────
                url = await bridge.get_url()
                needs_screenshot = (
                    step == 1 or  # Always on first step
                    url != last_url or  # URL changed (navigation)
                    last_action_type == "navigate" or  # Just navigated
                    last_action_type == "scroll" or  # Just scrolled — new content may have loaded
                    (last_action_type == "press_key" and last_press_key == "Enter") or  # Form submit
                    steps_since_screenshot >= 5  # Safety fallback every 5 steps
                )

                if needs_screenshot:
                    # ── Wait for page to fully load before capturing ───────
                    # (DOM complete, iframes loaded, DOM quiet 2.5s, + 2.5s post-quiet)
                    await _wait_page_settle("pre-screenshot")
                    # Extra delay for JS-rendered content, XHR/fetch, SPAs (e.g. LinkedIn)
                    await asyncio.sleep(5.0)

                    # ── Capture screenshot + DOM in parallel (faster) ───────
                    self.status_update.emit(f"📸  Step {step}: capturing screenshot + DOM…")

                    async def _capture_screenshot():
                        try:
                            return await bridge.screenshot(timeout=90.0)
                        except RuntimeError as ss_err:
                            err_msg = str(ss_err)
                            if "regular web page" in err_msg or "activeTab" in err_msg:
                                self.status_update.emit(
                                    f"📸  Step {step}: can't screenshot this page — "
                                    f"navigating to google.com first"
                                )
                                await bridge.navigate("https://www.google.com")
                                await _wait_page_settle("post-fallback-navigate", 1.0)
                                return await bridge.screenshot(timeout=90.0)
                            raise

                    screenshot_data_url, dom_result = await asyncio.gather(
                        _capture_screenshot(),
                        bridge.get_dom(),
                    )

                    raw_b64 = _data_url_to_raw_b64(screenshot_data_url)
                    # Run CPU-bound downscale in executor (full-page: allow taller image)
                    loop = asyncio.get_event_loop()
                    raw_b64 = await loop.run_in_executor(
                        None,
                        lambda: _downscale_screenshot(
                            raw_b64,
                            max_width=FULL_PAGE_IMAGE_WIDTH,
                            max_height=FULL_PAGE_IMAGE_MAX_HEIGHT,
                        ),
                    )
                    last_screenshot_b64 = raw_b64
                    last_url = url
                    steps_since_screenshot = 0

                    if dom_result.get("success"):
                        last_dom_elements = dom_result["data"].get("interactives", [])
                    else:
                        last_dom_elements = []
                else:
                    # ── Reuse cached screenshot ───────────────────────────
                    raw_b64 = last_screenshot_b64
                    steps_since_screenshot += 1
                    self.status_update.emit(
                        f"📸  Step {step}: reusing cached screenshot "
                        f"(page unchanged, {steps_since_screenshot} steps since capture)"
                    )

                # ── 2. Build elements text from cached or fresh DOM ─────────
                # 8. Accessibility tree fusion: include role and axLabel
                elements_text = ""
                if last_dom_elements:
                    lines = []
                    for el in last_dom_elements[:50]:
                        label = el.get("text", "").strip() or el.get("axLabel", "").strip()
                        if not label:
                            continue
                        role_part = f" role={el['role']}" if el.get("role") else ""
                        lines.append(
                            f"  [{el['tag']}"
                            f"{':' + el['type'] if el.get('type') else ''}"
                            f"{role_part}] "
                            f"\"{label}\" at ({el['x']}, {el['y']})"
                        )
                    if lines:
                        elements_text = "Interactive elements (with role/aria-label when available):\n" + "\n".join(lines)

                # ── 3. Build prompt ───────────────────────────────────────
                # Check for stuck patterns and add nudge
                stuck_nudge = _detect_stuck()
                forbidden = _forbidden_actions()
                nudge_text = ""
                if stuck_nudge:
                    nudge_text = f"\n\n⚠️  {stuck_nudge}"
                    self.status_update.emit(
                        f"🔄  Step {step}: detected repetitive pattern — nudging model"
                    )
                if forbidden:
                    nudge_text += (
                        f"\n\n🚫 ACTIONS THAT DID NOT WORK (do NOT repeat): "
                        f"{', '.join(forbidden[:8])}. Try something different."
                    )

                # Remaining-steps urgency
                remaining = EXT_MAX_STEPS - step
                urgency = ""
                if remaining <= 5:
                    urgency = (
                        f"\n\nURGENT: Only {remaining} steps left! If the answer "
                        f"is visible on screen, use 'done' NOW with your analysis."
                    )

                # Screenshot freshness indicator
                screenshot_note = ""
                if not needs_screenshot:
                    screenshot_note = (
                        f"\n\n📸 NOTE: This screenshot is CACHED (same view as previous step). "
                        f"The page layout and element positions are UNCHANGED. "
                        f"You can reference elements from the previous screenshot — "
                        f"their coordinates are still valid. Only take a new screenshot if you "
                        f"need to see visual changes (e.g., after form submission, navigation)."
                    )

                # First-step prompt: ask for task analysis and plan
                # 3. Task decomposition: include plan in subsequent prompts
                plan_prompt = ""
                if step == 1:
                    plan_prompt = (
                        "Before acting: (1) Analyze this task and outline your step-by-step plan. "
                        "(2) Think of 1–2 alternative approaches if the main path fails. "
                        "(3) Then choose your first action. "
                        "Optionally include \"plan\":[\"step1\",\"step2\",...] in your JSON.\n\n"
                    )
                elif task_plan:
                    plan_prompt = f"Your plan: {' | '.join(task_plan[:8])}\n\n"

                # 14. OCR + vision hybrid: optional OCR text for text-heavy pages
                ocr_text = ""
                if step == 1 or step % 5 == 0:
                    ocr_text = _ocr_screenshot(raw_b64)
                ocr_section = ""
                if ocr_text:
                    ocr_section = f"\n\nOCR text (may help for images/canvas):\n{ocr_text}\n"

                user_prompt = (
                    f"Task: {self.task}\n\n"
                    f"{plan_prompt}"
                    f"Current URL: {url}\n\n"
                    f"{elements_text}"
                    f"{ocr_section}"
                    f"{screenshot_note}"
                    f"{nudge_text}"
                    f"{urgency}\n\n"
                    f"Here is the {'current' if needs_screenshot else 'cached'} screenshot. "
                    f"Choose your next action. (step {step}/{EXT_MAX_STEPS})"
                )

                # ── 4. Ask the LLM with streaming (multi-turn) ────────────
                self.status_update.emit(f"🤔  Step {step}: thinking…")
                self.token_stream.emit("\n💬  ")

                # Build system prompt with optional custom guidelines
                system_prompt = _EXTENSION_SYSTEM_PROMPT
                if self.custom_guidelines:
                    system_prompt += (
                        f"\n\nADDITIONAL USER GUIDELINES (follow these strictly):\n"
                        f"{self.custom_guidelines}"
                    )

                # Build the new user message for this step
                step_user_msg = {
                    "role": "user",
                    "content": user_prompt,
                    "images": [raw_b64],
                }

                # Assemble full message list: system + conversation history + current
                messages = [{"role": "system", "content": system_prompt}]

                # Keep last N turns to avoid token overflow (each turn = user+assistant)
                MAX_HISTORY_TURNS = 8
                trimmed = conversation[-(MAX_HISTORY_TURNS * 2):]
                messages.extend(trimmed)
                messages.append(step_user_msg)

                llm_text = await _chat_stream(messages)
                self.token_stream.emit("\n")

                # Store this turn in conversation history
                # Strip the image from the stored user msg to save memory
                conversation.append({"role": "user", "content": user_prompt})
                conversation.append({"role": "assistant", "content": llm_text})

                # ── 5. Parse action ───────────────────────────────────────
                action = _parse_action(llm_text)
                if action is None:
                    self.status_update.emit(
                        f"⚠️  Step {step}: could not parse action — retrying"
                    )
                    action_history.append(f"PARSE_ERROR: {llm_text[:80]}")
                    action_signatures.append("PARSE_ERROR")
                    consecutive_failures += 1
                    if consecutive_failures >= 2:
                        # Recovery: one more attempt with stricter JSON-only prompt
                        self.status_update.emit(
                            f"🔄  Step {step}: parse failed {consecutive_failures}x — "
                            "trying with strict JSON prompt…"
                        )
                        recovery_prompt = (
                            f"Task: {self.task}\nCurrent URL: {url}\n\n"
                            "CRITICAL: Your previous response was not valid JSON. "
                            "You MUST respond with ONLY a single JSON object, no markdown, no extra text. "
                            "Format: {\"reasoning\":\"...\",\"action\":\"...\",\"...\":\"...\"}\n\n"
                            "If the task is complete, use {\"action\":\"done\",\"result\":\"...\"}. "
                            "Choose exactly ONE action and output valid JSON now:"
                        )
                        recovery_msg = {
                            "role": "user",
                            "content": recovery_prompt,
                            "images": [raw_b64],
                        }
                        llm_text = await _chat_stream([{"role": "system", "content": system_prompt}, recovery_msg])
                        self.token_stream.emit("\n")
                        action = _parse_action(llm_text)
                        if action is not None:
                            consecutive_failures = 0
                    if action is None:
                        if consecutive_failures >= 15:
                            self.error_occurred.emit(
                                "LLM returned unparseable responses 15 times in a row."
                            )
                            return
                        continue

                consecutive_failures = 0
                act_type = action["action"]
                action_signatures.append(_action_sig(action))

                # 3. Task decomposition: capture plan from first model response
                if not task_plan and action.get("plan") and isinstance(action["plan"], list):
                    task_plan = [str(p) for p in action["plan"][:10]]

                # 9. Confidence: low confidence -> add nudge for alternative strategies
                low_confidence = False
                conf = action.get("confidence")
                if conf is not None:
                    try:
                        low_confidence = float(conf) < 0.5
                    except (TypeError, ValueError):
                        pass

                # Log reasoning if the model provided it
                reasoning = action.get("reasoning", "")
                if reasoning:
                    self.status_update.emit(f"💭  {reasoning[:200]}")

                # ── 6. Execute action ─────────────────────────────────────

                if act_type == "done":
                    result_text = action.get("result", "Task completed.")
                    self.status_update.emit(f"✅  {result_text}")
                    self.task_finished.emit(result_text)
                    return

                # Track action outcome for conversation feedback
                action_outcome = ""

                if act_type == "click":
                    x, y = int(action["x"]), int(action["y"])
                    state_before = {}
                    try:
                        sb = await bridge.get_page_state()
                        if sb.get("success") and sb.get("data"):
                            state_before = sb["data"]
                    except Exception:
                        pass

                    res = None
                    for dx, dy in [(0, 0), (10, 0), (-10, 0), (0, 10), (0, -10)]:
                        try:
                            elem_check = await bridge.element_at(x + dx, y + dy)
                            if elem_check.get("success") and elem_check.get("data", {}).get("exists"):
                                res = await bridge.click(x + dx, y + dy)
                                if res.get("success"):
                                    x, y = x + dx, y + dy
                                    break
                        except Exception:
                            pass

                    if not res or not res.get("success"):
                        for el in last_dom_elements:
                            ex, ey = el.get("x", 0), el.get("y", 0)
                            if abs(ex - x) < 80 and abs(ey - y) < 80:
                                text_to_try = (el.get("text") or "").strip()
                                if text_to_try:
                                    res = await bridge.click_by_text(text_to_try[:60])
                                    break
                    if not res or not res.get("success"):
                        res = await bridge.click(x, y)
                    # 5. Script injection fallback when coordinate/click_by_text fails
                    if not res or not res.get("success"):
                        try:
                            script = f"""(function(){{
                              const imgX={x},imgY={y};
                              const sw=Math.max(document.documentElement.scrollWidth,document.body.scrollWidth,1);
                              const sh=Math.max(document.documentElement.scrollHeight,document.body.scrollHeight,1);
                              const iw=1200,ih=Math.min(Math.ceil(sh*1200/sw),6000);
                              const docX=imgX*sw/iw,docY=imgY*sh/ih;
                              const vw=window.innerWidth,vh=window.innerHeight;
                              window.scrollTo(docX-vw/2,docY-vh/2);
                              const el=document.elementFromPoint(vw/2,vh/2);
                              if(!el)return{{success:false}};
                              el.click();if(typeof el.focus==='function')el.focus();
                              return{{success:true,tag:el.tagName}};
                            }})();"""
                            inj = await bridge.execute_script(script)
                            inner = inj.get("data", {}).get("result") or {}
                            if inj.get("success") and inner.get("success"):
                                res = {"success": True, "data": inner}
                        except Exception:
                            pass

                    tag = res.get("data", {}).get("tag", "?") if (res and res.get("success")) else "FAIL"
                    action_history.append(f"click ({x},{y}) → {tag}")

                    state_after = {}
                    try:
                        await asyncio.sleep(0.6)
                        sa = await bridge.get_page_state()
                        if sa.get("success") and sa.get("data"):
                            state_after = sa["data"]
                    except Exception:
                        pass
                    state_diff = _state_diff_summary(state_before, state_after)

                    action_outcome = f"Clicked at ({x},{y}). Element hit: <{tag}>. {state_diff}If you intended to focus an input field, you can now use 'type' to enter text."
                    await asyncio.sleep(1.5)

                elif act_type == "type":
                    text = action["text"]
                    state_before_t = {}
                    try:
                        sb = await bridge.get_page_state()
                        if sb.get("success") and sb.get("data"):
                            state_before_t = sb["data"]
                    except Exception:
                        pass
                    # 12. Pre-action element check: verify activeElement is editable
                    pre_check = None
                    try:
                        r = await bridge.execute_script(
                            "var el=document.activeElement;return el?"
                            "(el.tagName==='INPUT'||el.tagName==='TEXTAREA'||el.isContentEditable):false;"
                        )
                        if r.get("success") and r.get("data", {}).get("result") is False:
                            pre_check = "No editable element focused — click an input first."
                    except Exception:
                        pass
                    self.status_update.emit(f"⌨️  Step {step}: type \"{text[:40]}\"")
                    res_type = await bridge.type_text(text)
                    # 7. Retry with variation: if type failed, try script injection
                    if not res_type.get("success"):
                        try:
                            esc = text.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")
                            script = f"""var el=document.activeElement;
if(el&&(el.tagName==='INPUT'||el.tagName==='TEXTAREA'||el.isContentEditable)){{
  if(el.isContentEditable)el.textContent='{esc[:500]}';
  else el.value='{esc[:500]}';
  el.dispatchEvent(new InputEvent('input',{{bubbles:true}}));
  el.dispatchEvent(new Event('change',{{bubbles:true}}));
  return{{success:true}};
}}return{{success:false}};"""
                            inj = await bridge.execute_script(script)
                            if inj.get("success") and inj.get("data", {}).get("result", {}).get("success"):
                                res_type = {"success": True}
                        except Exception:
                            pass
                    if not res_type.get("success") and pre_check is None:
                        pre_check = res_type.get("error", "Type failed.")
                    action_history.append(f"type \"{text[:40]}\"")
                    action_outcome = f"Typed \"{text[:40]}\" into the focused element."
                    if pre_check:
                        action_outcome = f"{pre_check} " + action_outcome
                    await asyncio.sleep(1.2)
                    state_after_t = {}
                    try:
                        sa = await bridge.get_page_state()
                        if sa.get("success") and sa.get("data"):
                            state_after_t = sa["data"]
                            action_outcome += " " + _state_diff_summary(state_before_t, state_after_t).rstrip()
                    except Exception:
                        pass

                elif act_type == "navigate":
                    nav_url = action["url"]
                    state_before_n = {}
                    try:
                        sb = await bridge.get_page_state()
                        if sb.get("success") and sb.get("data"):
                            state_before_n = sb["data"]
                    except Exception:
                        pass
                    self.status_update.emit(f"🌐  Step {step}: navigate → {nav_url}")
                    await bridge.navigate(nav_url)
                    action_history.append(f"navigate → {nav_url}")
                    await asyncio.sleep(2.0)
                    state_after_n = {}
                    try:
                        sa = await bridge.get_page_state()
                        if sa.get("success") and sa.get("data"):
                            state_after_n = sa["data"]
                    except Exception:
                        pass
                    action_outcome = f"Navigated to {nav_url}. Page is loading. {_state_diff_summary(state_before_n, state_after_n)}"

                elif act_type == "scroll":
                    direction = action.get("direction", "down")
                    amount = int(action.get("amount", 300))
                    state_before_s = {}
                    try:
                        sb = await bridge.get_page_state()
                        if sb.get("success") and sb.get("data"):
                            state_before_s = sb["data"]
                    except Exception:
                        pass
                    self.status_update.emit(f"📜  Step {step}: scroll {direction}")
                    await bridge.scroll(direction, amount)
                    action_history.append(f"scroll {direction} {amount}px")
                    await asyncio.sleep(1.8)
                    state_after_s = {}
                    try:
                        sa = await bridge.get_page_state()
                        if sa.get("success") and sa.get("data"):
                            state_after_s = sa["data"]
                    except Exception:
                        pass
                    action_outcome = f"Scrolled {direction} by {amount}px. {_state_diff_summary(state_before_s, state_after_s)}"

                elif act_type == "select_all_and_type":
                    text = action["text"]
                    state_before_sat = {}
                    try:
                        sb = await bridge.get_page_state()
                        if sb.get("success") and sb.get("data"):
                            state_before_sat = sb["data"]
                    except Exception:
                        pass
                    self.status_update.emit(f"⌨️  Step {step}: select all & type \"{text[:40]}\"")
                    await bridge.select_all_and_type(text)
                    action_history.append(f"select_all_and_type \"{text[:40]}\"")
                    await asyncio.sleep(1.2)
                    state_after_sat = {}
                    try:
                        sa = await bridge.get_page_state()
                        if sa.get("success") and sa.get("data"):
                            state_after_sat = sa["data"]
                    except Exception:
                        pass
                    action_outcome = f"Selected all text in the focused field and typed \"{text[:40]}\". {_state_diff_summary(state_before_sat, state_after_sat)}"

                elif act_type == "press_key":
                    key = action["key"]
                    state_before_pk = {}
                    try:
                        sb = await bridge.get_page_state()
                        if sb.get("success") and sb.get("data"):
                            state_before_pk = sb["data"]
                    except Exception:
                        pass
                    self.status_update.emit(f"⌨️  Step {step}: press {key}")
                    await bridge.press_key(key)
                    action_history.append(f"press_key {key}")
                    last_press_key = key  # Track which key for screenshot refresh logic
                    if key == "Enter":
                        await asyncio.sleep(2.0)
                    else:
                        await asyncio.sleep(0.8)
                    state_after_pk = {}
                    try:
                        sa = await bridge.get_page_state()
                        if sa.get("success") and sa.get("data"):
                            state_after_pk = sa["data"]
                    except Exception:
                        pass
                    action_outcome = f"Pressed key: {key}. {_state_diff_summary(state_before_pk, state_after_pk)}"

                elif act_type == "wait":
                    # Model shouldn't use "wait" (removed from prompt), but
                    # handle gracefully: treat as a short pause and log it.
                    self.status_update.emit(
                        f"⏳  Step {step}: model requested wait (discouraged)"
                    )
                    await asyncio.sleep(2)
                    action_history.append("wait 2s (auto-discouraged)")
                    action_outcome = "Waited 2s. Note: 'wait' is not a valid action. The page is already loaded. Take a real action."

                else:
                    self.status_update.emit(
                        f"⚠️  Step {step}: unknown action '{act_type}'"
                    )
                    action_history.append(f"UNKNOWN: {act_type}")
                    action_outcome = f"Unknown action '{act_type}' was ignored."
                    await asyncio.sleep(1.0)

                # Track last action type for screenshot refresh logic
                last_action_type = act_type
                if act_type != "press_key":
                    last_press_key = ""  # Clear key tracking for non-key actions

                # Append a system-like feedback so the model knows what happened
                if action_outcome:
                    if low_confidence:
                        action_outcome += " [Low confidence — consider alternative approaches.]"
                    conversation.append({
                        "role": "user",
                        "content": f"[System feedback] {action_outcome}",
                    })

                # ── 7. Extreme loops — force different approach (never give up) ─
                # 13. Human-in-the-loop only when stuck: emit stuck_need_human
                if len(action_signatures) >= 5 and len(set(action_signatures[-5:])) == 1:
                    stuck_action = action_signatures[-1]
                    self.stuck_need_human.emit(
                        f"Agent repeating '{stuck_action}' 5 times — consider taking over."
                    )
                    self.status_update.emit(
                        f"🔄  Repeated '{stuck_action}' 5 times — trying to force progress…"
                    )
                    # Try forcing a "done" answer first (task might be complete)
                    force_prompt = (
                        f"Task: {self.task}\n\n"
                        f"Current URL: {url}\n\n"
                        f"You have been repeating '{stuck_action}' without progress. "
                        f"The page is fully loaded. Based on what you can see in the "
                        f"screenshot, provide your answer now.\n\n"
                        f"If the task is complete, respond with: "
                        f'{{\"reasoning\":\"...\",\"action\":\"done\",\"result\":\"...\"}}\n\n'
                        f"If NOT complete, respond with a COMPLETELY DIFFERENT action "
                        f"(do NOT repeat {stuck_action})."
                    )
                    force_messages = [
                        {"role": "system", "content": _EXTENSION_SYSTEM_PROMPT},
                        {"role": "user", "content": force_prompt, "images": [raw_b64]},
                    ]
                    full_text = await _chat_stream(force_messages)
                    self.token_stream.emit("\n")

                    forced = _parse_action(full_text)
                    if forced and forced.get("action") == "done":
                        result_text = forced.get("result", "Task completed (forced).")
                        self.status_update.emit(f"✅  {result_text}")
                        self.task_finished.emit(result_text)
                        return
                    # Forced done failed — add strong nudge and CONTINUE (never give up)
                    conversation.append({
                        "role": "user",
                        "content": (
                            f"[System] You must try a COMPLETELY different action. "
                            f"NEVER repeat '{stuck_action}' again. It did not work. "
                            f"Try a different element, different URL, or different strategy."
                        ),
                    })
                    continue

            except ConnectionError as e:
                # ── Transient disconnection — wait for reconnect ──────────
                consecutive_failures += 1
                self.status_update.emit(
                    f"🔌  Step {step}: extension disconnected — "
                    f"waiting for reconnect… (attempt {consecutive_failures}/5)"
                )
                if consecutive_failures >= 5:
                    self.error_occurred.emit(
                        f"Extension disconnected {consecutive_failures} times. "
                        f"Last error: {e}\n\n"
                        f"Make sure the extension is installed and Chrome is open."
                    )
                    return

                # Wait for reconnection before retrying the step
                try:
                    reconnected = await bridge.wait_for_connection(timeout=15.0)
                    if reconnected:
                        self.status_update.emit(
                            f"🟢  Extension reconnected — retrying step {step}"
                        )
                        await asyncio.sleep(1.0)
                        # Don't increment step — retry from the same step number
                        # (we continue the for-loop so step increments; compensate
                        #  by not appending to history so context stays the same)
                        continue
                    else:
                        self.error_occurred.emit(
                            f"Extension did not reconnect within 15 seconds.\n{e}"
                        )
                        return
                except Exception as wait_err:
                    self.error_occurred.emit(
                        f"Error waiting for extension reconnect: {wait_err}"
                    )
                    return

            except Exception as e:
                self.status_update.emit(f"⚠️  Step {step} error: {e}")
                action_history.append(f"ERROR: {e}")
                consecutive_failures += 1
                if consecutive_failures >= 15:
                    self.error_occurred.emit(
                        f"15 consecutive errors. Last: {e}"
                    )
                    return
                # Back off before retrying to avoid rate-limit cascades
                await asyncio.sleep(2.0 * consecutive_failures)

        # Exhausted all steps (tried many approaches)
        self.error_occurred.emit(
            f"Reached maximum {EXT_MAX_STEPS} steps without completing the task. "
            f"The agent tried many different approaches. You can run the task again "
            f"to continue from where it left off, or try rephrasing the task."
        )
