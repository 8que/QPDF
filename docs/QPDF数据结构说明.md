# QPDF 数据结构说明（以当前代码为准）

> 对应用版本：QPDF v2.0.2 / `SCHEMA_VERSION = 4`
> 本文所有表结构、字段、样例都来自代码与实际生成的示范库（`tests/out/demo.qpdf`，用 `tests/_make_demo_db.py` 生成），
> 不是设计意图。凡「没有做」的地方都单独标注。

---

## 0. 三句话总结

1. `.qpdf` 是**一个 SQLite 数据库文件**（WAL 模式），里面没有任何 PDF 字节；它只记「哪一页、什么位置、写了什么字、什么样式」。
2. 标注**不写进 PDF 原生注释**，而是 `annotations` 表里的自定义行；只有在「导出 PDF」时才转成 MuPDF FreeText 注释（或扁平化成正文文字）。
3. 数字标注**不是普通文字**：`kind='number'`，真正决定显示值的是 `seq_group + seq_index`（规则存在 `meta.sequences`），`content` 只是一次快照。

---

## 1. 数据库文件里有什么

`database/schema.py`（147 行）定义全部结构：

| 表 | 作用 | 关键点 |
|---|---|---|
| `meta(key, value)` | 项目级键值对（TEXT/TEXT） | **所有项目设置、页尺寸、编号规则、书签都在这里** |
| `annotations` | 标注本体 | 一行一条；文字和数字共用此表 |
| `op_log` | 操作日志 | add / remove / update / clear，JSON 前后快照 |
| `settings` | 历史遗留表 | **空表，v1 之后就没人写**（项目设置全部走 `meta`） |
| `scan_results` | 每页识别出的管线号 | `page` 是主键 → **一页只能存一个编号** |
| `data_imports` | 导入过的 Excel/CSV 记录 | 用于追溯「这条标注来自哪张表、哪个工作表」 |
| `match_annotations` | 数据匹配标注的追溯行 | 与 `annotations.id` 一一对应 |

索引：`idx_ann_page(page)`、`idx_ann_seq(seq_group, seq_index)`、`idx_log_ts(ts)`、`idx_scan_tag(tag)`、`idx_match_page(page)`、`idx_match_tag(tag)`。

**没有任何外键约束**，也没有 `UNIQUE(seq_group, seq_index)`。`PRAGMA foreign_keys=ON` 只是打开开关，表上并无 FOREIGN KEY。

### 1.1 meta 表所有键（真实 dump）

```
app                 qpdf-project            ← 打开时校验，不对就报「这不是 QPDF 项目文件」
app_version         2.0.2                   ← 写入它的 QPDF 版本
schema_version      4
created_at          1794405595.623001
saved_at            1794405595.629825
source_path         C:\...\示范图纸.pdf      ← 原始 PDF 的绝对路径
source_size         958299
source_mtime        1793712677.503040
source_fingerprint  b5ea7ebb4b1579d9155fbd276e50be18787eed90   ← 大小+头尾各 64KB 的 sha1
page_count          30
page_sizes          [[2384.0,1684.0], ...]  ← 每页宽高（PDF 点），30 项
sequences           {"current":"s1","serial":1,
                     "sequences":[{"group":"s1","start":1,"step":1,"label":"第 1 组"}]}
boards              {"current":"b1","serial":1,"boards":[...]} ← 工作台
bookmarks           [{"title":"50-CWR-E120701-M1B","page":0},
                     {"title":"100-CIS-E6102B01-MIB","page":2}]  ← 0 基页号
scan_region / annot_region / magnifier_region / ocr_rules        ← 只有用过才存在
```

`source_fingerprint` + `source_size/mtime` 用来在打开项目时判断「源 PDF 换过了没有」（`ProjectManager.check_correspondence()` → 弹提示）。

---

## 2. annotations：标注本体

```sql
CREATE TABLE annotations (
    id         TEXT PRIMARY KEY,      -- uuid4().hex[:12]
    kind       TEXT NOT NULL,         -- 只有 'text' / 'number' 两种
    page       INTEGER NOT NULL,      -- 0 基页号
    x          REAL NOT NULL,         -- PDF 页面坐标（点），不是屏幕像素
    y          REAL NOT NULL,
    content    TEXT NOT NULL DEFAULT '',
    style      TEXT NOT NULL DEFAULT '{}',  -- JSON 字符串
    seq_group  TEXT,                  -- 数字专用：序列名（'s1'…）
    seq_index  INTEGER,               -- 数字专用：第几号（0 基）
    locked     INTEGER NOT NULL DEFAULT 0,
    hidden     INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
)
```

- 坐标 `x/y` 一律是 **PDF 页面坐标**（`page.rect` 那套）；旋转页在写入 PDF 时才用 `derotation_matrix` 换算。
- `style` 的完整形状：

```json
{"font_family":"Microsoft YaHei UI","font_size":16.0,"color":"#d0342c",
 "bold":true,"italic":false,"opacity":100,"align":"center"}
```

- 没有 `category`、没有 `arrow/line/box` 等类型、没有 z 序、没有旋转角字段。

`database/repository.py`：
- 读：`ProjectRepository.load_annotations()`（显式列名 SELECT，加列不会打坏旧代码）
- 写：`write_changes(upserts, deletes, sequences, boards, log_rows, saved_at)` 单事务；标注用 `INSERT OR REPLACE`

---

## 3. 数字标注到底存了什么（真实数据）

`core/sequence.py`：一组序列 = `{group, start, step, label}`，显示值 = `start + step * index`。
数据库里只存 `seq_group` + `seq_index`；`content` 是**生成那一刻的文本快照**。

示范库里 5 条数字（`tests/out/_demo_dump.txt` 原样）：

```
id=525d7842de17 page=0 x=300.0 y=120.0 group=s1 index=0 content='1'   font_size=16 bold
id=05e8aba704d6 page=0 x=330.0 y=165.0 group=s1 index=1 content='2'   font_size=12
id=64db85939c0c page=0 x=300.0 y=180.0 group=s1 index=3 content='3'   font_size=16 bold  ← 插队后 content 没改
id=93c5a66b3fc5 page=0 x=360.0 y=210.0 group=s1 index=7 content='8'   font_size=12
id=c01e845f699a page=1 x=520.0 y=300.0 group=s1 index=1 content='2'   font_size=16 bold  ← 与上一页 index=1 重复
```

**两个必须知道的事实：**

1. **`content` 不可信**。`index=3` 那条 display 是 `1+1×3=4`，但 `content` 还是 `'3'`（插队时只改了 `seq_index`，没重写文本）。显示值一律用 `meta.sequences` 现算：
   ```python
   seq = {
       s["group"]: s for s in json.loads(meta["sequences"])["sequences"]
   }[row["seq_group"]]
   value = seq["start"] + seq["step"] * row["seq_index"]
   ```
2. **`(seq_group, seq_index)` 不唯一**。插队默认 `scope=SCOPE_PAGE`，只挪同一页上的号，别的页同序号不动；「覆盖」也是保留原序号只挪位置，所以会造出跨页重复。做外部读取时不要用 `(group,index)` 当唯一键。

### 3.1 自动编号 / 手动编号 / 覆盖 / 插队分别怎么落库

| 操作 | 代码入口 | 落库结果 |
|---|---|---|
| 自动编号（点一下） | `SequenceManager.make_number(page,x,y,style)` → `PdfViewer.place_number_at()` → `AddAnnotationCommand` | 新增一行，`seq_index = 组内现有最大序号+1`（`next_index()` 从 store 现算，删号/撤销后会自动回退，不跳号），`content` 写当时文本 |
| 指定编号 | `SequenceManager.make_number_at(page,x,y,index,style)` | 同上，但 `seq_index` 由用户给（可以是 7，也可以跳号） |
| 覆盖（F3） | `PdfViewer.overwrite_number(ann_id,page,x,y)` | **不新增、不删除**：把那一行的 `page/x/y` 改掉，`id`、`seq_index`、`style` 全部保留（撤销就是挪回去） |
| 插队（F4） | `SequenceManager.plan_insert(at_index, scope, page/page_from/page_to, group)` → `BatchCommand.insert_number(store, new_ann, shifts, "插队标注")` | 一条 `BatchCommand` 里先 `n` 条 update（受影响的行 `seq_index += 1`），再加 1 条新行；单事务、可整体撤销 |

`op_log` 里能看到整个过程（示范库 8 行），例如覆盖是 `action='update'`、插队是若干 `update` + 一条 `add`。

---

## 4. 书签怎么存、怎么和页面关联

**没有 bookmarks 表**，书签是 `meta` 表里的一个 JSON 键：

```json
"bookmarks": [{"title": "50-CWR-E120701-M1B", "page": 0},
              {"title": "100-CIS-E6102B01-MIB", "page": 2}]
```

- `page` 是 **0 基**，与 `annotations.page`、`scan_results.page` 一致。
- 写入：`ProjectManager.set_setting("bookmarks", [...])`（内部 `repo.set_json` → `meta`）；
- 读回：`ui/main_window.py` 的 `_stored_bookmarks()`；导出时 `_export_toc()` 优先用这份，没有就用 `scan_results` 现生成。
- 转成 PDF 原生目录：`core/bookmarks.py` 的 `to_pymupdf_toc(marks) -> [[1, title, page + 1]]`（**这里 +1 变回 PDF 的 1 基**），
  导出时 `core/exporter.py` 的 `doc.set_toc(plan.toc)`，写后校验条数。
- 读原生目录：`PdfEngine.outline()` → `doc.get_toc(simple=True)`；`core/toc.py` 的 `read_toc(doc)`（也吃路径字符串）、`outline_rows(doc)` 转成 `{page, raw, tag, source:"bookmark"}`。
- 「导入目录书签」按钮不写 `bookmarks`，而是把每一层标题写进 `scan_results`（`source='outline'`），让目录树能显示。

一条管线号对多页：**结构支持**（`scan_results` 主键是页，`PipeIndex` 按 tag 聚合 `pages`），
但导出书签时 `build_bookmarks(..., merge_consecutive=True)` 会把「连续同名的多页」只留第一条。

---

## 5. 数据匹配标注（v4 新增）

- `data_imports`：`id, imported_at, path, fingerprint, sheet, row_count, key_column, display_columns(JSON 数组), template, note`
- `match_annotations`：`annotation_id(主键，对应 annotations.id), import_id, page, raw, tag, status('auto'|…), record_json(整行记录 JSON), content, note, created_at`
- 标注被删时 `ProjectManager._on_removed` 会顺手删掉对应追溯行；清空标注时整表清空；打开项目时 `prune_match_annotations` 清孤儿行。

---

## 6. 逐条回答 17 个问题

**一、项目与数据库结构**

1. **数据库结构**：SQLite 3，7 张表（见第 1 节），`PRAGMA journal_mode=WAL`、`synchronous=NORMAL`、`foreign_keys=ON`。无外键、无唯一约束（除主键）。
2. **标注存在哪**：`annotations` 表；只有导出时才生成 PDF 原生注释（`_add_freetext` → `add_freetext_annot`，或 `_flatten` → `insert_text`）。**不是两者都有**，.qpdf 里没有 PDF 注释。
3. **字段够不够**：页面 `page`、文字 `content`、颜色在 `style->>'$.color'`、坐标 `x/y`、类型 `kind`（text/number）、锁定/隐藏、时间戳都有。颜色是 JSON 内部字段，不是独立列。
4. **分类/自定义属性**：**没有**。只有 `kind`（text/number）、`seq_group`、`locked/hidden`。没有箭头/线/框；想加分类要么加列（见 Q15/Q16），要么另开 side 表。

**二、数字标注**

5. **四种操作怎么存**：见第 3.1 节表格。自动/手动是新增行；覆盖是改 `page/x/y`；插队是批量 `seq_index+1` 加一行，整体一次事务。
6. **有独立编号字段吗**：有，`seq_group` + `seq_index`（不是把数字当普通文字）。但 `content` 同时存了一份文本快照——它只是快照，不参与计算。
7. **能不能直接查某项目全部数字标注和页**：能：
   ```sql
   SELECT id, page, x, y, seq_group, seq_index, content
   FROM annotations WHERE kind = 'number' ORDER BY page, seq_group, seq_index;
   ```
   但「显示的数字」要再算：`start + step*seq_index`（规则在 `meta.sequences`）。
8. **颜色有独立字段吗**：没有，颜色在 `style` JSON 里。要区分红色数字和别的红色注释：`kind='number' AND seq_group IS NOT NULL` 就够；再精确可以 `json_extract(style,'$.color')='#d0342c'`。

**三、PDF 书签**

9. **怎么读写**：读 `doc.get_toc(simple=True)`（`core/pdf_engine.py:94`、`core/toc.py:read_toc`）；存 `meta.bookmarks`（JSON）；导出 `doc.set_toc([[1,title,page+1]])`。
10. **能否按名称拿页面**：能，遍历 `meta.bookmarks` 比对 `title`（完全相等；没有模糊匹配）。`PipeIndex.pages_for(tag)` 也能按编号拿页（来自 `scan_results`）。
11. **一管线号多页**：结构支持（`scan_results` 按页存、`PipeIndex` 聚合多页）；但导出书签时会合并连续同名页，只保留第一条。如果 QReport 需要「一个号对应的所有页」，应查 `scan_results`，不要查 `bookmarks`。

**四、跨项目读取（QReport）**

12. **能否只读访问**：能。它是标准 SQLite。**不要用 `ProjectRepository.open()`**——它会 `migrate()`（写表、写 `schema_version`、开 WAL），对只读场景不合适。推荐：
    ```python
    import sqlite3
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    # 如果 mode=ro 打不开（-shm 不在、目录不可写），改用 mode=ro&immutable=1（仅当确定 QPDF 没在写）
    ```
13. **可复用模块**：`database/repository.py` 的读方法（`load_annotations/load_scan_results/load_match_annotations/get_json`）、`core/pipe_index.PipeIndex`、`core/bookmarks`、`core/toc`。但见上条：只读场景别调用 `open()`。
14. **QPDF 正在运行时能否安全读**：WAL 模式下**可以**（读者不阻塞写者，写者不阻塞读者），但：
   - 读到的只是一个快照，可能落后 1.5 秒（自动保存防抖）；
   - `-wal` / `-shm` 必须能被 SQLite 使用（同机、目录可写）；网络盘/只读介质会失败；
   - QPDF 关闭时会 `checkpoint(TRUNCATE)` 把 WAL 并回主库；
   - QPDF 还有个 `.keep` 旁文件（手动保存时的基线），**不要**当数据库读。

**五、扩展**

15. **加「焊口编号」分类要不要改结构**：`kind` 是 TEXT，理论上直接写 `kind='weld_number'` 不需要 DDL；但现有代码只认 text/number（图标、编辑、导出都会走错分支），所以**实际要改代码**。建议加一个 `category` 列（v5 迁移 `ALTER TABLE annotations ADD COLUMN category TEXT NOT NULL DEFAULT ''`）。
16. **能否给标注加 `annotation_category` 而不影响现有**：可以，两条路：
    - 加列（推荐，查询最方便）：`ALTER TABLE ... ADD COLUMN category TEXT NOT NULL DEFAULT ''`；旧标注默认空串。
    - side 表：`annotation_attrs(annotation_id TEXT, key TEXT, value TEXT, PRIMARY KEY(annotation_id,key))`，完全不动 `annotations`。
    旧版本 QPDF 打开新库：读取用显式列名 → 多出的列被忽略；但旧版 `migrate()` 会把 `schema_version` 改回 4，最好版本检查里加「高于本程序就只读」。
17. **按管线号筛指定类别数字标注、算最大编号**：
    ```sql
    -- 1) 这根管出现在哪些页
    SELECT page FROM scan_results WHERE tag = '50-CWR-E120701-M1B';

    -- 2) 这些页上指定类别的数字标注（category 为将来新增列，现在还不存在）
    SELECT a.id, a.page, a.seq_group, a.seq_index
    FROM annotations a
    WHERE a.kind = 'number'
      AND a.category = 'weld_number'
      AND a.page IN (SELECT page FROM scan_results WHERE tag = '50-CWR-E120701-M1B');
    ```
    实际算最大编号在 Python 里做：读 `meta.sequences`，对每行 `start + step*index` 取最大；**不要用 `MAX(seq_index)` 当最大编号**（有重复序号、有跳号、有自定义 `start/step`）。

---

## 7. 明确「没有实现」的清单

- 没有 bookmarks / favorites 表（书签在 `meta` JSON；收藏功能未做）。
- 没有箭头、线段、矩形、云线等标注类型（只有 text / number）。
- 没有标注分类字段（`category` 不存在）。
- `settings` 表是死表（0 行），所有设置走 `meta`。
- 没有外键、没有 `(seq_group, seq_index)` 唯一约束、没有 z 序/旋转。
- `annotations.content` 对数字只是快照，插队后可能与实际显示不一致。
- 一个项目文件对应一个 PDF；`.qpdf` 内不存 PDF 字节，只有 `source_path` + 指纹。

---

## 8. 上手代码路径

| 想做什么 | 看这里 |
|---|---|
| 表结构 / 迁移 | `database/schema.py`（`_DDL`、`_MIGRATIONS`、`migrate`） |
| 读写 SQL / 事务 | `database/repository.py`（`ProjectRepository`） |
| 标注对象与样式 | `core/annotation.py`（`Annotation`、`AnnotationStyle`） |
| 编号规则 | `core/sequence.py`（`NumberSequence`、`plan_insert`） |
| 撤销/插队事务 | `core/history.py`（`BatchCommand.insert_number`） |
| 项目开合 / 自动保存 | `core/project_manager.py` |
| 导出 PDF（FreeText / 扁平化 / 书签） | `core/exporter.py`（`build_plan`、`_apply`、`set_toc`） |
| 书签 ↔ 页面 | `core/bookmarks.py`、`core/toc.py` |
| 管线号聚合 | `core/pipe_index.py` |

示范库与完整 dump：`tests/out/demo.qpdf`、`tests/out/_demo_dump.txt`（可用 DB Browser for SQLite 直接打开看）。
