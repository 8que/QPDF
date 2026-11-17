"""项目文件管理（第九章）。

文件结构（第 9.2 节，严格照做）:

    工程图纸/
    ├── 原始图纸.pdf        ← 用户的原件，一个字节都不动
    ├── 原始图纸.qpdf       ← 本模块管的 SQLite 项目文件
    ├── 输出/               ← Phase 7 导出到 已标注图纸.pdf
    └── 备份/               ← 自动备份，保留最近几份

三条硬规则：

1. **原 PDF 不进项目文件**（9.3）。``.qpdf`` 只有标注和"指过去"的信息，
   所以项目文件是 KB 级的，2700 页 1.3 GB 的图纸也不会多占一份。
2. **自动保存只写数据库**（9.5）。点一次数字只改一行 SQLite 记录，
   绝不重新生成 PDF——这正是 WPS 卡死的根因，绝对不能重蹈。
3. **每次落盘是一个事务**。崩溃后重新打开，看到的就是最后一次成功提交的状态（16 章）。
   采样用 WAL：写起来不用等磁盘，崩溃时 SQLite 自己回放。
"""

from __future__ import annotations

import json
import os
import shutil
import time

from PySide6.QtCore import QObject, QTimer, Signal

from core.annotation import Annotation
from database.repository import ProjectError, ProjectRepository, fingerprint_file

#: 界面停顿多久之后自动落盘。第 9.5 节"定期提交"，具体间隔待实测确定，
#: 1.5 秒是"连续点数字时不会每条都写、停手就立刻落盘"的折中。
AUTOSAVE_DELAY_MS = 1500

#: 备份保留份数
BACKUP_KEEP = 5

#: 定时备份间隔（毫秒）。第 16 章要求"自动备份"，但用户明确讨厌"没事往磁盘写"，
#: 10 分钟一次、只在有未落盘改动时写，是折中。
AUTO_BACKUP_INTERVAL_MS = 10 * 60 * 1000

#: 项目文件里标记"这次会话没正常收尾"的键名
UNCLEAN_KEY = "unclean_exit"

#: 写进项目文件里的版本号（换版本时只改这里和 main.py 的那一处）
APP_VERSION = "2.5.3"

OUTPUT_DIR = "输出"
BACKUP_DIR = "备份"


def default_project_path(pdf_path: str) -> str:
    """旧约定：同目录、同名、换后缀（第 9.2 节）。

    现在默认不再往 PDF 旁边写，只用来**找老项目**——用户在旧版本里
    保存过的 ``xxx.qpdf`` 还在原地，打开时得认出来。
    """
    stem = os.path.splitext(os.path.abspath(pdf_path))[0]
    return stem + ".qpdf"


def documents_dir() -> str:
    """用户的「文档」目录；取不到就退回主目录。"""
    home = os.path.expanduser("~")
    for name in ("Documents", "文档"):
        candidate = os.path.join(home, name)
        if os.path.isdir(candidate):
            return candidate
    return home


def default_project_dir() -> str:
    """默认的项目库：``文档\\雀雀PDF\\项目``。

    不放 PDF 旁边的原因：图纸经常从桌面、U 盘、共享盘直接打开，
    往那儿撒 ``.qpdf`` 既乱又容易跟别人的文件混在一起。

    环境变量 ``QPDF_PROJECT_DIR`` 可以盖掉它——测试用来把项目库放在自己的
    临时目录里，免得反复跑互相污染。
    """
    override = os.environ.get("QPDF_PROJECT_DIR", "").strip()
    if override:
        return override
    return os.path.join(documents_dir(), "雀雀PDF", "项目")


def library_project_path(project_dir: str, pdf_path: str, fingerprint: str) -> str:
    """项目库里的文件名：``图纸名-指纹前8位.qpdf``。

    带上指纹是为了**同名不同版**的图纸不会撞车（改过一版再打开，
    不会把上一版的标注盖掉）。
    """
    stem = os.path.splitext(os.path.basename(pdf_path))[0] or "图纸"
    tail = (fingerprint or "")[:8] or "00000000"
    return os.path.join(project_dir, f"{stem}-{tail}.qpdf")


def _free_project_path(path: str) -> str:
    """``a.qpdf`` 已存在时，依次试 ``a (2).qpdf``、``a (3).qpdf``…"""
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    for n in range(2, 100):
        candidate = f"{stem} ({n}){ext}"
        if not os.path.exists(candidate):
            return candidate
    return f"{stem} ({int(time.time())}){ext}"


def _json_or_none(value) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)


class ProjectManager(QObject):
    """把 ``AnnotationStore`` + ``SequenceManager`` 和磁盘上的 ``.qpdf`` 接起来。"""

    #: 脏标记 / 保存时间变化（界面刷新标题和状态栏）
    changed = Signal()
    #: 项目打开 / 关闭
    opened = Signal(str)
    closed = Signal()
    #: 一次性提示（写状态栏）
    notice = Signal(str)
    #: 源 PDF 和项目记录对不上
    mismatch = Signal(object)
    #: 定时备份写了一份（参数是备份文件路径）
    backup_done = Signal(str)
    #: 上次会话没有正常退出（参数是项目文件路径）
    crash_suspect = Signal(str)

    def __init__(self, store, sequence, parent=None) -> None:
        super().__init__(parent)
        self._store = store
        self._sequence = sequence
        self._repo: ProjectRepository | None = None
        self._doc = None
        self._source: dict = {}
        self._default_path = ""
        #: 找到的老式"PDF 旁边"项目路径（空 = 没有）
        self._legacy_path = ""
        #: 最近一次指纹/页数对不上的详情（界面弹选择框时用）
        self._last_mismatch: dict = {}

        # 待落盘的增量
        self._upserts: dict[str, Annotation] = {}
        self._deletes: set[str] = set()
        self._log_rows: list[dict] = []
        self._seq_dirty = False
        self._boards_dirty = False
        self._boards = None
        self._dirty = False
        #: 自上次「手动保存」以来有没有改过（自动保存不算，退出时要问）
        self._unsaved = False

        # 已落盘状态的镜像，用来算操作日志的"操作前状态"
        self._known: dict[str, Annotation] = {}
        self._loading = False
        # 设置面板可以改备份位置；空 = 跟项目目录走
        self._backup_override = ""
        # 设置面板可以改项目库位置；空 = 文档\雀雀PDF\项目
        self._project_override = ""
        self._last_saved_at = 0.0
        #: 上一次 `ensure_project()` 失败的原因（给界面弹提示用，别让保存失败变哑巴）
        self._last_error = ""
        #: 上一次 attach() 打不开的项目文件路径（损坏/不是 QPDF 时给「查看项目位置」用）
        self._last_error_path = ""
        #: 上次备份时的落盘时间戳：定时器到点先看它，免得"没改东西也一直写备份"
        self._backup_stamp = 0.0

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(AUTOSAVE_DELAY_MS)
        self._timer.timeout.connect(self._autosave)

        #: 定时备份（第 16 章）：只在这个项目有"还没手动保存"的改动时才写，
        #: 写完立刻把备份数裁到 BACKUP_KEEP 份
        self._backup_timer = QTimer(self)
        self._backup_timer.setInterval(AUTO_BACKUP_INTERVAL_MS)
        self._backup_timer.timeout.connect(self._auto_backup)

        store.added.connect(self._on_added)
        store.removed.connect(self._on_removed)
        store.changed.connect(self._on_changed)
        store.cleared.connect(self._on_cleared)
        sequence.changed.connect(self._on_sequence_changed)

    # -- 状态 -------------------------------------------------------------

    @property
    def path(self) -> str:
        """当前项目文件路径；还没建库时给的是"将会建在哪"。"""
        if self._repo is not None:
            return self._repo.path
        return self._default_path

    @property
    def exists(self) -> bool:
        return self._repo is not None and self._repo.exists()

    @property
    def is_open(self) -> bool:
        return self._repo is not None and self._repo.is_open

    @property
    def is_dirty(self) -> bool:
        """有没有「还没手动保存」的修改（自动保存只算存档，不算保存）。"""
        return self._unsaved

    @property
    def pending_count(self) -> int:
        return len(self._upserts) + len(self._deletes)

    @property
    def last_saved_at(self) -> float:
        return self._last_saved_at

    def project_path_for(self, pdf_path: str) -> str:
        return default_project_path(pdf_path)

    @property
    def last_mismatch(self) -> dict:
        """最近一次 attach() 对不上时的详情；对得上时为空。"""
        return dict(self._last_mismatch)

    @property
    def last_error(self) -> str:
        """最近一次建库 / 继续加载 / 打开项目失败的原因。"""
        return self._last_error

    @property
    def last_error_path(self) -> str:
        """最近一次打不开的项目文件路径（损坏提示里用来「查看项目位置」）。"""
        return self._last_error_path

    # -- 挂接文档 ---------------------------------------------------------

    def attach(self, doc, project_path: str = "") -> str:
        """打开 PDF 后调用。

        ``project_path`` 只在「从 .qpdf 打开」时给：用户可能把项目文件放在
        别处（甚至从另一台电脑拷过来），不能只按 PDF 同目录同名去找。

        返回 ``loaded`` / ``none`` / ``mismatch`` / ``missing`` / ``broken``。
        ``broken`` = 同名 .qpdf 存在但读不了（损坏、不是 QPDF、被占用等）：
        绝不能把它当“没有项目”去新建覆盖，界面必须提示并保护原文件。
        """
        self.detach(save=True)
        self._last_mismatch = {}
        self._last_error = ""
        self._last_error_path = ""
        self._doc = doc
        self._source = self._make_source(doc)
        forced = os.path.abspath(project_path) if project_path else ""
        if forced and os.path.isfile(forced):
            self._default_path = forced
        else:
            self._default_path = self._resolve_project_path(doc)

        if not os.path.isfile(self._default_path):
            self.changed.emit()
            return "none"

        self._repo = ProjectRepository(self._default_path)
        try:
            self._repo.open()
        except ProjectError as exc:
            self._last_error = str(exc)
            self._last_error_path = self._default_path
            self._repo = None
            self.notice.emit(str(exc))
            self.changed.emit()
            # 文件存在但打不开：不能当成“没有项目”。旧逻辑返回 missing，界面会以为
            # 可以新建，用户一保存就多出一个 (2).qpdf；更糟的是用户以为原项目坏了/丢了。
            return "broken"

        stored = self._repo.source_info()
        actual = self._source
        same_content = (
            stored.get("fingerprint") == actual.get("fingerprint")
            and int(stored.get("size", -1)) == int(actual.get("size", -2))
        )
        if not same_content:
            info = {"stored": stored, "actual": actual, "path": self._default_path}
            self._last_mismatch = dict(info)
            self._repo.close()
            self._repo = None
            self.changed.emit()
            self.mismatch.emit(info)
            return "mismatch"

        if stored.get("path") and os.path.abspath(stored["path"]) != os.path.abspath(doc.path):
            # 内容一致、只是被挪了位置（或者换了盘符）：把新路径记下来就行
            self._repo.set_meta("source_path", os.path.abspath(doc.path))
            self.notice.emit("源 PDF 位置变化，已更新项目记录")

        page_sizes = self._repo.page_sizes()
        if len(page_sizes) != doc.page_count:
            # 页数变了说明不是同一份图纸，宁可不自动加载也不要把标注画到错的页上
            info = {"stored": stored, "actual": actual, "path": self._default_path}
            self._last_mismatch = dict(info)
            self._repo.close()
            self._repo = None
            self.changed.emit()
            self.mismatch.emit(info)
            return "mismatch"

        crashed = self._repo.get_meta(UNCLEAN_KEY, "") == "1"
        self._load_into_memory()
        self._last_saved_at = self._repo.saved_at()
        self._mark_session_started()
        self._backup_timer.start()
        self.opened.emit(self.path)
        self.changed.emit()
        if crashed:
            # 上次没走正常关闭（崩溃 / 任务管理器结束进程）：数据一般还在最后一次
            # 自动保存里，但备份里可能有更完整的版本，交给界面提示用户。
            self.crash_suspect.emit(self.path)
        return "loaded"

    def load_mismatched(self, info: dict | None = None, remember: bool = False) -> str:
        """用户明确选择「仍然继续」后，加载那个对不上的项目。

        安全前提：**动项目文件之前先复制一份带时间戳的备份**。备份失败就拒绝
        继续，宁可这次不加载，也不让旧标注失去退路。

        ``remember=True`` 时，在备份并成功载入后，把当前 PDF 的路径、大小、
        mtime、指纹写回项目，下次用同一份 PDF 打开就不再提示。只有页数一致的
        情况才允许记住，避免把“页数都变了”的错配永久静默掉。
        """
        details = dict(info or self._last_mismatch)
        path = details.get("path") or self._default_path
        if self.is_open:
            return path
        if not path or not os.path.isfile(path):
            self._last_error = f"项目文件不在了：{path}"
            self.notice.emit(self._last_error)
            return ""

        folder = self.backup_dir()
        stem = os.path.splitext(os.path.basename(path))[0]
        stamp = time.strftime("%Y%m%d_%H%M%S")
        backup = os.path.join(folder, f"{stem}_指纹不一致_{stamp}.qpdf")
        backup = _free_project_path(backup)
        try:
            os.makedirs(folder, exist_ok=True)
            shutil.copy2(path, backup)
        except OSError as exc:
            self._last_error = f"备份对不上的项目失败，已取消加载：{exc}"
            self.notice.emit(self._last_error)
            return ""

        repo = ProjectRepository(path)
        try:
            repo.open()
            self._repo = repo
            self._load_into_memory()
        except (ProjectError, OSError) as exc:
            try:
                repo.close()
            except Exception:  # noqa: BLE001
                pass
            self._repo = None
            self._last_error = f"继续加载失败：{exc}"
            self.notice.emit(self._last_error)
            return ""

        self._last_saved_at = repo.saved_at()
        self._mark_session_started()
        self._backup_timer.start()
        self._last_mismatch = {}
        stored = details.get("stored") or {}
        actual = details.get("actual") or self._source or {}
        same_pages = int(stored.get("page_count", -1)) == int(actual.get("page_count", -2))
        if remember and same_pages and actual.get("fingerprint"):
            self._repo.set_meta_many(
                {
                    "source_path": actual.get("path", ""),
                    "source_size": str(actual.get("size", 0)),
                    "source_mtime": str(actual.get("mtime", 0.0)),
                    "source_fingerprint": actual.get("fingerprint", ""),
                }
            )
            self.notice.emit("已记住这份 PDF；以后打开不再提示对不上")
        else:
            self.notice.emit(
                "已继续加载对不上的项目；原项目已另存备份："
                f"{os.path.basename(backup)}"
            )
        self.opened.emit(self.path)
        self.changed.emit()
        return backup

    def detach(self, save: bool = True) -> None:
        if self._repo is None:
            self._doc = None
            return
        if save:
            self.close()
        else:
            self._repo.close()
            self._repo = None
            self._doc = None
            self.changed.emit()

    def _make_source(self, doc) -> dict:
        path = os.path.abspath(doc.path)
        try:
            stat = os.stat(path)
            size, mtime = int(stat.st_size), float(stat.st_mtime)
        except OSError:
            size, mtime = 0, 0.0
        return {
            "path": path,
            "size": size,
            "mtime": mtime,
            "fingerprint": fingerprint_file(path) if size else "",
            "page_count": int(doc.page_count),
        }

    # -- 载入 / 建立 ------------------------------------------------------

    def _load_into_memory(self) -> None:
        self._loading = True
        try:
            self._store.clear()
            self._sequence.load_dict(self._repo.get_json("sequences"))
            if self._boards is not None:
                self._boards.load_dict(self._repo.get_json("boards"))
            annotations = self._repo.load_annotations()
            for ann in annotations:
                self._store.add(ann)
            self._known = {ann.id: ann for ann in annotations}
            # 撤销过的自动标注会在追溯表里留下孤儿行，打开项目时顺手清掉
            try:
                self._repo.prune_match_annotations({ann.id for ann in annotations})
            except Exception:  # noqa: BLE001 - 老项目没有这张表
                pass
            self._upserts.clear()
            self._deletes.clear()
            self._log_rows.clear()
            self._seq_dirty = False
            self._boards_dirty = False
            self._dirty = False
            self._unsaved = False
        finally:
            self._loading = False
            self._timer.stop()
        # 打开时先留一份「打开那一刻」的快照：退出选「不保存」就回到这个状态
        self._ensure_keep()

    def _ensure_keep(self) -> None:
        if not self.is_open:
            return
        keep = self._keep_path()
        if keep and not os.path.isfile(keep):
            self._write_keep()

    def ensure_project(self, path: str = "") -> bool:
        """还没有项目文件就建一个（第一次真正产生标注时自动调用）。"""
        self._last_error = ""
        if self.is_open:
            return True
        if self._doc is None:
            self._last_error = "没有打开 PDF，建不了项目文件"
            return False
        target = path or self._default_path
        if not target:
            self._last_error = "项目文件路径是空的（检查一下「项目库」设置）"
            return False
        if not path and os.path.isfile(target):
            # 旁边已经躺着一个 .qpdf，但没打开成功（多半是它属于被改过的另一版 PDF）。
            # 这时候绝不能覆盖它——那会把别人的标注一起冲掉，换个名字另存。
            target = _free_project_path(target)
        parent = os.path.dirname(target)
        if parent:
            try:
                os.makedirs(parent, exist_ok=True)
            except OSError as exc:
                self._last_error = f"建项目文件夹失败：{exc}"
                self.notice.emit(self._last_error)
                return False

        repo = ProjectRepository(target)
        try:
            repo.create(
                self._source,
                [[size.width, size.height] for size in self._doc_page_sizes()],
                app_version=APP_VERSION,
            )
            repo.set_json("sequences", self._sequence.to_dict())
            if self._boards is not None:
                repo.set_json("boards", self._boards.to_dict())
        except (ProjectError, OSError) as exc:
            repo.close()
            self._last_error = f"创建项目文件失败：{exc}"
            self.notice.emit(self._last_error)
            return False

        self._repo = repo
        self._default_path = repo.path
        self._mark_session_started()
        self._backup_timer.start()
        self._make_side_dirs()
        self._known = {ann.id: ann for ann in self._store.all()}
        self._last_saved_at = time.time()
        self.notice.emit(f"已创建项目文件：{os.path.basename(repo.path)}")
        self.opened.emit(self.path)
        self.changed.emit()
        return True

    def _doc_page_sizes(self) -> list:
        doc = self._doc
        sizes = []
        for index in range(doc.page_count):
            size = doc.page_size(index)
            sizes.append(size)
        return sizes

    def _resolve_project_path(self, doc) -> str:
        """决定这份 PDF 的项目文件在哪。

        查找顺序：

        1. **PDF 旁边同名 .qpdf** —— 老版本就是这么存的，认出来继续用，
           不擅自搬家（搬走会让用户以为标注丢了）；
        2. **项目库里同名的** —— 正常情况；
        3. 都没有 → 就用项目库里的路径（第一次标注时才真正创建文件）。
        """
        legacy = default_project_path(doc.path)
        if os.path.isfile(legacy):
            self._legacy_path = legacy
            return legacy
        fingerprint = self._source.get("fingerprint", "")
        library = library_project_path(self.project_dir(), doc.path, fingerprint)
        self._legacy_path = ""
        if os.path.isfile(library):
            return library
        # 名字里带着指纹，图纸被改过就找不到同名文件了。这时候**不能装作没事**：
        # 去项目库里找同名图纸的其它版本，让 attach() 照常比指纹、报"对不上"，
        # 用户才知道"这份图纸以前标过、但已经不是同一份了"。
        older = self._sibling_projects(doc.path)
        if older:
            return older[0]
        return library

    def _sibling_projects(self, pdf_path: str) -> list[str]:
        """项目库里同一个图纸名、别的指纹版本的项目文件（按修改时间新到旧）。"""
        import glob

        stem = os.path.splitext(os.path.basename(pdf_path))[0] or "图纸"
        pattern = os.path.join(self.project_dir(), f"{glob.escape(stem)}-*.qpdf")
        found = [p for p in glob.glob(pattern) if os.path.isfile(p)]
        found.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        return found

    def project_dir(self) -> str:
        """项目库目录：设置里指定了就用指定的。"""
        return (self._project_override or default_project_dir()).strip()

    def set_project_dir(self, path: str) -> None:
        """设置面板改项目库位置时调它（空串 = 回到默认）。"""
        self._project_override = (path or "").strip()

    def _make_side_dirs(self) -> None:
        """第 9.2 节的 输出/ 与 备份/ 目录。"""
        base = os.path.dirname(self.path)
        for name in (OUTPUT_DIR, BACKUP_DIR):
            try:
                os.makedirs(os.path.join(base, name), exist_ok=True)
            except OSError:
                pass

    def output_dir(self) -> str:
        return os.path.join(os.path.dirname(self.path), OUTPUT_DIR) if self.path else ""

    def backup_dir(self) -> str:
        """备份目录：设置里指定了就用指定位置，否则跟项目走（第 9.2 节）。"""
        if self._backup_override:
            return self._backup_override
        return os.path.join(os.path.dirname(self.path), BACKUP_DIR) if self.path else ""

    def set_backup_dir(self, path: str) -> None:
        """设置面板改备份位置时调它（空串 = 回到"跟项目走"）。"""
        self._backup_override = (path or "").strip()

    # -- 变更跟踪 ---------------------------------------------------------

    def _on_added(self, ann: Annotation) -> None:
        if self._loading:
            return
        if not self.is_open:
            # 建库失败（目录不可写、磁盘满）也**不能**把这条标注丢掉：它已经画在
            # 界面上了，必须记进 _upserts 并标脏——否则"界面有、库里没有"，
            # 关窗口也不问一句。等项目库恢复后 flush 时会补写进去。
            self.ensure_project()
        self._upserts[ann.id] = ann
        self._deletes.discard(ann.id)
        self._log_rows.append(
            {
                "ts": time.time(),
                "action": "add",
                "target": ann.id,
                "before": None,
                "after": _json_or_none(ann.to_dict()),
            }
        )
        self._mark_dirty()

    def _on_removed(self, ann_id: str) -> None:
        # 库没开也要记账：删掉的可能是"刚加、还没落盘"的那条，不 pop 会留成孤儿。
        if self._loading:
            return
        before = self._known.get(ann_id) or self._upserts.get(ann_id)
        self._upserts.pop(ann_id, None)
        # 自动标注被删掉（含撤销生成），它的追溯记录也就没意义了
        try:
            self._repo.delete_match_annotations([ann_id])  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass
        if before is not None:
            self._deletes.add(ann_id)
        self._log_rows.append(
            {
                "ts": time.time(),
                "action": "remove",
                "target": ann_id,
                "before": _json_or_none(before.to_dict()) if before else None,
                "after": None,
            }
        )
        self._mark_dirty()

    def _on_changed(self, ann: Annotation) -> None:
        if self._loading:
            return
        before = self._known.get(ann.id)
        self._upserts[ann.id] = ann
        self._deletes.discard(ann.id)
        self._log_rows.append(
            {
                "ts": time.time(),
                "action": "update",
                "target": ann.id,
                "before": _json_or_none(before.to_dict()) if before else None,
                "after": _json_or_none(ann.to_dict()),
            }
        )
        self._mark_dirty()

    def _on_cleared(self) -> None:
        if self._loading:
            return
        for ann_id in list(self._known):
            self._deletes.add(ann_id)
        self._upserts.clear()
        try:
            self._repo.clear_match_annotations()  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass
        self._log_rows.append(
            {"ts": time.time(), "action": "clear", "target": "", "before": None, "after": None}
        )
        self._mark_dirty()

    def _on_sequence_changed(self) -> None:
        # 同上：库没开也要标脏，否则"改了编号起始值/步长"在关闭时不被追问。
        if self._loading:
            return
        self._seq_dirty = True
        self._mark_dirty()

    def _on_boards_changed(self) -> None:
        if self._loading:
            return
        self._boards_dirty = True
        if not self.is_open:
            # 只在工作台上摆了几页、还没标过任何东西时，项目文件也还没有。
            # 这里跟"第一次标注"一样自动建一个，否则拖好的卡片位置会随程序退出丢掉。
            self.ensure_project()
            if not self.is_open:
                return
        self._mark_dirty()

    def bind_boards(self, boards) -> None:
        """工作台的卡片位置也要存进 .qpdf（第 9.3 节的"项目设置"）。"""
        self._boards = boards
        boards.changed.connect(self._on_boards_changed)

    def _mark_dirty(self) -> None:
        if not self._unsaved:
            self._unsaved = True
            self.changed.emit()
        self._dirty = True
        self._timer.start()

    # -- 落盘 -------------------------------------------------------------

    def flush(self, mark_saved: bool = False) -> int:
        """把增量写进数据库（第 9.5 节：只写项目数据，不碰 PDF）。

        默认是「自动存档」：数据落盘，但 ``is_dirty`` 仍然为真——下次开程序
        会问你「要不要保存」。``mark_saved=True`` 只在手动保存时用。
        """
        self._timer.stop()
        if not self.is_open:
            return 0
        if not self._upserts and not self._deletes and not self._seq_dirty and not self._boards_dirty:
            if self._log_rows:
                # 只有日志（比如清空又撤回）也要落，但不动标注表
                self._repo.append_log(self._log_rows)
                self._log_rows.clear()
            self._dirty = False
            if mark_saved:
                self._unsaved = False
            self.changed.emit()
            return 0

        upserts = list(self._upserts.values())
        deletes = sorted(self._deletes)
        sequences = self._sequence.to_dict() if self._seq_dirty else None
        boards = self._boards.to_dict() if (self._boards_dirty and self._boards is not None) else None

        self._repo.write_changes(
            upserts=upserts,
            deletes=deletes,
            sequences=sequences,
            boards=boards,
            log_rows=self._log_rows,
            saved_at=time.time(),
        )

        for ann in upserts:
            self._known[ann.id] = ann
        for ann_id in deletes:
            self._known.pop(ann_id, None)

        self._upserts.clear()
        self._deletes.clear()
        self._log_rows.clear()
        self._seq_dirty = False
        self._boards_dirty = False
        self._dirty = False
        if mark_saved:
            self._unsaved = False
        self._last_saved_at = time.time()
        self.changed.emit()
        return len(upserts) + len(deletes)

    def save(self) -> int:
        """Ctrl+S：手动保存——写库、留一份「上次手动保存」的快照。

        失败一定抛 :class:`ProjectError`（带原因）。**不要**再用 ``return 0``：
        那和"没有改动可写"撞车，界面就分不出「保存成功但没东西可写」和
        「保存失败」——后者曾经表现成"点了保存、窗口照关、改动全丢"。
        """
        if not self.is_open:
            if not self.ensure_project():
                raise ProjectError(self._last_error or "保存失败：项目文件建不起来")
            # 新建的项目还没有任何记录，本次全部写一遍
            for ann in self._store.all():
                self._upserts[ann.id] = ann
            self._seq_dirty = True
            if self._boards is not None:
                self._boards_dirty = True
        try:
            count = self.flush(mark_saved=True)
        except ProjectError:
            raise
        except Exception as exc:  # noqa: BLE001 - 磁盘满/库被占用都要变成能看懂的一句话
            raise ProjectError(f"保存失败：{exc}") from exc
        self._write_keep()
        return count

    def _autosave(self) -> None:
        """定时落盘。失败只提示、不打断用户，但也绝不一声不吭。"""
        try:
            self.flush()
        except Exception as exc:  # noqa: BLE001 - 自动存档失败不该让程序崩
            self.notice.emit(f"自动保存失败：{exc}")

    def _keep_path(self) -> str:
        """「上次手动保存」的那份快照（退出时选"不保存"就还原成它）。"""
        base = self.path
        return base + ".keep" if base else ""

    def _write_keep(self) -> None:
        if not self.is_open:
            return
        keep = self._keep_path()
        if not keep:
            return
        try:
            self._repo.checkpoint()
            shutil.copy2(self.path, keep)
        except OSError as exc:
            self.notice.emit(f"保存快照失败：{exc}")

    def discard_unsaved(self) -> bool:
        """退出时选「不保存」：把项目文件还原成上次手动保存的样子。"""
        if not self.is_open:
            return False
        target = self.path
        keep = self._keep_path()
        self._timer.stop()
        self._backup_timer.stop()
        self._mark_session_closed()
        try:
            self._repo.checkpoint()
        except Exception:  # noqa: BLE001 - 关库本身不该拦住还原
            pass
        self._repo.close()
        self._repo = None
        self._doc = None
        self._upserts.clear()
        self._deletes.clear()
        self._log_rows.clear()
        try:
            if keep and os.path.isfile(keep):
                shutil.copy2(keep, target)
            elif os.path.isfile(target):
                # 一次都没手动保存过 → 这份项目整个丢掉
                os.remove(target)
        except OSError as exc:
            self.notice.emit(f"还原失败：{exc}")
            return False
        self._unsaved = False
        self._dirty = False
        self.closed.emit()
        return True

    def save_as(self, path: str) -> bool:
        """另存为：把当前项目复制到新位置，之后都写新文件。"""
        if not self.is_open:
            return False
        self.flush()
        self._repo.checkpoint()
        try:
            shutil.copy2(self.path, path)
        except OSError as exc:
            self.notice.emit(f"另存失败：{exc}")
            return False
        self._repo.close()
        self._repo = ProjectRepository(path)
        try:
            self._repo.open()
        except ProjectError as exc:
            self._repo = None
            self.notice.emit(str(exc))
            return False
        self._default_path = self._repo.path
        self._make_side_dirs()
        self._ensure_keep()
        self.opened.emit(self.path)
        self.changed.emit()
        return True

    def export_subset(self, target: str, pages: list[int], note: str = "") -> int:
        """把「这几页的东西」另存成一个单独的 .qpdf，返回写了多少条标注。

        给别人的时候不用把整份两千多页的记录都交出去：标注、识别结果、匹配追溯
        只留这几页。**页号保持原样（不重排）**，所以这份子集仍然配着同一份
        PDF 打开——把子集放到项目库里，打开那张图纸就会接上它。
        """
        if not self.is_open or self._repo is None:
            raise ProjectError("还没有项目文件")
        total = self._doc.page_count if self._doc is not None else 0
        keep = {
            int(page)
            for page in pages
            if (total <= 0 or 0 <= int(page) < total)
        }
        if not keep:
            raise ProjectError("没有可导出的页码")

        self.flush()
        source = dict(self._repo.source_info() or {})
        sizes = list(self._repo.get_json("page_sizes", []) or [])
        repo = ProjectRepository(target)
        try:
            repo.create(source, sizes, app_version=APP_VERSION)
            annotations = [ann for ann in self._store.all() if ann.page in keep]
            repo.write_annotations(annotations, [])

            scans = [
                dict(row, page=page)
                for page, row in self._repo.load_scan_results().items()
                if page in keep
            ]
            if scans:
                repo.write_scan_results(scans)
            matches = [
                row
                for row in self._repo.load_match_annotations()
                if int(row.get("page", -1)) in keep
            ]
            if matches:
                repo.write_match_annotations(matches)

            repo.set_json("sequences", self._sequence.to_dict())
            if self._boards is not None:
                repo.set_json("boards", self._boards.to_dict())
            for key in ("scan_region", "annot_region", "magnifier_region", "ocr_rules"):
                value = self._repo.get_json(key, None)
                if value is not None:
                    repo.set_json(key, value)
            repo.set_meta_many(
                {
                    "subset_pages": ",".join(str(page) for page in sorted(keep)),
                    "subset_from": self.path,
                    "subset_note": note,
                }
            )
            repo.mark_saved()
            return len(annotations)
        finally:
            repo.close()

    def backup(self) -> str | None:
        """把当前项目复制一份到 备份/（第 16 章"自动备份"）。"""
        if not self.exists:
            return None
        self.flush()
        self._repo.checkpoint()
        folder = self.backup_dir()
        try:
            os.makedirs(folder, exist_ok=True)
            stem = os.path.splitext(os.path.basename(self.path))[0]
            name = f"{stem}_{time.strftime('%Y%m%d_%H%M%S')}.qpdf"
            target = os.path.join(folder, name)
            # 同一秒内连着备两次不能互相覆盖：名字后面加 (2)、(3)…
            # （还原时会先自动备份一份"还原前"的，正好和刚建的备份落在同一秒，
            #   不加这个后缀会把用户要还原的那份直接盖掉。）
            if os.path.exists(target):
                base, ext = os.path.splitext(target)
                for index in range(2, 100):
                    candidate = f"{base} ({index}){ext}"
                    if not os.path.exists(candidate):
                        target = candidate
                        break
            shutil.copy2(self.path, target)
        except OSError as exc:
            self.notice.emit(f"备份失败：{exc}")
            return None
        self._prune_backups(folder, stem)
        self._backup_stamp = max(self._backup_stamp, self._last_saved_at)
        return target

    # -- 会话标记 / 定时备份 / 备份还原（第 16 章）-------------------------

    def _mark_session_started(self) -> None:
        """在项目文件里打个"这次会话还没正常收尾"的标记。

        正常关闭（`close()` / 退出选"不保存"）会把它清掉；如果下次打开还看到它，
        说明上次是崩溃或被强制结束的——数据一般在最后一次自动保存里，但备份里
        可能有更完整的版本，值得问一句。
        """
        try:
            self._repo.set_meta(UNCLEAN_KEY, "1")
        except Exception:  # noqa: BLE001 - 标记写不进去不该妨碍打开
            pass

    def _mark_session_closed(self) -> None:
        if self._repo is None:
            return
        try:
            self._repo.set_meta(UNCLEAN_KEY, "0")
        except Exception:  # noqa: BLE001
            pass

    def was_unclean_exit(self) -> bool:
        """项目文件里记着"上次没正常收尾"吗（还没打开库时也能问）。"""
        if self.is_open:
            try:
                return self._repo.get_meta(UNCLEAN_KEY, "") == "1"
            except Exception:  # noqa: BLE001
                return False
        return False

    def _auto_backup(self) -> None:
        """定时器到点：只有"自上次备份之后又落过盘"才写新的一份。

        用户明确讨厌没事往磁盘写东西：停在那儿不动，10 分钟也不会多出一份备份。
        """
        if not self.is_open or not self._unsaved:
            return
        if self._last_saved_at and self._last_saved_at <= self._backup_stamp:
            return
        target = self.backup()
        if target:
            self._backup_stamp = self._last_saved_at
            self.backup_done.emit(target)

    def list_backups(self) -> list[dict]:
        """备份目录里属于这个项目的备份，新的在前。"""
        folder = self.backup_dir()
        if not folder or not os.path.isdir(folder):
            return []
        stem = os.path.splitext(os.path.basename(self.path))[0] if self.path else ""
        rows: list[dict] = []
        try:
            names = os.listdir(folder)
        except OSError:
            return []
        for name in names:
            if not name.endswith(".qpdf"):
                continue
            if stem and not name.startswith(stem + "_"):
                continue
            path = os.path.join(folder, name)
            try:
                stat = os.stat(path)
            except OSError:
                continue
            rows.append(
                {
                    "path": path,
                    "name": name,
                    "mtime": float(stat.st_mtime),
                    "size": int(stat.st_size),
                }
            )
        rows.sort(key=lambda row: row["mtime"], reverse=True)
        return rows

    def restore_backup(self, backup_path: str) -> bool:
        """拿一份备份盖掉当前项目文件，然后把标注重新载入。

        **不动原始 PDF**：这里只换 `.qpdf`。盖之前会先 flush 当前改动、留一份
        "恢复前"的备份，免得用户点错了就再也回不去。
        """
        if not self.is_open or self._repo is None:
            raise ProjectError("还没有打开项目文件，没法还原")
        if not backup_path or not os.path.isfile(backup_path):
            raise ProjectError(f"备份文件不在了：{backup_path}")
        target = self.path
        # 先把当前状态落盘并留一份，误点也有退路
        try:
            self.flush()
            self._repo.checkpoint()
            self.backup()
        except Exception:  # noqa: BLE001 - 存不下来也继续还原，用户此刻要的是备份
            pass
        self._repo.close()
        self._repo = None
        try:
            shutil.copy2(backup_path, target)
            repo = ProjectRepository(target)
            repo.open()
        except (OSError, ProjectError) as exc:
            self.notice.emit(f"还原失败：{exc}")
            # 尽量把库重新打开，别让界面停在一个"没有项目"的状态
            try:
                self._repo = ProjectRepository(target)
                self._repo.open()
                self._load_into_memory()
            except ProjectError:
                self._repo = None
            return False
        self._repo = repo
        self._mark_session_started()
        self._backup_timer.start()
        self._load_into_memory()
        self._last_saved_at = self._repo.saved_at()
        # 还原出来的状态就是"上次手动保存"的样子，顺手把 .keep 对齐
        self._write_keep()
        self.opened.emit(self.path)
        self.changed.emit()
        return True

    def _prune_backups(self, folder: str, stem: str) -> None:
        try:
            names = [
                os.path.join(folder, name)
                for name in os.listdir(folder)
                if name.startswith(stem + "_") and name.endswith(".qpdf")
            ]
        except OSError:
            return
        names.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        for old in names[BACKUP_KEEP:]:
            try:
                os.remove(old)
            except OSError:
                pass

    def close(self) -> None:
        if self._repo is None:
            self._doc = None
            return
        self._backup_timer.stop()
        self._mark_session_closed()
        try:
            self.flush()
            self._repo.checkpoint()
            # 26MB 级项目里往往堆着几万条 op_log 和空闲页；关闭时顺手裁掉，
            # 下次打开不必再背着这些历史。只动操作日志，标注/追溯一行不碰。
            try:
                self._repo.compact_if_needed()
            except Exception:  # noqa: BLE001 - 整理失败不能影响正常关闭
                pass
        finally:
            self._repo.close()
            self._repo = None
            self._doc = None
            self._upserts.clear()
            self._deletes.clear()
            self._log_rows.clear()
            self._known.clear()
            self._dirty = False
        self.closed.emit()
        self.changed.emit()

    # -- 查询（测试与"最近日志"用）---------------------------------------

    # -------------------------------------------------------------- 项目设置
    # 第 9.3 節「项目设置」：目前只有识别区域与 OCR 开关，后面加字段就加在这里。

    def get_setting(self, key: str, default=None):
        if not self.is_open:
            return default
        try:
            return self._repo.get_json(key, default)
        except Exception:  # noqa: BLE001
            return default

    def set_setting(self, key: str, value) -> None:
        if not self.is_open:
            return
        try:
            self._repo.set_json(key, value)
            self._mark_dirty()
        except Exception:  # noqa: BLE001
            pass

    def recent_log(self, limit: int = 20) -> list[dict]:
        if not self.is_open:
            return []
        return self._repo.recent_log(limit)

    def log_count(self) -> int:
        if not self.is_open:
            return 0
        return self._repo.log_count()

    # ------------------------------------------------------- 区域识别结果
    def load_scan_rows(self) -> dict[int, dict]:
        """``{页号: {raw, tag, source, fixed, updated_at}}``，页号从 0 起。"""
        if not self.is_open:
            return {}
        try:
            return self._repo.load_scan_results()
        except Exception:  # noqa: BLE001 - 老项目可能还没有这张表
            return {}

    def save_scan_rows(self, rows: list[dict]) -> int:
        """把识别结果写进项目（一次事务），返回写入条数。"""
        if not self.is_open or not rows:
            return 0
        try:
            self._repo.write_scan_results(rows)
            self._mark_dirty()
            return len(rows)
        except Exception:  # noqa: BLE001
            return 0

    def clear_scan_rows(self) -> None:
        if not self.is_open:
            return
        try:
            self._repo.clear_scan_results()
            self._mark_dirty()
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------- 数据匹配标注（第 1、5 步）
    def import_dataset(
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
        """记一次导入并返回版本号（0 = 项目还没打开）。"""
        if not self.is_open or self._repo is None:
            return 0
        try:
            import_id = self._repo.add_data_import(
                path, fingerprint, sheet, row_count, key_column, display_columns, template, note
            )
            self._mark_dirty()
            return import_id
        except Exception:  # noqa: BLE001
            return 0

    def last_data_import(self) -> dict | None:
        if not self.is_open or self._repo is None:
            return None
        try:
            return self._repo.last_data_import()
        except Exception:  # noqa: BLE001
            return None

    def data_imports(self, limit: int = 200) -> list[dict]:
        """最近的数据导入版本（新的在前），用来分清同一页上的几批标注。"""
        if not self.is_open or self._repo is None:
            return []
        try:
            return self._repo.list_data_imports(limit)
        except Exception:  # noqa: BLE001
            return []

    def load_match_annotations(self) -> list[dict]:
        if not self.is_open or self._repo is None:
            return []
        try:
            return self._repo.load_match_annotations()
        except Exception:  # noqa: BLE001
            return []

    def save_match_annotations(self, rows: list[dict], import_id: int | None = None) -> int:
        if not self.is_open or self._repo is None or not rows:
            return 0
        try:
            self._repo.write_match_annotations(rows, import_id)
            self._mark_dirty()
            return len(rows)
        except Exception:  # noqa: BLE001
            return 0

    def delete_match_annotations(self, ids: list[str]) -> None:
        if not self.is_open or self._repo is None or not ids:
            return
        try:
            self._repo.delete_match_annotations(ids)
        except Exception:  # noqa: BLE001
            pass

    def source_info(self) -> dict:
        return dict(self._source)

    def check_correspondence(self) -> dict:
        """检查「当前 PDF」和「它的项目文件」对不对得上。

        返回 ``{"state": ok|none|mismatch|missing, "path":…, "message":…}``。

        场景：用户把 PDF 换成了另一版（改过、重出图），旁边的 ``.qpdf``
        里存的还是旧指纹——这种时候如果闷头加载，标注会画到错的页上。
        """
        if self._doc is None:
            return {"state": "none", "path": "", "message": "还没有打开 PDF。"}
        path = os.path.isfile(self._default_path) and self._default_path or ""
        if self.is_open:
            path = self._repo.path if self._repo else path

        stored = None
        if self.is_open and self._repo is not None:
            stored = self._repo.source_info()
        elif path:
            repo = ProjectRepository(path)
            try:
                repo.open()
                stored = repo.source_info()
            except ProjectError:
                stored = None
            finally:
                repo.close()

        if not stored:
            return {
                "state": "none" if not path else "missing",
                "path": path,
                "message": (
                    "这个 PDF 还没有项目文件（第一次标注时会自动建）。"
                    if not path
                    else f"项目文件读不了或已损坏：{path}"
                ),
            }

        actual = self._source or {}
        same = (
            stored.get("fingerprint") == actual.get("fingerprint")
            and int(stored.get("size", -1)) == int(actual.get("size", -2))
        )
        if same:
            return {
                "state": "ok",
                "path": path,
                "message": (
                    f"对得上。\n项目：{path}\nPDF：{actual.get('path', '')}\n"
                    f"大小：{int(actual.get('size', 0)) / 1048576:.1f} MB"
                ),
            }
        return {
            "state": "mismatch",
            "path": path,
            "message": (
                f"对不上——PDF 被换过或改过。\n项目：{path}\n"
                f"记录的大小：{int(stored.get('size', 0)) / 1048576:.1f} MB，"
                f"现在：{int(actual.get('size', 0)) / 1048576:.1f} MB\n\n"
                "为了不把标注画到错的页上，这种情况不会静默载入；"
                "打开时会提示是否继续，继续前会自动备份原项目。"
            ),
        }
