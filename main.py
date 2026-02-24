"""
main.py — MyBot Dock Interface
==============================
A frameless, always-on-top dock bar that sits at the bottom of the screen.
Styled with a Cyberpunk aesthetic (dark navy, neon cyan/blue accents).
Features: model selector, Chrome Extension bridge, persistent settings, shortcuts.

Created by AJ (Ahmed Jouini).
"""

import os
import sys
from pathlib import Path
import gc
import traceback

import psutil
from PyQt6.QtCore import (
    Qt,
    QTimer,
    QPropertyAnimation,
    QEasingCurve,
    QSettings,
    pyqtSlot,
)
from PyQt6.QtGui import (
    QGuiApplication,
    QFont,
    QColor,
    QIcon,
    QShortcut,
    QKeySequence,
    QTextCursor,
)
from PyQt6.QtWidgets import (
    QApplication,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLineEdit,
    QComboBox,
    QPushButton,
    QTextEdit,
    QLabel,
    QDialog,
    QFormLayout,
    QSpinBox,
    QCheckBox,
    QDialogButtonBox,
    QGraphicsDropShadowEffect,
)
from agent_logic import (
    AgentWorker,
    ModelFetchWorker,
    ModelWarmupWorker,
    ExtensionProbeWorker,
    BrowserSession,
    ModelPool,
    PROVIDER_OLLAMA,
    PROVIDER_OPENCODE,
    PROVIDER_OPENROUTER,
    PROVIDER_GOOGLE,
    PROVIDER_GROQ,
)
from extension_bridge import ExtensionBridge, DEFAULT_WS_PORT
from playbook import PlaybookStore


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║                        CYBERPUNK QSS STYLESHEET                            ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

STYLESHEET = """

/* ─── Global ────────────────────────────────────────────────────────────────── */

QWidget#DockBar {
    background: qlineargradient(
        x1:0, y1:0, x2:0, y2:1,
        stop:0   #0f0f23,
        stop:0.3 #0d0d1f,
        stop:1   #08081a
    );
    border-top: 2px solid qlineargradient(
        x1:0, y1:0, x2:1, y2:0,
        stop:0   #00d4ff00,
        stop:0.2 #00d4ff,
        stop:0.8 #00d4ff,
        stop:1   #00d4ff00
    );
}

QWidget#DockContainer {
    background: transparent;
}

/* ─── Prompt Input ──────────────────────────────────────────────────────────── */

QLineEdit#PromptInput {
    background-color: #12122a;
    color: #e8e8ff;
    border: 1px solid rgba(0, 212, 255, 0.25);
    border-radius: 12px;
    padding: 12px 20px;
    font-size: 15px;
    font-family: 'Segoe UI', 'Inter', 'Helvetica Neue', sans-serif;
    selection-background-color: #00d4ff;
    selection-color: #0d0d1a;
}

QLineEdit#PromptInput:focus {
    border: 1px solid #00d4ff;
    background-color: #16163a;
}

/* ─── Buttons ───────────────────────────────────────────────────────────────── */

QPushButton {
    background-color: rgba(0, 212, 255, 0.06);
    color: #00d4ff;
    border: 1px solid rgba(0, 212, 255, 0.2);
    border-radius: 8px;
    padding: 8px 16px;
    font-weight: 600;
    font-size: 12px;
    font-family: 'Segoe UI', 'Inter', sans-serif;
    min-width: 70px;
}

QPushButton:hover {
    background-color: rgba(0, 212, 255, 0.15);
    border: 1px solid rgba(0, 212, 255, 0.5);
    color: #33ddff;
}

QPushButton:pressed {
    background-color: rgba(0, 212, 255, 0.25);
}

QPushButton#StopBtn {
    color: #ff4477;
    border-color: rgba(255, 68, 119, 0.25);
}

QPushButton#StopBtn:hover {
    background-color: rgba(255, 68, 119, 0.15);
    border-color: rgba(255, 68, 119, 0.5);
    color: #ff6699;
}

QPushButton#StopBtn:disabled {
    color: #443344;
    border-color: rgba(255, 68, 119, 0.08);
    background-color: transparent;
}

/* ─── Toggle Log Button ────────────────────────────────────────────────────── */

QPushButton#ToggleLog {
    background: transparent;
    border: 1px solid rgba(0, 212, 255, 0.15);
    border-radius: 6px;
    color: #00d4ff88;
    font-size: 14px;
    min-width: 32px;
    max-width: 32px;
    min-height: 32px;
    max-height: 32px;
    padding: 0;
}

QPushButton#ToggleLog:hover {
    color: #00d4ff;
    border-color: rgba(0, 212, 255, 0.4);
    background: rgba(0, 212, 255, 0.08);
}

/* ─── Guidelines Pane ──────────────────────────────────────────────────────── */

QTextEdit#GuidelinesPane {
    background: #0d1117;
    color: #c9d1d9;
    border: none;
    border-top: 1px solid rgba(0, 212, 255, 0.25);
    font-family: 'Segoe UI', 'Consolas', monospace;
    font-size: 12px;
    padding: 8px 12px;
}

/* ─── Log / Chat Pane ───────────────────────────────────────────────────────── */

QTextEdit#LogPane {
    background-color: #08081a;
    color: #7af5ca;
    border: none;
    border-bottom: 1px solid rgba(0, 212, 255, 0.15);
    font-family: 'Cascadia Code', 'Fira Code', 'Consolas', monospace;
    font-size: 12px;
    padding: 12px 16px;
    line-height: 1.5;
}

/* ─── Status Label ──────────────────────────────────────────────────────────── */

QLabel#StatusLabel {
    color: #00d4ff;
    font-size: 11px;
    font-weight: 600;
    font-family: 'Cascadia Code', 'Consolas', monospace;
    padding: 0 8px;
    letter-spacing: 1px;
}

/* ─── RAM Monitor ───────────────────────────────────────────────────────────── */

QLabel#RamLabel {
    color: #444466;
    font-size: 10px;
    font-family: 'Cascadia Code', 'Consolas', monospace;
    padding-right: 4px;
}

/* ─── Branding ──────────────────────────────────────────────────────────────── */

QLabel#BrandLabel {
    color: #00d4ff;
    font-size: 13px;
    font-weight: 700;
    font-family: 'Segoe UI', 'Inter', sans-serif;
    letter-spacing: 2px;
    padding: 0 4px;
}

/* ─── Model Selector (dock bar combo) ──────────────────────────────────────── */

QComboBox#ModelSelector {
    background-color: #12122a;
    color: #e0e0ff;
    border: 1px solid rgba(0, 212, 255, 0.2);
    border-radius: 6px;
    padding: 4px 8px;
    font-size: 11px;
    font-family: 'Cascadia Code', 'Consolas', monospace;
    min-width: 140px;
    max-width: 200px;
}

QComboBox#ModelSelector:hover {
    border-color: rgba(0, 212, 255, 0.5);
}

QComboBox#ModelSelector::drop-down {
    subcontrol-origin: padding;
    subcontrol-position: top right;
    width: 18px;
    border-left-width: 0px;
}

QComboBox#ModelSelector QAbstractItemView {
    background-color: #12122a;
    color: #e0e0ff;
    border: 1px solid rgba(0, 212, 255, 0.3);
    selection-background-color: rgba(0, 212, 255, 0.2);
    selection-color: #00d4ff;
    font-size: 11px;
}

/* ─── Browser Mode Toggle ─────────────────────────────────────────────────── */

QPushButton#BrowserToggle {
    font-size: 11px;
    min-width: 50px;
    max-width: 100px;
    padding: 4px 8px;
    border-radius: 6px;
}

QPushButton#BrowserToggle[active="true"] {
    background-color: rgba(76, 175, 80, 0.15);
    border-color: rgba(76, 175, 80, 0.5);
    color: #4CAF50;
}

QPushButton#BrowserToggle[active="false"] {
    background-color: rgba(0, 212, 255, 0.06);
    border-color: rgba(0, 212, 255, 0.2);
    color: #666688;
}

/* ─── Minimize / Exit buttons ──────────────────────────────────────────────── */

QPushButton#MinimizeBtn {
    color: #00d4ff; border: 1px solid rgba(0, 212, 255, 0.3); background: transparent;
    min-width: 32px; max-width: 32px; font-size: 16px;
}
QPushButton#MinimizeBtn:hover {
    color: #00ffff; border-color: rgba(0, 255, 255, 0.6); background: rgba(0, 212, 255, 0.1);
}

QPushButton#ExitBtn {
    color: #aa4444; border: 1px solid #442222; background: transparent;
    min-width: 32px; max-width: 32px;
}
QPushButton#ExitBtn:hover {
    color: #ff5555; border-color: #ff5555; background: #2a1111;
}

/* ─── Separator ────────────────────────────────────────────────────────────── */

QWidget#DockSep {
    background: rgba(0, 212, 255, 0.15);
}

/* ─── Extension Status Dot ────────────────────────────────────────────────── */

QLabel#ExtStatus {
    font-size: 10px;
    font-family: 'Cascadia Code', 'Consolas', monospace;
    padding: 0 2px;
}

/* ─── Settings Dialog ───────────────────────────────────────────────────────── */

QDialog#SettingsDialog {
    background-color: #0d0d1f;
    border: 1px solid rgba(0, 212, 255, 0.3);
    border-radius: 12px;
}

QDialog QLabel {
    color: #8888aa;
    font-size: 12px;
    font-family: 'Segoe UI', sans-serif;
}

QDialog QLineEdit {
    background-color: #12122a;
    color: #e0e0ff;
    border: 1px solid rgba(0, 212, 255, 0.2);
    border-radius: 6px;
    padding: 6px 10px;
    font-size: 13px;
}

QDialog QSpinBox {
    background-color: #12122a;
    color: #e0e0ff;
    border: 1px solid rgba(0, 212, 255, 0.2);
    border-radius: 6px;
    padding: 6px 10px;
    font-size: 13px;
}

QDialog QPushButton {
    min-width: 80px;
    padding: 8px 20px;
}

QComboBox {
    background-color: #12122a;
    color: #e0e0ff;
    border: 1px solid rgba(0, 212, 255, 0.2);
    border-radius: 6px;
    padding: 6px 10px;
    font-size: 13px;
}

QComboBox::drop-down {
    subcontrol-origin: padding;
    subcontrol-position: top right;
    width: 20px;
    border-left-width: 0px;
}


/* ─── Checkbox (Settings) ──────────────────────────────────────────────────── */

QCheckBox {
    color: #aaaacc;
    font-size: 12px;
    spacing: 6px;
}

QCheckBox::indicator {
    width: 16px;
    height: 16px;
    border: 1px solid rgba(0, 212, 255, 0.3);
    border-radius: 3px;
    background: #12122a;
}

QCheckBox::indicator:checked {
    background: rgba(0, 212, 255, 0.3);
    border-color: #00d4ff;
}

/* ─── Scrollbar ─────────────────────────────────────────────────────────────── */

QScrollBar:vertical {
    background: #08081a;
    width: 8px;
    margin: 0;
}

QScrollBar::handle:vertical {
    background: rgba(0, 212, 255, 0.2);
    border-radius: 4px;
    min-height: 30px;
}

QScrollBar::handle:vertical:hover {
    background: rgba(0, 212, 255, 0.4);
}

QScrollBar::add-line:vertical,
QScrollBar::sub-line:vertical {
    height: 0;
}

QScrollBar::add-page:vertical,
QScrollBar::sub-page:vertical {
    background: transparent;
}
"""

# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║                          LIGHT THEME STYLESHEET                             ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

STYLESHEET_LIGHT = """

/* ─── Global ────────────────────────────────────────────────────────────────── */

QWidget#DockBar {
    background: qlineargradient(
        x1:0, y1:0, x2:0, y2:1,
        stop:0   #f0f4f8,
        stop:0.3 #e8eef4,
        stop:1   #dfe6ed
    );
    border-top: 2px solid qlineargradient(
        x1:0, y1:0, x2:1, y2:0,
        stop:0   #0077b600,
        stop:0.2 #0077b6,
        stop:0.8 #0077b6,
        stop:1   #0077b600
    );
}

QWidget#DockContainer {
    background: transparent;
}

/* ─── Prompt Input ──────────────────────────────────────────────────────────── */

QLineEdit#PromptInput {
    background-color: #ffffff;
    color: #1a1a2e;
    border: 1px solid #b8c4ce;
    border-radius: 12px;
    padding: 12px 20px;
    font-size: 15px;
    font-family: 'Segoe UI', 'Inter', 'Helvetica Neue', sans-serif;
    selection-background-color: #0077b6;
    selection-color: #ffffff;
}

QLineEdit#PromptInput:focus {
    border: 1px solid #0077b6;
    background-color: #ffffff;
}

/* ─── Buttons ───────────────────────────────────────────────────────────────── */

QPushButton {
    background-color: rgba(0, 119, 182, 0.08);
    color: #0077b6;
    border: 1px solid rgba(0, 119, 182, 0.35);
    border-radius: 8px;
    padding: 8px 16px;
    font-weight: 600;
    font-size: 12px;
    font-family: 'Segoe UI', 'Inter', sans-serif;
    min-width: 70px;
}

QPushButton:hover {
    background-color: rgba(0, 119, 182, 0.15);
    border: 1px solid rgba(0, 119, 182, 0.6);
    color: #005f8f;
}

QPushButton:pressed {
    background-color: rgba(0, 119, 182, 0.25);
}

QPushButton#StopBtn {
    color: #c0392b;
    border-color: rgba(192, 57, 43, 0.4);
}

QPushButton#StopBtn:hover {
    background-color: rgba(192, 57, 43, 0.1);
    border-color: rgba(192, 57, 43, 0.6);
    color: #a93226;
}

QPushButton#StopBtn:disabled {
    color: #aaa;
    border-color: rgba(192, 57, 43, 0.15);
    background-color: transparent;
}

/* ─── Toggle Log Button ────────────────────────────────────────────────────── */

QPushButton#ToggleLog {
    background: transparent;
    border: 1px solid rgba(0, 119, 182, 0.3);
    border-radius: 6px;
    color: #0077b6aa;
    font-size: 14px;
    min-width: 32px;
    max-width: 32px;
    min-height: 32px;
    max-height: 32px;
    padding: 0;
}

QPushButton#ToggleLog:hover {
    color: #0077b6;
    border-color: rgba(0, 119, 182, 0.5);
    background: rgba(0, 119, 182, 0.08);
}

/* ─── Guidelines Pane ──────────────────────────────────────────────────────── */

QTextEdit#GuidelinesPane {
    background: #f1f3f5;
    color: #2d3748;
    border: none;
    border-top: 1px solid #b8c4ce;
    font-family: 'Segoe UI', 'Consolas', monospace;
    font-size: 12px;
    padding: 8px 12px;
}

/* ─── Log / Chat Pane ───────────────────────────────────────────────────────── */

QTextEdit#LogPane {
    background-color: #f8f9fa;
    color: #2d3748;
    border: none;
    border-bottom: 1px solid #e2e8f0;
    font-family: 'Cascadia Code', 'Fira Code', 'Consolas', monospace;
    font-size: 12px;
    padding: 12px 16px;
    line-height: 1.5;
}

/* ─── Status Label ──────────────────────────────────────────────────────────── */

QLabel#StatusLabel {
    color: #0077b6;
    font-size: 11px;
    font-weight: 600;
    font-family: 'Cascadia Code', 'Consolas', monospace;
    padding: 0 8px;
    letter-spacing: 1px;
}

/* ─── RAM Monitor ───────────────────────────────────────────────────────────── */

QLabel#RamLabel {
    color: #6c757d;
    font-size: 10px;
    font-family: 'Cascadia Code', 'Consolas', monospace;
    padding-right: 4px;
}

/* ─── Branding ──────────────────────────────────────────────────────────────── */

QLabel#BrandLabel {
    color: #0077b6;
    font-size: 13px;
    font-weight: 700;
    font-family: 'Segoe UI', 'Inter', sans-serif;
    letter-spacing: 2px;
    padding: 0 4px;
}

/* ─── Model Selector ──────────────────────────────────────────────────────── */

QComboBox#ModelSelector {
    background-color: #ffffff;
    color: #1a1a2e;
    border: 1px solid #b8c4ce;
    border-radius: 6px;
    padding: 4px 8px;
    font-size: 11px;
    font-family: 'Cascadia Code', 'Consolas', monospace;
    min-width: 140px;
    max-width: 200px;
}

QComboBox#ModelSelector:hover {
    border-color: #0077b6;
}

QComboBox#ModelSelector::drop-down {
    subcontrol-origin: padding;
    subcontrol-position: top right;
    width: 18px;
    border-left-width: 0px;
}

QComboBox#ModelSelector QAbstractItemView {
    background-color: #ffffff;
    color: #1a1a2e;
    border: 1px solid #b8c4ce;
    selection-background-color: rgba(0, 119, 182, 0.15);
    selection-color: #0077b6;
    font-size: 11px;
}

/* ─── Browser Mode Toggle ─────────────────────────────────────────────────── */

QPushButton#BrowserToggle {
    font-size: 11px;
    min-width: 50px;
    max-width: 100px;
    padding: 4px 8px;
    border-radius: 6px;
}

QPushButton#BrowserToggle[active="true"] {
    background-color: rgba(76, 175, 80, 0.15);
    border-color: rgba(76, 175, 80, 0.6);
    color: #2e7d32;
}

QPushButton#BrowserToggle[active="false"] {
    background-color: rgba(0, 119, 182, 0.06);
    border-color: rgba(0, 119, 182, 0.25);
    color: #6c757d;
}

/* ─── Minimize / Exit buttons ──────────────────────────────────────────────── */

QPushButton#MinimizeBtn {
    color: #0077b6; border: 1px solid rgba(0, 119, 182, 0.4); background: transparent;
    min-width: 32px; max-width: 32px; font-size: 16px;
}
QPushButton#MinimizeBtn:hover {
    color: #005f8f; border-color: rgba(0, 119, 182, 0.7); background: rgba(0, 119, 182, 0.1);
}

QPushButton#ExitBtn {
    color: #c0392b; border: 1px solid #d5a5a5; background: transparent;
    min-width: 32px; max-width: 32px;
}
QPushButton#ExitBtn:hover {
    color: #a93226; border-color: #c0392b; background: #fdeaea;
}

/* ─── Separator ────────────────────────────────────────────────────────────── */

QWidget#DockSep {
    background: rgba(0, 119, 182, 0.2);
}

/* ─── Extension Status ────────────────────────────────────────────────────── */

QLabel#ExtStatus {
    font-size: 10px;
    font-family: 'Cascadia Code', 'Consolas', monospace;
    padding: 0 2px;
}

/* ─── Settings Dialog ───────────────────────────────────────────────────────── */

QDialog#SettingsDialog {
    background-color: #ffffff;
    border: 1px solid #b8c4ce;
    border-radius: 12px;
}

QDialog QLabel {
    color: #495057;
    font-size: 12px;
    font-family: 'Segoe UI', sans-serif;
}

QDialog QLineEdit {
    background-color: #ffffff;
    color: #1a1a2e;
    border: 1px solid #b8c4ce;
    border-radius: 6px;
    padding: 6px 10px;
    font-size: 13px;
}

QDialog QSpinBox {
    background-color: #ffffff;
    color: #1a1a2e;
    border: 1px solid #b8c4ce;
    border-radius: 6px;
    padding: 6px 10px;
    font-size: 13px;
}

QDialog QPushButton {
    min-width: 80px;
    padding: 8px 20px;
}

QComboBox {
    background-color: #ffffff;
    color: #1a1a2e;
    border: 1px solid #b8c4ce;
    border-radius: 6px;
    padding: 6px 10px;
    font-size: 13px;
}

QComboBox::drop-down {
    subcontrol-origin: padding;
    subcontrol-position: top right;
    width: 20px;
    border-left-width: 0px;
}

/* ─── Checkbox ──────────────────────────────────────────────────────────────── */

QCheckBox {
    color: #495057;
    font-size: 12px;
    spacing: 6px;
}

QCheckBox::indicator {
    width: 16px;
    height: 16px;
    border: 1px solid #b8c4ce;
    border-radius: 3px;
    background: #ffffff;
}

QCheckBox::indicator:checked {
    background: rgba(0, 119, 182, 0.25);
    border-color: #0077b6;
}

/* ─── Scrollbar ─────────────────────────────────────────────────────────────── */

QScrollBar:vertical {
    background: #f1f3f5;
    width: 8px;
    margin: 0;
}

QScrollBar::handle:vertical {
    background: #adb5bd;
    border-radius: 4px;
    min-height: 30px;
}

QScrollBar::handle:vertical:hover {
    background: #868e96;
}

QScrollBar::add-line:vertical,
QScrollBar::sub-line:vertical {
    height: 0;
}

QScrollBar::add-page:vertical,
QScrollBar::sub-page:vertical {
    background: transparent;
}
"""

THEME_DARK = "dark"
THEME_LIGHT = "light"


def get_stylesheet(theme: str) -> str:
    """Return the stylesheet for the given theme."""
    return STYLESHEET_LIGHT if theme == THEME_LIGHT else STYLESHEET


def get_theme_colors(theme: str) -> dict:
    """Return accent/muted colors for inline styling based on theme."""
    if theme == THEME_LIGHT:
        return {
            "accent": "#0077b6",
            "accent_rgba": "0, 119, 182",
            "muted": "#6c757d",
            "header": "#1e3a5f",
        }
    return {
        "accent": "#00d4ff",
        "accent_rgba": "0, 212, 255",
        "muted": "#666688",
        "header": "#00d4ff",
    }


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║                         SETTINGS CONSTANTS                                  ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

SETTINGS_ORG = "MyBot"
SETTINGS_APP = "MyBot"
DEFAULT_MODEL = "minicpm-v"
DEFAULT_RAM_THRESHOLD = 85


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║                          SETTINGS DIALOG                                    ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

class SettingsDialog(QDialog):
    """Pop-up dialog for RAM threshold, playbooks, API keys, theme, and other preferences."""

    def __init__(
        self,
        ram_threshold: int,
        use_playbooks: bool = True,
        playbook_count: int = 0,
        opencode_api_key: str = "",
        openrouter_api_key: str = "",
        google_api_key: str = "",
        groq_api_key: str = "",
        theme: str = THEME_DARK,
        parent=None,
    ):
        super().__init__(parent)
        self.setObjectName("SettingsDialog")
        self.setWindowTitle("⚙  Settings")
        self.setFixedSize(440, 660)
        self._theme = theme
        self.setStyleSheet(get_stylesheet(theme))
        self._colors = get_theme_colors(theme)

        layout = QFormLayout(self)
        layout.setSpacing(14)
        layout.setContentsMargins(28, 24, 28, 24)

        # ── Playbooks section ─────────────────────────────────────────────
        playbook_header = QLabel("PLAYBOOKS")
        playbook_header.setStyleSheet(
            f"color: {self._colors['accent']}; font-size: 10px; font-weight: 700; "
            "letter-spacing: 2px; padding-top: 4px;"
        )
        layout.addRow(playbook_header)

        self.playbooks_checkbox = QCheckBox(
            f"Enable playbook learning  ({playbook_count} saved)"
        )
        self.playbooks_checkbox.setChecked(use_playbooks)
        self.playbooks_checkbox.setToolTip(
            "When enabled, successful tasks are recorded and replayed\n"
            "for similar future requests — saving time and API costs."
        )
        self.playbooks_checkbox.setStyleSheet(f"color: {self._colors['muted']}; font-size: 12px;")
        layout.addRow(self.playbooks_checkbox)

        self.clear_playbooks_btn = QPushButton(f"Clear All Playbooks ({playbook_count})")
        self.clear_playbooks_btn.setToolTip("Delete all saved playbooks")
        self.clear_playbooks_btn.setStyleSheet(
            "color: #ff6677; border-color: rgba(255, 68, 119, 0.3); "
            "font-size: 11px; padding: 6px 12px;"
        )
        self._playbooks_cleared = False
        self.clear_playbooks_btn.clicked.connect(self._on_clear_playbooks)
        layout.addRow("", self.clear_playbooks_btn)

        # ── OpenCode Zen section ───────────────────────────────────────────
        opencode_header = QLabel("OPENCODE ZEN")
        opencode_header.setStyleSheet(
            f"color: {self._colors['accent']}; font-size: 10px; font-weight: 700; "
            "letter-spacing: 2px; padding-top: 8px;"
        )
        layout.addRow(opencode_header)

        self.opencode_key_edit = QLineEdit()
        self.opencode_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.opencode_key_edit.setPlaceholderText("API key from opencode.ai/auth")
        self.opencode_key_edit.setText(opencode_api_key)
        layout.addRow("API Key:", self.opencode_key_edit)

        opencode_link = QLabel(
            f'<a href="https://opencode.ai/auth" style="color:{self._colors["accent"]};">Get key at opencode.ai/auth</a>'
        )
        opencode_link.setOpenExternalLinks(True)
        opencode_link.setStyleSheet(f"font-size: 10px; color: {self._colors['muted']};")
        layout.addRow("", opencode_link)

        # ── OpenRouter section ────────────────────────────────────────────
        openrouter_header = QLabel("OPENROUTER")
        openrouter_header.setStyleSheet(
            f"color: {self._colors['accent']}; font-size: 10px; font-weight: 700; "
            "letter-spacing: 2px; padding-top: 8px;"
        )
        layout.addRow(openrouter_header)

        self.openrouter_key_edit = QLineEdit()
        self.openrouter_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.openrouter_key_edit.setPlaceholderText("API key from openrouter.ai/keys")
        self.openrouter_key_edit.setText(openrouter_api_key)
        layout.addRow("API Key:", self.openrouter_key_edit)

        openrouter_link = QLabel(
            f'<a href="https://openrouter.ai/keys" style="color:{self._colors["accent"]};">Get key at openrouter.ai/keys</a>'
        )
        openrouter_link.setOpenExternalLinks(True)
        openrouter_link.setStyleSheet(f"font-size: 10px; color: {self._colors['muted']};")
        layout.addRow("", openrouter_link)

        # ── Google Gemini section ─────────────────────────────────────────
        google_header = QLabel("GOOGLE GEMINI (Free tier)")
        google_header.setStyleSheet(
            f"color: {self._colors['accent']}; font-size: 10px; font-weight: 700; "
            "letter-spacing: 2px; padding-top: 8px;"
        )
        layout.addRow(google_header)

        self.google_key_edit = QLineEdit()
        self.google_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.google_key_edit.setPlaceholderText("API key from aistudio.google.com/app/apikey")
        self.google_key_edit.setText(google_api_key)
        layout.addRow("API Key:", self.google_key_edit)

        google_link = QLabel(
            f'<a href="https://aistudio.google.com/app/apikey" style="color:{self._colors["accent"]};">Get free key at aistudio.google.com</a>'
        )
        google_link.setOpenExternalLinks(True)
        google_link.setStyleSheet(f"font-size: 10px; color: {self._colors['muted']};")
        layout.addRow("", google_link)

        # ── Groq section ──────────────────────────────────────────────────
        groq_header = QLabel("GROQ (Free tier — fast Llama)")
        groq_header.setStyleSheet(
            f"color: {self._colors['accent']}; font-size: 10px; font-weight: 700; "
            "letter-spacing: 2px; padding-top: 8px;"
        )
        layout.addRow(groq_header)

        self.groq_key_edit = QLineEdit()
        self.groq_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.groq_key_edit.setPlaceholderText("API key from console.groq.com/keys")
        self.groq_key_edit.setText(groq_api_key)
        layout.addRow("API Key:", self.groq_key_edit)

        groq_link = QLabel(
            f'<a href="https://console.groq.com/keys" style="color:{self._colors["accent"]};">Get free key at console.groq.com</a>'
        )
        groq_link.setOpenExternalLinks(True)
        groq_link.setStyleSheet(f"font-size: 10px; color: {self._colors['muted']};")
        layout.addRow("", groq_link)

        # ── General section ───────────────────────────────────────────────
        general_header = QLabel("GENERAL")
        general_header.setStyleSheet(
            f"color: {self._colors['accent']}; font-size: 10px; font-weight: 700; "
            "letter-spacing: 2px; padding-top: 8px;"
        )
        layout.addRow(general_header)

        # Theme
        self.theme_combo = QComboBox()
        self.theme_combo.addItem("Dark", THEME_DARK)
        self.theme_combo.addItem("Light", THEME_LIGHT)
        idx = self.theme_combo.findData(theme)
        if idx >= 0:
            self.theme_combo.setCurrentIndex(idx)
        self.theme_combo.setToolTip("App color theme")
        layout.addRow("Theme:", self.theme_combo)

        # RAM threshold
        self.ram_spin = QSpinBox()
        self.ram_spin.setRange(50, 95)
        self.ram_spin.setSuffix(" %")
        self.ram_spin.setValue(ram_threshold)
        layout.addRow("RAM alert threshold:", self.ram_spin)

        # OK / Cancel
        btn_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        btn_box.accepted.connect(self.accept)
        btn_box.rejected.connect(self.reject)
        layout.addRow(btn_box)

    def _on_clear_playbooks(self):
        """Clear all saved playbooks."""
        try:
            store = PlaybookStore()
            for entry in store.list_all():
                store.delete(entry["id"])
            self._playbooks_cleared = True
            self.clear_playbooks_btn.setText("✓ Cleared!")
            self.clear_playbooks_btn.setEnabled(False)
            self.playbooks_checkbox.setText("Enable playbook learning  (0 saved)")
        except Exception as e:
            self.clear_playbooks_btn.setText(f"Error: {e}")


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║                          DOCK WINDOW (MAIN UI)                              ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

DOCK_HEIGHT = 100
LOG_EXPANDED_HEIGHT = 360
CORNER_BUBBLE_SIZE = 44
CORNER_BUBBLE_MARGIN = 12


class CornerBubble(QPushButton):
    """
    Small circular floating button in the screen corner. Click to restore the dock.
    """

    _STYLE_DARK = """
        QPushButton {
            background: qradialgradient(cx:0.5, cy:0.5, radius:0.9,
                stop:0 #1a2a3a, stop:0.8 #0d1a28, stop:1 #080c14);
            color: #00d4ff;
            border: 1px solid rgba(0, 212, 255, 0.5);
            border-radius: 22px;
            font-size: 14px; font-weight: bold;
        }
        QPushButton:hover {
            background: qradialgradient(cx:0.5, cy:0.5, radius:0.9,
                stop:0 #2a3a4a, stop:0.8 #1d2a38, stop:1 #0d1418);
            border-color: #00d4ff;
        }
    """
    _STYLE_LIGHT = """
        QPushButton {
            background: qradialgradient(cx:0.5, cy:0.5, radius:0.9,
                stop:0 #e8eef4, stop:0.8 #dce4ed, stop:1 #d0dae6);
            color: #0077b6;
            border: 1px solid rgba(0, 119, 182, 0.5);
            border-radius: 22px;
            font-size: 14px; font-weight: bold;
        }
        QPushButton:hover {
            background: qradialgradient(cx:0.5, cy:0.5, radius:0.9,
                stop:0 #f0f4f8, stop:0.8 #e4eaef, stop:1 #d8e0e8);
            border-color: #0077b6;
        }
    """

    def __init__(self, dock: "DockWindow", parent=None):
        super().__init__(parent)
        self._dock = dock
        self.setText("MB")
        self.setFixedSize(CORNER_BUBBLE_SIZE, CORNER_BUBBLE_SIZE)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Click to restore MyBot")
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.set_theme(dock.theme)
        self.clicked.connect(self._on_restore)

    def set_theme(self, theme: str):
        self.setStyleSheet(
            self._STYLE_LIGHT if theme == THEME_LIGHT else self._STYLE_DARK
        )

    def _on_restore(self):
        self.hide()
        self._dock.show()
        self._dock.raise_()
        self._dock.activateWindow()

    def place_in_corner(self):
        screen = QGuiApplication.primaryScreen()
        geo = screen.availableGeometry()
        x = geo.x() + geo.width() - CORNER_BUBBLE_SIZE - CORNER_BUBBLE_MARGIN
        y = geo.y() + geo.height() - CORNER_BUBBLE_SIZE - CORNER_BUBBLE_MARGIN
        self.move(x, y)


class DockWindow(QWidget):
    """
    Frameless dock bar pinned to the bottom of the primary screen.
    Contains a prompt input, model selector, browser mode toggle (Chromium /
    Chrome Extension), control buttons, a collapsible log pane, RAM monitor,
    and wires up to the AgentWorker thread.
    """

    def __init__(self):
        super().__init__()

        # ── Load persistent settings ──────────────────────────────────────
        self._settings = QSettings(SETTINGS_ORG, SETTINGS_APP)
        self.model_name = self._settings.value("model_name", DEFAULT_MODEL, str)
        self.ram_threshold = self._settings.value("ram_threshold", DEFAULT_RAM_THRESHOLD, int)
        self.use_playbooks = self._settings.value("use_playbooks", True, bool)
        self.use_extension = self._settings.value("use_extension", False, bool)
        self.custom_guidelines = self._settings.value("custom_guidelines", "", str)
        p = self._settings.value("provider", PROVIDER_OLLAMA, str)
        self.provider = str(p or "").lower().strip() or PROVIDER_OLLAMA
        self.theme = self._settings.value("theme", THEME_DARK, str)
        if self.theme not in (THEME_DARK, THEME_LIGHT):
            self.theme = THEME_DARK
        self.opencode_api_key = self._settings.value("opencode_api_key", "", str)
        self.openrouter_api_key = self._settings.value("openrouter_api_key", "", str)
        self.google_api_key = self._settings.value("google_api_key", "", str)
        self.groq_api_key = self._settings.value("groq_api_key", "", str)

        # ── State ──────────────────────────────────────────────────────────
        self._agent_worker: AgentWorker | None = None
        self._model_fetch_worker: ModelFetchWorker | None = None
        self._model_warmup_worker: ModelWarmupWorker | None = None
        self._ext_probe_worker: ExtensionProbeWorker | None = None
        self._log_visible = False
        self._current_anim = None
        self._ext_connected = False
        self._ext_last_log_state: bool | None = None  # for debouncing log spam
        self._ext_debounce_timer: QTimer | None = None

        # ── Corner bubble (minimize-to-corner) ────────────────────────────
        self._corner_bubble = CornerBubble(self)

        # ── Extension bridge (starts immediately, always listening) ───────
        self._bridge = ExtensionBridge(
            port=DEFAULT_WS_PORT,
            on_connect=self._on_ext_connected_callback,
            on_disconnect=self._on_ext_disconnected_callback,
        )
        self._bridge.start()

        # ── Build UI ───────────────────────────────────────────────────────
        self._setup_window()
        self._build_layout()
        self._apply_effects()
        self._setup_shortcuts()
        self._start_timers()

        # ── Load models on startup (non-blocking) ─────────────────────────
        self._refresh_models()

    # ── Window chrome ──────────────────────────────────────────────────────

    def _setup_window(self):
        self.setObjectName("DockContainer")
        self.setStyleSheet(get_stylesheet(self.theme))

        icon_path = Path(__file__).parent / "assets" / "icon.png"
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))

        self.setWindowFlags(

            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)

        screen = QGuiApplication.primaryScreen()
        geo = screen.availableGeometry()
        self._screen_geo = geo

        self.setFixedSize(geo.width(), DOCK_HEIGHT)
        self.move(geo.x(), geo.y() + geo.height() - DOCK_HEIGHT)

    # ── Layout ─────────────────────────────────────────────────────────────

    def _build_layout(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Log pane (initially collapsed) ─────────────────────────────────
        self.log_pane = QTextEdit()
        self.log_pane.setObjectName("LogPane")
        self.log_pane.setReadOnly(True)
        self.log_pane.setMaximumHeight(0)
        self.log_pane.setMinimumHeight(0)
        root.addWidget(self.log_pane)

        # ── Guidelines pane (initially collapsed) ─────────────────────────
        self.guidelines_pane = QTextEdit()
        self.guidelines_pane.setObjectName("GuidelinesPane")
        self.guidelines_pane.setReadOnly(False)
        self.guidelines_pane.setPlaceholderText(
            "Custom model guidelines (e.g. \"Be concise\", \"Don't reason more than once\", "
            "\"Always use done when the answer is visible\")…"
        )
        self.guidelines_pane.setMaximumHeight(0)
        self.guidelines_pane.setMinimumHeight(0)
        if self.custom_guidelines:
            self.guidelines_pane.setPlainText(self.custom_guidelines)
        self.guidelines_pane.textChanged.connect(self._on_guidelines_changed)
        self._guidelines_visible = False
        root.addWidget(self.guidelines_pane)

        # ── Dock bar ───────────────────────────────────────────────────────
        bar = QWidget()
        bar.setObjectName("DockBar")
        bar.setFixedHeight(DOCK_HEIGHT)

        bar_layout = QHBoxLayout(bar)
        bar_layout.setContentsMargins(16, 0, 16, 0)
        bar_layout.setSpacing(8)

        # Brand
        brand = QLabel("AI")
        brand.setObjectName("BrandLabel")
        bar_layout.addWidget(brand)

        bar_layout.addWidget(self._make_sep(1, 32))

        # Toggle log button
        self.toggle_btn = QPushButton("▲")
        self.toggle_btn.setObjectName("ToggleLog")
        self.toggle_btn.setToolTip("Toggle log panel  (Ctrl+L)")
        self.toggle_btn.clicked.connect(self._toggle_log)
        bar_layout.addWidget(self.toggle_btn)

        # Status
        self.status_label = QLabel("READY")
        self.status_label.setObjectName("StatusLabel")
        self.status_label.setFixedWidth(140)
        bar_layout.addWidget(self.status_label)

        bar_layout.addWidget(self._make_sep(1, 24))

        # ── Browser mode toggle ───────────────────────────────────────────
        self.browser_toggle_btn = QPushButton("Chromium")
        self.browser_toggle_btn.setObjectName("BrowserToggle")
        self.browser_toggle_btn.setToolTip(
            "Toggle between built-in Chromium and your Chrome (via extension)"
        )
        self.browser_toggle_btn.setCheckable(True)
        self.browser_toggle_btn.setChecked(self.use_extension)
        self.browser_toggle_btn.clicked.connect(self._on_browser_toggle)
        self._update_browser_toggle_style()
        bar_layout.addWidget(self.browser_toggle_btn)

        # Extension status dot
        self.ext_status_label = QLabel("●")
        self.ext_status_label.setObjectName("ExtStatus")
        self.ext_status_label.setToolTip("Extension connection status")
        self._update_ext_status_display(False, "Not connected")
        self.ext_status_label.setVisible(self.use_extension)
        bar_layout.addWidget(self.ext_status_label)

        # Install extension button
        self.ext_install_btn = QPushButton("📦")
        self.ext_install_btn.setObjectName("ExtInstall")
        self.ext_install_btn.setToolTip("Install the Chrome bridge extension")
        self.ext_install_btn.setFixedSize(32, 32)
        self.ext_install_btn.setStyleSheet("""
            QPushButton { min-width: 32px; max-width: 32px; padding: 0; font-size: 14px; }
        """)
        self.ext_install_btn.clicked.connect(self._on_install_extension)
        self.ext_install_btn.setVisible(self.use_extension)
        bar_layout.addWidget(self.ext_install_btn)

        bar_layout.addWidget(self._make_sep(1, 24))

        # ── Provider selector ──────────────────────────────────────────────
        self.provider_combo = QComboBox()
        self.provider_combo.setObjectName("ModelSelector")
        self.provider_combo.addItem("Ollama (local)", PROVIDER_OLLAMA)
        self.provider_combo.addItem("Google Gemini", PROVIDER_GOOGLE)
        self.provider_combo.addItem("Groq", PROVIDER_GROQ)
        self.provider_combo.addItem("OpenRouter", PROVIDER_OPENROUTER)
        self.provider_combo.addItem("OpenCode Zen", PROVIDER_OPENCODE)
        valid = (PROVIDER_OLLAMA, PROVIDER_OPENCODE, PROVIDER_OPENROUTER, PROVIDER_GOOGLE, PROVIDER_GROQ)
        if self.provider not in valid:
            self.provider = PROVIDER_OLLAMA
        idx = self.provider_combo.findData(self.provider)
        if idx >= 0:
            self.provider_combo.blockSignals(True)
            self.provider_combo.setCurrentIndex(idx)
            self.provider_combo.blockSignals(False)
        else:
            self.provider_combo.setCurrentIndex(0)
            self.provider = PROVIDER_OLLAMA
        self.provider_combo.setToolTip(
            "AI provider: Ollama, Google Gemini, Groq, OpenRouter, or OpenCode Zen"
        )
        self.provider_combo.currentIndexChanged.connect(self._on_provider_index_changed)
        bar_layout.addWidget(self.provider_combo)

        # ── Model selector ─────────────────────────────────────────────────
        self.model_combo = QComboBox()
        self.model_combo.setObjectName("ModelSelector")
        self.model_combo.setEditable(True)
        self.model_combo.setToolTip("Select AI model")
        self.model_combo.addItem(self.model_name)
        self.model_combo.setCurrentText(self.model_name)
        self.model_combo.currentTextChanged.connect(self._on_model_changed)
        bar_layout.addWidget(self.model_combo)

        # Refresh models button
        refresh_btn = QPushButton("↻")
        refresh_btn.setToolTip("Refresh available models")
        refresh_btn.setFixedSize(32, 32)
        refresh_btn.setStyleSheet("""
            QPushButton { min-width: 32px; max-width: 32px; padding: 0; font-size: 14px; }
        """)
        refresh_btn.clicked.connect(self._refresh_models)
        bar_layout.addWidget(refresh_btn)

        bar_layout.addWidget(self._make_sep(1, 24))

        # ── Prompt input ───────────────────────────────────────────────────
        self.prompt_input = QLineEdit()
        self.prompt_input.setObjectName("PromptInput")
        self.prompt_input.setPlaceholderText("Ask the browser to do something…")
        self.prompt_input.returnPressed.connect(self._on_submit)
        bar_layout.addWidget(self.prompt_input, stretch=1)

        # Stop
        self.stop_btn = QPushButton("■  Stop")
        self.stop_btn.setObjectName("StopBtn")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self._on_stop)
        bar_layout.addWidget(self.stop_btn)

        # Clear
        clear_btn = QPushButton("✕  Clear")
        clear_btn.setToolTip("Clear log  (Ctrl+Shift+L)")
        clear_btn.clicked.connect(self._on_clear)
        bar_layout.addWidget(clear_btn)

        # Guidelines toggle
        self.guidelines_btn = QPushButton("📋")
        self.guidelines_btn.setToolTip("Toggle custom model guidelines  (Ctrl+G)")
        self.guidelines_btn.setFixedSize(32, 32)
        self.guidelines_btn.setStyleSheet("""
            QPushButton { min-width: 32px; max-width: 32px; padding: 0; font-size: 14px; }
        """)
        self.guidelines_btn.clicked.connect(self._toggle_guidelines)
        bar_layout.addWidget(self.guidelines_btn)

        # Help
        help_btn = QPushButton("?")
        help_btn.setObjectName("HelpBtn")
        help_btn.setToolTip("Help & Setup Guide  (Ctrl+H)")
        help_btn.setFixedSize(32, 32)
        help_btn.setStyleSheet("""
            QPushButton { min-width: 32px; max-width: 32px; padding: 0; font-size: 14px; }
        """)
        help_btn.clicked.connect(self._on_help)
        bar_layout.addWidget(help_btn)

        # Settings
        settings_btn = QPushButton("⚙")
        settings_btn.setToolTip("Settings")
        settings_btn.setFixedSize(32, 32)
        settings_btn.setStyleSheet("""
            QPushButton { min-width: 32px; max-width: 32px; padding: 0; font-size: 14px; }
        """)
        settings_btn.clicked.connect(self._on_settings)
        bar_layout.addWidget(settings_btn)

        # Minimize to corner
        self.minimize_btn = QPushButton("◯")
        self.minimize_btn.setObjectName("MinimizeBtn")
        self.minimize_btn.setToolTip("Minimize to corner  (small floating button)")
        self.minimize_btn.setFixedSize(32, 32)
        self.minimize_btn.clicked.connect(self._minimize_to_corner)
        bar_layout.addWidget(self.minimize_btn)

        # RAM label
        self.ram_label = QLabel("")
        self.ram_label.setObjectName("RamLabel")
        bar_layout.addWidget(self.ram_label)

        bar_layout.addWidget(self._make_sep(1, 24))

        # Exit
        exit_btn = QPushButton("✕")
        exit_btn.setObjectName("ExitBtn")
        exit_btn.setToolTip("Close Dock  (Escape)")
        exit_btn.setFixedSize(32, 32)
        exit_btn.clicked.connect(self.close)
        bar_layout.addWidget(exit_btn)

        root.addWidget(bar)

    def _make_sep(self, w: int, h: int) -> QWidget:
        sep = QWidget()
        sep.setObjectName("DockSep")
        sep.setFixedSize(w, h)
        return sep

    # ── Visual effects ─────────────────────────────────────────────────────

    def _apply_effects(self):
        glow = QGraphicsDropShadowEffect(self.prompt_input)
        glow.setBlurRadius(24)
        glow.setColor(QColor(0, 212, 255, 50))
        glow.setOffset(0, 0)
        self.prompt_input.setGraphicsEffect(glow)

    # ── Keyboard shortcuts ─────────────────────────────────────────────────

    def _setup_shortcuts(self):
        QShortcut(QKeySequence(Qt.Key.Key_Escape), self, self.close)
        QShortcut(QKeySequence("Ctrl+L"), self, self._toggle_log)
        QShortcut(QKeySequence("Ctrl+Shift+L"), self, self._on_clear)
        QShortcut(QKeySequence("Ctrl+/"), self, self.prompt_input.setFocus)
        QShortcut(QKeySequence("Ctrl+G"), self, self._toggle_guidelines)
        QShortcut(QKeySequence("Ctrl+H"), self, self._on_help)

    # ── Timers ─────────────────────────────────────────────────────────────

    def _start_timers(self):
        self._ram_timer = QTimer(self)
        self._ram_timer.timeout.connect(self._check_ram)
        self._ram_timer.start(5000)
        self._check_ram()

        # Pulse animation for status label when running
        self._pulse_timer = QTimer(self)
        self._pulse_timer.timeout.connect(self._pulse_status)
        self._pulse_opacity = 1.0
        self._pulse_dir = -1

        # Extension connectivity check — every 8 seconds
        self._ext_timer = QTimer(self)
        self._ext_timer.timeout.connect(self._probe_extension)
        self._ext_timer.start(8000)

    def _minimize_to_corner(self):
        """Hide dock and show small floating bubble in corner."""
        self._corner_bubble.place_in_corner()
        self._corner_bubble.show()
        self._corner_bubble.raise_()
        self._corner_bubble.activateWindow()
        self.hide()

    # ╔══════════════════════════════════════════════════════════════════════╗
    # ║                  CHROME EXTENSION CONTROLS                         ║
    # ╚══════════════════════════════════════════════════════════════════════╝

    def _on_browser_toggle(self):
        """Toggle between built-in Chromium and Chrome Extension mode."""
        self.use_extension = self.browser_toggle_btn.isChecked()
        self._settings.setValue("use_extension", self.use_extension)
        self._update_browser_toggle_style()

        # Show/hide extension-related controls
        self.ext_status_label.setVisible(self.use_extension)
        self.ext_install_btn.setVisible(self.use_extension)

        if self.use_extension:
            self._probe_extension()
            self._log(
                "🔌  Switched to Chrome Extension mode — "
                "drives your own Chrome via the bridge extension"
            )
        else:
            self._log("🌐  Switched to built-in Chromium mode — launches fresh Chromium")

    def _update_browser_toggle_style(self):
        active = "true" if self.use_extension else "false"
        text = "🔌 Chrome" if self.use_extension else "🌐 Chromium"
        self.browser_toggle_btn.setText(text)
        self.browser_toggle_btn.setProperty("active", active)
        self.browser_toggle_btn.style().unpolish(self.browser_toggle_btn)
        self.browser_toggle_btn.style().polish(self.browser_toggle_btn)

    def _update_ext_status_display(self, connected: bool, tooltip: str):
        self._ext_connected = connected
        if connected:
            self.ext_status_label.setStyleSheet("color: #4CAF50; font-size: 12px;")
        else:
            self.ext_status_label.setStyleSheet("color: #ff4477; font-size: 12px;")
        self.ext_status_label.setToolTip(f"Extension: {tooltip}")

    # ── Extension connectivity callbacks (called from bridge thread!) ─────
    # These fire on the bridge's background thread, so we must NOT touch
    # widgets directly.  Instead, schedule a safe UI update via QTimer.
    #
    # DEBOUNCE: MV3 service workers can flicker connected/disconnected
    # multiple times per second during reload.  We debounce the log
    # messages with a 1.5s timer so only *stable* state changes are shown.

    def _on_ext_connected_callback(self):
        """Called from the bridge thread when the extension connects."""
        QTimer.singleShot(0, self._handle_ext_connected)

    def _on_ext_disconnected_callback(self):
        """Called from the bridge thread when the extension disconnects."""
        QTimer.singleShot(0, self._handle_ext_disconnected)

    def _handle_ext_connected(self):
        self._update_ext_status_display(True, "Connected")
        self._ext_debounce_log(True)

    def _handle_ext_disconnected(self):
        self._update_ext_status_display(False, "Disconnected")
        self._ext_debounce_log(False)

    def _ext_debounce_log(self, connected: bool):
        """Only log connect/disconnect if the state holds for 1.5 seconds."""
        self._ext_pending_state = connected
        if self._ext_debounce_timer is not None:
            self._ext_debounce_timer.stop()
        self._ext_debounce_timer = QTimer(self)
        self._ext_debounce_timer.setSingleShot(True)
        self._ext_debounce_timer.timeout.connect(self._ext_flush_debounce)
        self._ext_debounce_timer.start(1500)

    def _ext_flush_debounce(self):
        """Fire the debounced log message if state actually changed."""
        state = getattr(self, "_ext_pending_state", None)
        if state is None:
            return
        # Only log if this is a NEW state (not the same as last logged)
        if state != self._ext_last_log_state:
            self._ext_last_log_state = state
            if state:
                self._log("🟢  Chrome extension connected!")
            elif self.use_extension:
                self._log("🔴  Chrome extension disconnected")

    def _probe_extension(self):
        """Quick connectivity check in background."""
        connected = self._bridge.connected
        self._update_ext_status_display(connected, "Connected" if connected else "Waiting…")

    def _on_install_extension(self):
        """Open the extension folder so the user can load it in Chrome."""
        ext_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "extension")

        if not os.path.isdir(ext_dir):
            self._log(f"❌  Extension folder not found: {ext_dir}")
            return

        # Open the folder in the system file manager
        if sys.platform == "win32":
            os.startfile(ext_dir)
        elif sys.platform == "darwin":
            import subprocess
            subprocess.Popen(["open", ext_dir])
        else:
            import subprocess
            subprocess.Popen(["xdg-open", ext_dir])

        self._log(
            f"📦  Extension folder opened!\n"
            f"    To install the extension in Chrome:\n"
            f"    1. Open Chrome and go to  chrome://extensions\n"
            f"    2. Enable \"Developer mode\" (top-right toggle)\n"
            f"    3. Click \"Load unpacked\"\n"
            f"    4. Select the folder that just opened:\n"
            f"       {ext_dir}\n"
            f"    5. The extension will auto-connect to this app."
        )

        if not self._log_visible:
            self._toggle_log()

    # ╔══════════════════════════════════════════════════════════════════════╗
    # ║                     MODEL MANAGEMENT                               ║
    # ╚══════════════════════════════════════════════════════════════════════╝

    def _on_provider_index_changed(self, index: int):
        self.provider = self.provider_combo.itemData(index) or PROVIDER_OLLAMA
        self._settings.setValue("provider", self.provider)
        self._refresh_models()

    def _refresh_models(self):
        if self._model_fetch_worker and self._model_fetch_worker.isRunning():
            return
        self._model_fetch_worker = ModelFetchWorker(
            provider=self.provider,
            opencode_api_key=self.opencode_api_key,
            openrouter_api_key=self.openrouter_api_key,
            google_api_key=self.google_api_key,
            groq_api_key=self.groq_api_key,
            parent=self,
        )
        self._model_fetch_worker.models_fetched.connect(self._on_models_fetched)
        self._model_fetch_worker.fetch_failed.connect(self._on_models_fetch_failed)
        self._model_fetch_worker.start()

    @pyqtSlot(list)
    def _on_models_fetched(self, models: list):
        if not models:
            return
        current = self.model_combo.currentText()
        self.model_combo.blockSignals(True)
        self.model_combo.clear()
        self.model_combo.addItems(models)
        idx = self.model_combo.findText(current)
        if idx >= 0:
            self.model_combo.setCurrentIndex(idx)
        else:
            # Current model not in list (e.g. switched provider): pick first
            self.model_combo.setCurrentIndex(0)
            self.model_name = models[0]
            self._settings.setValue("model_name", self.model_name)
        self.model_combo.blockSignals(False)

        # Pre-warm the selected model in the background (Ollama only; cloud providers don't need warmup)
        if self.provider == PROVIDER_OLLAMA:
            self._warm_model(self.model_name)

    @pyqtSlot(str)
    def _on_models_fetch_failed(self, error: str):
        self._log(f"⚠️  Could not fetch models: {error}")

    def _on_model_changed(self, text: str):
        name = text.strip()
        if name:
            self.model_name = name
            self._settings.setValue("model_name", name)
            if self.provider == PROVIDER_OLLAMA:
                self._warm_model(name)

    # ── Model pre-warming ─────────────────────────────────────────────────

    def _warm_model(self, model_name: str):
        """Pre-warm a model in the background so the first task is instant."""
        pool = ModelPool.get()
        if pool.is_warm(model_name):
            return
        if self._model_warmup_worker and self._model_warmup_worker.isRunning():
            return  # already warming something
        self._model_warmup_worker = ModelWarmupWorker(model_name, parent=self)
        self._model_warmup_worker.warmup_done.connect(self._on_warmup_done)
        self._model_warmup_worker.start()

    @pyqtSlot(str, bool)
    def _on_warmup_done(self, model_name: str, success: bool):
        if success:
            self._log(f"🔥  Model \"{model_name}\" pre-warmed — first task will be faster")
        else:
            self._log(f"⚠️  Model \"{model_name}\" pre-warm failed (will cold-start on first use)")

    # ╔══════════════════════════════════════════════════════════════════════╗
    # ║                     LOG PANE TOGGLE                                ║
    # ╚══════════════════════════════════════════════════════════════════════╝

    def _toggle_log(self):
        self._log_visible = not self._log_visible
        target = LOG_EXPANDED_HEIGHT if self._log_visible else 0

        anim = QPropertyAnimation(self.log_pane, b"maximumHeight", self)
        anim.setDuration(350)
        anim.setStartValue(self.log_pane.maximumHeight())
        anim.setEndValue(target)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)

        anim_min = QPropertyAnimation(self.log_pane, b"minimumHeight", self)
        anim_min.setDuration(350)
        anim_min.setStartValue(self.log_pane.minimumHeight())
        anim_min.setEndValue(target)
        anim_min.setEasingCurve(QEasingCurve.Type.OutCubic)

        anim.valueChanged.connect(self._sync_geometry)
        anim.start()
        anim_min.start()
        self._current_anim = (anim, anim_min)

        self.toggle_btn.setText("▼" if self._log_visible else "▲")

    def _toggle_guidelines(self):
        GUIDELINES_HEIGHT = 100
        self._guidelines_visible = not self._guidelines_visible
        target = GUIDELINES_HEIGHT if self._guidelines_visible else 0

        anim = QPropertyAnimation(self.guidelines_pane, b"maximumHeight", self)
        anim.setDuration(250)
        anim.setStartValue(self.guidelines_pane.maximumHeight())
        anim.setEndValue(target)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)

        anim_min = QPropertyAnimation(self.guidelines_pane, b"minimumHeight", self)
        anim_min.setDuration(250)
        anim_min.setStartValue(self.guidelines_pane.minimumHeight())
        anim_min.setEndValue(target)
        anim_min.setEasingCurve(QEasingCurve.Type.OutCubic)

        anim.valueChanged.connect(self._sync_geometry)
        anim.start()
        anim_min.start()
        self._current_anim = (anim, anim_min)

        self.guidelines_btn.setStyleSheet(
            "QPushButton { min-width: 32px; max-width: 32px; padding: 0; "
            "font-size: 14px; background: rgba(0,212,255,0.15); }"
            if self._guidelines_visible else
            "QPushButton { min-width: 32px; max-width: 32px; padding: 0; font-size: 14px; }"
        )

    def _on_guidelines_changed(self):
        self.custom_guidelines = self.guidelines_pane.toPlainText().strip()
        self._settings.setValue("custom_guidelines", self.custom_guidelines)

    def _sync_geometry(self):
        geo = self._screen_geo
        current_log_h = self.log_pane.maximumHeight()
        current_guide_h = self.guidelines_pane.maximumHeight()
        total_h = DOCK_HEIGHT + current_log_h + current_guide_h
        self.setFixedHeight(total_h)
        self.move(geo.x(), geo.y() + geo.height() - total_h)

    # ╔══════════════════════════════════════════════════════════════════════╗
    # ║                     ACTIONS                                        ║
    # ╚══════════════════════════════════════════════════════════════════════╝

    def _on_submit(self):
        task = self.prompt_input.text().strip()
        if not task:
            return

        if self._agent_worker and self._agent_worker.isRunning():
            self._log("⚠️  Agent is already running — stop it first.")
            return

        # Pre-flight check for extension mode
        if self.use_extension and not self._bridge.connected:
            self._log(
                "❌  Chrome extension is not connected!\n"
                "    Click the 📦 button to install the extension first."
            )
            if not self._log_visible:
                self._toggle_log()
            return

        mode_label = "🔌 Chrome Extension" if self.use_extension else "🌐 Built-in Chromium"
        self._log(f"\n{'━' * 60}")
        self._log(f"📝  > {task}")
        self._log(f"🤖  Model: {self.model_name}  |  Browser: {mode_label}")
        self._log(f"{'━' * 60}")
        self.prompt_input.clear()
        self.prompt_input.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.status_label.setText("RUNNING")
        self._pulse_timer.start(80)

        if not self._log_visible:
            self._toggle_log()

        geo = self._screen_geo
        self._agent_worker = AgentWorker(
            task=task,
            screen_width=geo.width(),
            screen_height=geo.height(),
            model_name=self.model_name,
            use_extension=self.use_extension,
            extension_bridge=self._bridge if self.use_extension else None,
            use_playbooks=self.use_playbooks,
            custom_guidelines=self.custom_guidelines,
            provider=self.provider,
            opencode_api_key=self.opencode_api_key,
            openrouter_api_key=self.openrouter_api_key,
            google_api_key=self.google_api_key,
            groq_api_key=self.groq_api_key,
            parent=self,
        )
        self._agent_worker.status_update.connect(self._on_status)
        self._agent_worker.task_finished.connect(self._on_finished)
        self._agent_worker.error_occurred.connect(self._on_error)
        self._agent_worker.stuck_need_human.connect(self._on_stuck)
        self._agent_worker.token_stream.connect(self._on_token)
        self._agent_worker.finished.connect(self._on_thread_done)
        self._agent_worker.start()

    def _on_stop(self):
        if self._agent_worker and self._agent_worker.isRunning():
            self._agent_worker.request_stop()
            self._log("🛑  Stop requested — waiting for current step…")

    def _on_clear(self):
        self.log_pane.clear()

    def _apply_theme(self, theme: str):
        """Apply theme (dark/light) to dock and corner bubble."""
        self.setStyleSheet(get_stylesheet(theme))
        self._corner_bubble.set_theme(theme)

    def _on_settings(self):
        store = PlaybookStore()
        playbook_count = len(store.list_all())

        dlg = SettingsDialog(
            ram_threshold=self.ram_threshold,
            use_playbooks=self.use_playbooks,
            playbook_count=playbook_count,
            opencode_api_key=self.opencode_api_key,
            openrouter_api_key=self.openrouter_api_key,
            google_api_key=self.google_api_key,
            groq_api_key=self.groq_api_key,
            theme=self.theme,
            parent=self,
        )
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.ram_threshold = dlg.ram_spin.value()
            self._settings.setValue("ram_threshold", self.ram_threshold)

            self.use_playbooks = dlg.playbooks_checkbox.isChecked()
            self._settings.setValue("use_playbooks", self.use_playbooks)

            self.opencode_api_key = dlg.opencode_key_edit.text().strip()
            self._settings.setValue("opencode_api_key", self.opencode_api_key)

            self.openrouter_api_key = dlg.openrouter_key_edit.text().strip()
            self._settings.setValue("openrouter_api_key", self.openrouter_api_key)

            self.google_api_key = dlg.google_key_edit.text().strip()
            self._settings.setValue("google_api_key", self.google_api_key)

            self.groq_api_key = dlg.groq_key_edit.text().strip()
            self._settings.setValue("groq_api_key", self.groq_api_key)

            new_theme = dlg.theme_combo.currentData() or THEME_DARK
            if new_theme != self.theme:
                self.theme = new_theme
                self._settings.setValue("theme", self.theme)
                self._apply_theme(self.theme)

            pb_status = "ON" if self.use_playbooks else "OFF"
            if dlg._playbooks_cleared:
                pb_status += " (cleared)"

            self._log(
                f"⚙️  Settings updated  ▸  RAM = {self.ram_threshold}%"
                f"  ▸  Playbooks = {pb_status}"
            )

    def _on_help(self):
        """Open the README / help guide in the default app."""
        readme = os.path.join(os.path.dirname(os.path.abspath(__file__)), "README.md")
        if os.path.isfile(readme):
            try:
                if sys.platform == "win32":
                    os.startfile(readme)
                elif sys.platform == "darwin":
                    import subprocess
                    subprocess.Popen(["open", readme])
                else:
                    import subprocess
                    subprocess.Popen(["xdg-open", readme])
                self._log("📖  Opened Help guide (README.md)")
            except Exception as e:
                self._log(f"❌  Could not open help: {e}\n    Path: {readme}")
        else:
            self._log(f"❌  Help file not found: {readme}")

    # ╔══════════════════════════════════════════════════════════════════════╗
    # ║                     SLOTS (from AgentWorker)                       ║
    # ╚══════════════════════════════════════════════════════════════════════╝

    @pyqtSlot(str)
    def _on_status(self, msg: str):
        self._log(msg)
        short = (msg[:55] + "…") if len(msg) > 55 else msg
        self.status_label.setText(short)

    @pyqtSlot(str)
    def _on_token(self, token: str):
        """Append a streaming LLM token to the log pane (no newline prefix)."""
        cursor = self.log_pane.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(token)
        self.log_pane.setTextCursor(cursor)
        sb = self.log_pane.verticalScrollBar()
        sb.setValue(sb.maximum())

    @pyqtSlot(str)
    def _on_finished(self, result: str):
        self._log(f"\n{'═' * 60}")
        self._log(f"  ✅  RESULT: {result}")
        self._log(f"{'═' * 60}\n")
        self.status_label.setText("✅ DONE")
        self._pulse_timer.stop()

    @pyqtSlot(str)
    def _on_error(self, err: str):
        self._log(f"\n❌  ERROR: {err}\n")
        self.status_label.setText("❌ ERROR")
        self._pulse_timer.stop()

    @pyqtSlot(str)
    def _on_stuck(self, msg: str):
        self._log(f"\n⚠️  STUCK: {msg}\n")
        self.status_label.setText("⚠️ STUCK (consider taking over)")

    @pyqtSlot()
    def _on_thread_done(self):
        self.prompt_input.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self._pulse_timer.stop()
        self.status_label.setStyleSheet("")
        if self.status_label.text() == "RUNNING":
            self.status_label.setText("READY")

    # ╔══════════════════════════════════════════════════════════════════════╗
    # ║                     RAM / PULSE / LOG                              ║
    # ╚══════════════════════════════════════════════════════════════════════╝

    def _check_ram(self):
        mem = psutil.virtual_memory()
        pct = mem.percent
        used = mem.used / (1024 ** 3)
        total = mem.total / (1024 ** 3)
        self.ram_label.setText(f"RAM {pct:.0f}%  {used:.1f}/{total:.1f}G")

        if pct > self.ram_threshold:
            collected = gc.collect()
            self._log(
                f"⚠️  RAM {pct:.0f}% exceeds {self.ram_threshold}% threshold! "
                f"(GC freed {collected} Python objects — consider closing "
                f"browser tabs or restarting Ollama to reclaim memory)"
            )

    def _pulse_status(self):
        self._pulse_opacity += self._pulse_dir * 0.05
        if self._pulse_opacity <= 0.3:
            self._pulse_opacity = 0.3
            self._pulse_dir = 1
        elif self._pulse_opacity >= 1.0:
            self._pulse_opacity = 1.0
            self._pulse_dir = -1
        alpha = int(self._pulse_opacity * 255)
        self.status_label.setStyleSheet(
            f"color: rgba(0, 212, 255, {alpha});"
            f"font-size: 11px; font-weight: 600;"
            f"font-family: 'Cascadia Code', 'Consolas', monospace;"
            f"letter-spacing: 1px; padding: 0 8px;"
        )

    def _log(self, text: str):
        cursor = self.log_pane.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        if not self.log_pane.document().isEmpty():
            cursor.insertText("\n")
        cursor.insertText(text)
        self.log_pane.setTextCursor(cursor)
        sb = self.log_pane.verticalScrollBar()
        sb.setValue(sb.maximum())

    # ── Cleanup ───────────────────────────────────────────────────────────

    def closeEvent(self, event):
        """Stop all workers, the browser session, and the bridge on exit."""
        # Stop any running agent
        if self._agent_worker and self._agent_worker.isRunning():
            self._agent_worker.request_stop()
            self._agent_worker.wait(3000)

        # Stop timers
        self._ram_timer.stop()
        self._pulse_timer.stop()
        self._ext_timer.stop()

        # Kill the persistent Chromium browser (if any)
        BrowserSession.shutdown()

        # Shut down the WebSocket bridge (frees the port)
        self._bridge.stop()
        super().closeEvent(event)


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║                    GLOBAL EXCEPTION HANDLER                                 ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

_dock_ref: DockWindow | None = None


def _global_excepthook(exc_type, exc_value, exc_tb):
    formatted = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    print(f"[UNHANDLED EXCEPTION]\n{formatted}", file=sys.stderr)
    if _dock_ref is not None:
        try:
            _dock_ref._log(f"💥  UNHANDLED EXCEPTION:\n{formatted}")
        except Exception:
            pass


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║                           ENTRY POINT                                       ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

def main():
    global _dock_ref

    sys.excepthook = _global_excepthook

    app = QApplication(sys.argv)
    app.setFont(QFont("Segoe UI", 10))
    app.setOrganizationName(SETTINGS_ORG)
    app.setApplicationName(SETTINGS_APP)
    icon_path = Path(__file__).parent / "assets" / "icon.png"
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))

    dock = DockWindow()
    _dock_ref = dock
    dock.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
