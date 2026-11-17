"""QReport 的只读数据层。

QReport 是独立于 QPDF 界面的**只读**小工具：拿到一个 ``.qpdf``，按管线号找出相关页，
把页上的数字标注按 ``meta.sequences`` 还原成"用户当时看到的编号"，再算最大值。

设计约束（别破坏）：

1. **只读**。用 SQLite 的 ``mode=ro`` 打开；绝不在原文件上建表、写 WAL、改 meta。
   WAL 模式 + 别的进程还开着时会打不开，这时**复制到临时目录**再读，原件一个字节不动。
2. **不依赖 Qt / PyMuPDF**。这一层只 import 标准库，能单独拷到别的机器上跑。
3. **编号要还原，不是读 content**。``content`` 只是建标注那一刻的快照；插队之后
   显示值变了而 content 不会跟着改，所以真实编号 = ``start + step * seq_index``。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass, field

APP_ID = "qpdf-project"

#: 数字标注的 kind（和 core/annotation.py 保持一致）
KIND_NUMBER = "number"

#: 半角/全角百分号都算"百分比批注"
PERCENT_MARKS = ("%", "％")

#: 纯整数：只认可选负号 + 数字；"12a"、"1.5"、"1,000" 一律不算
_INT_RE = re.compile(r"^-?\d+$")


class QpdfError(RuntimeError):
    """.qpdf 打不开 / 不是 QPDF 项目 / 结构不对。"""


@dataclass(frozen=True)
class NumberMark:
    """一条"能算出整数编号"的数字标注。"""

    annotation_id: str
    page: int          # 0 基
    x: float
    y: float
    seq_group: str
    seq_index: int | None
    value: int
    source: str        # "sequence"（按编号组算出来）/ "content"（没有编号组，退回内容）
    hidden: bool = False

    @property
    def page_no(self) -> int:
        """给人看的页码（1 起）。"""
        return self.page + 1

    @property
    def text(self) -> str:
        return str(self.value)


@dataclass(frozen=True)
class SkippedMark:
    """看得到、但没法参与统计的数字标注（百分比、非整数、编号组丢了…）。"""

    annotation_id: str
    page: int
    raw: str
    reason: str

    @property
    def page_no(self) -> int:
        return self.page + 1


@dataclass
class TagReport:
    """一个管线号的统计结果。"""

    tag: str
    pages: list[int] = field(default_factory=list)   # 0 基
    marks: list[NumberMark] = field(default_factory=list)
    skipped: list[SkippedMark] = field(default_factory=list)

    @property
    def max_value(self) -> int | None:
        return max((mark.value for mark in self.marks), default=None)

    @property
    def page_nos(self) -> list[int]:
        return [page + 1 for page in self.pages]

    def to_dict(self) -> dict:
        return {
            "tag": self.tag,
            "pages": self.page_nos,
            "count": len(self.marks),
            "max": self.max_value,
            "marks": [
                {
                    "id": mark.annotation_id,
                    "page": mark.page_no,
                    "value": mark.value,
                    "group": mark.seq_group,
                    "index": mark.seq_index,
                    "source": mark.source,
                    "hidden": mark.hidden,
                    "x": round(mark.x, 2),
                    "y": round(mark.y, 2),
                }
                for mark in sorted(self.marks, key=lambda m: (m.page, m.value))
            ],
            "skipped": [
                {
                    "id": item.annotation_id,
                    "page": item.page_no,
                    "raw": item.raw,
                    "reason": item.reason,
                }
                for item in self.skipped
            ],
        }


class QpdfReader:
    """只读地打开一个 ``.qpdf``。

    用法::

        with QpdfReader(path) as reader:
            report = reader.report_for_tag("50-S0-P1302A01-N1B-H60")
            print(report.max_value)
    """

    def __init__(self, path: str) -> None:
        self.path = os.path.abspath(path)
        self._conn: sqlite3.Connection | None = None
        self._temp_dir = ""
        self._open()

    # ------------------------------------------------------------------ 生命周期

    def _open(self) -> None:
        if not os.path.isfile(self.path):
            raise QpdfError(f"文件不存在：{self.path}")
        try:
            conn = self._connect_ro(self.path)
        except sqlite3.Error:
            # WAL 库被别的进程开着时，只读连接拿不到 -shm，会打不开。
            # 复制一份到临时目录再读——原件仍然一个字节不动。
            conn = self._connect_copy()
        try:
            app = self._meta_of(conn).get("app", "")
            if app != APP_ID:
                raise QpdfError("这不是 QPDF 项目文件（meta.app 对不上）")
        except sqlite3.Error as exc:
            conn.close()
            raise QpdfError(f"读不了项目文件：{exc}") from exc
        self._conn = conn

    @staticmethod
    def _connect_ro(path: str) -> sqlite3.Connection:
        uri = "file:///" + path.replace("\\", "/").lstrip("/") + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=2.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        # 真读一下，坏库/没权限在这里就暴露出来
        conn.execute("SELECT COUNT(*) FROM meta").fetchone()
        return conn

    def _connect_copy(self) -> sqlite3.Connection:
        self._temp_dir = tempfile.mkdtemp(prefix="qreport-")
        target = os.path.join(self._temp_dir, os.path.basename(self.path))
        shutil.copy2(self.path, target)
        for suffix in ("-wal", "-shm"):
            side = self.path + suffix
            if os.path.isfile(side):
                shutil.copy2(side, target + suffix)
        return self._connect_ro(target)

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None
        if self._temp_dir:
            shutil.rmtree(self._temp_dir, ignore_errors=True)
            self._temp_dir = ""

    def __enter__(self) -> "QpdfReader":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise QpdfError("项目文件还没打开")
        return self._conn

    # ------------------------------------------------------------------ 元信息

    @staticmethod
    def _meta_of(conn: sqlite3.Connection) -> dict:
        return {row["key"]: row["value"] for row in conn.execute("SELECT key, value FROM meta")}

    @property
    def meta(self) -> dict:
        return self._meta_of(self.conn)

    def source_info(self) -> dict:
        meta = self.meta
        return {
            "pdf": meta.get("source_path", ""),
            "page_count": int(meta.get("page_count", 0) or 0),
            "app_version": meta.get("app_version", ""),
            "saved_at": float(meta.get("saved_at", 0) or 0),
            "fingerprint": meta.get("source_fingerprint", ""),
            "path": self.path,
        }

    def sequences(self) -> dict[str, tuple[int, int]]:
        """``{group: (start, step)}``；旧库没有 sequences 时返回空表（会退回 content）。"""
        raw = self.meta.get("sequences", "")
        try:
            payload = json.loads(raw) if raw else {}
        except ValueError:
            payload = {}
        out: dict[str, tuple[int, int]] = {}
        for item in payload.get("sequences", []) if isinstance(payload, dict) else []:
            group = str(item.get("group") or "")
            if not group:
                continue
            try:
                out[group] = (int(item.get("start", 1)), int(item.get("step", 1)))
            except (TypeError, ValueError):
                continue
        return out

    # ------------------------------------------------------------------ 页面

    def tags(self) -> list[str]:
        """项目里认出来的管线号（去重、排好序）。"""
        rows = self.conn.execute(
            "SELECT DISTINCT tag FROM scan_results WHERE tag IS NOT NULL AND tag <> '' ORDER BY tag"
        )
        return [row["tag"] for row in rows]

    def pages_for_tag(self, tag: str) -> list[int]:
        """这个管线号出现的页面（**0 基**，按页序、去重）。"""
        rows = self.conn.execute(
            "SELECT DISTINCT page FROM scan_results WHERE tag = ? ORDER BY page", (tag,)
        )
        return [int(row["page"]) for row in rows]

    def _columns(self, table: str) -> set[str]:
        """表里有哪些列（旧项目没有 ``confirmed``/``note``，拼 SQL 前先看一眼）。"""
        if self._conn is None:
            raise QpdfError("项目文件还没打开")
        return {row[1] for row in self._conn.execute(f"PRAGMA table_info({table})")}

    def scan_rows(self, pages: list[int] | None = None) -> list[dict]:
        """识别结果明细（给"这个编号在哪几页、原文是什么"用）。

        老项目（SCHEMA v2，例如 1.3.0 建的库）里没有 ``confirmed``/``note`` 两列，
        这里按实际存在的列取，缺的补 ``None``——QReport 只读，不能指望库被升级过。
        """
        columns = self._columns("scan_results")
        wanted = ["page", "raw", "tag", "source", "confirmed", "note"]
        picked = [name for name in wanted if name in columns]
        sql = f"SELECT {', '.join(picked)} FROM scan_results"
        params: list = []
        if pages:
            marks = ",".join("?" for _ in pages)
            sql += f" WHERE page IN ({marks})"
            params = [int(p) for p in pages]
        sql += " ORDER BY page"
        out: list[dict] = []
        for row in self.conn.execute(sql, params):
            item = {name: None for name in wanted}
            item.update({name: row[name] for name in picked})
            out.append(item)
        return out

    # ------------------------------------------------------------------ 标注

    def number_annotations(self, pages: list[int] | None = None) -> list[sqlite3.Row]:
        """数字标注原始行（``kind = 'number'``）。"""
        sql = (
            "SELECT id, page, x, y, content, seq_group, seq_index, locked, hidden"
            " FROM annotations WHERE kind = ?"
        )
        params: list = [KIND_NUMBER]
        if pages:
            marks = ",".join("?" for _ in pages)
            sql += f" AND page IN ({marks})"
            params.extend(int(p) for p in pages)
        sql += " ORDER BY page, y, x"
        return list(self.conn.execute(sql, params))

    def value_of(self, row: sqlite3.Row | dict, seqs: dict[str, tuple[int, int]] | None = None
                 ) -> tuple[int | None, str, str]:
        """一条数字标注的真实显示值。

        返回 ``(值, 来源, 跳过原因)``：能算就是 ``(int, "sequence"/"content", "")``；
        算不出来是 ``(None, "", 原因)``。
        """
        seqs = self.sequences() if seqs is None else seqs
        content = str(row["content"] or "").strip()
        group = str(row["seq_group"] or "")
        index = row["seq_index"]

        for mark in PERCENT_MARKS:
            if mark in content:
                return None, "", "带百分号（不是编号）"

        if group and index is not None and group in seqs:
            start, step = seqs[group]
            return start + step * int(index), "sequence", ""

        if _INT_RE.match(content):
            # 编号组丢了（老项目 / 手改过库），但内容本身是纯整数：还能救回来
            return int(content), "content", ""

        if group and group not in seqs:
            return None, "", f"编号组「{group}」不在 sequences 里，且内容是「{content or '空'}」"
        if not content:
            return None, "", "标注内容是空的"
        return None, "", f"内容不是纯整数：「{content}」"

    def percentages(self) -> dict[str, str]:
        """``{管线号: "20%"}`` —— 探伤比例（焊口检测比例）。

        两个来源，谁有就用谁：

        1. QPDF 的「数据匹配标注」（``match_annotations``）：F05 把 Excel 里的
           「焊缝检测比例」写成页面上的百分比文字，这里直接拿它的 ``content``；
        2. 页面上本来就有 ``%`` 的文字标注（用户手打的），按页归属到那个管线号。

        同一个管线号有多个不同百分比时取第一个出现的（QPDF 那边一页只会有一种）。
        """
        out: dict[str, str] = {}
        tables = {
            row[0]
            for row in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "match_annotations" in tables:
            try:
                rows = self.conn.execute(
                    "SELECT tag, content FROM match_annotations WHERE tag IS NOT NULL"
                ).fetchall()
            except sqlite3.Error:
                rows = []
            for row in rows:
                tag = str(row["tag"] or "").strip()
                text = str(row["content"] or "").strip()
                if tag and text and tag not in out:
                    out[tag] = text

        page_tag = {
            int(row["page"]): str(row["tag"] or "").strip()
            for row in self.scan_rows()
            if row["tag"]
        }
        for row in self.conn.execute(
            "SELECT page, content FROM annotations WHERE kind = 'text'"
        ):
            text = str(row["content"] or "").strip()
            if not text or not any(mark in text for mark in PERCENT_MARKS):
                continue
            tag = page_tag.get(int(row["page"]))
            if tag and tag not in out:
                out[tag] = text
        return out

    def report_for_tag(self, tag: str, include_hidden: bool = True) -> TagReport:
        """核心入口：按管线号汇总数字标注。"""
        tag = (tag or "").strip()
        pages = self.pages_for_tag(tag)
        report = TagReport(tag=tag, pages=pages)
        if not pages:
            return report
        seqs = self.sequences()
        for row in self.number_annotations(pages):
            if row["hidden"] and not include_hidden:
                continue
            value, source, reason = self.value_of(row, seqs)
            if value is None:
                report.skipped.append(
                    SkippedMark(
                        annotation_id=str(row["id"]),
                        page=int(row["page"]),
                        raw=str(row["content"] or ""),
                        reason=reason,
                    )
                )
                continue
            report.marks.append(
                NumberMark(
                    annotation_id=str(row["id"]),
                    page=int(row["page"]),
                    x=float(row["x"] or 0.0),
                    y=float(row["y"] or 0.0),
                    seq_group=str(row["seq_group"] or ""),
                    seq_index=int(row["seq_index"]) if row["seq_index"] is not None else None,
                    value=int(value),
                    source=source,
                    hidden=bool(row["hidden"]),
                )
            )
        report.marks.sort(key=lambda m: (m.page, m.value))
        report.skipped.sort(key=lambda s: s.page)
        return report

    def report_all(self, include_hidden: bool = True) -> dict[str, TagReport]:
        """所有管线号各算一份（``{tag: TagReport}``）。"""
        return {tag: self.report_for_tag(tag, include_hidden) for tag in self.tags()}
