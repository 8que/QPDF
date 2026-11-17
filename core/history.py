"""撤销 / 重做（第 10 章）。

用 QUndoStack + 命令模式：

- ``AddAnnotationCommand``      新建
- ``RemoveAnnotationsCommand``  删除（批量删除就是一次命令，保证整体撤销）
- ``ModifyAnnotationCommand``   改内容 / 改样式 / 拖位置（存新旧两份快照）
- ``BatchCommand``              Phase 4：插队——一次命令里改一整批序号

插队用 ``BatchCommand``，"撤销一次只恢复一部分编号"这个坑在设计上直接不存在：
一次插队 = 一条命令 = 一次 undo（第 10 章、第 6.2.3 节）。

``BatchCommand`` 还负责 6.2.3 的事务语义：执行途中出任何异常，已经改过的那部分
会被就地回滚，绝不会留下"一半数字更新了、一半没更新"的状态。
"""

from __future__ import annotations

from PySide6.QtGui import QUndoCommand

from core.annotation import Annotation, AnnotationStore


class AddAnnotationCommand(QUndoCommand):
    def __init__(self, store: AnnotationStore, ann: Annotation, label: str = "添加标注") -> None:
        super().__init__(label)
        self._store = store
        self._ann = ann

    def redo(self) -> None:  # noqa: D102 - Qt 接口
        self._store.add(self._ann)

    def undo(self) -> None:  # noqa: D102
        self._store.remove(self._ann.id)


class RemoveAnnotationsCommand(QUndoCommand):
    def __init__(self, store: AnnotationStore, anns: list[Annotation], label: str | None = None) -> None:
        super().__init__(label or f"删除 {len(anns)} 个标注")
        self._store = store
        self._anns = list(anns)

    def redo(self) -> None:  # noqa: D102
        for ann in self._anns:
            self._store.remove(ann.id)

    def undo(self) -> None:  # noqa: D102
        for ann in self._anns:
            self._store.add(ann)


class ModifyAnnotationCommand(QUndoCommand):
    def __init__(
        self,
        store: AnnotationStore,
        old: Annotation,
        new: Annotation,
        label: str = "修改标注",
    ) -> None:
        super().__init__(label)
        self._store = store
        self._old = old
        self._new = new

    def redo(self) -> None:  # noqa: D102
        self._store.replace(self._new)

    def undo(self) -> None:  # noqa: D102
        self._store.replace(self._old)


#: 一条批量操作的形状：``(旧标注 or None, 新标注 or None)``
#:
#: - ``(None, ann)``  新增
#: - ``(ann, None)``  删除
#: - ``(old, new)``   修改
Op = tuple[Annotation | None, Annotation | None]


class BatchCommand(QUndoCommand):
    """把一组成对的增/删/改打包成**一条**撤销命令（插队专用）。

    第 6.2.3 节要求"一次插队视为一个完整事务：不允许出现部分数字已经更新、
    部分数字尚未更新的状态"。所以 ``_apply`` 边做边记账，任何一步抛异常就
    把已经做的那几步倒着撤回，然后把异常继续抛出去——要么全改，要么一条都不改。
    """

    def __init__(self, store: AnnotationStore, ops: list[Op], label: str = "批量修改") -> None:
        super().__init__(label)
        self._store = store
        self._ops = list(ops)

    @property
    def op_count(self) -> int:
        return len(self._ops)

    @classmethod
    def insert_number(
        cls,
        store: AnnotationStore,
        ann: Annotation,
        shifts: list[Op],
        label: str = "插队标注",
    ) -> "BatchCommand":
        """插队的标准形状：先把后面的号挪一格，再插入新标注。"""
        return cls(store, list(shifts) + [(None, ann)], label)

    def _apply(self, ops: list[Op]) -> None:
        done: list[Op] = []
        try:
            for old, new in ops:
                if old is None:
                    self._store.add(new)  # type: ignore[arg-type]
                elif new is None:
                    self._store.remove(old.id)
                else:
                    self._store.replace(new)
                done.append((old, new))
        except Exception:
            for old, new in reversed(done):
                if old is None:
                    self._store.remove(new.id)  # type: ignore[union-attr]
                elif new is None:
                    self._store.add(old)
                else:
                    self._store.replace(old)
            raise

    def redo(self) -> None:  # noqa: D102
        self._apply(self._ops)

    def undo(self) -> None:  # noqa: D102
        self._apply([(new, old) for old, new in reversed(self._ops)])
