"""``.qpdf`` 的 SQLite 表结构（第 9.3 节）。"""

from __future__ import annotations

import sqlite3

#: 结构版本，将来加字段时靠它做迁移
SCHEMA_VERSION = 4

_DDL = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS annotations (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    page        INTEGER NOT NULL,
    x           REAL NOT NULL,
    y           REAL NOT NULL,
    content     TEXT NOT NULL DEFAULT '',
    style       TEXT NOT NULL DEFAULT '{}',
    seq_group   TEXT,
    seq_index   INTEGER,
    locked      INTEGER NOT NULL DEFAULT 0,
    hidden      INTEGER NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ann_page ON annotations(page);
CREATE INDEX IF NOT EXISTS idx_ann_seq  ON annotations(seq_group, seq_index);

CREATE TABLE IF NOT EXISTS op_log (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           REAL NOT NULL,
    action       TEXT NOT NULL,
    target       TEXT NOT NULL DEFAULT '',
    before_state TEXT,
    after_state  TEXT,
    note         TEXT
);

CREATE INDEX IF NOT EXISTS idx_log_ts ON op_log(ts);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

#: 按结构版本追加的补丁语句。v1 就是上面的 _DDL，之后每版只写增量。
#: v2：区域识别结果（第 Phase 10 章）——每页一条，存收拾干净的管线号。
_MIGRATIONS: dict[int, list[str]] = {
    2: [
        """
        CREATE TABLE IF NOT EXISTS scan_results (
            page       INTEGER PRIMARY KEY,
            raw        TEXT NOT NULL DEFAULT '',
            tag        TEXT NOT NULL DEFAULT '',
            source     TEXT NOT NULL DEFAULT '',
            fixed      INTEGER NOT NULL DEFAULT 0,
            updated_at REAL NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_scan_tag ON scan_results(tag)",
    ],
    # v3：管线号目录（F01）——识别结果要能标"已确认/待校对"，还要能写备注。
    # 直接长在 scan_results 上，不另建一张表：同一个页面只该有一个管线号。
    3: [
        "ALTER TABLE scan_results ADD COLUMN confirmed INTEGER NOT NULL DEFAULT 1",
        "ALTER TABLE scan_results ADD COLUMN note TEXT NOT NULL DEFAULT ''",
    ],
    # v4：数据匹配标注——一次导入 = 一个版本；每条自动标注都能追到
    # "哪个文件、哪一版、哪条记录、哪个字段、什么时候生成的"（用户第 5 条要求）。
    4: [
        """
        CREATE TABLE IF NOT EXISTS data_imports (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            imported_at     REAL NOT NULL,
            path            TEXT NOT NULL DEFAULT '',
            fingerprint     TEXT NOT NULL DEFAULT '',
            sheet           TEXT NOT NULL DEFAULT '',
            row_count       INTEGER NOT NULL DEFAULT 0,
            key_column      TEXT NOT NULL DEFAULT '',
            display_columns TEXT NOT NULL DEFAULT '[]',
            template        TEXT NOT NULL DEFAULT '',
            note            TEXT NOT NULL DEFAULT ''
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS match_annotations (
            annotation_id TEXT PRIMARY KEY,
            import_id     INTEGER,
            page          INTEGER NOT NULL,
            raw           TEXT NOT NULL DEFAULT '',
            tag           TEXT NOT NULL DEFAULT '',
            status        TEXT NOT NULL DEFAULT 'auto',
            record_json   TEXT NOT NULL DEFAULT '{}',
            content       TEXT NOT NULL DEFAULT '',
            note          TEXT NOT NULL DEFAULT '',
            created_at    REAL NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_match_page ON match_annotations(page)",
        "CREATE INDEX IF NOT EXISTS idx_match_tag ON match_annotations(tag)",
    ],
}


def read_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    if row is None:
        return 0
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return 0


def migrate(conn: sqlite3.Connection) -> int:
    """建表 / 升级到当前结构版本，返回最终版本号。

    注意：迁移语句是**每次都跑一遍**的，不是"只在版本号落后时跑"。

    原因是一个真实踩过的坑：v1 时期所有表都在 `_DDL` 里，新建库时写个版本号
    就完事；v2 起新增的表写在 `_MIGRATIONS` 里，新建库如果直接"建 v1 表 + 写
    版本号 2"就跳过了迁移，`scan_results` 表根本没建出来，一写识别结果就
    `no such table`。所以这里改成：所有迁移语句无条件执行，建表用
    `IF NOT EXISTS`、加字段撞了就当已经有了直接跳过 —— 换句话说，打开项目
    时会顺手把缺的表/字段补齐，旧库也自愈。
    """
    with conn:
        conn.executescript(_DDL)
        for statements in _MIGRATIONS.values():
            for statement in statements:
                try:
                    conn.execute(statement)
                except sqlite3.OperationalError:
                    # 表/字段已存在（重复执行，或旧库被手工改过）→ 跳过，不阻断打开
                    pass
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
    return SCHEMA_VERSION
