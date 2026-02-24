"""
playbook.py — Playbook Recording, Storage, Matching & Replay
=============================================================
Records successful browser automation sequences and replays them
for similar future tasks, dramatically reducing LLM API costs.

Created by AJ (Ahmed Jouini).

Architecture:
  • PlaybookStore     – Persistence layer: save / load / search playbooks as JSON.
  • PlaybookRecorder  – Captures actions from a successful AgentHistoryList.
  • PlaybookPlayer    – Replays a playbook via CDP through the browser-use Browser.
  • Keyword matcher   – Jaccard similarity on extracted keywords (no extra deps).

Directory layout:
  playbooks/
    index.json            ← lightweight search index
    <id>.json             ← full playbook with all steps
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# ── Where playbooks are stored ────────────────────────────────────────────────
PLAYBOOKS_DIR = Path(__file__).parent / "playbooks"

# ── Similarity threshold for matching ─────────────────────────────────────────
MATCH_THRESHOLD = 0.35          # Jaccard similarity (0–1)
MATCH_THRESHOLD_DOMAIN = 0.25   # lower bar when same domain


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║                          DATA MODELS                                       ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

@dataclass
class RecordedStep:
    """A single recorded browser action."""

    # What the agent did
    action_name: str = ""              # e.g. "navigate", "click", "input", "scroll", "done"
    action_params: dict = field(default_factory=dict)   # raw action params from model_dump

    # The page at this step
    url: str = ""
    page_title: str = ""

    # Element info (populated for click / input / select actions)
    element_xpath: str = ""
    element_tag: str = ""              # e.g. "button", "a", "input"
    element_text: str = ""             # visible text / ax_name
    element_attributes: dict = field(default_factory=dict)   # id, class, name, ...
    element_role: str = ""             # aria role

    # Whether the text input is task-specific (parameterized)
    is_param: bool = False

    # Step number (0-based)
    step_idx: int = 0


@dataclass
class Playbook:
    """A complete recorded sequence of browser actions."""

    id: str = ""
    task: str = ""                    # original user prompt
    keywords: list[str] = field(default_factory=list)
    domain: str = ""                  # primary domain visited
    created: str = ""
    last_used: str = ""
    use_count: int = 0
    success_count: int = 0
    steps: list[RecordedStep] = field(default_factory=list)


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║                          KEYWORD EXTRACTION                                ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

_STOP_WORDS = frozenset({
    "a", "an", "the", "to", "for", "and", "or", "but", "in", "on", "at",
    "is", "it", "of", "me", "my", "do", "go", "can", "get", "if", "so",
    "be", "by", "up", "no", "not", "all", "any", "out", "has", "had",
    "was", "are", "its", "how", "our", "you", "we", "he", "she", "they",
    "please", "i", "want", "need", "would", "like", "that", "this",
    "with", "from", "just", "very", "some", "also", "then", "than",
    "make", "find", "show", "tell", "look", "what", "which", "where",
    "when", "who", "will", "been", "have", "more", "most", "much",
    "only", "into", "over", "such", "after", "each", "about", "them",
    "here", "there", "could", "should", "might", "thing", "things",
})


def _simple_stem(word: str) -> str:
    """Ultra-light stemmer — just strips common English suffixes."""
    if len(word) > 5 and word.endswith("ting"):
        return word[:-4]
    if len(word) > 4 and word.endswith("ing"):
        return word[:-3]
    if len(word) > 3 and word.endswith("ed"):
        return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def extract_keywords(text: str) -> list[str]:
    """Extract meaningful keywords from a task description (with light stemming)."""
    words = re.findall(r"[a-z0-9]+", text.lower())
    return sorted({_simple_stem(w) for w in words if w not in _STOP_WORDS and len(w) > 1})


def jaccard_similarity(a: set[str], b: set[str]) -> float:
    """Jaccard similarity between two keyword sets."""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║                          PLAYBOOK STORE                                    ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

class PlaybookStore:
    """
    Manages playbook persistence on disk and keyword-based search.

    Storage format:
      playbooks/
        index.json              – [{id, task, keywords, domain, created}, ...]
        keyword_index.json      – inverted index: {keyword → [playbook_ids]}
        <id>.json               – full Playbook serialised via dataclasses.asdict

    Performance
    -----------
    ``find_match()`` uses an **inverted keyword index** for O(1) candidate
    lookup per keyword.  Only playbooks that share at least one keyword with
    the query are scored with Jaccard similarity.  For 1000+ playbooks this
    is orders of magnitude faster than the naïve O(n) linear scan.
    """

    def __init__(self, directory: Path | str = PLAYBOOKS_DIR):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self._index_file = self.directory / "index.json"
        self._kw_index_file = self.directory / "keyword_index.json"
        self._index: list[dict] = self._load_index()
        # keyword → set of playbook IDs
        self._kw_index: dict[str, set[str]] = self._load_kw_index()

    # ── Persistence ───────────────────────────────────────────────────────

    def _load_index(self) -> list[dict]:
        if self._index_file.exists():
            try:
                with open(self._index_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                return []
        return []

    def _save_index(self):
        with open(self._index_file, "w", encoding="utf-8") as f:
            json.dump(self._index, f, indent=2, ensure_ascii=False)

    def _load_kw_index(self) -> dict[str, set[str]]:
        """Load the inverted keyword index from disk, or rebuild it."""
        if self._kw_index_file.exists():
            try:
                with open(self._kw_index_file, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                # JSON stores lists — convert to sets
                return {kw: set(ids) for kw, ids in raw.items()}
            except (json.JSONDecodeError, OSError):
                pass
        # Rebuild from the main index
        return self._rebuild_kw_index()

    def _save_kw_index(self):
        """Persist the inverted keyword index (sets → lists for JSON)."""
        serialisable = {kw: sorted(ids) for kw, ids in self._kw_index.items()}
        with open(self._kw_index_file, "w", encoding="utf-8") as f:
            json.dump(serialisable, f, indent=1, ensure_ascii=False)

    def _rebuild_kw_index(self) -> dict[str, set[str]]:
        """Rebuild the inverted index from scratch using the main index."""
        kw_idx: dict[str, set[str]] = {}
        for entry in self._index:
            pb_id = entry["id"]
            for kw in entry.get("keywords", []):
                kw_idx.setdefault(kw, set()).add(pb_id)
        logger.debug("Rebuilt keyword index: %d keywords across %d playbooks",
                      len(kw_idx), len(self._index))
        return kw_idx

    def save(self, playbook: Playbook) -> None:
        """Save (or overwrite) a playbook to disk and update all indices."""
        filepath = self.directory / f"{playbook.id}.json"
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(asdict(playbook), f, indent=2, ensure_ascii=False)

        # ── Update main index ─────────────────────────────────────────────
        # Remove old entry for same id (and its keywords from kw_index)
        old_entry = next((e for e in self._index if e["id"] == playbook.id), None)
        if old_entry:
            for kw in old_entry.get("keywords", []):
                if kw in self._kw_index:
                    self._kw_index[kw].discard(playbook.id)
                    if not self._kw_index[kw]:
                        del self._kw_index[kw]

        self._index = [e for e in self._index if e["id"] != playbook.id]
        self._index.append({
            "id": playbook.id,
            "task": playbook.task,
            "keywords": playbook.keywords,
            "domain": playbook.domain,
            "created": playbook.created,
        })
        self._save_index()

        # ── Update inverted keyword index ─────────────────────────────────
        for kw in playbook.keywords:
            self._kw_index.setdefault(kw, set()).add(playbook.id)
        self._save_kw_index()

    def load(self, playbook_id: str) -> Playbook | None:
        """Load a playbook by id."""
        filepath = self.directory / f"{playbook_id}.json"
        if not filepath.exists():
            return None
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
            steps = [RecordedStep(**s) for s in data.pop("steps", [])]
            return Playbook(**data, steps=steps)
        except Exception as exc:
            logger.warning("Failed to load playbook %s: %s", playbook_id, exc)
            return None

    def delete(self, playbook_id: str) -> None:
        """Delete a playbook from disk and all indices."""
        # Remove from keyword index
        old_entry = next((e for e in self._index if e["id"] == playbook_id), None)
        if old_entry:
            for kw in old_entry.get("keywords", []):
                if kw in self._kw_index:
                    self._kw_index[kw].discard(playbook_id)
                    if not self._kw_index[kw]:
                        del self._kw_index[kw]
            self._save_kw_index()

        filepath = self.directory / f"{playbook_id}.json"
        if filepath.exists():
            filepath.unlink()
        self._index = [e for e in self._index if e["id"] != playbook_id]
        self._save_index()

    def list_all(self) -> list[dict]:
        """Return the full index (lightweight summaries)."""
        return list(self._index)

    # ── Search / Match (with inverted index) ──────────────────────────────

    def find_match(self, task: str) -> Playbook | None:
        """
        Find the best-matching playbook for a new task.

        Uses the **inverted keyword index** to collect candidate playbooks
        that share at least one keyword with the query (O(k) where k is the
        number of query keywords).  Only candidates are scored with Jaccard
        similarity — the rest are skipped entirely.

        Returns the best Playbook or None if nothing exceeds the threshold.
        """
        task_kw = set(extract_keywords(task))
        if not task_kw:
            return None

        # ── Step 1: Gather candidates via inverted index (O(k)) ───────────
        candidate_ids: set[str] = set()
        for kw in task_kw:
            if kw in self._kw_index:
                candidate_ids |= self._kw_index[kw]

        if not candidate_ids:
            return None

        # Build a quick lookup: id → index entry
        entry_by_id = {e["id"]: e for e in self._index}

        # ── Step 2: Score only candidates (O(c) where c ≪ n) ──────────────
        best_score = 0.0
        best_id: str | None = None

        for pb_id in candidate_ids:
            entry = entry_by_id.get(pb_id)
            if entry is None:
                continue

            entry_kw = set(entry.get("keywords", []))
            score = jaccard_similarity(task_kw, entry_kw)

            # Bonus: lower threshold if domain words overlap
            threshold = MATCH_THRESHOLD
            domain = entry.get("domain", "")
            if domain:
                domain_words = set(re.findall(r"[a-z0-9]+", domain.lower()))
                if domain_words & task_kw:
                    threshold = MATCH_THRESHOLD_DOMAIN

            if score >= threshold and score > best_score:
                best_score = score
                best_id = pb_id

        if best_id:
            pb = self.load(best_id)
            if pb:
                logger.info(
                    "Playbook match: %.0f%% similarity — '%s'  (%d candidates checked of %d total)",
                    best_score * 100, pb.task,
                    len(candidate_ids), len(self._index),
                )
            return pb
        return None

    def update_stats(self, playbook_id: str, success: bool) -> None:
        """Increment use_count (and success_count if successful) on disk."""
        pb = self.load(playbook_id)
        if not pb:
            return
        pb.use_count += 1
        if success:
            pb.success_count += 1
        pb.last_used = datetime.now().isoformat()
        self.save(pb)


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║                        PLAYBOOK RECORDER                                   ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

class PlaybookRecorder:
    """
    Extracts a Playbook from a completed AgentHistoryList.

    Call `record(history)` after a successful agent run to build a
    Playbook, then `PlaybookStore().save(pb)` to persist it.
    """

    def __init__(self, task: str):
        self.task = task
        self._task_kw = set(extract_keywords(task))

    def record(self, history) -> Playbook:
        """
        Build a Playbook from an AgentHistoryList.

        Parameters
        ----------
        history : AgentHistoryList
            The history returned by ``agent.run()``.
        """
        steps: list[RecordedStep] = []
        primary_domain = ""

        # model_actions() returns a list of dicts, each like:
        #   {"navigate": {"url": "..."}, "interacted_element": {...} | None}
        actions = getattr(history, "model_actions", lambda: [])()
        step_urls_raw = getattr(history, "urls", lambda: [])()
        step_urls = [str(u) if u is not None else "" for u in (step_urls_raw or [])]

        # Fallback: if model_actions is empty, iterate history.history directly
        if not actions and getattr(history, "history", None):
            actions = []
            step_urls = []
            for h in history.history:
                if not getattr(h, "model_output", None):
                    continue
                step_url = ""
                if hasattr(h, "state") and h.state:
                    step_url = str(getattr(h.state, "url", "") or "")
                interacted = getattr(h.state, "interacted_element", None) if (hasattr(h, "state") and h.state) else None
                interacted_list = (interacted or [None] * len(h.model_output.action))[: len(h.model_output.action)]
                if len(interacted_list) < len(h.model_output.action):
                    interacted_list.extend([None] * (len(h.model_output.action) - len(interacted_list)))
                for j, act in enumerate(h.model_output.action):
                    d = act.model_dump(exclude_none=True, mode="json") if hasattr(act, "model_dump") else {}
                    if isinstance(d, dict):
                        d = dict(d)
                        d["interacted_element"] = interacted_list[j] if j < len(interacted_list) else None
                        actions.append(d)
                        step_urls.append(step_url)

        for i, action_dict in enumerate(actions):
            if not isinstance(action_dict, dict):
                continue
            # Copy to avoid mutating the original
            action_dict = dict(action_dict)
            ie = action_dict.pop("interacted_element", None)
            # Serialize interacted_element if it's a Pydantic model (has model_dump)
            if ie is not None and not isinstance(ie, dict) and hasattr(ie, "model_dump"):
                try:
                    ie = ie.model_dump(mode="json") if hasattr(ie.model_dump, "__call__") else {}
                except Exception:
                    pass

            if not action_dict:
                continue

            # Action structure: {"navigate": {"url": "..."}} or {"action": "navigate", "url": "..."}
            action_name = ""
            action_params = {}
            if "action" in action_dict:
                action_name = str(action_dict.pop("action", ""))
                action_params = dict(action_dict) if isinstance(action_dict, dict) else {}
            else:
                keys = list(action_dict.keys())
                if keys:
                    action_name = str(keys[0])
                    raw = action_dict.get(action_name, {})
                    action_params = dict(raw) if isinstance(raw, dict) else {}

            # Skip "done" actions — not useful for replay
            if action_name == "done":
                continue

            url = step_urls[i] if i < len(step_urls) else (step_urls[-1] if step_urls else "")
            url = str(url).strip() if url else ""

            # Extract domain from first real URL
            if url and not primary_domain:
                parsed = urlparse(url)
                if parsed.netloc and "about:" not in url:
                    primary_domain = parsed.netloc

            step = RecordedStep(
                action_name=action_name,
                action_params=dict(action_params) if isinstance(action_params, dict) else {},
                url=url,
                step_idx=len(steps),
            )

            # Enrich from interacted element
            if ie and isinstance(ie, dict):
                step.element_xpath = ie.get("x_path", "")
                step.element_tag = ie.get("node_name", "").lower()
                step.element_text = ie.get("ax_name", "") or ie.get("node_value", "")
                step.element_attributes = ie.get("attributes", {}) or {}
            elif ie and hasattr(ie, "x_path"):
                step.element_xpath = getattr(ie, "x_path", "")
                step.element_tag = getattr(ie, "node_name", "").lower()
                step.element_text = getattr(ie, "ax_name", "") or getattr(ie, "node_value", "")
                attrs = getattr(ie, "attributes", None)
                step.element_attributes = dict(attrs) if attrs else {}

            # Detect parameterized inputs
            if action_name == "input" and isinstance(action_params, dict):
                text = action_params.get("text", "")
                if text:
                    input_words = set(re.findall(r"[a-z0-9]+", text.lower()))
                    if input_words & self._task_kw:
                        step.is_param = True

            steps.append(step)

        return Playbook(
            id=uuid.uuid4().hex[:10],
            task=self.task,
            keywords=extract_keywords(self.task),
            domain=primary_domain,
            created=datetime.now().isoformat(),
            last_used=datetime.now().isoformat(),
            steps=steps,
        )

    def record_extension(
        self,
        extension_steps: list[dict],
        primary_url: str = "",
    ) -> Playbook:
        """
        Build a Playbook from Extension-mode action steps.

        extension_steps: list of {action_name, action_params, url} dicts
            action_name: "click" | "input" | "navigate" | "scroll" | "send_keys"
            action_params: dict with coordinate_x/y, text, url, direction, keys, etc.
        """
        steps: list[RecordedStep] = []
        primary_domain = ""
        if primary_url:
            parsed = urlparse(primary_url)
            if parsed.netloc and "about:" not in primary_url:
                primary_domain = parsed.netloc

        for i, raw in enumerate(extension_steps):
            if not isinstance(raw, dict):
                continue
            action_name = str(raw.get("action_name", ""))
            action_params = raw.get("action_params") or {}
            if isinstance(action_params, dict):
                action_params = dict(action_params)
            else:
                action_params = {}
            url = str(raw.get("url", ""))

            if action_name == "done":
                continue

            if url and not primary_domain:
                parsed = urlparse(url)
                if parsed.netloc and "about:" not in url:
                    primary_domain = parsed.netloc

            step = RecordedStep(
                action_name=action_name,
                action_params=action_params,
                url=url,
                step_idx=i,
            )
            steps.append(step)

        return Playbook(
            id=uuid.uuid4().hex[:10],
            task=self.task,
            keywords=extract_keywords(self.task),
            domain=primary_domain,
            created=datetime.now().isoformat(),
            last_used=datetime.now().isoformat(),
            steps=steps,
        )


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║                        PLAYBOOK PLAYER                                     ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

class PlaybookPlayer:
    """
    Replays a recorded Playbook through an open browser-use Browser session.

    Uses CDP (Chrome DevTools Protocol) via the browser-use Page object:
      • navigate  → page.goto(url)
      • click     → find element by xpath / text → Element.click()
      • input     → find element → focus + type
      • scroll    → page.evaluate(scroll JS)
      • search    → page.goto(search engine URL)

    On any failure the player stops and returns how far it got, so the
    caller can fall back to the normal LLM agent for the remaining work.
    """

    def __init__(self, browser, playbook: Playbook, new_task: str = ""):
        """
        Parameters
        ----------
        browser : browser_use.Browser
            An already-initialised Browser instance (built-in or CDP).
        playbook : Playbook
            The recorded playbook to replay.
        new_task : str
            The new task text — used to substitute parameters.
        """
        self._browser = browser
        self._playbook = playbook
        self._new_task = new_task
        self._steps_completed = 0

    @property
    def steps_completed(self) -> int:
        return self._steps_completed

    @property
    def total_steps(self) -> int:
        return len(self._playbook.steps)

    async def replay(self, on_status=None) -> tuple[bool, str]:
        """
        Replay the playbook step by step.

        Parameters
        ----------
        on_status : callable(str) | None
            Optional callback for status messages.

        Returns
        -------
        (success, message)
            success=True if all steps completed.
            message describes what happened.
        """
        steps = self._playbook.steps
        if not steps:
            return False, "Playbook has no steps to replay."

        def _emit(msg: str):
            if on_status:
                on_status(msg)

        # Get the BrowserSession from the Browser
        session = await self._get_session()
        if session is None:
            return False, "Could not get browser session for replay."

        _emit(f"▶  Replaying playbook ({len(steps)} steps)…")

        for i, step in enumerate(steps):
            try:
                _emit(f"  ↳ Step {i + 1}/{len(steps)}: {step.action_name}")
                await self._execute_step(session, step)
                self._steps_completed = i + 1
            except Exception as exc:
                msg = (
                    f"Replay failed at step {i + 1}/{len(steps)} "
                    f"({step.action_name}): {exc}"
                )
                _emit(f"  ⚠️  {msg}")
                return False, msg

        _emit(f"✅  Playbook replayed successfully ({len(steps)} steps)")
        return True, f"All {len(steps)} steps replayed successfully."

    # ── Internal helpers ──────────────────────────────────────────────────

    async def _get_session(self):
        """Get the BrowserSession from the Browser object."""
        try:
            # browser-use Browser exposes the session via get_session()
            # or the browser itself IS the session in newer versions
            if hasattr(self._browser, "get_session"):
                return await self._browser.get_session()
            # In some versions, Browser subclasses BrowserSession
            if hasattr(self._browser, "get_current_page"):
                return self._browser
            return None
        except Exception:
            return None

    async def _execute_step(self, session, step: RecordedStep):
        """Execute a single recorded step."""
        name = step.action_name
        params = step.action_params or {}

        if name == "navigate":
            url = params.get("url", step.url)
            if url:
                page = await session.get_current_page()
                if page:
                    await page.goto(url)
                    # Wait for page to settle
                    await self._wait_stable(session)

        elif name == "search":
            query = params.get("query", "")
            # Substitute parameterized query from new task if needed
            if self._new_task and step.is_param:
                query = self._extract_param_from_new_task(query)
            engine = params.get("engine", "duckduckgo")
            url = self._build_search_url(query, engine)
            page = await session.get_current_page()
            if page:
                await page.goto(url)
                await self._wait_stable(session)

        elif name == "click":
            await self._replay_click(session, step)

        elif name == "input":
            await self._replay_input(session, step, params)

        elif name == "scroll":
            await self._replay_scroll(session, step, params)

        elif name == "send_keys":
            keys = params.get("keys", "")
            if keys:
                page = await session.get_current_page()
                if page:
                    await page.evaluate(
                        '(...args) => { document.dispatchEvent(new KeyboardEvent("keydown", {key: args[0]})); }',
                        keys,
                    )

        elif name in ("go_back",):
            page = await session.get_current_page()
            if page and hasattr(page, "go_back"):
                await page.go_back()
                await self._wait_stable(session)

        elif name in ("switch_tab", "close_tab"):
            # Tab management — skip during replay (usually not needed)
            pass

        elif name in ("extract", "read_content", "screenshot", "find_elements", "search_page"):
            # Read-only actions — skip (no side effects)
            pass

        else:
            logger.debug("Skipping unknown action: %s", name)

    async def _replay_click(self, session, step: RecordedStep):
        """Find and click an element using multiple strategies."""
        page = await session.get_current_page()
        if not page:
            raise RuntimeError("No active page")

        # Strategy 1: XPath (most precise)
        if step.element_xpath:
            try:
                clicked = await self._click_by_xpath(page, step.element_xpath)
                if clicked:
                    await self._wait_stable(session)
                    return
            except Exception:
                pass

        # Strategy 2: By ID attribute
        elem_id = (step.element_attributes or {}).get("id", "")
        if elem_id:
            try:
                clicked = await page.evaluate(
                    '(...args) => { const el = document.getElementById(args[0]); '
                    'if (el) { el.click(); return true; } return false; }',
                    elem_id,
                )
                if clicked == "true" or clicked is True:
                    await self._wait_stable(session)
                    return
            except Exception:
                pass

        # Strategy 3: By visible text + tag
        if step.element_text and step.element_tag:
            try:
                clicked = await self._click_by_text(
                    page, step.element_text, step.element_tag
                )
                if clicked:
                    await self._wait_stable(session)
                    return
            except Exception:
                pass

        # Strategy 4: By visible text (any tag)
        if step.element_text:
            try:
                clicked = await self._click_by_text(page, step.element_text)
                if clicked:
                    await self._wait_stable(session)
                    return
            except Exception:
                pass

        # Strategy 5: By coordinate (if available from original action)
        coord_x = (step.action_params or {}).get("coordinate_x")
        coord_y = (step.action_params or {}).get("coordinate_y")
        if coord_x is not None and coord_y is not None:
            try:
                mouse = page._mouse if hasattr(page, "_mouse") else None
                if mouse is None and hasattr(page, "mouse"):
                    mouse = page.mouse
                if mouse:
                    await mouse.click(int(coord_x), int(coord_y))
                    await self._wait_stable(session)
                    return
            except Exception:
                pass

        raise RuntimeError(
            f"Could not find element to click: "
            f"xpath='{step.element_xpath}', text='{step.element_text}', "
            f"tag='{step.element_tag}'"
        )

    async def _replay_input(self, session, step: RecordedStep, params: dict):
        """Find an input element and type text into it."""
        page = await session.get_current_page()
        if not page:
            raise RuntimeError("No active page")

        text = params.get("text", "")
        clear = params.get("clear", True)

        # If this is a parameterized input, try to substitute
        if step.is_param and self._new_task:
            text = self._extract_param_from_new_task(text)

        # Strategy 1: XPath
        if step.element_xpath:
            try:
                typed = await self._type_by_xpath(page, step.element_xpath, text, clear)
                if typed:
                    return
            except Exception:
                pass

        # Strategy 2: By ID
        elem_id = (step.element_attributes or {}).get("id", "")
        if elem_id:
            try:
                clear_js = "el.value = '';" if clear else ""
                js_code = (
                    '(...args) => {'
                    '  const el = document.getElementById(args[0]);'
                    '  if (el) {'
                    '    ' + clear_js +
                    '    el.focus();'
                    '    el.value = args[1];'
                    '    el.dispatchEvent(new Event("input", {bubbles: true}));'
                    '    return true;'
                    '  }'
                    '  return false;'
                    '}'
                )
                typed = await page.evaluate(js_code, elem_id, text)
                if typed == "true" or typed is True:
                    return
            except Exception:
                pass

        # Strategy 3: By element name attribute
        name_attr = (step.element_attributes or {}).get("name", "")
        if name_attr:
            try:
                clear_js = "el.value = '';" if clear else ""
                js_code = (
                    '(...args) => {'
                    '  const el = document.querySelector("[name=\\"" + args[0] + "\\"]");'
                    '  if (el) {'
                    '    ' + clear_js +
                    '    el.focus();'
                    '    el.value = args[1];'
                    '    el.dispatchEvent(new Event("input", {bubbles: true}));'
                    '    return true;'
                    '  }'
                    '  return false;'
                    '}'
                )
                typed = await page.evaluate(js_code, name_attr, text)
                if typed == "true" or typed is True:
                    return
            except Exception:
                pass

        raise RuntimeError(
            f"Could not find input element: xpath='{step.element_xpath}', "
            f"id='{elem_id}', name='{name_attr}'"
        )

    async def _replay_scroll(self, session, step: RecordedStep, params: dict):
        """Replay a scroll action."""
        page = await session.get_current_page()
        if not page:
            return
        down = params.get("down", True)
        pages_val = params.get("pages", 1.0)
        direction = 1 if down else -1
        pixels = int(direction * pages_val * 800)
        await page.evaluate(
            '(...args) => { window.scrollBy(0, args[0]); }',
            pixels,
        )

    async def _click_by_xpath(self, page, xpath: str) -> bool:
        """Click an element found by XPath."""
        result = await page.evaluate(
            '(...args) => {'
            '  const xpath = args[0];'
            '  const result = document.evaluate(xpath, document, null, '
            '    XPathResult.FIRST_ORDERED_NODE_TYPE, null);'
            '  const el = result.singleNodeValue;'
            '  if (el) { el.scrollIntoView({block: "center"}); el.click(); return true; }'
            '  return false;'
            '}',
            xpath,
        )
        return result == "true" or result is True

    async def _click_by_text(self, page, text: str, tag: str = "") -> bool:
        """Click an element found by its visible text content."""
        tag_filter = f'&& el.tagName.toLowerCase() === "{tag}"' if tag else ""
        result = await page.evaluate(
            '(...args) => {'
            '  const targetText = args[0].trim().toLowerCase();'
            '  const allElements = document.querySelectorAll("*");'
            '  for (const el of allElements) {'
            '    const elText = (el.textContent || el.innerText || "").trim().toLowerCase();'
            '    const ariaLabel = (el.getAttribute("aria-label") || "").trim().toLowerCase();'
            f'    if ((elText === targetText || ariaLabel === targetText) {tag_filter}) {{'
            '      el.scrollIntoView({block: "center"});'
            '      el.click();'
            '      return true;'
            '    }'
            '  }'
            '  // Partial match fallback'
            '  for (const el of allElements) {'
            '    const elText = (el.textContent || el.innerText || "").trim().toLowerCase();'
            f'    if (elText.includes(targetText) && elText.length < targetText.length * 3 {tag_filter}) {{'
            '      el.scrollIntoView({block: "center"});'
            '      el.click();'
            '      return true;'
            '    }'
            '  }'
            '  return false;'
            '}',
            text,
        )
        return result == "true" or result is True

    async def _type_by_xpath(self, page, xpath: str, text: str, clear: bool) -> bool:
        """Find an element by XPath and type into it."""
        clear_js = '    el.value = "";' if clear else ""
        js_code = (
            '(...args) => {'
            '  const xpath = args[0];'
            '  const text = args[1];'
            '  const result = document.evaluate(xpath, document, null, '
            '    XPathResult.FIRST_ORDERED_NODE_TYPE, null);'
            '  const el = result.singleNodeValue;'
            '  if (el) {'
            + clear_js +
            '    el.focus();'
            '    el.value = text;'
            '    el.dispatchEvent(new Event("input", {bubbles: true}));'
            '    el.dispatchEvent(new Event("change", {bubbles: true}));'
            '    return true;'
            '  }'
            '  return false;'
            '}'
        )
        result = await page.evaluate(js_code, xpath, text)
        return result == "true" or result is True

    async def _wait_stable(self, session, timeout_ms: int = 2000):
        """Wait a moment for the page to stabilise after an action."""
        import asyncio
        await asyncio.sleep(min(timeout_ms / 1000.0, 2.0))

    def _extract_param_from_new_task(self, original_text: str) -> str:
        """
        6. Learned playbook generalization: extract equivalent parameter from new task.

        Handles common patterns: "search for X", "look up Y", "find Z", "X on Amazon",
        "price of X", etc. Uses keyword diff + phrase extraction for better substitution.
        """
        old_kw = set(extract_keywords(self._playbook.task))
        new_kw = set(extract_keywords(self._new_task))
        diff = new_kw - old_kw

        # Common patterns that indicate a parameter (query, search term, product, etc.)
        patterns = [
            r"search\s+for\s+(.+?)(?:\s+on|$|\.)",
            r"look\s+up\s+(.+?)(?:\s+on|$|\.)",
            r"find\s+(.+?)(?:\s+on|$|\.)",
            r"price\s+(?:of\s+)?(.+?)(?:\s+on|$|\.)",
            r"(.+?)\s+on\s+(?:amazon|ebay|google)",
            r"buy\s+(.+?)(?:\s+on|$|\.)",
            r"get\s+(.+?)(?:\s+on|$|\.)",
        ]
        new_lower = self._new_task.lower()
        for pat in patterns:
            m = re.search(pat, new_lower, re.I | re.DOTALL)
            if m:
                phrase = m.group(1).strip()
                if len(phrase) > 1 and len(phrase) < 80:
                    return phrase

        if diff:
            new_words = re.findall(r"[a-zA-Z0-9]+", self._new_task)
            ordered = [w for w in new_words if w.lower() in diff]
            if ordered:
                return " ".join(ordered)

        return original_text

    @staticmethod
    def _build_search_url(query: str, engine: str) -> str:
        """Build a search engine URL."""
        from urllib.parse import quote_plus
        q = quote_plus(query)
        if engine == "google":
            return f"https://www.google.com/search?q={q}"
        elif engine == "bing":
            return f"https://www.bing.com/search?q={q}"
        else:  # duckduckgo
            return f"https://duckduckgo.com/?q={q}"
