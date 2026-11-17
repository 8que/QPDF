"""读 PDF 自带的目录（outline / 书签）。

图纸会带一层现成的目录——设计院出图时按"装置—管线—图号"建好的。它比 OCR 认出来的
东西可靠得多，所以这里只负责"读出来、理成树"，不猜、不补。

两条约定：
* 页码一律转成 **0 起**；PDF 里 ``-1`` 表示"没有目标页"，这里也保留成 ``-1``；
* 层级保留原样（``get_toc`` 给的 level 从 1 开始），树里每层缩进就是缩进，不改写。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator

import pymupdf


@dataclass(frozen=True)
class TocNode:
    """一层目录项。``page`` 是 0 起页号，``-1`` 表示这一项没有目标页。"""

    title: str
    page: int = -1
    level: int = 1
    children: tuple["TocNode", ...] = ()

    def walk(self) -> Iterator["TocNode"]:
        yield self
        for child in self.children:
            yield from child.walk()

    def page_count(self) -> int:
        return sum(1 for _ in self.walk())


@dataclass(frozen=True)
class TocRange:
    """一条目录项管着的那几页（0 起，闭区间）。"""

    title: str
    start: int
    end: int
    level: int = 1

    @property
    def count(self) -> int:
        return max(0, self.end - self.start + 1)


def read_toc(doc) -> tuple[TocNode, ...]:
    """读目录。``doc`` 可以是 ``pymupdf.Document``，也可以直接是 ``get_toc`` 的结果。"""
    try:
        raw = doc.get_toc(simple=True) if hasattr(doc, "get_toc") else doc
    except Exception:  # noqa: BLE001 - 老 PDF 的目录偶尔会读崩，不值得让整个文档打不开
        return ()
    if not raw:
        return ()
    roots: list[TocNode] = []
    stack: list[tuple[int, list[TocNode]]] = [(0, roots)]

    def _attach(level: int, node: TocNode) -> None:
        while stack and stack[-1][0] >= level:
            stack.pop()
        if not stack:
            stack.append((0, roots))
        container = stack[-1][1]
        container.append(node)
        stack.append((level, list(node.children)))
        # 上面那行只是占位，真正的子列表在建树时一次性替换（见下）

    # 用"先定层级、后补 children"的两步走，比边走边改 frozen dataclass 干净
    flat: list[tuple[int, str, int]] = []
    for entry in raw:
        try:
            level = int(entry[0])
            title = str(entry[1] or "").strip()
            page = int(entry[2]) - 1 if len(entry) > 2 else -1
        except (TypeError, ValueError, IndexError):
            continue
        if not title:
            continue
        flat.append((max(1, level), title, page))

    def _build(index: int, level: int) -> tuple[list[TocNode], int]:
        """从 ``index`` 开始，收掉所有 level 更深的项，返回 (本层节点, 下一个下标)。"""
        nodes: list[TocNode] = []
        while index < len(flat):
            cur_level, title, page = flat[index]
            if cur_level < level:
                break
            if cur_level > level:  # 上级缺失（PDF 目录层级跳了一档），按本层处理
                cur_level = level
            children, index = _build(index + 1, level + 1)
            nodes.append(TocNode(title=title, page=page, level=cur_level, children=tuple(children)))
        return nodes, index

    built, _ = _build(0, flat[0][0])
    return tuple(built)


def plan_ranges(nodes: tuple[TocNode, ...], page_count: int) -> list[TocRange]:
    """算出每条目录项"管到哪一页"。

    规则：一项管到「下一个同级或更高级目录项的前一页」，没有下一项就管到最后一页；
    比它自己页号还小的不算。这样勾「第 3 章」就把整章一起勾上，勾小节就只取小节。

    返回的顺序和 ``_walk_level``（先序遍历）一一对应，**每条目录项都有一条**；
    没有有效页号（``-1`` 或越界）的那条给 ``start=end=-1``，这样调用方按下标对得上。
    """
    entries = [(node, depth) for node, depth in _walk_level(nodes)]
    ranges: list[TocRange] = []
    total = max(0, int(page_count))
    for position, (node, depth) in enumerate(entries):
        start = node.page
        if start < 0 or start >= total:
            ranges.append(TocRange(title=node.title, start=-1, end=-1, level=node.level))
            continue
        end = total - 1
        for later, later_depth in entries[position + 1:]:
            if later_depth <= depth:
                if 0 <= later.page <= total:
                    end = max(start, later.page - 1)
                break
        ranges.append(TocRange(title=node.title, start=start, end=max(start, end), level=node.level))
    return ranges


def _walk_level(nodes: tuple[TocNode, ...], depth: int = 0):
    for node in nodes:
        yield node, depth
        yield from _walk_level(node.children, depth + 1)


def flatten(nodes: tuple[TocNode, ...]) -> list[TocNode]:
    out: list[TocNode] = []
    for node in nodes:
        out.append(node)
        out.extend(flatten(node.children))
    return out


def search(nodes: tuple[TocNode, ...], keyword: str) -> list[TocNode]:
    """按标题包含关系搜（大小写不敏感）；空关键字返回空。"""
    text = (keyword or "").strip().lower()
    if not text:
        return []
    return [node for node in flatten(nodes) if text in node.title.lower()]


def outline_rows(doc) -> list[dict]:
    """把 PDF 自带目录转成「识别结果」那种行，给按书签做匹配标注用。

    ``doc`` 可以是路径、``pymupdf.Document``，或者 ``get_toc`` 的结果。
    返回的每项是 ``{page, raw, tag, source, error}``：``tag`` 直接用标题，
    查表时 ``RecordTable.lookup`` 会自己去做 normalize；没有页号的条目跳过。
    """
    nodes: tuple[TocNode, ...] = ()
    if isinstance(doc, str):
        # 传进来的是路径：自己开一份读，读完就关（和 MatchWorker 一个路子）
        try:
            document = pymupdf.open(doc)
        except Exception:  # noqa: BLE001 - 打不开就当没有目录
            document = None
        if document is not None:
            try:
                nodes = read_toc(document)
            finally:
                document.close()
    else:
        nodes = read_toc(doc)

    rows: list[dict] = []
    for node in flatten(nodes):
        title = (node.title or "").strip()
        if node.page < 0 or not title:
            continue
        rows.append(
            {
                "page": int(node.page),
                "raw": title,
                "tag": title,
                "source": "bookmark",
                "error": "",
            }
        )
    return rows


def merge_bookmarks(toc: tuple[TocNode, ...], marks: list, page_count: int) -> list:
    """把识别出来的书签并进 PDF 原有目录。

    ``marks`` 是 ``Bookmark`` 列表。并进去的放在第一层末尾，标题前加「OCR · 」，
    不去改原有条目——原目录是设计院定的，我们只是往上加一层索引。
    """
    out = list(toc)
    taken = {
        (node.title.removeprefix("OCR · "), node.page) for node in flatten(toc)
    }
    extras = []
    for mark in marks:
        page = int(getattr(mark, "page", -1))
        title = str(getattr(mark, "title", "")).strip()
        if not title or page < 0 or page >= page_count:
            continue
        if (title, page) in taken:
            continue
        extras.append((title, page))
    if not extras:
        return out
    extras.sort(key=lambda item: (item[1], item[0]))
    for title, page in extras:
        out.append(TocNode(title=f"OCR · {title}", page=page, level=1, children=()))
    # 顶层按页号排一下：识别出来的页可能散在已有目录中间
    out.sort(key=lambda node: (node.page, node.title))
    return out
