"""
server.py — MyBot Headless API Server
================================================
REST/SSE server for running browser automation without the PyQt6 dock.
Use for: Android clients, free VPS hosting, headless deployment.

Created by AJ (Ahmed Jouini).

Usage:
  python server.py                    # Run on http://0.0.0.0:8765
  python server.py --port 9000        # Custom port
  QT_QPA_PLATFORM=offscreen python server.py   # Headless Linux (optional)

Endpoints:
  POST /task     — Run a task (JSON: {task, model_name?, provider?, ...})
  GET  /task/stream  — SSE stream for a running task (experimental)
  GET  /health   — Health check
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import sys
import threading
from dataclasses import dataclass, field
from typing import Any

# Headless Qt for Linux servers (skip on Windows to avoid display issues)
if sys.platform.startswith("linux") and "QT_QPA_PLATFORM" not in os.environ:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

log = logging.getLogger(__name__)

# ── Optional FastAPI ──────────────────────────────────────────────────────

try:
    from fastapi import FastAPI, HTTPException, BackgroundTasks
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import StreamingResponse
    from pydantic import BaseModel
    HAS_FASTAPI = True
except ImportError:
    HAS_FASTAPI = False


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║                        REQUEST / RESPONSE MODELS                         ║
# ╚══════════════════════════════════════════════════════════════════════════╗

if HAS_FASTAPI:

    class TaskRequest(BaseModel):
        task: str
        model_name: str = "minicpm-v"
        provider: str = "ollama"
        opencode_api_key: str = ""
        openrouter_api_key: str = ""
        google_api_key: str = ""
        groq_api_key: str = ""
        use_playbooks: bool = True
        screen_width: int = 1920
        screen_height: int = 1080
        use_extension: bool = False  # Extension mode needs extension bridge on same host

    class TaskResponse(BaseModel):
        success: bool
        result: str | None = None
        error: str | None = None
        status_log: list[str] = field(default_factory=list)


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║                           TASK RUNNER                                    ║
# ╚══════════════════════════════════════════════════════════════════════════╗

@dataclass
class TaskResult:
    success: bool
    result: str | None = None
    error: str | None = None
    status_log: list[str] = field(default_factory=list)


def _run_task_sync(
    task: str,
    model_name: str = "minicpm-v",
    provider: str = "ollama",
    opencode_api_key: str = "",
    openrouter_api_key: str = "",
    google_api_key: str = "",
    groq_api_key: str = "",
    use_playbooks: bool = True,
    screen_width: int = 1920,
    screen_height: int = 1080,
    use_extension: bool = False,
) -> TaskResult:
    """
    Run an agent task synchronously. Must be called from a thread that has
    a QApplication running (the server ensures this).
    """
    from PyQt6.QtWidgets import QApplication
    from agent_logic import (
        AgentWorker,
        PROVIDER_OLLAMA,
        PROVIDER_OPENCODE,
        PROVIDER_OPENROUTER,
        PROVIDER_GOOGLE,
        PROVIDER_GROQ,
    )
    from extension_bridge import ExtensionBridge, DEFAULT_WS_PORT

    provider_map = {
        "ollama": PROVIDER_OLLAMA,
        "opencode": PROVIDER_OPENCODE,
        "openrouter": PROVIDER_OPENROUTER,
        "google": PROVIDER_GOOGLE,
        "groq": PROVIDER_GROQ,
    }
    prov = provider_map.get(provider.lower(), PROVIDER_OLLAMA)

    status_log: list[str] = []
    result_holder: list[str | None] = [None]
    error_holder: list[str | None] = [None]

    bridge = None
    if use_extension:
        bridge = ExtensionBridge(port=DEFAULT_WS_PORT)
        bridge.start()
        # Give extension a moment to connect
        import time
        time.sleep(1)

    worker = AgentWorker(
        task=task,
        screen_width=screen_width,
        screen_height=screen_height,
        model_name=model_name,
        use_extension=use_extension,
        extension_bridge=bridge,
        use_playbooks=use_playbooks,
        custom_guidelines="",
        provider=prov,
        opencode_api_key=opencode_api_key,
        openrouter_api_key=openrouter_api_key,
        google_api_key=google_api_key,
        groq_api_key=groq_api_key,
    )

    def on_status(msg: str):
        status_log.append(msg)
        log.info("Agent: %s", msg[:80])

    def on_finished(result: str):
        result_holder[0] = result
        status_log.append(f"✅ {result}")

    def on_error(err: str):
        error_holder[0] = err
        status_log.append(f"❌ {err}")
        log.error("Agent error: %s", err)

    worker.status_update.connect(on_status)
    worker.task_finished.connect(on_finished)
    worker.error_occurred.connect(on_error)

    worker.start()
    worker.wait()

    if bridge:
        bridge.stop()

    if error_holder[0]:
        return TaskResult(success=False, error=error_holder[0], status_log=status_log)
    return TaskResult(
        success=True,
        result=result_holder[0] or "Task completed.",
        status_log=status_log,
    )


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║                          FASTAPI APP                                     ║
# ╚══════════════════════════════════════════════════════════════════════════╗

def create_app():
    if not HAS_FASTAPI:
        raise RuntimeError("Install FastAPI and uvicorn: pip install fastapi uvicorn")

    app = FastAPI(
        title="MyBot API",
        description="Headless browser automation via AI",
        version="1.0.0",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Ensure QApplication exists (required for AgentWorker)
    _qt_app = None

    def ensure_qt():
        nonlocal _qt_app
        if _qt_app is None:
            from PyQt6.QtWidgets import QApplication
            _qt_app = QApplication.instance() or QApplication([])
        return _qt_app

    @app.on_event("startup")
    async def startup():
        ensure_qt()
        log.info("MyBot server ready")

    @app.get("/health")
    async def health():
        return {"status": "ok", "service": "ai-browser"}

    @app.post("/task", response_model=TaskResponse)
    async def run_task(req: TaskRequest, background_tasks: BackgroundTasks):
        """Run a browser automation task. Blocks until complete."""
        ensure_qt()
        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(
                None,
                lambda: _run_task_sync(
                    task=req.task,
                    model_name=req.model_name,
                    provider=req.provider,
                    opencode_api_key=req.opencode_api_key,
                    openrouter_api_key=req.openrouter_api_key,
                    google_api_key=req.google_api_key,
                    groq_api_key=req.groq_api_key,
                    use_playbooks=req.use_playbooks,
                    screen_width=req.screen_width,
                    screen_height=req.screen_height,
                    use_extension=req.use_extension,
                ),
            )
            return TaskResponse(
                success=result.success,
                result=result.result,
                error=result.error,
                status_log=result.status_log,
            )
        except Exception as e:
            log.exception("Task failed")
            raise HTTPException(status_code=500, detail=str(e))

    @app.get("/task/stream")
    async def stream_task(
        task: str,
        model_name: str = "minicpm-v",
        provider: str = "ollama",
        opencode_api_key: str = "",
        openrouter_api_key: str = "",
        google_api_key: str = "",
        groq_api_key: str = "",
        use_playbooks: bool = True,
    ):
        """
        Run a task and stream status updates as Server-Sent Events.
        Use when you want real-time progress (e.g. from a web client).
        """
        ensure_qt()

        msg_queue: queue.Queue[str | None] = queue.Queue()

        def run_and_queue():
            from agent_logic import (
                AgentWorker,
                PROVIDER_OLLAMA,
                PROVIDER_OPENCODE,
                PROVIDER_OPENROUTER,
                PROVIDER_GOOGLE,
                PROVIDER_GROQ,
            )
            provider_map = {
                "ollama": PROVIDER_OLLAMA,
                "opencode": PROVIDER_OPENCODE,
                "openrouter": PROVIDER_OPENROUTER,
                "google": PROVIDER_GOOGLE,
                "groq": PROVIDER_GROQ,
            }
            prov = provider_map.get(provider.lower(), PROVIDER_OLLAMA)

            status_log: list[str] = []

            def on_status(m):
                status_log.append(m)
                msg_queue.put(f"data: {json.dumps({'type': 'status', 'msg': m})}\n\n")

            def on_finished(r):
                msg_queue.put(f"data: {json.dumps({'type': 'done', 'result': r})}\n\n")
                msg_queue.put(None)

            def on_error(e):
                msg_queue.put(f"data: {json.dumps({'type': 'error', 'error': e})}\n\n")
                msg_queue.put(None)

            worker = AgentWorker(
                task=task,
                model_name=model_name,
                use_extension=False,
                extension_bridge=None,
                use_playbooks=use_playbooks,
                provider=prov,
                opencode_api_key=opencode_api_key or "",
                openrouter_api_key=openrouter_api_key or "",
                google_api_key=google_api_key or "",
                groq_api_key=groq_api_key or "",
            )
            worker.status_update.connect(on_status)
            worker.task_finished.connect(on_finished)
            worker.error_occurred.connect(on_error)
            worker.start()
            worker.wait()

            if not msg_queue.queue or msg_queue.queue[-1] != None:
                msg_queue.put(None)

        def generate():
            thread = threading.Thread(target=run_and_queue, daemon=True)
            thread.start()
            while True:
                msg = msg_queue.get()
                if msg is None:
                    break
                yield msg

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║                            ENTRY POINT                                   ║
# ╚══════════════════════════════════════════════════════════════════════════╗

def main():
    import argparse
    parser = argparse.ArgumentParser(description="MyBot headless API server")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host")
    parser.add_argument("--port", type=int, default=8765, help="Bind port")
    parser.add_argument("--reload", action="store_true", help="Enable hot reload")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if not HAS_FASTAPI:
        print("Install FastAPI and uvicorn: pip install fastapi uvicorn", file=sys.stderr)
        sys.exit(1)

    import uvicorn
    app = create_app()
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        reload=args.reload,
    )


if __name__ == "__main__":
    main()
