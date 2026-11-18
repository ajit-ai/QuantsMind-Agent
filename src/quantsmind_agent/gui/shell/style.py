"""Qt styling for QuantsMind Engineering Studio.

One design system, expressed as tokens plus a stylesheet. The product surface is
a dark engineering environment: calm slate chrome, a single strong accent, and
status colours reserved for things that actually carry status. Colour is never
used as decoration, so a green mark always means success and an amber mark always
means attention.

The palette is deliberately not a generic dashboard theme. Panels are separated
by one step of elevation rather than by heavy borders, navigation carries real
destination names, and every surface keeps text contrast above WCAG AA on the
base colour.
"""

from __future__ import annotations

__all__ = ["COLORS", "ROLE_COLORS", "STYLESHEET", "apply_theme"]

COLORS = {
    # Surfaces, darkest to lightest.
    "bg": "#111827",
    "bg_deep": "#0B1120",
    "panel": "#1E293B",
    "panel_alt": "#172033",
    "panel_raised": "#243247",
    "input": "#0F172A",
    "border": "#334155",
    "border_soft": "#263449",
    # Text.
    "text": "#E2E8F0",
    "text_strong": "#F8FAFC",
    "muted": "#94A3B8",
    "subtle": "#64748B",
    # Brand and status.
    "accent": "#2563EB",
    "accent_hover": "#1D4ED8",
    "accent_soft": "#1E3A8A",
    "accent_text": "#BFDBFE",
    "success": "#22C55E",
    "warning": "#F59E0B",
    "error": "#EF4444",
    "tool": "#A78BFA",
    # Editor.
    "editor": "#0F172A",
    "editor_gutter": "#0B1120",
    "editor_current_line": "#1E293B",
    "editor_selection": "#1E3A8A",
}

#: Accent colours per conversation role.
ROLE_COLORS = {
    "user": COLORS["accent_text"],
    "assistant": COLORS["text"],
    "system": COLORS["muted"],
    "tool": COLORS["tool"],
    "error": COLORS["error"],
}

_FONT = '"Segoe UI Variable Text", "Segoe UI", "Inter", "Noto Sans", sans-serif'
_MONO = '"Cascadia Code", "Cascadia Mono", "Consolas", "DejaVu Sans Mono", monospace'

STYLESHEET = f"""
* {{
    font-family: {_FONT};
    font-size: 13px;
    color: {COLORS["text"]};
}}

QMainWindow, QDialog {{
    background: {COLORS["bg"]};
}}

QWidget#StudioPage {{
    background: {COLORS["bg"]};
}}

QScrollArea {{
    background: transparent;
    border: none;
}}
QScrollArea > QWidget > QWidget {{
    background: transparent;
}}

/* -- header and navigation ------------------------------------------- */

QFrame#AppHeader {{
    background: {COLORS["bg_deep"]};
    border-bottom: 1px solid {COLORS["border"]};
}}
QFrame#HeaderTop {{
    background: transparent;
}}
QLabel#BrandMark {{
    background: {COLORS["accent"]};
    color: white;
    border-radius: 9px;
    font-size: 15px;
    font-weight: 800;
    padding: 5px 9px;
}}
QLabel#BrandName {{
    color: {COLORS["text_strong"]};
    font-size: 15px;
    font-weight: 700;
    letter-spacing: 1.4px;
}}
QLabel#BrandSub {{
    color: {COLORS["muted"]};
    font-size: 10px;
    letter-spacing: 1.8px;
}}
QLabel#WorkspaceChip {{
    background: {COLORS["panel"]};
    border: 1px solid {COLORS["border_soft"]};
    border-radius: 8px;
    color: {COLORS["text"]};
    padding: 5px 10px;
}}
QLabel#StatusChip {{
    background: {COLORS["panel"]};
    border: 1px solid {COLORS["border_soft"]};
    border-radius: 8px;
    color: {COLORS["muted"]};
    padding: 5px 10px;
}}
QLabel#StatusChip[tone="success"] {{
    border-color: rgba(34, 197, 94, 0.45);
    color: {COLORS["success"]};
}}
QLabel#StatusChip[tone="warning"] {{
    border-color: rgba(245, 158, 11, 0.45);
    color: {COLORS["warning"]};
}}

QFrame#ActivityBar {{
    background: {COLORS["bg_deep"]};
    border-bottom: 1px solid {COLORS["border_soft"]};
}}
QToolButton#ActivityButton {{
    background: transparent;
    border: none;
    border-bottom: 2px solid transparent;
    color: {COLORS["muted"]};
    padding: 5px 12px;
    font-weight: 600;
}}
QToolButton#ActivityButton:hover {{
    color: {COLORS["text_strong"]};
    background: {COLORS["panel_alt"]};
}}
QToolButton#ActivityButton:checked {{
    color: {COLORS["text_strong"]};
    background: {COLORS["panel"]};
    border-bottom: 2px solid {COLORS["accent"]};
}}

QPushButton#DestinationButton {{
    background: transparent;
    border: none;
    border-bottom: 2px solid transparent;
    border-radius: 0;
    color: {COLORS["muted"]};
    padding: 10px 14px 9px 14px;
    text-align: left;
    font-weight: 600;
}}
QPushButton#DestinationButton:hover {{
    color: {COLORS["text_strong"]};
    background: {COLORS["panel_alt"]};
}}
QPushButton#DestinationButton:checked {{
    color: {COLORS["text_strong"]};
    border-bottom: 2px solid {COLORS["accent"]};
    background: {COLORS["panel"]};
}}

/* -- page headings and cards ----------------------------------------- */

QLabel#PageTitle {{
    color: {COLORS["text_strong"]};
    font-size: 24px;
    font-weight: 700;
}}
QLabel#PageSubtitle {{
    color: {COLORS["muted"]};
    font-size: 13px;
}}
QLabel#SectionLabel {{
    color: {COLORS["subtle"]};
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 1.4px;
}}
QLabel#CardTitle {{
    color: {COLORS["text_strong"]};
    font-size: 14px;
    font-weight: 700;
}}
QLabel#CardBody {{
    color: {COLORS["muted"]};
    font-size: 12px;
}}
QLabel#MetricValue {{
    color: {COLORS["text_strong"]};
    font-size: 22px;
    font-weight: 700;
}}
QLabel#MetricLabel {{
    color: {COLORS["subtle"]};
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 1.1px;
}}
QLabel#EmptyTitle {{
    color: {COLORS["text"]};
    font-size: 14px;
    font-weight: 600;
}}
QLabel#EmptyBody {{
    color: {COLORS["muted"]};
    font-size: 12px;
}}
QLabel#StatusText {{
    color: {COLORS["muted"]};
    font-size: 11px;
}}
QLabel#ErrorText {{
    color: {COLORS["error"]};
    font-weight: 600;
}}
QLabel#MonoText {{
    font-family: {_MONO};
    color: {COLORS["muted"]};
    font-size: 11px;
}}

QLabel#Badge {{
    background: {COLORS["panel_raised"]};
    border: 1px solid {COLORS["border_soft"]};
    border-radius: 7px;
    color: {COLORS["muted"]};
    font-size: 10px;
    font-weight: 700;
    padding: 2px 7px;
}}
QLabel#Badge[tone="success"] {{
    background: rgba(34, 197, 94, 0.14);
    border-color: rgba(34, 197, 94, 0.45);
    color: {COLORS["success"]};
}}
QLabel#Badge[tone="warning"] {{
    background: rgba(245, 158, 11, 0.14);
    border-color: rgba(245, 158, 11, 0.45);
    color: {COLORS["warning"]};
}}
QLabel#Badge[tone="error"] {{
    background: rgba(239, 68, 68, 0.14);
    border-color: rgba(239, 68, 68, 0.45);
    color: {COLORS["error"]};
}}
QLabel#Badge[tone="accent"] {{
    background: rgba(37, 99, 235, 0.18);
    border-color: rgba(37, 99, 235, 0.5);
    color: {COLORS["accent_text"]};
}}

QFrame#Card {{
    background: {COLORS["panel"]};
    border: 1px solid {COLORS["border_soft"]};
    border-radius: 12px;
}}
QFrame#Card[tone="accent"] {{
    border-color: rgba(37, 99, 235, 0.5);
}}
QFrame#StatCard {{
    background: {COLORS["panel"]};
    border: 1px solid {COLORS["border_soft"]};
    border-radius: 12px;
}}
QFrame#EmptyState {{
    background: {COLORS["panel_alt"]};
    border: 1px dashed {COLORS["border"]};
    border-radius: 12px;
}}
QFrame#Banner {{
    background: rgba(37, 99, 235, 0.12);
    border: 1px solid rgba(37, 99, 235, 0.4);
    border-radius: 10px;
}}
QFrame#WarningBanner {{
    background: rgba(245, 158, 11, 0.1);
    border: 1px solid rgba(245, 158, 11, 0.4);
    border-radius: 10px;
}}
QFrame#TrustBanner {{
    background: rgba(245, 158, 11, 0.12);
    border-bottom: 1px solid rgba(245, 158, 11, 0.45);
}}
QFrame#NotificationBar {{
    background: {COLORS["panel_raised"]};
    border-bottom: 1px solid {COLORS["border"]};
}}
QFrame#NotificationBar[kind="error"] {{
    background: rgba(239, 68, 68, 0.16);
    border-bottom-color: rgba(239, 68, 68, 0.5);
}}

/* -- controls --------------------------------------------------------- */

QPushButton {{
    background: {COLORS["panel_raised"]};
    border: 1px solid {COLORS["border"]};
    border-radius: 7px;
    color: {COLORS["text"]};
    padding: 6px 13px;
    min-height: 18px;
}}
QPushButton:hover {{
    border-color: {COLORS["accent"]};
    color: {COLORS["text_strong"]};
}}
QPushButton:pressed {{
    background: {COLORS["panel"]};
}}
QPushButton:disabled {{
    color: {COLORS["subtle"]};
    background: {COLORS["panel_alt"]};
    border-color: {COLORS["border_soft"]};
}}
QPushButton#PrimaryButton {{
    background: {COLORS["accent"]};
    color: white;
    border-color: {COLORS["accent"]};
    font-weight: 600;
}}
QPushButton#PrimaryButton:hover {{
    background: {COLORS["accent_hover"]};
    border-color: {COLORS["accent_hover"]};
}}
QPushButton#PrimaryButton:disabled {{
    background: {COLORS["accent_soft"]};
    border-color: {COLORS["accent_soft"]};
    color: {COLORS["muted"]};
}}
QPushButton#DangerButton {{
    background: {COLORS["error"]};
    color: white;
    border-color: {COLORS["error"]};
}}
QPushButton#DangerButton:disabled {{
    background: rgba(239, 68, 68, 0.3);
    border-color: rgba(239, 68, 68, 0.3);
    color: {COLORS["muted"]};
}}
QPushButton#GhostButton {{
    background: transparent;
    border-color: transparent;
    color: {COLORS["muted"]};
    padding: 5px 9px;
}}
QPushButton#GhostButton:hover {{
    background: {COLORS["panel_raised"]};
    color: {COLORS["text_strong"]};
}}
QPushButton#SegmentButton {{
    background: transparent;
    border: 1px solid transparent;
    color: {COLORS["muted"]};
    border-radius: 6px;
    padding: 5px 11px;
}}
QPushButton#SegmentButton:hover {{
    color: {COLORS["text_strong"]};
    background: {COLORS["panel_raised"]};
}}
QPushButton#SegmentButton:checked {{
    background: {COLORS["panel_raised"]};
    border-color: {COLORS["border"]};
    color: {COLORS["text_strong"]};
    font-weight: 600;
}}
QPushButton#ViewTab {{
    background: transparent;
    border: none;
    border-bottom: 2px solid transparent;
    border-radius: 0;
    color: {COLORS["muted"]};
    padding: 7px 12px;
    font-weight: 600;
}}
QPushButton#ViewTab:checked {{
    color: {COLORS["text_strong"]};
    border-bottom-color: {COLORS["accent"]};
}}

QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
    background: {COLORS["input"]};
    border: 1px solid {COLORS["border"]};
    border-radius: 7px;
    color: {COLORS["text"]};
    padding: 6px 8px;
    selection-background-color: {COLORS["accent"]};
    selection-color: white;
}}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus,
QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{
    border-color: {COLORS["accent"]};
}}
QLineEdit:read-only, QPlainTextEdit:read-only {{
    background: {COLORS["panel_alt"]};
    color: {COLORS["muted"]};
}}
QComboBox::drop-down {{
    border: none;
    width: 20px;
}}
QComboBox QAbstractItemView {{
    background: {COLORS["panel"]};
    border: 1px solid {COLORS["border"]};
    selection-background-color: {COLORS["accent"]};
    selection-color: white;
}}

QLabel {{
    background: transparent;
}}

/* -- collections ------------------------------------------------------ */

QListWidget, QTreeWidget, QTreeView, QTableView {{
    background: {COLORS["panel_alt"]};
    border: 1px solid {COLORS["border_soft"]};
    border-radius: 8px;
    outline: none;
    color: {COLORS["text"]};
}}
QListWidget::item, QTreeWidget::item, QTreeView::item {{
    padding: 5px 7px;
    border-bottom: 1px solid {COLORS["border_soft"]};
}}
QListWidget::item:selected, QTreeWidget::item:selected, QTreeView::item:selected {{
    background: {COLORS["accent_soft"]};
    color: {COLORS["text_strong"]};
}}
QListWidget::item:hover, QTreeWidget::item:hover {{
    background: {COLORS["panel_raised"]};
}}
QHeaderView::section {{
    background: {COLORS["panel"]};
    color: {COLORS["muted"]};
    border: none;
    border-bottom: 1px solid {COLORS["border"]};
    padding: 5px;
}}

QTextBrowser {{
    background: {COLORS["panel_alt"]};
    border: 1px solid {COLORS["border_soft"]};
    border-radius: 10px;
    padding: 10px;
    color: {COLORS["text"]};
}}

QTabWidget::pane {{
    border: 1px solid {COLORS["border_soft"]};
    border-radius: 8px;
    top: -1px;
    background: {COLORS["panel_alt"]};
}}
/* Qt paints the tab bar's base itself, which is light; without these two rules
   the dark tab text sits on that light strip and the open file name disappears. */
QTabBar {{
    background: {COLORS["panel_alt"]};
    border: none;
    qproperty-drawBase: 0;
}}
QTabBar::tab {{
    background: {COLORS["panel_alt"]};
    color: {COLORS["muted"]};
    border: none;
    border-bottom: 2px solid transparent;
    padding: 7px 12px;
}}
QTabBar::tab:selected {{
    background: {COLORS["panel"]};
    color: {COLORS["text_strong"]};
    border-bottom-color: {COLORS["accent"]};
}}
QTabBar::tab:hover {{
    background: {COLORS["panel_raised"]};
    color: {COLORS["text"]};
}}

QGroupBox {{
    background: {COLORS["panel"]};
    border: 1px solid {COLORS["border_soft"]};
    border-radius: 10px;
    margin-top: 14px;
    padding: 10px;
    font-weight: 600;
    color: {COLORS["text_strong"]};
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 4px;
    color: {COLORS["muted"]};
}}

QCheckBox, QRadioButton {{
    color: {COLORS["text"]};
    spacing: 7px;
}}
QCheckBox::indicator, QRadioButton::indicator {{
    width: 15px;
    height: 15px;
    border: 1px solid {COLORS["border"]};
    background: {COLORS["input"]};
    border-radius: 4px;
}}
QCheckBox::indicator:checked, QRadioButton::indicator:checked {{
    background: {COLORS["accent"]};
    border-color: {COLORS["accent"]};
}}

QMenu {{
    background: {COLORS["panel"]};
    border: 1px solid {COLORS["border"]};
    padding: 4px;
}}
QMenu::item {{
    padding: 6px 22px 6px 12px;
    border-radius: 5px;
}}
QMenu::item:selected {{
    background: {COLORS["accent_soft"]};
}}

/* -- scrollbars and splitters ---------------------------------------- */

QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 2px;
}}
QScrollBar::handle:vertical {{
    background: {COLORS["border"]};
    border-radius: 5px;
    min-height: 30px;
}}
QScrollBar::handle:vertical:hover {{
    background: {COLORS["subtle"]};
}}
QScrollBar:horizontal {{
    background: transparent;
    height: 10px;
    margin: 2px;
}}
QScrollBar::handle:horizontal {{
    background: {COLORS["border"]};
    border-radius: 5px;
    min-width: 30px;
}}
QScrollBar::add-line, QScrollBar::sub-line {{
    height: 0;
    width: 0;
}}
QSplitter::handle {{
    background: {COLORS["border_soft"]};
    width: 1px;
    height: 1px;
}}

QStatusBar {{
    background: {COLORS["bg_deep"]};
    border-top: 1px solid {COLORS["border"]};
    color: {COLORS["muted"]};
}}
QStatusBar QLabel {{
    color: {COLORS["muted"]};
}}

QToolTip {{
    background: {COLORS["panel_raised"]};
    color: {COLORS["text"]};
    border: 1px solid {COLORS["border"]};
    padding: 4px 7px;
}}

QMessageBox {{
    background: {COLORS["panel"]};
}}
QProgressBar {{
    background: {COLORS["panel_alt"]};
    border: 1px solid {COLORS["border_soft"]};
    border-radius: 6px;
    text-align: center;
    color: {COLORS["text"]};
}}
QProgressBar::chunk {{
    background: {COLORS["accent"]};
    border-radius: 5px;
}}
"""


def apply_theme(app: object) -> None:
    """Apply the application stylesheet to a ``QApplication``-like object."""

    set_style = getattr(app, "setStyleSheet", None)
    if callable(set_style):  # pragma: no branch - trivial
        set_style(STYLESHEET)
