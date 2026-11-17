"""``.qpdf`` 的读写（第 9.3 / 9.4 节）。

这一层只认 SQL 和 Python 对象，不认 Qt 窗口——所以它可以在测试里脱离界面
单独跑，也方便将来换成别的存储。所有写操作都走**一个事务**，要么整批进去，
要么一点都不进去（第 9.5 / 16 章的"数据库事务"要求）。
"""

from __future__ import annotations

import json
import os
import sqlite3
import time

from core.annotation import Annotation, AnnotationStyle
from database.schema import SCHEMA_VERSION, migrate

APP_ID = "qpdf-project"

#: 源文件指纹取头尾各这么多字节——1.3 GB 的文件也能瞬间算完
FINGERPRINT_BYTES = 64 * 1024


class ProjectError(RuntimeError):
    """项目文件打不开 / 版本不对 / 不是 QPDF 项目。"""


def fingerprint_file(path: str) -> str:
    """源 PDF 的快速指纹：大小 + 头尾各 64 KB 的 sha1。

    第 9.3 节要"检查原 PDF 是否被移动或修改"。整份算 sha256 对 1.3 GB
    太慢（每次打开都要几十秒），头尾抽样足以发现"换了个文件"和"被重新导出"，
    剩下极小的概率差异由大小 + mtime 一起兜住。
    """
    import hashlib

    size = os.path.getsize(path)
    digest = hashlib.sha1()
    digest.update(str(size).encode("ascii"))
    with open(path, "rb") as handle:
        digest.update(handle.read(FINGERPRINT_BYTES))
        if size > FINGERPRINT_BYTES * 2:
            handle.seek(-FINGERPRINT_BYTES, os.SEEK_END)
            digest.update(handle.read(FINGERPRINT_BYTES))
    return digest.hexdigest()


class ProjectRepository:
    """一个 ``.qpdf`` 文件。"""

    def __init__(self, path: str) -> None:
        self.path = os.path.abspath(path)
        self._conn: sqlite3.Connection | None = None

    # -- 生命周期 ---------------------------------------------------------

    @property
    def is_open(self) -> bool:
        return self._conn is not None

    def exists(self) -> bool:
        return os.path.isfile(self.path)

    def open(self, allow_new: bool = False) -> None:
        """打开项目文件。

        ``allow_new=True`` 是 ``create()`` 建库时用的：那时库里还没有 app 标记，
        不该被判成"这不是 QPDF 项目文件"。
        """
        if self._conn is not None:
            return
        if not os.path.isfile(self.path):
            raise ProjectError(f"项目文件不存在：{self.path}")
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        # 第 9.5 节要求评估 WAL：它让"自动保存时的一次小写入"不必停下来等磁盘同步，
        # 崩溃后 SQLite 自己会用 -wal 回放已提交的事务，正好满足"恢复到上一次成功提交"。
        # 代价是会多出 -wal/-shm 两个临时文件，备份前要做 checkpoint（见 checkpoint()）。
        #
        # PRAGMA 也可能在文件损坏时抛 sqlite3.DatabaseError（典型：disk image is malformed）。
        # 以前这几行在 try 外，异常会一路冒到 Qt 顶层，整个程序直接弹「Unhandled exception」。
        # 现在统一转成 ProjectError：调用方可以提示、可以保护原文件，但绝不能让程序崩掉。
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._conn = conn
            migrate(conn)
            app = self.get_meta("app")
        except Exception as exc:  # noqa: BLE001
            self._conn = None
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
            raise ProjectError(f"无法读取项目文件：{exc}") from exc
        if app != APP_ID and not allow_new:
            self.close()
            raise ProjectError("这不是 QPDF 项目文件")

    def create(self, source: dict, page_sizes: list, app_version: str = "") -> None:
        if self.exists():
            raise ProjectError(f"项目文件已存在：{self.path}")
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        # 先落一个空文件，再走正常的 open(allow_new=True)——这样建库和开库共用同一段初始化逻辑
        with open(self.path, "ab"):
            pass
        self.open(allow_new=True)
        now = time.time()
        self.set_meta_many(
            {
                "app": APP_ID,
                "app_version": app_version,
                "schema_version": str(SCHEMA_VERSION),
                "created_at": f"{now:.6f}",
                "saved_at": f"{now:.6f}",
                "source_path": source.get("path", ""),
                "source_size": str(source.get("size", 0)),
                "source_mtime": f"{float(source.get('mtime', 0.0)):.6f}",
                "source_fingerprint": source.get("fingerprint", ""),
                "page_count": str(source.get("page_count", 0)),
            }
        )
        self.set_json("page_sizes", page_sizes)

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None

    def checkpoint(self) -> None:
        """把 WAL 合并回主文件（备份/关闭前调用，保证备份是单文件）。"""
        if self._conn is None:
            return
        try:
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass

    def file_size(self) -> int:
        try:
            return int(os.path.getsize(self.path))
        except OSError:
            return 0

    def maintenance_stats(self) -> dict:
        """项目文件体积/空闲页/日志行数，用来判断关闭时要不要整理。"""
        conn = self._require()
        stats = {
            "file_size": self.file_size(),
            "page_count": 0,
            "freelist_count": 0,
            "page_size": 4096,
            "log_count": 0,
            "match_count": 0,
        }
        try:
            stats["page_count"] = int(conn.execute("PRAGMA page_count").fetchone()[0])
            stats["freelist_count"] = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
            stats["page_size"] = int(conn.execute("PRAGMA page_size").fetchone()[0])
            stats["log_count"] = int(conn.execute("SELECT COUNT(*) FROM op_log").fetchone()[0])
            stats["match_count"] = int(
                conn.execute("SELECT COUNT(*) FROM match_annotations").fetchone()[0]
            )
        except sqlite3.Error:
            pass
        return stats

    def compact(self, keep_log: int = 5000) -> dict:
        """裁剪过老的操作日志并 VACUUM，把文件真正缩小。

        只动 ``op_log``（撤销历史/审计），不动 ``annotations`` 和
        ``match_annotations``。VACUUM 会重写整个文件，所以只在关闭时调用。
        """
        conn = self._require()
        before = self.maintenance_stats()
        with conn:
            conn.execute(
                "DELETE FROM op_log WHERE seq NOT IN"
                " (SELECT seq FROM op_log ORDER BY seq DESC LIMIT ?)",
                (max(1, int(keep_log)),),
            )
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass
        try:
            conn.execute("VACUUM")
            conn.execute("PRAGMA optimize")
        except sqlite3.Error:
            pass
        after = self.maintenance_stats()
        return {"before": before, "after": after}

    def compact_if_needed(self, keep_log: int = 5000) -> dict | None:
        """文件有空闲页或日志太多才整理；正常项目关闭时不做无谓重写。"""
        try:
            stats = self.maintenance_stats()
        except ProjectError:
            return None
        free_bytes = int(stats.get("freelist_count", 0)) * int(stats.get("page_size", 4096))
        if int(stats.get("log_count", 0)) <= keep_log and free_bytes < 2 * 1024 * 1024:
            return None
        return self.compact(keep_log=keep_log)

    # -- 内部 -------------------------------------------------------------

    def _require(self) -> sqlite3.Connection:
        if self._conn is None:
            raise ProjectError("项目还没有打开")
        return self._conn

    # -- meta / settings --------------------------------------------------

    def get_meta(self, key: str, default=None):
        conn = self._require()
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return default if row is None else row[0]

    def set_meta(self, key: str, value) -> None:
        self.set_meta_many({key: value})

    def set_meta_many(self, values: dict) -> None:
        conn = self._require()
        with conn:
            conn.executemany(
                "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
                [(k, "" if v is None else str(v)) for k, v in values.items()],
            )

    def get_json(self, key: str, default=None):
        raw = self.get_meta(key)
        if raw is None or raw == "":
            return default
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return default

    def set_json(self, key: str, value) -> None:
        self.set_meta(key, json.dumps(value, ensure_ascii=False))

    def source_info(self) -> dict:
        return {
            "path": self.get_meta("source_path", ""),
            "size": int(self.get_meta("source_size", 0) or 0),
            "mtime": float(self.get_meta("source_mtime", 0.0) or 0.0),
            "fingerprint": self.get_meta("source_fingerprint", ""),
            "page_count": int(self.get_meta("page_count", 0) or 0),
        }

    def page_sizes(self) -> list:
        return self.get_json("page_sizes", []) or []

    def saved_at(self) -> float:
        return float(self.get_meta("saved_at", 0.0) or 0.0)

    def mark_saved(self) -> None:
        self.set_meta("saved_at", f"{time.time():.6f}")

    # -- 标注 -------------------------------------------------------------

    def load_annotations(self) -> list[Annotation]:
        conn = self._require()
        rows = conn.execute(
            "SELECT id, kind, page, x, y, content, style, seq_group, seq_index,"
            " locked, hidden, created_at, updated_at FROM annotations ORDER BY rowid"
        ).fetchall()
        result: list[Annotation] = []
        for row in rows:
            payload = dict(row)
            try:
                payload["style"] = json.loads(payload.get("style") or "{}")
            except (TypeError, ValueError):
                payload["style"] = {}
            payload["locked"] = bool(payload.get("locked"))
            payload["hidden"] = bool(payload.get("hidden"))
            result.append(Annotation.from_dict(payload))
        return result

    def annotation_ids(self) -> set[str]:
        conn = self._require()
        return {row[0] for row in conn.execute("SELECT id FROM annotations")}

    def write_annotations(self, upserts: list[Annotation], deletes: list[str]) -> None:
        """只写标注（简单场景用的窄接口）。"""
        self.write_changes(upserts=upserts, deletes=deletes)

    def write_changes(
        self,
        upserts: list[Annotation] | None = None,
        deletes: list[str] | None = None,
        sequences=None,
        boards=None,
        log_rows: list[dict] | None = None,
        saved_at: float | None = None,
    ) -> None:
        """一次落盘 = 一个事务（第 9.5 节）。

        标注增删改、编号序列、工作台、操作日志、保存时间**要么全部生效，要么全都不生效**。
        中途崩了 SQLite 会回滚到事务开始前，所以重新打开项目看到的
        一定是最后一次完整提交的状态，而不是写了一半的残局（第 16 章）。

        注意：这里绝不碰原始 PDF。点一次数字只改这一张表的几行。
        """
        conn = self._require()
        with conn:
            self._write_annotations(conn, upserts or [], deletes or [])
            if sequences is not None:
                self._put_meta(
                    conn, "sequences", json.dumps(sequences, ensure_ascii=False)
                )
            if boards is not None:
                self._put_meta(conn, "boards", json.dumps(boards, ensure_ascii=False))
            if log_rows:
                self._write_log(conn, log_rows)
            if saved_at is not None:
                self._put_meta(conn, "saved_at", f"{float(saved_at):.6f}")

    @staticmethod
    def _put_meta(conn, key: str, value) -> None:
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
            (key, "" if value is None else str(value)),
        )

    @staticmethod
    def _write_annotations(
        conn, upserts: list[Annotation], deletes: list[str]
    ) -> None:
        if deletes:
            conn.executemany("DELETE FROM annotations WHERE id=?", [(i,) for i in deletes])
        if upserts:
            conn.executemany(
                "INSERT OR REPLACE INTO annotations"
                "(id, kind, page, x, y, content, style, seq_group, seq_index,"
                " locked, hidden, created_at, updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        ann.id,
                        ann.kind,
                        ann.page,
                        ann.x,
                        ann.y,
                        ann.content,
                        json.dumps(ann.style.to_dict(), ensure_ascii=False),
                        ann.seq_group,
                        ann.seq_index,
                        1 if ann.locked else 0,
                        1 if ann.hidden else 0,
                        ann.created_at,
                        ann.updated_at,
                    )
                    for ann in upserts
                ],
            )

    @staticmethod
    def _write_log(conn, rows: list[dict]) -> None:
        conn.executemany(
            "INSERT INTO op_log(ts, action, target, before_state, after_state, note)"
            " VALUES(?,?,?,?,?,?)",
            [
                (
                    float(row.get("ts") or time.time()),
                    str(row.get("action", "")),
                    str(row.get("target", "")),
                    row.get("before"),
                    row.get("after"),
                    row.get("note"),
                )
                for row in rows
            ],
        )

    def count_annotations(self) -> int:
        conn = self._require()
        return int(conn.execute("SELECT COUNT(*) FROM annotations").fetchone()[0])

    # -- 操作日志（第 9.4 节）---------------------------------------------

    def append_log(self, rows: list[dict]) -> None:
        if not rows:
            return
        conn = self._require()
        with conn:
            self._write_log(conn, rows)

    def log_count(self) -> int:
        conn = self._require()
        return int(conn.execute("SELECT COUNT(*) FROM op_log").fetchone()[0])

    def recent_log(self, limit: int = 20) -> list[dict]:
        conn = self._require()
        rows = conn.execute(
            "SELECT ts, action, target, before_state, after_state, note"
            " FROM op_log ORDER BY seq DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        return [dict(row) for row in rows]

    # -- 区域识别结果（Phase 10 / F01）--------------------------------------
    def load_scan_results(self) -> dict[int, dict]:
        """返回 ``{页号: {raw, tag, source, fixed, confirmed, note, updated_at}}``，页号从 0 起。"""
        conn = self._require()
        rows = conn.execute(
            "SELECT page, raw, tag, source, fixed, confirmed, note, updated_at"
            " FROM scan_results"
        ).fetchall()
        return {
            int(r["page"]): {
                "raw": r["raw"],
                "tag": r["tag"],
                "source": r["source"],
                "fixed": bool(r["fixed"]),
                "confirmed": bool(r["confirmed"]),
                "note": r["note"] or "",
                "updated_at": float(r["updated_at"]),
            }
            for r in rows
        }

    def write_scan_results(self, rows: list[dict]) -> None:
        """批量写识别结果（一次事务）。``rows`` 每项含 page/raw/tag/source/fixed/updated_at，
        可选 confirmed / note（缺省 1 / 空串）。"""
        if not rows:
            return
        conn = self._require()
        payload = [
            {
                "page": int(row["page"]),
                "raw": row.get("raw", ""),
                "tag": row.get("tag", ""),
                "source": row.get("source", ""),
                "fixed": 1 if row.get("fixed") else 0,
                "confirmed": 0 if row.get("confirmed") is False else 1,
                "note": row.get("note", "") or "",
                "updated_at": row.get("updated_at", 0.0),
            }
            for row in rows
        ]
        with conn:
            conn.executemany(
                "INSERT OR REPLACE INTO scan_results"
                "(page, raw, tag, source, fixed, confirmed, note, updated_at)"
                " VALUES(:page, :raw, :tag, :source, :fixed, :confirmed, :note, :updated_at)",
                payload,
            )

    def clear_scan_results(self) -> None:
        conn = self._require()
        with conn:
            conn.execute("DELETE FROM scan_results")

    def count_scan_results(self) -> int:
        conn = self._require()
        return int(conn.execute("SELECT COUNT(*) FROM scan_results").fetchone()[0])

    # -- 数据匹配标注（v4）-------------------------------------------------
    def add_data_import(
        self,
        path: str,
        fingerprint: str,
        sheet: str,
        row_count: int,
        key_column: str,
        display_columns: list[str],
        template: str,
        note: str = "",
    ) -> int:
        """记一次导入 = 一个版本号，自动标注都挂在它上面。"""
        conn = self._require()
        with conn:
            cursor = conn.execute(
                "INSERT INTO data_imports"
                "(imported_at, path, fingerprint, sheet, row_count, key_column,"
                " display_columns, template, note) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    time.time(),
                    path or "",
                    fingerprint or "",
                    sheet or "",
                    int(row_count or 0),
                    key_column or "",
                    json.dumps(list(display_columns or []), ensure_ascii=False),
                    template or "",
                    note or "",
                ),
            )
            return int(cursor.lastrowid or 0)

    def _import_row(self, row) -> dict:
        payload = dict(row)
        try:
            payload["display_columns"] = json.loads(payload.get("display_columns") or "[]")
        except (TypeError, ValueError):
            payload["display_columns"] = []
        return payload

    def list_data_imports(self, limit: int = 20) -> list[dict]:
        conn = self._require()
        rows = conn.execute(
            "SELECT id, imported_at, path, fingerprint, sheet, row_count,"
            " key_column, display_columns, template, note"
            " FROM data_imports ORDER BY id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        return [self._import_row(row) for row in rows]

    def last_data_import(self) -> dict | None:
        rows = self.list_data_imports(1)
        return rows[0] if rows else None

    def write_match_annotations(self, rows: list[dict], import_id: int | None = None) -> None:
        """写自动标注的追溯记录（一次事务）。"""
        if not rows:
            return
        conn = self._require()
        now = time.time()
        payload = [
            (
                str(row["annotation_id"]),
                int(import_id) if import_id else row.get("import_id"),
                int(row.get("page", 0)),
                row.get("raw", "") or "",
                row.get("tag", "") or "",
                row.get("status", "auto") or "auto",
                json.dumps(row.get("record") or {}, ensure_ascii=False),
                row.get("content", "") or "",
                row.get("note", "") or "",
                float(row.get("created_at") or now),
            )
            for row in rows
        ]
        with conn:
            conn.executemany(
                "INSERT OR REPLACE INTO match_annotations"
                "(annotation_id, import_id, page, raw, tag, status, record_json,"
                " content, note, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                payload,
            )

    def load_match_annotations(self) -> list[dict]:
        conn = self._require()
        rows = conn.execute(
            "SELECT annotation_id, import_id, page, raw, tag, status, record_json,"
            " content, note, created_at FROM match_annotations"
        ).fetchall()
        out: list[dict] = []
        for row in rows:
            payload = dict(row)
            try:
                payload["record"] = json.loads(payload.pop("record_json") or "{}")
            except (TypeError, ValueError):
                payload["record"] = {}
            out.append(payload)
        return out

    def delete_match_annotations(self, ids: list[str]) -> None:
        if not ids:
            return
        conn = self._require()
        with conn:
            conn.executemany(
                "DELETE FROM match_annotations WHERE annotation_id=?",
                [(item,) for item in ids],
            )

    def clear_match_annotations(self) -> None:
        conn = self._require()
        with conn:
            conn.execute("DELETE FROM match_annotations")

    def prune_match_annotations(self, valid_ids: set[str]) -> None:
        """删掉指向已不存在的标注的追溯行（撤销/删标注之后的自愈）。"""
        conn = self._require()
        try:
            ids = [row[0] for row in conn.execute("SELECT annotation_id FROM match_annotations")]
        except sqlite3.Error:
            return
        stale = [item for item in ids if item not in valid_ids]
        self.delete_match_annotations(stale)

    def count_match_annotations(self) -> int:
        conn = self._require()
        return int(conn.execute("SELECT COUNT(*) FROM match_annotations").fetchone()[0])
