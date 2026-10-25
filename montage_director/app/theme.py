"""Dark editing-suite theme: deep blue-black panels, violet accent, flash-white highlights."""

BG = "#0e0f16"
PANEL = "#161824"
PANEL_2 = "#1d2030"
LINE = "#2a2e42"
TEXT = "#e9e9f2"
MUTED = "#8d92ab"
ACCENT = "#8b6cff"
ACCENT_HI = "#a38bff"
HOT = "#ff4f8b"
OK = "#45d483"
WARN = "#f5b14c"

SECTION_COLORS = {
    "intro": "#3b5bdb", "verse": "#5c7cfa", "build": "#f59f00",
    "drop": "#ff4f8b", "breakdown": "#20c997", "outro": "#7950f2",
}

STYLESHEET = f"""
* {{ font-family: "Segoe UI", "Inter", "Helvetica Neue", Arial, sans-serif; font-size: 13px; color: {TEXT}; }}
QMainWindow, QDialog {{ background: {BG}; }}
QWidget#Sidebar {{ background: {PANEL}; border-right: 1px solid {LINE}; }}
QScrollArea {{ border: none; background: transparent; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}
QLabel#H1 {{ font-size: 20px; font-weight: 700; letter-spacing: 0.5px; }}
QLabel#H2 {{ font-size: 11px; font-weight: 700; color: {MUTED}; letter-spacing: 1.5px; }}
QLabel#Muted {{ color: {MUTED}; }}
QLabel#Summary {{ background: {PANEL_2}; border: 1px solid {LINE}; border-radius: 8px; padding: 10px; }}
QListWidget, QPlainTextEdit, QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {{
    background: {PANEL_2}; border: 1px solid {LINE}; border-radius: 6px; padding: 6px;
    selection-background-color: {ACCENT};
}}
QListWidget#DropList {{ min-height: 70px; }}
QListWidget#DropSingle {{ padding: 2px 6px; }}
QListWidget::item {{ padding: 3px 2px; }}
QPlainTextEdit:focus, QLineEdit:focus, QComboBox:focus {{ border-color: {ACCENT}; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox QAbstractItemView {{ background: {PANEL_2}; border: 1px solid {LINE}; selection-background-color: {ACCENT}; }}
QPushButton {{
    background: {PANEL_2}; border: 1px solid {LINE}; border-radius: 6px; padding: 7px 12px;
}}
QPushButton:hover {{ border-color: {ACCENT}; }}
QPushButton:pressed {{ background: {LINE}; }}
QPushButton:disabled {{ color: #565b73; border-color: {PANEL_2}; }}
QPushButton:focus {{ outline: none; border-color: {ACCENT_HI}; }}
QPushButton#Primary {{
    background: {ACCENT}; border: none; color: white; font-size: 15px; font-weight: 700; padding: 11px;
}}
QPushButton#Primary:hover {{ background: {ACCENT_HI}; }}
QPushButton#Primary:disabled {{ background: #3a3360; color: #9a93c4; }}
QPushButton#Hot {{ background: {HOT}; border: none; color: white; font-weight: 700; }}
QPushButton#Hot:disabled {{ background: #5a2a3c; color: #c9a0b0; }}
QPushButton#StyleCard {{ text-align: left; padding: 8px 10px; }}
QPushButton#StyleCard:checked {{ border: 2px solid {ACCENT}; background: #241f3d; }}
QPushButton#Seg {{ border-radius: 0; padding: 7px 10px; }}
QPushButton#Seg:checked {{ background: {ACCENT}; border-color: {ACCENT}; color: white; font-weight: 600; }}
QProgressBar {{ background: {PANEL_2}; border: 1px solid {LINE}; border-radius: 4px; height: 8px; text-align: center; }}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 3px; }}
QSlider::groove:horizontal {{ height: 4px; background: {LINE}; border-radius: 2px; }}
QSlider::handle:horizontal {{ background: {TEXT}; width: 12px; height: 12px; margin: -4px 0; border-radius: 6px; }}
QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 2px; }}
QCheckBox::indicator {{ width: 16px; height: 16px; }}
QToolTip {{ background: {PANEL_2}; color: {TEXT}; border: 1px solid {LINE}; padding: 4px; }}
QScrollBar:vertical {{ background: transparent; width: 10px; }}
QScrollBar::handle:vertical {{ background: {LINE}; border-radius: 4px; min-height: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
"""
