"""管线号 → 焊口号 / 页码 / 探伤比例 的清单（导出 Excel 用）。

口径和 ``QpdfReader.report_for_tag()`` 一致：

- 一个管线号 = 识别结果（``scan_results.tag``）里那个字符串，**原样使用**：
  带页号后缀的（``…-（06）``）不合并、不去后缀；
- 焊口号 = 这个管线号所有页面上的数字标注，按编号组还原成真实数字；
- 只留整数（百分比等算不出来的跳过，不写进焊口号列）；
- **一个数字一行**，同一个数字在几页重复出现只留一行；
- 管线号按「第一次出现的 PDF 页码」排（页码小的在前），同一个管线号里数字从小到大；
- 每一行都带上这个管线号出现的**全部页码**（多页就全列，1 起）；
- 探伤比例（``20%``）来自 QPDF 的数据匹配标注（或页面上手打的百分比文字）；
- **每个管线号至少一行**：没标焊口的，焊口号留空、页码照列、比例照写；
  既没焊口也没比例的（多半是识别噪声）也照原样列出来——用户明确要求"都要放"。
"""

from __future__ import annotations

from dataclasses import dataclass

from .reader import QpdfReader

#: 多页之间的分隔符（Excel 里一个格子放得下，复制出来也好认）
PAGE_SEPARATOR = "、"


@dataclass(frozen=True)
class WeldRow:
    """清单里的一行：某个管线号上的某个焊口号（或者只有管线号/比例的那一行）。"""

    tag: str
    value: int | None          # None = 这一行没有焊口号（只是把页码和比例列出来）
    page: int                  # 这个数字第一次出现的页（0 基），排查用
    pages: tuple[int, ...] = ()  # 这个管线号出现的全部页（0 基，升序）
    percent: str = ""          # 探伤比例，如 "20%"；没有就空着

    @property
    def weld_no(self) -> str:
        """表格里显示的样子：``2#``、``17#``；没有焊口号就是空串。"""
        return "" if self.value is None else f"{self.value}#"

    @property
    def pages_text(self) -> str:
        """表格里显示的页码：``271``、``271、272``（1 起）。"""
        return PAGE_SEPARATOR.join(str(no + 1) for no in self.pages)


def weld_rows(
    reader: QpdfReader,
    tag: str | None = None,
    include_hidden: bool = False,
    include_empty: bool = True,
) -> list[WeldRow]:
    """整理出「管线号 / 焊口号 / 页码 / 探伤比例」的全部行（顺序即导出的顺序）。

    默认**所有管线号都出一行**（``include_empty=True``）：连既没焊口、也没比例的
    识别噪声编号也照原样列出来，宁可多一行空行，也不让现场对不上号。
    """
    wanted = (tag or "").strip()

    page_tag: dict[int, str] = {}
    tag_pages: dict[str, list[int]] = {}
    for row in reader.scan_rows():
        page = int(row["page"])
        name = str(row["tag"] or "").strip()
        if not name:
            continue
        page_tag[page] = name
        pages = tag_pages.setdefault(name, [])
        if not pages or pages[-1] != page:  # scan_rows 已按 page 排序
            pages.append(page)

    percents = reader.percentages()
    seqs = reader.sequences()
    values: dict[str, dict[int, int]] = {}
    for row in reader.number_annotations():
        if row["hidden"] and not include_hidden:
            continue
        name = page_tag.get(int(row["page"]))
        if not name or (wanted and name != wanted):
            continue
        value, _source, _reason = reader.value_of(row, seqs)
        if value is None:
            continue
        bucket = values.setdefault(name, {})
        bucket.setdefault(int(value), int(row["page"]))

    rows: list[WeldRow] = []
    for name in sorted(tag_pages, key=lambda item: (tag_pages[item][0], item)):
        if wanted and name != wanted:
            continue
        pages = tuple(sorted(tag_pages[name]))
        percent = percents.get(name, "")
        welds = values.get(name)
        if welds:
            for value in sorted(welds):
                rows.append(
                    WeldRow(
                        tag=name,
                        value=value,
                        page=welds[value],
                        pages=pages,
                        percent=percent,
                    )
                )
            continue
        if percent or include_empty:
            # 没标焊口：也给它留一行，焊口号空着，页码和探伤比例照列（没有就空着）
            rows.append(
                WeldRow(
                    tag=name,
                    value=None,
                    page=pages[0],
                    pages=pages,
                    percent=percent,
                )
            )
    return rows
