"""编号序列（第 5 章的核心，第 6 章插队的地基）。

为什么数字标注不存显示文本
-----------------------
第 6.2 节要求插队之后"原来的后续数字自动递增"。如果数字是写死的文本，
一次插队就要改 N 条记录，还得保证这 N 条要么全改、要么全不改（6.2.3 事务安全）。

所以一条数字标注只存两件事：属于哪个序列（``seq_group``）、组内第几号
（``seq_index``）。屏幕上显示的数字 = ``起点 + 步长 × 序号``，用时现算：

    插队  =>  序号 ≥ k 的加一
    撤销  =>  再加回来

一条 UPDATE 就是一个事务，天然满足 6.2.3 的原子性要求。

"重新从 1 开始"（第 5.4 节）怎么实现
--------------------------------
不删除、不重排已有数字，而是**新开一个序列**（``s1`` → ``s2``），新序列
从起始值开始。已有数字保持原样，"回到 1 重来"因此是一个零风险的轻操作，
也不会把撤销链条搞乱。

下一个序号从 store 现算，不额外维护计数器——这样撤销掉最后一个数字之后，
下一个数字自然还是它，不需要任何补偿逻辑。
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from PySide6.QtCore import QObject, Signal

from core.annotation import KIND_NUMBER, Annotation, AnnotationStyle

DEFAULT_GROUP = "s1"

#: 第 6.2.2 节的三种影响范围
SCOPE_PAGE = "page"
SCOPE_RANGE = "range"
SCOPE_ALL = "all"


def _group_sort_key(group: str) -> tuple[int, str]:
    digits = group[1:] if group[:1] == "s" else ""
    return (int(digits) if digits.isdigit() else 0, group)


@dataclass(frozen=True)
class NumberSequence:
    """一组编号规则：从 ``start`` 开始，每次加 ``step``。"""

    group: str = DEFAULT_GROUP
    start: int = 1
    step: int = 1
    label: str = "第 1 组"

    def value_at(self, index: int) -> int:
        return self.start + self.step * int(index)

    def text_at(self, index: int) -> str:
        return str(self.value_at(index))

    def to_dict(self) -> dict:
        return {
            "group": self.group,
            "start": self.start,
            "step": self.step,
            "label": self.label,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "NumberSequence":
        return cls(**data)


class SequenceManager(QObject):
    """管理所有编号序列，并把「第几号」翻译成「显示什么数字」。"""

    #: 当前组、起始值或步长发生变化（状态栏跟着刷新）
    changed = Signal()

    def __init__(self, store, parent=None) -> None:
        super().__init__(parent)
        self._store = store
        self._sequences: dict[str, NumberSequence] = {DEFAULT_GROUP: NumberSequence()}
        self._current_group = DEFAULT_GROUP
        self._serial = 1

    # -- 查询 -------------------------------------------------------------

    @property
    def current_group(self) -> str:
        return self._current_group

    @property
    def current(self) -> NumberSequence:
        return self._sequences[self._current_group]

    def sequence(self, group: str | None = None) -> NumberSequence:
        return self._sequences.get(group or self._current_group, NumberSequence())

    def groups(self) -> list[NumberSequence]:
        return [self._sequences[g] for g in sorted(self._sequences, key=_group_sort_key)]

    def group_count(self) -> int:
        return len(self._sequences)

    def set_current(self, group: str) -> bool:
        if group not in self._sequences or group == self._current_group:
            return False
        self._current_group = group
        self.changed.emit()
        return True

    def index_of(self, ann: Annotation) -> int | None:
        return ann.seq_index if ann.is_number else None

    def text_for(self, ann: Annotation) -> str:
        """给 ``AnnotationItem`` 用的文本解析器：数字标注的显示值。"""
        if not ann.is_number:
            return ann.content
        if ann.seq_index is None:
            return ann.content
        seq = self._sequences.get(ann.seq_group or DEFAULT_GROUP)
        if seq is None:
            return ann.content
        return seq.text_at(ann.seq_index)

    # -- 序号计算 ---------------------------------------------------------

    def next_index(self, group: str | None = None) -> int:
        """组内下一个可用序号。

        从 store 现算而不是维护计数器：撤销/删除之后序号会自动回退，
        不会出现"删了 3 号，下次还标 5 号"这种跳号。
        """
        group = group or self._current_group
        highest = -1
        for ann in self._store.all():
            if ann.is_number and ann.seq_group == group and ann.seq_index is not None:
                if ann.seq_index > highest:
                    highest = ann.seq_index
        return highest + 1

    def next_value(self, group: str | None = None) -> int:
        seq = self.sequence(group)
        return seq.value_at(self.next_index(seq.group))

    def next_text(self, group: str | None = None) -> str:
        return str(self.next_value(group))

    def count(self, group: str | None = None) -> int:
        group = group or self._current_group
        return sum(
            1 for ann in self._store.all() if ann.is_number and ann.seq_group == group
        )

    # -- 生成标注 ---------------------------------------------------------

    def make_number(
        self,
        page: int,
        x: float,
        y: float,
        style: AnnotationStyle | None = None,
    ) -> Annotation:
        """在当前组里新建一条数字标注（不落 store，交给撤销命令去落）。

        ``content`` 只是创建时算出来的快照，方便导出兜底和人工排查；
        **界面显示永远走 ``text_for`` 现算**，所以插队改了序号，显示立刻跟着变。
        """
        seq = self.current
        index = self.next_index(seq.group)
        return Annotation(
            kind=KIND_NUMBER,
            page=int(page),
            x=float(x),
            y=float(y),
            content=seq.text_at(index),
            style=style or AnnotationStyle(),
            seq_group=seq.group,
            seq_index=index,
        )

    def make_number_at(
        self,
        page: int,
        x: float,
        y: float,
        index: int,
        style: AnnotationStyle | None = None,
        group: str | None = None,
    ) -> Annotation:
        """在**指定序号**处新建数字（插队用：新数字占据被插位置的那个序号）。"""
        seq = self.sequence(group)
        index = max(0, int(index))
        return Annotation(
            kind=KIND_NUMBER,
            page=int(page),
            x=float(x),
            y=float(y),
            content=seq.text_at(index),
            style=style or AnnotationStyle(),
            seq_group=seq.group,
            seq_index=index,
        )

    # -- 插队（第 6.2 节）-------------------------------------------------

    def shifted(self, ann: Annotation, delta: int = 1) -> Annotation:
        """把一个数字标注挪 ``delta`` 个序号（显示值随之变化，位置不动）。"""
        if not ann.is_number or ann.seq_index is None:
            return ann
        return ann.with_sequence(ann.seq_group or DEFAULT_GROUP, ann.seq_index + int(delta))

    def in_scope(
        self,
        ann: Annotation,
        scope: str = SCOPE_PAGE,
        page: int = 0,
        page_from: int = 0,
        page_to: int = 0,
    ) -> bool:
        """这条标注是否落在本次插队的影响范围内（第 6.2.2 节）。"""
        if scope == SCOPE_ALL:
            return True
        if scope == SCOPE_RANGE:
            low, high = sorted((int(page_from), int(page_to)))
            return low <= ann.page <= high
        return ann.page == int(page)

    def plan_insert(
        self,
        at_index: int,
        scope: str = SCOPE_PAGE,
        page: int = 0,
        page_from: int = 0,
        page_to: int = 0,
        group: str | None = None,
        delta: int = 1,
    ) -> list[tuple[Annotation, Annotation]]:
        """算出一次插队要挪哪些数字，返回 ``[(旧, 新), ...]``。

        只**计算**不落库——这样对话框可以拿它做"操作预览"，
        真正执行时再把同一份清单交给 ``BatchCommand`` 一次落库（6.2.3 事务）。
        """
        group = group or self._current_group
        at_index = max(0, int(at_index))
        pairs: list[tuple[Annotation, Annotation]] = []
        for ann in self._store.all():
            if not ann.is_number or ann.seq_group != group:
                continue
            if ann.seq_index is None or ann.seq_index < at_index:
                continue
            if not self.in_scope(ann, scope, page, page_from, page_to):
                continue
            pairs.append((ann, self.shifted(ann, delta)))
        pairs.sort(key=lambda pair: pair[0].seq_index or 0)
        return pairs

    # -- 序列调整 ---------------------------------------------------------

    def configure(self, start: int, step: int) -> NumberSequence:
        """设置起始值与步长。

        当前组还没标过数字就直接生效；已经标过就新开一组——因为"改起始值"
        对已经存在的数字意味着重新编号，那是第 6 章插队/覆盖要谨慎处理的事，
        不该在设置对话框里悄悄发生。
        """
        start = int(start)
        step = max(1, int(step))
        if self.next_index(self._current_group) == 0:
            seq = replace(self.current, start=start, step=step)
            self._sequences[self._current_group] = seq
            self.changed.emit()
            return seq
        return self.new_sequence(start, step)

    def restart(self, start: int = 1, step: int | None = None) -> NumberSequence:
        """回到 1 重新开始（第 5.4 节）：新开一组，已有数字一个都不动。"""
        return self.new_sequence(start, self.current.step if step is None else step)

    def new_sequence(self, start: int = 1, step: int = 1, label: str = "") -> NumberSequence:
        self._serial += 1
        group = f"s{self._serial}"
        seq = NumberSequence(
            group=group,
            start=int(start),
            step=max(1, int(step)),
            label=label or f"第 {self._serial} 组",
        )
        self._sequences[group] = seq
        self._current_group = group
        self.changed.emit()
        return seq

    # -- 序列化（Phase 6）-------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "current": self._current_group,
            "serial": self._serial,
            "sequences": [seq.to_dict() for seq in self.groups()],
        }

    def load_dict(self, data: dict | None) -> None:
        data = data or {}
        sequences = {}
        for item in data.get("sequences", []):
            seq = NumberSequence.from_dict(item)
            sequences[seq.group] = seq
        if DEFAULT_GROUP not in sequences:
            sequences[DEFAULT_GROUP] = NumberSequence()
        self._sequences = sequences
        serial = int(data.get("serial") or 0)
        self._serial = max(serial, len(sequences))
        current = str(data.get("current") or DEFAULT_GROUP)
        self._current_group = current if current in sequences else DEFAULT_GROUP
        self.changed.emit()

    def reset(self) -> None:
        """换文档时清空，回到默认的一组编号。"""
        self._sequences = {DEFAULT_GROUP: NumberSequence()}
        self._current_group = DEFAULT_GROUP
        self._serial = 1
        self.changed.emit()
