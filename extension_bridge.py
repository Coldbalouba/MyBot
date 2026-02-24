"""
extension_bridge.py — Chrome Extension WebSocket Bridge
========================================================
Runs a lightweight WebSocket server that the AI Browser Bridge Chrome
extension connects to.  Provides a thread-safe async API so any thread
(e.g. the AgentWorker) can send commands to Chrome and await results.

Architecture
------------
  Python dock app                    Chrome extension
  ┌────────────────┐   WebSocket    ┌───────────────┐
  │ ExtensionBridge ├──ws://localhost:7865──►│ background.js │
  │  (own thread)   │◄──────────────┤               │
  └────────────────┘   JSON msgs    └───────────────┘

The bridge runs its own asyncio event loop in a daemon thread.
Other threads call the ``send_*`` helpers which hop across via
``asyncio.run_coroutine_threadsafe`` + ``asyncio.wrap_future``.

Resilience
----------
MV3 service workers can be killed at ANY moment by Chrome.  To cope:
  • ``_send_command`` will wait for the extension to reconnect and retry
    automatically (up to ``RECONNECT_WAIT`` seconds).
  • The bridge ignores ``ws_ping`` keepalive messages from the extension.
  • ``wait_for_connection()`` is available for callers who want to block
    until the extension is back.
"""

from __future__ import annotations

import base64
import atexit
import asyncio
import io
import json
import logging
import socket
import subprocess
import sys
import threading
import time
import uuid
from typing import Any, Callable

log = logging.getLogger(__name__)

# Full-page screenshot dimensions (match extension)
_FULL_PAGE_WIDTH = 1200
_FULL_PAGE_MAX_HEIGHT = 6000


def _stitch_full_page_to_data_url(data: dict) -> str:
    """Stitch extension full-page strips into one image; return data URL."""
    try:
        from PIL import Image
    except ImportError:
        log.warning("Pillow required for full-page stitch — using first strip only")
        strips = data.get("screenshots") or []
        return strips[0] if strips else "data:image/png;base64,"

    screenshots = data.get("screenshots") or []
    if not screenshots:
        return "data:image/png;base64,"
    scroll_width = max(int(data.get("scrollWidth", 1)), 1)
    scroll_height = max(int(data.get("scrollHeight", 1)), 1)
    vw = max(int(data.get("viewportWidth", 1)), 1)
    vh = max(int(data.get("viewportHeight", 1)), 1)
    out_width = _FULL_PAGE_WIDTH
    out_height = min(
        int(round(scroll_height * out_width / scroll_width)),
        _FULL_PAGE_MAX_HEIGHT,
    )
    strip_height_out = max(1, int(round(vh * out_height / scroll_height)))
    canvas = Image.new("RGB", (out_width, out_height), (255, 255, 255))
    y = 0
    for i, data_url in enumerate(screenshots):
        raw_b64 = data_url.split(",", 1)[1] if "," in data_url else data_url
        try:
            img_bytes = base64.b64decode(raw_b64)
            strip = Image.open(io.BytesIO(img_bytes)).convert("RGB")
        except Exception as e:
            log.warning("Stitch strip %d failed: %s", i, e)
            continue
        target_h = min(strip_height_out, out_height - y)
        if target_h <= 0:
            break
        strip_resized = strip.resize((out_width, target_h), Image.LANCZOS)
        canvas.paste(strip_resized, (0, y))
        y += target_h
        if y >= out_height:
            break
    buf = io.BytesIO()
    canvas.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{b64}"

# ── Constants ──────────────────────────────────────────────────────────────────

DEFAULT_WS_PORT = 7865
COMMAND_TIMEOUT = 30.0        # seconds — max wait for a single command response
RECONNECT_WAIT = 10.0         # seconds — how long _send_command waits for reconnect
RECONNECT_POLL = 0.25         # seconds — poll interval while waiting
SEND_RETRIES = 3              # total attempts per command (incl. first)
BIND_RETRY_ATTEMPTS = 4
BIND_RETRY_DELAY = 1.5        # seconds


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║                       PORT CLEANUP (Windows-aware)                          ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

def _is_port_in_use(port: int) -> bool:
    """Quick check whether *port* on localhost is already bound."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return False
        except OSError:
            return True


def _kill_stale_port(port: int) -> bool:
    """
    On Windows, find the PID listening on *port* and kill it.
    Returns True if something was killed.
    """
    if sys.platform != "win32":
        return False
    try:
        out = subprocess.check_output(
            ["netstat", "-ano", "-p", "TCP"],
            text=True, timeout=5,
        )
        for line in out.splitlines():
            # Look for LISTENING lines on our port
            if f":{port}" in line and "LISTENING" in line:
                parts = line.split()
                pid = int(parts[-1])
                if pid > 0:
                    log.warning(
                        "Killing stale process PID %d holding port %d", pid, port,
                    )
                    subprocess.run(
                        ["taskkill", "/PID", str(pid), "/F"],
                        capture_output=True, timeout=5,
                    )
                    time.sleep(0.5)
                    return True
    except Exception as e:
        log.debug("_kill_stale_port failed: %s", e)
    return False


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║                        EXTENSION BRIDGE                                     ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

class ExtensionBridge:
    """
    WebSocket server that the Chrome extension connects to.

    Lifecycle::

        bridge = ExtensionBridge(port=7865)
        bridge.start()            # spawns daemon thread
        ...
        await bridge.screenshot() # from any async context — auto-retries
        ...
        bridge.stop()             # clean shutdown
    """

    def __init__(
        self,
        port: int = DEFAULT_WS_PORT,
        on_connect: Callable[[], Any] | None = None,
        on_disconnect: Callable[[], Any] | None = None,
    ):
        self.port = port
        self._on_connect = on_connect
        self._on_disconnect = on_disconnect

        # Internal state (only touched from the bridge's own event loop)
        self._ws = None
        self._server = None
        self._pending: dict[str, asyncio.Future] = {}
        self._connected = False

        # Reconnection event — set whenever extension connects, cleared on disconnect
        self._connected_event: asyncio.Event | None = None  # created on bridge loop

        # Thread + loop
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._started = threading.Event()  # set once the server is listening
        self._start_error: str | None = None

    # ── Public properties ─────────────────────────────────────────────────

    @property
    def connected(self) -> bool:
        return self._connected and self._ws is not None

    # ── Start / stop ──────────────────────────────────────────────────────

    def start(self):
        """Spin up the WebSocket server in a background daemon thread."""
        if self._thread and self._thread.is_alive():
            return

        # Free the port if a zombie from a previous run is holding it
        if _is_port_in_use(self.port):
            log.warning("Port %d already in use — attempting cleanup…", self.port)
            _kill_stale_port(self.port)

        self._started.clear()
        self._start_error = None
        self._thread = threading.Thread(target=self._run, daemon=True, name="ExtBridge")
        self._thread.start()

        # Register atexit so the server always gets cleaned up
        atexit.register(self.stop)

        # Wait up to a few seconds for the server to actually bind
        if not self._started.wait(timeout=8):
            log.error("ExtensionBridge failed to start within timeout")

        if self._start_error:
            log.error("ExtensionBridge start error: %s", self._start_error)

    def stop(self):
        """Request a clean shutdown — close server socket, stop loop, join thread."""
        # Close the server socket first so the port is freed immediately
        if self._server is not None and self._loop and self._loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(
                    self._shutdown_server(), self._loop,
                ).result(timeout=3)
            except Exception:
                pass

        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)

        self._server = None
        self._ws = None
        self._connected = False

    async def _shutdown_server(self):
        """Gracefully close the websocket server."""
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    def _run(self):
        """Thread entry — creates an event loop and runs the server forever."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        # Create the reconnection event on this loop
        self._connected_event = asyncio.Event()
        try:
            self._loop.run_until_complete(self._start_server())
            self._loop.run_forever()
        except Exception as e:
            self._start_error = str(e)
            log.error("ExtensionBridge loop crashed: %s", e)
        finally:
            # Make sure we close the server socket even on crash
            if self._server:
                try:
                    self._server.close()
                except Exception:
                    pass
            try:
                self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            except Exception:
                pass
            self._loop.close()
            self._started.set()  # unblock anyone waiting

    async def _start_server(self):
        """Bind the WebSocket server with retry logic."""
        import websockets

        last_err = None
        for attempt in range(1, BIND_RETRY_ATTEMPTS + 1):
            try:
                self._server = await websockets.serve(
                    self._handler,
                    "127.0.0.1",
                    self.port,
                    # Screenshots can be 2-5 MB as PNG — need a generous limit
                    max_size=20 * 1024 * 1024,  # 20 MB
                    # Disable library-level pings — we rely on our own ws_ping messages
                    ping_interval=None,
                    ping_timeout=None,
                )
                log.info("Extension bridge listening on ws://127.0.0.1:%d", self.port)
                self._started.set()
                return
            except OSError as e:
                last_err = e
                log.warning(
                    "Bind attempt %d/%d failed (port %d): %s",
                    attempt, BIND_RETRY_ATTEMPTS, self.port, e,
                )
                # Try harder to free the port
                _kill_stale_port(self.port)
                await asyncio.sleep(BIND_RETRY_DELAY)

        # All attempts exhausted
        self._start_error = str(last_err)
        self._started.set()
        raise OSError(
            f"Could not bind to port {self.port} after {BIND_RETRY_ATTEMPTS} "
            f"attempts: {last_err}"
        )

    async def _handler(self, websocket):
        """Handle a single WebSocket connection (one extension at a time)."""
        # Accept the new connection (replaces any previous one)
        self._ws = websocket
        self._connected = True
        if self._connected_event:
            self._connected_event.set()
        log.info("Chrome extension connected")
        if self._on_connect:
            try:
                self._on_connect()
            except Exception:
                pass

        try:
            async for raw in websocket:
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                # Extension "connected" announcement — informational
                if data.get("type") == "connected":
                    log.info("Extension hello: %s", data)
                    continue

                # WebSocket keepalive ping from extension — ignore
                if data.get("type") == "ws_ping":
                    continue

                # Response to a pending command
                msg_id = data.get("id")
                if msg_id and msg_id in self._pending:
                    fut = self._pending.pop(msg_id)
                    if not fut.done():
                        fut.set_result(data)
        except Exception as e:
            log.warning("Extension WebSocket closed: %s", e)
        finally:
            self._connected = False
            self._ws = None
            if self._connected_event:
                self._connected_event.clear()
            # Cancel any pending futures so callers don't hang
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("Extension disconnected"))
            self._pending.clear()
            log.info("Chrome extension disconnected")
            if self._on_disconnect:
                try:
                    self._on_disconnect()
                except Exception:
                    pass

    # ── Low-level send (runs on bridge loop) ──────────────────────────────

    async def _send_command(
        self, command: str, params: dict | None = None, timeout: float = COMMAND_TIMEOUT,
    ) -> dict:
        """
        Send a command and wait for the response.  Must be called on the bridge loop.

        If the extension is disconnected, waits up to ``RECONNECT_WAIT`` seconds
        for it to reconnect, then retries.  This covers the common MV3 scenario
        where Chrome kills the service worker mid-task.
        """
        last_err: Exception | None = None

        for attempt in range(1, SEND_RETRIES + 1):
            # ── Wait for connection if needed ─────────────────────────────
            if not self._connected or self._ws is None:
                if attempt == 1:
                    log.info(
                        "Extension not connected — waiting up to %.0fs for reconnect…",
                        RECONNECT_WAIT,
                    )
                if self._connected_event:
                    try:
                        await asyncio.wait_for(
                            self._connected_event.wait(),
                            timeout=RECONNECT_WAIT,
                        )
                    except asyncio.TimeoutError:
                        raise ConnectionError(
                            f"Extension did not reconnect within {RECONNECT_WAIT}s"
                        )
                else:
                    raise ConnectionError("Extension not connected and no event loop")

                # Small delay after reconnect to let the extension settle
                await asyncio.sleep(0.3)

            # ── Double-check still connected after wait ───────────────────
            if not self._connected or self._ws is None:
                last_err = ConnectionError("Extension disconnected after wait")
                continue

            # ── Send the command ──────────────────────────────────────────
            msg_id = uuid.uuid4().hex[:12]
            msg: dict[str, Any] = {"id": msg_id, "command": command}
            if params:
                msg["params"] = params

            fut = self._loop.create_future()
            self._pending[msg_id] = fut

            try:
                await self._ws.send(json.dumps(msg))
                result = await asyncio.wait_for(fut, timeout=timeout)
                return result
            except asyncio.TimeoutError:
                self._pending.pop(msg_id, None)
                raise TimeoutError(
                    f"Extension did not respond to '{command}' within {timeout}s"
                )
            except ConnectionError:
                self._pending.pop(msg_id, None)
                last_err = ConnectionError(
                    f"Extension disconnected during '{command}' (attempt {attempt})"
                )
                log.warning(
                    "Extension disconnected during '%s' — attempt %d/%d, waiting for reconnect…",
                    command, attempt, SEND_RETRIES,
                )
                # Clear the event so we properly wait for the NEXT connection
                if self._connected_event:
                    self._connected_event.clear()
                continue
            except Exception as e:
                self._pending.pop(msg_id, None)
                last_err = e
                log.warning(
                    "Send '%s' error (attempt %d/%d): %s",
                    command, attempt, SEND_RETRIES, e,
                )
                if self._connected_event:
                    self._connected_event.clear()
                continue

        # All retries exhausted
        raise last_err or ConnectionError("Extension command failed after retries")

    # ── Thread-safe async API (callable from any event loop) ──────────────
    #
    # Call these with ``await`` from the AgentWorker's own event loop.
    # They hop the command to the bridge's loop and bring the result back.

    async def send(
        self, command: str, params: dict | None = None, timeout: float = COMMAND_TIMEOUT,
    ) -> dict:
        """Thread-safe async command. Await from *any* event loop."""
        if self._loop is None:
            raise RuntimeError("Bridge not started")
        concurrent_fut = asyncio.run_coroutine_threadsafe(
            self._send_command(command, params, timeout),
            self._loop,
        )
        return await asyncio.wrap_future(concurrent_fut)

    # ── Wait for connection (thread-safe) ─────────────────────────────────

    async def wait_for_connection(self, timeout: float = RECONNECT_WAIT) -> bool:
        """
        Wait until the extension connects (or is already connected).
        Returns True if connected, False on timeout.
        Callable from any event loop.
        """
        if self.connected:
            return True
        if self._loop is None:
            return False

        async def _wait():
            if self._connected_event:
                try:
                    await asyncio.wait_for(self._connected_event.wait(), timeout=timeout)
                    return True
                except asyncio.TimeoutError:
                    return False
            return False

        concurrent_fut = asyncio.run_coroutine_threadsafe(_wait(), self._loop)
        return await asyncio.wrap_future(concurrent_fut)

    # ── Convenience methods ───────────────────────────────────────────────

    async def screenshot(self, timeout: float = 90.0) -> str:
        """
        Capture full-page tab and return a single data-URL.
        Extension returns strips; we stitch to fixed 1200px width, height capped at 6000.
        """
        result = await self.send("screenshot", timeout=timeout)
        if not result.get("success"):
            raise RuntimeError(f"Screenshot failed: {result.get('error')}")
        data = result.get("data", {})
        if data.get("fullPage") and data.get("screenshots"):
            return _stitch_full_page_to_data_url(data)
        return data.get("screenshot", "")

    async def navigate(self, url: str) -> dict:
        return await self.send("navigate", {"url": url})

    async def click(self, x: int, y: int) -> dict:
        return await self.send("click", {"x": x, "y": y})

    async def type_text(self, text: str) -> dict:
        return await self.send("type", {"text": text})

    async def press_key(self, key: str) -> dict:
        return await self.send("press_key", {"key": key})

    async def select_all_and_type(self, text: str) -> dict:
        return await self.send("select_all_and_type", {"text": text})

    async def scroll(self, direction: str = "down", amount: int = 300) -> dict:
        return await self.send("scroll", {"direction": direction, "amount": amount})

    async def get_url(self) -> str:
        result = await self.send("get_url")
        if result.get("success"):
            return result["data"]["url"]
        return ""

    async def get_page_state(self) -> dict:
        """Return {url, domHash, focusedTag, focusedValue} for state diff."""
        return await self.send("get_page_state")

    async def execute_script(self, script: str) -> dict:
        """Execute JS in the page. Returns {success, data: {result}} or {success: False, error}."""
        return await self.send("execute_script", {"script": script})

    async def click_by_text(self, text: str, tag: str = "*") -> dict:
        """Click element by visible text. Fallback for coordinate click."""
        return await self.send("click_by_text", {"text": text, "tag": tag})

    async def element_at(self, x: int, y: int) -> dict:
        """Check if element exists at image coords. Scrolls into view."""
        return await self.send("element_at", {"x": x, "y": y})

    async def get_dom(self) -> dict:
        return await self.send("get_dom")

    async def get_tabs(self) -> dict:
        return await self.send("get_tabs")

    async def switch_tab(self, tab_id: int) -> dict:
        return await self.send("switch_tab", {"tabId": tab_id})

    async def wait_stable(self, quiet_ms: int = 750, timeout_ms: int = 8000) -> dict:
        """
        Wait until the active page has finished loading and the DOM has been
        quiet (no mutations) for *quiet_ms* milliseconds.

        Returns the result dict which includes ``data.waited`` (ms actually waited)
        and ``data.reason`` (why we stopped waiting).  Never raises on timeout —
        it just returns with reason ``timeout_mutations`` or ``timeout_readyState``.
        """
        return await self.send(
            "wait_stable",
            {"quietMs": quiet_ms, "timeoutMs": timeout_ms},
            timeout=max(timeout_ms / 1000 + 5, COMMAND_TIMEOUT),
        )

    async def ping(self) -> bool:
        """Quick connectivity check."""
        try:
            result = await self.send("ping", timeout=5.0)
            return result.get("success", False)
        except Exception:
            return False
