"""快捷键表：把「哪个功能默认是什么键」集中在一处，方便设置里改、也方便外面查。

默认值就是之前写死在 ``ui/main_window.py`` 里的那些，改之前先看这里——
用户已经按熟了的键别乱动。
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtGui import QAction, QKeySequence


@dataclass(frozen=True)
class Shortcut:
    key: str
    label: str
    default: str


#: 顺序就是设置面板里的显示顺序
SHORTCUTS: tuple[Shortcut, ...] = (
    Shortcut("open", "打开 PDF", "Ctrl+O"),
    Shortcut("save", "保存项目", "Ctrl+S"),
    Shortcut("save_as", "项目另存为", "Ctrl+Shift+S"),
    Shortcut("export", "导出 PDF", "Ctrl+E"),
    Shortcut("close", "关闭文档", "Ctrl+W"),
    Shortcut("goto", "跳到页码", "Ctrl+G"),
    Shortcut("prev", "上一页", "PgUp"),
    Shortcut("next", "下一页", "PgDown"),
    Shortcut("zoom_in", "放大", "Ctrl+="),
    Shortcut("zoom_out", "缩小", "Ctrl+-"),
    Shortcut("fit_page", "适应整页", "Ctrl+1"),
    Shortcut("fit_width", "适应宽度", "Ctrl+2"),
    Shortcut("actual", "实际大小", "Ctrl+0"),
    Shortcut("thumbs", "左侧页面栏", "Ctrl+B"),
    Shortcut("toc", "左侧目录栏", "Ctrl+Shift+T"),
    Shortcut("pipe_tree", "左侧管线号栏", "Ctrl+Shift+I"),
    Shortcut("undo", "撤销", "Ctrl+Z"),
    Shortcut("redo", "重做", "Ctrl+Y"),
    Shortcut("delete", "删除选中标注", "Delete"),
    Shortcut("edit_text", "编辑标注文字", "Return"),
    Shortcut("copy", "复制标注", "Ctrl+C"),
    Shortcut("paste", "粘贴标注", "Ctrl+V"),
    Shortcut("lock", "锁定/解锁", "Ctrl+L"),
    Shortcut("hide", "隐藏选中", "Ctrl+H"),
    Shortcut("show_all", "显示全部", "Ctrl+U"),
    Shortcut("mode_text", "文字标注模式", "F5"),
    Shortcut("mode_number", "数字标注模式", "F2"),
    Shortcut("mode_overwrite", "覆盖编号模式", "F3"),
    Shortcut("mode_insert", "插队编号模式", "F4"),
    Shortcut("mode_select", "框选删除模式", "F6"),
    Shortcut("match_data", "数据匹配标注", "Ctrl+Shift+M"),
    Shortcut("scan_all", "识别区域 / 目录", "Ctrl+Shift+O"),
    Shortcut("material_scan", "材料表识别", "Ctrl+Shift+D"),
    Shortcut("page_number", "插入/设置页码", "Ctrl+Shift+N"),
    Shortcut("magnifier", "放大窗开关", "Ctrl+Shift+L"),
    Shortcut("magnifier_new", "新建放大窗", ""),
    Shortcut("magnifier_region", "设置放大区域", ""),
    Shortcut("workbench", "工作台", "F7"),
    Shortcut("add_to_board", "放到台上", "F8"),
    Shortcut("restart_number", "重新从 1 编号", "Ctrl+Shift+R"),
    Shortcut("eye_care", "护眼底色", "Ctrl+Shift+E"),
    Shortcut("settings", "设置", "Ctrl+,"),
)

DEFAULTS: dict[str, str] = {item.key: item.default for item in SHORTCUTS}
LABELS: dict[str, str] = {item.key: item.label for item in SHORTCUTS}

#: 鼠标侧键：Qt 的 QKeySequence 表示不了鼠标键，单独当两条「快捷键」存
MOUSE_KEYS: tuple[tuple[str, str], ...] = (
    ("mouse1", "鼠标侧键 1（靠后那侧）"),
    ("mouse2", "鼠标侧键 2（靠前那侧）"),
)
MOUSE_DEFAULTS: dict[str, str] = {key: "" for key, _ in MOUSE_KEYS}
MOUSE_LABELS: dict[str, str] = dict(MOUSE_KEYS)

_PREFIX = "shortcuts/"


def mouse_action(settings, key: str) -> str:
    """侧键绑到哪个功能（返回功能的 key，空表示没绑）。"""
    if settings is None:
        return ""
    value = settings.value(_PREFIX + key, "", type=str) or ""
    return value if value in LABELS else ""


def save_mouse(settings, values: dict[str, str]) -> None:
    if settings is None:
        return
    for key, _label in MOUSE_KEYS:
        action = (values.get(key) or "").strip()
        if action in LABELS:
            settings.setValue(_PREFIX + key, action)
        elif settings.contains(_PREFIX + key):
            settings.remove(_PREFIX + key)


def normalize(text: str) -> str:
    """把用户输入的键写法统一成 Qt 的标准写法（``ctrl+shift+t`` → ``Ctrl+Shift+T``）。"""
    text = (text or "").strip()
    if not text:
        return ""
    seq = QKeySequence(text)
    if seq.isEmpty():
        return ""
    return seq.toString(QKeySequence.SequenceFormat.PortableText)


def text_for(settings, key: str) -> str:
    """当前生效的键：设置里存过就用存的，没存过/存成空就用默认。"""
    default = DEFAULTS.get(key, "")
    if settings is None:
        return default
    stored = settings.value(_PREFIX + key, "", type=str)
    stored = normalize(stored) if stored else ""
    return stored or default


def load_all(settings) -> dict[str, str]:
    return {item.key: text_for(settings, item.key) for item in SHORTCUTS}


def save(settings, values: dict[str, str]) -> None:
    """只存跟默认不一样的；跟默认一样就把这一项删掉，免得以后改默认值改不动。"""
    if settings is None:
        return
    for key, text in values.items():
        if key not in DEFAULTS:
            continue
        text = normalize(text)
        if text == DEFAULTS[key]:
            if settings.contains(_PREFIX + key):
                settings.remove(_PREFIX + key)
        elif text:
            settings.setValue(_PREFIX + key, text)
        else:
            if settings.contains(_PREFIX + key):
                settings.remove(_PREFIX + key)


def apply(actions: dict[str, QAction], settings) -> None:
    """把当前生效的键写回 QAction（设置里改完立刻生效）。"""
    for item in SHORTCUTS:
        action = actions.get(item.key)
        if action is None:
            continue
        text = normalize(text_for(settings, item.key))
        if text:
            action.setShortcut(QKeySequence(text))
        else:
            action.setShortcut(QKeySequence())


def conflicts(values: dict[str, str]) -> list[tuple[str, list[str]]]:
    """返回「同一个键被几个功能占着」：[(键, [功能名…]), …]。"""
    buckets: dict[str, list[str]] = {}
    for item in SHORTCUTS:
        text = normalize(values.get(item.key, item.default))
        if not text:
            continue
        buckets.setdefault(text, []).append(item.label)
    return [(text, labels) for text, labels in buckets.items() if len(labels) > 1]
