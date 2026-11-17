"""全局外观：设计 token + 样式表（QSS）。

一套 token 三处用：QSS 从这里取色；自绘控件（图标、画布、放大窗）也在函数体里
读这里的模块级常量。所以换肤只走 :func:`set_theme` / :func:`set_accent` ——
它们重写模块常量，``theme.ACCENT`` 这种"调用时才读"的引用下一次重绘就跟着变。

配色分两层：
- **中性层**（bg / surface / sunken / card / hover / pressed / border）：只管明暗，
  不带色相，界面的秩序全靠它；纸张永远是白的，深色主题只把"取景框"变深。
- **语义层**（accent / ok / warn / danger）：只表示状态，不做装饰。

间距一律 4 的倍数（``SPACE_*``），圆角只有三档（``RADIUS_*``），字号只有四档
（``FS_*``）。新写界面请直接用这些名字，别再写魔数字面量。
"""

from __future__ import annotations

import os
import sys

from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication, QStyleFactory

# -- 间距 / 圆角 / 字号（4pt 栅格）-----------------------------------------
SPACE_1 = 4
SPACE_2 = 8
SPACE_3 = 12
SPACE_4 = 16
SPACE_6 = 24

RADIUS_SM = 6          # 菜单项、小标签
RADIUS_MD = 8          # 按钮、输入框、工具按钮
RADIUS_LG = 12         # 卡片、弹层
RADIUS_PILL = 999      # 胶囊

FS_CAPTION = 8         # 辅助说明、状态栏次要读数
FS_BODY = 9            # 正文
FS_SECTION = 10        # 分组标题、状态栏主读数
FS_TITLE = 12          # 空状态标题

FONT_FAMILY = "Microsoft YaHei UI"
FONT_SIZE = FS_BODY

# ------------------------------------------------------------------ 配色 token
# 中性色按"用到的地方"命名，不按深浅编号：换肤时改值不用改名字。
_LIGHT = {
    "BG": "#eef0f4",            # 窗口底色
    "SURFACE": "#ffffff",       # 面板、工具栏、对话框
    "SUNKEN": "#f7f8fa",        # 下沉区（缩略图条、分段控件底槽）
    "CARD": "#fafbfd",          # 面板里的分组卡
    "CANVAS": "#e7eaf0",        # 页面区（比窗口深，白纸才浮得起来）
    "EYE_CANVAS": "#e0e7d6",    # 护眼：淡豆绿
    "HOVER": "#eef1f6",
    "PRESSED": "#e3e8f1",
    "TEXT": "#1b1f24",
    "TEXT_STRONG": "#0f1318",
    "MUTED": "#6b7280",
    "FAINT": "#9aa1ab",
    "BORDER": "#e4e7ec",
    "BORDER_STRONG": "#d5dae2",
    "OK": "#1a7f4b",
    "OK_SOFT": "#e6f5ec",
    "WARN": "#a2670a",
    "WARN_SOFT": "#fff3df",
    "DANGER": "#d93a3f",
    "DANGER_SOFT": "#fdeceb",
    "SELECT_SOFT": "#eef4ff",
    "TOOLTIP_BG": "#1f2328",
    "TOOLTIP_FG": "#ffffff",
    "SCROLL": "#c8ced8",
    "SCROLL_HOVER": "#a8b1bf",
    "ON_ACCENT": "#ffffff",
}

_DARK = {
    "BG": "#101216",
    "SURFACE": "#191c22",
    "SUNKEN": "#14161b",
    "CARD": "#1e222a",
    "CANVAS": "#0b0d11",
    "EYE_CANVAS": "#12160f",
    "HOVER": "#242832",
    "PRESSED": "#2c313c",
    "TEXT": "#e6e9ef",
    "TEXT_STRONG": "#f5f7fa",
    "MUTED": "#9aa3b2",
    "FAINT": "#6b7484",
    "BORDER": "#262b34",
    "BORDER_STRONG": "#39404d",
    "OK": "#4dbd88",
    "OK_SOFT": "#122a1e",
    "WARN": "#e0a640",
    "WARN_SOFT": "#2b2110",
    "DANGER": "#ef6b6f",
    "DANGER_SOFT": "#2c1618",
    "SELECT_SOFT": "#18233a",
    "TOOLTIP_BG": "#2b313b",
    "TOOLTIP_FG": "#f2f5fa",
    "SCROLL": "#3a4250",
    "SCROLL_HOVER": "#4a5364",
    "ON_ACCENT": "#ffffff",
}

_PINK = {
    "BG": "#f4e9ee",            # 窗口底色：一点暖粉的灰
    "SURFACE": "#fffbfc",
    "SUNKEN": "#f7e5ec",        # 面板托盘：比窗口再深一档，卡片才浮得起来
    "CARD": "#fffdfd",
    "CANVAS": "#ecd9e1",        # 页面区：比窗口更粉一档，白纸照样浮得起来
    "EYE_CANVAS": "#dfe8d8",
    "HOVER": "#f9edf2",
    "PRESSED": "#f0d9e2",
    "TEXT": "#251a1f",
    "TEXT_STRONG": "#140d11",
    "MUTED": "#7b6770",
    "FAINT": "#a6919b",
    "BORDER": "#eed7e1",
    "BORDER_STRONG": "#e0becd",
    "OK": "#1a7f4b",
    "OK_SOFT": "#e7f5ec",
    "WARN": "#a2670a",
    "WARN_SOFT": "#fff2dd",
    "DANGER": "#cf3b46",
    "DANGER_SOFT": "#fce9ea",
    "SELECT_SOFT": "#fdeef3",
    "TOOLTIP_BG": "#2c1f26",
    "TOOLTIP_FG": "#fff7fa",
    "SCROLL": "#ddc5cf",
    "SCROLL_HOVER": "#cba9b7",
    "ON_ACCENT": "#ffffff",
}

# 纸张不随主题变：图纸本身必须是白的，否则看颜色不准
PAPER = "#ffffff"
PAPER_EDGE = "#c9ced8"

# 强调色四套：一张图纸看一天，蓝最稳；青偏冷；石墨最静；朱做标记最显眼
_ACCENTS = {
    "blue": {
        "label": "蓝",
        "ACCENT": "#2f6bf6",
        "ACCENT_HOVER": "#2456cf",
        "ACCENT_PRESSED": "#1e4bb8",
        "ACCENT_SOFT": "#e9f0ff",
        "ACCENT_SOFT_DARK": "#17244a",
        "ACCENT_LINE": "#b9cdfb",
        "ACCENT_LINE_DARK": "#2f4677",
    },
    "teal": {
        "label": "青",
        "ACCENT": "#0f8a83",
        "ACCENT_HOVER": "#0b6f69",
        "ACCENT_PRESSED": "#085854",
        "ACCENT_SOFT": "#e2f4f2",
        "ACCENT_SOFT_DARK": "#10312f",
        "ACCENT_LINE": "#a9ded9",
        "ACCENT_LINE_DARK": "#1f5754",
    },
    "graphite": {
        "label": "石墨",
        "ACCENT": "#49525f",
        "ACCENT_HOVER": "#363d48",
        "ACCENT_PRESSED": "#2a3038",
        "ACCENT_SOFT": "#eef1f5",
        "ACCENT_SOFT_DARK": "#262c35",
        "ACCENT_LINE": "#cdd3dc",
        "ACCENT_LINE_DARK": "#414a57",
    },
    "vermilion": {
        "label": "朱",
        "ACCENT": "#d2483a",
        "ACCENT_HOVER": "#b23a2d",
        "ACCENT_PRESSED": "#942d22",
        "ACCENT_SOFT": "#fdeeec",
        "ACCENT_SOFT_DARK": "#331714",
        "ACCENT_LINE": "#f2bdb6",
        "ACCENT_LINE_DARK": "#6b2a23",
    },
    "rose": {
        "label": "玫瑰",
        "ACCENT": "#d1527b",
        "ACCENT_HOVER": "#b23f66",
        "ACCENT_PRESSED": "#943253",
        "ACCENT_SOFT": "#fbe8ef",
        "ACCENT_SOFT_DARK": "#3a1b26",
        "ACCENT_LINE": "#f0c3d3",
        "ACCENT_LINE_DARK": "#6d2c41",
    },
}

# 主题名 → 那套中性色。「pink」是浅色系的粉色变体，图纸照样白纸
_PALETTES = {"light": _LIGHT, "pink": _PINK, "dark": _DARK}
_THEME_LABELS = (("light", "浅色"), ("pink", "浅粉"), ("dark", "深色"))

_THEME = "light"          # light | pink | dark
_ACCENT = "blue"
_eye_care = False
_applied = False
_applied_sheet = ""

# 这些名字由 _reload_tokens() 写进模块全局；写在这里只是给编辑器看类型
BG = SURFACE = SUNKEN = CARD = CANVAS = EYE_CANVAS = GRID_LINE = ""
HOVER = PRESSED = TEXT = TEXT_STRONG = MUTED = FAINT = ""
BORDER = BORDER_STRONG = OK = OK_SOFT = WARN = WARN_SOFT = ""
DANGER = DANGER_SOFT = SELECT_SOFT = TOOLTIP_BG = TOOLTIP_FG = ""
SCROLL = SCROLL_HOVER = ON_ACCENT = ACCENT = ACCENT_HOVER = ""
ACCENT_PRESSED = ACCENT_SOFT = ACCENT_LINE = ""


def _reload_tokens() -> None:
    """把当前主题 + 强调色写进模块常量（自绘代码读的就是这些名字）。"""
    tokens = dict(_PALETTES.get(_THEME, _LIGHT))
    accent = _ACCENTS[_ACCENT]
    dark = _THEME == "dark"
    tokens["ACCENT"] = accent["ACCENT"]
    tokens["ACCENT_HOVER"] = accent["ACCENT_HOVER"]
    tokens["ACCENT_PRESSED"] = accent["ACCENT_PRESSED"]
    tokens["ACCENT_SOFT"] = accent["ACCENT_SOFT_DARK"] if dark else accent["ACCENT_SOFT"]
    tokens["ACCENT_LINE"] = accent["ACCENT_LINE_DARK"] if dark else accent["ACCENT_LINE"]
    globals().update(tokens)


# ------------------------------------------------------------------ 主题 API


def theme_name() -> str:
    """当前主题：``light`` / ``pink`` / ``dark``。"""
    return _THEME


def theme_choices() -> list[tuple[str, str]]:
    """``[(key, 中文名), ...]``，给菜单用。"""
    return list(_THEME_LABELS)


def is_dark() -> bool:
    return _THEME == "dark"


def accent_name() -> str:
    return _ACCENT


def accent_choices() -> list[tuple[str, str]]:
    """``[(key, 中文名), ...]``，给菜单用。"""
    return [(key, value["label"]) for key, value in _ACCENTS.items()]


def eye_care() -> bool:
    """护眼模式开着没有。"""
    return _eye_care


def set_eye_care(on: bool) -> None:
    """切换护眼模式。只改页面区底色——图纸本身还是白纸，颜色不能被带偏。"""
    global _eye_care
    _eye_care = bool(on)


def canvas_color() -> str:
    """页面区底色：普通是冷灰，护眼模式换成淡豆绿。"""
    return EYE_CANVAS if _eye_care else CANVAS


def qcolor(token: str) -> QColor:
    return QColor(token)


def tabular_font(label, size: float | None = None, weight: int | None = None) -> QFont:
    """把数字换成等宽字形——一排读数跳数字时不会左右抖。"""
    font = QFont(FONT_FAMILY, size or FONT_SIZE)
    if weight is not None:
        font.setWeight(QFont.Weight(weight))
    try:  # 6.7+ 才有 setFeature；老版本静默降级
        font.setFeature(QFont.Tag("tnum"), 1)
    except (AttributeError, TypeError):
        pass
    label.setFont(font)
    return font


def set_theme(name: str, app: QApplication | None = None) -> None:
    """切浅色 / 浅粉 / 深色。图纸仍是白纸，只有界面框架换色。"""
    global _THEME, _ACCENT
    key = str(name).lower()
    if key not in _PALETTES:
        key = "light"
    if key == _THEME:
        return
    previous = _THEME
    _THEME = key
    # 粉主题配玫瑰强调色最顺眼：只在用着默认蓝时才顺手换，用户自己挑过的色不动
    if key == "pink" and _ACCENT == "blue":
        _ACCENT = "rose"
    elif previous == "pink" and _ACCENT == "rose":
        _ACCENT = "blue"
    refresh(app)


def set_accent(name: str, app: QApplication | None = None) -> None:
    """换强调色（蓝 / 青 / 石墨 / 朱 / 玫瑰）。"""
    global _ACCENT
    if name not in _ACCENTS or name == _ACCENT:
        return
    _ACCENT = name
    refresh(app)


def refresh(app: QApplication | None = None) -> None:
    """token 变了之后重刷样式表 + 重绘所有窗口。"""
    global _applied_sheet
    _reload_tokens()
    app = app or QApplication.instance()
    if app is None:
        return
    _applied_sheet = ""
    ensure_applied(app)


def _palette() -> QPalette:
    pal = QPalette()
    pal.setColor(QPalette.ColorRole.Window, qcolor(BG))
    pal.setColor(QPalette.ColorRole.WindowText, qcolor(TEXT))
    pal.setColor(QPalette.ColorRole.Base, qcolor(SURFACE))
    pal.setColor(QPalette.ColorRole.AlternateBase, qcolor(SUNKEN))
    pal.setColor(QPalette.ColorRole.Text, qcolor(TEXT))
    pal.setColor(QPalette.ColorRole.Button, qcolor(SURFACE))
    pal.setColor(QPalette.ColorRole.ButtonText, qcolor(TEXT))
    pal.setColor(QPalette.ColorRole.Highlight, qcolor(ACCENT))
    pal.setColor(QPalette.ColorRole.HighlightedText, qcolor(ON_ACCENT))
    pal.setColor(QPalette.ColorRole.ToolTipBase, qcolor(TOOLTIP_BG))
    pal.setColor(QPalette.ColorRole.ToolTipText, qcolor(TOOLTIP_FG))
    pal.setColor(QPalette.ColorRole.PlaceholderText, qcolor(FAINT))
    # 深色下这几个不改，QMenu 的阴影和文字会发灰
    pal.setColor(QPalette.ColorRole.Light, qcolor(SURFACE))
    pal.setColor(QPalette.ColorRole.Midlight, qcolor(BORDER))
    pal.setColor(QPalette.ColorRole.Mid, qcolor(BORDER_STRONG))
    pal.setColor(QPalette.ColorRole.Dark, qcolor(BORDER_STRONG))
    return pal


def style_sheet() -> str:
    """当前主题的 QSS。所有颜色都来自上面的 token，不许出现色值字面量。"""
    return f"""
/* ---- 基础 ---------------------------------------------------------- */
QWidget {{
    color: {TEXT};
    selection-background-color: {ACCENT};
    selection-color: {ON_ACCENT};
}}
QMainWindow, QDialog {{ background: {BG}; }}
QWidget#propPanel, QWidget#thumbHost, QWidget#emptyState {{ background: {SURFACE}; }}
QFrame#hLine {{ background: {BORDER}; max-height: 1px; border: none; }}

/* ---- 菜单 ---------------------------------------------------------- */
QMenuBar {{
    background: {SURFACE};
    border-bottom: 1px solid {BORDER};
    padding: {SPACE_1}px {SPACE_2}px;
}}
QMenuBar::item {{
    background: transparent;
    padding: 6px 12px;
    border-radius: {RADIUS_SM}px;
}}
QMenuBar::item:selected {{ background: {HOVER}; }}
QMenuBar::item:pressed {{ background: {PRESSED}; }}
QMenu {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_LG}px;
    padding: 6px;
}}
QMenu::item {{
    padding: 7px 28px 7px 12px;
    border-radius: {RADIUS_SM}px;
    color: {TEXT};
}}
QMenu::item:selected {{ background: {ACCENT_SOFT}; color: {ACCENT_HOVER}; }}
QMenu::item:disabled {{ color: {FAINT}; }}
QMenu::separator {{ height: 1px; background: {BORDER}; margin: 5px 8px; }}

/* ---- 工具栏 -------------------------------------------------------- */
QToolBar {{
    background: {SURFACE};
    border: none;
    border-bottom: 1px solid {BORDER};
    padding: 6px 10px;
    spacing: 2px;
}}
QToolBar::separator {{ width: 1px; background: {BORDER}; margin: 5px 8px; }}
QToolButton {{
    background: transparent;
    border: 1px solid transparent;
    border-radius: {RADIUS_MD}px;
    padding: 6px 10px;
    color: {TEXT};
}}
QToolButton:hover {{ background: {HOVER}; }}
QToolButton:pressed {{ background: {PRESSED}; }}
QToolButton[compact="true"] {{ padding: 6px 7px; }}
QToolButton:checked {{
    background: {ACCENT_SOFT};
    border-color: {ACCENT_LINE};
    color: {ACCENT_HOVER};
    font-weight: 600;
}}
QToolButton:checked:hover {{ background: {ACCENT_SOFT}; border-color: {ACCENT}; }}
QToolButton:disabled {{ color: {FAINT}; }}
QToolButton::menu-indicator {{ image: none; width: 0px; }}

/* 工具栏里的"视图"分组：一条浅底槽把图标串成一组，不再是散落的按钮 */
QFrame#toolSegment {{
    background: {SUNKEN};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_LG}px;
}}
QFrame#toolSegment QToolButton {{
    border: 1px solid transparent;
    border-radius: {RADIUS_MD}px;
    padding: 5px 8px;
}}
QFrame#toolSegment QToolButton:hover {{ background: {HOVER}; }}
QFrame#toolSegment QToolButton:checked {{
    background: {SURFACE};
    border-color: {BORDER_STRONG};
    color: {ACCENT_HOVER};
    font-weight: 600;
}}

/* ---- 品牌区 -------------------------------------------------------- */
/* 品牌标就是软件图标本身（resources/qpdf.ico），不要再垫一块蓝底 */
QLabel#brandMark {{ background: transparent; border: none; padding: 0px; margin: 0px; }}
QLabel#brandName {{ font-weight: 700; color: {TEXT}; font-size: {FS_SECTION + 1}pt; }}
QLabel#brandVersion {{ color: {FAINT}; font-size: {FS_CAPTION}pt; }}

/* ---- 面板里的自定义小块 -------------------------------------------- */
QLabel#pagePill {{
    background: {SURFACE};
    border: 1px solid {BORDER_STRONG};
    border-radius: 7px;
    padding: 3px 8px;
    font-weight: 600;
    color: {TEXT};
}}
QToolButton#overflowButton {{
    color: {MUTED};
    padding: 2px 8px;
    font-size: 15px;
}}
QToolButton#overflowButton:hover {{ background: {HOVER}; color: {TEXT}; }}
QToolButton#overflowButton::menu-indicator {{ image: none; width: 0px; }}
QLabel#panelHeader {{ color: {FAINT}; font-size: {FS_CAPTION}pt; }}
QLabel#panelTitle {{ font-weight: 700; font-size: {FS_SECTION}pt; }}
QLabel#panelHint {{ color: {MUTED}; font-size: {FS_CAPTION}pt; }}
QLabel#groupTitle {{ color: {MUTED}; font-size: {FS_CAPTION}pt; font-weight: 600; }}
QLabel#muted {{ color: {MUTED}; }}

/* 属性面板的分组卡：一整块浅底 + 12 圆角，代替一排裸提示文字 */
QFrame#groupCard {{
    background: {CARD};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_LG}px;
}}
QFrame#groupCard QLabel#groupTitle {{ padding: 0px; }}

/* ---- 空状态 -------------------------------------------------------- */
QLabel#emptyTitle {{ font-size: {FS_TITLE}pt; font-weight: 600; color: {TEXT}; }}
QLabel#emptyHint {{ color: {MUTED}; }}
QLabel#emptyDrop {{
    border: 2px dashed {BORDER_STRONG};
    border-radius: {RADIUS_LG}px;
    color: {FAINT};
    padding: 28px 34px;
}}
QLabel#emptyDrop:hover {{ border-color: {ACCENT_LINE}; color: {MUTED}; }}
QFrame#emptyCard {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 16px;
}}
QLabel#emptyMark {{
    background: {SUNKEN};
    border: 1px solid {BORDER};
    border-radius: 16px;
}}
QToolButton#emptyAction {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_MD}px;
    color: {TEXT};
    padding: 5px 10px;
    text-align: left;
}}
QToolButton#emptyAction:hover {{ background: {HOVER}; border-color: {ACCENT_LINE}; }}

/* ---- 状态栏 -------------------------------------------------------- */
QStatusBar {{
    background: {SURFACE};
    border-top: 1px solid {BORDER};
    color: {MUTED};
}}
QStatusBar::item {{ border: none; }}
QStatusBar QLabel {{ color: {MUTED}; padding: 0 2px; }}
QLabel#statusPrimary {{ font-weight: 600; color: {TEXT}; font-size: {FS_SECTION}pt; }}
QLabel#statusDim {{ color: {FAINT}; font-size: {FS_CAPTION}pt; }}
QLabel#modeChip {{
    background: {ACCENT_SOFT};
    color: {ACCENT_HOVER};
    border: 1px solid {ACCENT_LINE};
    border-radius: {RADIUS_PILL}px;
    padding: 1px 10px;
    font-weight: 600;
}}
QFrame#statusSep {{ background: {BORDER}; }}
QFrame#vSep {{ background: {BORDER}; }}

/* ---- 分割条 / 停靠面板 --------------------------------------------- */
QSplitter::handle {{ background: {BORDER}; }}
QSplitter::handle:horizontal {{ width: 1px; }}
QSplitter::handle:hover {{ background: {ACCENT_LINE}; }}
QDockWidget {{ color: {MUTED}; }}
QDockWidget::title {{
    background: {SURFACE};
    padding: {SPACE_2}px {SPACE_3}px;
    border-bottom: 1px solid {BORDER};
    text-align: left;
    font-size: {FS_CAPTION}pt;
    font-weight: 600;
    color: {MUTED};
}}
QDockWidget > QWidget {{ background: {SURFACE}; }}
/* 属性面板：整块下沉一档（SUNKEN）当"托盘"，里面的卡片才是白的（CARD），
   这样面板本身和画布、卡片三层深浅分明，不会糊成一片。 */
QDockWidget#properties_dock {{
    background: {SUNKEN};
    border-left: 1px solid {BORDER_STRONG};
}}
QDockWidget#properties_dock::title {{
    background: {SURFACE};
    border-bottom: 1px solid {BORDER_STRONG};
    padding: {SPACE_2}px {SPACE_3}px;
}}
QDockWidget#properties_dock > QWidget {{ background: {SUNKEN}; }}
QWidget#propPanel {{
    background: {SUNKEN};
}}

/* ---- 缩略图 -------------------------------------------------------- */
QListWidget#thumbList {{
    background: {SUNKEN};
    border: none;
    border-right: 1px solid {BORDER};
    outline: 0;
    padding: 6px;
}}
QListWidget#thumbList::item {{
    border: 1px solid transparent;
    border-radius: {RADIUS_MD}px;
    padding: 5px;
    margin: 3px 2px;
    color: {MUTED};
}}
QListWidget#thumbList::item:hover {{ background: {HOVER}; border-color: {BORDER}; }}
QListWidget#thumbList::item:selected {{
    background: {ACCENT_SOFT};
    border: 1px solid {ACCENT_LINE};
    color: {ACCENT_HOVER};
}}

/* ---- 管线号目录（F01）---------------------------------------------- */
QTreeWidget#pipeTree {{
    background: {SUNKEN};
    border: none;
    border-right: 1px solid {BORDER};
    outline: 0;
    padding: 4px 6px;
}}
QTreeWidget#pipeTree::item {{
    padding: 3px 2px;
    border-radius: {RADIUS_SM}px;
    color: {TEXT};
}}
QTreeWidget#pipeTree::item:hover {{ background: {HOVER}; }}
QTreeWidget#pipeTree::item:selected {{
    background: {ACCENT_SOFT};
    color: {ACCENT_HOVER};
}}

/* ---- 左侧标签页（页面 / 目录 / 目录树）------------------------------
   做成"分段控件"：整条 tab 栏是一块浅底槽，选中项是浮起来的一张白卡。
   整栏给一层白底 + 右侧实线，和窗口底色拉开，不再"融进背景"。 */
QTabWidget#leftTabs {{
    background: {SURFACE};
    border-right: 1px solid {BORDER_STRONG};
}}
QTabWidget#leftTabs::pane {{ border: none; background: {SURFACE}; }}
QTabWidget#leftTabs > QWidget {{ background: {SURFACE}; }}
QTabWidget#leftTabs QTabBar {{
    background: {SUNKEN};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_MD}px;
    margin: {SPACE_2}px {SPACE_2}px {SPACE_1}px {SPACE_2}px;
    padding: 2px;
}}
QTabWidget#leftTabs QTabBar::tab {{
    background: transparent;
    color: {MUTED};
    border: 1px solid transparent;
    border-radius: 7px;
    padding: 5px 12px;
    margin: 0px 1px;
}}
QTabWidget#leftTabs QTabBar::tab:selected {{
    background: {SURFACE};
    border-color: {BORDER_STRONG};
    color: {TEXT};
    font-weight: 600;
}}
QTabWidget#leftTabs QTabBar::tab:hover:!selected {{ color: {TEXT}; background: {HOVER}; }}

/* ---- 顶部文档标签（多开）------------------------------------------- */
QTabBar#docTabs {{ background: transparent; }}
QTabBar#docTabs::tab {{
    background: {SURFACE};
    color: {MUTED};
    border: 1px solid {BORDER};
    border-bottom: none;
    border-top-left-radius: {RADIUS_MD}px;
    border-top-right-radius: {RADIUS_MD}px;
    padding: 5px 10px 5px 12px;
    margin-right: 3px;
    max-width: 230px;
}}
QTabBar#docTabs::tab:selected {{
    background: {CANVAS};
    color: {TEXT};
    border-color: {BORDER};
    font-weight: 600;
}}
QTabBar#docTabs::tab:hover:!selected {{ color: {TEXT}; }}
QTabBar#docTabs::tab:last {{ margin-right: 0; }}
QToolButton#newTabButton {{
    color: {MUTED};
    border: 1px solid transparent;
    border-radius: {RADIUS_SM}px;
    padding: 2px 8px;
    font-size: 16px;
}}
QToolButton#newTabButton:hover {{ background: {HOVER}; border-color: {BORDER}; color: {TEXT}; }}

/* ---- 输入控件 ------------------------------------------------------ */
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QFontComboBox {{
    background: {SURFACE};
    border: 1px solid {BORDER_STRONG};
    border-radius: {RADIUS_MD}px;
    padding: 5px 8px;
    min-height: 18px;
    color: {TEXT};
}}
QLineEdit:hover, QSpinBox:hover, QDoubleSpinBox:hover,
QComboBox:hover, QFontComboBox:hover {{ border-color: {FAINT}; }}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus,
QComboBox:focus, QFontComboBox:focus {{ border-color: {ACCENT}; }}
QLineEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled,
QComboBox:disabled, QFontComboBox:disabled {{ color: {FAINT}; background: {SUNKEN}; }}
QComboBox::drop-down {{ border: none; width: 20px; }}
QComboBox QAbstractItemView {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_MD}px;
    padding: 4px;
    selection-background-color: {ACCENT_SOFT};
    selection-color: {ACCENT_HOVER};
    outline: 0;
}}
QSpinBox::up-button, QDoubleSpinBox::up-button,
QSpinBox::down-button, QDoubleSpinBox::down-button {{
    width: 16px;
    border: none;
    background: transparent;
}}

/* ---- 按钮 ---------------------------------------------------------- */
QPushButton {{
    background: {SURFACE};
    border: 1px solid {BORDER_STRONG};
    border-radius: {RADIUS_MD}px;
    padding: 7px 16px;
    color: {TEXT};
}}
QPushButton:hover {{ background: {HOVER}; }}
QPushButton:pressed {{ background: {PRESSED}; }}
QPushButton:default {{ border-color: {ACCENT}; color: {ACCENT_HOVER}; }}
QPushButton:disabled {{ color: {FAINT}; background: {SUNKEN}; border-color: {BORDER}; }}
QPushButton[primary="true"] {{
    background: {ACCENT};
    border-color: {ACCENT};
    color: {ON_ACCENT};
    font-weight: 600;
}}
QPushButton[primary="true"]:hover {{ background: {ACCENT_HOVER}; border-color: {ACCENT_HOVER}; }}
QPushButton[primary="true"]:pressed {{ background: {ACCENT_PRESSED}; border-color: {ACCENT_PRESSED}; }}
QToolButton#swatch {{
    border: 1px solid {BORDER_STRONG};
    border-radius: {RADIUS_MD}px;
    padding: 0px;
}}
QToolButton#swatch:hover {{ border-color: {ACCENT}; }}
QToolButton#segBtn {{
    padding: 0px;
    border: 1px solid {BORDER};
    border-radius: 7px;
    background: {SURFACE};
    color: {MUTED};
}}
QToolButton#segBtn:hover {{
    background: {HOVER};
    border-color: {BORDER_STRONG};
    color: {TEXT};
}}
QToolButton#segBtn:checked {{
    background: {ACCENT_SOFT};
    border-color: {ACCENT_LINE};
    color: {ACCENT_HOVER};
}}

/* ---- 单选 / 复选 --------------------------------------------------- */
QRadioButton, QCheckBox {{ spacing: 8px; padding: 2px 0; }}
QRadioButton:disabled, QCheckBox:disabled {{ color: {FAINT}; }}

/* ---- 滑动条 -------------------------------------------------------- */
QSlider::groove:horizontal {{
    height: 4px;
    border-radius: 2px;
    background: {BORDER_STRONG};
}}
QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 2px; }}
QSlider::handle:horizontal {{
    width: 12px;
    height: 12px;
    margin: -5px 0;
    border-radius: 7px;
    background: {SURFACE};
    border: 2px solid {ACCENT};
}}
QSlider::handle:horizontal:hover {{ border-color: {ACCENT_HOVER}; }}

/* ---- 进度条 -------------------------------------------------------- */
QProgressBar {{
    border: none;
    border-radius: {RADIUS_SM}px;
    background: {SUNKEN};
    color: {MUTED};
    font-size: {FS_CAPTION}pt;
    text-align: center;
    max-height: 12px;
}}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: {RADIUS_SM}px; }}

/* ---- 滚动条 -------------------------------------------------------- */
QScrollBar:vertical {{ background: transparent; width: 8px; margin: 2px; }}
QScrollBar::handle:vertical {{
    background: {SCROLL};
    border-radius: 4px;
    min-height: 30px;
}}
QScrollBar::handle:vertical:hover {{ background: {SCROLL_HOVER}; }}
QScrollBar:horizontal {{ background: transparent; height: 8px; margin: 2px; }}
QScrollBar::handle:horizontal {{
    background: {SCROLL};
    border-radius: 4px;
    min-width: 30px;
}}
QScrollBar::handle:horizontal:hover {{ background: {SCROLL_HOVER}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0px; height: 0px; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
/* 画布自己就是一块"台面"：滚动条那条窄边也用它，别露出窗口底色成一条竖杠 */
QGraphicsView#pdfViewer {{ background: {CANVAS}; border: none; }}

/* ---- 画布 ---------------------------------------------------------- */
QGraphicsView {{ border: none; }}
QRubberBand {{
    background: rgba(47, 107, 246, 0.12);
    border: 1px solid {ACCENT};
}}

/* ---- 提示 ---------------------------------------------------------- */
QToolTip {{
    background: {TOOLTIP_BG};
    color: {TOOLTIP_FG};
    border: none;
    border-radius: {RADIUS_SM}px;
    padding: 6px 9px;
}}
"""


def mark_primary(button) -> None:
    """把某个按钮标成主按钮（强调色实心）。"""
    button.setProperty("primary", True)
    style = button.style()
    style.unpolish(button)
    style.polish(button)


def ensure_applied(app: QApplication | None = None) -> None:
    """给整个应用套上主题。主题没变时重复调用无副作用。"""
    global _applied, _applied_sheet
    app = app or QApplication.instance()
    if app is None:
        return
    style = QStyleFactory.create("Fusion")
    if style is not None:
        app.setStyle(style)
    app.setPalette(_palette())
    font = QFont(FONT_FAMILY, FONT_SIZE)
    font.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
    app.setFont(font)
    sheet = style_sheet()
    if not _applied or sheet != _applied_sheet:
        app.setStyleSheet(sheet)
        _applied = True
        _applied_sheet = sheet
        # 自绘控件读的是 token，换了之后要重画一遍。
        # 有些 QAbstractItemView 派生的类在 PySide 里只暴露 update(rect)，
        # 无参调用会抛 TypeError，这里退回 repaint()。
        for widget in app.allWidgets():
            try:
                widget.update()
            except TypeError:
                widget.repaint()


def resource(*parts: str) -> str:
    """源码运行时取项目目录，打包后取 _MEIPASS（resources 已随包打进 _internal）。"""
    base = getattr(sys, "_MEIPASS", None)
    if not base:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, *parts)


_WALLPAPER_CACHE: dict = {"key": None, "pixmap": None}


def wallpaper():
    """浅粉主题的画布底图：resources/bg.png（开发时也认程序根目录的 bg.png）。

    其它主题返回 None（画布用纯色底）。按 (路径, 修改时间) 缓存，只在切主题或
    换图之后重新读盘。
    """
    if _THEME != "pink":
        return None
    from PySide6.QtGui import QPixmap

    for path in (resource("resources", "bg.png"), resource("bg.png")):
        if not os.path.isfile(path):
            continue
        try:
            stamp = os.path.getmtime(path)
        except OSError:
            stamp = 0.0
        key = (path, stamp)
        if _WALLPAPER_CACHE["key"] == key and _WALLPAPER_CACHE["pixmap"] is not None:
            return _WALLPAPER_CACHE["pixmap"]
        pixmap = QPixmap(path)
        if pixmap.isNull():
            continue
        _WALLPAPER_CACHE["key"] = key
        _WALLPAPER_CACHE["pixmap"] = pixmap
        return pixmap
    return None


def app_icon():
    """软件图标：resources/qpdf.ico（8que.png 打的 7 种尺寸）。"""
    from PySide6.QtGui import QIcon

    path = resource("resources", "qpdf.ico")
    return QIcon(path) if os.path.isfile(path) else QIcon()


_reload_tokens()
STYLE_SHEET = style_sheet()
