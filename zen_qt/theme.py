"""Palette and application stylesheet for the Qt workbench."""

BG = "#eef1f5"
CARD = "#ffffff"
INSET = "#f2f5f8"
LINE = "#dde3ea"
INK = "#18222f"
MUTED = "#6a7584"
ACCENT = "#1f5fbf"
ACCENT_SOFT = "#e7f0fc"
ACCENT_INK = "#12407f"
OK = "#1b8756"
OK_SOFT = "#e4f5ec"
WARN = "#a96300"
WARN_SOFT = "#fff3d9"
WARN_LINE = "#f0d49a"
BAD = "#bf3a2b"
BAD_SOFT = "#fdecea"
BAD_LINE = "#f2c1bb"
MONO = 'Consolas, "Cascadia Mono", monospace'

PILL_STYLES = {
    "ok": (OK_SOFT, OK, "#bfe3d0"),
    "warn": (WARN_SOFT, WARN, WARN_LINE),
    "bad": (BAD_SOFT, BAD, BAD_LINE),
    "mute": ("#eef2f6", "#5b6672", LINE),
    "accent": (ACCENT_SOFT, ACCENT_INK, "#c5d9f5"),
}

QSS = f"""
* {{ font-family: "Microsoft YaHei UI", "Segoe UI", sans-serif; font-size: 13px; color: {INK}; }}
QMainWindow, QWidget#root {{ background: {BG}; }}
QFrame#card, QGroupBox#card {{ background: {CARD}; border: 1px solid {LINE}; border-radius: 10px; }}
QFrame#topbar {{ background: {CARD}; border-bottom: 1px solid {LINE}; }}
QFrame#navbar {{ background: {CARD}; border-right: 1px solid {LINE}; }}
QFrame#lockbar {{ background: {BAD_SOFT}; border-bottom: 1px solid {BAD_LINE}; }}
QFrame#bar {{ background: {CARD}; border: 1px solid {LINE}; border-radius: 10px; }}
QLabel {{ background: transparent; }}
QLabel#brand {{ font-size: 15px; font-weight: 700; }}
QLabel#h1 {{ font-size: 18px; font-weight: 700; }}
QLabel#h3 {{ font-size: 13px; font-weight: 600; }}
QLabel#muted {{ color: {MUTED}; }}
QLabel#mono {{ font-family: {MONO}; }}
QLabel#num {{ font-family: {MONO}; font-size: 19px; font-weight: 700; color: {ACCENT}; }}
QLabel#numDark {{ font-family: {MONO}; font-size: 19px; font-weight: 700; }}
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {{ background: {CARD}; border: 1px solid #cfd6de;
    border-radius: 6px; padding: 4px 8px; min-height: 20px; selection-background-color: {ACCENT_SOFT}; }}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {{ border-color: {ACCENT}; }}
QLineEdit:disabled, QComboBox:disabled {{ background: #f3f5f8; color: {MUTED}; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox QAbstractItemView {{ background: {CARD}; border: 1px solid {LINE}; selection-background-color: {ACCENT_SOFT}; }}
QPushButton {{ background: {CARD}; border: 1px solid #cbd3dc; border-radius: 7px; padding: 6px 12px; }}
QPushButton:hover:!disabled {{ border-color: {ACCENT}; }}
QPushButton:disabled {{ color: #9aa5b2; background: #f3f5f8; }}
QPushButton#primary {{ background: {ACCENT}; border-color: {ACCENT}; color: #ffffff; font-weight: 600; }}
QPushButton#primary:hover:!disabled {{ background: #1a52a8; }}
QPushButton#primary:disabled {{ background: #a8c0e2; }}
QPushButton#moving {{ background: {WARN_SOFT}; border-color: #e5c27c; color: #7a4700; font-weight: 600; }}
QPushButton#danger {{ background: {BAD}; border-color: {BAD}; color: #ffffff; font-weight: 700; }}
QPushButton#danger:disabled {{ background: #dd9c94; }}
QPushButton#flat {{ border: none; background: transparent; color: {ACCENT_INK}; padding: 4px 8px; }}
QPushButton#flat:hover:!disabled {{ text-decoration: underline; }}
QToolButton#nav {{ border: none; border-radius: 8px; padding: 8px 10px; text-align: left; color: #354152; }}
QToolButton#nav:hover:!checked {{ background: #f1f4f7; }}
QToolButton#nav:checked {{ background: {ACCENT_SOFT}; color: {ACCENT_INK}; font-weight: 600; }}
QCheckBox, QRadioButton {{ spacing: 6px; }}
QTabWidget::pane {{ border: none; border-top: 1px solid {LINE}; }}
QTabBar::tab {{ background: {CARD}; color: {MUTED}; padding: 7px 14px; border: none; }}
QTabBar::tab:selected {{ color: {ACCENT}; font-weight: 600; border-bottom: 2px solid {ACCENT}; }}
QTableWidget, QTreeWidget, QListWidget {{ background: {CARD}; border: 1px solid {LINE}; border-radius: 8px;
    gridline-color: #edf0f4; alternate-background-color: #f7f9fb; selection-background-color: {ACCENT_SOFT};
    selection-color: {INK}; }}
QTableWidget::item, QTreeWidget::item {{ padding: 3px; }}
QHeaderView::section {{ background: #fafbfc; color: {MUTED}; font-weight: 600; font-size: 11px;
    border: none; border-bottom: 1px solid {LINE}; padding: 5px 8px; }}
QPlainTextEdit, QTextEdit {{ background: #fbfcfd; border: 1px solid {LINE}; border-radius: 8px;
    font-family: {MONO}; font-size: 12px; }}
QProgressBar {{ background: #e6ebf0; border: none; border-radius: 3px; max-height: 6px; }}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 3px; }}
QScrollBar:vertical {{ background: transparent; width: 10px; }}
QScrollBar::handle:vertical {{ background: #c8d0da; border-radius: 5px; min-height: 30px; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; }}
QScrollBar::handle:horizontal {{ background: #c8d0da; border-radius: 5px; min-width: 30px; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}
QToolTip {{ background: {INK}; color: #ffffff; border: none; padding: 5px 8px; }}
QDialog {{ background: {BG}; }}
QMessageBox QLabel {{ font-size: 13px; }}
"""

CHECK_ICONS = {
    "ok": (OK, "✓", "#ffffff"),
    "warn": ("#e9a530", "!", "#ffffff"),
    "bad": (BAD, "×", "#ffffff"),
    "todo": ("#ffffff", "", "#9aa5b2"),
}
