"""一个标签页 = 一份 PDF + 它自己的项目会话。

多标签页最容易写崩的地方是「共用一套全局状态」：标注库、撤销栈、编号、
工作台、项目文件、渲染缓存，全都得各管各的。这里把这一整套打包成一个
``DocumentSession``，主窗口切换标签时只是把下面这些引用整体换掉，
各功能模块内部的逻辑一行都不用改。
"""

from __future__ import annotations

import os
from typing import Any

from PySide6.QtCore import QObject
from PySide6.QtGui import QUndoStack

from core.annotation import AnnotationStore
from core.project_manager import ProjectManager
from core.render_manager import RenderManager
from core.sequence import SequenceManager
from core.workbench import BoardStore


class DocumentSession(QObject):
    """一份打开着的文档：PDF 句柄 + 项目 + 标注 + 界面状态。"""

    def __init__(self, path: str, doc, sizes: list, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.path = os.path.abspath(path)
        self.doc = doc
        self.sizes = list(sizes)
        self.render_manager = RenderManager(self)
        # 渲染管理器必须知道「渲染哪份文档」——不知道的话 request() 会静默丢弃，
        # 表现就是切回标签页后画布一片空白（缩略图是直接读 doc 渲染的，所以还在）。
        self.render_manager.set_document(doc)
        self.store = AnnotationStore(self)
        self.undo_stack = QUndoStack(self)
        self.sequence = SequenceManager(self.store, self)
        self.boards = BoardStore(self)
        self.project = ProjectManager(self.store, self.sequence, self)
        self.project.bind_boards(self.boards)
        #: 项目是否已经 attach 过（第一次切到这个标签时才去读 .qpdf）
        self.attached = False
        #: 「从 .qpdf 打开」时指定要挂的项目文件；普通打开 PDF 为空
        self.forced_project_path = ""
        #: 每个标签各自的浏览状态：切回来还是原来那一页、那个倍率
        self.state: dict[str, Any] = {
            "page": 0,
            "zoom": 0.0,
            "hscroll": 0,
            "vscroll": 0,
            "left": 0,
            "workbench": False,
            "mode": None,
        }

    # ------------------------------------------------------------------ 查询

    @property
    def title(self) -> str:
        return os.path.basename(self.path) or self.path

    @property
    def dirty(self) -> bool:
        # 这里**不能**再加 `project.is_open and`：项目文件建不出来时（目录不可写、
        # 磁盘满），标注已经画在界面上了，关标签/退出必须照样问一句，
        # 否则那批改动会一声不响地消失。
        return bool(self.project.is_dirty)

    def page_sizes(self) -> list[tuple[float, float]]:
        return [(float(size.width), float(size.height)) for size in self.sizes]

    def tooltip(self) -> str:
        text = self.path
        if self.project.is_open and self.project.path:
            text += f"\n项目：{self.project.path}"
        return text

    # ------------------------------------------------------------------ 生命周期

    def release_cache(self) -> None:
        """不是当前标签了：把高清瓦片放掉，但**留住整页预览图**。

        预览一张一两兆，切回来能立刻看见画面（不至于白屏等渲染）；
        瓦片才是几十兆的大头，切走就还给别的标签。
        """
        try:
            self.render_manager.clear_focus_pages()
            self.render_manager.trim_tiles(32)
        except Exception:  # noqa: BLE001 - 释放缓存失败不值得让切换崩掉
            pass

    def ensure_render_doc(self) -> None:
        """兜底：万一渲染管理器丢了文档句柄，切回来时补上。"""
        try:
            if self.render_manager.document is None and self.doc is not None:
                self.render_manager.set_document(self.doc)
        except Exception:  # noqa: BLE001
            pass

    def dispose(self) -> None:
        """关标签：先保存项目（未保存的由调用方问过），再关文档。

        顺序很重要：必须**先**把渲染队列停掉，再关文档。反过来的话，渲染线程
        正握着 ``PdfDocument._lock`` 出图，``doc.close()`` 会一直等这把锁，
        主线程就卡在"点了叉没反应"上（1.2GB 的图，一页渲染几百毫秒很常见）。
        """
        try:
            self.render_manager.shutdown()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.project.close()
        except Exception:  # noqa: BLE001
            pass
        # close() 现在最长只等 CLOSE_LOCK_TIMEOUT：万一队列里那个任务还没结束
        # （waitForDone 超时），它会留个标记，由渲染线程出完图后自己关掉。
        try:
            self.doc.close()
        except Exception:  # noqa: BLE001
            pass
        self.doc = None
        # 断开与主窗口的父子关系：会话是挂在主窗口下面的 QObject，不摘下来的话
        # 每关一个标签就留一整套（标注库、撤销栈、渲染线程池）挂在窗口上，
        # 标签开开关关内存只涨不落。
        try:
            self.setParent(None)
        except Exception:  # noqa: BLE001 - 摘父对象失败不影响"已关闭"这个事实
            pass
