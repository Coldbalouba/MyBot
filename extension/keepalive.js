/**
 * keepalive.js — Content script that keeps the MV3 service worker alive.
 * ======================================================================
 * Manifest V3 service workers can be terminated at ANY time by Chrome,
 * even mid-WebSocket-message.  However, Chrome keeps the service worker
 * alive as long as there is an **open extension port** from a content script.
 *
 * This content script opens a port to the background and automatically
 * reconnects if it drops (e.g. after a service worker restart).
 *
 * NOTE: This script may be injected MULTIPLE times into the same page
 * (once by the manifest, once by injectKeepaliveIntoAllTabs).  The guard
 * variable prevents duplicate ports.
 */

(() => {
  // Guard against duplicate injection
  if (window.__aiBridgeKeepaliveActive) return;
  window.__aiBridgeKeepaliveActive = true;

  const PORT_NAME = "keepalive";
  const MAX_RETRIES = 50;          // give up after this many consecutive failures
  const BASE_DELAY_MS = 1000;      // start with 1s
  const MAX_DELAY_MS = 30000;      // cap at 30s

  let port = null;
  let reconnectTimer = null;
  let retries = 0;

  function connectPort() {
    if (reconnectTimer) {
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }

    try {
      port = chrome.runtime.connect({ name: PORT_NAME });
      retries = 0; // reset on successful connect

      port.onDisconnect.addListener(() => {
        port = null;
        // Check for extension context invalidation (extension was reloaded)
        const lastError = chrome.runtime.lastError;
        if (lastError && String(lastError.message || "").includes("Extension context invalidated")) {
          // Extension was reloaded — stop trying, the new extension will
          // inject a fresh copy of this script.
          window.__aiBridgeKeepaliveActive = false;
          return;
        }
        // Normal disconnect (service worker died) — reconnect with backoff
        scheduleReconnect();
      });
    } catch (e) {
      port = null;
      const errMsg = String(e);
      if (errMsg.includes("Extension context invalidated")) {
        // Extension was reloaded — give up
        window.__aiBridgeKeepaliveActive = false;
        return;
      }
      // Service worker is temporarily unavailable — retry with backoff
      scheduleReconnect();
    }
  }

  function scheduleReconnect() {
    retries++;
    if (retries > MAX_RETRIES) {
      // Too many failures — stop. A fresh injection from
      // injectKeepaliveIntoAllTabs will restart us if needed.
      window.__aiBridgeKeepaliveActive = false;
      return;
    }
    // Exponential backoff with jitter
    const delay = Math.min(BASE_DELAY_MS * Math.pow(1.5, retries - 1), MAX_DELAY_MS);
    const jitter = delay * 0.2 * Math.random();
    reconnectTimer = setTimeout(connectPort, delay + jitter);
  }

  connectPort();
})();
