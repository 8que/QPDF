"""把识别出来的管线号变成 PDF 书签（第 Phase 10 章）。

一条规则值得写在这儿：**连续几页是同一个管线号时只留一个书签**。一套图纸里同一根
管线经常横跨十几页，逐页做书签等于把书签栏刷成一样的内容，反而找不到东西。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Bookmark:
    """一层书签。``page`` 是 0 起的页号。"""

    title: str
    page: int

    def to_dict(self) -> dict:
        return {"title": self.title, "page": int(self.page)}

    @classmethod
    def from_dict(cls, payload: dict) -> "Bookmark":
        return cls(title=str(payload.get("title", "")), page=int(payload.get("page", 0)))


def build_bookmarks(tags: dict[int, str], merge_consecutive: bool = True) -> list[Bookmark]:
    """``{页号: 管线号}`` → 书签列表（按页号升序）。

    * 空标签的页跳过；
    * ``merge_consecutive`` 为真时，紧挨着的相同标签只保留第一条；
    * 同一页标签重复时不合并（不同页才算"连续"）。
    """
    items = sorted((page, (tag or "").strip()) for page, tag in tags.items())
    marks: list[Bookmark] = []
    last_tag = ""
    last_page = -2
    for page, tag in items:
        if not tag:
            continue
        if merge_consecutive and tag == last_tag and page == last_page + 1:
            last_page = page
            continue
        marks.append(Bookmark(title=tag, page=page))
        last_tag = tag
        last_page = page
    return marks


def to_pymupdf_toc(marks: list[Bookmark]) -> list[list]:
    """``pymupdf.Document.set_toc`` 要的格式：``[[level, title, page(1 起)], ...]``。"""
    return [[1, m.title, int(m.page) + 1] for m in marks]
