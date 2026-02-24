/**
 * background.js — MyBot Bridge Service Worker
 * ==================================================
 * Maintains a WebSocket connection to the Python dock app and
 * executes browser-automation commands on its behalf.
 *
 * MV3 Service Worker Survival Strategy:
 *   1. On install/startup, inject keepalive.js into ALL existing tabs
 *      so they maintain long-lived ports to this worker.
 *   2. A self-keepalive timer runs every 20s while the WS is connected,
 *      calling a Chrome API to prevent the 30s idle timeout.
 *   3. chrome.alarms fire every ~25s as a fallback safety net.
 *   4. Content-script keepalive ports (keepalive.js) provide the primary
 *      mechanism once pages have the script loaded.
 *   5. Fast reconnect (1s) with backoff minimises downtime.
 */

// ── Configuration ────────────────────────────────────────────────────────────

const WS_URL = "ws://localhost:7865";
const RECONNECT_DELAY_MS = 1000;          // initial reconnect delay
const RECONNECT_MAX_DELAY_MS = 5000;      // max backoff
const KEEPALIVE_ALARM = "ai-bridge-keepalive";
const WS_PING_INTERVAL_MS = 15_000;       // send ping every 15s to detect dead sockets
const SELF_KEEPALIVE_MS = 20_000;          // Chrome API call every 20s to prevent idle kill

// ── State ────────────────────────────────────────────────────────────────────

let ws = null;
let wsConnected = false;
let pingTimer = null;
let selfKeepAliveTimer = null;
let reconnectDelay = RECONNECT_DELAY_MS;
/** @type {Set<chrome.runtime.Port>} */
const keepalivePorts = new Set();

// ── Keepalive port handling (from content scripts) ───────────────────────────

chrome.runtime.onConnect.addListener((port) => {
  if (port.name === "keepalive") {
    keepalivePorts.add(port);
    port.onDisconnect.addListener(() => {
      keepalivePorts.delete(port);
    });
  }
});

// ── Self-keepalive (prevents 30s idle termination while WS is active) ────────

function startSelfKeepAlive() {
  stopSelfKeepAlive();
  // Calling any chrome.* API that returns a promise counts as "activity"
  // and resets Chrome's idle timer for the service worker.
  selfKeepAliveTimer = setInterval(() => {
    chrome.runtime.getPlatformInfo().then(() => {});
  }, SELF_KEEPALIVE_MS);
}

function stopSelfKeepAlive() {
  if (selfKeepAliveTimer) {
    clearInterval(selfKeepAliveTimer);
    selfKeepAliveTimer = null;
  }
}

// ── Inject keepalive.js into all existing tabs ───────────────────────────────
// After extension reload, existing tabs have orphaned content scripts.
// We must re-inject keepalive.js so they create fresh ports.

async function injectKeepaliveIntoAllTabs() {
  try {
    const tabs = await chrome.tabs.query({});
    for (const tab of tabs) {
      // Skip un-scriptable pages
      if (!tab.url) continue;
      if (tab.url.startsWith("chrome://") ||
          tab.url.startsWith("chrome-extension://") ||
          tab.url.startsWith("edge://") ||
          tab.url.startsWith("about:")) {
        continue;
      }
      try {
        await chrome.scripting.executeScript({
          target: { tabId: tab.id },
          files: ["keepalive.js"],
        });
      } catch (_) {
        // Some tabs may not be scriptable — that's fine
      }
    }
    console.log("[MyBot Bridge] Injected keepalive.js into existing tabs");
  } catch (e) {
    console.error("[MyBot Bridge] Failed to inject keepalive:", e);
  }
}

// ── WebSocket lifecycle ──────────────────────────────────────────────────────

function connect() {
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
    return;
  }

  try {
    ws = new WebSocket(WS_URL);

    ws.onopen = () => {
      wsConnected = true;
      reconnectDelay = RECONNECT_DELAY_MS; // reset backoff on success
      console.log("[MyBot Bridge] Connected to dock app");

      // Announce ourselves
      chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
        const info = (tabs || []).map((t) => ({ id: t.id, url: t.url, title: t.title }));
        safeSend({ type: "connected", tabs: info });
      });

      // Start WebSocket-level pings
      clearInterval(pingTimer);
      pingTimer = setInterval(() => {
        safeSend({ type: "ws_ping", ts: Date.now() });
      }, WS_PING_INTERVAL_MS);

      // Start self-keepalive so Chrome doesn't kill us between pings
      startSelfKeepAlive();
    };

    ws.onmessage = (event) => {
      let msg;
      try {
        msg = JSON.parse(event.data);
      } catch {
        console.error("[MyBot Bridge] Bad JSON from app:", event.data);
        return;
      }

      // Process the command asynchronously but CATCH all errors
      processMessage(msg).catch((err) => {
        console.error("[MyBot Bridge] processMessage error:", err);
        safeSend({ id: msg.id, success: false, error: String(err) });
      });
    };

    ws.onclose = () => {
      wsConnected = false;
      ws = null;
      clearInterval(pingTimer);
      pingTimer = null;
      stopSelfKeepAlive();
      console.log("[MyBot Bridge] Disconnected — will retry in", reconnectDelay, "ms");
      setTimeout(connect, reconnectDelay);
      // Exponential backoff (capped)
      reconnectDelay = Math.min(reconnectDelay * 1.5, RECONNECT_MAX_DELAY_MS);
    };

    ws.onerror = (err) => {
      console.error("[MyBot Bridge] WebSocket error:", err);
      try { if (ws) ws.close(); } catch (_) {}
    };
  } catch (e) {
    console.error("[MyBot Bridge] connect() error:", e);
    setTimeout(connect, reconnectDelay);
    reconnectDelay = Math.min(reconnectDelay * 1.5, RECONNECT_MAX_DELAY_MS);
  }
}

function safeSend(obj) {
  try {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(obj));
    }
  } catch (e) {
    console.error("[MyBot Bridge] safeSend error:", e);
  }
}

// ── Keep service-worker alive (MV3 safety net) ──────────────────────────────

chrome.alarms.create(KEEPALIVE_ALARM, { periodInMinutes: 0.4 });

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === KEEPALIVE_ALARM) {
    if (!wsConnected) connect();
  }
});

// ── Command processing ───────────────────────────────────────────────────────

async function processMessage(msg) {
  const result = await handleCommand(msg);
  safeSend({ id: msg.id, ...result });
}

async function handleCommand(msg) {
  const { command, params } = msg;
  try {
    switch (command) {
      case "screenshot":    return await cmdScreenshot();
      case "navigate":      return await cmdNavigate(params.url);
      case "click":         return await cmdClick(params.x, params.y);
      case "type":          return await cmdType(params.text);
      case "select_all_and_type": return await cmdSelectAllAndType(params.text);
      case "press_key":     return await cmdPressKey(params.key);
      case "scroll":        return await cmdScroll(params.direction, params.amount || 300);
      case "get_url":       return await cmdGetUrl();
      case "get_page_state": return await cmdGetPageState();
      case "get_dom":       return await cmdGetDom();
      case "execute_script": return await cmdExecuteScript(params.script);
      case "click_by_text": return await cmdClickByText(params.text, params.tag || "*");
      case "element_at":    return await cmdElementAt(params.x, params.y);
      case "get_tabs":      return await cmdGetTabs();
      case "switch_tab":    return await cmdSwitchTab(params.tabId);
      case "wait_stable":   return await cmdWaitStable(params?.quietMs, params?.timeoutMs);
      case "ping":          return { success: true, data: "pong" };
      default:              return { success: false, error: `Unknown command: ${command}` };
    }
  } catch (e) {
    console.error("[MyBot Bridge] handleCommand error for", command, ":", e);
    return { success: false, error: String(e) };
  }
}

// ── Helpers ──────────────────────────────────────────────────────────────────

async function activeTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  return tab;
}

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

// ── Commands ─────────────────────────────────────────────────────────────────

// Full-page screenshot: fixed width and max height for consistent coordinate space
const FULL_PAGE_IMAGE_WIDTH = 1200;
const FULL_PAGE_IMAGE_MAX_HEIGHT = 6000;

async function cmdScreenshot() {
  const tab = await activeTab();
  if (!tab?.id) return { success: false, error: "No active tab" };

  await cmdWaitStable(2500, 20000);
  await sleep(2000);

  const win = await chrome.windows.getCurrent({ populate: false });
  let lastErr;

  for (let attempt = 0; attempt < 4; attempt++) {
    try {
      // Get document and viewport dimensions
      const [dimResult] = await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        func: () => ({
          scrollWidth: Math.max(document.documentElement.scrollWidth, document.body.scrollWidth, 1),
          scrollHeight: Math.max(document.documentElement.scrollHeight, document.body.scrollHeight, 1),
          viewportWidth: window.innerWidth,
          viewportHeight: window.innerHeight,
        }),
      });
      const dims = dimResult?.result;
      if (!dims || dims.scrollHeight <= 0) {
        return { success: false, error: "Could not get page dimensions" };
      }

      const vw = dims.viewportWidth;
      const vh = dims.viewportHeight;
      const docH = dims.scrollHeight;

      // Wake up dynamic/lazy content (e.g. LinkedIn job list): scroll down a few viewports
      // with pauses so lazy-loaded content renders, then back to top
      await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        func: () => { window.scrollTo(0, 0); },
      });
      await sleep(600);
      for (let s = 1; s <= 4; s++) {
        await chrome.scripting.executeScript({
          target: { tabId: tab.id },
          func: (y) => { window.scrollTo(0, y); },
          args: [s * vh],
        });
        await sleep(800);
      }
      await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        func: () => { window.scrollTo(0, 0); },
      });
      await sleep(1000);

      // Standard window scroll capture
      const strips = [];
      let scrollY = 0;
      while (scrollY < docH) {
          try {
            const dataUrl = await chrome.tabs.captureVisibleTab(win.id, { format: "png" });
            strips.push(dataUrl);
          } catch (e) {
            if (String(e).includes("activeTab") || String(e).includes("Cannot access")) {
              return { success: false, error: String(e) + " (try navigating to a regular web page first)" };
            }
            throw e;
          }
          scrollY += vh;
          if (scrollY < docH) {
            await chrome.scripting.executeScript({
              target: { tabId: tab.id },
              func: (y) => { window.scrollTo(0, y); },
              args: [scrollY],
            });
            await sleep(400);
          }
        }
      }

      // Scroll back to top
      await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        func: () => { window.scrollTo(0, 0); },
      });

      return {
        success: true,
        data: {
          screenshot: null,
          fullPage: true,
          screenshots: strips,
          scrollWidth: dims.scrollWidth,
          scrollHeight: dims.scrollHeight,
          viewportWidth: vw,
          viewportHeight: vh,
        },
      };
    } catch (e) {
      lastErr = e;
      const errMsg = String(e);
      if (errMsg.includes("MAX_CAPTURE_VISIBLE_TAB_CALLS_PER_SECOND")) {
        await sleep(1500 * (attempt + 1));
        continue;
      }
      if (errMsg.includes("activeTab") || errMsg.includes("Cannot access")) {
        return { success: false, error: errMsg + " (try navigating to a regular web page first)" };
      }
      await sleep(600 * (attempt + 1));
    }
  }
  return { success: false, error: String(lastErr) };
}

async function cmdNavigate(url) {
  const tab = await activeTab();
  await chrome.tabs.update(tab.id, { url });
  return new Promise((resolve) => {
    const timeout = setTimeout(() => {
      chrome.tabs.onUpdated.removeListener(onUpdated);
      resolve({ success: true, data: { url, note: "navigation timeout — page may still be loading" } });
    }, 30000);

    const onUpdated = (tabId, info) => {
      if (tabId === tab.id && info.status === "complete") {
        clearTimeout(timeout);
        chrome.tabs.onUpdated.removeListener(onUpdated);
        resolve({ success: true, data: { url } });
      }
    };
    chrome.tabs.onUpdated.addListener(onUpdated);
  });
}

async function cmdClick(x, y) {
  const tab = await activeTab();
  const [result] = await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    func: (imgX, imgY) => {
      const scrollWidth = Math.max(document.documentElement.scrollWidth, document.body.scrollWidth, 1);
      const scrollHeight = Math.max(document.documentElement.scrollHeight, document.body.scrollHeight, 1);
      const imageWidth = 1200;
      const imageHeight = Math.min(Math.ceil(scrollHeight * 1200 / scrollWidth), 6000);
      const docX = imgX * scrollWidth / imageWidth;
      const docY = imgY * scrollHeight / imageHeight;
      const vw = window.innerWidth;
      const vh = window.innerHeight;
      window.scrollTo(docX - vw / 2, docY - vh / 2);
      const centerX = vw / 2;
      const centerY = vh / 2;
      const el = document.elementFromPoint(centerX, centerY);
      if (!el) return { success: false, error: "No element at coordinates" };
      for (const evtName of ["pointerdown", "mousedown", "pointerup", "mouseup", "click"]) {
        el.dispatchEvent(new PointerEvent(evtName, {
          bubbles: true, cancelable: true, view: window,
          clientX: centerX, clientY: centerY, pointerId: 1,
        }));
      }
      if (typeof el.focus === "function") el.focus();
      return {
        success: true,
        data: { tag: el.tagName, text: (el.innerText || "").substring(0, 100) },
      };
    },
    args: [x, y],
  });
  return result?.result || { success: false, error: "executeScript failed" };
}

async function cmdType(text) {
  const tab = await activeTab();
  const [result] = await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    func: (txt) => {
      const el = document.activeElement;
      if (!el) return { success: false, error: "No focused element" };
      if (el.tagName === "INPUT" || el.tagName === "TEXTAREA") {
        el.focus();
        el.value = "";
        for (const ch of txt) {
          el.value += ch;
          el.dispatchEvent(new InputEvent("input", { bubbles: true, data: ch, inputType: "insertText" }));
        }
        el.dispatchEvent(new Event("change", { bubbles: true }));
      } else if (el.isContentEditable) {
        el.textContent = txt;
        el.dispatchEvent(new InputEvent("input", { bubbles: true }));
      } else {
        return { success: false, error: "Active element is not editable" };
      }
      return { success: true, data: { typed: txt.substring(0, 50), tag: el.tagName } };
    },
    args: [text],
  });
  return result?.result || { success: false, error: "executeScript failed" };
}

async function cmdSelectAllAndType(text) {
  const tab = await activeTab();
  const [result] = await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    func: (txt) => {
      const el = document.activeElement;
      if (!el) return { success: false, error: "No focused element" };
      if (el.tagName === "INPUT" || el.tagName === "TEXTAREA") {
        el.focus();
        el.select();  // Select all existing text
        // Set value directly and fire events
        el.value = txt;
        el.dispatchEvent(new InputEvent("input", {
          bubbles: true, data: txt, inputType: "insertReplacementText"
        }));
        el.dispatchEvent(new Event("change", { bubbles: true }));
      } else if (el.isContentEditable) {
        el.textContent = txt;
        el.dispatchEvent(new InputEvent("input", { bubbles: true }));
      } else {
        return { success: false, error: "Active element is not editable" };
      }
      return { success: true, data: { typed: txt.substring(0, 50), tag: el.tagName } };
    },
    args: [text],
  });
  return result?.result || { success: false, error: "executeScript failed" };
}

async function cmdPressKey(key) {
  const tab = await activeTab();
  const [result] = await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    func: (keyStr) => {
      const parts = String(keyStr).split("+").map(p => p.trim().toLowerCase());
      const mainKey = parts.pop();
      const modList = parts.filter(p => ["ctrl", "alt", "shift", "meta"].includes(p));
      const keyCodeMap = {
        enter: 13, tab: 9, escape: 27, backspace: 8, delete: 46, insert: 45,
        home: 36, end: 35, pageup: 33, pagedown: 34, space: 32, " ": 32,
        arrowdown: 40, arrowup: 38, arrowleft: 37, arrowright: 39,
        f1: 112, f2: 113, f3: 114, f4: 115, f5: 116, f6: 117, f7: 118, f8: 119, f9: 120, f10: 121, f11: 122, f12: 123,
        capslock: 20,
      };
      const letter = mainKey.length === 1 && mainKey >= "a" && mainKey <= "z";
      const keyName = mainKey.length === 1 ? (letter ? mainKey.toLowerCase() : mainKey) : mainKey.charAt(0).toUpperCase() + mainKey.slice(1).toLowerCase();
      const code = keyCodeMap[mainKey] || (letter ? mainKey.toUpperCase().charCodeAt(0) : 0);
      const el = document.activeElement || document.body;
      const ctrl = modList.includes("ctrl");
      const alt = modList.includes("alt");
      const shift = modList.includes("shift");
      const meta = modList.includes("meta");
      const opts = { key: keyName, code: mainKey.length === 1 ? "Key" + mainKey.toUpperCase() : mainKey, keyCode: code, which: code, bubbles: true, cancelable: true, ctrlKey: ctrl, altKey: alt, shiftKey: shift, metaKey: meta };
      for (const m of ["Control", "Alt", "Shift", "Meta"]) {
        if ((m === "Control" && ctrl) || (m === "Alt" && alt) || (m === "Shift" && shift) || (m === "Meta" && meta)) {
          el.dispatchEvent(new KeyboardEvent("keydown", { key: m, code: m, keyCode: m === "Control" ? 17 : m === "Alt" ? 18 : m === "Shift" ? 16 : 91, bubbles: true, cancelable: true, ctrlKey: ctrl, altKey: alt, shiftKey: shift, metaKey: meta }));
        }
      }
      for (const t of ["keydown", "keypress", "keyup"]) {
        el.dispatchEvent(new KeyboardEvent(t, opts));
      }
      for (const m of ["Meta", "Shift", "Alt", "Control"]) {
        if ((m === "Control" && ctrl) || (m === "Alt" && alt) || (m === "Shift" && shift) || (m === "Meta" && meta)) {
          el.dispatchEvent(new KeyboardEvent("keyup", { key: m, code: m, keyCode: m === "Control" ? 17 : m === "Alt" ? 18 : m === "Shift" ? 16 : 91, bubbles: true, cancelable: true }));
        }
      }
      if (mainKey === "enter" && el.form && !modList.length) {
        el.form.requestSubmit ? el.form.requestSubmit() : el.form.dispatchEvent(new Event("submit", { bubbles: true }));
      }
      return { success: true, data: { key: keyStr } };
    },
    args: [key],
  });
  return result?.result || { success: false, error: "executeScript failed" };
}

async function cmdScroll(direction, amount) {
  const tab = await activeTab();
  const [result] = await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    func: (dir, amt) => {
      const px = dir === "up" ? -amt : amt;
      window.scrollBy({ top: px, behavior: "smooth" });
      return { success: true, data: { scrolled: px, scrollY: window.scrollY } };
    },
    args: [direction, amount],
  });
  return result?.result || { success: false, error: "executeScript failed" };
}

async function cmdGetUrl() {
  const tab = await activeTab();
  return { success: true, data: { url: tab.url, title: tab.title } };
}

async function cmdGetPageState() {
  const tab = await activeTab();
  const [result] = await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    func: () => {
      const doc = document.documentElement?.outerHTML || "";
      let hash = 0;
      for (let i = 0; i < Math.min(doc.length, 5000); i++) {
        hash = ((hash << 5) - hash) + doc.charCodeAt(i) | 0;
      }
      const el = document.activeElement;
      return {
        url: location.href,
        domHash: hash.toString(36),
        focusedTag: el ? el.tagName : "",
        focusedValue: el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA") ? (el.value || "").substring(0, 100) : "",
      };
    },
  });
  return result?.result || { success: false, error: "Failed to get page state" };
}

async function cmdExecuteScript(script) {
  if (!script || typeof script !== "string") return { success: false, error: "No script" };
  const tab = await activeTab();
  try {
    const [result] = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: (s) => eval(s),
      args: [script],
    });
    return { success: true, data: { result: result?.result } };
  } catch (e) {
    return { success: false, error: String(e) };
  }
}

async function cmdClickByText(text, tag) {
  const tab = await activeTab();
  const [result] = await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    func: (_text, _tag) => {
      const sel = _tag === "*" ? "a,button,input,textarea,[role='button'],[onclick]" : _tag;
      const els = document.querySelectorAll(sel);
      const t = String(_text || "").toLowerCase().trim();
      for (const el of els) {
        const label = (el.innerText || el.value || el.placeholder || el.getAttribute("aria-label") || "").toLowerCase().trim();
        if (label && label.includes(t)) {
          el.scrollIntoView({ block: "center", behavior: "instant" });
          const r = el.getBoundingClientRect();
          const cx = r.left + r.width / 2;
          const cy = r.top + r.height / 2;
          const el2 = document.elementFromPoint(cx, cy);
          const target = (el2 === el || el.contains(el2)) ? el : el2;
          if (target) {
            target.click();
            return { success: true, data: { tag: target.tagName } };
          }
        }
      }
      return { success: false, error: "No element with text: " + t.substring(0, 30) };
    },
    args: [text, tag || "*"],
  });
  return result?.result || { success: false, error: "executeScript failed" };
}

async function cmdElementAt(imgX, imgY) {
  const tab = await activeTab();
  const [result] = await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    func: (ix, iy) => {
      const scrollWidth = Math.max(document.documentElement.scrollWidth, document.body.scrollWidth, 1);
      const scrollHeight = Math.max(document.documentElement.scrollHeight, document.body.scrollHeight, 1);
      const imageWidth = 1200;
      const imageHeight = Math.min(Math.ceil(scrollHeight * 1200 / scrollWidth), 6000);
      const docX = ix * scrollWidth / imageWidth;
      const docY = iy * scrollHeight / imageHeight;
      const vw = window.innerWidth;
      const vh = window.innerHeight;
      window.scrollTo(docX - vw / 2, docY - vh / 2);
      const el = document.elementFromPoint(vw / 2, vh / 2);
      if (!el) return { success: true, data: { exists: false } };
      const r = el.getBoundingClientRect();
      const visible = r.width > 0 && r.height > 0;
      return {
        success: true,
        data: {
          exists: true,
          tag: el.tagName,
          visible,
          text: (el.innerText || el.value || "").substring(0, 60),
        },
      };
    },
    args: [imgX, imgY],
  });
  return result?.result || { success: false, error: "element_at failed" };
}

async function cmdGetDom() {
  const tab = await activeTab();
  const [result] = await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    func: () => {
      const scrollWidth = Math.max(document.documentElement.scrollWidth, document.body.scrollWidth, 1);
      const scrollHeight = Math.max(document.documentElement.scrollHeight, document.body.scrollHeight, 1);
      const imageWidth = 1200;
      const imageHeight = Math.min(Math.ceil(scrollHeight * 1200 / scrollWidth), 6000);
      const text = (document.body.innerText || "").substring(0, 8000);
      const interactives = [];
      const sel = "a,button,input,textarea,select,[role='button'],[onclick]";
      document.querySelectorAll(sel).forEach((el, i) => {
        if (i >= 60) return;
        const r = el.getBoundingClientRect();
        if (r.width === 0 && r.height === 0) return;
        const docX = r.left + r.width / 2 + window.scrollX;
        const docY = r.top + r.height / 2 + window.scrollY;
        const x = Math.round(docX * imageWidth / scrollWidth);
        const y = Math.round(docY * imageHeight / scrollHeight);
        const role = el.getAttribute("role") || (el.tagName === "A" ? "link" : el.tagName === "BUTTON" ? "button" : el.tagName === "INPUT" ? "textbox" : "");
        const axLabel = el.getAttribute("aria-label") || el.getAttribute("aria-labelledby") || "";
        interactives.push({
          tag: el.tagName, type: el.type || null,
          text: (el.innerText || el.value || el.placeholder || axLabel || "").substring(0, 80),
          x, y, role: role || undefined, axLabel: axLabel || undefined,
        });
      });
      return {
        success: true,
        data: {
          title: document.title,
          url: location.href,
          text,
          interactives,
          imageWidth,
          imageHeight,
          scrollWidth,
          scrollHeight,
        },
      };
    },
  });
  return result?.result || { success: false, error: "executeScript failed" };
}

async function cmdGetTabs() {
  const tabs = await chrome.tabs.query({});
  return {
    success: true,
    data: { tabs: tabs.map((t) => ({ id: t.id, url: t.url, title: t.title, active: t.active })) },
  };
}

async function cmdSwitchTab(tabId) {
  await chrome.tabs.update(tabId, { active: true });
  return { success: true, data: { tabId } };
}

async function cmdWaitStable(quietMs = 2500, timeoutMs = 20000) {
  const tab = await activeTab();
  if (!tab) return { success: false, error: "No active tab" };

  if (tab.url && (tab.url.startsWith("chrome://") || tab.url.startsWith("edge://"))) {
    await sleep(1500);
    return { success: true, data: { method: "flat_delay", waited: 1500 } };
  }

  try {
    const [result] = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: (_quietMs, _timeoutMs) => {
        return new Promise((resolve) => {
          const start = Date.now();
          const postQuietMs = 2500;

          function waitForIframes(cb) {
            const iframes = document.querySelectorAll("iframe");
            if (iframes.length === 0) return cb();
            let loaded = 0;
            let done = false;
            function onLoad() {
              loaded++;
              if (loaded >= iframes.length && !done) { done = true; cb(); }
            }
            for (const ifr of iframes) {
              try {
                if (ifr.contentDocument?.readyState === "complete") onLoad();
                else ifr.addEventListener("load", onLoad, { once: true });
              } catch (_) { onLoad(); }
            }
            setTimeout(() => { if (!done) { done = true; cb(); } }, 4000);
          }

          function waitForReady() {
            if (document.readyState === "complete") {
              waitForIframes(waitForQuiet);
            } else if (Date.now() - start > _timeoutMs) {
              resolve({ success: true, data: { waited: Date.now() - start, reason: "timeout_readyState" } });
            } else {
              setTimeout(waitForReady, 150);
            }
          }

          function waitForQuiet() {
            let lastMutation = Date.now();
            const observer = new MutationObserver(() => { lastMutation = Date.now(); });
            observer.observe(document.body || document.documentElement, {
              childList: true, subtree: true, attributes: true, characterData: true,
            });

            function check() {
              const now = Date.now();
              if (now - lastMutation >= _quietMs) {
                observer.disconnect();
                setTimeout(() => {
                  resolve({ success: true, data: { waited: now - start + postQuietMs, reason: "dom_quiet" } });
                }, postQuietMs);
              } else if (now - start > _timeoutMs) {
                observer.disconnect();
                resolve({ success: true, data: { waited: now - start, reason: "timeout_mutations" } });
              } else {
                setTimeout(check, 300);
              }
            }
            setTimeout(check, Math.min(_quietMs, 500));
          }

          waitForReady();
        });
      },
      args: [quietMs, timeoutMs],
    });

    return result?.result || { success: true, data: { waited: 0, reason: "no_result" } };
  } catch (e) {
    await sleep(3000);
    return { success: true, data: { method: "fallback_delay", waited: 3000, error: String(e) } };
  }
}

// ── Popup status query ───────────────────────────────────────────────────────

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg.type === "get_status") {
    sendResponse({
      connected: wsConnected,
      url: WS_URL,
      keepalivePorts: keepalivePorts.size,
    });
  }
  return true;
});

// ── Boot ─────────────────────────────────────────────────────────────────────

connect();

chrome.runtime.onStartup.addListener(() => {
  connect();
  // Re-inject keepalive into all tabs (service worker restarted)
  injectKeepaliveIntoAllTabs();
});

chrome.runtime.onInstalled.addListener(() => {
  connect();
  // After install/reload, existing tabs have orphaned content scripts.
  // Re-inject keepalive.js so they can maintain ports to this new worker.
  injectKeepaliveIntoAllTabs();
});
