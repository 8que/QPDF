"""F01：管线号目录（增量需求第 3 章）。

把「每页一个识别结果」整理成「一个管线号挂多个页面」的目录树。

为什么单独做一层、而不是直接用 PDF 书签：

- 书签是**导出时写进 PDF 的一次性快照**，写完就跟你没关系了；
- 目录要**长期可编辑**：同一个管线号可能出现在 128、893、1024 页，
  页序变了要能重排，识别错了要能改，改了还不能把旧记录覆盖掉。

所以目录是项目里的数据（`.qpdf` 的 `scan_results` 表），导出时才转成书签。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: 归不进任何前缀的条目（页面没识别出东西）挂到这个组下
UNSORTED = "待校对"

_NUM_RE = re.compile(r"(\d+)")


def natural_key(text: str) -> tuple:
    """自然排序键：``PA-2`` 排在 ``PA-10`` 前面，而不是按字符串比。

    返回的元组里数字段是 int，文字段是小写字符串——Python 逐项比较时
    同类型才比得下去，所以每段前面塞一个标记位区分。
    """
    key: list = []
    for chunk in _NUM_RE.split(text or ""):
        if not chunk:
            continue
        if chunk.isdigit():
            key.append((1, int(chunk), ""))
        else:
            key.append((0, 0, chunk.lower()))
    return tuple(key)


def alpha_key(tag: str) -> str:
    """按字母排序用的键：去掉开头表示管子口径的数字段。

    ``100-BU-V110501-N1B-C50`` → ``BU-V110501-N1B-C50``。

    管线号的第一段是公称直径（50/80/100…），差别只在口径时用户想按
    后面真正表示介质/工位的字母来排，否则全是 "50-… 65-… 100-…" 一大串。
    第一段不是纯数字（比如 ``EL+6529``）就整串参与排序。
    """
    clean = (tag or "").strip()
    head, sep, tail = clean.partition("-")
    if sep and head.isdigit() and tail.strip():
        return tail.strip()
    return clean


def tag_prefix(tag: str) -> str:
    """分组用的前缀：取第一个 ``-`` 之前那段。

    ``50-S0-P1302B01-N1B-H60`` → ``50``；
    ``EL+6529``（不是管线号）→ 整串自己当一组。
    """
    clean = (tag or "").strip()
    if not clean:
        return UNSORTED
    head = clean.split("-", 1)[0]
    return head or UNSORTED


def _segments(tag: str) -> list[str]:
    return [part for part in (tag or "").strip().split("-") if part]


def medium_of(tag: str) -> str:
    """按字母排序的分组名：第二段（介质），没有第二段就用整串。

    ``100-CIS-E6102B01-MIB`` → ``CIS``；``PA-1023`` → ``PA``。
    """
    parts = _segments(tag)
    if len(parts) >= 2:
        return parts[1]
    return parts[0] if parts else UNSORTED


def tail_of(tag: str) -> str:
    """按字母排序时组内比较用的键：去掉前两段（口径、介质），剩下原样拼回。

    ``150-CIS-E120301-MIB`` → ``e120301-mib``，跟 ``e6102b01-mib`` 直接
    按字符串比——所以 ``E120301`` 排在 ``E6102B01`` 前面，和用户给的例子一致。
    """
    parts = _segments(tag)
    if len(parts) > 2:
        return "-".join(parts[2:]).lower()
    if len(parts) == 2:
        return parts[1].lower()
    return (tag or "").strip().lower()


@dataclass(frozen=True)
class IndexPage:
    """目录里的一条页面记录。"""

    page: int
    tag: str
    source: str = ""
    confirmed: bool = True
    note: str = ""
    suspect: tuple[str, ...] = ()

    @property
    def page_no(self) -> int:
        """给人看的页码（从 1 起）。"""
        return self.page + 1

    @property
    def needs_check(self) -> bool:
        return bool(self.suspect) or not self.confirmed or not self.tag


@dataclass
class IndexEntry:
    """一个管线号，下面挂若干页面。"""

    tag: str
    pages: list[IndexPage] = field(default_factory=list)

    @property
    def page_numbers(self) -> list[int]:
        return [p.page_no for p in self.pages]

    @property
    def needs_check(self) -> bool:
        return any(p.needs_check for p in self.pages)

    @property
    def summary(self) -> str:
        """树里显示在管线号右边的小字——只报页数，具体页码在子节点上。

        标题列要给 23 个字符的管线号留位置，右边这列越窄越好。
        """
        return f"{len(self.pages)} 页"


class PipeIndex:
    """目录本体：吃 ``{页号: 记录}``，吐出分组好的树。"""

    def __init__(self, rows: dict[int, dict] | None = None) -> None:
        self._pages: list[IndexPage] = []
        self.load(rows or {})

    # -- 装填 ---------------------------------------------------------------
    def load(self, rows: dict[int, dict]) -> None:
        pages: list[IndexPage] = []
        for page, row in (rows or {}).items():
            tag = (row.get("tag") or "").strip()
            suspects = row.get("suspect") or ()
            pages.append(
                IndexPage(
                    page=int(page),
                    tag=tag,
                    source=row.get("source") or "",
                    confirmed=row.get("confirmed", True),
                    note=row.get("note") or "",
                    suspect=tuple(suspects) if not isinstance(suspects, str) else (suspects,),
                )
            )
        pages.sort(key=lambda p: p.page)
        self._pages = pages

    def clear(self) -> None:
        self._pages = []

    # -- 查询 ---------------------------------------------------------------
    @property
    def pages(self) -> list[IndexPage]:
        return list(self._pages)

    @property
    def page_count(self) -> int:
        return len(self._pages)

    @property
    def tag_count(self) -> int:
        return len({p.tag for p in self._pages if p.tag})

    @property
    def pending_pages(self) -> list[IndexPage]:
        """没识别出内容 / 有疑似 / 被取消采用的页，单独拎出来给人看。"""
        return [p for p in self._pages if p.needs_check]

    def entries(self) -> list[IndexEntry]:
        """按管线号合并；没有标签的页面归到 ``UNSORTED`` 一个条目里。"""
        buckets: dict[str, IndexEntry] = {}
        for page in self._pages:
            tag = page.tag or UNSORTED
            entry = buckets.get(tag)
            if entry is None:
                entry = IndexEntry(tag=tag)
                buckets[tag] = entry
            entry.pages.append(page)
        for entry in buckets.values():
            entry.pages.sort(key=lambda p: p.page)
        return sorted(buckets.values(), key=lambda e: (natural_key(e.tag), e.tag))

    #: 树上管线号的排序方式
    SORT_TAG = "tag"        # 按编号（自然序，PA-9 在 PA-10 前）
    SORT_ALPHA = "alpha"    # 按字母（忽略开头表示管子口径的数字段）
    SORT_PAGES = "pages"    # 按页数，多的在前
    SORT_PAGE = "page"      # 按首次出现的页号

    #: 目录树面板上的三种排法（用户定的）
    MODE_PAGE = "page"      # 按页码：一层平铺，哪一页在前谁在前
    MODE_NUMBER = "number"  # 按数字：先按口径段（50/100/150）分组，组内按页码
    MODE_MEDIUM = "medium"  # 按字母：先按介质段（CIS/CWR）分组，组内按后面几段字母序

    def groups(
        self,
        keyword: str = "",
        sort: str = "tag",
        only_pending: bool = False,
    ) -> list[tuple[str, list[IndexEntry]]]:
        """按前缀分组 + 关键词过滤 + 排序，返回 ``[(前缀, [条目…])]``。

        关键词只匹配管线号本身（大小写不敏感）；命中后整组保留，
        因为同一组里的兄弟编号往往就是要一起看的。
        """
        # 没识别出东西的页在面板顶上单独成组（待校对），这里不再重复列一遍
        found = [e for e in self.entries() if e.tag != UNSORTED]
        if keyword:
            needle = keyword.strip().lower()
            found = [e for e in found if needle in e.tag.lower()]
        if only_pending:
            found = [e for e in found if e.needs_check]

        buckets: dict[str, list[IndexEntry]] = {}

        def sort_items(items: list[IndexEntry]) -> list[IndexEntry]:
            if sort == PipeIndex.SORT_PAGES:
                # 页数多的在前；一样多就按编号，避免顺序抖动
                return sorted(items, key=lambda e: (-len(e.pages), natural_key(e.tag), e.tag))
            if sort == PipeIndex.SORT_PAGE:
                return sorted(
                    items,
                    key=lambda e: (e.page_numbers[0] if e.pages else 0, natural_key(e.tag)),
                )
            if sort == PipeIndex.SORT_ALPHA:
                return sorted(items, key=lambda e: (natural_key(alpha_key(e.tag)), e.tag))
            return sorted(items, key=lambda e: (natural_key(e.tag), e.tag))

        for entry in found:
            buckets.setdefault(tag_prefix(entry.tag), []).append(entry)

        def group_key(kv: tuple[str, list[IndexEntry]]) -> tuple:
            prefix, items = kv
            if sort == PipeIndex.SORT_ALPHA:
                # 「按字母」时组之间也要按字母排：否则组还是按口径 50/65/100 排，
                # 组内的字母序根本看不出来
                keys = [alpha_key(e.tag) for e in items]
                return (natural_key(min(keys, key=natural_key)), natural_key(prefix))
            return (natural_key(prefix),)

        return [
            (prefix, sort_items(items))
            for prefix, items in sorted(buckets.items(), key=group_key)
        ]

    def arrange(
        self,
        keyword: str = "",
        mode: str = "page",
        only_pending: bool = False,
    ) -> list[tuple[str | None, list[IndexEntry]]]:
        """目录树面板用的三种排法。

        返回 ``[(分组标题, [条目…])]``；``标题`` 为 ``None`` 表示「不分层，
        直接平铺」（按页码那一种）。

        - ``page``：按页码——谁先出现在前面的页谁在前，同页按编号自然序；
        - ``number``：按前导数字段（口径 50/100/150…）分组，组内按页码；
        - ``medium``：忽略前导数字段，按第二段（介质 CIS/CWR…）分组，
          组内按后面几段「字母、数字」的普通字符串序（``E120301`` 在
          ``E6102B01`` 前面，跟用户给的例子一致）。
        """
        found = [e for e in self.entries() if e.tag != UNSORTED]
        if keyword:
            needle = keyword.strip().lower()
            found = [e for e in found if needle in e.tag.lower()]
        if only_pending:
            found = [e for e in found if e.needs_check]

        def first_page(entry: IndexEntry) -> int:
            return entry.pages[0].page if entry.pages else 0

        if mode == PipeIndex.MODE_NUMBER:
            buckets: dict[str, list[IndexEntry]] = {}
            for entry in found:
                head = (entry.tag.split("-", 1)[0] or "").strip()
                if not head.isdigit():
                    head = "其他"
                buckets.setdefault(head, []).append(entry)
            for items in buckets.values():
                items.sort(key=lambda e: (first_page(e), natural_key(e.tag)))
            ordered = sorted(
                buckets.items(),
                key=lambda kv: (0, int(kv[0]), "") if kv[0].isdigit() else (1, 0, kv[0]),
            )
            return list(ordered)

        if mode == PipeIndex.MODE_MEDIUM:
            buckets = {}
            for entry in found:
                buckets.setdefault(medium_of(entry.tag), []).append(entry)
            for items in buckets.values():
                items.sort(key=lambda e: (tail_of(e.tag), natural_key(e.tag)))
            return sorted(buckets.items(), key=lambda kv: (natural_key(kv[0]), kv[0]))

        found.sort(key=lambda e: (first_page(e), natural_key(e.tag)))
        return [(None, found)]

    def pages_for(self, tag: str) -> list[int]:
        """某个管线号对应的页码（从 1 起），给「按管线号导出」用。"""
        return sorted(p.page_no for p in self._pages if p.tag == tag)

    def to_rows(self) -> list[dict]:
        """回写成数据库行，导出 PDF 书签时也用它。"""
        return [
            {
                "page": p.page,
                "tag": p.tag,
                "source": p.source,
                "confirmed": p.confirmed,
                "note": p.note,
            }
            for p in self._pages
        ]

    def bookmark_rows(self) -> list[tuple[int, str]]:
        """给书签用的 ``[(页号 0 基, 标题)]``，跳过没确认的页。"""
        return [(p.page, p.tag or f"第 {p.page_no} 页") for p in self._pages if p.confirmed]
