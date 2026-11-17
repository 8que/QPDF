"""工作台：把一份 PDF 里相关的若干页挑出来，摆在一块画布上一起看。

为什么单独做一层：
- 页面在 PDF 里的先后顺序（页码）和"我在看哪几张图"是两件事。走向、管线号经常跨页，
  但 PDF 的页序是出图顺序，不是看图顺序。工作台只记录"哪几页、摆在哪"，**从不改 PDF**。
- 一块工作台装不下的时候，可以建多块（比如"1 号楼""水泵房"各一块），互不干扰。

卡片坐标用工作台自己的场景坐标系（单位 px，不是 PDF 的 pt），因为它是界面布局，
和图纸内容无关；卡片内部再按 ``scale`` 把 PDF 坐标缩到卡片上。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace

from PySide6.QtCore import QObject, Signal

#: 卡片默认缩放：0.2 表示 1 PDF pt 显示成 0.2 px。
#: 所有卡片共用同一个 scale，这样 A1 比 A3 大一圈，图幅的相对大小还在。
DEFAULT_CARD_SCALE = 0.2

#: 自动排列时的间距（px）
LAYOUT_GAP = 48.0


@dataclass(frozen=True)
class BoardCard:
    """工作台上的一张卡片：第几页 + 摆在哪 + 多大 + 盖在谁上面。"""

    page: int
    x: float = 0.0
    y: float = 0.0
    scale: float = DEFAULT_CARD_SCALE
    #: 叠放次序，大的盖在上面。页码本身不参与排序——这是"看图顺序"，不是 PDF 页序。
    z: int = 0

    def moved_to(self, x: float, y: float) -> BoardCard:
        return replace(self, x=float(x), y=float(y))

    def with_z(self, z: int) -> BoardCard:
        return replace(self, z=int(z))

    def to_dict(self) -> dict:
        return {"page": self.page, "x": self.x, "y": self.y, "scale": self.scale, "z": self.z}

    @classmethod
    def from_dict(cls, payload: dict) -> BoardCard:
        return cls(
            page=int(payload.get("page", 0)),
            x=float(payload.get("x", 0.0)),
            y=float(payload.get("y", 0.0)),
            scale=float(payload.get("scale", DEFAULT_CARD_SCALE)) or DEFAULT_CARD_SCALE,
            z=int(payload.get("z", 0)),
        )


def _now() -> float:
    return time.time()


@dataclass
class Board:
    """一块工作台。

    ``note`` 是给人看的自由备注（"这是 1 号楼地下室"之类），
    ``created_at`` / ``updated_at`` 只用于面板上显示"什么时候建的、最后一次改动"。
    """

    id: str
    name: str
    cards: list[BoardCard] = field(default_factory=list)
    note: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0

    def touch(self) -> None:
        """记一次改动时间。所有会改变内容的操作都应该调它。"""
        self.updated_at = _now()
        if not self.created_at:
            self.created_at = self.updated_at

    def card(self, page: int) -> BoardCard | None:
        for item in self.cards:
            if item.page == page:
                return item
        return None

    def has(self, page: int) -> bool:
        return self.card(page) is not None

    def ordered(self) -> list[BoardCard]:
        """按叠放次序排好的卡片（先画的在前，后画的盖上面）。"""
        return sorted(self.cards, key=lambda card: card.z)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "cards": [card.to_dict() for card in self.cards],
            "note": self.note,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> Board:
        cards = [BoardCard.from_dict(item) for item in payload.get("cards", [])]
        return cls(
            id=str(payload.get("id", "b1")),
            name=str(payload.get("name", "工作台 1")),
            cards=cards,
            note=str(payload.get("note", "") or ""),
            created_at=float(payload.get("created_at", 0.0) or 0.0),
            updated_at=float(payload.get("updated_at", 0.0) or 0.0),
        )


class BoardStore(QObject):
    """所有工作台 + 当前用哪一块。变更通过 ``changed`` 通知界面，由主窗口转成"项目变脏"。"""

    changed = Signal()          # 卡片 / 名称 / 增删 有任何变化
    current_changed = Signal(str)  # 当前工作台 id
    boards_changed = Signal()   # 工作台本身的增删改名

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._boards: list[Board] = []
        self._current = ""
        self._serial = 0
        self.reset()

    # -- 查询 -------------------------------------------------------------

    def boards(self) -> list[Board]:
        return list(self._boards)

    def board_ids(self) -> list[str]:
        return [board.id for board in self._boards]

    def board(self, board_id: str | None = None) -> Board | None:
        target = board_id or self._current
        for item in self._boards:
            if item.id == target:
                return item
        return None

    def current(self) -> Board:
        board = self.board()
        if board is None:  # 兜底：至少有一块
            board = self.add_board()
        return board

    def current_id(self) -> str:
        return self.current().id

    def count(self) -> int:
        return len(self._boards)

    # -- 工作台增删改 -----------------------------------------------------

    def add_board(self, name: str = "") -> Board:
        self._serial += 1
        board = Board(
            id=f"b{self._serial}",
            name=name or f"工作台 {self._serial}",
        )
        board.touch()
        self._boards.append(board)
        if not self._current:
            self._current = board.id
        self.boards_changed.emit()
        self.changed.emit()
        return board

    def rename_board(self, name: str, board_id: str | None = None) -> bool:
        board = self.board(board_id)
        name = name.strip()
        if board is None or not name or board.name == name:
            return False
        board.name = name
        board.touch()
        self.boards_changed.emit()
        self.changed.emit()
        return True

    def remove_board(self, board_id: str | None = None) -> bool:
        board = self.board(board_id)
        if board is None or len(self._boards) <= 1:
            return False
        self._boards.remove(board)
        if self._current == board.id:
            self._current = self._boards[0].id
            self.current_changed.emit(self._current)
        self.boards_changed.emit()
        self.changed.emit()
        return True

    def set_note(self, note: str, board_id: str | None = None) -> bool:
        """给工作台写一条备注。内容没变就不记改动。"""
        board = self.board(board_id)
        note = str(note or "").strip()
        if board is None or board.note == note:
            return False
        board.note = note
        board.touch()
        self.changed.emit()
        return True

    def duplicate_board(self, board_id: str | None = None, name: str = "") -> Board | None:
        """复制一块工作台（页、摆位、叠放次序全带走），直接切到新的那块。

        复制出来的卡片保持相对位置，但 z 重新从 0 编号——两块各自独立叠放。
        """
        source = self.board(board_id)
        if source is None:
            return None
        self._serial += 1
        copy = Board(
            id=f"b{self._serial}",
            name=(name or f"{source.name} 副本").strip(),
            note=source.note,
        )
        for card in source.ordered():
            copy.cards.append(BoardCard(page=card.page, x=card.x, y=card.y, scale=card.scale,
                                        z=len(copy.cards)))
        copy.touch()
        self._boards.insert(self._boards.index(source) + 1, copy)
        self._current = copy.id
        self.boards_changed.emit()
        self.current_changed.emit(copy.id)
        self.changed.emit()
        return copy

    def move_board(self, board_id: str, delta: int) -> bool:
        """在工作台列表里上移/下移（管理用，不影响内容）。"""
        board = self.board(board_id)
        if board is None or delta == 0:
            return False
        index = self._boards.index(board)
        target = index + int(delta)
        if target < 0 or target >= len(self._boards):
            return False
        self._boards.insert(target, self._boards.pop(index))
        self.boards_changed.emit()
        return True

    def set_current(self, board_id: str) -> bool:
        if board_id == self._current or board_id not in self.board_ids():
            return False
        self._current = board_id
        self.current_changed.emit(board_id)
        return True

    # -- 卡片 -------------------------------------------------------------

    def add_page(self, page: int, x: float | None = None, y: float | None = None,
                 scale: float = DEFAULT_CARD_SCALE, board_id: str | None = None) -> bool:
        board = self.board(board_id) or self.current()
        if board.has(page):
            return False
        if x is None or y is None:
            x, y = self._next_slot(board)
        board.cards.append(BoardCard(page=int(page), x=float(x), y=float(y), scale=scale,
                                     z=self._next_z(board)))
        board.touch()
        self.changed.emit()
        return True

    def add_pages(self, pages: list[int], board_id: str | None = None) -> int:
        board = self.board(board_id) or self.current()
        added = 0
        for page in pages:
            if board.has(page):
                continue
            x, y = self._next_slot(board)
            board.cards.append(BoardCard(page=int(page), x=x, y=y, z=self._next_z(board)))
            added += 1
        if added:
            board.touch()
            self.changed.emit()
        return added

    def remove_page(self, page: int, board_id: str | None = None) -> bool:
        board = self.board(board_id)
        if board is None:
            return False
        before = len(board.cards)
        board.cards = [card for card in board.cards if card.page != page]
        if len(board.cards) == before:
            return False
        board.touch()
        self.changed.emit()
        return True

    def move_card(self, page: int, x: float, y: float, board_id: str | None = None) -> None:
        board = self.board(board_id)
        if board is None:
            return
        for index, card in enumerate(board.cards):
            if card.page == page:
                if abs(card.x - x) < 0.01 and abs(card.y - y) < 0.01:
                    return
                board.cards[index] = card.moved_to(x, y)
                board.touch()
                self.changed.emit()
                return

    def set_positions(self, positions: dict[int, tuple[float, float]],
                      board_id: str | None = None) -> None:
        board = self.board(board_id)
        if board is None:
            return
        for index, card in enumerate(board.cards):
            point = positions.get(card.page)
            if point is not None:
                board.cards[index] = card.moved_to(point[0], point[1])
        board.touch()
        self.changed.emit()

    def clear_cards(self, board_id: str | None = None) -> bool:
        board = self.board(board_id)
        if board is None or not board.cards:
            return False
        board.cards = []
        board.touch()
        self.changed.emit()
        return True

    # -- 叠放次序 ---------------------------------------------------------

    def _next_z(self, board: Board) -> int:
        return max((card.z for card in board.cards), default=0) + 1

    def _set_z(self, board: Board, page: int, z: int) -> None:
        for index, card in enumerate(board.cards):
            if card.page == page:
                board.cards[index] = card.with_z(z)
                board.touch()
                self.changed.emit()
                return

    def bring_to_front(self, page: int, board_id: str | None = None) -> bool:
        board = self.board(board_id)
        if board is None or not board.has(page):
            return False
        self._set_z(board, page, self._next_z(board))
        return True

    def send_to_back(self, page: int, board_id: str | None = None) -> bool:
        board = self.board(board_id)
        if board is None or not board.has(page):
            return False
        self._set_z(board, page, min((card.z for card in board.cards), default=0) - 1)
        return True

    def raise_card(self, page: int, board_id: str | None = None) -> bool:
        """往上盖一层：和上面第一张换次序。"""
        board = self.board(board_id)
        if board is None:
            return False
        ordered = board.ordered()
        for index, card in enumerate(ordered):
            if card.page != page:
                continue
            if index == len(ordered) - 1:
                return False
            upper = ordered[index + 1]
            self._set_z(board, card.page, upper.z)
            self._set_z(board, upper.page, card.z)
            return True
        return False

    def lower_card(self, page: int, board_id: str | None = None) -> bool:
        """往下压一层：和下面第一张换次序。"""
        board = self.board(board_id)
        if board is None:
            return False
        ordered = board.ordered()
        for index, card in enumerate(ordered):
            if card.page != page:
                continue
            if index == 0:
                return False
            lower = ordered[index - 1]
            self._set_z(board, card.page, lower.z)
            self._set_z(board, lower.page, card.z)
            return True
        return False

    def _next_slot(self, board: Board) -> tuple[float, float]:
        """还没排版的新卡片丢在现有卡片右边一点，免得叠在一起。"""
        if not board.cards:
            return 0.0, 0.0
        right = max(card.x for card in board.cards)
        top = min(card.y for card in board.cards)
        return right + 320.0, top

    # -- 存取 -------------------------------------------------------------

    def reset(self) -> None:
        self._boards = []
        self._serial = 0
        self._current = ""
        self.add_board()

    def to_dict(self) -> dict:
        return {
            "current": self._current,
            "serial": self._serial,
            "boards": [board.to_dict() for board in self._boards],
        }

    def load_dict(self, payload: dict | None) -> None:
        if not isinstance(payload, dict):
            self.reset()
            return
        boards = [Board.from_dict(item) for item in payload.get("boards", []) if item]
        self._boards = boards or []
        self._serial = int(payload.get("serial", 0)) or len(self._boards)
        if not self._boards:
            self._serial = 0
            self._boards = [Board(id="b1", name="工作台 1")]
            self._serial = 1
        current = str(payload.get("current", ""))
        self._current = current if current in self.board_ids() else self._boards[0].id
        self.boards_changed.emit()
        self.current_changed.emit(self._current)
        self.changed.emit()


def parse_page_spec(text: str, page_count: int) -> list[int]:
    """把 ``"4,7,9-15,22"`` 解析成页码列表（0 起，去重升序）。解析不了的部分直接跳过。"""
    result: set[int] = set()
    for chunk in str(text).replace("，", ",").replace("、", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk or "－" in chunk or "~" in chunk:
            raw = chunk.replace("－", "-").replace("~", "-")
            first, _, last = raw.partition("-")
            try:
                start, end = int(first.strip()), int(last.strip())
            except ValueError:
                continue
            if start > end:
                start, end = end, start
            for page in range(start, end + 1):
                if 1 <= page <= page_count:
                    result.add(page - 1)
            continue
        try:
            page = int(chunk)
        except ValueError:
            continue
        if 1 <= page <= page_count:
            result.add(page - 1)
    return sorted(result)
