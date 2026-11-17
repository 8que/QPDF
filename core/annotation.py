"""标注数据模型（第 8 章）。

这一层完全不依赖界面：坐标是 **PDF 页面坐标（pt）**，不含任何像素值。
Phase 6 把 ``Annotation`` 原样序列化进 SQLite（``.qpdf``），所以字段名和
第 8.1 节表格一一对应，改字段等于改库结构，谨慎。

两个设计要点：

1. **对象不可变（frozen）**。任何修改都产生新实例，撤销栈里直接存新旧两份
   快照，天然没有别名污染问题。
2. **数字标注不存显示文本**。只存 ``seq_group`` + ``seq_index``，显示值由
   编号序列现算（见 Phase 3）——插队时只改序号，不用改文本。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, replace

from PySide6.QtCore import QObject, Signal

KIND_TEXT = "text"
KIND_NUMBER = "number"

ALIGN_LEFT = "left"
ALIGN_CENTER = "center"
ALIGN_RIGHT = "right"


def _now() -> float:
    return time.time()


def new_id() -> str:
    return uuid.uuid4().hex[:12]


@dataclass(frozen=True)
class AnnotationStyle:
    """标注的显示样式。数字标注和文字标注共用。"""

    font_family: str = "Microsoft YaHei UI"
    font_size: float = 12.0
    color: str = "#d0342c"
    bold: bool = False
    italic: bool = False
    opacity: int = 100  # 20–100，百分比
    align: str = ALIGN_LEFT

    def to_dict(self) -> dict:
        return {
            "font_family": self.font_family,
            "font_size": self.font_size,
            "color": self.color,
            "bold": self.bold,
            "italic": self.italic,
            "opacity": self.opacity,
            "align": self.align,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "AnnotationStyle":
        return cls(**(data or {}))


@dataclass(frozen=True)
class Annotation:
    """一条标注。坐标为页面左上角起算的 PDF 点（pt）。"""

    id: str = field(default_factory=new_id)
    kind: str = KIND_TEXT
    page: int = 0
    x: float = 0.0
    y: float = 0.0
    content: str = ""
    style: AnnotationStyle = field(default_factory=AnnotationStyle)
    seq_group: str | None = None
    seq_index: int | None = None
    #: 第 8.3 节：锁定后不能拖动 / 删除 / 改内容（仍可选中查看）
    locked: bool = False
    #: 第 8.3 节：隐藏后不绘制，也不参与框选和删除
    hidden: bool = False
    created_at: float = field(default_factory=_now)
    updated_at: float = field(default_factory=_now)

    # -- 派生属性 ---------------------------------------------------------

    @property
    def is_number(self) -> bool:
        return self.kind == KIND_NUMBER

    def moved_to(self, x: float, y: float, page: int | None = None) -> "Annotation":
        update = {"x": float(x), "y": float(y), "updated_at": _now()}
        if page is not None:
            update["page"] = int(page)
        return replace(self, **update)

    def with_content(self, content: str) -> "Annotation":
        return replace(self, content=content, updated_at=_now())

    def with_style(self, style: AnnotationStyle) -> "Annotation":
        return replace(self, style=style, updated_at=_now())

    def with_sequence(self, group: str, index: int) -> "Annotation":
        """插队用：只改序号，显示文本会自动跟着变（不写 content）。"""
        return replace(self, seq_group=group, seq_index=int(index), updated_at=_now())

    def with_locked(self, locked: bool) -> "Annotation":
        return replace(self, locked=bool(locked), updated_at=_now())

    def with_hidden(self, hidden: bool) -> "Annotation":
        return replace(self, hidden=bool(hidden), updated_at=_now())

    def toggled_lock(self) -> "Annotation":
        return self.with_locked(not self.locked)

    def toggled_hidden(self) -> "Annotation":
        return self.with_hidden(not self.hidden)

    # -- 序列化（Phase 6 用）----------------------------------------------

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "page": self.page,
            "x": self.x,
            "y": self.y,
            "content": self.content,
            "style": self.style.to_dict(),
            "seq_group": self.seq_group,
            "seq_index": self.seq_index,
            "locked": self.locked,
            "hidden": self.hidden,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Annotation":
        payload = dict(data)
        payload["style"] = AnnotationStyle.from_dict(payload.get("style"))
        return cls(**payload)


class AnnotationStore(QObject):
    """内存中的标注集合。

    所有增删改都发信号，界面层只负责跟着刷新；撤销/重做命令直接调这三个
    方法，因此界面和历史的耦合为零。
    """

    added = Signal(object)  # Annotation
    removed = Signal(str)  # id
    changed = Signal(object)  # 新的 Annotation
    cleared = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._items: dict[str, Annotation] = {}
        self._order: list[str] = []
        #: 被隐藏的条数。界面每次刷新都要问"有没有隐藏的标注"（决定「全部显示」
        #: 按钮亮不亮），现成扫一遍 store 是 O(n)——加载几千条标注时每条 added
        #: 都扫一次，光这一项就是几百万次属性读取，所以维护成计数器。
        self._hidden_count = 0

    # -- 查询 -------------------------------------------------------------

    def __len__(self) -> int:
        return len(self._items)

    @property
    def hidden_count(self) -> int:
        """被隐藏的标注条数（O(1)）。"""
        return self._hidden_count

    def get(self, ann_id: str) -> Annotation | None:
        return self._items.get(ann_id)

    def all(self) -> list[Annotation]:
        return [self._items[i] for i in self._order if i in self._items]

    def on_page(self, page: int) -> list[Annotation]:
        return [self._items[i] for i in self._order if i in self._items and self._items[i].page == page]

    def pages(self) -> set[int]:
        return {ann.page for ann in self._items.values()}

    # -- 增删改 -----------------------------------------------------------

    def add(self, ann: Annotation) -> None:
        if ann.id in self._items:
            return
        self._items[ann.id] = ann
        self._order.append(ann.id)
        if ann.hidden:
            self._hidden_count += 1
        self.added.emit(ann)

    def remove(self, ann_id: str) -> Annotation | None:
        ann = self._items.pop(ann_id, None)
        if ann is None:
            return None
        if ann.hidden:
            self._hidden_count = max(0, self._hidden_count - 1)
        try:
            self._order.remove(ann_id)
        except ValueError:
            pass
        self.removed.emit(ann_id)
        return ann

    def replace(self, ann: Annotation) -> None:
        if ann.id not in self._items:
            return
        old = self._items[ann.id]
        if old.hidden != ann.hidden:
            self._hidden_count += 1 if ann.hidden else -1
            self._hidden_count = max(0, self._hidden_count)
        self._items[ann.id] = ann
        self.changed.emit(ann)

    def clear(self) -> None:
        self._items.clear()
        self._order.clear()
        self._hidden_count = 0
        self.cleared.emit()
