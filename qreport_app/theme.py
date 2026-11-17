"""QReport 的配色与样式表（浅色 / 深色两套，代码画图标，不带任何图片素材）。

和 QPDF 主程序保持"同一个家族"的观感（冷灰底、单一强调蓝），但更现代化：
大圆角卡片、柔和描边、强调色渐变的主数字卡片、无网格表格。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Palette:
    bg: str
    surface: str
    surface2: str
    border: str
    text: str
    muted: str
    faint: str
    accent: str
    accent2: str
    accent_soft: str
    hero_from: str
    hero_to: str
    danger: str
    success: str
    warn: str
    shadow: str


LIGHT = Palette(
    bg="#f2f4f9",
    surface="#ffffff",
    surface2="#f7f9fd",
    border="#e3e8f2",
    text="#111a2c",
    muted="#5b6478",
    faint="#98a2b3",
    accent="#2f6bf6",
    accent2="#7c5cff",
    accent_soft="#eaf0ff",
    hero_from="#2f6bf6",
    hero_to="#7c5cff",
    danger="#d92d20",
    success="#12a150",
    warn="#e08a00",
    shadow="rgba(17, 26, 44, 0.10)",
)

DARK = Palette(
    bg="#0d1017",
    surface="#151924",
    surface2="#1b2130",
    border="#283043",
    text="#eef2fa",
    muted="#a3aec3",
    faint="#6f7a90",
    accent="#6d9bff",
    accent2="#a48bff",
    accent_soft="#1d2b4d",
    hero_from="#2b5bd7",
    hero_to="#6f52d0",
    danger="#ff6b64",
    success="#3ddc97",
    warn="#ffb547",
    shadow="rgba(0, 0, 0, 0.45)",
)


def stylesheet(palette: Palette) -> str:
    p = palette
    return f"""
QWidget {{
    color: {p.text};
    font-family: "Microsoft YaHei UI", "Segoe UI", sans-serif;
    font-size: 13px;
}}
QMainWindow, QWidget#root {{ background: {p.bg}; }}
QWidget#content {{ background: transparent; }}

QFrame#sidebar {{
    background: {p.surface};
    border: 1px solid {p.border};
    border-radius: 16px;
}}
QFrame#card {{
    background: {p.surface2};
    border: 1px solid {p.border};
    border-radius: 12px;
}}
QFrame#hero {{
    border: none;
    border-radius: 16px;
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                                stop:0 {p.hero_from}, stop:1 {p.hero_to});
}}
QLabel#heroCaption {{ color: rgba(255,255,255,0.82); font-size: 12px; letter-spacing: 1px; }}
QLabel#heroNumber {{ color: #ffffff; font-size: 44px; font-weight: 700; }}
QLabel#heroSuffix {{ color: rgba(255,255,255,0.86); font-size: 14px; }}

QLabel#brand {{ font-size: 19px; font-weight: 700; letter-spacing: 0.5px; }}
QLabel#brandSub, QLabel#hint {{ color: {p.muted}; font-size: 12px; }}
QLabel#sectionTitle {{ color: {p.muted}; font-size: 12px; font-weight: 600; letter-spacing: 1px; }}
QLabel#tagTitle {{ font-size: 20px; font-weight: 700; }}
QLabel#cardTitle {{ color: {p.muted}; font-size: 12px; }}
QLabel#cardValue {{ font-size: 13px; font-weight: 600; }}
QLabel#status {{ color: {p.muted}; font-size: 12px; }}
QLabel#chip {{
    background: {p.accent_soft};
    color: {p.accent};
    border-radius: 9px;
    padding: 3px 10px;
    font-size: 12px;
    font-weight: 600;
}}
QLabel#chipWarn {{
    background: rgba(224, 138, 0, 0.14);
    color: {p.warn};
    border-radius: 9px;
    padding: 3px 10px;
    font-size: 12px;
    font-weight: 600;
}}

QPushButton {{
    background: {p.surface};
    border: 1px solid {p.border};
    border-radius: 10px;
    padding: 8px 14px;
    color: {p.text};
}}
QPushButton:hover {{ border-color: {p.accent}; color: {p.accent}; }}
QPushButton:pressed {{ background: {p.accent_soft}; }}
QPushButton:disabled {{ color: {p.faint}; border-color: {p.border}; }}
QPushButton#primary {{
    background: {p.accent};
    border: 1px solid {p.accent};
    color: #ffffff;
    font-weight: 600;
}}
QPushButton#primary:hover {{ background: {p.accent2}; border-color: {p.accent2}; }}
QPushButton#primary:disabled {{ background: {p.faint}; border-color: {p.faint}; }}
QPushButton#ghost {{
    background: transparent;
    border: 1px solid transparent;
    color: {p.muted};
}}
QPushButton#ghost:hover {{ background: {p.surface2}; color: {p.text}; border-color: {p.border}; }}
QPushButton#segment {{
    background: transparent;
    border: none;
    border-radius: 9px;
    padding: 7px 16px;
    color: {p.muted};
}}
QPushButton#segment:hover {{ color: {p.text}; }}
QPushButton#segment:checked {{
    background: {p.surface};
    color: {p.accent};
    font-weight: 600;
    border: 1px solid {p.border};
}}

QLineEdit {{
    background: {p.surface};
    border: 1px solid {p.border};
    border-radius: 10px;
    padding: 8px 12px;
    selection-background-color: {p.accent};
}}
QLineEdit:focus {{ border-color: {p.accent}; }}

QListView#tagList {{
    background: transparent;
    border: none;
    outline: 0;
}}
QListView#tagList::item {{ border-radius: 9px; margin: 2px 2px; }}
QListView#tagList::item:hover {{ background: {p.surface2}; }}
QListView#tagList::item:selected {{ background: {p.accent_soft}; }}

QTableView {{
    background: {p.surface};
    alternate-background-color: {p.surface2};
    border: 1px solid {p.border};
    border-radius: 12px;
    gridline-color: transparent;
    selection-background-color: {p.accent_soft};
    selection-color: {p.text};
}}
QTableView::item {{ padding: 6px 10px; border: none; }}
QTableView::item:hover {{ background: {p.surface2}; }}
QHeaderView::section {{
    background: {p.surface2};
    color: {p.muted};
    border: none;
    border-bottom: 1px solid {p.border};
    padding: 9px 10px;
    font-size: 12px;
    font-weight: 600;
}}
QTableCornerButton::section {{ background: {p.surface2}; border: none; }}

QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {p.border}; border-radius: 4px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: {p.faint}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {p.border}; border-radius: 4px; min-width: 30px; }}

QToolTip {{
    background: {p.text};
    color: {p.surface};
    border: none;
    padding: 6px 8px;
    border-radius: 6px;
}}
"""
